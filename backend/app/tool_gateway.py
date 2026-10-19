# 文件职责：模型工具调用的受控入口：校验参数与权限、登记额度、签发临时 capability 并调用内部 MCP。
import json
import os
import sys
import time
from datetime import timedelta

import jsonschema
import jwt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .gateways import begin_call, complete_call
from .models import CallLedger
from .policy import fail
from .r2_policy import TOOLS, tool_context

# 模型只能看到业务参数；临时凭证由网关注入，连接和付款人不能作为工具参数传入。
SCHEMAS = {
    "weather_query": (
        "联网查询城市当前天气和今日最高/最低温、降水概率。place 使用用户提供的中文城市名，如上海。重名时请用户选择候选 id 后传 location_id，不得编造 id，不读取 GPS。回答注明地点、数据时间和 Open-Meteo 来源。",
        {"place": {"type": "string"}, "location_id": {"type": "integer"}},
    ),
    "maps_search": ("查询城市候选及 WGS84 地图链接，多个候选时请用户消歧", {"place": {"type": "string"}}),
    "maps_route": (
        "按用户明确提供的 WGS84 坐标查询驾车路线，不读取 GPS",
        {
            "origin_lon": {"type": "number"},
            "origin_lat": {"type": "number"},
            "destination_lon": {"type": "number"},
            "destination_lat": {"type": "number"},
        },
    ),
    "vision_describe": (
        "理解当前会话已上传的图片，不能访问其他会话",
        {"asset_id": {"type": "string"}, "question": {"type": "string"}},
    ),
    "image_generate": ("生成并发送一张图片，需要本会话生图额度", {"prompt": {"type": "string"}}),
}


# tool_schemas：生成模型可见的工具定义，禁止未声明参数；付款账号和 Key 不由模型传入。
def tool_schemas(names):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": SCHEMAS[name][0],
                "parameters": {
                    "type": "object",
                    "properties": SCHEMAS[name][1],
                    "required": [k for k in SCHEMAS[name][1] if k != "location_id"],
                    "additionalProperties": False,
                },
            },
        }
        for name in names
        if name in SCHEMAS
    ]


# rpc：以固定解释器和模块启动内部 MCP 子进程，并传入必要配置。
async def rpc(name=None, args=None):
    # The process executable and module are fixed. No arbitrary user commands/endpoints.
    env = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("HAI_", "LANGSMITH_", "LANGCHAIN_"))
        or k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONPATH")
    }
    for key in ("database_url", "media_dir", "secret_key", "secret_file", "demo_mode", "ai_stopped"):
        value = getattr(settings(), key)
        env["HAI_" + key.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env=env)
    # Celery replaces stderr with a LoggingProxy without fileno(). A child
    # process needs an actual OS file descriptor; do not forward private tool
    # diagnostics to shared logs. MCP protocol errors still return via stdout.
    with open(os.devnull, "w") as error_log:
        return await _rpc_session(params, error_log, name, args)


# _rpc_session：初始化 stdio 会话并发现工具，调用后解析结构化结果或 JSON 文本。
async def _rpc_session(params, error_log, name, args):
    async with stdio_client(params, errlog=error_log) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=75)) as session:
            await session.initialize()
            discovered = await session.list_tools()
            available = {t.name for t in discovered.tools}
            if name is None:
                return sorted(available)
            if name not in available:
                fail("MCP_TOOL_UNAVAILABLE", 502)
            result = await session.call_tool(name, args)
            if result.isError:
                fail("MCP_TOOL_FAILED", 502)
            if result.structuredContent:
                return result.structuredContent
            return json.loads(next(c.text for c in result.content if c.type == "text"))


# invoke：执行权限、次数与账本检查，再签发短期凭证；key 是步骤标识，不是 API Key。
async def invoke(rid, key, name, arguments):
    if name not in TOOLS:
        fail("TOOL_NOT_ALLOWED", 403)
    # 拒绝未知字段和类型错误，防止模型用额外 payer_id/api_key 绕过业务规则。
    jsonschema.validate(arguments, tool_schemas([name])[0]["function"]["parameters"])
    with SessionLocal() as db:
        run, role, c = tool_context(db, rid, name)
        old = db.scalar(select(CallLedger).where(CallLedger.run_id == rid, CallLedger.step_key == key))
        if old and old.state == "DONE":
            return old.result
        count = db.scalar(
            select(func.count())
            .select_from(CallLedger)
            .where(CallLedger.run_id == rid, CallLedger.kind.in_(TOOLS))
        )
        if count >= c.grants.get("max_tool_calls", 0):
            fail("TOOL_BUDGET_EXCEEDED", 409)
        images = db.scalar(
            select(func.count())
            .select_from(CallLedger)
            .where(CallLedger.run_id == rid, CallLedger.kind == "image_generate")
        )
        if name == "image_generate" and images >= c.grants.get("max_images", 0):
            fail("IMAGE_BUDGET_EXCEEDED", 409)
    billing = "public-demo-quota" if name in ("weather_query", "maps_search", "maps_route") else "payer"
    ident, cached = begin_call(
        rid, key, name, amount=4096 if name == "vision_describe" else 0, units=1, billing=billing
    )
    if cached is not None:
        return cached
    token = jwt.encode(
        {"rid": rid, "call": ident, "tool": name, "exp": time.time() + 80, "aud": "hai-mcp"},
        settings().signing_key(),
        algorithm="HS256",
    )
    # 签名凭证绑定任务、调用记录、工具和期限；它不是提供商 Key，也不含参数全文哈希。
    result = await rpc(name, {"capability": token, **arguments})
    with SessionLocal() as db:
        tool_context(db, rid, name)
    complete_call(ident, result, result.get("tokens", 0))
    return result
