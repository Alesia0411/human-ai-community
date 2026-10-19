# 文件职责：自动化测试：本地分词计量、长人设工具描述和回退容量估算；具体保证以各测试断言为准。
import json

from app.config import settings
from app.context_capacity import capacity
from app.persona_prompt import STYLE_REMINDER, persona_prompt
from app.tool_gateway import tool_schemas


# test_long_chinese_persona_and_weather_tools_fit_without_byte_false_positive：验证：长中文人设和天气工具使用正确分词计量，避免字节数误报；范围以本函数断言为准。
def test_long_chinese_persona_and_weather_tools_fit_without_byte_false_positive():
    persona = ("你是温柔细心的朋友，喜欢文学和音乐，回答简洁自然，尊重对方的意愿。" * 80)[:2095]
    messages = [{"role": "system", "content": persona_prompt("测试角色", persona)},
                {"role": "user", "content": "晚上好"},
                {"role": "system", "content": STYLE_REMINDER}]
    tools = tool_schemas(["weather_query", "maps_search", "maps_route"])
    original = json.dumps(messages, ensure_ascii=False)
    assert len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode()) + 512 > 8192
    usage = capacity("qwen3:8b", messages, tools)
    assert usage["method"] == "qwen3_template"
    assert usage["required_tokens"] < 8192
    assert usage["parts"]["tool_definitions"] > 0
    assert usage["input_tokens"] > capacity("qwen3:8b", messages)["input_tokens"]
    assert json.dumps(messages, ensure_ascii=False) == original


# test_all_memory_and_tool_results_count_with_configurable_limit：验证：长期记忆和工具结果都纳入可配置容量限制；范围以本函数断言为准。
def test_all_memory_and_tool_results_count_with_configurable_limit(monkeypatch):
    messages = [{"role": "system", "content": "人设\n长期聊天记忆：" + "记忆事实。" * 2500},
                {"role": "user", "content": "上海天气"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"type": "function", "id": "t1", "function": {"name": "weather_query", "arguments": '{"place":"上海"}'}}]},
                {"role": "tool", "tool_call_id": "t1", "content": "温度20摄氏度"}]
    usage = capacity("qwen3:8b", messages, tool_schemas(["weather_query"]))
    assert usage["required_tokens"] > usage["limit_tokens"]
    assert usage["dominant"] == "memory" and usage["parts"]["tool_results"] > 0
    monkeypatch.setattr(settings(), "ollama_context_length", usage["required_tokens"])
    assert capacity("qwen3:8b", messages, tool_schemas(["weather_query"]))["limit_tokens"] == usage["required_tokens"]


# test_unmatched_model_explicitly_uses_fallback：验证：未匹配模型明确标记为保守回退估算；范围以本函数断言为准。
def test_unmatched_model_explicitly_uses_fallback():
    assert capacity("different-model", [{"role": "user", "content": "你好"}])["method"] == "byte_upper_bound"
