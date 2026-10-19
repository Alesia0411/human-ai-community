# 文件职责：只对隔离恢复库执行的撤权保护：关闭 AI、清除旧凭证并保守结算未知消费；不是日常清理工具。
"""Offline recovery quarantine. Use only on an isolated restored database.

An old backup cannot reveal subsequent revocations. Reset every AI grant instead
of guessing which grants remain valid. This deliberately sacrifices availability.
"""

import argparse
import json
import time

from sqlalchemy import select

from .db import SessionLocal
from .models import (
    Account,
    ActionPlan,
    AgentConfig,
    AuthSession,
    CallLedger,
    Connection,
    Conversation,
    KnowledgeChunk,
    KnowledgeDocument,
    LearningConsent,
    PersonaCandidate,
    Reception,
    Role,
    Run,
    SocketTicket,
)


# quarantine：批量撤销旧授权并停止未完成任务，避免恢复备份后重放外部消费；不在此提交事务。
def quarantine(db):
    counts = {}

    # rows：读取指定模型全部记录并记录扫描行数；计数不等于实际变化行数。
    def rows(model):
        result = list(db.scalars(select(model)))
        counts[model.__tablename__] = len(result)
        return result

    for role in rows(Role):
        role.ai_enabled = role.public_ai = False
        # A role made private/deleted after the backup must not reappear publicly.
        # 旧备份无法知道后续是否撤销公开；保守设为私有，等待管理员和拥有者重新确认。
        role.visibility = "PRIVATE"
        role.policy_version += 1
    for config in rows(AgentConfig):
        config.config = {}
        config.active_candidate = None
        config.consent_version += 1
        config.learning_since = time.time()
    for consent in rows(LearningConsent):
        consent.enabled, consent.providers = False, []
    for permission in rows(Reception):
        permission.enabled = False
    for plan in rows(ActionPlan):
        plan.enabled = False
    for candidate in rows(PersonaCandidate):
        candidate.valid, candidate.fields, candidate.evidence = False, {}, []
    for document in rows(KnowledgeDocument):
        document.deleted, document.body, document.status = True, "", "REVOKED"
    for chunk in rows(KnowledgeChunk):
        db.delete(chunk)
    for conv in rows(Conversation):
        # 只标记派生文件需同步；此函数并不物理删除消息文件或 ChatMemory 摘要表。
        conv.files_dirty = True
        conv.memory_enabled, conv.grants, conv.active_run, conv.candidate_id = False, {}, None, None
        conv.policy_version += 1
    for connection in rows(Connection):
        # 清除当前恢复库中的凭证副本，不等于在服务商后台撤销真实 API Key。
        connection.deleted, connection.encrypted_key = True, ""
    for session in rows(AuthSession):
        session.revoked = True
    for ticket in rows(SocketTicket):
        ticket.used = True
    # Conservatively reserve uncertain consumption once; never call a provider.
    accounts = {a.id: a for a in rows(Account)}
    calls = rows(CallLedger)
    for run in rows(Run):
        if run.status not in {"QUEUED", "RUNNING", "PAUSED", "CANCEL_REQUESTED"}:
            continue
        ledger = [c for c in calls if c.run_id == run.id]
        uncertain = any(c.billing == "payer" and c.state != "DONE" for c in ledger)
        # 已开始外部请求却无账本时也按未知消费处理，不自动重试获取答案。
        uncertain |= run.provider_called and not ledger
        run.status = "UNKNOWN_USAGE" if uncertain else "CANCELLED"
        run.error, run.partial, run.result = "RESTORE_QUARANTINE", "", {}
        run.finished_at = time.time()
        run.used = (
            sum(c.used if c.state == "DONE" else c.reserved for c in ledger)
            if ledger
            else run.reserved
            if uncertain
            else 0
        )
        if run.payer_id in accounts:
            # 仅结算仍未结束的任务；第二次隔离时这些任务已是终态，不再重复累计。
            accounts[run.payer_id].used += run.used
    for account in accounts.values():
        account.reserved = 0
    for call in calls:
        call.result = {}
        if call.state in {"INFLIGHT", "EXECUTING"}:
            call.state = "UNKNOWN_USAGE"
    db.flush()
    return counts


# main：要求显式恢复标志后执行隔离并提交；标志本身不验证目标库确实隔离。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # 显式标志防误运行，但不自动证明数据库已隔离；连接目标仍由应用配置决定。
    parser.add_argument("--apply-to-isolated-restore", action="store_true", required=True)
    parser.parse_args()
    with SessionLocal() as db:
        counts = quarantine(db)
        db.commit()
    print(json.dumps({"status": "quarantined", "counts": counts}))


if __name__ == "__main__":
    main()
