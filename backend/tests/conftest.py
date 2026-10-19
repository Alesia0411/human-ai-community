# 文件职责：pytest 公共准备：先设置临时数据库与文件目录，再导入应用；不针对真实用户数据库运行。
import os
import tempfile
from pathlib import Path

import pytest

# 必须先配置临时路径再导入应用；db.engine 创建后不会因修改环境变量自动切换。
test_dir = Path(tempfile.mkdtemp(prefix="hai-tests-"))
os.environ.update(
    HAI_DATABASE_URL="sqlite:///" + str(test_dir / "test.db").replace("\\", "/"),
    HAI_SECRET_KEY="test-only-not-a-production-key-0000000000",
    HAI_INVITE_CODE="test-invite",
    HAI_DEMO_MODE="true",
    HAI_INLINE_TASKS="false",
    HAI_COOKIE_SECURE="false",
    HAI_MEDIA_DIR=str(test_dir / "media"),
    HAI_CHAT_RECORD_DIR=str(test_dir / "chat-records"),
    HAI_ALLOWED_ORIGINS="http://testserver",
    HAI_CHECKPOINT_PATH=str(test_dir / "checkpoints.sqlite"),
    HAI_QDRANT_PATH=str(test_dir / "qdrant"),
)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app, attempts  # noqa: E402


# client：每个测试重建临时 SQL 表、清除限流状态，并通过 TestClient 提供应用请求入口。
@pytest.fixture
def client():
    # 此处会删表，依赖上方临时数据库隔离；禁止把 fixture 改为连接正常用户库。
    Base.metadata.drop_all(engine)
    # 直接按 ORM 建表，不经过 Alembic；迁移行为由 test_migrations 单独验证。
    Base.metadata.create_all(engine)
    attempts.clear()
    with TestClient(app) as c:
        yield c


# register：通过注册接口建立测试账号，清除 Cookie 后返回 Bearer 请求头和账号 ID。
def register(client, name):
    r = client.post(
        "/api/v1/auth/register",
        json={
            "username": name,
            "nickname": name,
            "password": "test-password-123",
            "invite_code": "test-invite",
        },
    )
    assert r.status_code == 201, r.text
    # 同一 TestClient 模拟多账号时避免 Cookie 串号；后续明确传 Bearer 身份。
    client.cookies.clear()
    return {"Authorization": "Bearer " + r.json()["access_token"]}, r.json()["account"]["id"]


# role：创建带默认人设的测试角色；changes 可覆盖默认字段。
def role(client, headers, name="role", **changes):
    data = {
        "name": name,
        "bio": "hello",
        "persona": "温和、诚实",
        "visibility": "PUBLIC",
        "ai_enabled": True,
        "public_ai": True,
        **changes,
    }
    r = client.post("/api/v1/me/roles", json=data, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# setup_chat：准备 Alice、Bob、Bob 的 mock 连接及双方会话，返回后续测试需要的对象。
def setup_chat(client, kind="AI_DIRECT"):
    a, aid = register(client, "alice")
    b, bid = register(client, "bob")
    ar, br = role(client, a, "Alice"), role(client, b, "Bob")
    connection = client.post(
        "/api/v1/me/connections",
        headers=b,
        json={"provider": "mock", "model": "test", "api_key": "secret-key-test"},
    ).json()
    c = client.post(
        "/api/v1/conversations",
        headers=b,
        json={
            "kind": kind,
            "source_role_id": br["id"],
            "target_role_id": ar["id"],
            "connection_id": connection["id"],
        },
    )
    assert c.status_code == 201, c.text
    return a, b, ar, br, c.json(), connection
