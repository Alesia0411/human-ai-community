# 文件职责：密码哈希、API Key 加解密、JWT 与请求身份验证；数据库会话支持即时撤销。
import base64
import hashlib
import os
import time

import jwt
from argon2 import PasswordHasher
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Account, AuthSession

hasher = PasswordHasher()


# digest：计算字符串 SHA256 摘要，用于令牌查找；密码另用 Argon2，不用此函数存密码。
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# encrypt：用随机 nonce 和 AES-GCM 加密模型 Key，再将 nonce 与密文一起 Base64 编码。
def encrypt(value):
    # 同一密钥每次加密使用新的 12 字节 nonce；Base64 只是封装，不提供加密能力。
    nonce = os.urandom(12)
    cipher = AESGCM(hashlib.sha256(settings().signing_key().encode()).digest())
    return base64.b64encode(nonce + cipher.encrypt(nonce, value.encode(), b"hai-key-v1")).decode()


# decrypt：拆分 nonce 并验证 AES-GCM 认证数据，恢复仅供服务端调用的 Key。
def decrypt(value):
    raw = base64.b64decode(value)
    cipher = AESGCM(hashlib.sha256(settings().signing_key().encode()).digest())
    return cipher.decrypt(raw[:12], raw[12:], b"hai-key-v1").decode()


# token_for：签发绑定账号及会话 ID 的短期访问 JWT。
def token_for(session):
    return jwt.encode(
        {"sub": session.account_id, "sid": session.id, "exp": time.time() + settings().access_minutes * 60},
        settings().signing_key(),
        algorithm="HS256",
    )


# authenticate：验证 JWT 后回查账号和登录会话，拒绝撤销、过期或不匹配的身份。
def authenticate(token, db):
    try:
        # 固定签名算法并验证过期时间；验签后还必须查询会话撤销状态。
        claims = jwt.decode(token, settings().signing_key(), algorithms=["HS256"])
        sess = db.get(AuthSession, claims["sid"])
        account = db.get(Account, claims["sub"])
        if (
            not sess
            or sess.revoked
            or sess.expires < time.time()
            or not account
            or not account.active
            or sess.account_id != account.id
        ):
            raise ValueError()
        return account, sess
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(401, "AUTH_REQUIRED") from None


# current：从 Bearer 或 Cookie 取得身份；Cookie 写请求需 CSRF 校验，并记录当前会话 ID。
def current(request: Request, db: Session = Depends(get_db)):
    header = request.headers.get("authorization", "")
    token = (
        header.removeprefix("Bearer ")
        if header.startswith("Bearer ")
        else request.cookies.get("hai_access", "")
    )
    if not header and request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf = request.cookies.get("hai_csrf")
        if not csrf or request.headers.get("x-csrf-token") != csrf:
            raise HTTPException(403, "CSRF_REQUIRED")
    account, sess = authenticate(token, db)
    # 保存当前请求对应登录会话，退出登录可精确撤销本次会话。
    request.state.session_id = sess.id
    return account
