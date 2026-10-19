# 文件职责：基础业务权限：角色归属与可见性、账号屏蔽、会话参与者和访客付款连接。
from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from .models import Account, Conversation, Relation, Role


# fail：抛出统一 HTTP 错误；默认隐藏不存在与无权访问的区别。
def fail(code="NOT_FOUND", status=404):
    raise HTTPException(status, code)


# owned_role：查询当前账号拥有且未删除的角色，可选择加锁并刷新 ORM 缓存。
def owned_role(db, account_id, role_id, lock=False):
    query = select(Role).where(Role.id == role_id, Role.owner_id == account_id, Role.deleted.is_(False))
    role = db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if not role:
        fail()
    return role


# blocked：检查任一方向的账号屏蔽关系，返回布尔值。
def blocked(db, a, b):
    return (
        db.scalar(
            select(Relation.id).where(
                Relation.kind == "BLOCK",
                or_(
                    and_(Relation.owner_id == a, Relation.target_id == b),
                    and_(Relation.owner_id == b, Relation.target_id == a),
                ),
            )
        )
        is not None
    )


# visible_role：允许本人查看私有角色，其他账号仅可访问公开且未屏蔽的角色。
def visible_role(db, account_id, role_id):
    role = db.get(Role, role_id)
    if (
        not role
        or role.deleted
        or (role.visibility != "PUBLIC" and role.owner_id != account_id)
        or blocked(db, account_id, role.owner_id)
    ):
        fail()
    return role


# conversation：限制会话访问者：AI 会话仅创建者，真人会话允许双方；可选加锁。
def conversation(db, account_id, conversation_id, lock=False):
    q = select(Conversation).where(Conversation.id == conversation_id)
    item = db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)
    if not item or (
        account_id != item.creator_id
        and not (item.kind == "HUMAN_DIRECT" and account_id == item.peer_account_id)
    ):
        fail()
    return item


# can_send：检查角色存在、屏蔽关系及 AI 对话授权，返回目标角色。
def can_send(db, conv):
    source, target = db.get(Role, conv.source_role_id), db.get(Role, conv.target_role_id)
    if not source or not target or source.deleted or target.deleted:
        fail("ROLE_UNAVAILABLE", 409)
    if blocked(db, source.owner_id, target.owner_id):
        fail("INTERACTION_BLOCKED", 403)
    if conv.kind == "AI_DIRECT":
        if not target.ai_enabled or (
            target.owner_id != conv.creator_id and (target.visibility != "PUBLIC" or not target.public_ai)
        ):
            fail("AI_UNAVAILABLE", 409)
    return target


# PayerPolicy：付款主体规则；角色所有权不等于有权使用该角色拥有者的 Key。
class PayerPolicy:
    # account：锁定有效付款账号，供后续预算预留和结算使用。
    @staticmethod
    def account(db, account_id):
        account = db.scalar(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not account or not account.active:
            fail()
        return account

    # visitor：要求 AI 会话创建者使用自己的未删除模型连接，再取得付款账号锁。
    @staticmethod
    def visitor(db, account_id, conv, connection):
        if conv.kind != "AI_DIRECT" or conv.creator_id != account_id:
            fail()
        if not connection or connection.deleted or connection.owner_id != account_id:
            fail("CONNECTION_REQUIRED", 422)
        return PayerPolicy.account(db, account_id)
