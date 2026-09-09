"""Mailbox-bound invitations with explicit acceptance and durable delivery intent."""
from __future__ import annotations

from datetime import timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..access_models import WorkspaceInvitation, now
from ..access_schemas import InvitationOut, InvitationPage, InviteIn
from ..config import get_settings
from ..models import Organization, OrganizationMember, OrganizationRole, User
from . import auth_service, permission_service, workspace_service
from .access_governance_service import audit, check_revision, lock_governance


def is_expired(invitation: WorkspaceInvitation) -> bool:
    expiry = invitation.expires_at
    return expiry.replace(tzinfo=timezone.utc) <= now() if expiry.tzinfo is None else expiry <= now()


def invitation_out(row: WorkspaceInvitation, workspace_name: str, role: str) -> InvitationOut:
    return InvitationOut(id=row.id, workspace_name=workspace_name, email=row.email,
        display_name=row.display_name, role=role, status="expired" if row.status == "pending" and is_expired(row) else row.status,
        delivery_status=row.delivery_status, expires_at=row.expires_at, revision=row.revision)


def list_invitations(db: Session, user: User, *, inbox: bool, offset: int, limit: int) -> InvitationPage:
    query = select(WorkspaceInvitation, Organization.name, OrganizationRole.key).join(
        Organization, Organization.id == WorkspaceInvitation.organization_id).join(
        OrganizationRole, OrganizationRole.id == WorkspaceInvitation.role_id)
    if inbox:
        query = query.where(WorkspaceInvitation.email == user.email)
    else:
        principal = workspace_service.require_manager(db)
        query = query.where(WorkspaceInvitation.organization_id == principal.organization_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(WorkspaceInvitation.updated_at.desc(), WorkspaceInvitation.id)
        .offset(offset).limit(limit)).all()
    return InvitationPage(total=total, items=[invitation_out(row, name, role) for row, name, role in rows])


def create_invitation(db: Session, payload: InviteIn) -> InvitationOut:
    email = auth_service.normalize_email(payload.email)
    if not get_settings().public_app_url:
        raise HTTPException(503, "请先配置平台访问地址 PUBLIC_APP_URL 后再发送邀请")
    lock_governance(db)
    principal = workspace_service.require_manager(db)
    workspace_service.validate_role_grant(principal.role_key, payload.role)
    target = db.scalar(select(User).where(User.email == email))
    if target:
        member = workspace_service.membership(db, target.id, principal.tenant_id)
        if member:
            raise HTTPException(409, "该邮箱已是当前工作区成员")
    invitation = db.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.organization_id == principal.organization_id, WorkspaceInvitation.email == email))
    if invitation:
        raise HTTPException(409, "该邮箱已有邀请记录，请在邀请列表中重新发送")
    role = permission_service.role_for_organization(db, principal.organization_id, payload.role)
    invitation = WorkspaceInvitation(organization_id=principal.organization_id, role_id=role.id,
        email=email, display_name=payload.display_name.strip(), invited_by_user_id=principal.user_id,
        expires_at=now() + timedelta(hours=24))
    db.add(invitation)
    db.flush()
    audit(db, actor_id=principal.user_id, target_id=invitation.id, tenant_id=principal.tenant_id,
        action="invitation.created", after=payload.role)
    organization = db.get(Organization, principal.organization_id)
    return invitation_out(invitation, organization.name, role.key)


def manage_invitation(db: Session, invitation_id: str, expected_revision: int, resend: bool) -> None:
    lock_governance(db)
    principal = workspace_service.require_manager(db)
    invitation = db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation_id,
        WorkspaceInvitation.organization_id == principal.organization_id))
    if not invitation:
        raise HTTPException(404, "邀请不存在")
    check_revision(invitation.revision, expected_revision)
    role = db.get(OrganizationRole, invitation.role_id)
    workspace_service.validate_role_grant(principal.role_key, role.key)
    if resend:
        if not get_settings().public_app_url:
            raise HTTPException(503, "请先配置平台访问地址")
        target = db.scalar(select(User).where(User.email == invitation.email))
        if target and workspace_service.membership(db, target.id, principal.tenant_id):
            raise HTTPException(409, "该邮箱已是当前工作区成员")
        if invitation.delivery_status in {"queued", "sending"}:
            raise HTTPException(409, "邮件正在投递，请稍后刷新")
        updated = invitation.updated_at
        if (now() - updated.replace(tzinfo=timezone.utc)).total_seconds() < 60:
            raise HTTPException(429, "邀请发送过于频繁，请一分钟后重试")
        invitation.expires_at = now() + timedelta(hours=24)
        invitation.responded_at = None
        invitation.invited_by_user_id = principal.user_id
    elif invitation.status != "pending":
        raise HTTPException(409, "只能撤销待处理的邀请")
    before = invitation.status
    invitation.status = "pending" if resend else "revoked"
    invitation.delivery_status = "queued" if resend else "cancelled"
    invitation.delivery_generation += 1
    invitation.delivery_lease = None
    invitation.delivery_lease_expires_at = None
    invitation.revision += 1
    audit(db, actor_id=principal.user_id, target_id=invitation.id, tenant_id=principal.tenant_id,
        action="invitation.resent" if resend else "invitation.revoked", before=before, after=invitation.status)
    db.flush()


def respond_invitation(db: Session, user_id: str, invitation_id: str, expected_revision: int, accept: bool) -> None:
    lock_governance(db)
    user = db.get(User, user_id, populate_existing=True)
    if not user or user.status != "active" or not user.email_verified_at:
        raise HTTPException(403, "请先登录并完成邮箱验证")
    invitation = db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation_id,
        WorkspaceInvitation.email == user.email))
    if not invitation:
        raise HTTPException(404, "邀请不存在")
    # A retried acknowledgement is safe; a revoked/renewed invitation is not.
    resulting_status = "accepted" if accept else "declined"
    if invitation.status == resulting_status and invitation.revision == expected_revision + 1:
        return
    check_revision(invitation.revision, expected_revision)
    if invitation.status != "pending" or is_expired(invitation):
        raise HTTPException(409, "邀请已失效，请联系工作区管理员重新邀请")
    organization = db.get(Organization, invitation.organization_id)
    if accept:
        inviter = db.get(User, invitation.invited_by_user_id)
        inviter_member = workspace_service.membership(db, invitation.invited_by_user_id, organization.tenant_id)
        role = db.get(OrganizationRole, invitation.role_id)
        if not inviter or inviter.status != "active" or not inviter_member or inviter_member.role.key not in {"owner", "admin"}:
            raise HTTPException(409, "邀请人已无管理权限，请联系工作区管理员重新邀请")
        workspace_service.validate_role_grant(inviter_member.role.key, role.key)
        member = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == user.id))
        if member and member.status == "active":
            raise HTTPException(409, "你已是该工作区成员")
        if member:
            member.role_id = invitation.role_id
            member.status = "active"
            member.revision += 1
        else:
            db.add(OrganizationMember(organization_id=organization.id, user_id=user.id,
                role_id=invitation.role_id, status="active"))
    invitation.status = resulting_status
    invitation.responded_at = now()
    invitation.revision += 1
    audit(db, actor_id=user.id, target_id=invitation.id, tenant_id=organization.tenant_id,
        action=f"invitation.{resulting_status}", after=resulting_status)
    db.flush()
