# 文件职责：自动化测试：真人单侧清空、AI 上下文重置和 WebSocket 清空通知；具体保证以各测试断言为准。
from conftest import register, setup_chat
from sqlalchemy import select

from app.agents import context, finish
from app.db import SessionLocal
from app.models import Conversation, Message, Run


# send：通过测试客户端发送带去重标识的消息，返回响应以便后续断言。
def send(client, headers, cid, ident):
    response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers,
                           json={"body": ident, "client_message_id": ident})
    assert response.status_code == 201, response.text
    return response.json()


# test_human_clear_is_private_persistent_and_socket_updates：验证：真人清空只影响本人且通过 WebSocket 保持视图一致；范围以本函数断言为准。
def test_human_clear_is_private_persistent_and_socket_updates(client):
    a, b, _, _, c, _ = setup_chat(client, "HUMAN_DIRECT")
    cid = c["id"]
    url = f"/api/v1/conversations/{cid}"
    send(client, b, cid, "old-one")
    send(client, a, cid, "old-two")
    outsider, _ = register(client, "outsider")
    assert client.delete(url + "/messages", headers=outsider).status_code == 404
    ticket = client.post(url + "/socket-ticket", headers=b).json()["ticket"]
    with client.websocket_connect("/ws?ticket=" + ticket, headers={"origin": "http://testserver"}) as ws:
        assert len(ws.receive_json()["messages"]) == 2
        assert client.delete(url + "/messages", headers=b).json()["cleared_seq"] == 2
        for _ in range(10):
            snapshot = ws.receive_json()
            if snapshot.get("cleared_seq") == 2:
                break
        assert snapshot["messages"] == []
    assert client.get(url + "/messages?before=100", headers=b).json() == []
    assert len(client.get(url + "/messages", headers=a).json()) == 2
    assert client.delete(url + "/messages", headers=b).json()["cleared_seq"] == 2
    send(client, a, cid, "new-three")
    assert [m["body"] for m in client.get(url + "/messages", headers=b).json()] == ["new-three"]
    assert len(client.get(url + "/messages", headers=a).json()) == 3
    assert client.get("/api/v1/me/conversations", headers=b).json()[0]["unread"] == 1


# test_ai_clear_blocks_inflight_then_resets_context_and_hides_old_run：验证：AI 清空与进行中任务互斥，重置上下文并隔离旧任务；范围以本函数断言为准。
def test_ai_clear_blocks_inflight_then_resets_context_and_hides_old_run(client):
    owner, visitor, _, _, c, _ = setup_chat(client)
    cid = c["id"]
    url = f"/api/v1/conversations/{cid}"
    original = send(client, visitor, cid, "secret-old-history")
    assert client.delete(url + "/messages", headers=owner).status_code == 404
    assert client.delete(url + "/messages", headers=visitor).status_code == 409
    finish(original["run_id"], error="CANCELLED")
    with SessionLocal() as db:
        persona = db.get(Conversation, cid).persona_snapshot
    assert client.delete(url + "/messages", headers=visitor).status_code == 200
    assert client.get(url + "/messages", headers=visitor).json() == []
    ticket = client.post(url + "/socket-ticket", headers=visitor).json()["ticket"]
    with client.websocket_connect("/ws?ticket=" + ticket) as ws:
        snapshot = ws.receive_json()
        assert snapshot["messages"] == [] and snapshot["run"] is None
    fresh = send(client, visitor, cid, "new-context")
    with SessionLocal() as db:
        assert db.get(Conversation, cid).persona_snapshot == persona
        assert len(list(db.scalars(select(Message).where(Message.conversation_id == cid)))) == 1
        db.get(Run, fresh["run_id"]).status = "RUNNING"
        db.commit()
    prompt = str(context(fresh["run_id"]))
    assert "new-context" in prompt and "secret-old-history" not in prompt
