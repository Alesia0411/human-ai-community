# 文件职责：SQLAlchemy 数据模型：定义账号、角色、聊天、任务、授权与知识表；只有 ForeignKey 声明才是数据库外键。
import time
import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# uid：生成 32 位十六进制 UUID，作为数据库记录标识而非递增序号。
def uid():
    return uuid.uuid4().hex


# Record：共享主键与创建时间字段；其他表继承此混入类。
class Record:
    # 记录主键；业务查询和关联使用此标识。
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    # 创建时刻的 Unix 秒数，用于排序和时间窗口。
    created_at: Mapped[float] = mapped_column(Float(53), default=time.time, index=True)


# Account：账号与累计预算；used 为已结算量，reserved 为未结束任务占用量。
class Account(Record, Base):
    __tablename__ = "accounts"
    username: Mapped[str] = mapped_column(String(80), unique=True)
    # Argon2 密码哈希，不保存明文密码，也不用于还原密码。
    password_hash: Mapped[str] = mapped_column(Text)
    nickname: Mapped[str] = mapped_column(String(80))
    bio: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    budget: Mapped[int] = mapped_column(Integer, default=100000)
    # 已记录使用量；未知消费场景可能采用保守预留值。
    used: Mapped[int] = mapped_column(Integer, default=0)
    # 任务尚未结算时占用的内部预算，不等于服务商精确金额。
    reserved: Mapped[int] = mapped_column(Integer, default=0)


# AuthSession：可撤销登录会话；刷新令牌保存摘要，访问 JWT 仍需查询此表验证有效性。
class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    account_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float] = mapped_column(Float(53))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# Role：角色手动人设、公开与 AI 开关；version 与 policy_version 分别跟踪设定和权限变化。
class Role(Record, Base):
    __tablename__ = "roles"
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    bio: Mapped[str] = mapped_column(Text, default="")
    persona: Mapped[str] = mapped_column(Text, default="温和、真诚，承认自己是 AI，不编造现实经历。")
    visibility: Mapped[str] = mapped_column(String(16), default="PRIVATE")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    public_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_scope: Mapped[list] = mapped_column(JSON, default=lambda: ["mock", "deepseek", "dashscope"])
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 权限版本；变更后旧任务必须重新授权或停止。
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    # 逻辑删除标记；不能仅凭记录仍存在就允许访问。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


# Connection：账号拥有的模型连接；仅保存加密 Key，接口不应返回密钥正文。
class Connection(Record, Base):
    __tablename__ = "model_connections"
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    # 可解密的模型凭证密文，只在服务端发起调用时解密。
    encrypted_key: Mapped[str] = mapped_column(Text)
    # 逻辑删除标记；不能仅凭记录仍存在就允许访问。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


# Conversation：真人或 AI 会话；保存身份、人设快照、工具授权、顺序号及账号侧清空/隐藏边界。
class Conversation(Record, Base):
    __tablename__ = "conversations"
    kind: Mapped[str] = mapped_column(String(20))
    creator_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"), index=True)
    peer_account_id: Mapped[str] = mapped_column(String(32), index=True)
    source_role_id: Mapped[str] = mapped_column(String(32), ForeignKey("roles.id"))
    target_role_id: Mapped[str] = mapped_column(String(32), ForeignKey("roles.id"))
    # 模型连接记录 ID，不是 API Key；使用前须验证所属账号。
    connection_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("model_connections.id"), nullable=True
    )
    # 会话创建/刷新时保存的人设快照，避免编辑角色悄悄改变旧会话。
    persona_snapshot: Mapped[str] = mapped_column(Text, default="")
    definition_version: Mapped[int] = mapped_column(Integer, default=1)
    # 会话内递增序号，用于补拉、排序和清空边界，不是数据库主键。
    seq: Mapped[int] = mapped_column(Integer, default=0)
    creator_read: Mapped[int] = mapped_column(Integer, default=0)
    peer_read: Mapped[int] = mapped_column(Integer, default=0)
    # 会话隐藏按账号分别保存；一方移除列表不代表删除对方会话。
    creator_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # SQL 状态已变但文件可能未同步，后台需重新生成派生文件。
    files_dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    # 创建者只能读取此序号之后的消息；peer_cleared_seq 为另一参与者的独立边界。
    creator_cleared_seq: Mapped[int] = mapped_column(Integer, default=0)
    peer_cleared_seq: Mapped[int] = mapped_column(Integer, default=0)
    cleared_at: Mapped[float] = mapped_column(Float(53), default=0)
    # 当前会话未结束的生成任务 ID，限制重复并发生成。
    active_run: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 当前会话授予的工具、连接和限额；角色允许之外还需此处同意。
    grants: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 是否允许会话私有知识检索；不同于每五条消息摘要的删除开关。
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 权限版本；变更后旧任务必须重新授权或停止。
    policy_version: Mapped[int] = mapped_column(Integer, default=1)


# Message：按会话序号保存消息，用客户端请求标识去重，并区分 HUMAN 与 AI。
class Message(Record, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        UniqueConstraint("conversation_id", "sender_id", "client_id"),
    )
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id"), index=True)
    # 会话内递增序号，用于补拉、排序和清空边界，不是数据库主键。
    seq: Mapped[int] = mapped_column(Integer)
    sender_id: Mapped[str] = mapped_column(String(32))
    role_id: Mapped[str] = mapped_column(String(32))
    # 客户端操作去重标识；重试应沿用而不是再次创建消息。
    client_id: Mapped[str] = mapped_column(String(80))
    # 内容来源 HUMAN/AI；由服务端决定，不由客户端冒充。
    actor: Mapped[str] = mapped_column(String(16), default="HUMAN")
    body: Mapped[str] = mapped_column(Text)
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 消息撤回标志；展示与学习不能继续使用原正文。
    retracted: Mapped[bool] = mapped_column(Boolean, default=False)


# ChatMemory：每五条消息的摘要及原消息 ID；不是知识库检索文档。
class ChatMemory(Record, Base):
    __tablename__ = "chat_memories"
    __table_args__ = (UniqueConstraint("conversation_id", "batch_key"),)
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id"), index=True)
    # 原消息 ID 分组的哈希；同会话同批次摘要唯一，重试不重复插入。
    batch_key: Mapped[str] = mapped_column(String(64))
    # 摘要对应的原消息 ID 列表，用于分组校验与撤回失效。
    message_ids: Mapped[list] = mapped_column(JSON)
    end_seq: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)


# Run：一次后台任务及付款、预算、执行状态；用于恢复、取消和最终结算。
class Run(Record, Base):
    __tablename__ = "ai_runs"
    conversation_id: Mapped[str] = mapped_column(String(32), index=True)
    # 本任务付款账号，由后端规则确定，不能交给模型选择。
    payer_id: Mapped[str] = mapped_column(String(32), index=True)
    # 模型连接记录 ID，不是 API Key；使用前须验证所属账号。
    connection_id: Mapped[str] = mapped_column(String(32))
    role_id: Mapped[str] = mapped_column(String(32))
    # 权限版本；变更后旧任务必须重新授权或停止。
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    error: Mapped[str] = mapped_column(String(64), default="")
    # 任务尚未结算时占用的内部预算，不等于服务商精确金额。
    reserved: Mapped[int] = mapped_column(Integer)
    # 已记录使用量；未知消费场景可能采用保守预留值。
    used: Mapped[int] = mapped_column(Integer, default=0)
    # 曾开始提供商调用的标记；无完整账本时需按未知消费处理。
    provider_called: Mapped[bool] = mapped_column(Boolean, default=False)
    partial: Mapped[str] = mapped_column(Text, default="")
    # 任务执行截止时间，超时不能继续正常调用或发布。
    deadline: Mapped[float] = mapped_column(Float(53))
    purpose: Mapped[str] = mapped_column(String(20), default="CHAT")
    # 任务的服务端上下文与授权版本，不等同完整模型提示词。
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    heartbeat: Mapped[float] = mapped_column(Float(53), default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    first_token_at: Mapped[float | None] = mapped_column(Float(53), nullable=True)


# Post：角色发表的动态；actor 区分真人与 AI，deleted 为逻辑删除。
class Post(Record, Base):
    __tablename__ = "posts"
    role_id: Mapped[str] = mapped_column(String(32), ForeignKey("roles.id"), index=True)
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"))
    body: Mapped[str] = mapped_column(Text)
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 逻辑删除标记；不能仅凭记录仍存在就允许访问。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # 内容来源 HUMAN/AI；由服务端决定，不由客户端冒充。
    actor: Mapped[str] = mapped_column(String(16), default="HUMAN")


# Comment：动态评论及可选父评论，用于普通评论和 AI 连续回复。
class Comment(Record, Base):
    __tablename__ = "comments"
    post_id: Mapped[str] = mapped_column(String(32), ForeignKey("posts.id"), index=True)
    role_id: Mapped[str] = mapped_column(String(32), ForeignKey("roles.id"))
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("accounts.id"))
    body: Mapped[str] = mapped_column(Text)
    # 逻辑删除标记；不能仅凭记录仍存在就允许访问。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # 内容来源 HUMAN/AI；由服务端决定，不由客户端冒充。
    actor: Mapped[str] = mapped_column(String(16), default="HUMAN")
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# AgentConfig：角色级 Agent 授权、学习配置及当前生效的人设候选引用。
class AgentConfig(Record, Base):
    __tablename__ = "agent_configs"
    role_id: Mapped[str] = mapped_column(String(32), unique=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 学习授权版本；候选与任务需与当前版本匹配。
    consent_version: Mapped[int] = mapped_column(Integer, default=1)
    # 当前学习授权允许读取的最早时间点。
    learning_since: Mapped[float] = mapped_column(Float(53), default=time.time)
    active_candidate: Mapped[str | None] = mapped_column(String(32), nullable=True)


# LearningConsent：真人会话中各参与者对指定角色学习的授权与起始时间。
class LearningConsent(Record, Base):
    __tablename__ = "learning_consents"
    __table_args__ = (UniqueConstraint("conversation_id", "account_id", "role_id"),)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(32))
    role_id: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    since: Mapped[float] = mapped_column(Float(53), default=time.time)
    providers: Mapped[list] = mapped_column(JSON, default=list)


# PersonaCandidate：学习生成的人设字段及证据，绑定授权版本以支持撤回失效。
class PersonaCandidate(Record, Base):
    __tablename__ = "persona_candidates"
    role_id: Mapped[str] = mapped_column(String(32), index=True)
    # 学习授权版本；候选与任务需与当前版本匹配。
    consent_version: Mapped[int] = mapped_column(Integer)
    fields: Mapped[dict] = mapped_column(JSON)
    # 学习候选的来源引用；使用候选时仍需重新检查每个来源授权。
    evidence: Mapped[list] = mapped_column(JSON)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


# KnowledgeDocument：获准知识正文、提供商范围及索引状态；可限定到访客私有会话。
class KnowledgeDocument(Record, Base):
    __tablename__ = "knowledge_documents"
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    role_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    providers: Mapped[list] = mapped_column(JSON)
    embedding_connection_id: Mapped[str] = mapped_column(String(32))
    embedding_model: Mapped[str] = mapped_column(String(100), default="text-embedding-v4")
    embedding_provider: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    # 逻辑删除标记；不能仅凭记录仍存在就允许访问。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


# KnowledgeChunk：文档切片及向量的 SQL 副本，向量库命中仍须回到 SQL 验权。
class KnowledgeChunk(Record, Base):
    __tablename__ = "knowledge_chunks"
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    body: Mapped[str] = mapped_column(Text)
    vector: Mapped[list] = mapped_column(JSON)
    position: Mapped[int] = mapped_column(Integer)


# CallLedger：任务内唯一调用步骤的账本；保存状态、预算和临时结果，防止重复外部消费。
class CallLedger(Record, Base):
    __tablename__ = "call_ledger"
    __table_args__ = (UniqueConstraint("run_id", "step_key"),)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    # Run 内调用步骤标识；与 run_id 联合唯一以防重复外部调用。
    step_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="INFLIGHT")
    # 任务尚未结算时占用的内部预算，不等于服务商精确金额。
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    # 已记录使用量；未知消费场景可能采用保守预留值。
    used: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    billing: Mapped[str] = mapped_column(String(40), default="payer")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    finished_at: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False)


# ActionPlan：主动交互的目标、提示词、模型连接、间隔和每日允许时段。
class ActionPlan(Record, Base):
    __tablename__ = "action_plans"
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    role_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32), default="")
    prompt: Mapped[str] = mapped_column(Text)
    # 模型连接记录 ID，不是 API Key；使用前须验证所属账号。
    connection_id: Mapped[str] = mapped_column(String(32))
    # 计划下一次到期时间，仍受每日窗口和超期跳过规则约束。
    next_at: Mapped[float] = mapped_column(Float(53))
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    start_hour: Mapped[int] = mapped_column(Integer, default=9)
    end_hour: Mapped[int] = mapped_column(Integer, default=21)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# ProactiveDelivery：主动生成内容的草稿/提交状态；不包含接收方后续私聊状态。
class ProactiveDelivery(Record, Base):
    __tablename__ = "proactive_deliveries"
    run_id: Mapped[str] = mapped_column(String(32), unique=True)
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    role_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")


# Reception：接收角色对指定发送角色的主动私聊许可。
class Reception(Record, Base):
    __tablename__ = "reception_permissions"
    __table_args__ = (UniqueConstraint("recipient_role_id", "sender_role_id"),)
    recipient_role_id: Mapped[str] = mapped_column(String(32))
    sender_role_id: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


# Relation：账号之间的关注/屏蔽等关系，权限判断不因切换角色而绕过账号屏蔽。
class Relation(Record, Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("owner_id", "target_id", "kind"),)
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))


# Asset：图片归属、会话范围及服务器相对路径；下载仍须验证访问权限。
class Asset(Record, Base):
    __tablename__ = "assets"
    # 数据所属账号；访问时仍需显式验证归属。
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    path: Mapped[str] = mapped_column(String(100))


# SocketTicket：短期一次性 WebSocket 登录票据，只保存摘要并记录是否已消费。
class SocketTicket(Record, Base):
    __tablename__ = "socket_tickets"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str] = mapped_column(String(32))
    conversation_id: Mapped[str] = mapped_column(String(32))
    expires: Mapped[float] = mapped_column(Float(53))
    # 已记录使用量；未知消费场景可能采用保守预留值。
    used: Mapped[bool] = mapped_column(Boolean, default=False)
