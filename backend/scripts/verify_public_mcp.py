# 文件职责：在临时合成数据库中通过真实 MCP 协议查询公共天气和地图；需要网络，不使用个人 Key。
"""Live public-provider smoke. Uses a fresh temporary synthetic database, no personal keys."""

import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="human-ai-live-mcp-"))
os.environ.update(
    HAI_DATABASE_URL="sqlite:///" + (root / "smoke.db").as_posix(),
    HAI_SECRET_KEY=secrets.token_hex(32),
    HAI_SECRET_FILE="",
    HAI_DEMO_MODE="true",
    HAI_AI_STOPPED="false",
)

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Account, AgentConfig, Connection, Conversation, Role, Run  # noqa: E402
from app.r2_policy import AgentSettings  # noqa: E402
from app.security import encrypt  # noqa: E402
from app.tool_gateway import invoke, rpc  # noqa: E402

Base.metadata.create_all(engine)
with SessionLocal() as db:
    a = Account(username="synthetic", nickname="Synthetic", password_hash="not-a-login")
    db.add(a)
    db.flush()
    r = Role(owner_id=a.id, name="Synthetic", visibility="PUBLIC", ai_enabled=True, public_ai=True)
    db.add(r)
    db.flush()
    conn = Connection(owner_id=a.id, provider="mock", model="demo", encrypted_key=encrypt(""))
    db.add(conn)
    db.flush()
    names = ["maps_search", "weather_query", "maps_route"]
    db.add(AgentConfig(role_id=r.id, config={**AgentSettings().model_dump(), "enabled_tools": names}))
    c = Conversation(
        kind="AI_DIRECT",
        creator_id=a.id,
        peer_account_id="",
        source_role_id=r.id,
        target_role_id=r.id,
        connection_id=conn.id,
        grants={"enabled_tools": names, "max_tool_calls": 4},
    )
    db.add(c)
    db.flush()
    run = Run(
        conversation_id=c.id,
        payer_id=a.id,
        connection_id=conn.id,
        role_id=r.id,
        policy_version=1,
        reserved=16000,
        deadline=time.time() + 300,
        status="RUNNING",
        context={"session_version": 1},
    )
    db.add(run)
    db.commit()
    rid = run.id


# main：发现 MCP 工具后查询柏林地点、天气和驾车路线；最后断言天气与路线确实返回数据。
async def main():
    print("discovered:", await rpc())
    found = await invoke(rid, "map", "maps_search", {"place": "Berlin"})
    print("maps:", json.dumps(found, ensure_ascii=True))
    place = found["candidates"][0]
    weather = await invoke(rid, "weather", "weather_query", {"place": "Berlin", "location_id": place["id"]})
    print("weather:", json.dumps(weather, ensure_ascii=True))
    route = await invoke(
        rid,
        "route",
        "maps_route",
        {
            "origin_lon": 13.38886,
            "origin_lat": 52.51703,
            "destination_lon": 13.39763,
            "destination_lat": 52.5294,
        },
    )
    print("route:", json.dumps(route, ensure_ascii=True))
    assert weather.get("weather") and route.get("routes")


if __name__ == "__main__":
    asyncio.run(main())
