"""Tenant-scoped member, invitation and role-management endpoints."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import (
    EmailVerificationCode,
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)
from ..schemas import (
    AuthMessage,
    OrganizationInvitationAcceptIn,
    OrganizationInvitationInboxOut,
    OrganizationInvitationIn,
    OrganizationMemberOut,
    OrganizationMemberRoleIn,
    OrganizationRoleOut,
    OrganizationUserCreateIn,
    OrganizationWorkspaceOut,
    UserOut,
)
from ..services import auth_service, permission_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/organization", tags=["organization"])

_WORKSPACE_INVITATION_LIFETIME = timedelta(hours=24)


def _role_key(value: object) -> str:
    return str(value or "").strip().lower()


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _ensure_independent_home_workspace(
    db: Session,
    user: User,
    *,
    shared_tenant_id: str,
) -> None:
    """Give a legacy tenant-local collaborator a home workspace before removal."""
    if user.tenant_id != shared_tenant_id:
        return
    home_tenant = Tenant(
        name=f"{user.display_name or user.email.split('@', 1)[0]} 的工作区"
    )
    db.add(home_tenant)
    db.flush()
    user.tenant_id = home_tenant.id
    permission_service.ensure_organization(
        db,
        home_tenant.id,
        owner_user_id=user.id,
    )


def _member_out(
    member: OrganizationMember,
    invitation: OrganizationInvitation | None = None,
) -> OrganizationMemberOut:
    role = member.role
    user = member.user
    if not role or not user:
        raise HTTPException(409, "成员角色或账户记录不完整")
    member_status = str(member.status or "removed")
    if member_status not in {"active", "invited", "removed", "disabled"}:
        member_status = "removed"
    now = datetime.now(timezone.utc)
    has_pending_invitation = bool(
        invitation
        and invitation.status == "pending"
        and _as_utc(invitation.expires_at) > now
    )
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
        is_external_member=bool(
            member.organization
            and user.tenant_id != member.organization.tenant_id
        ),
        invited_by_name=(
            member.invited_by_user.display_name or member.invited_by_user.email
            if member.invited_by_user
            else ""
        ),
        invitation_expires_at=(invitation.expires_at if invitation else None),
        has_pending_invitation=has_pending_invitation,
    )


def _pending_invitations_by_member(
    db: Session,
    member_ids: list[str],
) -> dict[str, OrganizationInvitation]:
    if not member_ids:
        return {}
    records = db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.member_id.in_(member_ids),
            OrganizationInvitation.status == "pending",
        )
        .order_by(
            OrganizationInvitation.member_id,
            OrganizationInvitation.created_at.desc(),
        )
    ).scalars().all()
    latest: dict[str, OrganizationInvitation] = {}
    for record in records:
        latest.setdefault(record.member_id, record)
    return latest


def _revoke_pending_invitations(
    db: Session,
    *,
    member_id: str,
    now: datetime,
) -> None:
    pending = db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.member_id == member_id,
            OrganizationInvitation.status == "pending",
        )
        .with_for_update()
    ).scalars().all()
    for invitation in pending:
        invitation.status = "revoked"
        invitation.revoked_at = now


def _issue_registered_workspace_invitation(
    db: Session,
    *,
    organization: Organization,
    principal,
    member: OrganizationMember,
) -> None:
    """Queue a fresh, 24-hour invitation without mutating the recipient's account."""
    if not member.user:
        raise HTTPException(409, "成员账户记录不完整")
    now = datetime.now(timezone.utc)
    _revoke_pending_invitations(db, member_id=member.id, now=now)
    member.status = "invited"
    member.invited_by_user_id = principal.user_id
    code = auth_service.issue_workspace_invitation_code()
    inviter = db.get(User, principal.user_id)
    db.add(
        OrganizationInvitation(
            organization_id=organization.id,
            member_id=member.id,
            user_id=member.user_id,
            invited_by_user_id=principal.user_id,
            status="pending",
            code_hash=auth_service.hash_workspace_invitation_code(code),
            expires_at=now + _WORKSPACE_INVITATION_LIFETIME,
        )
    )
    auth_service.send_workspace_invitation_email(
        member.user.email,
        code,
        workspace_name=organization.name or "未命名工作区",
        inviter_name=(
            (inviter.display_name or inviter.email) if inviter else "工作区管理员"
        ),
    )


def _workspace_out(member: OrganizationMember, active_tenant_id: str) -> OrganizationWorkspaceOut:
    if not member.organization or not member.role:
        raise HTTPException(409, "工作区成员记录不完整")
    return OrganizationWorkspaceOut(
        organization_id=member.organization_id,
        tenant_id=member.organization.tenant_id,
        name=member.organization.name or "",
        role_key=_role_key(member.role.key),
        role_name=member.role.name or "",
        is_active=member.organization.tenant_id == active_tenant_id,
    )


def _invitation_inbox_out(
    invitation: OrganizationInvitation,
) -> OrganizationInvitationInboxOut:
    if not invitation.organization or not invitation.member or not invitation.member.role:
        raise HTTPException(409, "工作区邀请记录不完整")
    inviter = invitation.invited_by_user
    return OrganizationInvitationInboxOut(
        id=invitation.id,
        organization_id=invitation.organization_id,
        organization_name=invitation.organization.name or "",
        inviter_name=(inviter.display_name or inviter.email) if inviter else "工作区管理员",
        role_key=_role_key(invitation.member.role.key),
        role_name=invitation.member.role.name or "",
        expires_at=invitation.expires_at,
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
        .options(
            selectinload(OrganizationMember.role),
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.invited_by_user),
        )
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
    members = list(permission_service.member_rows(db, organization.id))
    invitations = _pending_invitations_by_member(
        db, [member.id for member in members]
    )
    return [
        _member_out(member, invitations.get(member.id))
        for member in members
    ]


@router.get("/workspaces", response_model=list[OrganizationWorkspaceOut])
def list_workspaces(db: Session = Depends(get_tenant_db)) -> list[OrganizationWorkspaceOut]:
    """List active workspaces available to the current account."""
    principal = permission_service.require_principal(db)
    active_tenant_id = str(db.info.get("tenant_id") or "")
    return [
        _workspace_out(member, active_tenant_id)
        for member in permission_service.active_workspace_memberships(db, principal.user_id)
    ]


@router.post("/workspaces/{organization_id}/switch", response_model=UserOut)
def switch_workspace(
    organization_id: str,
    request: Request,
    db: Session = Depends(get_tenant_db),
) -> UserOut:
    """Switch only the current browser session after verifying membership."""
    principal = permission_service.require_principal(db)
    membership = db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.organization)
        .options(
            selectinload(OrganizationMember.organization),
            selectinload(OrganizationMember.role),
        )
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == principal.user_id,
            OrganizationMember.status == "active",
        )
        .with_for_update()
    ).scalars().first()
    if not membership or not membership.organization:
        raise HTTPException(403, "当前账户没有该工作区的访问权限")
    auth_service.set_session_active_tenant(
        request,
        db,
        user_id=principal.user_id,
        tenant_id=membership.organization.tenant_id,
    )
    db.commit()
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(401, "当前账户已失效")
    return auth_service.build_user_out(
        db, user, active_tenant_id=membership.organization.tenant_id
    )


@router.post("/users", response_model=OrganizationMemberOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: OrganizationUserCreateIn,
    db: Session = Depends(get_tenant_db),
) -> OrganizationMemberOut:
    """Account creation belongs to platform onboarding, not workspace RBAC."""
    raise HTTPException(
        410,
        "当前工作区仅支持邀请协作者；账户注册和账户管理不属于成员与权限功能",
    )


@router.post("/invitations", response_model=AuthMessage, status_code=status.HTTP_201_CREATED)
def invite_member(
    payload: OrganizationInvitationIn,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Invite either a new account or an existing account to this workspace."""
    principal, organization = _management_context(db)
    _require_assignable_role(principal, payload.role_key)
    email = auth_service.normalize_email(payload.email)
    role = permission_service.role_for_organization(db, organization.id, payload.role_key)

    existing_user = db.execute(
        select(User).where(User.email == email)
    ).scalars().first()
    if existing_user:
        if existing_user.id == principal.user_id:
            raise HTTPException(400, "不能邀请当前登录账户")
        if existing_user.status != "active":
            raise HTTPException(409, "该账户当前不可接受工作区邀请")
        member = db.execute(
            select(OrganizationMember)
            .options(
                selectinload(OrganizationMember.role),
                selectinload(OrganizationMember.user),
            )
            .where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == existing_user.id,
            )
            .with_for_update()
        ).scalars().first()
        if member and member.status == "active":
            raise HTTPException(409, "该账户已经是当前工作区成员")
        if member:
            _require_manageable_member(principal, member)
            member.role_id = role.id
        else:
            member = OrganizationMember(
                organization_id=organization.id,
                user_id=existing_user.id,
                role_id=role.id,
                invited_by_user_id=principal.user_id,
                status="invited",
            )
            db.add(member)
            db.flush()
        try:
            _issue_registered_workspace_invitation(
                db,
                organization=organization,
                principal=principal,
                member=member,
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - no partial invitation on delivery failure.
            db.rollback()
            raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
        db.commit()
        return AuthMessage(
            message="已向已注册成员发送 24 小时有效的邀请码",
            email=email,
        )

    user = User(
        tenant_id=organization.tenant_id,
        email=email,
        display_name=payload.display_name.strip() or email.split("@", 1)[0],
        password_hash=auth_service.hash_password(secrets.token_urlsafe(48)),
        status="pending",
    )
    db.add(user)
    db.flush()
    member = OrganizationMember(
        organization_id=organization.id,
        user_id=user.id,
        role_id=role.id,
        invited_by_user_id=principal.user_id,
        status="invited",
    )
    db.add(member)
    db.flush()
    now = datetime.now(timezone.utc)
    code = auth_service.issue_email_code(
        db, user, "invite", expires_minutes=24 * 60
    )
    db.add(
        OrganizationInvitation(
            organization_id=organization.id,
            member_id=member.id,
            user_id=user.id,
            invited_by_user_id=principal.user_id,
            status="pending",
            code_hash=auth_service.hash_workspace_invitation_code(code),
            expires_at=now + _WORKSPACE_INVITATION_LIFETIME,
        )
    )
    try:
        auth_service.send_verification_email(email, code, "invite")
    except Exception as exc:  # noqa: BLE001 - no account persists when delivery fails.
        db.rollback()
        raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
    db.commit()
    return AuthMessage(message="邀请验证码已发送，24 小时内有效", email=email)


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
    # A new invitee remains an independent platform account. The invitation
    # membership below is cross-workspace access rather than their home.
    _ensure_independent_home_workspace(
        db,
        user,
        shared_tenant_id=member.organization.tenant_id,
    )
    invitation = db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.member_id == member.id,
            OrganizationInvitation.status == "pending",
        )
        .order_by(OrganizationInvitation.created_at.desc())
        .with_for_update()
    ).scalars().first()
    if invitation:
        if _as_utc(invitation.expires_at) <= now:
            invitation.status = "expired"
            db.commit()
            raise HTTPException(400, "邀请码不正确或已失效")
        invitation.status = "accepted"
        invitation.accepted_at = now
    db.commit()
    return AuthMessage(message="已加入工作区，请登录", email=email)


@router.get("/invitations/inbox", response_model=list[OrganizationInvitationInboxOut])
def list_my_invitations(
    db: Session = Depends(get_tenant_db),
) -> list[OrganizationInvitationInboxOut]:
    """Expose active invitations to the already authenticated recipient."""
    principal = permission_service.require_principal(db)
    now = datetime.now(timezone.utc)
    invitations = db.execute(
        select(OrganizationInvitation)
        .options(
            selectinload(OrganizationInvitation.organization),
            selectinload(OrganizationInvitation.member).selectinload(
                OrganizationMember.role
            ),
            selectinload(OrganizationInvitation.invited_by_user),
        )
        .where(
            OrganizationInvitation.user_id == principal.user_id,
            OrganizationInvitation.status == "pending",
            OrganizationInvitation.expires_at > now,
        )
        .order_by(OrganizationInvitation.expires_at.asc())
    ).scalars().all()
    return [_invitation_inbox_out(invitation) for invitation in invitations]


@router.post("/invitations/{invitation_id}/accept", response_model=UserOut)
def accept_my_invitation(
    invitation_id: str,
    request: Request,
    db: Session = Depends(get_tenant_db),
) -> UserOut:
    """Accept a registered-user invitation without changing their account data."""
    principal = permission_service.require_principal(db)
    now = datetime.now(timezone.utc)
    invitation = db.execute(
        select(OrganizationInvitation)
        .options(selectinload(OrganizationInvitation.organization))
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.user_id == principal.user_id,
            OrganizationInvitation.status == "pending",
        )
        .with_for_update()
    ).scalars().first()
    if not invitation:
        raise HTTPException(404, "邀请不存在、已处理或已撤销")
    if _as_utc(invitation.expires_at) <= now:
        invitation.status = "expired"
        db.commit()
        raise HTTPException(409, "该邀请已过期，请联系邀请人重新发送")
    member = db.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.organization),
            selectinload(OrganizationMember.role),
        )
        .where(
            OrganizationMember.id == invitation.member_id,
            OrganizationMember.user_id == principal.user_id,
            OrganizationMember.organization_id == invitation.organization_id,
        )
        .with_for_update()
    ).scalars().first()
    if not member or not member.organization:
        raise HTTPException(409, "邀请关联的成员身份已失效")
    if member.status != "invited":
        raise HTTPException(409, "邀请对应的成员身份已失效")
    member.status = "active"
    invitation.status = "accepted"
    invitation.accepted_at = now
    auth_service.set_session_active_tenant(
        request,
        db,
        user_id=principal.user_id,
        tenant_id=member.organization.tenant_id,
    )
    db.commit()
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(401, "当前账户已失效")
    return auth_service.build_user_out(
        db, user, active_tenant_id=member.organization.tenant_id
    )


@router.post("/invitations/{invitation_id}/decline", response_model=AuthMessage)
def decline_my_invitation(
    invitation_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Decline a pending invite while leaving the recipient's account untouched."""
    principal = permission_service.require_principal(db)
    invitation = db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.user_id == principal.user_id,
            OrganizationInvitation.status == "pending",
        )
        .with_for_update()
    ).scalars().first()
    if not invitation:
        raise HTTPException(404, "邀请不存在、已处理或已撤销")
    invitation.status = "declined"
    invitation.revoked_at = datetime.now(timezone.utc)
    member = db.execute(
        select(OrganizationMember)
        .where(
            OrganizationMember.id == invitation.member_id,
            OrganizationMember.user_id == principal.user_id,
        )
        .with_for_update()
    ).scalars().first()
    if member and member.status == "invited":
        member.status = "removed"
    db.commit()
    return AuthMessage(message="已拒绝工作区邀请")


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


def _ownership_recipient_user_id(
    db: Session,
    *,
    organization: Organization,
    member: OrganizationMember,
    fallback_user_id: str,
) -> str:
    """Prefer the original inviter when they still have workspace access."""
    inviter_id = str(member.invited_by_user_id or "").strip()
    if not inviter_id:
        return fallback_user_id
    eligible = db.execute(
        select(OrganizationMember.id)
        .join(OrganizationMember.user)
        .where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == inviter_id,
            OrganizationMember.status == "active",
            User.status == "active",
        )
        .limit(1)
    ).scalar_one_or_none()
    return inviter_id if eligible else fallback_user_id


@router.post("/members/{member_id}/remove", response_model=AuthMessage)
def remove_member(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Remove a collaborator without disabling their account or home workspace."""
    principal, organization = _locked_management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    if member.user_id == principal.user_id:
        raise HTTPException(400, "不能移除当前登录账户")
    _require_manageable_member(principal, member)
    if member.status not in {"active", "invited"} or not member.user:
        raise HTTPException(409, "该成员已被移除或状态不可操作")
    _assert_owner_change_is_safe(
        db,
        organization_id=organization.id,
        member=member,
        disabling=True,
    )
    was_pending_invitation = member.status == "invited"
    if not was_pending_invitation and member.user.status == "active":
        _ensure_independent_home_workspace(
            db,
            member.user,
            shared_tenant_id=organization.tenant_id,
        )
    recipient_user_id = _ownership_recipient_user_id(
        db,
        organization=organization,
        member=member,
        fallback_user_id=principal.user_id,
    )
    transferred_count = permission_service.transfer_workspace_resource_ownership(
        db,
        tenant_id=organization.tenant_id,
        from_user_id=member.user_id,
        to_user_id=recipient_user_id,
    )
    now = datetime.now(timezone.utc)
    _revoke_pending_invitations(db, member_id=member.id, now=now)
    member.status = "removed"
    db.commit()
    if was_pending_invitation:
        return AuthMessage(
            message="工作区邀请已撤销，受邀账户不会被禁用或删除",
            email=member.user.email,
        )
    return AuthMessage(
        message=(
            "成员已移除，账户和原工作区数据未受影响；"
            f"已将 {transferred_count} 项共同工作区资源转交给邀请者"
        ),
        email=member.user.email,
    )


@router.post("/members/{member_id}/disable", response_model=OrganizationMemberOut)
def disable_member(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> OrganizationMemberOut:
    raise HTTPException(
        410,
        "成员与权限不支持禁用平台账户；请使用移出工作区操作",
    )


@router.post("/members/{member_id}/reset-password", response_model=AuthMessage)
def send_member_password_reset(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    raise HTTPException(
        410,
        "成员与权限不管理协作者密码；协作者可在自己的账户中完成密码管理",
    )


@router.post("/members/{member_id}/reinvite", response_model=AuthMessage)
def reinvite_member(
    member_id: str,
    db: Session = Depends(get_tenant_db),
) -> AuthMessage:
    """Reissue an invitation without resetting a registered user's account."""
    principal, organization = _management_context(db)
    member = _member_for_management(
        db, organization_id=organization.id, member_id=member_id, lock=True
    )
    _require_manageable_member(principal, member)
    if not member.user:
        raise HTTPException(409, "成员账户记录不完整")
    if member.status == "active":
        raise HTTPException(409, "已加入工作区的成员请使用移出工作区或角色管理操作")
    role_key = _role_key(member.role.key if member.role else "")
    _require_assignable_role(principal, role_key)
    if member.user.status == "active":
        try:
            _issue_registered_workspace_invitation(
                db,
                organization=organization,
                principal=principal,
                member=member,
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - delivery and state are atomic.
            db.rollback()
            raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
        db.commit()
        return AuthMessage(
            message="新的工作区邀请码已发送，24 小时内有效",
            email=member.user.email,
        )

    if member.user.status != "pending":
        raise HTTPException(409, "该账户当前不可接受新的工作区邀请")
    member.status = "invited"
    member.invited_by_user_id = principal.user_id
    db.execute(
        delete(EmailVerificationCode).where(EmailVerificationCode.user_id == member.user_id)
    )
    now = datetime.now(timezone.utc)
    code = auth_service.issue_email_code(
        db, member.user, "invite", expires_minutes=24 * 60
    )
    _revoke_pending_invitations(db, member_id=member.id, now=now)
    db.add(
        OrganizationInvitation(
            organization_id=organization.id,
            member_id=member.id,
            user_id=member.user_id,
            invited_by_user_id=principal.user_id,
            status="pending",
            code_hash=auth_service.hash_workspace_invitation_code(code),
            expires_at=now + _WORKSPACE_INVITATION_LIFETIME,
        )
    )
    try:
        auth_service.send_verification_email(member.user.email, code, "invite")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(503, "邀请邮件发送失败，请检查邮件服务后重试") from exc
    db.commit()
    return AuthMessage(message="新的邀请验证码已发送，24 小时内有效", email=member.user.email)
