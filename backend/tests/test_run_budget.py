# 文件职责：自动化测试：调用完成后的预留释放与旧对话裁剪规则；具体保证以各测试断言为准。
import time

import pytest
from conftest import setup_chat
from fastapi import HTTPException

from app.db import SessionLocal
from app.gateways import begin_call, complete_call, fit_messages
from app.models import Run


# test_completed_call_releases_estimate_but_pending_call_keeps_reservation：验证：已完成调用释放多余估计，而未决调用保留预留；范围以本函数断言为准。
def test_completed_call_releases_estimate_but_pending_call_keeps_reservation(client):
    _, visitor, _, _, conv, _ = setup_chat(client)
    rid = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=visitor,
        json={"body": "test", "client_message_id": "budget"},
    ).json()["run_id"]
    with SessionLocal() as db:
        run = db.get(Run, rid)
        run.status, run.reserved, run.deadline = "RUNNING", 16000, time.time() + 90
        db.commit()
    ident, _ = begin_call(rid, "model:0", "model", 14329)
    with pytest.raises(HTTPException):
        begin_call(rid, "model:1", "model", 12000)
    complete_call(ident, {"content": "fixture"}, 3146)
    begin_call(rid, "model:1", "model", 12000)
    with pytest.raises(HTTPException):
        begin_call(rid, "model:2", "model", 1000)


# test_trim_old_dialogue_preserves_persona_latest_question_and_tool_exchange：验证：预算裁剪保留人设、最新问题和完整工具交换；范围以本函数断言为准。
def test_trim_old_dialogue_preserves_persona_latest_question_and_tool_exchange():
    fixed = [
        {"role": "system", "content": "角色与权限规则"},
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tool1"}]},
        {"role": "tool", "content": "工具结果", "tool_call_id": "tool1"},
        {"role": "system", "content": "最终提醒"},
    ]
    history = [
        fixed[0],
        {"role": "user", "content": "旧问题" * 3000},
        {"role": "assistant", "content": "旧回答"},
        *fixed[1:],
    ]
    result, reserve = fit_messages(history, [], 4000)
    assert result == fixed and reserve <= 4000
    with pytest.raises(HTTPException) as exc:
        fit_messages(fixed, [], 100)
    assert exc.value.detail == "RUN_BUDGET_LIMIT"
