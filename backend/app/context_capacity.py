# 文件职责：离线估算完整请求的上下文容量；Qwen 使用本地模板和分词器，其他模型明确采用保守字节估算。
"""Offline Qwen3 token accounting; no remote tokenizer code or prompt uploads."""
import json
from functools import lru_cache
from pathlib import Path

from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

from .config import settings


# qwen_resources：加载并缓存本地 Qwen 分词器和沙箱模板，不下载远程代码或上传提示词。
@lru_cache
def qwen_resources():
    root = Path(__file__).parent / "tokenizer_assets" / "qwen3"
    tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
    config = json.loads((root / "tokenizer_config.json").read_text(encoding="utf-8"))
    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    env.filters["tojson"] = lambda value, **kwargs: json.dumps(value, ensure_ascii=False, **kwargs)
    return tokenizer, env.from_string(config["chat_template"])


# capacity：计算输入、回复预留、安全余量和各部分占用，返回可用于超限提示的诊断对象。
def capacity(model, messages, tools=None):
    cfg = settings()
    qwen = model in cfg.ollama_tokenizer_models
    if qwen:
        tokenizer, template = qwen_resources()

        # count：计算当前分支的文本长度：Qwen 分支计 token，回退分支计 UTF-8 字节。
        def count(value):
            return len(tokenizer.encode(value, add_special_tokens=False).ids)

        rendered = template.render(messages=messages, tools=tools or [],
                                   add_generation_prompt=True, enable_thinking=False)
        total = count(rendered)
    else:
        # Explicitly labelled fallback: never assume an arbitrary model uses Qwen tokens.
        # count：计算当前分支的文本长度：Qwen 分支计 token，回退分支计 UTF-8 字节。
        def count(value):
            return len(value.encode("utf-8"))

        total = count(json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False))
    parts = {"persona_rules": 0, "memory": 0, "dialogue": 0, "tool_results": 0,
             "tool_definitions": count(json.dumps(tools, ensure_ascii=False)) if tools else 0}
    for message in messages:
        value = message.get("content") or ""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if message["role"] == "system":
            head, marker, tail = value.partition("\n长期聊天记忆")
            parts["persona_rules"] += count(head)
            if marker:
                parts["memory"] += count(marker + tail)
        else:
            parts["tool_results" if message["role"] == "tool" else "dialogue"] += count(value)
    return {"input_tokens": total, "output_tokens": cfg.model_output_tokens,
            "margin_tokens": cfg.ollama_context_margin, "limit_tokens": cfg.ollama_context_length,
            "required_tokens": total + cfg.model_output_tokens + cfg.ollama_context_margin,
            "method": "qwen3_template" if qwen else "byte_upper_bound",
            "parts": parts, "dominant": max(parts, key=parts.get)}
