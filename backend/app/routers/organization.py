"""Tenant-scoped member, invitation and role-management endpoints."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import (
    AuthSession,
    EmailVerificationCode,
    OrganizationMember,
    OrganizationRole,
    User,
)
from ..schemas import (
    AuthMessage,
    OrganizationInvitationAcceptIn,
    OrganizationInvitationIn,
    OrganizationMemberOut,
    OrganizationMemberRoleIn,
    OrganizationRoleOut,
    OrganizationUserCreateIn,
)
from ..services import auth_service, permission_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/organization", tags=["organization"])


def _role_key(value: object) -> str:
    return str(value or "").strip().lower()


def _member_out(member: OrganizationMember) -> OrganizationMemberOut:
    role = member.role
    user = member.user
    if not role or not user:
        raise HTTPException(409, "成员角色或账户记录不完整")
    member_status = str(member.status or "disabled")
    if member_status not in {"active", "invited", "disabled"}:
        member_status = "disabled"
    return OrganizationMemberOut(
        id=member.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name or "",
        role_key=_role_key(role.key),
        role_name=role.name or "",
        status=member_status,
        email_verified=bool(user.email_verified_at),
        created_at=member.created_at,
    )


def _management_context(db: Session):
    permission_service.require_tenant_permission(db, "manage")
    principal = permission_service.require_principal(db)
    organization = permission_service.organization_for_principal(db)
    return principal, organization


def _locked_management_context(db: Session):
    """Enter the organization-wide owner mutation critical section.

    Take the shared lock before locking a target member row so concurrent owner
    demotions cannot deadlock on opposite targets. The waiting request must
    also re-evaluate its own role after acquiring the lock.
    """
    _principal, organization = _management_context(db)
    permission_service.lock_organization_owner_changes(db, organization.id)
    permission_service.clear_request_permission_cache(db)
    return _management_context(db)


def _require_assignable_role(principal, role_key: str) -> None:
    if role_key not in permission_service.SYSTEM_ROLES:
        raise HTTPException(400, "角色不存在")
    if principal.role_key != "owner" and role_key in {"owner", "admin"}:
        raise HTTPException(403, "只有所有者可以授予所有者或管理员角色")


def _require_manageable_member(principal, member: OrganizationMember) -> None:
    target_role = _role_key(member.role.key if member.role else "")
    if principal.role_key != "owner" and target_role in {"owner", "admin"}:
        raise HTTPException(403, "管理员不能管理所有者或其他管理员账户")


def _member_for_management(
    db: Session,
    *,
    organization_id: str,
    member_id: str,
    lock: bool = False,
) -> OrganizationMember:
    stmt = (
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.role), selectinload(OrganizationMember.user))
        .where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == organization_id,
        )
    )
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    member = db.execute(stmt).scalars().first()
    if not member:
        raise HTTPException(404, "成员不存在")
    return member


def _assert_owner_change_is_safe(
    db: Session,
    *,
    organization_id: str,
    member: OrganizationMember,
    next_role_key: str | None = None,
    disabling: bool = False,
) -> None:
    current_role = _role_key(member.role.key if member.role else "")
    removes_owner = current_role == "owner" and (
        disabling or next_role_key not in {None, "owner"}
    )
    if removes_owner and permission_service.owner_count(db, organization_id) <= 1:
        raise HTTPException(409, "组织至少需要保留一名启用的所有者")


@router.get("/roles", response_model=list[OrganizationRoleOut])
def list_roles(db: Session = Depends(get_tenant_db)) -> list[OrganizationRoleOut]:
    _principal, organization = _management_context(db)
    return [
        OrganizationRoleOut(
            key=_role_key(role.key),
            name=role.name or "",
            description=role.description or "",
        )
        for role in db.execute(
            select(OrganizationRole)
            .where(OrganizationRole.organization_id == organization.id)
            .order_by(OrganizationRole.created_at, OrganizationRole.key)
        ).scalars().all()
    ]


@router.get("/members", response_model=list[OrganizationMemberOut])
def list_members(db: Session = Depends(get_tenant_db)) -> list[OrganizationMemberOut]:
    _principal, organization = _management_context(db)
    return [
        _member_out(member)
        for member in permission_service.member_rows(db, organization.id)
    ]


@router.post("/users", response_model=OrganizationMemberOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: OrganizationUserCreateIn,
    db: Session = Depends(get_tenant_db),
) -> OrganizationMemberOut:
    """Create a tenant-local account, optionally requiring email verification."""
    principal, organization = _management_context(db)
    _require_assignable_role(principal, payload.role_key)
    if payload.activate_immediately and principal.role_key != "owner":
        raise HTTPException(403, "只有所有者可以跳过邮箱验证创建账户")
    auth_service.validate_password(payload.password)
    if payload.password != payload.password_confirm:
        raise HTTPException(400, "两次输入的密码不一致")
    email = auth_service.normalize_email(payload.email)
    if db.execute(select(User.id).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(409, "该邮箱已经属于现有账户，不能跨工作区复用或迁移")

    role = permission_service.role_for_organization(db, organization.id, payload.role_key)
    now = datetime.now(timezone.utc)
    user = User(
        tenant_id=organization.tenant_id,
        email=email,
        display_name=payload.display_name.strip() or email.split("@", 1)[0],
        password_hash=auth_service.hash_password(payload.password),
        status="active" if payload.activate_immediately else "pending",
        email_verified_at=now if payload.activate_immediately else None,
    )
    db.add(user)
    db.flush()
    member = OrganizationMember(
        organization_id=organization.id,
        user_id=user.id,
        role_id=role.id,
        status="active",
    )
    db.add(member)
    db.flush()
    if not payload.activate_immediately:
        code = auth_service.issue_email_code(db, user, "register")
        try:
            auth_service.send_verification_email(email, code, "register")
        except Exception as exc:  # noqa: BLE001 - message stays non-sensitive.
            db.rollback()
            raise HTTPException(503, "账户创建成功前无法发送验证邮件") from exc
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.post("/invitations", response_model=AuthMessage, status_code=status.HTTP_201_CREATED)
def invite_member(
    payload: OrganizationInvitationIn,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Invite a new account into this tenant without a reusable URL token."""
    principal, organization = _management_context(db)
    _require_assignable_role(principal, payload.role_key)
    email = auth_service.normalize_email(payload.email)
    if db.execute(select(User.id).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(409, "该邮箱已经属于现有账户，不能跨工作区复用或迁移")
    role = permission_service.role_for_organization(db, organization.id, payload.role_key)
    user = User(
        tenant_id=organization.tenant_id,
        email=email,
        display_name=payload.display_name.strip() or email.split("@", 1)[0],
        password_hash=auth_service.hash_password(secrets.token_urlsafe(48)),
        status="pending",
    )
    db.add(user)
    db.flush()
    db.add(
        OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            role_id=role.id,
            status="invited",
        )
    )
    code = auth_service.issue_email_code(db, user, "invite")
    try:
        auth_service.send_verification_email(email, code, "invite")
    except Exception as exc:  # noqa: BLE001 - no account persists when delivery fails.
        db.rollback()
        raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
    db.commit()
    return AuthMessage(message="邀请验证码已发送", email=email)


@router.post("/invitations/accept", response_model=AuthMessage)
def accept_invitation(
    payload: OrganizationInvitationAcceptIn,
    db: Session = Depends(get_db),
) -> AuthMessage:
    """Activate exactly one administrator-created invitation."""
    email = auth_service.normalize_email(payload.email)
    auth_service.validate_password(payload.password)
    if payload.password != payload.password_confirm:
        raise HTTPException(400, "两次输入的密码不一致")
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or user.status != "pending":
        raise HTTPException(400, "邀请不存在、已失效或已处理")
    member = db.execute(
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.user))
        .where(OrganizationMember.user_id == user.id)
        .with_for_update()
    ).scalars().first()
    if not member or member.status != "invited":
        raise HTTPException(400, "邀请不存在、已失效或已处理")
    code = auth_service.find_valid_email_code(db, user, payload.code, "invite")
    if not code:
        # Persist the failed attempt before returning the intentionally generic
        # response; otherwise each rejected request would roll it back.
        db.commit()
        raise HTTPException(400, "邀请码不正确或已失效")
    now = datetime.now(timezone.utc)
    code.used_at = now
    user.password_hash = auth_service.hash_password(payload.password)
    user.display_name = payload.display_name.strip() or user.display_name
    user.status = "active"
    user.email_verified_at = now
    member.status = "active"
    db.commit()
    return AuthMessage(message="已加入工作区，请登录", email=email)


@router.put("/members/{member_id}/role", response_model=OrganizationMemberOut)
def update_member_role(
    member_id: str,
    payload: OrganizationMemberRoleIn,
    db: Session = Depends(get_tenant_db),
) -> OrganizationMemberOut:
    principal, organization = _locked_management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    current_role = _role_key(member.role.key if member.role else "")
    _require_assignable_role(principal, payload.role_key)
    _require_manageable_member(principal, member)
    _assert_owner_change_is_safe(
        db,
        organization_id=organization.id,
        member=member,
        next_role_key=payload.role_key,
    )
    member.role_id = permission_service.role_for_organization(
        db, organization.id, payload.role_key
    ).id
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.post("/members/{member_id}/disable", response_model=OrganizationMemberOut)
def disable_member(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> OrganizationMemberOut:
    principal, organization = _locked_management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    if member.user_id == principal.user_id:
        raise HTTPException(400, "不能禁用当前登录账户")
    _require_manageable_member(principal, member)
    if member.status != "active" or not member.user:
        raise HTTPException(409, "该成员未处于可禁用状态")
    _assert_owner_change_is_safe(
        db,
        organization_id=organization.id,
        member=member,
        disabling=True,
    )
    member.status = "disabled"
    member.user.status = "disabled"
    db.execute(delete(AuthSession).where(AuthSession.user_id == member.user_id))
    db.execute(
        delete(EmailVerificationCode).where(EmailVerificationCode.user_id == member.user_id)
    )
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.post("/members/{member_id}/reset-password", response_model=AuthMessage)
def send_member_password_reset(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    principal, organization = _management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    _require_manageable_member(principal, member)
    if member.status != "active" or not member.user or member.user.status != "active":
        raise HTTPException(409, "只能为已启用成员发送密码重置邮件")
    code = auth_service.issue_email_code(db, member.user, "password_reset")
    try:
        auth_service.send_verification_email(member.user.email, code, "password_reset")
    except Exception as exc:  # noqa: BLE001 - code is rolled back with the mail failure.
        db.rollback()
        raise HTTPException(503, "密码重置邮件发送失败，请检查邮件服务后重试") from exc
    db.commit()
    return AuthMessage(message="密码重置验证码已发送", email=member.user.email)


@router.post("/members/{member_id}/reinvite", response_model=AuthMessage)
def reinvite_member(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Re-enable a disabled/pending account only through a new verified invite."""
    principal, organization = _management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    _require_manageable_member(principal, member)
    if not member.user:
        raise HTTPException(409, "成员账户记录不完整")
    if member.status == "active":
        raise HTTPException(409, "已启用成员请使用密码重置或角色管理操作")
    role_key = _role_key(member.role.key if member.role else "")
    _require_assignable_role(principal, role_key)
    member.user.status = "pending"
    member.user.email_verified_at = None
    member.user.password_hash = auth_service.hash_password(secrets.token_urlsafe(48))
    member.status = "invited"
    db.execute(delete(AuthSession).where(AuthSession.user_id == member.user_id))
    db.execute(
        delete(EmailVerificationCode).where(EmailVerificationCode.user_id == member.user_id)
    )
    code = auth_service.issue_email_code(db, member.user, "invite")
    try:
        auth_service.send_verification_email(member.user.email, code, "invite")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
    db.commit()
    return AuthMessage(message="新的邀请验证码已发送", email=member.user.email)
