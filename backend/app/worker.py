# 文件职责：Celery/本地共用的任务扫描器；Redis 负责 broker，SQL Run 和账本决定任务与消费状态。
import asyncio
import time

from celery import Celery
from sqlalchemy import select

from .agents import execute_run, finish
from .config import settings
from .db import SessionLocal
from .models import Run

# Redis 传递扫描任务；未配置 Celery result backend，业务结果保存到 SQL Run。
celery = Celery("human_ai", broker=settings().redis_url)
celery.conf.update(
    timezone="UTC",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={"drain-durable-runs": {"task": "app.worker.drain", "schedule": 2.0}},
)


# drain：调度计划、修复文件和撤回索引，处理超时/取消任务，再执行有限批次待运行任务。
@celery.task(name="app.worker.drain")
def drain():
    from .chat_memory import repair_files
    from .models import CallLedger
    from .proactive import schedule_due
    from .rag import purge_revoked

    schedule_due()
    repair_files()
    try:
        purge_revoked()
    except Exception:
        pass  # Authority filtering is synchronous; physical cleanup retries next tick.
    with SessionLocal() as db:
        stale = list(
            db.scalars(
                select(Run.id).where(
                    Run.status.in_(["RUNNING", "QUEUED", "CANCEL_REQUESTED"]), Run.deadline < time.time()
                )
            )
        )
        cancelled = list(db.scalars(select(Run.id).where(Run.status == "CANCEL_REQUESTED")))
        # 每次最多取少量 SQL 排队任务，避免一个扫描周期无限占用 Worker。
        queued = list(
            db.scalars(
                select(Run.id)
                .where(Run.status == "QUEUED", Run.deadline >= time.time())
                .order_by(Run.created_at)
                .limit(5)
            )
        )
    for rid in set(stale + cancelled):
        with SessionLocal() as db:
            run = db.get(Run, rid)
            uncertain = db.scalar(
                select(CallLedger.id).where(
                    CallLedger.run_id == rid, CallLedger.state.in_(["INFLIGHT", "EXECUTING"])
                )
            )
            legacy_unknown = run.provider_called and not db.scalar(
                select(CallLedger.id).where(CallLedger.run_id == rid)
            )
            if run.status == "RUNNING" and not uncertain and not legacy_unknown:
                run.status = "PAUSED"
                run.partial = ""
                db.commit()
                continue
        finish(rid, error="CANCELLED_OR_EXPIRED")
    for rid in queued:
        asyncio.run(execute_run(rid))
