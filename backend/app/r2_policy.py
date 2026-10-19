# 文件职责：Agent 用途相关的授权中心；每次执行和发布都以 SQL 当前授权为准，而非旧检查点。
"""Purpose-specific authorization. MySQL remains authoritative over graph/vector state."""

import time

from pydantic import Field
from sqlalchemy import select

from .config import settings
from .models import (
    AgentConfig,
    Comment,
    Connection,
    Conversation,
    KnowledgeDocument,
    LearningConsent,
    Message,
    PersonaCandidate,
    Post,
    Reception,
    Role,
    Run,
)
from .policy import PayerPolicy, blocked, can_send, fail
from .schemas import Input

TOOLS = ("vision_describe", "image_generate", "weather_query", "maps_search", "maps_route")


# AgentSettings：角色级工具、学习及主动行为开关；默认关闭需要额外许可的能力。
class AgentSettings(Input):
    learning_enabled: bool = False
    learning_sources: list[str] = Field(default_factory=lambda: ["posts", "comments"])
    learning_providers: list[str] = Field(default_factory=list)
    auto_apply_persona: bool = False
    locked_fields: list[str] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=list)
    allow_proactive_dm: bool = False
    allow_auto_post: bool = False
    allow_auto_comment: bool = False
    allow_ai_comment_reply: bool = False
    auto_publish_proactive: bool = False
    accept_ai_comments: bool = False
    learning_connection_id: str | None = None
    auto_learn_hours: int = Field(default=0, ge=0, le=168)


# SessionSettings：会话工具、连接、次数和预算限制；角色授权之外还需访客授权。
class SessionSettings(Input):
    # 模型连接记录 ID，不是 API Key；使用前须验证所属账号。
    connection_id: str | None = None
    enabled_tools: list[str] = Field(default_factory=list)
    tool_connections: dict[str, str] = Field(default_factory=dict)
    max_tool_calls: int = Field(default=0, ge=0, le=4)
    max_images: int = Field(default=0, ge=0, le=1)
    max_tokens: int = Field(default=16000, ge=4000, le=100000)
    # 是否允许会话私有知识检索；不同于每五条消息摘要的删除开关。
    memory_enabled: bool = False
    embedding_connection_id: str | None = None


# agent_config：读取角色配置，缺失时创建默认配置并 flush，提交由调用者负责。
def agent_config(db, role_id):
    row = db.scalar(select(AgentConfig).where(AgentConfig.role_id == role_id))
    if not row:
        row = AgentConfig(role_id=role_id, config=AgentSettings().model_dump())
        db.add(row)
        db.flush()
    return row


# config_values：把已有配置覆盖到默认值上，补齐历史记录缺失的配置键。
def config_values(db, role_id):
    row = db.scalar(select(AgentConfig).where(AgentConfig.role_id == role_id))
    return {**AgentSettings().model_dump(), **(row.config if row else {})}


# connection_for：检查连接存在、未删除、属于指定付款账号，且提供商在许可范围内。
def connection_for(db, account_id, ident, providers=None):
    conn = db.get(Connection, ident) if ident else None
    if not conn or conn.deleted or conn.owner_id != account_id:
        fail("CONNECTION_REQUIRED", 422)
    if providers is not None and conn.provider not in providers:
        fail("PROVIDER_SCOPE_MISMATCH", 403)
    return conn


# eligible_source：先检查来源元数据和学习许可，再读取正文；真人私聊要求双方同意。
def eligible_source(db, role, kind, ident, provider):
    """Check metadata/consent before returning any body to the learning pipeline."""
    cfg = agent_config(db, role.id)
    opts = cfg.config
    if not opts.get("learning_enabled") or provider not in opts.get("learning_providers", []):
        return None
    model = {"posts": Post, "comments": Comment, "messages": Message}.get(kind)
    if kind not in opts.get("learning_sources", []) or model is None:
        return None
    # Projection avoids selecting body before permission checks.
    q = select(model.id, model.created_at, model.role_id, model.actor)
    q = q.where(model.id == ident, model.role_id == role.id, model.actor == "HUMAN")
    q = q.where(model.retracted.is_(False) if kind == "messages" else model.deleted.is_(False))
    meta = db.execute(q).first()
    if not meta or meta.created_at < cfg.learning_since:
        return None
    if kind == "messages":
        cid = db.scalar(select(Message.conversation_id).where(Message.id == ident))
        conv = db.get(Conversation, cid)
        if not conv or conv.kind != "HUMAN_DIRECT":
            return None
        if blocked(db, conv.creator_id, conv.peer_account_id):
            return None
        for account in {conv.creator_id, conv.peer_account_id}:
            permit = db.scalar(
                select(LearningConsent).where(
                    LearningConsent.conversation_id == cid,
                    LearningConsent.account_id == account,
                    LearningConsent.role_id == role.id,
                )
            )
            if (
                not permit
                or not permit.enabled
                or provider not in permit.providers
                or meta.created_at < permit.since
            ):
                return None
    if kind == "comments":
        pid = db.scalar(select(Comment.post_id).where(Comment.id == ident))
        post = db.get(Post, pid)
        if not post or post.deleted:
            return None
    return db.get(model, ident)


# candidate_valid：检查候选状态、授权版本及全部证据当前仍有效。
def candidate_valid(db, candidate, provider=None):
    if not candidate or not candidate.valid:
        return False
    role = db.get(Role, candidate.role_id)
    if not role or role.deleted:
        return False
    cfg = agent_config(db, role.id)
    if cfg.consent_version != candidate.consent_version or not cfg.config.get("learning_enabled"):
        return False
    provider = provider or (cfg.config.get("learning_providers") or [""])[0]
    return all(
        eligible_source(db, role, e["kind"], e["id"], provider) is not None for e in candidate.evidence
    )


# effective_persona：返回手动人设和当前可用候选 ID，不把失效候选继续带入生成。
def effective_persona(db, role, provider):
    cfg = agent_config(db, role.id)
    candidate = db.get(PersonaCandidate, cfg.active_candidate) if cfg.active_candidate else None
    if candidate_valid(db, candidate, provider):
        return role.persona, candidate.id
    return role.persona, None


# assert_proactive：按主动私聊/动态/评论类型检查角色、接收许可、目标可见性及屏蔽关系。
def assert_proactive(db, role, kind, target_id):
    opts = config_values(db, role.id)
    flag = {"DM": "allow_proactive_dm", "POST": "allow_auto_post", "COMMENT": "allow_auto_comment"}.get(kind)
    if not role.ai_enabled or role.deleted or role.visibility != "PUBLIC" or not flag or not opts.get(flag):
        fail("PROACTIVE_DISABLED", 409)
    if kind == "DM":
        recipient = db.scalar(
            select(Role)
            .where(Role.id == target_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        permit = db.scalar(
            select(Reception).where(
                Reception.recipient_role_id == target_id,
                Reception.sender_role_id == role.id,
                Reception.enabled.is_(True),
            )
        )
        if not recipient or recipient.deleted or not permit or blocked(db, role.owner_id, recipient.owner_id):
            fail("RECIPIENT_NOT_ACCEPTING", 403)
    if kind == "COMMENT":
        p = db.get(Post, target_id)
        target = (
            db.scalar(
                select(Role)
                .where(Role.id == p.role_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if p and not p.deleted
            else None
        )
        if (
            not target
            or target.deleted
            or target.visibility != "PUBLIC"
            or blocked(db, role.owner_id, target.owner_id)
        ):
            fail("RECIPIENT_NOT_ACCEPTING", 403)
        if not config_values(db, target.id).get("accept_ai_comments"):
            fail("RECIPIENT_NOT_ACCEPTING", 403)


# check_run：检查任务状态、期限、角色版本和连接，再按任务用途验证会话/学习/文档等授权。
def check_run(db, rid):
    run = db.get(Run, rid)
    if settings().ai_stopped or not run or run.status != "RUNNING" or run.deadline < time.time():
        fail("RUN_STOPPED", 409)
    role = db.get(Role, run.role_id)
    if not role or role.deleted or role.policy_version != run.policy_version:
        fail("CONSENT_REVOKED", 409)
    conn = connection_for(db, run.payer_id, run.connection_id, role.provider_scope)
    if run.purpose == "CHAT":
        c = db.get(Conversation, run.conversation_id)
        can_send(db, c)
        if c.creator_id != run.payer_id or c.policy_version != run.context.get("session_version", 1):
            fail("SESSION_CHANGED", 409)
    elif run.purpose == "LEARN":
        cfg = agent_config(db, role.id)
        if cfg.consent_version != run.context.get("consent_version") or not cfg.config.get(
            "learning_enabled"
        ):
            fail("CONSENT_REVOKED", 409)
        for source in run.context.get("evidence", []):
            if eligible_source(db, role, source["kind"], source["id"], conn.provider) is None:
                fail("LEARNING_SOURCE_REVOKED", 409)
    elif run.purpose == "INDEX":
        doc = db.get(KnowledgeDocument, run.context["document_id"])
        if not doc or doc.deleted or doc.owner_id != run.payer_id:
            fail("DOCUMENT_REVOKED", 409)
        if doc.conversation_id and not db.get(Conversation, doc.conversation_id).memory_enabled:
            fail("MEMORY_REVOKED", 409)
    elif run.purpose == "PROACTIVE":
        assert_proactive(db, role, run.context["kind"], run.context.get("target_id", ""))
    elif run.purpose == "COMMENT":
        parent = db.get(Comment, run.context["parent_id"])
        p = (
            db.scalar(
                select(Post)
                .where(Post.id == parent.post_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if parent
            else None
        )
        if (
            not parent
            or parent.deleted
            or not p
            or p.deleted
            or not config_values(db, role.id)["allow_ai_comment_reply"]
        ):
            fail("COMMENT_REVOKED", 409)
        if not role.ai_enabled or role.visibility != "PUBLIC" or blocked(db, run.payer_id, role.owner_id):
            fail("AI_UNAVAILABLE", 409)
    return run, role, conn


# reserve_run：验证付款连接并锁定账号预算，为新任务预留额度；提交由上层统一执行。
def reserve_run(db, payer_id, role, connection_id, purpose, context, amount=16000, cid=""):
    conn = connection_for(db, payer_id, connection_id, role.provider_scope)
    payer = PayerPolicy.account(db, payer_id)
    if payer.used + payer.reserved + amount > payer.budget:
        fail("BUDGET_EXCEEDED", 409)
    payer.reserved += amount
    run = Run(
        conversation_id=cid,
        payer_id=payer_id,
        connection_id=conn.id,
        role_id=role.id,
        policy_version=role.policy_version,
        reserved=amount,
        purpose=purpose,
        context=context,
        deadline=time.time() + settings().run_timeout + 300,
    )
    db.add(run)
    db.flush()
    return run


# tool_context：只允许获准 CHAT 任务调用工具，并检查图片工具接收提供商的资料授权。
def tool_context(db, rid, name):
    run, role, conn = check_run(db, rid)
    if run.purpose != "CHAT":
        fail("TOOLS_NOT_GRANTED", 403)
    c = db.get(Conversation, run.conversation_id)
    if name not in config_values(db, role.id)["enabled_tools"] or name not in c.grants.get(
        "enabled_tools", []
    ):
        fail("TOOL_NOT_GRANTED", 403)
    if name in ("vision_describe", "image_generate"):
        conn = connection_for(
            db, run.payer_id, c.grants.get("tool_connections", {}).get(name), role.provider_scope
        )
        for did in run.context.get("knowledge_ids", []):
            doc = db.get(KnowledgeDocument, did)
            if not doc or doc.deleted or conn.provider not in doc.providers:
                fail("TOOL_DATA_PROVIDER_SCOPE_MISMATCH", 403)
        if c.candidate_id and not candidate_valid(
            db, db.get(PersonaCandidate, c.candidate_id), conn.provider
        ):
            fail("TOOL_DATA_PROVIDER_SCOPE_MISMATCH", 403)
    return run, role, c


# invalidate_sources：使引用已撤回来源的候选失效，并提升对应角色权限版本。
def invalidate_sources(db, kind, ident):
    # Small-circle bounded data; authority filtering remains active even before this sweep.
    for candidate in db.scalars(select(PersonaCandidate).where(PersonaCandidate.valid.is_(True))):
        if any(e["kind"] == kind and e["id"] == ident for e in candidate.evidence):
            candidate.valid = False
            candidate.fields = {}
            role = db.get(Role, candidate.role_id)
            role.policy_version += 1
