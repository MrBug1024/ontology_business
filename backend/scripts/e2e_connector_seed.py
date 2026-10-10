"""Local-dev E2E seed: one verified test user + session in the LLM-configured tenant.

Prints the session token for API-level end-to-end runs. Dev database only;
the account is clearly marked and can be deleted by email prefix.
"""
from __future__ import annotations

import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuthSession, OrganizationMember, User
from app.services.auth_service import _token_hash, hash_password

EMAIL = "e2e-connector@local.dev"
TENANT_WITH_LLM = "7a776f403b86443194c12f2be4aafd04"
ORGANIZATION_ID = "f8a97b001bf44bbaab2e305f34cb325e"
ADMIN_ROLE_ID = "4cbcfb68831a4fdab6a357b9066ddcdd"


def main() -> int:
    password = secrets.token_urlsafe(18)
    token = secrets.token_urlsafe(48)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        if user is None:
            user = User(email=EMAIL, tenant_id=TENANT_WITH_LLM, display_name="E2E连接器测试",
                        password_hash=hash_password(password), status="active",
                        email_verified_at=datetime.now(timezone.utc))
            db.add(user)
            db.flush()
        else:
            user.status = "active"
            user.email_verified_at = user.email_verified_at or datetime.now(timezone.utc)
        member = db.scalar(select(OrganizationMember).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.organization_id == ORGANIZATION_ID))
        if member is None:
            db.add(OrganizationMember(organization_id=ORGANIZATION_ID, user_id=user.id,
                                      role_id=ADMIN_ROLE_ID, status="active"))
        else:
            member.status = "active"
            member.role_id = ADMIN_ROLE_ID
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
        db.add(AuthSession(user_id=user.id, active_tenant_id=TENANT_WITH_LLM,
                           token_hash=_token_hash(token),
                           expires_at=datetime.now(timezone.utc) + timedelta(hours=12)))
        db.commit()
        print(f"EMAIL={EMAIL}")
        print(f"PASSWORD={password}")
        print(f"SESSION_TOKEN={token}")
        print(f"USER_ID={user.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
