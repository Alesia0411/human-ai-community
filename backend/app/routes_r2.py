# 文件职责：Agent 配置、人设学习、会话授权、知识上传与任务恢复接口；读写均绑定当前账号权限。
import hashlib
import json
import time
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import CallLedger, Conversation, KnowledgeDocument, LearningConsent, PersonaCandidate, Role, Run
from .policy import conversation, fail, owned_role
from .r2_policy import (
    TOOLS,
    AgentSettings,
    SessionSettings,
    agent_config,
    candidate_valid,
    connection_for,
    reserve_run,
)
from .schemas import Input
from .security import current

router = APIRouter()


# resume：只允许付款人恢复安全暂停任务，恢复前复查授权和调用状态。
@router.post("/runs/{rid}/resume")
def resume(rid: str, background: BackgroundTasks, a=Depends(current), db: Session = Depends(get_db)):
    run = db.scalar(select(Run).where(Run.id == rid).with_for_update())
    if not run or run.payer_id != a.id:
        fail()
    if run.status != "PAUSED":
        fail("RUN_NOT_PAUSED", 409)
    if db.scalar(select(CallLedger.id).where(CallLedger.run_id == rid, CallLedger.state != "DONE")):
        fail("UNKNOWN_USAGE_NO_REPLAY", 409)
    run.status = "QUEUED"
    run.deadline = time.time() + settings().run_timeout + 300
    db.commit()
    enqueue(background, rid)
    return {"ok": True}


# enqueue：保留排队接口；当前为空实现，真正任务由 Worker 扫描已提交 SQL Run 获取。
def enqueue(background, rid):
    # Both local single-consumer scheduler and Celery drain the durable SQL queue.
    pass


# ConfigChange：在角色 Agent 设置上增加明确回溯历史的输入字段。
class ConfigChange(AgentSettings):
    backfill_from: float | None = None


# get_config：返回角色拥有者自己的 Agent 配置及授权元数据。
@router.get("/roles/{rid}/agent")
def get_config(rid: str, a=Depends(current), db: Session = Depends(get_db)):
    owned_role(db, a.id, rid)
    cfg = agent_config(db, rid)
    db.commit()
    return {
        **cfg.config,
        "consent_version": cfg.consent_version,
        "learning_since": cfg.learning_since,
        "active_candidate": cfg.active_candidate,
    }


# update_config：校验学习/工具设置并更新授权版本，撤回后使旧候选和旧任务失效。
@router.put("/roles/{rid}/agent")
def update_config(rid: str, data: ConfigChange, a=Depends(current), db: Session = Depends(get_db)):
    role = owned_role(db, a.id, rid, True)
    if set(data.enabled_tools) - set(TOOLS) or set(data.learning_sources) - {"posts", "comments", "messages"}:
        fail("INVALID_SCOPE", 422)
    if set(data.locked_fields) - {"personality", "speaking_style", "habits"}:
        fail("INVALID_FIELD", 422)
    if not set(data.learning_providers) <= set(role.provider_scope):
        fail("PROVIDER_SCOPE_MISMATCH", 403)
    if data.learning_connection_id:
        connection_for(db, a.id, data.learning_connection_id, data.learning_providers)
    cfg = agent_config(db, rid)
    new = data.model_dump(exclude={"backfill_from"})
    learning_changed = (
        any(
            cfg.config.get(k) != new[k]
            for k in ("learning_enabled", "learning_sources", "learning_providers")
        )
        or data.backfill_from is not None
    )
    if learning_changed:
        cfg.consent_version += 1
        cfg.active_candidate = None
        cfg.learning_since = data.backfill_from if data.backfill_from is not None else time.time()
        for c in db.scalars(select(PersonaCandidate).where(PersonaCandidate.role_id == rid)):
            c.valid = False
            c.fields = {}
    cfg.config = new
    role.policy_version += 1
    db.commit()
    return {"ok": True}


# ChatConsent：真人私聊学习许可输入，包含启用状态、提供商及回溯起点。
class ChatConsent(Input):
    role_id: str
    enabled: bool = False
    providers: list[str] = Field(default_factory=list)
    backfill_from: float | None = None


# chat_consent：保存当前参与者对真人会话学习的许可，不能替另一方授权。
@router.put("/conversations/{cid}/learning-consent")
def chat_consent(cid: str, data: ChatConsent, a=Depends(current), db: Session = Depends(get_db)):
    c = conversation(db, a.id, cid)
    if c.kind != "HUMAN_DIRECT" or data.role_id not in (c.source_role_id, c.target_role_id):
        fail("HUMAN_CONVERSATION_REQUIRED", 422)
    role = db.scalar(select(Role).where(Role.id == data.role_id).with_for_update())
    permit = db.scalar(
        select(LearningConsent).where(
            LearningConsent.conversation_id == cid,
            LearningConsent.account_id == a.id,
            LearningConsent.role_id == data.role_id,
        )
    )
    if not permit:
        permit = LearningConsent(conversation_id=cid, account_id=a.id, role_id=data.role_id)
        db.add(permit)
    permit.enabled, permit.providers = data.enabled, data.providers
    permit.since = data.backfill_from if data.backfill_from is not None else time.time()
    role.policy_version += 1
    db.commit()
    return {"ok": True}


# LearnRequest：手动学习请求，只指定本人的模型连接；预算由服务端决定。
class LearnRequest(Input):
    connection_id: str


# request_learning：验证角色学习授权和付款连接后创建 LEARN 任务。
@router.post("/roles/{rid}/learn")
def request_learning(
    rid: str,
    data: LearnRequest,
    background: BackgroundTasks,
    a=Depends(current),
    db: Session = Depends(get_db),
):
    role = owned_role(db, a.id, rid, True)
    cfg = agent_config(db, rid)
    if not cfg.config.get("learning_enabled"):
        fail("LEARNING_DISABLED", 409)
    connection_for(db, a.id, data.connection_id, cfg.config.get("learning_providers", []))
    run = reserve_run(
        db, a.id, role, data.connection_id, "LEARN", {"consent_version": cfg.consent_version}, 32000
    )
    db.commit()
    enqueue(background, run.id)
    return {"run_id": run.id}


# candidates：列出本人角色的候选人设及当前有效性。
@router.get("/roles/{rid}/candidates")
def candidates(rid: str, a=Depends(current), db: Session = Depends(get_db)):
    owned_role(db, a.id, rid)
    return [
        {
            "id": c.id,
            "fields": c.fields if candidate_valid(db, c) else {},
            "evidence": c.evidence,
            "valid": candidate_valid(db, c),
            "created_at": c.created_at,
        }
        for c in db.scalars(
            select(PersonaCandidate)
            .where(PersonaCandidate.role_id == rid)
            .order_by(PersonaCandidate.created_at.desc())
            .limit(30)
        )
    ]


# activate：复查候选证据与授权后切换生效人设，并更新设定版本。
@router.post("/roles/{rid}/candidates/{candidate_id}/activate")
def activate(rid: str, candidate_id: str, a=Depends(current), db: Session = Depends(get_db)):
    role = owned_role(db, a.id, rid, True)
    c = db.get(PersonaCandidate, candidate_id)
    if not c or c.role_id != rid or not candidate_valid(db, c):
        fail("CANDIDATE_INVALID", 409)
    cfg = agent_config(db, rid)
    cfg.active_candidate = c.id
    role.version += 1
    db.commit()
    return {"ok": True}


# export_persona：导出当前角色可用设定，不导出访客私聊和模型凭证。
@router.get("/roles/{rid}/persona-export")
def export_persona(
    rid: str, format: Literal["json", "markdown"] = "json", a=Depends(current), db: Session = Depends(get_db)
):
    role = owned_role(db, a.id, rid)
    cfg = agent_config(db, rid)
    c = db.get(PersonaCandidate, cfg.active_candidate) if cfg.active_candidate else None
    fields = c.fields if candidate_valid(db, c) else {}
    data = {"role": role.name, "manual": role.persona, "learned": fields}
    body = (
        json.dumps(data, ensure_ascii=False, indent=2)
        if format == "json"
        else "# "
        + role.name
        + "\n\n"
        + role.persona
        + "\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in fields.items())
    )
    return Response(body, media_type="application/json" if format == "json" else "text/markdown")


# session_config：保存 AI 会话创建者自己的连接、工具额度和私有知识授权，更新会话版本。
@router.put("/conversations/{cid}/agent-settings")
def session_config(cid: str, data: SessionSettings, a=Depends(current), db: Session = Depends(get_db)):
    pre = conversation(db, a.id, cid)
    db.scalar(select(Role).where(Role.id == pre.target_role_id).with_for_update())
    c = conversation(db, a.id, cid, True)
    if c.kind != "AI_DIRECT" or c.creator_id != a.id:
        fail()
    if set(data.enabled_tools) - set(TOOLS) or set(data.tool_connections) - set(TOOLS):
        fail("INVALID_TOOL", 422)
    for connid in [data.connection_id, data.embedding_connection_id, *data.tool_connections.values()]:
        if connid:
            connection_for(db, a.id, connid, db.get(Role, c.target_role_id).provider_scope)
    if data.connection_id:
        c.connection_id = data.connection_id
    c.grants = data.model_dump(exclude={"memory_enabled", "connection_id"})
    c.memory_enabled = data.memory_enabled
    c.policy_version += 1
    if not c.memory_enabled:
        for doc in db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.conversation_id == cid)):
            doc.deleted = True
    db.commit()
    return {"ok": True}


# refresh_definition：由会话创建者显式刷新人设快照，避免角色编辑悄悄改变旧会话。
@router.post("/conversations/{cid}/refresh-definition")
def refresh_definition(cid: str, a=Depends(current), db: Session = Depends(get_db)):
    from .r2_policy import effective_persona

    pre = conversation(db, a.id, cid)
    role = db.scalar(select(Role).where(Role.id == pre.target_role_id).with_for_update())
    c = conversation(db, a.id, cid, True)
    if c.kind != "AI_DIRECT":
        fail()
    if c.active_run:
        fail("RUN_IN_PROGRESS", 409)
    conn = connection_for(db, a.id, c.connection_id, role.provider_scope)
    c.persona_snapshot, c.candidate_id = effective_persona(db, role, conn.provider)
    c.definition_version = role.version
    c.policy_version += 1
    db.commit()
    return {"ok": True}


# KnowledgeInput：知识输入约束：正文、授权声明、提供商、连接和可选会话范围。
class KnowledgeInput(Input):
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=20000)
    connection_id: str
    embedding_model: str = Field(default="text-embedding-v4", max_length=100)
    providers: list[str]
    conversation_id: str = ""
    authorized: bool = False


# create_document：验证资料授权与范围，创建文档并预留索引任务预算。
def create_document(rid, data, a, db, background):
    if not data.authorized:
        fail("EXPLICIT_KNOWLEDGE_AUTHORIZATION_REQUIRED", 422)
    if data.conversation_id:
        c = conversation(db, a.id, data.conversation_id)
        if c.kind != "AI_DIRECT" or not c.memory_enabled or c.target_role_id != rid:
            fail("MEMORY_DISABLED", 409)
        role = db.scalar(select(Role).where(Role.id == rid).with_for_update())
    else:
        role = owned_role(db, a.id, rid, True)
    conn = connection_for(db, a.id, data.connection_id, role.provider_scope)
    if (
        not data.providers
        or not set(data.providers) <= set(role.provider_scope)
        or conn.provider not in data.providers
    ):
        fail("PROVIDER_SCOPE_MISMATCH", 403)
    digest = hashlib.sha256(data.text.encode()).hexdigest()
    old = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.role_id == rid,
            KnowledgeDocument.owner_id == a.id,
            KnowledgeDocument.conversation_id == data.conversation_id,
            KnowledgeDocument.digest == digest,
            KnowledgeDocument.deleted.is_(False),
        )
    )
    if old:
        return {"id": old.id, "status": old.status, "duplicate": True}
    doc = KnowledgeDocument(
        owner_id=a.id,
        role_id=rid,
        conversation_id=data.conversation_id,
        title=data.title,
        body=data.text,
        digest=digest,
        providers=data.providers,
        embedding_connection_id=conn.id,
        embedding_model=data.embedding_model,
        embedding_provider=conn.provider,
    )
    db.add(doc)
    db.flush()
    run = reserve_run(
        db,
        a.id,
        role,
        conn.id,
        "INDEX",
        {"document_id": doc.id},
        min(100000, len(data.text.encode()) * 2 + 4096),
    )
    db.commit()
    enqueue(background, run.id)
    return {"id": doc.id, "status": doc.status, "run_id": run.id}


# knowledge：接受文本知识请求，复用文档创建与排队逻辑。
@router.post("/roles/{rid}/knowledge")
def knowledge(
    rid: str,
    data: KnowledgeInput,
    background: BackgroundTasks,
    a=Depends(current),
    db: Session = Depends(get_db),
):
    return create_document(rid, data, a, db, background)


# knowledge_file：限制上传大小和类型，提取 TXT/Markdown/PDF 文本后复用知识创建逻辑。
@router.post("/roles/{rid}/knowledge-file")
async def knowledge_file(
    rid: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    options: str = Form(...),
    a=Depends(current),
    db: Session = Depends(get_db),
):
    # Validate scope before reading/parsing file body.
    try:
        opts = json.loads(options)
        opts.update(text="pending", title=file.filename or "document")
        parsed = KnowledgeInput.model_validate(opts)
    except (ValueError, AttributeError):
        fail("INVALID_DOCUMENT_OPTIONS", 422)
    if not parsed.authorized:
        fail("EXPLICIT_KNOWLEDGE_AUTHORIZATION_REQUIRED", 422)
    if parsed.conversation_id:
        conversation(db, a.id, parsed.conversation_id)
    else:
        owned_role(db, a.id, rid)
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        fail("FILE_TOO_LARGE", 413)
    try:
        if (file.filename or "").lower().endswith(".pdf"):
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            try:
                pdf = PdfReader(BytesIO(raw))
            except PdfReadError:
                fail("INVALID_PDF", 422)
            if pdf.is_encrypted:
                fail("ENCRYPTED_PDF_NOT_SUPPORTED", 422)
            if len(pdf.pages) > 30:
                fail("TOO_MANY_PAGES", 422)
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        elif (file.filename or "").lower().endswith((".txt", ".md")):
            text = raw.decode("utf-8")
        else:
            fail("UNSUPPORTED_DOCUMENT", 422)
    except (ValueError, UnicodeError):
        fail("INVALID_DOCUMENT", 422)
    try:
        parsed = KnowledgeInput.model_validate({**opts, "text": text})
    except ValidationError:
        fail("DOCUMENT_TEXT_MUST_BE_1_TO_20000_CHARACTERS", 422)
    return create_document(rid, parsed, a, db, background)


# list_knowledge：按拥有者或本人私有会话范围列出可管理文档。
@router.get("/roles/{rid}/knowledge")
def list_knowledge(rid: str, conversation_id: str = "", a=Depends(current), db: Session = Depends(get_db)):
    if conversation_id:
        conversation(db, a.id, conversation_id)
    else:
        owned_role(db, a.id, rid)
    return [
        {"id": d.id, "title": d.title, "status": d.status, "providers": d.providers}
        for d in db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.owner_id == a.id,
                KnowledgeDocument.role_id == rid,
                KnowledgeDocument.conversation_id == conversation_id,
                KnowledgeDocument.deleted.is_(False),
            )
        )
    ]


# delete_knowledge：撤销文档并使依赖它的后续使用失效，物理向量清理由后台完成。
@router.delete("/knowledge/{did}")
def delete_knowledge(did: str, a=Depends(current), db: Session = Depends(get_db)):
    d = db.get(KnowledgeDocument, did)
    if not d or d.owner_id != a.id:
        fail()
    db.scalar(select(Role).where(Role.id == d.role_id).with_for_update())
    d.deleted = True
    if d.conversation_id:
        db.get(Conversation, d.conversation_id).policy_version += 1
    else:
        db.get(Role, d.role_id).policy_version += 1
    db.commit()
    return {"ok": True}


# call_timeline：仅向付款人返回任务调用时间线和计量元数据。
@router.get("/runs/{rid}/calls")
def call_timeline(rid: str, a=Depends(current), db: Session = Depends(get_db)):
    run = db.get(Run, rid)
    if not run or run.payer_id != a.id:
        fail()
    return [
        {
            "id": c.id,
            "kind": c.kind,
            "state": c.state,
            "tokens": c.used,
            "units": c.units,
            "billing": c.billing,
            "usage_known": c.usage_known,
            "duration_ms": round((c.finished_at - c.created_at) * 1000, 1)
            if c.finished_at is not None
            else None,
        }
        for c in db.scalars(
            select(CallLedger).where(CallLedger.run_id == rid).order_by(CallLedger.created_at)
        )
    ]
