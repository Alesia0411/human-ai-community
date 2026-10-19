# 文件职责：自动化测试：Ollama 连接权限与流式响应使用量解析；具体保证以各测试断言为准。
import asyncio
import json

import httpx
from conftest import register, role

from app.agents import execute_run


# test_ollama_connection_scope_and_streaming_usage：验证：Ollama 连接提供商范围及流式用量解析；范围以本函数断言为准。
def test_ollama_connection_scope_and_streaming_usage(client, monkeypatch):
    owner, _ = register(client, "local_owner")
    visitor, _ = register(client, "local_visitor")
    target = role(client, owner, "local_target")
    source = role(client, visitor, "local_source")
    result = client.post(
        "/api/v1/me/connections", headers=visitor, json={"provider": "ollama", "model": "qwen3:8b"}
    )
    assert result.status_code == 201
    connection = result.json()
    # No user-controlled URL, and cloud providers still require their own key.
    assert (
        client.post(
            "/api/v1/me/connections",
            headers=visitor,
            json={"provider": "ollama", "model": "x", "base_url": "http://evil"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/me/connections", headers=visitor, json={"provider": "deepseek", "model": "x"}
        ).status_code
        == 422
    )
    endpoint = "/api/v1/me/connections/" + connection["id"] + "/test"
    assert client.post(endpoint, headers=owner).status_code == 404

    # respond：返回预设提供商响应，隔离真实网络并检验流式适配器行为。
    def respond(request):
        assert request.url.host == "ollama"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "qwen3:8b"}]})
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "none"
        assert payload["stream"] is True and payload["max_tokens"] == 512
        chunks = [
            {"choices": [{"delta": {"content": "你好，本地模型回复。"}}]},
            {"choices": [], "usage": {"total_tokens": 31}},
        ]
        body = "".join("data: " + json.dumps(v) + "\n\n" for v in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    assert client.post(endpoint, headers=visitor).json()["ok"]
    conv = client.post(
        "/api/v1/conversations",
        headers=visitor,
        json={
            "kind": "AI_DIRECT",
            "source_role_id": source["id"],
            "target_role_id": target["id"],
            "connection_id": connection["id"],
        },
    ).json()
    sent = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=visitor,
        json={"body": "你好", "client_message_id": "local-test"},
    )
    assert sent.status_code == 201, sent.text
    rid = sent.json()["run_id"]
    asyncio.run(execute_run(rid))
    run = client.get("/api/v1/runs/" + rid, headers=visitor).json()
    assert run["status"] == "SUCCEEDED" and run["used"] == 31
    url = "/api/v1/conversations/" + conv["id"] + "/messages"
    assert client.get(url, headers=visitor).json()[-1]["body"] == "你好，本地模型回复。"
    assert client.get(url, headers=owner).status_code == 404
