# 文件职责：主动交互的接收许可、计划、草稿发布及后续付款交接；调度不意味着绕过接收方授权。
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, get_db
from .models import (
    ActionPlan,
    AgentConfig,
    Comment,
    Conversation,
    Message,
    Post,
    ProactiveDelivery,
    Reception,
    Role,
    Run,
)
from .policy import fail, owned_role, visible_role
from .r2_policy import assert_proactive, config_values, connection_for, effective_persona, reserve_run
from .routes_r2 import enqueue
from .schemas import Input
from .security import current

router = APIRouter()


# ReceptionInput：接收许可请求：指定发送角色和开关。
class ReceptionInput(Input):
    sender_role_id: str
    enabled: bool


# reception：验证接收角色归属，保存对指定发送角色的主动私聊接收许可。
@router.put("/roles/{rid}/reception")
def reception(rid: str, data: ReceptionInput, a=Depends(current), db: Session = Depends(get_db)):
    # Lock sender as well: revocation is serialized against proactive publication.
    visible_role(db, a.id, data.sender_role_id)
    db.scalar(select(Role).where(Role.id == data.sender_role_id).with_for_update())
    owned_role(db, a.id, rid, True)
    row = db.scalar(
        select(Reception).where(
            Reception.recipient_role_id == rid, Reception.sender_role_id == data.sender_role_id
        )
    )
    if not row:
        row = Reception(recipient_role_id=rid, sender_role_id=data.sender_role_id)
        db.add(row)
    row.enabled = data.enabled
    db.commit()
    return {"ok": True}


# PlanInput：主动计划输入：限定类型、提示词长度、间隔和每日执行时段。
class PlanInput(Input):
    kind: Literal["DM", "POST", "COMMENT"]
    target_id: str = ""
    prompt: str = Field(min_length=1, max_length=1500)
    connection_id: str
    interval_hours: int = Field(default=24, ge=1, le=168)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=21, ge=1, le=24)


# create_plan：检查行为与连接授权、时段和计划数量，创建到期可调度计划。
@router.post("/roles/{rid}/plans")
def create_plan(rid: str, data: PlanInput, a=Depends(current), db: Session = Depends(get_db)):
    role = owned_role(db, a.id, rid, True)
    assert_proactive(db, role, data.kind, data.target_id)
    connection_for(db, a.id, data.connection_id, role.provider_scope)
    if data.end_hour <= data.start_hour:
        fail("INVALID_TIME_WINDOW", 422)
    count = len(
        list(db.scalars(select(ActionPlan.id).where(ActionPlan.role_id == rid, ActionPlan.enabled.is_(True))))
    )
    if count >= 3:
        fail("PLAN_LIMIT", 409)
    plan = ActionPlan(owner_id=a.id, role_id=rid, next_at=time.time(), **data.model_dump())
    db.add(plan)
    db.commit()
    return {"id": plan.id}


# plans：返回当前账号自己的计划摘要，不读取其他账号计划。
@router.get("/me/plans")
def plans(a=Depends(current), db: Session = Depends(get_db)):
    return [
        {
            "id": p.id,
            "kind": p.kind,
            "role_id": p.role_id,
            "target_id": p.target_id,
            "prompt": p.prompt,
            "next_at": p.next_at,
            "enabled": p.enabled,
        }
        for p in db.scalars(select(ActionPlan).where(ActionPlan.owner_id == a.id))
    ]


# disable_plan：停用计划并提升角色权限版本，使使用旧授权的任务不能继续发布。
@router.delete("/plans/{pid}")
def disable_plan(pid: str, a=Depends(current), db: Session = Depends(get_db)):
    plan = db.get(ActionPlan, pid)
    if not plan or plan.owner_id != a.id:
        fail()
    role = owned_role(db, a.id, plan.role_id, True)
    plan.enabled = False
    role.policy_version += 1
    db.commit()
    return {"ok": True}


# queue_plan：锁定到期计划并检查上海时区窗口，预留拥有者预算并创建 PROACTIVE Run。
def queue_plan(db, plan, *, scheduled=False):
    role = db.scalar(select(Role).where(Role.id == plan.role_id).with_for_update())
    plan = db.scalar(
        select(ActionPlan)
        .where(ActionPlan.id == plan.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not plan.enabled or plan.next_at > time.time():
        fail("PLAN_NOT_DUE", 409)
    if scheduled and time.time() - plan.next_at > max(0, settings().proactive_grace_seconds):
        # Sleep / outage does not cause a burst of old paid social actions.
        # 从当前时间推进计划，不循环追赶每次错过的历史时点。
        plan.next_at = time.time() + plan.interval_hours * 3600
        return None
    # 计划按北京时间的半开区间执行；end_hour 本身不包含在允许窗口内。
    hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    if not plan.start_hour <= hour < plan.end_hour:
        fail("OUTSIDE_SCHEDULE", 409)
    assert_proactive(db, role, plan.kind, plan.target_id)
    run = reserve_run(
        db,
        plan.owner_id,
        role,
        plan.connection_id,
        "PROACTIVE",
        {"plan_id": plan.id, "kind": plan.kind, "target_id": plan.target_id, "prompt": plan.prompt},
        16000,
    )
    plan.next_at = time.time() + plan.interval_hours * 3600
    return run


# trigger：手动排队到期计划并提交；仍检查到期和时段，不立即同步生成内容。
@router.post("/plans/{pid}/trigger")
def trigger(pid: str, background: BackgroundTasks, a=Depends(current), db: Session = Depends(get_db)):
    plan = db.get(ActionPlan, pid)
    if not plan or plan.owner_id != a.id:
        fail()
    run = queue_plan(db, plan)
    db.commit()
    enqueue(background, run.id)
    return {"run_id": run.id}


# deliveries：返回本人最近交付记录，不暴露接收者会话 ID、已读或后续回复。
@router.get("/me/deliveries")
def deliveries(a=Depends(current), db: Session = Depends(get_db)):
    # Deliberately has no recipient session id / read / reply / subsequent run data.
    return [
        {"id": d.id, "kind": d.kind, "body": d.body, "status": d.status, "created_at": d.created_at}
        for d in db.scalars(
            select(ProactiveDelivery)
            .where(ProactiveDelivery.owner_id == a.id)
            .order_by(ProactiveDelivery.created_at.desc())
            .limit(50)
        )
    ]


# publish：复查许可后将草稿写为 AI 私聊、动态或评论；提交事务由调用者负责。
def publish(db, delivery):
    if delivery.status == "SUBMITTED":
        return
    role = db.get(Role, delivery.role_id)
    assert_proactive(db, role, delivery.kind, delivery.target_id)
    if delivery.kind == "DM":
        recipient = db.get(Role, delivery.target_id)
        # 创建接收者拥有的 AI 会话；下面不会复制发送者的模型连接，后续回复用接收者连接。
        manual, candidate = effective_persona(db, role, "")
        c = Conversation(
            kind="AI_DIRECT",
            creator_id=recipient.owner_id,
            peer_account_id="",
            source_role_id=recipient.id,
            target_role_id=role.id,
            persona_snapshot=manual,
            candidate_id=candidate,
            definition_version=role.version,
            seq=1,
        )
        db.add(c)
        db.flush()
        db.add(
            Message(
                conversation_id=c.id,
                seq=1,
                sender_id="AGENT",
                role_id=role.id,
                client_id=delivery.id,
                actor="AI",
                body=delivery.body,
            )
        )
    elif delivery.kind == "POST":
        db.add(Post(owner_id=role.owner_id, role_id=role.id, body=delivery.body, actor="AI"))
    else:
        db.add(
            Comment(
                post_id=delivery.target_id,
                owner_id=role.owner_id,
                role_id=role.id,
                body=delivery.body,
                actor="AI",
            )
        )
    delivery.status = "SUBMITTED"


# approve：验证草稿归属与权限版本，人工批准发布并提交。
@router.post("/deliveries/{did}/publish")
def approve(did: str, a=Depends(current), db: Session = Depends(get_db)):
    d = db.get(ProactiveDelivery, did)
    if not d or d.owner_id != a.id:
        fail()
    role = owned_role(db, a.id, d.role_id, True)
    d = db.scalar(
        select(ProactiveDelivery)
        .where(ProactiveDelivery.id == did)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    run = db.get(Run, d.run_id)
    if run.policy_version != role.policy_version:
        fail("DRAFT_POLICY_CHANGED", 409)
    publish(db, d)
    db.commit()
    return {"ok": True}


# ReplyInput：AI 评论后续回复输入：指定本人的角色、连接和客户端去重标识。
class ReplyInput(Input):
    role_id: str
    body: str = Field(min_length=1, max_length=2000)
    connection_id: str
    client_message_id: str = Field(min_length=1, max_length=80)


# reply：保存用户对 AI 评论的回复，用当前认证用户自己的连接创建后续生成任务。
@router.post("/comments/{parent_id}/ai-reply")
def reply(
    parent_id: str,
    data: ReplyInput,
    background: BackgroundTasks,
    a=Depends(current),
    db: Session = Depends(get_db),
):
    parent = db.get(Comment, parent_id)
    if not parent or parent.deleted or parent.actor != "AI":
        fail()
    from .routes_social import readable_post

    readable_post(db, a.id, parent.post_id)
    target = db.scalar(select(Role).where(Role.id == parent.role_id).with_for_update())
    visible_role(db, a.id, target.id)
    owned_role(db, a.id, data.role_id)
    if not target.ai_enabled or not config_values(db, target.id)["allow_ai_comment_reply"]:
        fail("AI_COMMENT_REPLY_DISABLED", 409)
    # Stable trigger key stored in server context, serialized by target role lock.
    # 回复事件的稳定去重键；付费人取认证账号 a.id，不能让模型选择。
    event = f"comment:{a.id}:{data.client_message_id}"
    for existing in db.scalars(select(Run).where(Run.payer_id == a.id, Run.purpose == "COMMENT")):
        if existing.context.get("event") == event:
            return {"run_id": existing.id}
    c = Comment(
        post_id=parent.post_id, owner_id=a.id, role_id=data.role_id, body=data.body, parent_id=parent.id
    )
    db.add(c)
    db.flush()
    run = reserve_run(
        db,
        a.id,
        target,
        data.connection_id,
        "COMMENT",
        {"parent_id": parent.id, "reply_id": c.id, "event": event},
        16000,
    )
    db.commit()
    enqueue(background, run.id)
    return {"run_id": run.id, "comment_id": c.id}


# save_delivery：按 Run 去重保存主动内容；配置允许时自动发布，否则保留草稿。
def save_delivery(db, run, text):
    d = db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.run_id == run.id))
    if not d:
        d = ProactiveDelivery(
            run_id=run.id,
            owner_id=run.payer_id,
            role_id=run.role_id,
            kind=run.context["kind"],
            target_id=run.context.get("target_id", ""),
            body=text,
        )
        db.add(d)
        db.flush()
    if config_values(db, run.role_id)["auto_publish_proactive"]:
        publish(db, d)
    return {"delivery_id": d.id}


# schedule_due：扫描到期计划及自动学习配置；旧计划超期跳过，避免恢复后集中补发。
def schedule_due():
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                select(ActionPlan.id).where(ActionPlan.enabled.is_(True), ActionPlan.next_at <= time.time())
            )
        )
    for ident in ids:
        with SessionLocal() as db:
            try:
                queue_plan(db, db.get(ActionPlan, ident), scheduled=True)
                db.commit()
            except Exception:
                db.rollback()
    # Auto-learning uses the owner's explicit connection and never reads body before authorization.
    with SessionLocal() as db:
        configs = list(db.scalars(select(AgentConfig)))
        for cfg in configs:
            interval = cfg.config.get("auto_learn_hours", 0)
            conn = cfg.config.get("learning_connection_id")
            if not interval or not conn or not cfg.config.get("learning_enabled"):
                continue
            role = db.scalar(select(Role).where(Role.id == cfg.role_id).with_for_update())
            if not role or role.deleted:
                continue
            last = db.scalar(
                select(Run)
                .where(Run.role_id == role.id, Run.purpose == "LEARN")
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if last and last.created_at + interval * 3600 > time.time():
                continue
            try:
                reserve_run(
                    db, role.owner_id, role, conn, "LEARN", {"consent_version": cfg.consent_version}, 32000
                )
                db.commit()
            except Exception:
                db.rollback()
