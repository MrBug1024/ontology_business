"""Workspace identity and membership use cases; account role never grants access."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..access_models import WorkspaceInvitation
from ..access_schemas import MemberOut, MemberPage, WorkspaceOut
from ..models import Organization, OrganizationMember, OrganizationRole, Tenant, User
from . import permission_service
from .access_governance_service import audit, check_revision, lock_governance, protect_last_owner, revoke_credentials


def membership(db: Session, user_id: str, tenant_id: str) -> OrganizationMember | None:
    return db.execute(select(OrganizationMember).join(OrganizationMember.organization)
        .options(joinedload(OrganizationMember.role)).where(
            OrganizationMember.user_id == user_id, Organization.tenant_id == tenant_id,
            OrganizationMember.status == "active")).scalar_one_or_none()


def list_workspaces(db: Session, user: User) -> list[WorkspaceOut]:
    rows = db.execute(select(Tenant, OrganizationRole.key).join(Organization, Organization.tenant_id == Tenant.id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .join(OrganizationRole, OrganizationRole.id == OrganizationMember.role_id)
        .where(OrganizationMember.user_id == user.id, OrganizationMember.status == "active")
        .order_by(Tenant.created_at, Tenant.id).limit(100)).all()
    return [WorkspaceOut(tenant_id=tenant.id, name=tenant.name, role=role) for tenant, role in rows]


def require_manager(db: Session):
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "manage")
    return principal


def may_edit(actor_role: str, target_role: str, own: bool) -> bool:
    return not own and (actor_role == "owner" or (actor_role == "admin" and target_role in {"operator", "viewer"}))


def validate_role_grant(actor_role: str, role: str) -> None:
    if role not in permission_service.SYSTEM_ROLES:
        raise HTTPException(400, "不支持的工作区角色")
    if actor_role != "owner" and role not in {"operator", "viewer"}:
        raise HTTPException(403, "只有工作区所有者可以授予所有者或管理员角色")


def list_members(db: Session, offset: int, limit: int, *, search: str = "", user_ids: list[str] | None = None) -> MemberPage:
    principal = permission_service.require_principal(db)
    query = select(OrganizationMember).where(OrganizationMember.organization_id == principal.organization_id)
    if user_ids:
        query = query.where(OrganizationMember.user_id.in_(user_ids))
    if search.strip():
        term = search.strip().lower()
        query = query.join(OrganizationMember.user).where(or_(
            func.lower(User.display_name).contains(term, autoescape=True),
            func.lower(User.email).contains(term, autoescape=True),
        ))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.options(joinedload(OrganizationMember.user), joinedload(OrganizationMember.role))
        .order_by(OrganizationMember.created_at, OrganizationMember.id).offset(offset).limit(limit)).all()
    return MemberPage(items=[MemberOut(id=row.id, user_id=row.user_id,
        email=row.user.email, display_name=row.user.display_name, role=row.role.key,
        status=row.status, account_status=row.user.status, email_verified=bool(row.user.email_verified_at),
        revision=row.revision, created_at=row.created_at,
        can_edit=may_edit(principal.role_key, row.role.key, row.user_id == principal.user_id)) for row in rows],
        total=total, can_manage=principal.privileged, role=principal.role_key)


def update_member(db: Session, member_id: str, expected_revision: int, role_key: str | None) -> None:
    lock_governance(db)
    principal = require_manager(db)
    member = db.scalar(select(OrganizationMember).where(OrganizationMember.id == member_id,
        OrganizationMember.organization_id == principal.organization_id))
    if not member:
        raise HTTPException(404, "成员不存在")
    check_revision(member.revision, expected_revision)
    role = db.get(OrganizationRole, member.role_id)
    if not role or not may_edit(principal.role_key, role.key, member.user_id == principal.user_id):
        raise HTTPException(403, "不能修改该成员，请由其他所有者操作")
    if member.status != "active":
        raise HTTPException(409, "该成员已移出，请重新邀请")
    if role_key:
        validate_role_grant(principal.role_key, role_key)
        if role_key == role.key:
            return
    if role_key != "owner":
        protect_last_owner(db, member)
    before = role.key
    if role_key:
        member.role_id = permission_service.role_for_organization(db, principal.organization_id, role_key).id
    else:
        member.status = "removed"
        user = db.get(User, member.user_id)
        invitation = db.scalar(select(WorkspaceInvitation).where(
            WorkspaceInvitation.organization_id == principal.organization_id,
            WorkspaceInvitation.email == user.email))
        if invitation:
            invitation.status = "revoked"
            invitation.revision += 1
            invitation.delivery_generation += 1
            invitation.delivery_status = "cancelled"
    member.revision += 1
    revoke_credentials(db, member.user_id, principal.tenant_id)
    audit(db, actor_id=principal.user_id, target_id=member.user_id, tenant_id=principal.tenant_id,
          action="member.role_changed" if role_key else "member.removed", before=before,
          after=role_key or "removed")
    db.flush()
