# 文件职责：账号注册、登录、刷新和撤销会话接口；密码哈希与模型 Key 加密是两种不同用途。
import secrets
import time

from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Account, AuthSession
from .schemas import BudgetInput, Login, Profile, Register
from .security import current, digest, hasher, token_for

router = APIRouter()


# profile：构造可返回客户端的账号信息，不返回密码哈希等凭证数据。
def profile(a):
    return {
        "id": a.id,
        "username": a.username,
        "nickname": a.nickname,
        "bio": a.bio,
        "budget": a.budget,
        "used": a.used,
        "reserved": a.reserved,
    }


# issue：创建登录会话并签发访问/刷新凭证，设置 Cookie 和 CSRF 信息。
def issue(db, response, a):
    # 刷新令牌只存摘要；CSRF 值用于同源写请求，二者作用不同。
    refresh, csrf = secrets.token_urlsafe(40), secrets.token_hex(24)
    session = AuthSession(
        account_id=a.id, refresh_hash=digest(refresh), csrf=csrf, expires=time.time() + 604800
    )
    db.add(session)
    db.commit()
    access = token_for(session)
    for name, value, http_only, age in (
        ("hai_access", access, True, 900),
        ("hai_refresh", refresh, True, 604800),
        ("hai_csrf", csrf, False, 604800),
    ):
        response.set_cookie(
            name,
            value,
            httponly=http_only,
            secure=settings().cookie_secure,
            samesite="strict",
            max_age=age,
            path="/",
        )
    return {"access_token": access, "csrf_token": csrf, "account": profile(a)}


# register：检查邀请码和账号唯一性，哈希密码后创建账号并登录。
@router.post("/auth/register", status_code=201)
def register(data: Register, response: Response, db: Session = Depends(get_db)):
    if not settings().invite_code or not secrets.compare_digest(data.invite_code, settings().invite_code):
        raise HTTPException(403, "INVITE_INVALID")
    a = Account(
        username=data.username.lower(), nickname=data.nickname, password_hash=hasher.hash(data.password)
    )
    db.add(a)
    try:
        db.commit()
    except IntegrityError:
        # 账号名冲突时回滚事务，再返回统一错误，避免留下半完成注册。
        db.rollback()
        raise HTTPException(409, "REGISTRATION_UNAVAILABLE") from None
    return issue(db, response, a)


# login：验证账号状态和密码，成功后调用统一会话签发流程。
@router.post("/auth/login")
def login(data: Login, response: Response, db: Session = Depends(get_db)):
    a = db.scalar(select(Account).where(Account.username == data.username.lower(), Account.active.is_(True)))
    try:
        if not a or not hasher.verify(a.password_hash, data.password):
            raise ValueError()
    except (ValueError, VerificationError):
        raise HTTPException(401, "INVALID_CREDENTIALS") from None
    return issue(db, response, a)


# refresh：校验刷新凭证与会话状态，轮换凭证并生成新的访问 token。
@router.post("/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get("hai_refresh", "")
    s = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == digest(token)).with_for_update())
    if not s or s.revoked or s.expires < time.time():
        raise HTTPException(401, "AUTH_REQUIRED")
    if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), s.csrf):
        raise HTTPException(403, "CSRF_REQUIRED")
    a = db.get(Account, s.account_id)
    if not a or not a.active:
        raise HTTPException(401, "AUTH_REQUIRED")
    s.revoked = True
    return issue(db, response, a)


# logout：撤销当前登录会话并删除客户端 Cookie。
@router.post("/auth/logout")
def logout(request: Request, response: Response, a=Depends(current), db: Session = Depends(get_db)):
    db.get(AuthSession, request.state.session_id).revoked = True
    db.commit()
    for key in ("hai_access", "hai_refresh", "hai_csrf"):
        response.delete_cookie(key, path="/")
    return {"ok": True}


# me：返回认证账号的公开资料和预算状态。
@router.get("/me")
def me(a=Depends(current)):
    return profile(a)


# edit_profile：更新当前账号的昵称与简介并返回资料。
@router.patch("/me")
def edit_profile(data: Profile, a=Depends(current), db: Session = Depends(get_db)):
    a.nickname, a.bio = data.nickname, data.bio
    db.commit()
    return profile(a)


# budget：更新当前账号预算配置；不能把预算降到已使用和预留总额以下。
@router.patch("/me/budgets")
def budget(data: BudgetInput, a=Depends(current), db: Session = Depends(get_db)):
    a = db.scalar(select(Account).where(Account.id == a.id).with_for_update())
    if data.budget < a.used + a.reserved:
        raise HTTPException(409, "BELOW_COMMITTED_USAGE")
    a.budget = data.budget
    db.commit()
    return profile(a)
