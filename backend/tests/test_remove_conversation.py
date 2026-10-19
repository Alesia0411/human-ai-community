# 文件职责：自动化测试：会话删除的账号隔离、持久隐藏及新消息恢复；具体保证以各测试断言为准。
from conftest import register, setup_chat

from app.agents import finish
from app.db import SessionLocal
from app.models import Role


# listed：读取测试账号会话列表，便于验证隐藏和重新出现。
def listed(client, headers):
    return [c["id"] for c in client.get("/api/v1/me/conversations", headers=headers).json()]


# send：通过测试客户端发送带去重标识的消息，返回响应以便后续断言。
def send(client, headers, cid, key):
    response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers,
                           json={"body": key, "client_message_id": key})
    assert response.status_code == 201
    return response.json()


# test_human_remove_is_account_specific_and_new_message_restores：验证：真人删除只影响本人，新消息使会话重新出现；范围以本函数断言为准。
def test_human_remove_is_account_specific_and_new_message_restores(client):
    a, b, ar, br, c, _ = setup_chat(client, "HUMAN_DIRECT")
    cid = c["id"]
    url = f"/api/v1/conversations/{cid}"
    send(client, a, cid, "existing")
    outsider, _ = register(client, "outsider")
    assert client.delete(url, headers=outsider).status_code == 404
    assert client.delete(url, headers=b).status_code == 200
    assert client.delete(url, headers=b).status_code == 200
    assert cid not in listed(client, b) and cid in listed(client, a)
    assert client.get(url + "/messages", headers=b).json() == []
    assert len(client.get(url + "/messages", headers=a).json()) == 1
    # A retry of an old request must not make a removed conversation reappear.
    send(client, a, cid, "existing")
    assert cid not in listed(client, b)
    send(client, a, cid, "new-message")
    assert cid in listed(client, b)
    assert client.get("/api/v1/me/conversations", headers=b).json()[0]["unread"] == 1
    # Exercise the opposite participant's flag and explicit reopen.
    assert client.delete(url, headers=a).status_code == 200
    assert cid not in listed(client, a) and cid in listed(client, b)
    reopened = client.post("/api/v1/conversations", headers=a, json={
        "kind": "HUMAN_DIRECT", "source_role_id": ar["id"], "target_role_id": br["id"],
    })
    assert reopened.status_code == 201 and reopened.json()["id"] == cid
    assert cid in listed(client, a)
    assert client.get(url + "/messages", headers=a).json() == []


# test_ai_remove_permission_running_guard_and_new_chat：验证：AI 会话删除的权限、运行中保护和新会话创建；范围以本函数断言为准。
def test_ai_remove_permission_running_guard_and_new_chat(client):
    a, b, ar, br, c, connection = setup_chat(client)
    cid = c["id"]
    url = f"/api/v1/conversations/{cid}"
    assert client.delete(url, headers=a).status_code == 404
    sent = send(client, b, cid, "old-ai-message")
    assert client.delete(url, headers=b).status_code == 409
    finish(sent["run_id"], error="CANCELLED")
    assert client.delete(url, headers=b).status_code == 200
    assert cid not in listed(client, b)
    assert client.get(url + "/messages", headers=b).json() == []
    fresh = client.post("/api/v1/conversations", headers=b, json={
        "kind": "AI_DIRECT", "source_role_id": br["id"], "target_role_id": ar["id"],
        "connection_id": connection["id"],
    }).json()
    assert fresh["id"] != cid and fresh["id"] in listed(client, b)
    assert cid not in listed(client, b)
    with SessionLocal() as db:
        assert not db.get(Role, ar["id"]).deleted


# test_open_socket_notifies_only_the_removing_participant：验证：删除会话通知只发送给执行删除的参与者；范围以本函数断言为准。
def test_open_socket_notifies_only_the_removing_participant(client):
    a, b, _, _, c, _ = setup_chat(client, "HUMAN_DIRECT")
    url = f"/api/v1/conversations/{c['id']}"
    ticket = client.post(url + "/socket-ticket", headers=b).json()["ticket"]
    with client.websocket_connect("/ws?ticket=" + ticket) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        assert client.delete(url, headers=b).status_code == 200
        for _ in range(10):
            notice = ws.receive_json()
            if notice["type"] == "conversation_removed":
                break
        assert notice["type"] == "conversation_removed"
    ticket = client.post(url + "/socket-ticket", headers=a).json()["ticket"]
    with client.websocket_connect("/ws?ticket=" + ticket) as ws:
        assert ws.receive_json()["type"] == "snapshot"
