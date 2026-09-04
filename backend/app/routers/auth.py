"""邮箱注册、验证、登录和会话接口。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AuthSession, EmailVerificationCode, OrganizationMember, Tenant, User
from ..schemas import (
    AuthMessage,
    ForgotPasswordIn,
    LoginIn,
    Msg,
    RegisterIn,
    ResendCodeIn,
    ResetPasswordIn,
    UserOut,
    VerifyEmailIn,
)
from ..services import auth_service, permission_service

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _find_code(db: Session, user: User, code: str, purpose: str) -> EmailVerificationCode:
    record = auth_service.find_valid_email_code(db, user, code, purpose)
    if not record:
        # Failed-attempt counters are security state. They must survive the
        # HTTP exception rather than being rolled back when the request closes.
        db.commit()
        raise HTTPException(400, "验证码不正确或已失效")
    return record


@router.post("/register", response_model=AuthMessage)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    auth_service.validate_password(payload.password)
    if payload.password != payload.password_confirm:
        raise HTTPException(400, "两次输入的密码不一致")

    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if user:
        # Registration is intentionally not an account-recovery mechanism.
        # In particular, a disabled account must never regain access by simply
        # re-verifying its own email address.  Administrators use the scoped
        # organization invitation/re-enable flow instead.
        if user.status == "disabled":
            raise HTTPException(403, "该账户已被禁用，请联系工作区管理员")
        raise HTTPException(409, "该邮箱已注册，请登录或重发验证码")

    first_user = db.execute(select(User.id)).first() is None
    created_tenant = False
    tenant = Tenant(name=f"{payload.display_name.strip() or email.split('@')[0]} 的工作区")
    db.add(tenant)
    db.flush()
    created_tenant = True
    user = User(
        tenant_id=tenant.id,
        email=email,
        display_name=payload.display_name.strip() or email.split("@")[0],
        password_hash=auth_service.hash_password(payload.password),
        status="pending",
    )
    db.add(user)
    db.flush()
    if first_user:
        auth_service.claim_legacy_resources(db, tenant.id)

    # 新租户创建者立即成为 owner；升级前已有同租户用户由服务统一回填为 owner/admin。
    permission_service.ensure_organization(
        db,
        user.tenant_id,
        owner_user_id=user.id if created_tenant else None,
    )

    code = auth_service.issue_email_code(db, user, "register")
    try:
        auth_service.send_verification_email(email, code, "register")
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("注册验证码邮件发送失败（邮箱域名=%s）", email.rsplit("@", 1)[-1])
        raise HTTPException(503, "验证码邮件发送失败，请稍后重试")
    db.commit()
    return AuthMessage(message="验证码已发送，请查收邮件", email=email)


@router.post("/verify-email", response_model=AuthMessage)
def verify_email(payload: VerifyEmailIn, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or user.status != "pending":
        raise HTTPException(400, "验证码不正确或已失效")
    record = _find_code(db, user, payload.code, "register")
    member = db.execute(
        select(OrganizationMember).where(OrganizationMember.user_id == user.id)
    ).scalars().first()
    if not member or member.status != "active":
        raise HTTPException(403, "当前用户没有有效组织成员身份")
    record.used_at = datetime.now(timezone.utc)
    user.status = "active"
    user.email_verified_at = datetime.now(timezone.utc)
    member.status = "active"
    db.commit()
    return AuthMessage(message="邮箱验证成功，请登录", email=email)


@router.post("/resend-code", response_model=AuthMessage)
def resend_code(payload: ResendCodeIn, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or user.status != "pending":
        return AuthMessage(message="如果该邮箱需要验证，新的验证码已发送", email=email)
    member = db.execute(
        select(OrganizationMember).where(OrganizationMember.user_id == user.id)
    ).scalars().first()
    if not member or member.status != "active":
        # Invitations have a dedicated acceptance path so a public resend does
        # not mutate their purpose or turn a disabled membership active.
        return AuthMessage(message="如果该邮箱需要验证，新的验证码已发送", email=email)
    code = auth_service.issue_email_code(db, user, "register")
    try:
        auth_service.send_verification_email(email, code, "register")
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("重发注册验证码邮件失败（邮箱域名=%s）", email.rsplit("@", 1)[-1])
        raise HTTPException(503, "验证码邮件发送失败，请稍后重试")
    db.commit()
    return AuthMessage(message="新的验证码已发送", email=email)


@router.post("/login", response_model=UserOut)
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not auth_service.verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码不正确")
    if user.status == "disabled":
        raise HTTPException(403, "账户已被禁用，请联系工作区管理员")
    if user.status != "active":
        raise HTTPException(403, "请先完成邮箱验证")
    auth_service.set_session_cookie(response, user, db)
    return auth_service.build_user_out(db, user, active_tenant_id=user.tenant_id)


@router.post("/logout", response_model=Msg)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth_service.clear_session(request, response, db)
    return Msg(message="已退出登录")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(auth_service.get_current_user), db: Session = Depends(get_db)):
    return auth_service.build_user_out(db, user)


@router.post("/forgot-password", response_model=AuthMessage)
def forgot_password(payload: ForgotPasswordIn, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    user = db.execute(select(User).where(User.email == email, User.status == "active")).scalars().first()
    if not user:
        return AuthMessage(message="如果该邮箱已注册，重置验证码已发送", email=email)
    code = auth_service.issue_email_code(db, user, "password_reset")
    try:
        auth_service.send_verification_email(email, code, "password_reset")
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("重置密码验证码邮件发送失败（邮箱域名=%s）", email.rsplit("@", 1)[-1])
        raise HTTPException(503, "验证码邮件发送失败，请稍后重试")
    db.commit()
    return AuthMessage(message="重置验证码已发送", email=email)


@router.post("/reset-password", response_model=AuthMessage)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)):
    email = auth_service.normalize_email(payload.email)
    auth_service.validate_password(payload.password)
    if payload.password != payload.password_confirm:
        raise HTTPException(400, "两次输入的密码不一致")
    user = db.execute(select(User).where(User.email == email, User.status == "active")).scalars().first()
    if not user:
        raise HTTPException(400, "验证码不正确或已失效")
    record = _find_code(db, user, payload.code, "password_reset")
    record.used_at = datetime.now(timezone.utc)
    user.password_hash = auth_service.hash_password(payload.password)
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.commit()
    return AuthMessage(message="密码已重置，请使用新密码登录", email=email)
