# 文件职责：为浏览器演示准备独立临时应用目录并启动本机服务，避免复用用户正常数据库。
"""Dedicated synthetic recording server: never reuses the user's database or keys."""

import os
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="hai-synthetic-video-"))
for key in list(os.environ):
    if key.startswith("HAI_"):
        del os.environ[key]
os.environ.update(
    HAI_DEMO_DIRECTORY=str(root),
    HAI_DATABASE_URL="sqlite:///" + (root / "demo.db").as_posix(),
    HAI_SECRET_FILE=str(root / "dev.key"),
    HAI_CHAT_RECORD_DIR=str(root / "chat-records"),
    HAI_DEMO_PORT="18013",
    HAI_DEMO_MODE="true",
    HAI_INLINE_TASKS="true",
    HAI_INVITE_CODE="local-demo",
    HAI_RETRIEVAL_MODE="hybrid_rerank",
    HAI_ALLOWED_ORIGINS="http://127.0.0.1:15174",
)

from app.dev import main

main()
