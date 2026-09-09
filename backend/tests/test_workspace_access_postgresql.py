"""Opt-in real PostgreSQL acceptance in a database created by this suite."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.access_models import WorkspaceInvitation, now
from app.access_schemas import AccountUpdateIn, InviteIn
from app.config import get_settings
from app.models import DataSource, ManagedUploadRun, OrganizationMember, OrganizationRole, User
from app.services import invitation_delivery_service as delivery
from app.services import permission_service, system_account_service, workspace_service
from app.services import workspace_invitation_service as invitations
from .access_fixtures import seed_access
from .access_postgresql import isolated_access_database

pytestmark = pytest.mark.skipif(os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1", reason="explicit PostgreSQL opt-in required")


@pytest.fixture(scope="module")
def pg_factory():
    with isolated_access_database() as (url, admin):
        runtime = create_engine(url)
        yield sessionmaker(bind=runtime, expire_on_commit=False, autoflush=False), admin
        runtime.dispose()


def test_two_admins_cannot_concurrently_demote_each_other(pg_factory):
    factory, _ = pg_factory
    with factory() as db:
        f = seed_access(db)
        f.recipient.system_role = "superadmin"
        db.commit()
        actors = [f.owner.id, f.recipient.id]
    barrier = Barrier(2)
    def demote(actor, target):
        with factory() as db:
            db.get(User, target)  # Intentionally preload a potentially stale row.
            barrier.wait(timeout=10)
            try:
                system_account_service.update_account(db, actor, target,
                    AccountUpdateIn(expected_revision=1, system_role="user", status="active", reason="concurrency fixture"))
                db.commit()
                return 200
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(demote, actors[0], actors[1]), pool.submit(demote, actors[1], actors[0])]
        outcomes = [f.result(timeout=30) for f in futures]
    assert sorted(outcomes) == [200, 403]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.id.in_(actors), User.system_role == "superadmin", User.status == "active")) == 1


def test_double_accept_creates_one_membership_and_revoke_prevents_replay(pg_factory, monkeypatch):
    factory, _ = pg_factory
    monkeypatch.setattr(get_settings(), "public_app_url", "http://localhost:5173")
    with factory() as db:
        f = seed_access(db)
        invitation = invitations.create_invitation(db, InviteIn(email=f.recipient.email))
        db.commit()
        actor, recipient, tenant = f.owner.id, f.recipient.id, f.tenant.id
    barrier = Barrier(2)
    def accept():
        with factory() as db:
            db.get(WorkspaceInvitation, invitation.id)
            barrier.wait(timeout=10)
            invitations.respond_invitation(db, recipient, invitation.id, invitation.revision, True)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept), pool.submit(accept)]
        for future in futures:
            future.result(timeout=30)
    with factory() as db:
        db.info.update(user_id=actor, tenant_id=tenant)
        member = workspace_service.membership(db, recipient, tenant)
        assert member is not None
        workspace_service.update_member(db, member.id, member.revision, None)
        db.commit()
        with pytest.raises(HTTPException) as error:
            invitations.respond_invitation(db, recipient, invitation.id, invitation.revision, True)
        assert error.value.status_code == 409


def test_invited_account_can_persist_run_in_workspace_and_foreign_resource_is_rejected(pg_factory, monkeypatch):
    factory, _ = pg_factory
    monkeypatch.setattr(get_settings(), "public_app_url", "http://localhost:5173")
    with factory() as db:
        f = seed_access(db)
        invitation = invitations.create_invitation(db, InviteIn(email=f.recipient.email))
        invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
        db.commit()
        db.info.update(user_id=f.recipient.id, tenant_id=f.tenant.id)
        permission_service.refresh_request_authorization(db)
        assert permission_service.require_principal(db).role_key == "operator"
        sources = [DataSource(tenant_id=t, name="Synthetic source", type="file_bucket") for t in [f.tenant.id, f.recipient_tenant.id]]
        db.add_all(sources); db.flush()
        def run(source_id):
            return ManagedUploadRun(tenant_id=f.tenant.id, requested_by_user_id=f.recipient.id,
                data_source_id=source_id, idempotency_key=uuid4().hex, request_fingerprint="a" * 64,
                purpose="invocation_attachment", filename="fixture.csv", declared_byte_size=10,
                expires_at=now() + timedelta(hours=1))
        valid = run(sources[0].id)
        db.add(valid); db.flush()
        assert valid.id and f.recipient.tenant_id != valid.tenant_id
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.add(run(sources[1].id)); db.flush()
        db.commit()


def test_runtime_cannot_rewrite_audit_reset_bootstrap_or_run_ddl(pg_factory):
    factory, _ = pg_factory
    with factory() as db:
        from scripts.verify_postgresql_runtime import _verify_access_governance_privileges
        _verify_access_governance_privileges(db.connection())
        role = db.scalar(text("SELECT current_user"))
        expected = {"access_audit_events": (True, True, False, False),
                    "access_governance_guard": (True, False, True, False)}
        for table, permissions in expected.items():
            actual = tuple(db.scalar(text("SELECT has_table_privilege(:role, :table, :verb)"),
                {"role": role, "table": table, "verb": verb}) for verb in ["SELECT", "INSERT", "UPDATE", "DELETE"])
            assert actual == permissions
        assert not db.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"))
        assert not db.scalar(text("SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname=current_user"))


def test_two_delivery_workers_claim_once_and_cancel_fences_late_result(pg_factory, monkeypatch):
    factory, _ = pg_factory
    monkeypatch.setattr(get_settings(), "public_app_url", "http://localhost:5173")
    monkeypatch.setattr(delivery, "SessionLocal", factory)
    # Isolate queued fixtures from other tests: delivery cancellation is metadata only.
    with factory() as db:
        db.execute(text("UPDATE workspace_invitations SET delivery_status='cancelled'"))
        f = seed_access(db)
        invitation = invitations.create_invitation(db, InviteIn(email=f.recipient.email))
        db.commit()
        actor, tenant = f.owner.id, f.tenant.id
    sending, release = Event(), Event()
    def send(*args):
        sending.set()
        assert release.wait(timeout=20)
    sender = Mock(side_effect=send)
    monkeypatch.setattr(delivery, "send_mail_message", sender)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(delivery.deliver_one)
        assert sending.wait(timeout=10)
        second = pool.submit(delivery.deliver_one)
        assert second.result(timeout=10) is False
        with factory() as db:
            db.info.update(user_id=actor, tenant_id=tenant)
            invitations.manage_invitation(db, invitation.id, invitation.revision, False)
            db.commit()
        release.set()
        assert first.result(timeout=10)
    with factory() as db:
        row = db.get(WorkspaceInvitation, invitation.id)
        assert row.status == "revoked" and row.delivery_status == "cancelled"
    assert sender.call_count == 1


def test_two_owners_cannot_concurrently_remove_each_other(pg_factory, monkeypatch):
    factory, _ = pg_factory
    monkeypatch.setattr(get_settings(), "public_app_url", "http://localhost:5173")
    with factory() as db:
        f = seed_access(db)
        invitation = invitations.create_invitation(db, InviteIn(email=f.recipient.email, role="owner"))
        invitations.respond_invitation(db, f.recipient.id, invitation.id, invitation.revision, True)
        db.commit()
        members = [workspace_service.membership(db, user, f.tenant.id) for user in [f.owner.id, f.recipient.id]]
        values = [(f.owner.id, members[1].id, members[1].revision), (f.recipient.id, members[0].id, members[0].revision)]
        tenant = f.tenant.id
    barrier = Barrier(2)
    def remove(actor, target, revision):
        with factory() as db:
            db.info.update(user_id=actor, tenant_id=tenant)
            permission_service.require_principal(db)
            barrier.wait(timeout=10)
            try:
                workspace_service.update_member(db, target, revision, None)
                db.commit()
                return 200
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(remove, *value) for value in values]
        assert sorted(future.result(timeout=30) for future in futures) == [200, 403]


def test_database_rejects_role_from_another_workspace(pg_factory):
    factory, _ = pg_factory
    with factory() as db:
        f = seed_access(db)
        member = workspace_service.membership(db, f.owner.id, f.tenant.id)
        foreign = workspace_service.membership(db, f.recipient.id, f.recipient_tenant.id)
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                member.role_id = foreign.role_id
                db.flush()
