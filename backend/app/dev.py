# 文件职责：仅用于本机演示的启动器：准备开发配置、执行迁移并绑定回环地址；不用于容器生产入口。
"""Explicit localhost-only demo runner. Never used by the container deployment."""

import os
import secrets
from pathlib import Path


# main：准备演示数据和密钥路径，填充未显式提供的配置，再迁移数据库并启动 Uvicorn。
def main():
    root = Path(__file__).resolve().parents[2]
    data = Path(os.environ.get("HAI_DEMO_DIRECTORY", str(root / "data"))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("HAI_DEMO_PORT", "8014"))
    keyfile = data / "dev.key"
    if not keyfile.exists():
        keyfile.write_text(secrets.token_hex(32))
    os.environ.setdefault("HAI_SECRET_FILE", str(keyfile))
    os.environ.setdefault("HAI_INVITE_CODE", "local-demo")
    os.environ.setdefault("HAI_DATABASE_URL", "sqlite:///" + str(data / "demo.db").replace("\\", "/"))
    os.environ.setdefault("HAI_MEDIA_DIR", str(data / "media"))
    os.environ.setdefault("HAI_CHAT_RECORD_DIR", str(data / "chat-records"))
    os.environ.setdefault("HAI_CHECKPOINT_PATH", str(data / "checkpoints.sqlite"))
    os.environ.setdefault("HAI_QDRANT_PATH", str(data / "qdrant"))
    os.environ.setdefault("HAI_COOKIE_SECURE", "false")
    os.environ.setdefault("HAI_DEMO_MODE", "true")
    os.environ.setdefault("HAI_INLINE_TASKS", "true")
    os.environ.setdefault(
        "HAI_ALLOWED_ORIGINS", "http://127.0.0.1:5175,http://localhost:5175,http://127.0.0.1:8014"
    )
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(root / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend/migrations"))
    command.upgrade(cfg, "head")
    import uvicorn

    print(f"Local-only R3 demo. API: http://127.0.0.1:{port}/docs")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
