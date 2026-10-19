# 文件职责：真人与 AI 聊天 HTTP 接口：按参与者隔离历史、幂等发送、清空/隐藏、任务查询和票据签发。
import secrets
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from .chat_memory import reset_summaries, try_sync
from .config import settings
from .db import get_db
from .models import (
    Asset,
    ChatMemory,
    Connection,
    Conversation,
    KnowledgeDocument,
    Message,
    Role,
    Run,
    SocketTicket,
)
from .policy import PayerPolicy, can_send, conversation, fail, owned_role, visible_role
from .schemas import ConversationInput, SendMessage
from .security import current, digest

router = APIRouter()


# message_view：序列化消息供客户端显示，撤回内容不继续暴露原正文。
def message_view(m):
    return {
        "id": m.id,
        "seq": m.seq,
        "role_id": m.role_id,
        "body": "[消息已撤回]" if m.retracted else m.body,
        "asset_id": None if m.retracted else m.asset_id,
        "actor": m.actor,
        "run_id": m.run_id,
        "created_at": m.created_at,
    }


# run_view：构造任务状态和容量诊断视图，不返回内部模型 Key。
def run_view(r):
    return {
        "context_usage": r.context.get("context_usage"),
        "id": r.id,
        "conversation_id": r.conversation_id,
        "status": r.status,
        "used": r.used,
        "reserved": r.reserved,
        "error": r.error,
        "partial": r.partial,
        "created_at": r.created_at,
        "purpose": r.purpose,
        "result": r.result,
        "duration_ms": round((r.finished_at - r.started_at) * 1000, 1)
        if r.finished_at is not None and r.started_at is not None
        else None,
        "first_token_ms": round((r.first_token_at - r.started_at) * 1000, 1)
        if r.first_token_at is not None and r.started_at is not None
        else None,
    }


# cleared_seq：取得指定账号的历史清空序号，旧消息在该账号侧不可再显示。
def cleared_seq(c, account_id):
    return c.creator_cleared_seq if c.creator_id == account_id else c.peer_cleared_seq


# conversation_hidden：根据账号侧隐藏状态判断会话是否应从列表移除。
def conversation_hidden(c, account_id):
    return c.creator_hidden if c.creator_id == account_id else c.peer_hidden


# conv_view：生成会话列表/详情展示字段，并附加当前账号可见的状态。
def conv_view(db, c, account_id):
    target = db.get(Role, c.target_role_id if c.creator_id == account_id else c.source_role_id)
    read = c.creator_read if c.creator_id == account_id else c.peer_read
    return {
        "id": c.id,
        "kind": c.kind,
        "source_role_id": c.source_role_id,
        "target_role_id": c.target_role_id,
        "title": target.name if target and not target.deleted else "已删除角色",
        "seq": c.seq,
        "cleared_seq": cleared_seq(c, account_id),
        "unread": max(0, c.seq - read),
        "active_run": c.active_run,
        "definition_version": c.definition_version,
        "grants": c.grants if c.kind == "AI_DIRECT" else {},
        "connection_id": c.connection_id if c.kind == "AI_DIRECT" else None,
        "memory_enabled": c.memory_enabled,
    }


# create_conversation：校验角色、关系和 AI 连接归属，创建或复用允许的会话。
@router.post("/conversations", status_code=201)
def create_conversation(data: ConversationInput, a=Depends(current), db: Session = Depends(get_db)):
    owned_role(db, a.id, data.source_role_id)
    target = visible_role(db, a.id, data.target_role_id)
    c = Conversation(
        kind=data.kind,
        creator_id=a.id,
        peer_account_id=target.owner_id if data.kind == "HUMAN_DIRECT" else "",
        source_role_id=data.source_role_id,
        target_role_id=target.id,
        connection_id=data.connection_id,
        persona_snapshot=target.persona if data.kind == "AI_DIRECT" else "",
        definition_version=target.version,
    )
    if data.kind == "AI_DIRECT":
        conn = db.get(Connection, data.connection_id) if data.connection_id else None
        PayerPolicy.visitor(db, a.id, c, conn)
        if conn.provider not in target.provider_scope:
            fail("PROVIDER_SCOPE_MISMATCH", 403)
        can_send(db, c)
        from .r2_policy import effective_persona

        c.persona_snapshot, c.candidate_id = effective_persona(db, target, conn.provider)
    else:
        # Return the existing human conversation for this ordered pair.
        old = db.scalar(
            select(Conversation).where(
                Conversation.kind == "HUMAN_DIRECT",
                or_(
                    and_(
                        Conversation.source_role_id == data.source_role_id,
                        Conversation.target_role_id == target.id,
                    ),
                    and_(
                        Conversation.source_role_id == target.id,
                        Conversation.target_role_id == data.source_role_id,
                    ),
                ),
            )
        )
        if old:
            old = conversation(db, a.id, old.id, True)
            if old.creator_id == a.id:
                old.creator_hidden = False
            else:
                old.peer_hidden = False
            db.commit()
            return conv_view(db, old, a.id)
    db.add(c)
    db.commit()
    return conv_view(db, c, a.id)


# conversations：查询当前账号有权看到且未隐藏的会话列表。
@router.get("/me/conversations")
def conversations(a=Depends(current), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Conversation)
        .where(
            or_(
                and_(Conversation.creator_id == a.id, Conversation.creator_hidden.is_(False)),
                and_(Conversation.kind == "HUMAN_DIRECT", Conversation.peer_account_id == a.id,
                     Conversation.peer_hidden.is_(False)),
            )
        )
        .order_by(Conversation.created_at.desc())
    )
    return [conv_view(db, c, a.id) for c in rows]


# remove_conversation：按真人/AI 规则清理历史并移除当前账号会话入口，不删除角色。
@router.delete("/conversations/{cid}")
def remove_conversation(cid: str, a=Depends(current), db: Session = Depends(get_db)):
    pre = conversation(db, a.id, cid)
    db.scalar(select(Role).where(Role.id == pre.target_role_id).with_for_update())
    c = conversation(db, a.id, cid, True)
    clear_history(db, c, a.id)
    if c.creator_id == a.id:
        c.creator_hidden, c.creator_read = True, c.seq
    else:
        c.peer_hidden, c.peer_read = True, c.seq
    db.commit()
    try_sync(cid)
    return {"ok": True}


# messages：按顺序游标分页返回清空边界之后的可见消息。
@router.get("/conversations/{cid}/messages")
def messages(
    cid: str,
    after: int | None = None,
    before: int | None = None,
    a=Depends(current),
    db: Session = Depends(get_db),
):
    c = conversation(db, a.id, cid)
    query = select(Message).where(Message.conversation_id == cid, Message.seq > cleared_seq(c, a.id))
    if after is not None:
        query = query.where(Message.seq > after).order_by(Message.seq)
    else:
        if before is not None:
            query = query.where(Message.seq < before)
        query = query.order_by(Message.seq.desc())
    rows = list(db.scalars(query.limit(100)))
    if after is None:
        rows.reverse()
    return [message_view(m) for m in rows]


# send：验证发言身份并按 client_message_id 去重；真人写消息，AI 另预留预算创建 Run。
@router.post("/conversations/{cid}/messages", status_code=201)
def send(
    cid: str,
    data: SendMessage,
    background: BackgroundTasks,
    a=Depends(current),
    db: Session = Depends(get_db),
):
    pre = conversation(db, a.id, cid)
    db.scalar(select(Role).where(Role.id == pre.target_role_id).with_for_update())
    c = conversation(db, a.id, cid, True)
    existing = db.scalar(
        select(Message).where(
            Message.conversation_id == cid,
            Message.sender_id == a.id,
            Message.client_id == data.client_message_id,
        )
    )
    if existing:
        return message_view(existing)
    target = can_send(db, c)
    if not data.body and not data.asset_id:
        fail("EMPTY_MESSAGE", 422)
    # 发言身份来自会话固定双方角色，不随页面当前选中角色而任意改变。
    sender_role = c.source_role_id if c.creator_id == a.id else c.target_role_id
    owned_role(db, a.id, sender_role)
    if data.asset_id:
        asset = db.get(Asset, data.asset_id)
        if not asset or asset.owner_id != a.id or asset.conversation_id != cid:
            fail()
    run = None
    if c.kind == "AI_DIRECT":
        if c.active_run:
            fail("RUN_IN_PROGRESS", 409)
        conn = db.get(Connection, c.connection_id) if c.connection_id else None
        # AI 私聊由访客使用自己的连接付费，目标角色拥有者不能被隐式扣费。
        payer = PayerPolicy.visitor(db, a.id, c, conn)
        if conn.provider not in target.provider_scope:
            fail("PROVIDER_SCOPE_MISMATCH", 403)
        history = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == cid, Message.retracted.is_(False))
                .order_by(Message.seq.desc())
                .limit(4)
            )
        )
        # Conservative UTF-8 byte upper bound + output cap and protocol overhead.
        reserve = (
            sum(len(m.body.encode()) for m in history)
            + len(data.body.encode())
            + len(c.persona_snapshot.encode())
            + sum(len(s.encode()) for s in db.scalars(select(ChatMemory.summary).where(ChatMemory.conversation_id == cid)))
            + 2048
        )
        if reserve > 32000:
            fail("CONTEXT_LIMIT_NEW_SESSION_REQUIRED", 422)
        # 预留不少于会话本轮预算；之后每次模型调用还会检查剩余预算和真实上下文容量。
        reserve = max(reserve, c.grants.get("max_tokens", 16000))
        if payer.used + payer.reserved + reserve > payer.budget:
            fail("BUDGET_EXCEEDED", 409)
        payer.reserved += reserve
        run = Run(
            conversation_id=cid,
            payer_id=payer.id,
            connection_id=conn.id,
            role_id=target.id,
            policy_version=target.policy_version,
            reserved=reserve,
            deadline=time.time() + settings().run_timeout + 300,
            # 任务保存当前会话授权版本，清空或更新配置后旧任务会被拒绝。
            context={"session_version": c.policy_version},
        )
        db.add(run)
        db.flush()
        c.active_run = run.id
    c.seq += 1
    m = Message(
        conversation_id=cid,
        seq=c.seq,
        sender_id=a.id,
        role_id=sender_role,
        client_id=data.client_message_id,
        body=data.body,
        asset_id=data.asset_id,
        run_id=run.id if run else None,
    )
    db.add(m)
    c.files_dirty = True
    # A new human message makes the existing conversation visible to both participants.
    # An idempotent retry returns above without resurrecting a removed conversation.
    if c.kind == "HUMAN_DIRECT":
        c.creator_hidden = c.peer_hidden = False
    db.commit()
    try_sync(cid)
    return message_view(m)


# clear_messages：清空当前账号的会话记录入口，调用共用清理逻辑并同步文件。
@router.delete("/conversations/{cid}/messages")
def clear_messages(cid: str, a=Depends(current), db: Session = Depends(get_db)):
    pre = conversation(db, a.id, cid)
    # Match send/finish lock order, so a completed reply cannot race with the reset.
    db.scalar(select(Role).where(Role.id == pre.target_role_id).with_for_update())
    c = conversation(db, a.id, cid, True)
    clear_history(db, c, a.id)
    db.commit()
    try_sync(cid)
    return {"ok": True, "cleared_seq": cleared_seq(c, a.id)}


# clear_history：按会话类型处理历史与摘要，并维护版本/清空边界以阻止旧后台结果回写。
def clear_history(db, c, account_id):
    cid = c.id
    if c.kind == "AI_DIRECT":
        if c.active_run:
            fail("RUN_IN_PROGRESS", 409)
        from .r2_policy import invalidate_sources

        for mid in db.scalars(select(Message.id).where(Message.conversation_id == cid)):
            invalidate_sources(db, "messages", mid)
        db.execute(delete(Message).where(Message.conversation_id == cid))
        # Conversation-scoped memory must not repopulate the cleared AI context.
        for doc in db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.conversation_id == cid)):
            doc.deleted, doc.body = True, ""
        for run in db.scalars(select(Run).where(Run.conversation_id == cid, Run.purpose == "CHAT")):
            run.partial, run.result = "", {}
        c.policy_version += 1
        c.cleared_at = time.time()
        reset_summaries(db, c)
    c.files_dirty = True
    if c.creator_id == account_id:
        # 真人消息不必全表删除；将本人的可见起点移到当前序号，不影响对方。
        c.creator_cleared_seq = c.creator_read = c.seq
    else:
        c.peer_cleared_seq = c.peer_read = c.seq


# read：更新当前账号已读序号，不改变对方阅读状态。
@router.put("/conversations/{cid}/read")
def read(cid: str, a=Depends(current), db: Session = Depends(get_db)):
    c = conversation(db, a.id, cid, True)
    if c.creator_id == a.id:
        c.creator_read = c.seq
    else:
        c.peer_read = c.seq
    db.commit()
    return {"ok": True}


# retract：验证发送方后撤回消息，同时失效相关学习来源及聊天摘要。
@router.post("/messages/{mid}/retract")
def retract(mid: str, a=Depends(current), db: Session = Depends(get_db)):
    m = db.get(Message, mid)
    if not m or m.sender_id != a.id:
        fail()
    db.scalar(select(Role).where(Role.id == m.role_id).with_for_update())
    c = conversation(db, a.id, m.conversation_id, True)
    m.retracted, m.body, m.asset_id = True, "", None
    from .r2_policy import invalidate_sources

    invalidate_sources(db, "messages", mid)
    reset_summaries(db, c)
    c = db.get(Conversation, m.conversation_id)
    if c.active_run:
        run = db.get(Run, c.active_run)
        run.status = "CANCEL_REQUESTED"
    db.commit()
    try_sync(c.id)
    return {"ok": True}


# my_runs：只返回当前付款账号的近期任务，不让角色拥有者查看访客任务。
@router.get("/me/runs")
def my_runs(a=Depends(current), db: Session = Depends(get_db)):
    return [
        run_view(r)
        for r in db.scalars(select(Run).where(Run.payer_id == a.id).order_by(Run.created_at.desc()).limit(50))
    ]


# get_run：按付款账号验证权限后返回单个任务状态。
@router.get("/runs/{rid}")
def get_run(rid: str, a=Depends(current), db: Session = Depends(get_db)):
    r = db.get(Run, rid)
    if not r or r.payer_id != a.id:
        fail()
    if r.conversation_id:
        conversation(db, a.id, r.conversation_id)
    return run_view(r)


# cancel：要求当前付款方有权操作，设置取消状态以阻止后续发布。
@router.post("/runs/{rid}/cancel")
def cancel(rid: str, a=Depends(current), db: Session = Depends(get_db)):
    r = db.get(Run, rid)
    if not r or r.payer_id != a.id:
        fail()
    if r.status in ("QUEUED", "RUNNING", "PAUSED"):
        r.status = "CANCEL_REQUESTED"
        r.partial = ""
    db.commit()
    return {"ok": True}


# ticket：为已授权会话签发短期一次性 WebSocket 票据。
@router.post("/conversations/{cid}/socket-ticket")
def ticket(cid: str, request: Request, a=Depends(current), db: Session = Depends(get_db)):
    conversation(db, a.id, cid)
    token = secrets.token_urlsafe(32)
    db.add(
        SocketTicket(
            token_hash=digest(token),
            account_id=a.id,
            session_id=request.state.session_id,
            conversation_id=cid,
            expires=time.time() + 30,
        )
    )
    db.commit()
    return {"ticket": token}
