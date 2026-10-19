# 文件职责：内部 MCP stdio 工具服务：凭证授权后执行天气、地图、识图和生图；不是公网 HTTP 路由。
"""Real MCP stdio server. All tools require an internal, run-bound capability."""

import base64
import logging
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from PIL import Image
from sqlalchemy import select, update

from .config import settings
from .db import SessionLocal
from .models import Asset, CallLedger, Message, uid
from .policy import fail
from .r2_policy import connection_for, tool_context
from .security import decrypt

mcp = FastMCP(
    "human-ai-tools", instructions="Scoped tools; no arbitrary URLs, file paths, or payer arguments."
)


# authorize：验证短期签名凭证和当前 SQL 权限；首次执行以条件更新消费调用记录。
def authorize(token, name, consume=False):
    claims = jwt.decode(token, settings().signing_key(), algorithms=["HS256"], audience="hai-mcp")
    if claims["tool"] != name:
        fail("INVALID_CAPABILITY", 403)
    with SessionLocal() as db:
        run, role, c = tool_context(db, claims["rid"], name)
        call = db.get(CallLedger, claims["call"])
        if (
            not call
            or call.run_id != run.id
            or call.kind != name
            or call.state not in ("INFLIGHT", "EXECUTING")
        ):
            fail("INVALID_CAPABILITY", 403)
        if consume:
            changed = db.execute(
                update(CallLedger)
                .where(CallLedger.id == call.id, CallLedger.state == "INFLIGHT")
                .values(state="EXECUTING")
            )
            db.commit()
            # 只有一次调用能消费 INFLIGHT 状态；重复开始执行同一凭证会被拒绝。
            if changed.rowcount != 1:
                fail("CAPABILITY_ALREADY_USED", 403)
        return run, role, c


# tool_connection：按 Run 付费人和会话授权查找工具连接，验证提供商后解密 Key。
def tool_connection(run, role, conv, name):
    with SessionLocal() as db:
        conn = connection_for(
            db, run.payer_id, conv.grants.get("tool_connections", {}).get(name), role.provider_scope
        )
        if conn.provider != "dashscope":
            fail("DASHSCOPE_TOOL_CONNECTION_REQUIRED", 422)
        # Key 只进入服务端请求头，不作为工具结果交给 LLM。
        return conn.model, decrypt(conn.encrypted_key)


# get_json：请求工具代码指定的网址并解析 JSON；禁止自动重定向和环境代理。
async def get_json(url, params=None):
    async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
        r = await client.get(url, params=params, headers={"User-Agent": "HumanAI-Personal-Demo/0.2"})
        r.raise_for_status()
        return r.json()


# cities：用城市名查询最多五个地点候选，返回 WGS84 坐标与地图链接，不读取 GPS。
async def cities(place):
    if not place.strip() or len(place) > 120:
        fail("PLACE_REQUIRED", 422)
    data = await get_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": place, "count": 5, "language": "zh", "format": "json"},
    )
    return [
        {
            "id": x.get("id"),
            "name": x["name"],
            "country": x.get("country"),
            "admin": x.get("admin1"),
            "feature_code": x.get("feature_code"),
            "latitude": x["latitude"],
            "longitude": x["longitude"],
            "coordinate_system": "WGS84",
            "map_url": f"https://www.openstreetmap.org/?mlat={x['latitude']}&mlon={x['longitude']}#map=12/{x['latitude']}/{x['longitude']}",
        }
        for x in data.get("results", [])
    ]


# weather_query：消歧城市后获取当前天气和今日预报；结果注明来源、时区与获取时间。
@mcp.tool()
async def weather_query(capability: str, place: str, location_id: int | None = None) -> dict:
    """Weather for an explicitly supplied city; public demonstration quota."""
    authorize(capability, "weather_query", consume=True)
    found = await cities(place)
    if location_id is not None:
        found = [p for p in found if p["id"] == location_id]
    elif len(found) > 1:
        # City queries may also match villages. Prefer only a uniquely named
        # administrative capital; genuinely ambiguous cities still need a choice.
        capitals = [
            p
            for p in found
            if p.get("feature_code") in ("PPLC", "PPLA")
            and p["name"].removesuffix("市") == place.strip().removesuffix("市")
        ]
        if len(capitals) == 1:
            found = capitals
    # 多个或零个候选均需澄清，不能编造地点 ID 或把任一同名地点当用户所在地。
    if len(found) != 1:
        return {"needs_clarification": True, "candidates": found, "source": "Open-Meteo geocoding"}
    p = found[0]
    data = await get_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 1,
            "timezone": "auto",
        },
    )
    authorize(capability, "weather_query")
    return {
        "place": p,
        "weather": data.get("current"),
        "units": data.get("current_units"),
        "today": data.get("daily"),
        "daily_units": data.get("daily_units"),
        "timezone": data.get("timezone"),
        "source": "Open-Meteo",
        "source_url": "https://open-meteo.com/",
        "data_note": "联网获取的天气模型数据，非现场气象站实时实测；按返回的本地时间说明时效。",
        "fetched_at": time.time(),
        "billing": "public-demo-quota",
    }


# maps_search：返回城市候选及地图链接，执行前后均验证任务授权。
@mcp.tool()
async def maps_search(capability: str, place: str) -> dict:
    """City search, ambiguity candidates, WGS84 coordinates and OpenStreetMap links."""
    authorize(capability, "maps_search", consume=True)
    result = await cities(place)
    authorize(capability, "maps_search")
    return {
        "candidates": result,
        "source": "Open-Meteo / OpenStreetMap",
        "fetched_at": time.time(),
        "billing": "public-demo-quota",
    }


# maps_route：验证明确提供的经纬度，再查询驾车路线距离和耗时；不是实时导航。
@mcp.tool()
async def maps_route(
    capability: str, origin_lon: float, origin_lat: float, destination_lon: float, destination_lat: float
) -> dict:
    """Driving route for explicitly supplied WGS84 coordinates. No device GPS access."""
    authorize(capability, "maps_route", consume=True)
    if not (
        -180 <= origin_lon <= 180
        and -180 <= destination_lon <= 180
        and -90 <= origin_lat <= 90
        and -90 <= destination_lat <= 90
    ):
        fail("INVALID_COORDINATES", 422)
    coords = f"{origin_lon},{origin_lat};{destination_lon},{destination_lat}"
    data = await get_json("https://router.project-osrm.org/route/v1/driving/" + coords, {"overview": "false"})
    authorize(capability, "maps_route")
    routes = [
        {"distance_m": r["distance"], "duration_seconds": r["duration"]} for r in data.get("routes", [])[:1]
    ]
    return {
        "routes": routes,
        "coordinate_system": "WGS84",
        "source": "OSRM public demo",
        "fetched_at": time.time(),
        "map_url": f"https://www.openstreetmap.org/directions?engine=fossgis_osrm_car&route={origin_lat}%2C{origin_lon}%3B{destination_lat}%2C{destination_lon}",
    }


# vision_describe：只读取当前会话已附加且未撤回的图片，用付费人的视觉模型连接生成描述。
@mcp.tool()
async def vision_describe(capability: str, asset_id: str, question: str) -> dict:
    """Understand an authorized current-conversation image using the payer's vision credential."""
    run, role, c = authorize(capability, "vision_describe", consume=True)
    if len(question) > 1000:
        fail("QUESTION_TOO_LONG", 422)
    with SessionLocal() as db:
        asset = db.get(Asset, asset_id)
        attached = db.scalar(
            select(Message.id).where(
                Message.conversation_id == c.id, Message.asset_id == asset_id, Message.retracted.is_(False)
            )
        )
        if not asset or asset.conversation_id != c.id or not attached:
            fail()
        raw = (Path(settings().media_dir) / asset.path).read_bytes()
    model, key = tool_connection(run, role, c, "vision_describe")
    async with httpx.AsyncClient(timeout=55, trust_env=False, follow_redirects=False) as client:
        r = await client.post(
            settings().providers["dashscope"] + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": model,
                "max_tokens": 512,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()
                                },
                            },
                        ],
                    }
                ],
            },
        )
        r.raise_for_status()
        data = r.json()
    authorize(capability, "vision_describe")
    return {
        "description": data["choices"][0]["message"]["content"][:5000],
        "tokens": data.get("usage", {}).get("total_tokens", 4096),
        "source": model,
    }


# image_generate：用获准连接生成一张图片，校验下载地址和体积，转存 JPEG 与 Asset 记录。
@mcp.tool()
async def image_generate(capability: str, prompt: str) -> dict:
    """Generate one real image using the payer's configured synchronous Qwen image model."""
    run, role, c = authorize(capability, "image_generate", consume=True)
    if not prompt or len(prompt) > 1500:
        fail("INVALID_PROMPT", 422)
    model, key = tool_connection(run, role, c, "image_generate")
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
        response = await client.post(
            settings().image_endpoint,
            headers={"Authorization": "Bearer " + key},
            json={
                "model": model,
                "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                "parameters": {"size": "1024*1024", "n": 1, "watermark": True},
            },
        )
        response.raise_for_status()
        url = response.json()["output"]["choices"][0]["message"]["content"][0]["image"]
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in settings().image_download_hosts
            or parsed.username
        ):
            fail("UNTRUSTED_IMAGE_HOST", 502)
        async with client.stream("GET", url) as download:
            download.raise_for_status()
            raw = bytearray()
            async for piece in download.aiter_bytes():
                raw.extend(piece)
                if len(raw) > 10 * 1024 * 1024:
                    fail("IMAGE_TOO_LARGE", 502)
    authorize(capability, "image_generate")
    # 设置 Pillow 解压保护阈值；这是库的告警/异常机制，不是独立硬裁剪像素断言。
    Image.MAX_IMAGE_PIXELS = 20_000_000
    img = Image.open(BytesIO(raw))
    img.load()
    img.thumbnail((2000, 2000))
    output = BytesIO()
    img.convert("RGB").save(output, "JPEG", quality=90)
    aid = uid()
    path = Path(settings().media_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / (aid + ".jpg")).write_bytes(output.getvalue())
    with SessionLocal() as db:
        tool_context(db, run.id, "image_generate")
        db.add(Asset(id=aid, owner_id=run.payer_id, conversation_id=c.id, path=aid + ".jpg"))
        db.commit()
    # 图片按张数控制；没有 token 字段不代表免费，金额以提供商账单为准。
    return {"asset_id": aid, "source": model, "images": 1, "fee": "unknown-provider-bill"}


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run(transport="stdio")
