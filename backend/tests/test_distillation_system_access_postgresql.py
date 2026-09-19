from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.distillation_access_models import DistillationSystemAccess
from app.distillation_access_schemas import SystemAccessRequest, SystemConfigurationRequest
from app.distillation_schemas import DistillationDocument, ProjectCreate, ProjectUpdate
from app.distillation_target_schemas import TargetSystem
from app.services import distillation_access_crypto, distillation_access_service as access, distillation_service
from isolated_postgresql import isolated_postgresql, isolated_database, seed_workspace, tenant_session


def test_credentials_scope_expiry_revocation_and_tenant_isolation(isolated_postgresql, monkeypatch):
    ring = SimpleNamespace(active_key_id="ephemeral", keys={"ephemeral": secrets.token_bytes(32)})
    monkeypatch.setattr(distillation_access_crypto, "load_keyring", lambda: ring)
    isolated = isolated_postgresql
    workspace, foreign = seed_workspace(isolated.admin_engine), seed_workspace(isolated.admin_engine)
    target = TargetSystem(key="system", name="系统", base_url="https://example.com", purpose="核对结果", allowed_paths=["/history"], access_mode="authorized_readonly")
    secret = secrets.token_urlsafe(32)

    def request(revision, **changes):
        return SystemAccessRequest.model_validate({"expected_revision": revision, "auth_type": "bearer", "secret": secret,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), "authorization_basis": "业务负责人授权核对这一类历史结果",
            "authorized_readonly": True, **changes})

    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = distillation_service.create_project(db, ProjectCreate(name="授权验收", scenario_id=workspace["scenario_id"], document=DistillationDocument(target_systems=[target])))
        db.commit()
        project_id = row.id
        with pytest.raises(HTTPException, match="有效只读授权"):
            access.read_authorization(db, row.id, target)
        db.rollback()
        with pytest.raises(HTTPException) as expired:
            access.authorize(db, row.id, target.key, request(1, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        assert expired.value.status_code == 422
        db.rollback()
        row = access.authorize(db, row.id, target.key, request(1))
        db.commit()
        assert row.revision == 2
        assert access.read_authorization(db, row.id, target) == "Bearer " + secret
        assert secret not in str(row.document) and secret not in str(access.statuses(db, row.id))
        grant = db.scalar(select(DistillationSystemAccess).where(DistillationSystemAccess.project_id == row.id))
        assert secret not in str(grant.credential_envelope)
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE distillation_system_access SET auth_type='basic'"))
        db.rollback()
        changed_target = target.model_copy(update={"allowed_paths": ["/different"]})
        row = distillation_service.update_project(db, project_id, ProjectUpdate(name="Changed", scenario_id=workspace["scenario_id"],
            expected_revision=2, document=DistillationDocument(target_systems=[changed_target])))
        db.commit()
        assert access.statuses(db, project_id)[0].status == "scope_changed"
        with pytest.raises(HTTPException):
            access.read_authorization(db, project_id, changed_target)
        db.rollback()
        row = access.authorize(db, project_id, target.key, request(3))
        db.commit()
        assert access.read_authorization(db, project_id, changed_target) == "Bearer " + secret
        row = access.revoke(db, project_id, target.key, row.revision)
        db.commit()
        assert access.statuses(db, project_id)[0].status == "revoked"
        with pytest.raises(HTTPException):
            access.read_authorization(db, project_id, changed_target)
    with tenant_session(isolated.runtime_engine, foreign) as db:
        with pytest.raises(HTTPException) as rejected:
            access.statuses(db, project_id)
        assert rejected.value.status_code == 404


def test_discovery_migrations_roundtrip_on_empty_isolated_database():
    with isolated_database() as isolated:
        isolated.migrate("20260918_38", downgrade=True)
        isolated.migrate("head")
        with isolated.runtime_engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM distillation_system_access")) == 0
            assert connection.scalar(text("SELECT has_column_privilege(current_user, 'distillation_system_access', 'revoked_at', 'UPDATE')"))


def test_system_configuration_is_atomic_and_scope_changes_require_new_authorization(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace = seed_workspace(isolated.admin_engine)
    ring = SimpleNamespace(active_key_id="ephemeral", keys={"ephemeral": secrets.token_bytes(32)})
    monkeypatch.setattr(distillation_access_crypto, "load_keyring", lambda: ring)
    target = TargetSystem(key="system", name="系统", base_url="https://example.com", purpose="调查",
        allowed_paths=["/"], access_mode="authorized_readonly", browser={})
    credentials = dict(auth_type="browser", username="synthetic-reader", secret=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), authorization_basis="系统负责人允许只读核对", authorized_readonly=True)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        project_id = distillation_service.create_project(db, ProjectCreate(name="配置验收", scenario_id=workspace["scenario_id"])).id
        db.commit()
        with pytest.raises(HTTPException) as missing:
            access.configure(db, project_id, SystemConfigurationRequest(expected_revision=1, target=target))
        assert missing.value.status_code == 409
        db.rollback()
        assert distillation_service.project(db, project_id).document["target_systems"] == []
        with pytest.raises(HTTPException) as expired:
            access.configure(db, project_id, SystemConfigurationRequest(expected_revision=1, target=target,
                credentials={**credentials, "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}))
        assert expired.value.status_code == 422
        db.rollback()
        assert distillation_service.project(db, project_id).revision == 1
        assert distillation_service.project(db, project_id).document["target_systems"] == []
        row = access.configure(db, project_id, SystemConfigurationRequest(expected_revision=1, target=target, credentials=credentials))
        db.commit()
        revision = row.revision
        changed = target.model_copy(update={"allowed_paths": ["/history"]})
        with pytest.raises(HTTPException) as stale_scope:
            access.configure(db, project_id, SystemConfigurationRequest(expected_revision=revision, target=changed))
        assert stale_scope.value.status_code == 409
        db.rollback()
        assert distillation_service.project(db, project_id).document["target_systems"][0]["allowed_paths"] == ["/"]
        row = access.configure(db, project_id, SystemConfigurationRequest(expected_revision=revision, target=target))
        db.commit()
        assert access.browser_credentials(db, project_id, target)["secret"] == credentials["secret"]
        revision = row.revision
        row = access.configure(db, project_id, SystemConfigurationRequest(expected_revision=revision, target=changed, credentials=credentials))
        db.commit()
        assert access.statuses(db, project_id)[0].status == "active"
        with pytest.raises(HTTPException) as stale_write:
            access.configure(db, project_id, SystemConfigurationRequest(expected_revision=revision, target=changed, credentials=credentials))
        assert stale_write.value.status_code == 409
