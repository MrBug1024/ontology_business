"""Synthetic identities shared by access behavior and isolated PostgreSQL tests."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session

from app.access_models import AccessGovernanceGuard, now
from app.models import Tenant, User
from app.services import auth_service, permission_service


def seed_access(db: Session) -> SimpleNamespace:
    suffix = uuid4().hex[:10]
    tenants = [Tenant(name=f"Workspace {index} {suffix}") for index in range(3)]
    db.add_all(tenants)
    db.flush()
    users = [User(tenant_id=tenant.id, email=f"access-{index}-{suffix}@example.test",
        display_name=f"Account {index}", password_hash=auth_service.hash_password(uuid4().hex),
        status="active", email_verified_at=now(), system_role="superadmin" if index == 0 else "user")
        for index, tenant in enumerate(tenants)]
    db.add_all(users)
    db.flush()
    organizations = [permission_service.ensure_organization(db, t.id, owner_user_id=u.id) for t, u in zip(tenants, users)]
    if db.get(AccessGovernanceGuard, 1) is None:
        db.add(AccessGovernanceGuard(id=1, bootstrap_completed=True))
    db.commit()
    db.info.update(user_id=users[0].id, tenant_id=tenants[0].id)
    return SimpleNamespace(owner=users[0], recipient=users[1], stranger=users[2],
        tenant=tenants[0], recipient_tenant=tenants[1], organization=organizations[0])
