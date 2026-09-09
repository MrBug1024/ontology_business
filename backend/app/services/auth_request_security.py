"""Persistent auth throttling and same-origin cookie writes."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from ..access_models import AuthRateLimit, now
from ..config import get_settings
from ..database import get_db


class SensitiveAuthRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default validation body includes the rejected input,
                # which can be a password or verification code on these routes.
                return JSONResponse({"detail": "输入格式不正确，请检查邮箱、密码和验证码"}, status_code=422)
        return handle


def consume_limit(db: Session, subject: str, *, maximum: int, seconds: int) -> None:
    instant = now()
    digest = hashlib.sha256(b"ontology/auth-limit/v1\0" + subject.encode()).hexdigest()
    stale = AuthRateLimit.expires_at <= instant
    statement = insert(AuthRateLimit).values(key_hash=digest, attempts=1, expires_at=instant + timedelta(seconds=seconds))
    statement = statement.on_conflict_do_update(index_elements=[AuthRateLimit.key_hash], set_={
        "attempts": case((stale, 1), else_=AuthRateLimit.attempts + 1),
        "expires_at": case((stale, instant + timedelta(seconds=seconds)), else_=AuthRateLimit.expires_at),
    }).returning(AuthRateLimit.attempts)
    attempts = db.scalar(statement)
    expired = select(AuthRateLimit.key_hash).where(AuthRateLimit.expires_at < instant - timedelta(days=1)).limit(100)
    db.execute(delete(AuthRateLimit).where(AuthRateLimit.key_hash.in_(expired)))
    # This is a standalone atomic throttle transaction; failures consume attempts.
    db.commit()
    if attempts > maximum:
        raise HTTPException(429, "操作过于频繁，请稍后重试", headers={"Retry-After": str(seconds)})


async def auth_rate_limit(request: Request, db: Session = Depends(get_db)) -> None:
    if request.method != "POST" or request.url.path.endswith("/logout"):
        return
    # Request parsing is async; the synchronous database work is moved to a thread.
    from starlette.concurrency import run_in_threadpool

    action = request.url.path.rsplit("/", 1)[-1]
    address = request.client.host if request.client else "unknown"
    await run_in_threadpool(consume_limit, db, f"ip:{address}:{action}", maximum=60, seconds=600)
    try:
        payload = await request.json()
    except ValueError:
        return
    email = payload.get("email", "") if isinstance(payload, dict) else ""
    if isinstance(email, str) and len(email) <= 320:
        await run_in_threadpool(consume_limit, db, f"email:{email.strip().lower()}:{action}",
            maximum=10 if action in {"login", "verify-email", "reset-password"} else 5, seconds=600)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


class CookieOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            request = Request(scope)
            settings = get_settings()
            if request.cookies.get(settings.auth_cookie_name):
                source = request.headers.get("origin") or request.headers.get("referer", "")
                expected = {_origin(settings.public_app_url)} if settings.public_app_url else {
                    _origin(str(request.base_url)), *(_origin(origin) for origin in settings.cors_origins)
                }
                expected.discard("")
                if not _origin(source) or _origin(source) not in expected:
                    await JSONResponse({"detail": "请求来源无效，请从平台页面重新操作"}, status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)
