# 文件职责：自动化测试：天气地点消歧、今日数据及 Celery stderr 兼容；具体保证以各测试断言为准。
import asyncio
import io

from app import mcp_server


# test_mcp_discovery_with_worker_style_stderr：验证：Worker 日志代理环境下仍能发现 MCP 工具；范围以本函数断言为准。
def test_mcp_discovery_with_worker_style_stderr(monkeypatch):
    from app import tool_gateway

    # Like Celery's logging proxy, this stream cannot be a subprocess fd.
    proxy = io.StringIO()
    real_stdio = tool_gateway.stdio_client

    # worker_stdio：模拟 Celery 的 stderr 日志代理环境。
    def worker_stdio(params, errlog=proxy):
        # Reproduce the MCP default captured when imported inside a worker.
        return real_stdio(params, errlog=errlog)

    monkeypatch.setattr(tool_gateway, "stdio_client", worker_stdio)
    assert "weather_query" in asyncio.run(tool_gateway.rpc())


# test_weather_prefers_unique_named_capital_and_returns_today：验证：唯一同名行政首府优先且返回今日天气字段；范围以本函数断言为准。
def test_weather_prefers_unique_named_capital_and_returns_today(monkeypatch):
    permits = []
    monkeypatch.setattr(mcp_server, "authorize", lambda *a, **kw: permits.append(kw))

    # fake_json：按预设请求参数返回合成城市或天气数据。
    async def fake_json(url, params=None):
        if "geocoding" in url:
            return {
                "results": [
                    {
                        "id": 1,
                        "name": "上海",
                        "latitude": 31.22,
                        "longitude": 121.46,
                        "feature_code": "PPLA",
                        "admin1": "上海市",
                    },
                    {
                        "id": 2,
                        "name": "上海",
                        "latitude": 29.3,
                        "longitude": 121.0,
                        "feature_code": "PPL",
                        "admin1": "浙江",
                    },
                ]
            }
        assert params["latitude"] == 31.22 and params["forecast_days"] == 1
        assert "precipitation_probability_max" in params["daily"]
        return {
            "current": {"time": "2026-10-03T18:00", "temperature_2m": 20},
            "daily": {"time": ["2026-10-03"], "temperature_2m_max": [23]},
            "timezone": "Asia/Shanghai",
        }

    monkeypatch.setattr(mcp_server, "get_json", fake_json)
    result = asyncio.run(mcp_server.weather_query("test", "上海"))
    assert result["place"]["id"] == 1
    assert result["today"]["temperature_2m_max"] == [23]
    assert result["timezone"] == "Asia/Shanghai" and result["source"] == "Open-Meteo"
    assert permits == [{"consume": True}, {}]


# test_weather_retains_ambiguity_and_explicit_selection：验证：真正重名时保留候选并尊重明确选择；范围以本函数断言为准。
def test_weather_retains_ambiguity_and_explicit_selection(monkeypatch):
    monkeypatch.setattr(mcp_server, "authorize", lambda *a, **kw: None)
    places = [
        {"id": n, "name": "同名", "feature_code": "PPLA", "latitude": n, "longitude": n} for n in (1, 2)
    ]

    # fake_cities：返回重名地点候选，覆盖天气工具的消歧分支。
    async def fake_cities(place):
        return places

    # fake_json：按预设请求参数返回合成城市或天气数据。
    async def fake_json(url, params=None):
        assert params["latitude"] == 2
        return {"current": {"temperature_2m": 20}}

    monkeypatch.setattr(mcp_server, "cities", fake_cities)
    monkeypatch.setattr(mcp_server, "get_json", fake_json)
    result = asyncio.run(mcp_server.weather_query("test", "同名"))
    assert result["needs_clarification"] and len(result["candidates"]) == 2
    assert asyncio.run(mcp_server.weather_query("test", "同名", location_id=2))["place"]["id"] == 2
    assert asyncio.run(mcp_server.weather_query("test", "同名", location_id=99))["candidates"] == []
