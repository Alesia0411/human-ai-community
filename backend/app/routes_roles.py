# 文件职责：角色管理、公开目录、模型连接与屏蔽接口；凭证仅在服务端加密保存。
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, object_session

from .config import settings
from .db import get_db
from .models import Connection, Relation, Role
from .policy import blocked, fail, owned_role, visible_role
from .schemas import ConnectionInput, RoleInput
from .security import current, decrypt, encrypt

router = APIRouter()


# role_view：按调用场景序列化角色，控制手动人设等私有字段的暴露。
def role_view(r, private=False):
    data = {
        "id": r.id,
        "name": r.name,
        "bio": r.bio,
        "visibility": r.visibility,
        "ai_enabled": r.ai_enabled,
        "public_ai": r.public_ai,
        "created_at": r.created_at,
    }
    if private:
        data.update(persona=r.persona, version=r.version, provider_scope=r.provider_scope)
    from .r2_policy import config_values

    session = object_session(r)
    data["enabled_tools"] = config_values(session, r.id)["enabled_tools"] if session else []
    return data


# my_roles：返回当前账号拥有且未删除的角色。
@router.get("/me/roles")
def my_roles(a=Depends(current), db: Session = Depends(get_db)):
    return [
        role_view(r, True)
        for r in db.scalars(
            select(Role).where(Role.owner_id == a.id, Role.deleted.is_(False)).order_by(Role.created_at)
        )
    ]


# create_role：检查角色数量与提供商配置，创建当前账号的新角色。
@router.post("/me/roles", status_code=201)
def create_role(data: RoleInput, a=Depends(current), db: Session = Depends(get_db)):
    from .models import Account

    db.scalar(select(Account).where(Account.id == a.id).with_for_update())
    if (
        db.scalar(
            select(func.count()).select_from(Role).where(Role.owner_id == a.id, Role.deleted.is_(False))
        )
        >= 5
    ):
        fail("ROLE_LIMIT", 409)
    r = Role(owner_id=a.id, **data.model_dump(exclude={"version"}))
    db.add(r)
    db.commit()
    return role_view(r, True)


# edit_role：以版本检查防止覆盖并发编辑，区分设定变更与权限变更。
@router.patch("/roles/{role_id}")
def edit_role(role_id: str, data: RoleInput, a=Depends(current), db: Session = Depends(get_db)):
    r = owned_role(db, a.id, role_id, True)
    if data.version != r.version:
        fail("VERSION_CONFLICT", 409)
    if any(
        getattr(r, key) != getattr(data, key)
        for key in ("visibility", "ai_enabled", "public_ai", "provider_scope")
    ):
        r.policy_version += 1
    for key, value in data.model_dump(exclude={"version"}).items():
        setattr(r, key, value)
    r.version += 1
    db.commit()
    return role_view(r, True)


# delete_role：逻辑删除角色并撤销 AI 能力，使旧授权不可继续使用。
@router.delete("/roles/{role_id}")
def delete_role(role_id: str, a=Depends(current), db: Session = Depends(get_db)):
    r = owned_role(db, a.id, role_id, True)
    r.deleted, r.ai_enabled, r.public_ai = True, False, False
    r.policy_version += 1
    from .models import KnowledgeDocument, PersonaCandidate

    db.execute(update(KnowledgeDocument).where(KnowledgeDocument.role_id == role_id).values(deleted=True))
    db.execute(
        update(PersonaCandidate).where(PersonaCandidate.role_id == role_id).values(valid=False, fields={})
    )
    db.commit()
    return {"ok": True}


# roles：分页搜索当前账号可见的公开角色，并排除屏蔽关系。
@router.get("/roles")
def roles(
    before: float | None = None,
    q: str = Query("", max_length=80),
    a=Depends(current),
    db: Session = Depends(get_db),
):
    query = select(Role).where(Role.visibility == "PUBLIC", Role.deleted.is_(False))
    if before is not None:
        query = query.where(Role.created_at < before)
    if q:
        query = query.where(Role.name.contains(q, autoescape=True))
    rows = list(db.scalars(query.order_by(Role.created_at.desc()).limit(30)))
    return {
        "items": [role_view(r) for r in rows if not blocked(db, a.id, r.owner_id)],
        "next": rows[-1].created_at if len(rows) == 30 else None,
    }


# public_role：检查角色可见性后返回公开展示信息。
@router.get("/roles/{role_id}")
def public_role(role_id: str, a=Depends(current), db: Session = Depends(get_db)):
    return role_view(visible_role(db, a.id, role_id))


# connections：列出当前账号连接的非密钥信息。
@router.get("/me/connections")
def connections(a=Depends(current), db: Session = Depends(get_db)):
    return [
        {"id": c.id, "provider": c.provider, "model": c.model, "key": "configured"}
        for c in db.scalars(
            select(Connection).where(Connection.owner_id == a.id, Connection.deleted.is_(False))
        )
    ]


# add_connection：验证提供商与模型配置，加密 API Key 后保存到当前账号。
@router.post("/me/connections", status_code=201)
def add_connection(data: ConnectionInput, a=Depends(current), db: Session = Depends(get_db)):
    if data.provider == "mock":
        if not settings().demo_mode:
            fail("MOCK_DISABLED", 422)
    elif data.provider not in settings().providers or (data.provider != "ollama" and not data.api_key):
        fail("PROVIDER_OR_KEY_INVALID", 422)
    c = Connection(
        owner_id=a.id, provider=data.provider, model=data.model, encrypted_key=encrypt(data.api_key)
    )
    db.add(c)
    db.commit()
    return {"id": c.id, "provider": c.provider, "model": c.model, "key": "configured"}


# delete_connection：删除自己的连接并清除密钥，使后续任务不能继续使用。
@router.delete("/me/connections/{connection_id}")
def delete_connection(connection_id: str, a=Depends(current), db: Session = Depends(get_db)):
    c = db.get(Connection, connection_id)
    if not c or c.owner_id != a.id:
        fail()
    c.deleted = True
    db.commit()
    return {"ok": True}


# test_connection：使用本人的连接检查提供商可用性，外部地址取自服务端配置。
@router.post("/me/connections/{connection_id}/test")
async def test_connection(connection_id: str, a=Depends(current), db: Session = Depends(get_db)):
    import httpx

    c = db.get(Connection, connection_id)
    if not c or c.deleted or c.owner_id != a.id:
        fail()
    if c.provider == "mock":
        return {"ok": settings().demo_mode, "mode": "mock", "note": "演示连接，不代表真实模型验证"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            r = await client.get(
                settings().providers[c.provider] + "/models",
                headers={"Authorization": "Bearer " + (decrypt(c.encrypted_key) or "ollama")},
            )
            r.raise_for_status()
            available = [m.get("id") for m in r.json().get("data", [])]
            return {
                "ok": c.model in available,
                "mode": "models-endpoint",
                "note": "仅验证凭证与模型列表；不等于已完成真实生成",
            }
    except (httpx.HTTPError, ValueError, KeyError):
        fail("CONNECTION_TEST_FAILED", 502)


# block_role：将目标角色映射为账号屏蔽，并使相关互动权限失效。
@router.put("/roles/{role_id}/block")
def block_role(role_id: str, a=Depends(current), db: Session = Depends(get_db)):
    r = db.get(Role, role_id)
    if not r or r.owner_id == a.id:
        fail()
    if not db.scalar(
        select(Relation).where(
            Relation.owner_id == a.id, Relation.target_id == r.owner_id, Relation.kind == "BLOCK"
        )
    ):
        db.add(Relation(owner_id=a.id, target_id=r.owner_id, kind="BLOCK"))
        db.commit()
    return {"ok": True}


# unblock_role：移除当前账号对目标角色所属账号的屏蔽关系。
@router.delete("/roles/{role_id}/block")
def unblock_role(role_id: str, a=Depends(current), db: Session = Depends(get_db)):
    r = db.get(Role, role_id)
    if not r:
        fail()
    row = db.scalar(
        select(Relation).where(
            Relation.owner_id == a.id, Relation.target_id == r.owner_id, Relation.kind == "BLOCK"
        )
    )
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}
