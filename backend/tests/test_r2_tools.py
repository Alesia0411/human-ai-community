# 文件职责：自动化测试：MCP 协议、工具循环与流式适配；外部提供商通过测试替身隔离；具体保证以各测试断言为准。
import asyncio
import base64
import json
import time
from io import BytesIO

import httpx
import jwt
from conftest import setup_chat
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image
from test_r2 import config

from app.config import settings
from app.db import SessionLocal
from app.gateways import begin_call
from app.models import Run
from app.r2_policy import TOOLS


# test_all_mcp_tools_via_protocol_with_mocked_external_providers：验证：真实 MCP 协议配合模拟外部响应完成工具输入输出检查；范围以本函数断言为准。
def test_all_mcp_tools_via_protocol_with_mocked_external_providers(client, monkeypatch):
    """Real tools/list + tools/call; external HTTP fixtures are explicitly not live evidence."""
    from app import mcp_server

    a, b, ar, br, c, conn = setup_chat(client)
    config(client, a, ar, enabled_tools=list(TOOLS))
    vision = client.post(
        "/api/v1/me/connections",
        headers=b,
        json={"provider": "dashscope", "model": "qwen-vl-plus", "api_key": "fixture-key"},
    ).json()
    image = client.post(
        "/api/v1/me/connections",
        headers=b,
        json={"provider": "dashscope", "model": "qwen-image-2.0", "api_key": "fixture-key"},
    ).json()
    client.put(
        "/api/v1/conversations/" + c["id"] + "/agent-settings",
        headers=b,
        json={
            "connection_id": conn["id"],
            "enabled_tools": list(TOOLS),
            "max_tool_calls": 4,
            "max_images": 1,
            "tool_connections": {"vision_describe": vision["id"], "image_generate": image["id"]},
        },
    )
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, "PNG")
    raw = buffer.getvalue()
    asset = client.post(
        "/api/v1/assets",
        headers=b,
        data={"conversation_id": c["id"]},
        files={"file": ("a.png", raw, "image/png")},
    ).json()["id"]
    message = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "test", "asset_id": asset, "client_message_id": "test"},
    ).json()
    rid = message["run_id"]
    with SessionLocal() as db:
        db.get(Run, rid).status = "RUNNING"
        db.commit()

    # handler：根据请求地址返回合成 HTTP 响应，不访问真实提供商。
    def handler(request):
        host = request.url.host
        if "geocoding" in host:
            return httpx.Response(
                200, json={"results": [{"name": "Test City", "latitude": 31, "longitude": 121}]}
            )
        if host == "api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "current": {"time": "2026-09-29T12:00", "temperature_2m": 22},
                    "current_units": {"temperature_2m": "°C"},
                },
            )
        if host == "router.project-osrm.org":
            return httpx.Response(200, json={"routes": [{"distance": 1000, "duration": 120}]})
        if request.method == "GET":
            return httpx.Response(200, content=raw)
        payload = json.loads(request.content)
        if "/chat/completions" in str(request.url):
            assert (
                base64.b64encode(raw).decode() not in request.content.decode()
            )  # uploaded image was reencoded
            assert payload["model"] == "qwen-vl-plus"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "绿色的测试图片"}}], "usage": {"total_tokens": 50}},
            )
        return httpx.Response(
            200,
            json={
                "output": {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "image": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.png"
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_server.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    # exercise：在测试连接中执行 MCP 工具发现及调用并检查结果。
    async def exercise():
        async with create_connected_server_and_client_session(mcp_server.mcp) as session:
            assert len((await session.list_tools()).tools) == 5
            results = {}
            for name, args in [
                ("weather_query", {"place": "Test City"}),
                ("maps_search", {"place": "Test City"}),
                (
                    "maps_route",
                    {"origin_lon": 121, "origin_lat": 31, "destination_lon": 121.1, "destination_lat": 31.1},
                ),
                ("vision_describe", {"asset_id": asset, "question": "描述"}),
                ("image_generate", {"prompt": "绿色测试图"}),
            ]:
                ident, _ = begin_call(rid, name, name)
                token = jwt.encode(
                    {"rid": rid, "call": ident, "tool": name, "exp": time.time() + 60, "aud": "hai-mcp"},
                    settings().signing_key(),
                    algorithm="HS256",
                )
                response = await session.call_tool(name, {"capability": token, **args})
                assert not response.isError, response
                results[name] = response.structuredContent or json.loads(
                    next(x.text for x in response.content if x.type == "text")
                )
            invalid = await session.call_tool("weather_query", {"capability": "bad", "place": "Test City"})
            assert invalid.isError
            assert results["weather_query"]["weather"]["temperature_2m"] == 22
            assert results["image_generate"]["asset_id"] != asset
            assert results["maps_route"]["coordinate_system"] == "WGS84"
            return results

    asyncio.run(exercise())


# test_react_loop_uses_ledger_observation_and_publishes_once：验证：ReAct 循环从账本取工具观察且只发布一次；范围以本函数断言为准。
def test_react_loop_uses_ledger_observation_and_publishes_once(client, monkeypatch):
    from app.agents import execute_run
    from app.gateways import complete_call

    a, b, ar, br, c, conn = setup_chat(client)
    config(client, a, ar, enabled_tools=["maps_search"])
    client.put(
        "/api/v1/conversations/" + c["id"] + "/agent-settings",
        headers=b,
        json={"connection_id": conn["id"], "enabled_tools": ["maps_search"], "max_tool_calls": 1},
    )

    # model：构造受控模型决策和账本结果，推动测试中的 ReAct 循环。
    async def model(rid, key, messages, tools=None, structured=False):
        ident, cached = begin_call(rid, key, "model", billing="mock")
        if cached:
            return cached
        result = (
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tool1",
                        "type": "function",
                        "function": {"name": "maps_search", "arguments": '{"place":"北京"}'},
                    }
                ],
            }
            if key == "model:0"
            else {"content": "找到了城市候选，请选择。", "tool_calls": []}
        )
        if key != "model:0":
            observation = next(m for m in reversed(messages) if m["role"] != "system")
            assert observation["role"] == "tool" and "candidates" in observation["content"]
        complete_call(ident, result)
        return result

    # rpc：模拟工具返回并记录调用，避免测试产生真实外部副作用。
    async def rpc(name, args):
        return {"candidates": [{"name": "北京"}], "source": "test fixture"}

    monkeypatch.setattr("app.agents.model_call", model)
    monkeypatch.setattr("app.tool_gateway.rpc", rpc)
    rid = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "查地图", "client_message_id": "loop"},
    ).json()["run_id"]
    asyncio.run(execute_run(rid))
    asyncio.run(execute_run(rid))
    assert client.get("/api/v1/runs/" + rid, headers=b).json()["status"] == "SUCCEEDED"
    assert len(client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()) == 2
    assert len(client.get("/api/v1/runs/" + rid + "/calls", headers=b).json()) == 3


# test_streaming_provider_adapter_assembles_tool_arguments_and_usage：验证：流式模型片段正确拼装工具参数与用量；范围以本函数断言为准。
def test_streaming_provider_adapter_assembles_tool_arguments_and_usage(client, monkeypatch):
    from sqlalchemy import select

    from app.gateways import model_call
    from app.models import CallLedger

    a, b, ar, br, c, conn = setup_chat(client)
    live = client.post(
        "/api/v1/me/connections",
        headers=b,
        json={"provider": "deepseek", "model": "fixture-model", "api_key": "fixture-key"},
    ).json()
    client.put(
        "/api/v1/conversations/" + c["id"] + "/agent-settings", headers=b, json={"connection_id": live["id"]}
    )
    rid = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "test", "client_message_id": "stream"},
    ).json()["run_id"]
    with SessionLocal() as db:
        db.get(Run, rid).status = "RUNNING"
        db.commit()
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "t1",
                                "function": {"name": "maps_search", "arguments": '{"place":'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"北京"}'}}]}}]},
        {"choices": [], "usage": {"total_tokens": 123}},
    ]
    payload = "".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n"
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.gateways.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=payload, headers={"Content-Type": "text/event-stream"}
                )
            ),
            **kwargs,
        ),
    )
    response = asyncio.run(model_call(rid, "fixture", [{"role": "user", "content": "test"}]))
    assert json.loads(response["tool_calls"][0]["function"]["arguments"]) == {"place": "北京"}
    with SessionLocal() as db:
        call = db.scalar(select(CallLedger).where(CallLedger.run_id == rid))
        assert call.used == 123 and call.state == "DONE"
        assert call.usage_known and call.finished_at >= call.created_at
        assert db.get(Run, rid).first_token_at is None  # Tool deltas are not user-visible text.
