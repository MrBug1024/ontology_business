"""Observable membership, invitation, role separation and auth regressions."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import secrets

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.access_models import AccessAuditEvent, AccessGovernanceGuard, AuthRateLimit, WorkspaceInvitation, now
from app.access_schemas import AccountUpdateIn, InviteIn
from app.config import get_settings
from app.database import Base, get_db
from app.external_api_models import ExternalApiKey
from app.models import AuthSession, EmailVerificationCode, OrganizationMember, User
from app.routers import auth, workspace_access, system_accounts
from app.schemas import RegisterIn, VerifyEmailIn
from app.services import auth_service, permission_service, system_account_service, workspace_service
from app.services import workspace_invitation_service as invitations
from app.services import invitation_delivery_service as delivery
from app.services.auth_request_security import CookieOriginMiddleware, consume_limit
from .access_fixtures import seed_access


@pytest.fixture
def access_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(get_settings(), "public_app_url", "http://testserver")
    monkeypatch.setattr(get_settings(), "bootstrap_superadmin_email", "")
    with factory() as db:
        fixture = seed_access(db)
        yield db, fixture, factory
    engine.dispose()


def invite(db, fixture, role="operator"):
    return invitations.create_invitation(db, InviteIn(email=fixture.recipient.email, role=role))


def test_invitation_acceptance_keeps_personal_workspace_and_uses_live_membership(access_db):
    db, f, _ = access_db
    invitation = invite(db, f)
    db.commit()
    assert workspace_service.membership(db, f.recipient.id, f.tenant.id) is None
    invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    db.commit()
    assert f.recipient.tenant_id == f.recipient_tenant.id
    assert len(workspace_service.list_workspaces(db, f.recipient)) == 2
    db.info.update(user_id=f.recipient.id, tenant_id=f.tenant.id)
    assert permission_service.require_principal(db).role_key == "operator"
    assert not permission_service.check_tenant_permission(db, "manage").allowed


def test_wrong_mailbox_expiry_and_stale_revision_are_rejected(access_db):
    db, f, _ = access_db
    invitation = invite(db, f)
    with pytest.raises(HTTPException) as error:
        invitations.respond_invitation(db, f.stranger.id, invitation.id, invitation.revision, True)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision + 1, True)
    assert error.value.status_code == 409
    db.get(WorkspaceInvitation, invitation.id).expires_at = now() - timedelta(seconds=1)
    db.flush()
    with pytest.raises(HTTPException) as error:
        invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    assert error.value.status_code == 409


def test_accept_retry_is_idempotent_but_removed_member_cannot_replay_old_invitation(access_db):
    db, f, _ = access_db
    invitation = invite(db, f)
    invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    assert db.scalar(select(func.count()).select_from(OrganizationMember).where(
        OrganizationMember.organization_id == f.organization.id, OrganizationMember.user_id == f.recipient.id)) == 1
    member = workspace_service.membership(db, f.recipient.id, f.tenant.id)
    workspace_service.update_member(db, member.id, member.revision, None)
    db.commit()
    with pytest.raises(HTTPException) as error:
        invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    assert error.value.status_code == 409
    assert workspace_service.membership(db, f.recipient.id, f.tenant.id) is None


def test_system_superadmin_does_not_grant_foreign_workspace_access(access_db):
    db, f, _ = access_db
    db.info.update(tenant_id=f.recipient_tenant.id)
    with pytest.raises(HTTPException) as error:
        workspace_service.list_members(db, 0, 20)
    assert error.value.status_code == 403
    assert f.owner.system_role == "superadmin"


def test_workspace_owner_cannot_manage_system_accounts(access_db):
    db, f, _ = access_db
    with pytest.raises(HTTPException) as error:
        system_account_service.list_accounts(db, f.recipient.id, "", "", 0, 20)
    assert error.value.status_code == 403


def test_admin_cannot_grant_owner_and_inviter_removal_blocks_acceptance(access_db):
    db, f, _ = access_db
    invitation = invite(db, f, "admin")
    invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    db.commit()
    db.info.update(user_id=f.recipient.id)
    with pytest.raises(HTTPException) as error:
        invitations.create_invitation(db, InviteIn(email=f.stranger.email, role="owner"))
    assert error.value.status_code == 403
    other = invitations.create_invitation(db, InviteIn(email=f.stranger.email, role="viewer"))
    db.info.update(user_id=f.owner.id)
    member = workspace_service.membership(db, f.recipient.id, f.tenant.id)
    workspace_service.update_member(db, member.id, member.revision, None)
    with pytest.raises(HTTPException) as error:
        invitations.respond_invitation(db, f.stranger.id, other.id, other.revision, True)
    assert error.value.status_code == 409


def test_disabling_account_revokes_credentials_without_changing_workspace_roles(access_db):
    db, f, _ = access_db
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(user_id=f.recipient.id, token_hash=auth_service._token_hash(token), expires_at=now() + timedelta(days=1)))
    db.add(ExternalApiKey(user_id=f.recipient.id, tenant_id=f.recipient_tenant.id,
        name="synthetic", key_prefix="test", token_hint="test", token_hash=secrets.token_hex(32),
        scopes=["scenarios:read"], status="active", expires_at=now() + timedelta(days=1)))
    db.flush()
    system_account_service.update_account(db, f.owner.id, f.recipient.id, AccountUpdateIn(
        expected_revision=1, system_role="user", status="disabled", reason="test suspension"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == f.recipient.id)) == 0
    assert db.scalar(select(ExternalApiKey.status).where(ExternalApiKey.user_id == f.recipient.id)) == "revoked"
    assert workspace_service.membership(db, f.recipient.id, f.recipient_tenant.id).role.key == "owner"
    assert db.scalar(select(AccessAuditEvent.reason).where(AccessAuditEvent.target_id == f.recipient.id)) == "test suspension"


def test_disabled_account_cannot_be_reactivated_by_registration_or_old_code(access_db):
    db, f, _ = access_db
    f.recipient.status = "disabled"
    code = "123456"
    db.add(EmailVerificationCode(user_id=f.recipient.id, email=f.recipient.email,
        purpose="register", code_hash=auth_service._hash_code(code), expires_at=now() + timedelta(minutes=10)))
    db.commit()
    with pytest.raises(HTTPException) as error:
        auth.register(RegisterIn(email=f.recipient.email, password="test-password", password_confirm="test-password"), db)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        auth.verify_email(VerifyEmailIn(email=f.recipient.email, code=code), db)
    assert error.value.status_code == 400
    assert db.get(User, f.recipient.id).status == "disabled"


def test_last_superadmin_and_stale_account_writes_are_rejected(access_db):
    db, f, _ = access_db
    with pytest.raises(HTTPException) as error:
        system_account_service.update_account(db, f.owner.id, f.owner.id,
            AccountUpdateIn(expected_revision=1, system_role="user", status="active", reason="test"))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        system_account_service.update_account(db, f.owner.id, f.recipient.id,
            AccountUpdateIn(expected_revision=99, system_role="user", status="disabled", reason="test"))
    assert error.value.status_code == 409


def test_bootstrap_is_explicit_verified_and_does_not_repromote(access_db, monkeypatch):
    db, f, _ = access_db
    f.owner.system_role = "user"
    guard = db.get(AccessGovernanceGuard, 1)
    guard.bootstrap_completed = False
    db.commit()
    system_account_service.bootstrap_superadmin(db)
    assert f.owner.system_role == "user"
    monkeypatch.setattr(get_settings(), "bootstrap_superadmin_email", f.owner.email)
    system_account_service.bootstrap_superadmin(db)
    db.commit()
    assert f.owner.system_role == "superadmin"
    f.owner.system_role = "user"
    db.commit()
    system_account_service.bootstrap_superadmin(db)
    assert f.owner.system_role == "user"


def test_invitation_mail_contains_configured_link_and_escapes_workspace(access_db):
    message = delivery.invitation_message("synthetic@example.test", '<img src=x onerror="evil">')
    assert "http://testserver/invitations" in message.get_body(preferencelist=("plain",)).get_content()
    body = message.get_body(preferencelist=("html",)).get_content()
    assert "&lt;img" in body and '<img src=x' not in body
    assert "token=" not in body and "code=" not in body


def test_delivery_crash_is_indeterminate_and_not_replayed(access_db, monkeypatch):
    db, f, factory = access_db
    invitation = invite(db, f)
    db.commit()
    monkeypatch.setattr(delivery, "SessionLocal", factory)
    sender = Mock(side_effect=OSError("synthetic uncertain delivery"))
    monkeypatch.setattr(delivery, "send_mail_message", sender)
    assert delivery.deliver_one()
    assert not delivery.deliver_one()
    db.expire_all()
    assert db.get(WorkspaceInvitation, invitation.id).delivery_status == "indeterminate"
    assert sender.call_count == 1


def test_auth_rate_limit_persists_attempts_between_sessions(access_db):
    db, _, factory = access_db
    consume_limit(db, "synthetic:login", maximum=1, seconds=60)
    with factory() as other:
        with pytest.raises(HTTPException) as error:
            consume_limit(other, "synthetic:login", maximum=1, seconds=60)
        assert error.value.status_code == 429
    assert db.scalar(select(AuthRateLimit.attempts)) == 2


def test_switch_rotates_session_and_csrf_rejects_cross_origin_writes(access_db, monkeypatch):
    db, f, factory = access_db
    invitation = invite(db, f)
    invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
    raw = secrets.token_urlsafe(32)
    session = AuthSession(user_id=f.recipient.id, token_hash=auth_service._token_hash(raw), expires_at=now() + timedelta(days=1))
    db.add(session); db.commit()
    old_session_id = session.id
    app = FastAPI()
    app.add_middleware(CookieOriginMiddleware)
    app.include_router(workspace_access.router)
    app.include_router(system_accounts.router)
    def request_db():
        with factory() as request_session:
            yield request_session
    app.dependency_overrides[get_db] = request_db
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    with TestClient(app) as client:
        client.cookies.set(get_settings().auth_cookie_name, raw)
        rejected = client.post("/workspaces/switch", json={"tenant_id": f.tenant.id}, headers={"Origin": "https://foreign.example"})
        assert rejected.status_code == 403
        accepted = client.post("/workspaces/switch", json={"tenant_id": f.tenant.id}, headers={"Origin": "http://testserver"})
        assert accepted.status_code == 200
        assert client.get("/system/accounts").status_code == 403
    db.expire_all()
    assert db.get(AuthSession, old_session_id) is None
    current = db.scalar(select(AuthSession).where(AuthSession.user_id == f.recipient.id))
    assert current.active_tenant_id == f.tenant.id
    assert current.token_hash != auth_service._token_hash(raw)
    assert db.get(User, f.recipient.id).tenant_id == f.recipient_tenant.id


def test_invalid_auth_input_never_echoes_password_or_verification_code(access_db):
    _, _, factory = access_db
    app = FastAPI()
    app.include_router(auth.router)
    def request_db():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = request_db
    with TestClient(app) as client:
        response = client.post('/auth/register', json={'email': 'synthetic@example.test',
            'password': 'short', 'password_confirm': 'short'})
        assert response.status_code == 422
        assert 'short' not in response.text


def test_restoring_suspended_owner_does_not_restore_old_keys_or_change_roles(access_db):
    db, f, _ = access_db
    system_account_service.update_account(db, f.owner.id, f.recipient.id,
        AccountUpdateIn(expected_revision=1, system_role="user", status="disabled", reason="suspend"))
    db.commit()
    system_account_service.update_account(db, f.owner.id, f.recipient.id,
        AccountUpdateIn(expected_revision=2, system_role="user", status="active", reason="restore"))
    db.commit()
    assert db.get(User, f.recipient.id).status == "active"
    assert db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == f.recipient.id)) == 0
    assert workspace_service.membership(db, f.recipient.id, f.recipient_tenant.id).role.key == "owner"
