# 文件职责：自动化测试：付款权限矩阵、恢复隔离、日志脱敏与检索评测约束；具体保证以各测试断言为准。
import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import register, role, setup_chat
from sqlalchemy import select
from test_r2 import config, own_mock, run_job

from app.agents import execute_run
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Account,
    ActionPlan,
    CallLedger,
    Comment,
    Connection,
    Conversation,
    KnowledgeDocument,
    Role,
    Run,
)
from app.proactive import schedule_due
from app.restore_guard import quarantine

DATASET = json.loads(
    (Path(__file__).resolve().parents[2] / "evaluations/synthetic-v1.json").read_text(encoding="utf-8")
)


# test_payer_permission_matrix：验证：不同主动/回复场景的付款权限、缺失连接、预算不足与撤权组合；范围以本函数断言为准。
@pytest.mark.parametrize("case", DATASET["payments"], ids=lambda case: case["id"])
def test_payer_permission_matrix(client, case):
    a, b, ar, br, conv, conn = setup_chat(client)
    own = own_mock(client, a)
    config(client, a, ar, allow_proactive_dm=True, allow_auto_post=True, allow_ai_comment_reply=True)
    client.put(
        "/api/v1/roles/" + br["id"] + "/reception",
        headers=b,
        json={"sender_role_id": ar["id"], "enabled": True},
    )
    who, source, connection = (a, ar, own) if case["expected_payer"] == "A" else (b, br, conn["id"])
    if case["expected_payer"] == "C":
        who, _ = register(client, "charlie")
        source, connection = role(client, who, "Charlie"), own_mock(client, who)
    payer = client.get("/api/v1/me", headers=who).json()["id"]
    if case["state"] == "missing_connection":
        with SessionLocal() as db:
            db.get(Connection, connection).deleted = True
            db.commit()
    if case["state"] == "budget_exhausted":
        with SessionLocal() as db:
            db.get(Account, payer).budget = 0
            db.commit()
    if case["state"] == "permission_revoked":
        with SessionLocal() as db:
            target = db.get(Role, ar["id"])
            target.ai_enabled = False
            target.policy_version += 1
            db.commit()
    trigger = case["trigger"]
    if trigger in {"proactive_dm", "owner_next_action"}:
        result = client.post(
            "/api/v1/roles/" + ar["id"] + "/plans",
            headers=a,
            json={
                "kind": "DM" if trigger == "proactive_dm" else "POST",
                "target_id": br["id"] if trigger == "proactive_dm" else "",
                "prompt": "合成评测问候",
                "connection_id": connection,
                "start_hour": 0,
                "end_hour": 24,
            },
        )
        if result.status_code == 200:
            result = client.post("/api/v1/plans/" + result.json()["id"] + "/trigger", headers=a)
    elif trigger == "visitor_reply":
        result = client.post(
            "/api/v1/conversations/" + conv["id"] + "/messages",
            headers=b,
            json={"body": "合成评测回复", "client_message_id": case["id"]},
        )
    else:
        post = client.post("/api/v1/posts", headers=a, json={"role_id": ar["id"], "body": "合成动态"}).json()
        with SessionLocal() as db:
            parent = Comment(
                post_id=post["id"],
                owner_id=db.get(Role, ar["id"]).owner_id,
                role_id=ar["id"],
                body="合成 AI 评论",
                actor="AI",
            )
            db.add(parent)
            db.commit()
            parent_id = parent.id
        result = client.post(
            "/api/v1/comments/" + parent_id + "/ai-reply",
            headers=who,
            json={
                "role_id": source["id"],
                "body": "合成回复",
                "connection_id": connection,
                "client_message_id": case["id"],
            },
        )
    if case["generation_allowed"]:
        assert result.status_code in {200, 201}, result.text
        rid = result.json()["run_id"]
        with SessionLocal() as db:
            run = db.get(Run, rid)
            assert run.payer_id == payer and run.connection_id == connection
        asyncio.run(execute_run(rid))
        assert client.get("/api/v1/runs/" + rid, headers=who).json()["status"] == "SUCCEEDED"
    else:
        assert result.status_code in {403, 404, 409, 422}, result.text
        with SessionLocal() as db:
            assert list(db.scalars(select(Run))) == []
            assert all(x.reserved == 0 for x in db.scalars(select(Account)))


# test_owner_views_unchanged_by_entire_visitor_run：验证：访客完整生成过程不改变角色拥有者可见的信息；范围以本函数断言为准。
def test_owner_views_unchanged_by_entire_visitor_run(client):
    a, b, ar, br, conv, conn = setup_chat(client)
    paths = [
        "/me",
        "/me/roles",
        "/roles",
        "/roles/" + ar["id"],
        "/roles/" + ar["id"] + "/agent",
        "/roles/" + ar["id"] + "/candidates",
        "/roles/" + ar["id"] + "/knowledge",
        "/me/runs",
        "/me/conversations",
        "/me/deliveries",
        "/me/plans",
        "/me/connections",
    ]

    # snapshot：采集拥有者可见接口结果，用于比较访客生成前后是否泄露状态。
    def snapshot():
        responses = [client.get("/api/v1" + p, headers=a) for p in paths]
        assert all(r.status_code == 200 for r in responses)
        return [r.json() for r in responses]

    before = snapshot()
    response = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=b,
        json={"body": "只有访客知道的合成消息", "client_message_id": "private-r3"},
    ).json()
    assert snapshot() == before
    asyncio.run(execute_run(response["run_id"]))
    assert snapshot() == before
    for suffix in ["", "/calls"]:
        assert client.get("/api/v1/runs/" + response["run_id"] + suffix, headers=a).status_code == 404
    assert client.post("/api/v1/conversations/" + conv["id"] + "/socket-ticket", headers=a).status_code == 404
    visitor = client.get("/api/v1/runs/" + response["run_id"], headers=b).json()
    assert visitor["duration_ms"] >= 0
    assert visitor["first_token_ms"] is None  # Mock output must not fake a streaming measurement.
    calls = client.get("/api/v1/runs/" + response["run_id"] + "/calls", headers=b).json()
    assert calls and all(c["duration_ms"] >= 0 and c["usage_known"] for c in calls)


# test_websocket_tickets_are_redacted_from_server_logs：验证：服务日志中的 WebSocket 票据被脱敏；范围以本函数断言为准。
def test_websocket_tickets_are_redacted_from_server_logs():
    import logging

    from app.logging_config import RedactSocketTicket

    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        0,
        "WebSocket %s [accepted]",
        ("/ws?ticket=secret-one-time-token",),
        None,
    )
    assert RedactSocketTicket().filter(record)
    assert "secret-one-time-token" not in record.getMessage()
    assert "/ws?[redacted]" in record.getMessage()


# test_overdue_proactive_plan_skipped_without_spending：验证：过期主动计划跳过且不预留费用；范围以本函数断言为准。
def test_overdue_proactive_plan_skipped_without_spending(client):
    a, b, ar, br, conv, conn = setup_chat(client)
    own = own_mock(client, a)
    config(client, a, ar, allow_auto_post=True)
    with SessionLocal() as db:
        plan = ActionPlan(
            owner_id=db.get(Role, ar["id"]).owner_id,
            role_id=ar["id"],
            kind="POST",
            prompt="old",
            connection_id=own,
            next_at=time.time() - 3600,
            start_hour=0,
            end_hour=24,
        )
        db.add(plan)
        db.commit()
        pid = plan.id
    schedule_due()
    schedule_due()
    with SessionLocal() as db:
        assert db.get(ActionPlan, pid).next_at > time.time()
        assert list(db.scalars(select(Run))) == []
        assert db.get(Account, db.get(Role, ar["id"]).owner_id).reserved == 0


# test_restore_quarantine_is_idempotent_and_blocks_old_credentials：验证：恢复隔离不重复结算、关闭旧授权并拒绝旧凭证；范围以本函数断言为准。
def test_restore_quarantine_is_idempotent_and_blocks_old_credentials(client):
    a, b, ar, br, conv, conn = setup_chat(client)
    response = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=b,
        json={"body": "pending", "client_message_id": "recover"},
    ).json()
    rid = response["run_id"]
    with SessionLocal() as db:
        run = db.get(Run, rid)
        run.status, run.provider_called = "RUNNING", True
        db.add(CallLedger(run_id=rid, step_key="model:0", kind="model", reserved=500, billing="payer"))
        db.commit()
        quarantine(db)
        db.commit()
        used = db.get(Account, run.payer_id).used
        quarantine(db)
        db.commit()
        assert db.get(Account, run.payer_id).used == used == 500
        assert run.status == "UNKNOWN_USAGE"
        assert not db.get(Role, ar["id"]).ai_enabled
        assert db.get(Role, ar["id"]).visibility == "PRIVATE"
        assert db.get(Connection, conn["id"]).encrypted_key == ""
        assert db.get(Conversation, conv["id"]).active_run is None
    assert client.get("/api/v1/me", headers=b).status_code == 401
    asyncio.run(execute_run(rid))  # A restored uncertain call cannot execute again.
    with SessionLocal() as db:
        assert db.get(Run, rid).status == "UNKNOWN_USAGE"


# test_hybrid_scope_and_deleted_document_filter：验证：混合检索仍限制角色范围并过滤删除文档；范围以本函数断言为准。
@pytest.mark.parametrize("mode", ["hybrid", "hybrid_rerank"])
def test_hybrid_scope_and_deleted_document_filter(client, monkeypatch, mode):
    monkeypatch.setattr(settings(), "retrieval_mode", mode)
    a, b, ar, br, conv, conn = setup_chat(client)
    own = own_mock(client, a)
    payload = {
        "title": "共享名称",
        "text": "红色海岸基地只在周二开放。",
        "connection_id": own,
        "providers": ["mock"],
        "authorized": True,
    }
    result = run_job(client, a, "/roles/" + ar["id"] + "/knowledge", payload)
    docid = result["result"]["document_id"]
    private_role = role(client, a, "Other")
    run_job(
        client,
        a,
        "/roles/" + private_role["id"] + "/knowledge",
        {**payload, "text": "红色海岸基地秘密密码是 FORBIDDEN_CROSS_ROLE。"},
    )
    response = client.post(
        "/api/v1/conversations/" + conv["id"] + "/messages",
        headers=b,
        json={"body": "红色海岸基地", "client_message_id": mode},
    ).json()
    with SessionLocal() as db:
        db.get(Run, response["run_id"]).status = "RUNNING"
        db.commit()
    from app.rag import retrieve

    hits = asyncio.run(retrieve(response["run_id"], "红色海岸基地"))
    assert hits and all(h["document_id"] == docid for h in hits)
    with SessionLocal() as db:
        db.get(KnowledgeDocument, docid).deleted = True
        db.commit()
    hits = asyncio.run(retrieve(response["run_id"], "红色海岸基地"))
    assert hits == []


# test_evaluation_rejects_scope_leaks_and_invalid_tool_parameters：验证：评测拒绝越权检索结果与无效工具参数；范围以本函数断言为准。
def test_evaluation_rejects_scope_leaks_and_invalid_tool_parameters():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "evaluations/run.py"
    spec = importlib.util.spec_from_file_location("hai_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ranking = {q["id"]: [("cook-01", 1.0)] for q in DATASET["queries"]}
    with pytest.raises(ValueError, match="cross-role"):
        module.evaluate(DATASET, ranking)
    predictions = {
        "rows": [
            {"id": q["id"], "decision": q["expected"], "arguments": {"unexpected": "forbidden"}}
            for q in DATASET["tools"]
        ]
    }
    scored = module.score_tools(DATASET, predictions)
    assert scored["correct"] == 0 and scored["total"] == 40
    predictions["rows"].append(predictions["rows"][0])
    with pytest.raises(ValueError, match="exactly one"):
        module.score_tools(DATASET, predictions)
