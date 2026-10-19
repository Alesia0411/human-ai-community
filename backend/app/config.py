# 文件职责：通过 HAI_ 环境变量和本地 .env 加载配置；缓存配置实例，密钥可从私有文件读取。
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


# Settings：应用配置模型；字段默认值用于开发，部署可用同名 HAI_ 环境变量覆盖。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAI_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/human-ai.db"
    # Docker 内服务名 redis 与逻辑库 0；这是 Celery broker 地址，不存聊天正文。
    redis_url: str = "redis://redis:6379/0"
    secret_key: str = ""
    secret_file: str = ""
    invite_code: str = ""
    demo_mode: bool = False
    inline_tasks: bool = False
    cookie_secure: bool = True
    allowed_origins: str = "https://localhost"
    media_dir: str = "./data/media"
    chat_record_dir: str = "./data/chat-records"
    access_minutes: int = 15
    run_timeout: int = 90
    # 应用容量检查须与 Ollama 实际上下文配置协调；不是所有模型自动都支持此大小。
    ollama_context_length: int = 8192
    ollama_context_margin: int = 256
    # 单次生成输出上限，同时作为容量检查的回复预留。
    model_output_tokens: int = 512
    ollama_tokenizer_models: list[str] = ["qwen3:8b"]
    checkpoint_path: str = "./data/checkpoints.sqlite"
    # 非空时使用远程 Qdrant；为空时使用下方本地目录。
    qdrant_url: str = ""
    qdrant_path: str = "./data/qdrant"
    ai_stopped: bool = False
    retrieval_mode: Literal["vector", "hybrid", "hybrid_rerank"] = "vector"
    proactive_grace_seconds: int = 300
    image_endpoint: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    )
    image_download_hosts: list[str] = [
        "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        "dashscope-result-sh.oss-cn-shanghai.aliyuncs.com",
        "dashscope-result-sg.oss-ap-southeast-1.aliyuncs.com",
    ]
    providers: dict[str, str] = {
        "deepseek": "https://api.deepseek.com/v1",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "ollama": "http://ollama:11434/v1",
    }

    # signing_key：优先读取配置的密钥文件，否则使用直接配置值；拒绝长度不足的签名密钥。
    def signing_key(self) -> str:
        value = Path(self.secret_file).read_text().strip() if self.secret_file else self.secret_key
        if len(value) < 32:
            raise RuntimeError("Set HAI_SECRET_FILE or HAI_SECRET_KEY (at least 32 random characters)")
        return value


# settings：缓存并返回 Settings；运行中改环境变量不会自动刷新已缓存配置。
@lru_cache
def settings() -> Settings:
    return Settings()
