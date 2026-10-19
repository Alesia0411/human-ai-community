# 文件职责：自动化测试：账号、角色、付款、消息幂等及隐私权限的基础行为；具体保证以各测试断言为准。
import asyncio
from io import BytesIO

from conftest import register, role, setup_chat
from PIL import Image
from sqlalchemy import select

from app.agents import execute_run, finish
from app.db import SessionLocal
from app.models import Connection, Run
from app.security import decrypt


# test_history_pagination_and_connection_test_is_private：验证：消息历史分页与连接测试结果的账号隔离；范围以本函数断言为准。
def test_history_pagination_and_connection_test_is_private(client):
    from app.models import Message

    a, b, ar, br, c, connection = setup_chat(client)
    with SessionLocal() as db:
        db.add_all(
            [
                Message(
                    conversation_id=c["id"],
                    seq=i,
                    sender_id="AGENT",
                    role_id=ar["id"],
                    client_id=str(i),
                    actor="AI",
                    body=str(i),
                )
                for i in range(1, 206)
            ]
        )
        db.commit()
    url = "/api/v1/conversations/" + c["id"] + "/messages"
    latest = client.get(url, headers=b).json()
    assert [m["seq"] for m in latest] == list(range(106, 206))
    assert client.get(url + "?before=106", headers=b).json()[0]["seq"] == 6
    assert client.get(url + "?after=200", headers=b).json()[-1]["seq"] == 205
    endpoint = "/api/v1/me/connections/" + connection["id"] + "/test"
    assert client.post(endpoint, headers=a).status_code == 404
    assert client.post(endpoint, headers=b).json()["mode"] == "mock"


# send：通过测试客户端发送带去重标识的消息，返回响应以便后续断言。
def send(client, b, c, text="hello", ident="msg-1"):
    return client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": text, "client_message_id": ident},
    )


# edit：复用测试中的角色编辑请求，保留必要版本字段。
def edit(client, h, r, **changes):
    data = {
        k: r[k]
        for k in (
            "name",
            "bio",
            "persona",
            "visibility",
            "ai_enabled",
            "public_ai",
            "provider_scope",
            "version",
        )
    }
    return client.patch("/api/v1/roles/" + r["id"], headers=h, json={**data, **changes})


# test_registration_invite_logout_and_session_revocation：验证：注册邀请码、退出登录及会话撤销；范围以本函数断言为准。
def test_registration_invite_logout_and_session_revocation(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"username": "test", "nickname": "test", "password": "valid-password", "invite_code": "wrong"},
    )
    assert r.status_code == 403
    a, _ = register(client, "alice")
    assert client.get("/api/v1/me", headers=a).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=a).status_code == 200
    assert client.get("/api/v1/me", headers=a).status_code == 401


# test_refresh_rotates_and_requires_csrf：验证：刷新凭证轮换且需要 CSRF 校验；范围以本函数断言为准。
def test_refresh_rotates_and_requires_csrf(client):
    r = client.post(
        "/api/v1/auth/register",
        json={
            "username": "test",
            "nickname": "test",
            "password": "valid-password",
            "invite_code": "test-invite",
        },
    )
    old = r.json()["access_token"]
    assert client.post("/api/v1/auth/refresh").status_code == 403
    refreshed = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": r.json()["csrf_token"]})
    assert refreshed.status_code == 200
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer " + old}).status_code == 401


# test_role_ownership_private_directory_and_version：验证：角色归属、私有目录过滤与编辑版本检查；范围以本函数断言为准。
def test_role_ownership_private_directory_and_version(client):
    a, _ = register(client, "alice")
    b, _ = register(client, "bob")
    r = role(client, a, visibility="PRIVATE")
    assert client.get("/api/v1/roles", headers=b).json()["items"] == []
    assert client.delete("/api/v1/roles/" + r["id"], headers=b).status_code == 404
    assert edit(client, a, r, bio="new").status_code == 200
    assert edit(client, a, r, bio="stale").status_code == 409


# test_owner_cannot_observe_ai_conversation_or_run：验证：角色拥有者不能查询访客 AI 会话和任务；范围以本函数断言为准。
def test_owner_cannot_observe_ai_conversation_or_run(client):
    a, b, ar, br, c, conn = setup_chat(client)
    before = client.get("/api/v1/roles/" + ar["id"], headers=a).json()
    sent = send(client, b, c)
    assert sent.status_code == 201
    rid = sent.json()["run_id"]
    assert client.get("/api/v1/me/conversations", headers=a).json() == []
    assert client.get("/api/v1/me/runs", headers=a).json() == []
    for path in ("/conversations/" + c["id"] + "/messages", "/runs/" + rid):
        assert client.get("/api/v1" + path, headers=a).status_code == 404
    assert send(client, a, c, "takeover").status_code == 404
    assert client.post("/api/v1/conversations/" + c["id"] + "/socket-ticket", headers=a).status_code == 404
    asyncio.run(execute_run(rid))
    assert client.get("/api/v1/roles/" + ar["id"], headers=a).json() == before
    msgs = client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()
    assert [m["actor"] for m in msgs] == ["HUMAN", "AI"]
    assert "本地演示" in msgs[-1]["body"]


# test_ai_visitor_isolation_and_own_payment：验证：访客间 AI 会话隔离且各自承担自己的调用；范围以本函数断言为准。
def test_ai_visitor_isolation_and_own_payment(client):
    a, b, ar, br, c, conn = setup_chat(client)
    outsider, _ = register(client, "charlie")
    assert client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=outsider).status_code == 404
    rid = send(client, b, c).json()["run_id"]
    with SessionLocal() as db:
        r = db.get(Run, rid)
        assert r.connection_id == conn["id"]
        assert r.payer_id == db.get(Connection, conn["id"]).owner_id
        assert r.payer_id != db.get(Connection, conn["id"]).id


# test_message_idempotency_and_reservation_once：验证：重复消息请求只写入和预留一次；范围以本函数断言为准。
def test_message_idempotency_and_reservation_once(client):
    _, b, _, _, c, _ = setup_chat(client)
    one = send(client, b, c)
    two = send(client, b, c)
    assert one.json()["id"] == two.json()["id"]
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Run)))) == 1
    rid = one.json()["run_id"]
    asyncio.run(execute_run(rid))
    asyncio.run(execute_run(rid))
    assert len(client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()) == 2
    assert client.get("/api/v1/me", headers=b).json()["reserved"] == 0


# test_running_conversation_rejects_new_run：验证：已有进行中任务的会话拒绝重复启动生成；范围以本函数断言为准。
def test_running_conversation_rejects_new_run(client):
    _, b, _, _, c, _ = setup_chat(client)
    assert send(client, b, c).status_code == 201
    assert send(client, b, c, ident="msg-2").status_code == 409


# test_model_keys_encrypted_and_cross_owner_connection_denied：验证：模型 Key 加密保存且不能借用其他账号连接；范围以本函数断言为准。
def test_model_keys_encrypted_and_cross_owner_connection_denied(client):
    a, b, ar, br, c, conn = setup_chat(client)
    with SessionLocal() as db:
        ciphertext = db.get(Connection, conn["id"]).encrypted_key
        assert "secret-key-test" not in ciphertext
        assert decrypt(ciphertext) == "secret-key-test"
    assert "secret-key-test" not in client.get("/api/v1/me/connections", headers=b).text
    bad = client.post(
        "/api/v1/conversations",
        headers=a,
        json={
            "kind": "AI_DIRECT",
            "source_role_id": ar["id"],
            "target_role_id": br["id"],
            "connection_id": conn["id"],
        },
    )
    assert bad.status_code == 422
    assert client.delete("/api/v1/me/connections/" + conn["id"], headers=a).status_code == 404


# test_revocation_prevents_queued_publication：验证：排队后撤回授权会阻止内容发布；范围以本函数断言为准。
def test_revocation_prevents_queued_publication(client):
    a, b, ar, _, c, _ = setup_chat(client)
    rid = send(client, b, c).json()["run_id"]
    assert edit(client, a, ar, ai_enabled=False).status_code == 200
    asyncio.run(execute_run(rid))
    assert len(client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()) == 1
    assert client.get("/api/v1/me", headers=b).json()["reserved"] == 0


# test_persona_edit_is_pinned_but_policy_revokes：验证：会话保留人设快照而权限变更仍可撤销任务；范围以本函数断言为准。
def test_persona_edit_is_pinned_but_policy_revokes(client):
    a, b, ar, _, c, _ = setup_chat(client)
    assert edit(client, a, ar, persona="new profile").status_code == 200
    from app.models import Conversation

    with SessionLocal() as db:
        assert db.get(Conversation, c["id"]).persona_snapshot == "温和、诚实"
    rid = send(client, b, c).json()["run_id"]
    asyncio.run(execute_run(rid))
    assert client.get("/api/v1/runs/" + rid, headers=b).json()["status"] == "SUCCEEDED"


# test_cancel_prevents_publication_and_settles_once：验证：取消阻止发布且预算只结算一次；范围以本函数断言为准。
def test_cancel_prevents_publication_and_settles_once(client):
    _, b, _, _, c, _ = setup_chat(client)
    rid = send(client, b, c).json()["run_id"]
    assert client.post("/api/v1/runs/" + rid + "/cancel", headers=b).status_code == 200
    finish(rid, error="CANCELLED")
    finish(rid, error="CANCELLED")
    assert client.get("/api/v1/me", headers=b).json()["reserved"] == 0
    assert len(client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()) == 1


# test_unknown_usage_never_reports_free_or_reexecutes：验证：未知消费不会误报免费或再次执行；范围以本函数断言为准。
def test_unknown_usage_never_reports_free_or_reexecutes(client):
    _, b, _, _, c, _ = setup_chat(client)
    rid = send(client, b, c).json()["run_id"]
    with SessionLocal() as db:
        r = db.get(Run, rid)
        r.status = "RUNNING"
        r.provider_called = True
        amount = r.reserved
        db.commit()
    finish(rid, error="GENERATION_STOPPED")
    asyncio.run(execute_run(rid))
    account = client.get("/api/v1/me", headers=b).json()
    assert account["used"] == amount and account["reserved"] == 0
    assert client.get("/api/v1/runs/" + rid, headers=b).json()["status"] == "UNKNOWN_USAGE"


# test_budget_rejection_and_forged_actor：验证：预算不足被拒绝且客户端不能伪造 AI 身份；范围以本函数断言为准。
def test_budget_rejection_and_forged_actor(client):
    _, b, _, _, c, _ = setup_chat(client)
    client.patch("/api/v1/me/budgets", headers=b, json={"budget": 1000})
    assert send(client, b, c).status_code == 409
    assert (
        client.post(
            "/api/v1/conversations/" + c["id"] + "/messages",
            headers=b,
            json={"body": "fake", "client_message_id": "x", "actor": "AI"},
        ).status_code
        == 422
    )


# test_human_chat_is_separate_and_both_can_reply：验证：真人聊天与 AI 任务分开且双方能回复；范围以本函数断言为准。
def test_human_chat_is_separate_and_both_can_reply(client):
    a, b, ar, br, c, _ = setup_chat(client, "HUMAN_DIRECT")
    assert len(client.get("/api/v1/me/conversations", headers=a).json()) == 1
    assert send(client, b, c).status_code == 201
    assert send(client, a, c, "reply").status_code == 201
    assert all(
        m["actor"] == "HUMAN"
        for m in client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()
    )


# test_block_covers_all_roles_and_pending_ai：验证：账号屏蔽覆盖多个角色与待执行 AI 任务；范围以本函数断言为准。
def test_block_covers_all_roles_and_pending_ai(client):
    a, b, ar, br, c, _ = setup_chat(client)
    rid = send(client, b, c).json()["run_id"]
    assert client.put("/api/v1/roles/" + br["id"] + "/block", headers=a).status_code == 200
    asyncio.run(execute_run(rid))
    assert client.get("/api/v1/runs/" + rid, headers=b).json()["status"] == "FAILED"
    assert send(client, b, c, ident="second").status_code == 403


# test_post_comment_permissions_and_hidden_role：验证：动态评论权限与隐藏角色不可见性；范围以本函数断言为准。
def test_post_comment_permissions_and_hidden_role(client):
    a, b, ar, br, _, _ = setup_chat(client)
    p = client.post("/api/v1/posts", headers=a, json={"role_id": ar["id"], "body": "a post"}).json()
    assert (
        client.post("/api/v1/posts", headers=b, json={"role_id": ar["id"], "body": "forged"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/posts/" + p["id"] + "/comments", headers=b, json={"role_id": br["id"], "body": "nice"}
        ).status_code
        == 201
    )
    assert client.delete("/api/v1/posts/" + p["id"], headers=b).status_code == 404
    assert edit(client, a, ar, visibility="PRIVATE").status_code == 200
    assert client.get("/api/v1/posts", headers=b).json()["items"] == []
    assert client.get("/api/v1/posts/" + p["id"] + "/comments", headers=b).status_code == 404


# test_private_image_acl_and_invalid_upload：验证：私有图片访问权限和非法上传拒绝；范围以本函数断言为准。
def test_private_image_acl_and_invalid_upload(client):
    a, b, _, _, c, _ = setup_chat(client)
    buf = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    r = client.post(
        "/api/v1/assets",
        headers=b,
        data={"conversation_id": c["id"]},
        files={"file": ("x.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    assert client.get("/api/v1/assets/" + aid + "/content", headers=a).status_code == 404
    assert client.get("/api/v1/assets/" + aid + "/content", headers=b).status_code == 200
    assert (
        client.post(
            "/api/v1/assets", headers=b, files={"file": ("x.png", b"not an image", "image/png")}
        ).status_code
        == 422
    )


# test_socket_ticket_single_use_and_private：验证：WebSocket 票据只能使用一次且绑定可访问会话；范围以本函数断言为准。
def test_socket_ticket_single_use_and_private(client):
    a, b, _, _, c, _ = setup_chat(client)
    ticket = client.post("/api/v1/conversations/" + c["id"] + "/socket-ticket", headers=b).json()["ticket"]
    with client.websocket_connect("/ws?ticket=" + ticket) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?ticket=" + ticket):
            pass


# test_provider_endpoint_not_user_controlled：验证：模型服务地址不能由用户请求任意指定；范围以本函数断言为准。
def test_provider_endpoint_not_user_controlled(client):
    b, _ = register(client, "bob")
    assert (
        client.post(
            "/api/v1/me/connections",
            headers=b,
            json={"provider": "http://127.0.0.1", "model": "x", "api_key": "key"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/me/connections",
            headers=b,
            json={"provider": "deepseek", "model": "x", "api_key": "key", "base_url": "http://localhost"},
        ).status_code
        == 422
    )
