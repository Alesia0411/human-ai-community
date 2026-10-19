# 文件职责：FastAPI 应用入口：生命周期、安全中间件、路由注册、健康检查和私有 WebSocket 快照。
import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, update

from . import proactive, routes_auth, routes_chat, routes_media, routes_r2, routes_roles, routes_social
from .config import settings
from .db import SessionLocal
from .logging_config import install
from .models import Account, AuthSession, Message, Run, SocketTicket
from .policy import conversation
from .routes_chat import cleared_seq, conversation_hidden, message_view, run_view
from .security import digest

install()


# lifespan：启动时校验密钥；按配置启动本地工作循环，退出时取消并等待该任务。
@asynccontextmanager
async def lifespan(app):
    settings().signing_key()

    # local_worker：定期在线程中执行同步 drain；单次异常不终止后续扫描。
    async def local_worker():
        from .worker import drain

        while True:
            try:
                await asyncio.to_thread(drain)
            except Exception:
                pass
            await asyncio.sleep(2)

    task = asyncio.create_task(local_worker()) if settings().inline_tasks else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="共栖 · Human AI", version="0.3.0", lifespan=lifespan)
origins = settings().allowed_origins.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)
attempts = defaultdict(deque)


# guards：检查写请求来源并限制认证接口频率，为响应增加禁止缓存等安全头。
@app.middleware("http")
async def guards(request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in origins:
        return JSONResponse({"detail": "ORIGIN_DENIED"}, status_code=403)
    if request.url.path.startswith("/api/v1/auth/"):
        key = request.client.host
        window = attempts[key]
        now = time.monotonic()
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= 30:
            return JSONResponse({"detail": "RATE_LIMITED"}, status_code=429)
        window.append(now)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


for router in (
    routes_auth.router,
    routes_roles.router,
    routes_chat.router,
    routes_social.router,
    routes_media.router,
    routes_r2.router,
    proactive.router,
):
    app.include_router(router, prefix="/api/v1")


# live：返回存活状态和发布标识；不代表数据库、模型等依赖全部可用。
@app.get("/api/v1/health/live")
def live():
    return {"status": "ok", "release": "R3", "demo_mode": settings().demo_mode}


# ready：探测数据库可用性，用于容器就绪检查。
@app.get("/api/v1/health/ready")
def ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT version_num FROM alembic_version"))
    except Exception:
        raise HTTPException(503, "DATABASE_NOT_READY") from None
    return {"status": "ok"}


# socket：消费一次性票据后推送有权查看的消息和任务快照；持续复查权限及清空边界。
@app.websocket("/ws")
async def socket(ws: WebSocket):
    if ws.headers.get("origin") and ws.headers["origin"] not in origins:
        await ws.close(code=4403)
        return
    with SessionLocal() as db:
        ticket = db.scalar(
            select(SocketTicket).where(SocketTicket.token_hash == digest(ws.query_params.get("ticket", "")))
        )
        if not ticket or ticket.expires < time.time():
            await ws.close(code=4404)
            return
        changed = db.execute(
            update(SocketTicket)
            .where(SocketTicket.id == ticket.id, SocketTicket.used.is_(False))
            .values(used=True)
        )
        db.commit()
        if changed.rowcount != 1:
            await ws.close(code=4404)
            return
        account_id, session_id, cid = ticket.account_id, ticket.session_id, ticket.conversation_id
    await ws.accept()
    # Durable DB snapshots repair missed changes (including retractions) after any reconnect.
    last = None
    try:
        while True:
            with SessionLocal() as db:
                sess, account = db.get(AuthSession, session_id), db.get(Account, account_id)
                if (
                    not sess
                    or sess.revoked
                    or sess.expires < time.time()
                    or not account
                    or not account.active
                ):
                    await ws.close(code=4401)
                    return
                conv = conversation(db, account_id, cid)
                if conversation_hidden(conv, account_id):
                    await ws.send_json({"type": "conversation_removed"})
                    await ws.close(code=1000)
                    return
                rows = list(
                    db.scalars(
                        select(Message)
                        .where(Message.conversation_id == cid, Message.seq > cleared_seq(conv, account_id))
                        .order_by(Message.seq.desc())
                        .limit(100)
                    )
                )[::-1]
                runs = list(
                    db.scalars(
                        select(Run)
                        .where(
                            Run.conversation_id == cid, Run.payer_id == account_id,
                            Run.created_at > conv.cleared_at,
                        )
                        .order_by(Run.created_at.desc())
                        .limit(1)
                    )
                )
                payload = {
                    "type": "snapshot",
                    "cleared_seq": cleared_seq(conv, account_id),
                    "messages": [message_view(m) for m in rows],
                    "run": run_view(runs[0]) if runs else None,
                    "seq": conv.seq,
                }
            if payload != last:
                await ws.send_json(payload)
                last = payload
            else:
                await ws.send_json({"type": "heartbeat"})
            await asyncio.sleep(0.6)
    except Exception:
        try:
            await ws.close(code=4404)
        except Exception:
            pass
