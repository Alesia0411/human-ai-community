# 文件职责：社区动态、评论与点赞接口；根据角色公开状态和账号屏蔽规则过滤内容。
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Asset, Comment, Post, Relation, Role
from .policy import blocked, fail, owned_role, visible_role
from .routes_roles import role_view
from .schemas import CommentInput, PostInput
from .security import current

router = APIRouter()


# readable_post：检查动态及角色当前可读性，供动态与评论入口复用。
def readable_post(db, account_id, post_id):
    p = db.get(Post, post_id)
    if not p or p.deleted:
        fail()
    visible_role(db, account_id, p.role_id)
    return p


# posts：按游标分页读取可见动态及关联展示信息。
@router.get("/posts")
def posts(
    before: float | None = None, role_id: str | None = None, a=Depends(current), db: Session = Depends(get_db)
):
    q = (
        select(Post)
        .join(Role, Role.id == Post.role_id)
        .where(Post.deleted.is_(False), Role.deleted.is_(False), Role.visibility == "PUBLIC")
    )
    if before is not None:
        q = q.where(Post.created_at < before)
    if role_id:
        q = q.where(Post.role_id == role_id)
    rows = list(db.scalars(q.order_by(Post.created_at.desc()).limit(30)))
    items = []
    for p in rows:
        if blocked(db, a.id, p.owner_id):
            continue
        items.append(
            {
                "id": p.id,
                "body": p.body,
                "asset_id": p.asset_id,
                "role": role_view(db.get(Role, p.role_id)),
                "mine": p.owner_id == a.id,
                "actor": p.actor,
                "created_at": p.created_at,
                "likes": db.scalar(
                    select(func.count())
                    .select_from(Relation)
                    .where(Relation.target_id == p.id, Relation.kind == "LIKE")
                ),
            }
        )
    return {"items": items, "next": rows[-1].created_at if len(rows) == 30 else None}


# post：验证当前账号拥有发言角色后发布真人动态，可关联授权图片。
@router.post("/posts", status_code=201)
def post(data: PostInput, a=Depends(current), db: Session = Depends(get_db)):
    role = owned_role(db, a.id, data.role_id)
    if role.visibility != "PUBLIC":
        fail("PUBLIC_ROLE_REQUIRED", 422)
    if data.asset_id:
        asset = db.get(Asset, data.asset_id)
        if not asset or asset.owner_id != a.id or asset.conversation_id:
            fail()
    p = Post(owner_id=a.id, **data.model_dump())
    db.add(p)
    db.commit()
    return {"id": p.id}


# delete_post：仅允许拥有者逻辑删除动态，并撤销相关学习来源。
@router.delete("/posts/{post_id}")
def delete_post(post_id: str, a=Depends(current), db: Session = Depends(get_db)):
    p = db.get(Post, post_id)
    if not p or p.owner_id != a.id:
        fail()
    db.scalar(select(Role).where(Role.id == p.role_id).with_for_update())
    p.deleted = True
    from .r2_policy import invalidate_sources

    invalidate_sources(db, "posts", post_id)
    for child in db.scalars(select(Comment).where(Comment.post_id == post_id)):
        invalidate_sources(db, "comments", child.id)
    db.commit()
    return {"ok": True}


# comments：列出指定可见动态下当前用户可查看的评论。
@router.get("/posts/{post_id}/comments")
def comments(post_id: str, a=Depends(current), db: Session = Depends(get_db)):
    readable_post(db, a.id, post_id)
    result = []
    for c in db.scalars(
        select(Comment)
        .where(Comment.post_id == post_id, Comment.deleted.is_(False))
        .order_by(Comment.created_at)
        .limit(100)
    ):
        r = db.get(Role, c.role_id)
        if r and not r.deleted and not blocked(db, a.id, c.owner_id):
            result.append(
                {
                    "id": c.id,
                    "body": c.body,
                    "role_name": r.name,
                    "mine": c.owner_id == a.id,
                    "actor": c.actor,
                    "parent_id": c.parent_id,
                    "role_id": c.role_id,
                }
            )
    return result


# comment：以本人角色发表真人评论，不能伪造 AI actor。
@router.post("/posts/{post_id}/comments", status_code=201)
def comment(post_id: str, data: CommentInput, a=Depends(current), db: Session = Depends(get_db)):
    readable_post(db, a.id, post_id)
    owned_role(db, a.id, data.role_id)
    c = Comment(post_id=post_id, owner_id=a.id, **data.model_dump())
    db.add(c)
    db.commit()
    return {"id": c.id}


# delete_comment：删除自己的评论并失效以它为证据的人设候选。
@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str, a=Depends(current), db: Session = Depends(get_db)):
    c = db.get(Comment, comment_id)
    if not c or c.owner_id != a.id:
        fail()
    db.scalar(select(Role).where(Role.id == c.role_id).with_for_update())
    c.deleted = True
    from .r2_policy import invalidate_sources

    invalidate_sources(db, "comments", comment_id)
    db.commit()
    return {"ok": True}


# like：为当前账号添加点赞，已有点赞直接返回；此接口不执行取消点赞。
@router.put("/posts/{post_id}/like")
def like(post_id: str, a=Depends(current), db: Session = Depends(get_db)):
    readable_post(db, a.id, post_id)
    existing = db.scalar(
        select(Relation).where(
            Relation.owner_id == a.id, Relation.target_id == post_id, Relation.kind == "LIKE"
        )
    )
    if not existing:
        db.add(Relation(owner_id=a.id, target_id=post_id, kind="LIKE"))
        db.commit()
    return {"ok": True}
