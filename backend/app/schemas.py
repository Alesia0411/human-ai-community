# 文件职责：Pydantic HTTP 输入模型：限制字段、长度与枚举；输入校验不能代替账号权限检查。
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Input：请求基类：禁止额外字段，统一去除字符串首尾空白。
class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Register：注册输入：用户名格式、密码最小长度和邀请码。
class Register(Input):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_@.\-]+$")
    password: str = Field(min_length=10, max_length=128)
    nickname: str = Field(min_length=1, max_length=80)
    invite_code: str


# Login：账号密码登录输入。
class Login(Input):
    username: str
    password: str


# Profile：允许编辑的账号资料字段。
class Profile(Input):
    nickname: str = Field(min_length=1, max_length=80)
    bio: str = Field(max_length=1000)


# RoleInput：角色名称、人设、公开与 AI 授权配置及版本输入。
class RoleInput(Input):
    name: str = Field(min_length=1, max_length=80)
    bio: str = Field(default="", max_length=1000)
    persona: str = Field(default="温和、真诚，承认自己是 AI，不编造现实经历。", max_length=4000)
    visibility: Literal["PRIVATE", "PUBLIC"] = "PRIVATE"
    ai_enabled: bool = False
    public_ai: bool = False
    provider_scope: list[str] = Field(
        default_factory=lambda: ["mock", "deepseek", "dashscope", "ollama"], max_length=10
    )
    version: int | None = None


# ConnectionInput：模型连接输入；提供商地址不接受用户任意指定。
class ConnectionInput(Input):
    provider: str = Field(max_length=30)
    model: str = Field(min_length=1, max_length=100)
    api_key: str = Field(default="", max_length=4096)


# ConversationInput：会话类型、双方角色及可选模型连接。
class ConversationInput(Input):
    kind: Literal["HUMAN_DIRECT", "AI_DIRECT"]
    source_role_id: str
    target_role_id: str
    # 模型连接记录 ID，不是 API Key；使用前须验证所属账号。
    connection_id: str | None = None


# SendMessage：聊天正文、客户端去重标识和可选图片 ID。
class SendMessage(Input):
    body: str = Field(default="", max_length=4000)
    # 同一次发送操作重试时保持不变，避免重复写入与费用预留。
    client_message_id: str = Field(min_length=1, max_length=80)
    asset_id: str | None = None


# PostInput：动态正文、发言角色及可选图片。
class PostInput(Input):
    role_id: str
    body: str = Field(min_length=1, max_length=5000)
    asset_id: str | None = None


# CommentInput：评论正文与发言角色。
class CommentInput(Input):
    role_id: str
    body: str = Field(min_length=1, max_length=2000)


# BudgetInput：账号预算调整请求。
class BudgetInput(Input):
    budget: int = Field(ge=1000, le=10000000)
