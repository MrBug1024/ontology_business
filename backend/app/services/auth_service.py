"""认证、会话和邮箱验证码服务。"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import smtplib
import ssl
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Generator

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal, get_db
from ..models import (
    Agent,
    AuthSession,
    BusinessScenario,
    DataSource,
    EmailVerificationCode,
    LLMConfig,
    MCPConfig,
    User,
)
from . import permission_service

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PBKDF2_ITERATIONS = 310_000


class MailConfigurationError(RuntimeError):
    """邮件发送配置不完整。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(value) or len(value) > 320:
        raise HTTPException(400, "请输入有效的邮箱地址")
    return value


def validate_password(password: str) -> None:
    if len(password or "") < 8:
        raise HTTPException(400, "密码至少需要 8 位")
    if len(password) > 128:
        raise HTTPException(400, "密码不能超过 128 位")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mail_message(email: str, code: str, purpose: str) -> EmailMessage:
    settings = get_settings()
    subject = "本体智能平台邮箱验证码"
    action = "完成注册" if purpose == "register" else "重置密码"
    sender = settings.mail_from.strip() or settings.mail_username.strip()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("本体智能平台", sender))
    message["To"] = email
    message.set_content(
        f"您好，您正在使用本体智能平台{action}。\n\n"
        f"验证码：{code}\n\n验证码有效期为 {settings.verification_code_minutes} 分钟。"
        "如果这不是您的操作，请忽略此邮件。"
    )
    message.add_alternative(
        "<div style=\"font-family:Arial,'Microsoft YaHei',sans-serif;color:#24333c;line-height:1.6\">"
        "<h2 style=\"margin:0 0 16px\">本体智能平台</h2>"
        f"<p>你的验证码是：</p><p style=\"font-size:28px;font-weight:700;letter-spacing:6px\">{html.escape(code)}</p>"
        f"<p>验证码 {settings.verification_code_minutes} 分钟内有效。如果不是你本人操作，请忽略此邮件。</p>"
        "</div>",
        subtype="html",
    )
    return message


def send_verification_email(email: str, code: str, purpose: str) -> None:
    settings = get_settings()
    sender = settings.mail_from.strip() or settings.mail_username.strip()
    if not settings.mail_server.strip() or not sender:
        raise MailConfigurationError("邮件服务未配置")
    message = _mail_message(email, code, purpose)
    context = ssl.create_default_context()
    timeout = max(3, settings.mail_timeout_seconds)
    if settings.mail_ssl_tls:
        with smtplib.SMTP_SSL(
            settings.mail_server,
            settings.mail_port,
            timeout=timeout,
            context=context,
        ) as smtp:
            _authenticate_and_send(smtp, sender, email, message, settings)
        return
    with smtplib.SMTP(settings.mail_server, settings.mail_port, timeout=timeout) as smtp:
        smtp.ehlo()
        if settings.mail_starttls:
            smtp.starttls(context=context)
            smtp.ehlo()
        _authenticate_and_send(smtp, sender, email, message, settings)


def _authenticate_and_send(
    client: smtplib.SMTP,
    sender: str,
    recipient: str,
    message: EmailMessage,
    settings,
) -> None:
    if settings.mail_use_credentials:
        username = settings.mail_username.strip()
        password = settings.mail_password
        if not username or not password:
            raise MailConfigurationError("邮件账号或授权码未配置")
        client.login(username, password)
    client.send_message(message, from_addr=sender, to_addrs=[recipient])


def issue_email_code(db: Session, user: User, purpose: str) -> str:
    now = utc_now()
    latest = db.execute(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == user.id,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.used_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc())
    ).scalars().first()
    if latest and (now - latest.created_at.replace(tzinfo=timezone.utc)).total_seconds() < 60:
        raise HTTPException(429, "验证码发送过于频繁，请稍后再试")
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        EmailVerificationCode(
            user_id=user.id,
            email=user.email,
            purpose=purpose,
            code_hash=_hash_code(code),
            expires_at=now + timedelta(minutes=get_settings().verification_code_minutes),
        )
    )
    return code


def claim_legacy_resources(db: Session, tenant_id: str) -> None:
    """首个用户注册时认领旧版本未带租户信息的数据。"""
    for model in (BusinessScenario, DataSource, LLMConfig, MCPConfig):
        db.execute(
            update(model)
            .where(model.tenant_id.is_(None), model.is_public.is_(False))
            .values(tenant_id=tenant_id)
        )
    db.execute(update(Agent).where(Agent.tenant_id.is_(None)).values(tenant_id=tenant_id))


def _extract_token(request: Request) -> str:
    token = request.cookies.get(get_settings().auth_cookie_name, "")
    if token:
        return token
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    session = db.execute(
        select(AuthSession).where(
            AuthSession.token_hash == _token_hash(token),
            AuthSession.expires_at > utc_now(),
        )
    ).scalars().first()
    if not session or not session.user or session.user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录")
    request.state.user_id = session.user.id
    request.state.tenant_id = session.user.tenant_id
    db.info["user_id"] = session.user.id
    db.info["tenant_id"] = session.user.tenant_id
    # 仅初始化尚不存在的组织及其历史成员；绝不因一次登录把已被管理员移除的
    # 用户重新补成 admin。缺失成员身份会在权限校验时保持拒绝，直到管理员显式添加。
    permission_service.ensure_organization(db, session.user.tenant_id)
    if not permission_service.ensure_user_membership(db, session.user):
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前用户没有有效组织成员身份")
    db.commit()
    return session.user


def get_tenant_db(user: User = Depends(get_current_user)) -> Generator[Session, None, None]:
    """受保护路由使用的数据库会话，同时携带当前用户与租户上下文。"""
    db = SessionLocal()
    db.info["user_id"] = user.id
    db.info["tenant_id"] = user.tenant_id
    try:
        yield db
    finally:
        db.close()


def tenant_id(db: Session) -> str:
    value = db.info.get("tenant_id")
    if not value:
        raise HTTPException(status_code=401, detail="缺少租户上下文")
    return str(value)


def set_session_cookie(response: Response, user: User, db: Session) -> None:
    token = secrets.token_urlsafe(48)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=utc_now() + timedelta(days=get_settings().auth_session_days),
        )
    )
    db.commit()
    response.set_cookie(
        get_settings().auth_cookie_name,
        token,
        max_age=get_settings().auth_session_days * 86400,
        httponly=True,
        secure=get_settings().auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session(request: Request, response: Response, db: Session) -> None:
    token = _extract_token(request)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
        db.commit()
    response.delete_cookie(get_settings().auth_cookie_name, path="/")
