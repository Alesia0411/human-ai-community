# 文件职责：自动化测试：检查点恢复、私有资料隔离及评论回复付款；具体保证以各测试断言为准。
import asyncio

from conftest import setup_chat
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select
from test_r2 import config, own_mock, run_job

from app.agents import builder, execute_run
from app.config import settings
from app.db import SessionLocal
from app.models import CallLedger, Run


# test_checkpoint_resume_revalidates_and_does_not_duplicate：验证：检查点恢复重新验权且不重复发布；范围以本函数断言为准。
def test_checkpoint_resume_revalidates_and_does_not_duplicate(client):
    a, b, ar, br, c, conn = setup_chat(client)
    rid = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "恢复测试", "client_message_id": "recover"},
    ).json()["run_id"]
    with SessionLocal() as db:
        db.get(Run, rid).status = "RUNNING"
        db.commit()

    # checkpoint：模拟图执行中断或检查点行为，验证恢复路径。
    async def checkpoint():
        async with AsyncSqliteSaver.from_conn_string(settings().checkpoint_path) as saver:
            graph = builder.compile(checkpointer=saver, interrupt_after=["reason"])
            state = await graph.ainvoke({"run_id": rid}, {"configurable": {"thread_id": rid}})
            assert state["step"] == 0

    asyncio.run(checkpoint())
    with SessionLocal() as db:
        run = db.get(Run, rid)
        run.status = "PAUSED"
        db.commit()
    assert client.post("/api/v1/runs/" + rid + "/resume", headers=a).status_code == 404
    assert client.post("/api/v1/runs/" + rid + "/resume", headers=b).status_code == 200
    asyncio.run(execute_run(rid))
    assert client.get("/api/v1/runs/" + rid, headers=b).json()["status"] == "SUCCEEDED"
    assert len(client.get("/api/v1/conversations/" + c["id"] + "/messages", headers=b).json()) == 2
    with SessionLocal() as db:
        assert len(list(db.scalars(select(CallLedger).where(CallLedger.run_id == rid)))) == 1


# test_private_memory_cannot_be_retrieved_by_other_visitor_or_owner：验证：私有会话知识不会被其他访客或角色拥有者检索；范围以本函数断言为准。
def test_private_memory_cannot_be_retrieved_by_other_visitor_or_owner(client):
    from app.rag import retrieve

    a, b, ar, br, c, conn = setup_chat(client)
    endpoint = "/api/v1/conversations/" + c["id"] + "/agent-settings"
    assert (
        client.put(
            endpoint, headers=b, json={"connection_id": conn["id"], "memory_enabled": True}
        ).status_code
        == 200
    )
    data = {
        "title": "私密代号",
        "text": "我的露营计划代号是红色蒲公英",
        "connection_id": conn["id"],
        "providers": ["mock"],
        "authorized": True,
        "conversation_id": c["id"],
    }
    result = run_job(client, b, "/roles/" + ar["id"] + "/knowledge", data)
    assert result["status"] == "SUCCEEDED"
    other = client.post(
        "/api/v1/conversations",
        headers=b,
        json={
            "kind": "AI_DIRECT",
            "source_role_id": br["id"],
            "target_role_id": ar["id"],
            "connection_id": conn["id"],
        },
    ).json()
    rid = client.post(
        "/api/v1/conversations/" + other["id"] + "/messages",
        headers=b,
        json={"body": "露营计划代号", "client_message_id": "x"},
    ).json()["run_id"]
    with SessionLocal() as db:
        db.get(Run, rid).status = "RUNNING"
        db.commit()
    assert asyncio.run(retrieve(rid, "露营计划代号")) == []
    assert client.get("/api/v1/roles/" + ar["id"] + "/knowledge", headers=a).json() == []
    assert client.get("/api/v1/runs/" + result["id"] + "/calls", headers=a).status_code == 404


# test_ai_comment_direct_parent_pays_once_and_cannot_forge_actor：验证：AI 评论回复绑定父评论、付款去重且身份不可伪造；范围以本函数断言为准。
def test_ai_comment_direct_parent_pays_once_and_cannot_forge_actor(client):
    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)
    config(client, a, ar, allow_auto_comment=True, allow_ai_comment_reply=True)
    config(client, b, br, accept_ai_comments=True)
    post = client.post("/api/v1/posts", headers=b, json={"role_id": br["id"], "body": "公园散步"}).json()
    plan = client.post(
        "/api/v1/roles/" + ar["id"] + "/plans",
        headers=a,
        json={
            "kind": "COMMENT",
            "target_id": post["id"],
            "prompt": "问候",
            "connection_id": own,
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    result = run_job(client, a, "/plans/" + plan["id"] + "/trigger", {})
    client.post("/api/v1/deliveries/" + result["result"]["delivery_id"] + "/publish", headers=a)
    parent = client.get("/api/v1/posts/" + post["id"] + "/comments", headers=b).json()[0]
    assert parent["actor"] == "AI"
    payload = {
        "role_id": br["id"],
        "body": "谢谢你",
        "connection_id": conn["id"],
        "client_message_id": "reply-once",
    }
    first = run_job(client, b, "/comments/" + parent["id"] + "/ai-reply", payload)
    second = client.post("/api/v1/comments/" + parent["id"] + "/ai-reply", headers=b, json=payload).json()
    assert first["id"] == second["run_id"] and first["status"] == "SUCCEEDED"
    assert len(client.get("/api/v1/posts/" + post["id"] + "/comments", headers=b).json()) == 3
    assert client.get("/api/v1/runs/" + first["id"], headers=a).status_code == 404
    assert (
        client.post(
            "/api/v1/posts/" + post["id"] + "/comments",
            headers=b,
            json={"role_id": br["id"], "body": "伪造", "actor": "AI"},
        ).status_code
        == 422
    )


# test_revoke_after_index_before_retrieval_blocks_immediately：验证：索引后检索前撤权立即阻止资料使用；范围以本函数断言为准。
def test_revoke_after_index_before_retrieval_blocks_immediately(client):
    from app.rag import retrieve

    a, b, ar, br, c, conn = setup_chat(client)
    own = own_mock(client, a)
    result = run_job(
        client,
        a,
        "/roles/" + ar["id"] + "/knowledge",
        {
            "title": "知识",
            "text": "秘密配方为星光露水",
            "connection_id": own,
            "providers": ["mock"],
            "authorized": True,
        },
    )
    did = result["result"]["document_id"]
    assert client.delete("/api/v1/knowledge/" + did, headers=a).status_code == 200
    rid = client.post(
        "/api/v1/conversations/" + c["id"] + "/messages",
        headers=b,
        json={"body": "秘密配方", "client_message_id": "delete"},
    ).json()["run_id"]
    with SessionLocal() as db:
        db.get(Run, rid).status = "RUNNING"
        db.commit()
    assert asyncio.run(retrieve(rid, "秘密配方")) == []
