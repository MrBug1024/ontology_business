"""Persistent serialization, revocation and audit for access changes."""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from fastapi import HTTPException
import secrets

from ..access_models import AccessAuditEvent, AccessGovernanceGuard, WorkspaceInvitation, now
from ..external_api_models import AgentMCPService, ExternalApiKey
from ..models import AuthSession, OrganizationMember, OrganizationRole, User
from . import permission_service


def lock_governance(db: Session) -> AccessGovernanceGuard:
    # One short, durable critical section closes last-owner/admin and revoke/accept
    # races across all API instances. No external I/O runs under this row lock.
    guard = db.execute(select(AccessGovernanceGuard).where(
        AccessGovernanceGuard.id == 1).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if guard is None:
        raise HTTPException(503, "权限治理尚未初始化，请先完成数据库迁移")
    permission_service.refresh_request_authorization(db)
    for instance in list(db.identity_map.values()):
        if isinstance(instance, WorkspaceInvitation):
            db.expire(instance)
    return guard


def audit(db: Session, *, actor_id: str, target_id: str, action: str,
          tenant_id: str | None = None, before: str = "", after: str = "", reason: str = "") -> None:
    db.add(AccessAuditEvent(actor_user_id=actor_id, target_id=target_id,
        tenant_id=tenant_id, action=action, before_value=before,
        after_value=after, reason=reason))


def check_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise HTTPException(409, "信息已被更新，请刷新后重试")


def revoke_credentials(db: Session, user_id: str, tenant_id: str | None = None) -> None:
    # Rotate every browser session after a security-sensitive change. Integration
    # keys are workspace-scoped; disabled accounts invalidate all their keys.
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    keys = update(ExternalApiKey).where(ExternalApiKey.user_id == user_id, ExternalApiKey.status == "active")
    if tenant_id:
        keys = keys.where(ExternalApiKey.tenant_id == tenant_id)
    db.execute(keys.values(status="revoked", revoked_at=now()))
    publications = select(AgentMCPService).where(AgentMCPService.execution_user_id == user_id)
    if tenant_id:
        publications = publications.where(AgentMCPService.tenant_id == tenant_id)
    # Invalidate the old MCP token even if a publication is enabled again later.
    for publication in db.scalars(publications).yield_per(100):
        publication.enabled = False
        publication.token_hash = secrets.token_hex(32)
        publication.token_hint = ""


def protect_last_owner(db: Session, member: OrganizationMember) -> None:
    role = db.get(OrganizationRole, member.role_id)
    user = db.get(User, member.user_id)
    if member.status != "active" or not user or user.status != "active" or not role or role.key != "owner":
        return
    other = db.execute(select(OrganizationMember.id).join(OrganizationMember.role)
        .join(OrganizationMember.user).where(
            OrganizationMember.organization_id == member.organization_id,
            OrganizationMember.id != member.id, OrganizationMember.status == "active",
            OrganizationRole.key == "owner", User.status == "active").limit(1)).first()
    if not other:
        raise HTTPException(409, "工作区必须保留至少一位有效所有者，请先移交所有权")
