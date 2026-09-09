"""System account governance, independent of every workspace role."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..access_schemas import AccountOut, AccountPage, AccountUpdateIn
from ..config import get_settings
from ..models import OrganizationMember, User
from .access_governance_service import audit, check_revision, lock_governance, revoke_credentials


def require_superadmin(db: Session, user_id: str) -> User:
    user = db.get(User, user_id, populate_existing=True)
    if not user or user.status != "active" or user.system_role != "superadmin":
        raise HTTPException(403, "只有系统超级管理员可以管理账户")
    return user


def bootstrap_superadmin(db: Session) -> None:
    email = get_settings().bootstrap_superadmin_email.strip().lower()
    if not email:
        return
    guard = lock_governance(db)
    if guard.bootstrap_completed:
        return
    user = db.scalar(select(User).where(User.email == email, User.status == "active", User.email_verified_at.is_not(None)))
    if not user:
        return
    if db.scalar(select(User.id).where(User.system_role == "superadmin").limit(1)):
        guard.bootstrap_completed = True
        return
    user.system_role = "superadmin"
    user.revision += 1
    guard.bootstrap_completed = True
    revoke_credentials(db, user.id)
    audit(db, actor_id=user.id, target_id=user.id, action="account.bootstrap", after="superadmin",
          reason="部署者显式配置的首位系统管理员")
    db.flush()


def list_accounts(db: Session, actor_id: str, search: str, status: str, offset: int, limit: int) -> AccountPage:
    require_superadmin(db, actor_id)
    query = select(User)
    if search:
        term = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(User.email.ilike(f"%{term}%", escape="\\"), User.display_name.ilike(f"%{term}%", escape="\\")))
    if status:
        query = query.where(User.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    users = db.scalars(query.order_by(User.created_at.desc(), User.id).offset(offset).limit(limit)).all()
    counts = dict(db.execute(select(OrganizationMember.user_id, func.count())
        .where(OrganizationMember.user_id.in_([u.id for u in users]), OrganizationMember.status == "active")
        .group_by(OrganizationMember.user_id)).all()) if users else {}
    return AccountPage(total=total, items=[AccountOut(id=u.id, email=u.email, display_name=u.display_name,
        system_role=u.system_role, status=u.status, email_verified=bool(u.email_verified_at), revision=u.revision,
        created_at=u.created_at, last_login_at=u.last_login_at, workspace_count=counts.get(u.id, 0)) for u in users])


def update_account(db: Session, actor_id: str, target_id: str, payload: AccountUpdateIn) -> None:
    lock_governance(db)
    require_superadmin(db, actor_id)
    user = db.get(User, target_id)
    if not user:
        raise HTTPException(404, "账户不存在")
    check_revision(user.revision, payload.expected_revision)
    if not payload.reason.strip():
        raise HTTPException(400, "请填写变更原因")
    if payload.status == "active" and not user.email_verified_at:
        raise HTTPException(409, "账户须先完成邮箱验证，可恢复为待验证状态")
    if payload.system_role == "superadmin" and user.system_role != "superadmin" and (payload.status != "active" or not user.email_verified_at):
        raise HTTPException(409, "只有已验证且正常的账户可担任超级管理员")
    if payload.status == "pending" and user.email_verified_at:
        raise HTTPException(409, "已验证账户只能恢复为正常状态")
    if user.system_role == "superadmin" and (payload.system_role != "superadmin" or payload.status != "active"):
        other = db.scalar(select(User.id).where(User.id != user.id, User.status == "active",
            User.system_role == "superadmin").limit(1))
        if not other:
            raise HTTPException(409, "系统必须保留至少一位有效超级管理员")
    # System suspension applies even to personal-workspace owners. Memberships
    # and data remain intact; every protocol rejects the disabled principal.
    before = f"{user.system_role}/{user.status}"
    after = f"{payload.system_role}/{payload.status}"
    if before == after:
        return
    user.system_role = payload.system_role
    user.status = payload.status
    user.revision += 1
    revoke_credentials(db, user.id)
    audit(db, actor_id=actor_id, target_id=user.id, action="account.updated",
          before=before, after=after, reason=payload.reason.strip())
    db.flush()
