# 文件职责：集中管理模型调用、容量和预算、流式结果及调用账本；未知消费不会自动重放。
"""Idempotent external-call ledger; an uncertain request is never blindly replayed."""

import json
import time

import httpx
from langchain_core.messages import convert_to_messages, convert_to_openai_messages
from sqlalchemy import case, func, select

from .config import settings
from .db import SessionLocal
from .models import CallLedger, Run
from .policy import fail
from .r2_policy import check_run
from .security import decrypt


# committed_budget：汇总当前任务已完成调用的实际用量与未完成调用的预留量。
def committed_budget(db, rid):
    # Finished calls release their overestimate; unknown/inflight calls retain
    # their full reservation. Account-level reservations remain held until finish.
    return db.scalar(
        select(
            func.coalesce(
                func.sum(case((CallLedger.state == "DONE", CallLedger.used), else_=CallLedger.reserved)), 0
            )
        ).where(CallLedger.run_id == rid)
    )


# fit_messages：在预算不足时去掉最旧对话轮，保留系统规则、最新问题及当前工具交换。
def fit_messages(messages, tools, remaining):
    messages = list(messages)
    while True:
        reserve = (
            len(json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False).encode()) + 2048
        )
        if reserve <= remaining:
            return messages, reserve
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        if len(users) < 2:
            fail("RUN_BUDGET_LIMIT", 409)
        # Remove one oldest dialogue turn, keeping all system rules, the latest
        # user question, and the current assistant/tool exchange intact.
        boundary = users[1]
        messages = [m for i, m in enumerate(messages) if i >= boundary or m["role"] == "system"]


# begin_call：锁定任务并登记唯一调用步骤；完成记录可复用，未决记录拒绝盲目重试。
def begin_call(rid, key, kind, amount=0, units=0, billing="payer"):
    with SessionLocal() as db:
        db.scalar(select(Run).where(Run.id == rid).with_for_update())
        run, _, _ = check_run(db, rid)
        old = db.scalar(select(CallLedger).where(CallLedger.run_id == rid, CallLedger.step_key == key))
        if old:
            if old.state == "DONE":
                return old.id, old.result
            fail("UNKNOWN_USAGE_NO_REPLAY", 409)
        committed = committed_budget(db, rid)
        if committed + amount > run.reserved:
            fail("RUN_BUDGET_LIMIT", 409)
        call = CallLedger(run_id=rid, step_key=key, kind=kind, reserved=amount, units=units, billing=billing)
        db.add(call)
        run.heartbeat = time.time()
        if billing == "payer":
            run.provider_called = True
        db.commit()
        return call.id, None


# complete_call：复查任务后保存调用结果、使用量与完成时间；无可靠用量时保留预留值。
def complete_call(ident, result, used=0):
    with SessionLocal() as db:
        call = db.get(CallLedger, ident)
        if call.state not in ("INFLIGHT", "EXECUTING"):
            fail("CALL_ALREADY_COMPLETED", 409)
        check_run(db, call.run_id)
        call.state, call.result = "DONE", result
        call.finished_at = time.time()
        call.usage_known = type(used) is int and used >= 0
        call.used = used if type(used) is int and used >= 0 else call.reserved
        db.get(Run, call.run_id).heartbeat = time.time()
        db.commit()


# model_call：校验上下文和预算后调用固定提供商，组装流式文本/工具参数并登记结果。
async def model_call(rid, key, messages, tools=None, structured=False):
    # Normalize human/AI/tool messages through LangChain before provider serialization.
    messages = convert_to_openai_messages(convert_to_messages(messages))
    with SessionLocal() as db:
        run, _, conn = check_run(db, rid)
        provider, model, credential = conn.provider, conn.model, decrypt(conn.encrypted_key)
        old = db.scalar(select(CallLedger).where(CallLedger.run_id == rid, CallLedger.step_key == key))
        if old:
            if old.state == "DONE":
                return old.result
            fail("UNKNOWN_USAGE_NO_REPLAY", 409)
        # 剩余额度扣除已完成实际消费和未决预留，避免按历史最大估计重复占额。
        remaining = run.reserved - committed_budget(db, rid)
        # 聊天任务必须保留约定的近期消息和全部摘要；不能用旧对话裁剪掩盖容量不足。
        preserve = run.purpose == "CHAT"
    if preserve:
        # Never silently drop the requested recent four messages or long-term summaries.
        size = len(json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False).encode())
        reserve = size + 2048
        if provider == "ollama":
            from .context_capacity import capacity

            # 本地模型按完整模板计量，包括工具描述、回复预留和安全余量，而不只算用户正文。
            usage = capacity(model, messages, tools)
            with SessionLocal() as db:
                run, _, _ = check_run(db, rid)
                run.context = {**run.context, "context_usage": usage}
                db.commit()
            if usage["required_tokens"] > usage["limit_tokens"]:
                fail("MEMORY_CONTEXT_LIMIT", 409)
            reserve = usage["required_tokens"]
        if reserve > remaining:
            fail("RUN_BUDGET_LIMIT", 409)
    else:
        messages, reserve = fit_messages(messages, tools, remaining)
    ident, cached = begin_call(rid, key, "model", reserve, billing="mock" if provider == "mock" else "payer")
    if cached is not None:
        return cached
    if provider == "mock":
        if not settings().demo_mode:
            fail("MOCK_DISABLED", 409)
        # Explicitly deterministic fixture, never described as model inference.
        content = (
            json.dumps(
                {"personality": "未知", "speaking_style": "未知", "habits": "未知"}, ensure_ascii=False
            )
            if structured
            else (
                "【本地演示，非真实模型】"
                + str(next((m.get("content", "") for m in reversed(messages) if m["role"] == "user"), ""))[
                    :180
                ]
            )
        )
        result = {"content": content, "tool_calls": []}
        complete_call(ident, result, 0)
        return result
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": settings().model_output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if provider == "ollama":
        # Preserve the output budget for the reply rather than Qwen3 thinking.
        payload["reasoning_effort"] = "none"
    if tools:
        payload["tools"] = tools
    if structured:
        payload["response_format"] = {"type": "json_object"}
    # 分别累计普通文本、按 index 合并的工具调用，以及提供商报告的实际 token 用量。
    content, calls, usage = "", {}, None
    last_save = 0.0
    first_content_at = None
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
        async with client.stream(
            "POST",
            settings().providers[provider] + "/chat/completions",
            headers={"Authorization": "Bearer " + (credential or "ollama")},
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                value = json.loads(line[5:])
                if value.get("usage"):
                    usage = value["usage"].get("total_tokens")
                for choice in value.get("choices", []):
                    delta = choice.get("delta", {})
                    # 流式 delta 只包含新增片段；工具名称和 JSON 参数也可能拆成多段。
                    content += delta.get("content") or ""
                    if delta.get("content") and first_content_at is None:
                        first_content_at = time.time()
                    for t in delta.get("tool_calls", []):
                        dest = calls.setdefault(
                            t["index"],
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        dest["id"] = t.get("id") or dest["id"]
                        dest["function"]["name"] += t.get("function", {}).get("name", "")
                        dest["function"]["arguments"] += t.get("function", {}).get("arguments", "")
                if len(content) + len(str(calls)) > 16000:
                    fail("OUTPUT_LIMIT", 409)
                if time.monotonic() - last_save > 0.3:
                    with SessionLocal() as db:
                        run, _, _ = check_run(db, rid)
                        if not key.startswith("memory:"):
                            run.partial = content
                        run.heartbeat = time.time()
                        if not key.startswith("memory:") and first_content_at is not None and run.first_token_at is None:
                            run.first_token_at = first_content_at
                        db.commit()
                    last_save = time.monotonic()
    result = {"content": content, "tool_calls": list(calls.values())}
    if not content and not calls:
        fail("EMPTY_RESPONSE", 502)
    if first_content_at is not None and not key.startswith("memory:"):
        with SessionLocal() as db:
            run, _, _ = check_run(db, rid)
            if run.first_token_at is None:
                run.first_token_at = first_content_at
            db.commit()
    complete_call(ident, result, usage)
    return result
