"""Manual release decisions and infrastructure-independent business identity."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.business_query_contract import BusinessQueryContractMiddleware
from app.config import get_settings
from app.database import Base
from app.models import BusinessScenario, OntologyRelease, Tenant, User
from app.release_models import ReleaseLifecycleEvent
from app.services import permission_service, release_service, runtime_definition_service, scenario_release_service


@pytest.fixture
def release_world():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        tenant = Tenant(id="release-tenant", name="Release workspace")
        user = User(id="release-user", tenant_id=tenant.id, email="release@example.test", password_hash="test-only", status="active")
        scenario = BusinessScenario(id="release-scenario", tenant_id=tenant.id, name="Release scenario", status="active")
        db.add_all([tenant, user, scenario])
        db.commit()
        permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
        db.commit()
        db.info.update(tenant_id=tenant.id, user_id=user.id)
        yield db, scenario
    engine.dispose()


def test_creation_enable_retire_delete_are_separate_audited_human_commands(release_world):
    db, scenario = release_world
    with pytest.raises(release_service.ReleaseValidationError):
        scenario_release_service.create_release(db, scenario.id, confirmed=False)
    assert list(db.scalars(select(OntologyRelease))) == []
    release = scenario_release_service.create_release(db, scenario.id, confirmed=True, name="Approved version")
    assert release.status == "released" and not release.enabled and release.revision == 1
    with pytest.raises(runtime_definition_service.RuntimeDefinitionError):
        runtime_definition_service.resolve_active(db, scenario)
    scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=1, action="enable")
    pinned = runtime_definition_service.resolve_active(db, scenario)
    scenario.name = "Changed current scenario"
    db.commit()
    assert runtime_definition_service.resolve_active(db, scenario).definition_hash == pinned.definition_hash
    with pytest.raises(release_service.ReleaseConflictError):
        scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=1, action="disable")
    with pytest.raises(release_service.ReleaseConflictError):
        scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=2, action="delete")
    second = scenario_release_service.create_release(db, scenario.id, confirmed=True)
    with pytest.raises(release_service.ReleaseConflictError):
        scenario_release_service.change_release(db, scenario.id, second.id, expected_revision=1, action="enable")
    scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=2, action="retire")
    scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=3, action="delete")
    assert db.get(OntologyRelease, release.id).deleted_at is not None
    history = runtime_definition_service.resolve_pinned(db, scenario, release_id=release.id,
        snapshot_id=release.snapshot_id, definition_hash=pinned.definition_hash)
    assert history.definition_hash == pinned.definition_hash
    assert [event.action for event in db.scalars(select(ReleaseLifecycleEvent).where(
        ReleaseLifecycleEvent.release_id == release.id).order_by(ReleaseLifecycleEvent.revision))] == ["created", "enable", "retire", "delete"]


def test_deployment_configuration_never_selects_business_definition_or_permission(release_world):
    db, scenario = release_world
    release = scenario_release_service.create_release(db, scenario.id, confirmed=True)
    scenario_release_service.change_release(db, scenario.id, release.id, expected_revision=1, action="enable")
    observations = []
    for mode in ("dev", "staging", "prod"):
        with patch.object(get_settings(), "runtime_environment", mode):
            active = runtime_definition_service.resolve_active(db, scenario)
            authored = runtime_definition_service.resolve_authoring(db, scenario)
            observations.append((active.release_id, active.definition_hash, authored.definition_hash,
                                 permission_service.check_scenario(db, scenario, "read").allowed))
    assert observations[0] == observations[1] == observations[2]
    for table in Base.metadata.tables.values():
        assert not {"environment", "runtime_environment", "environment_status"}.intersection(table.c.keys()), table.name


def test_retired_query_selectors_are_rejected_and_typed_business_inputs_remain_available():
    app = FastAPI()
    app.add_middleware(BusinessQueryContractMiddleware, api_prefix="/api")

    @app.get("/api/probe")
    def probe():
        return {"ready": True}

    with TestClient(app) as client:
        assert client.get("/api/probe").status_code == 200
        for key in ("environment", "runtime_environment", "expected_environment"):
            response = client.get("/api/probe", params={key: "prod"})
            assert response.status_code == 410
            assert response.json()["detail"]["code"] == "business_environment_removed"
