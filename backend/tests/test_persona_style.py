# 文件职责：自动化测试：工具往返之后仍注入会话人设快照与风格规则；具体保证以各测试断言为准。
import asyncio
import time

from conftest import setup_chat

from app.agents import context, reason
from app.db import SessionLocal
from app.models import CallLedger, Conversation, Role, Run


# test_style_contract_survives_tool_followup_and_uses_session_snapshot：验证：工具后续请求仍使用会话人设快照和风格约束；范围以本函数断言为准。
def test_style_contract_survives_tool_followup_and_uses_session_snapshot(client, monkeypatch):
    _, visitor, target, source, conv, _ = setup_chat(client)
    sent = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=visitor,
        json={"body": "上海天气如何？", "client_message_id": "style-tool"},
    ).json()
    rid = sent["run_id"]
    with SessionLocal() as db:
        run = db.get(Run, rid)
        run.status, run.deadline = "RUNNING", time.time() + 100
        db.get(Conversation, conv["id"]).persona_snapshot = "温柔，用短句，称呼对方为朋友。"
        db.get(Role, target["id"]).persona = "后来修改的角色设定，不应自动进入旧会话"
        db.get(Role, source["id"]).persona = "发送者的秘密设定，不应注入目标角色"
        db.add(
            CallLedger(
                run_id=rid,
                step_key="model:0",
                kind="model",
                state="DONE",
                result={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "weather-1",
                            "type": "function",
                            "function": {"name": "weather_query", "arguments": '{"place":"上海"}'},
                        }
                    ],
                },
            )
        )
        db.add(
            CallLedger(
                run_id=rid,
                step_key="tool:0:0",
                kind="weather_query",
                state="DONE",
                result={"temperature": 20, "source": "synthetic-test"},
            )
        )
        db.commit()
    captured = []

    # fake_model：捕获并返回模型替身结果，以检查请求中的人设和工具上下文。
    async def fake_model(rid, key, messages, tools=None):
        captured.append(messages)
        return {"content": "朋友，测试温度是20度。", "tool_calls": []}

    monkeypatch.setattr("app.agents.model_call", fake_model)
    asyncio.run(reason({"run_id": rid, "step": 0, "refs": []}))
    asyncio.run(reason({"run_id": rid, "step": 1, "refs": []}))
    for messages in captured:
        system = messages[0]["content"]
        assert "温柔，用短句" in system and target["name"] in system
        assert "每一条" in system and "调用工具后" in system
        assert "后来修改的角色设定" not in system and "发送者的秘密设定" not in system
    assert captured[1][-2]["role"] == "tool"
    assert "synthetic-test" in captured[1][-2]["content"]
    assert context(rid)[-1]["content"] == "上海天气如何？"
