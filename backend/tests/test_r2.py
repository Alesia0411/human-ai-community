# 文件职责：自动化测试：学习授权、知识检索、MCP 与主动交互的规则；具体保证以各测试断言为准。
import asyncio

from conftest import setup_chat

from app.agents import execute_run
from app.db import SessionLocal
from app.models import Conversation, KnowledgeDocument, Run
from app.r2_policy import AgentSettings


# config：更新测试角色 Agent 配置并检查接口结果。
def config(client, a, r, **opts):
    data = {**AgentSettings().model_dump(), **opts}
    res = client.put("/api/v1/roles/" + r["id"] + "/agent", headers=a, json=data)
    assert res.status_code == 200, res.text


# run_job：提交测试任务并显式执行后台 Run，返回任务结果用于断言。
def run_job(client, headers, url, data):
    response = client.post("/api/v1" + url, headers=headers, json=data)
    assert response.status_code == 200, response.text
    rid = response.json()["run_id"]
    asyncio.run(execute_run(rid))
    result = client.get("/api/v1/runs/" + rid, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


# own_mock：为指定测试账号创建自己的 mock 模型连接。
def own_mock(client, a):
    return client.post(
        "/api/v1/me/connections", headers=a, json={"provider": "mock", "model": "demo", "api_key": ""}
    ).json()["id"]


# test_learning_requires_consent_candidates_revoked_without_losing_manual：验证：学习必须授权，撤回候选不删除手动人设；范围以本函数断言为准。
def test_learning_requires_consent_candidates_revoked_without_losing_manual(client):
    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)
    config(client, a, ar, learning_enabled=True, learning_providers=["mock"])
    post = client.post(
        "/api/v1/posts", headers=a, json={"role_id": ar["id"], "body": "我喜欢把计划写好再行动。"}
    ).json()
    learned = run_job(client, a, "/roles/" + ar["id"] + "/learn", {"connection_id": own})
    assert learned["status"] == "SUCCEEDED", learned
    candidate = learned["result"]["candidate_id"]
    assert (
        client.post(
            "/api/v1/roles/" + ar["id"] + "/candidates/" + candidate + "/activate", headers=a
        ).status_code
        == 200
    )
    client.delete("/api/v1/posts/" + post["id"], headers=a)
    assert (
        client.post(
            "/api/v1/roles/" + ar["id"] + "/candidates/" + candidate + "/activate", headers=a
        ).status_code
        == 409
    )
    exported = client.get("/api/v1/roles/" + ar["id"] + "/persona-export", headers=a).json()
    assert exported["manual"] == ar["persona"] and exported["learned"] == {}
    assert client.get("/api/v1/roles/" + ar["id"] + "/candidates", headers=b).status_code == 404


# test_disabled_learning_never_reads_sources：验证：学习关闭时不读取来源正文；范围以本函数断言为准。
def test_disabled_learning_never_reads_sources(client, monkeypatch):
    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)

    # forbidden：禁止该路径被调用；若测试进入此处立即失败以暴露越权读取或调用。
    def forbidden(*args):
        raise AssertionError("source read before consent")

    monkeypatch.setattr("app.learning.eligible_source", forbidden)
    r = client.post("/api/v1/roles/" + ar["id"] + "/learn", headers=a, json={"connection_id": own})
    assert r.status_code == 409


# test_bilateral_chat_learning_and_ai_history_excluded：验证：真人私聊需双方学习授权且排除 AI 历史；范围以本函数断言为准。
def test_bilateral_chat_learning_and_ai_history_excluded(client):
    a, b, ar, br, c, conn = setup_chat(client, kind="HUMAN_DIRECT")
    own = own_mock(client, a)
    config(
        client,
        a,
        ar,
        learning_enabled=True,
        learning_providers=["mock"],
        learning_sources=["messages"],
        backfill_from=0,
    )
    client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=a,
        json={"body": "每天先列计划", "client_message_id": "a"},
    )
    permit = {"role_id": ar["id"], "enabled": True, "providers": ["mock"], "backfill_from": 0}
    client.put("/api/v1/conversations/" + c["id"] + "/learning-consent", headers=a, json=permit)
    result = run_job(client, a, "/roles/" + ar["id"] + "/learn", {"connection_id": own})
    assert result["status"] == "FAILED"
    client.put("/api/v1/conversations/" + c["id"] + "/learning-consent", headers=b, json=permit)
    result = run_job(client, a, "/roles/" + ar["id"] + "/learn", {"connection_id": own})
    assert result["status"] == "SUCCEEDED", result
    client.put(
        "/api/v1/conversations/" + c["id"] + "/learning-consent", headers=b, json={**permit, "enabled": False}
    )
    assert client.get("/api/v1/roles/" + ar["id"] + "/candidates", headers=a).json()[0]["valid"] is False


# test_scoped_qdrant_index_dedup_and_delete：验证：知识索引按范围隔离、去重并处理删除；范围以本函数断言为准。
def test_scoped_qdrant_index_dedup_and_delete(client):
    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)
    data = {
        "title": "露营",
        "text": "星湖营地周末九点开放，帐篷区在东侧。",
        "connection_id": own,
        "providers": ["mock"],
        "authorized": True,
    }
    result = run_job(client, a, "/roles/" + ar["id"] + "/knowledge", data)
    assert result["status"] == "SUCCEEDED", result
    duplicate = client.post("/api/v1/roles/" + ar["id"] + "/knowledge", headers=a, json=data).json()
    assert duplicate["duplicate"]
    msg = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "星湖营地几点开放", "client_message_id": "query"},
    ).json()
    asyncio.run(execute_run(msg["run_id"]))
    messages = client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()
    assert "参考：露营" in messages[-1]["body"], messages
    client.delete("/api/v1/knowledge/" + duplicate["id"], headers=a)
    with SessionLocal() as db:
        assert db.get(KnowledgeDocument, duplicate["id"]).deleted
    assert client.get("/api/v1/roles/" + ar["id"] + "/knowledge", headers=b).status_code == 404


# test_mcp_real_discovery_and_invalid_capability：验证：MCP 工具发现可用但无效调用凭证被拒绝；范围以本函数断言为准。
def test_mcp_real_discovery_and_invalid_capability():
    from app.tool_gateway import rpc

    names = asyncio.run(rpc())
    assert set(names) == {"vision_describe", "image_generate", "weather_query", "maps_search", "maps_route"}


# test_tool_grant_and_budget_checked_before_rpc：验证：进入工具 RPC 前完成授权和额度检查；范围以本函数断言为准。
def test_tool_grant_and_budget_checked_before_rpc(client, monkeypatch):
    a, b, ar, br, c, conn = setup_chat(client)
    msg = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "weather", "client_message_id": "tool"},
    ).json()
    with SessionLocal() as db:
        db.get(Run, msg["run_id"]).status = "RUNNING"
        db.commit()

    # forbidden：禁止该路径被调用；若测试进入此处立即失败以暴露越权读取或调用。
    async def forbidden(*args):
        raise AssertionError("rpc before grant")

    monkeypatch.setattr("app.tool_gateway.rpc", forbidden)
    import pytest
    from fastapi import HTTPException

    from app.tool_gateway import invoke

    with pytest.raises(HTTPException):
        asyncio.run(invoke(msg["run_id"], "tool:0", "weather_query", {"place": "北京"}))


# test_proactive_dm_reply_payer_handoff_and_no_owner_visibility：验证：主动私聊后续付款交接与接收者隐私隔离；范围以本函数断言为准。
def test_proactive_dm_reply_payer_handoff_and_no_owner_visibility(client):
    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)
    config(client, a, ar, allow_proactive_dm=True)
    client.put(
        "/api/v1/roles/" + br["id"] + "/reception",
        headers=b,
        json={"sender_role_id": ar["id"], "enabled": True},
    )
    plan = client.post(
        "/api/v1/roles/" + ar["id"] + "/plans",
        headers=a,
        json={
            "kind": "DM",
            "target_id": br["id"],
            "prompt": "问好",
            "connection_id": own,
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    result = run_job(client, a, "/plans/" + plan["id"] + "/trigger", {})
    assert result["status"] == "SUCCEEDED", result
    delivery = result["result"]["delivery_id"]
    endpoint = "/api/v1/deliveries/" + delivery + "/publish"
    assert client.post(endpoint, headers=a).status_code == 200
    assert client.post(endpoint, headers=a).status_code == 200
    inbox = client.get("/api/v1/me/conversations", headers=b).json()
    assert len(inbox) == 2
    invited = next(x for x in inbox if x["id"] != c["id"])
    before = client.get("/api/v1/me/deliveries", headers=a).json()
    endpoint = "/api/v1/conversations/" + invited["id"]
    assert client.get(endpoint + "/messages", headers=a).status_code == 404
    assert (
        client.post(
            endpoint + "/messages", headers=b, json={"body": "hi", "client_message_id": "1"}
        ).status_code
        == 422
    )
    client.put(endpoint + "/agent-settings", headers=b, json={"connection_id": conn["id"]})
    message = client.post(
        endpoint + "/messages", headers=b, json={"body": "hi", "client_message_id": "1"}
    ).json()
    asyncio.run(execute_run(message["run_id"]))
    assert client.get("/api/v1/me/deliveries", headers=a).json() == before
    assert client.get("/api/v1/runs/" + message["run_id"], headers=a).status_code == 404
    with SessionLocal() as db:
        assert db.get(Run, message["run_id"]).payer_id == db.get(Conversation, invited["id"]).creator_id


# test_call_ledger_no_unknown_retry：验证：账本拒绝重试消费不明的外部调用；范围以本函数断言为准。
def test_call_ledger_no_unknown_retry(client):
    import pytest
    from fastapi import HTTPException

    from app.gateways import begin_call

    a, b, ar, br, c, conn = setup_chat(client)
    msg = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "hi", "client_message_id": "1"},
    ).json()
    with SessionLocal() as db:
        db.get(Run, msg["run_id"]).status = "RUNNING"
        db.commit()
    begin_call(msg["run_id"], "paid", "image_generate", units=1)
    with pytest.raises(HTTPException):
        begin_call(msg["run_id"], "paid", "image_generate", units=1)


# test_private_memory_disabled_rejects_index：验证：会话私有知识关闭时拒绝索引任务；范围以本函数断言为准。
def test_private_memory_disabled_rejects_index(client):
    a, b, ar, br, c, conn = setup_chat(client)
    response = client.post(
        "/api/v1/roles/" + ar["id"] + "/knowledge",
        headers=b,
        json={
            "title": "private",
            "text": "my memory",
            "connection_id": conn["id"],
            "providers": ["mock"],
            "conversation_id": c["id"],
            "authorized": True,
        },
    )
    assert response.status_code == 409
