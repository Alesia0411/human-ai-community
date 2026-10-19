# 文件职责：自动化测试：聊天文件、五条摘要、上下文组合与清空后修复的一致性；具体保证以各测试断言为准。
import asyncio
import json

import pytest
from conftest import setup_chat
from sqlalchemy import select

from app import chat_memory
from app.agents import context, execute_run, finish
from app.db import SessionLocal
from app.models import CallLedger, ChatMemory, Connection, Conversation, Role, Run


# send：通过测试客户端发送带去重标识的消息，返回响应以便后续断言。
def send(client, headers, cid, body):
    response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers,
                           json={"body": body, "client_message_id": body})
    assert response.status_code == 201
    return response.json()


# records：读取指定账号和会话的 JSONL 文件，逐行解析供内容断言。
def records(account, cid, name="messages.jsonl"):
    return [json.loads(line) for line in chat_memory.record_path(account, cid, name).read_text(encoding="utf-8").splitlines()]


# test_human_files_both_directions_clear_delete_and_retry：验证：真人双方文件、重试去重、单侧清空/删除以及磁盘失败后的修复；范围以本函数断言为准。
def test_human_files_both_directions_clear_delete_and_retry(client, monkeypatch):
    a, b, ar, br, c, _ = setup_chat(client, "HUMAN_DIRECT")
    cid = c["id"]
    with SessionLocal() as db:
        aid, bid = db.get(Role, ar["id"]).owner_id, db.get(Role, br["id"]).owner_id
    send(client, a, cid, "human-one")
    send(client, b, cid, "human-two")
    send(client, b, cid, "human-two")
    assert len(records(aid, cid)) == len(records(bid, cid)) == 2
    assert client.delete(f"/api/v1/conversations/{cid}/messages", headers=b).status_code == 200
    assert records(bid, cid) == [] and len(records(aid, cid)) == 2
    send(client, a, cid, "human-three")
    assert [r["body"] for r in records(bid, cid)] == ["human-three"]
    assert client.delete(f"/api/v1/conversations/{cid}", headers=b).status_code == 200
    assert records(bid, cid) == [] and len(records(aid, cid)) == 3
    original = chat_memory.atomic_jsonl
    # disk_full：人为抛出磁盘写入异常，验证业务数据提交后文件仍可修复。
    def disk_full(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(chat_memory, "atomic_jsonl", disk_full)
    send(client, a, cid, "human-four")
    with SessionLocal() as db:
        assert db.get(Conversation, cid).files_dirty
    monkeypatch.setattr(chat_memory, "atomic_jsonl", original)
    chat_memory.repair_files()
    assert [r["body"] for r in records(bid, cid)] == ["human-four"]


# test_five_message_batches_context_files_retract_and_reset：验证：五条分组摘要、最近消息与长期记忆注入、撤回和删除后的文件状态；范围以本函数断言为准。
def test_five_message_batches_context_files_retract_and_reset(client):
    _, visitor, target, source, c, _ = setup_chat(client)
    cid = c["id"]
    with SessionLocal() as db:
        owner, target_owner = db.get(Role, source["id"]).owner_id, db.get(Role, target["id"]).owner_id
    for i in range(1, 6):
        sent = send(client, visitor, cid, f"turn-{i}")
        asyncio.run(execute_run(sent["run_id"]))
        with SessionLocal() as db:
            assert db.get(Run, sent["run_id"]).status == "SUCCEEDED"
        count = len(records(owner, cid, "long_term_memory.jsonl"))
        assert count == (i * 2) // 5
    full = records(owner, cid)
    assert len(full) == 10
    summaries = records(owner, cid, "long_term_memory.jsonl")
    assert [s["message_ids"] for s in summaries] == [[r["id"] for r in full[:5]], [r["id"] for r in full[5:]]]
    assert not chat_memory.record_path(target_owner, cid, "messages.jsonl").exists()
    with SessionLocal() as db:
        calls = list(db.scalars(select(CallLedger).where(CallLedger.step_key.like("memory:%"))))
        assert len(calls) == 2 and all(c.state == "DONE" for c in calls)
    fresh = send(client, visitor, cid, "current-eleven")
    with SessionLocal() as db:
        db.get(Run, fresh["run_id"]).status = "RUNNING"
        db.commit()
    prompt = context(fresh["run_id"])
    dialogue = [m["content"] for m in prompt if m["role"] != "system"]
    assert dialogue == [r["body"] for r in records(owner, cid)[-5:]]
    assert dialogue.count("current-eleven") == 1
    assert all(s["summary"] in prompt[0]["content"] for s in summaries)
    # Repeated preparation cannot create duplicate summary lines.
    asyncio.run(chat_memory.summarize(fresh["run_id"]))
    assert len(records(owner, cid, "long_term_memory.jsonl")) == 2
    finish(fresh["run_id"], error="CANCELLED")
    assert client.post(f"/api/v1/messages/{full[0]['id']}/retract", headers=visitor).status_code == 200
    assert records(owner, cid, "long_term_memory.jsonl") == []
    assert records(owner, cid)[0]["body"] == "[消息已撤回]"
    assert client.delete(f"/api/v1/conversations/{cid}", headers=visitor).status_code == 200
    assert records(owner, cid) == records(owner, cid, "long_term_memory.jsonl") == []
    chat_memory.repair_files()
    assert records(owner, cid) == []
    with SessionLocal() as db:
        assert not list(db.scalars(select(ChatMemory).where(ChatMemory.conversation_id == cid)))


# test_invalid_summary_cannot_publish_partial_ai_message：验证：无效摘要不能使 AI 消息被部分发布；范围以本函数断言为准。
def test_invalid_summary_cannot_publish_partial_ai_message(client):
    _, visitor, _, _, c, _ = setup_chat(client)
    sent = send(client, visitor, c["id"], "original")
    with SessionLocal() as db:
        db.get(Run, sent["run_id"]).status = "RUNNING"
        db.commit()
    finish(sent["run_id"], {"text": "must-not-publish", "summaries": [{
        "conversation_id": c["id"], "batch_key": "bad", "message_ids": [], "summary": "bad", "end_seq": 2,
    }]})
    messages = client.get(f"/api/v1/conversations/{c['id']}/messages", headers=visitor).json()
    assert [m["body"] for m in messages] == ["original"]
    with SessionLocal() as db:
        assert db.get(Conversation, c["id"]).seq == 1


# test_oversized_context_is_rejected_instead_of_silently_trimming：验证：超限上下文显式拒绝并记录原因，而不是静默丢弃长期记忆；范围以本函数断言为准。
def test_oversized_context_is_rejected_instead_of_silently_trimming(client):
    from fastapi import HTTPException

    from app.gateways import model_call

    _, visitor, target, _, c, connection = setup_chat(client)
    sent = send(client, visitor, c["id"], "latest")
    with SessionLocal() as db:
        run = db.get(Run, sent["run_id"])
        run.status, run.reserved = "RUNNING", 32000
        db.get(Connection, connection["id"]).provider = "ollama"
        db.get(Connection, connection["id"]).model = "qwen3:8b"
        db.get(Role, target["id"]).provider_scope = ["mock", "ollama"]
        db.commit()
    with pytest.raises(HTTPException) as error:
        asyncio.run(model_call(sent["run_id"], "model:0", [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "晨" * 12000},
            {"role": "user", "content": "latest"},
        ]))
    assert error.value.detail == "MEMORY_CONTEXT_LIMIT"
    with SessionLocal() as db:
        usage = db.get(Run, sent["run_id"]).context["context_usage"]
        assert usage["method"] == "qwen3_template"
        assert usage["dominant"] == "dialogue"
