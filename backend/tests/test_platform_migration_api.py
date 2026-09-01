from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BusinessScenario,
    FunctionDefinition,
    LLMConfig,
    PlatformMigrationCheckpoint,
    Tenant,
    User,
)
from app.routers import agents, platform_migrations
from app.services import agent_capability_service, permission_service
from app.services.auth_service import get_tenant_db


def test_migration_api_allows_only_ready_direct_capability_cutover() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = "tenant-migration-api"
    user_id = "user-migration-api"
    agent_id = "agent-migration-api"
    function_id = "function-migration-api"
    llm_id = "llm-migration-api"
    with SessionLocal() as db:
        tenant = Tenant(id=tenant_id, name="Migration API tenant")
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            email="migration-api@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-migration-api",
            tenant_id=tenant_id,
            name="Migration API scenario",
        )
        llm = LLMConfig(
            id=llm_id,
            tenant_id=tenant_id,
            name="Validation model",
            model="test-model",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        function = FunctionDefinition(
            id=function_id,
            scenario_id=scenario.id,
            name="Deterministic API score",
            input_schema={
                "type": "object",
                "properties": {"amount": {"type": "number"}},
                "required": ["amount"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"score": {"type": "number"}},
                "additionalProperties": False,
            },
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 1}},
        )
        scope = agent_capability_service.explicit_empty_scope()
        scope["functions"] = {
            "mode": "explicit",
            "selected_ids": [function_id],
        }
        agent = Agent(
            id=agent_id,
            tenant_id=tenant_id,
            name="Validation Agent",
            scenario_id=scenario.id,
            llm_config_id=None,
            capability_scope=scope,
            data_source_ids=[],
            runtime_binding_mode="legacy",
        )
        db.add_all([tenant, user, scenario, llm, function, agent])
        db.commit()
        permission_service.ensure_organization(db, tenant_id, owner_user_id=user_id)
        db.commit()

    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.include_router(platform_migrations.router, prefix="/api")

    def override_db():
        db = SessionLocal()
        db.info["tenant_id"] = tenant_id
        db.info["user_id"] = user_id
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_tenant_db] = override_db
    client = TestClient(app)
    try:
        started = client.post("/api/platform/migrations/legacy-catalog/start")
        assert started.status_code == 200, started.text
        completed = client.post(
            "/api/platform/migrations/legacy-catalog/run", json={"batch_size": 10}
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "verified"

        bypass = client.put(
            f"/api/agents/{agent_id}",
            json={
                "name": "Validation Agent",
                "scenario_id": "scenario-migration-api",
                "data_source_ids": [],
                "capability_scope": scope,
                "runtime_binding_mode": "capability_only",
            },
        )
        assert bypass.status_code == 409, bypass.text
        assert bypass.json()["detail"]["code"] == "agent_mode_migration_required"

        for target in ("shadow", "prefer_capability", "legacy"):
            rejected = client.post(
                f"/api/agents/{agent_id}/migration/mode",
                json={"target_mode": target, "reason": "Historical mode forbidden"},
            )
            assert rejected.status_code == 422, rejected.text

        for suffix in ("shadow/refresh", "shadow/validate"):
            retired = client.post(
                f"/api/agents/{agent_id}/migration/{suffix}", json={}
            )
            assert retired.status_code == 404, retired.text

        blocked = client.post(
            f"/api/agents/{agent_id}/migration/mode",
            json={
                "target_mode": "capability_only",
                "reason": "Attempt before readiness",
                "idempotency_key": "api-direct-cutover",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "migration_readiness_failed"
        with SessionLocal() as db:
            persisted = db.get(Agent, agent_id)
            assert persisted.runtime_binding_mode == "legacy"
            assert not db.query(PlatformMigrationCheckpoint).filter_by(
                stage="agent_mode_event"
            ).count()
            persisted.llm_config_id = llm_id
            db.commit()

        cutover = client.post(
            f"/api/agents/{agent_id}/migration/mode",
            json={
                "target_mode": "capability_only",
                "reason": "Attempt before readiness",
                "idempotency_key": "api-direct-cutover",
            },
        )
        assert cutover.status_code == 200, cutover.text
        document = cutover.json()
        assert document["runtime_binding_mode"] == "capability_only"
        assert document["gate"]["passed"] is True
        assert document["events"][0]["readiness_fingerprint"]

        rollback = client.post(
            f"/api/agents/{agent_id}/migration/rollback",
            json={"reason": "Do not restore legacy"},
        )
        assert rollback.status_code == 404, rollback.text
        with SessionLocal() as db:
            persisted = db.get(Agent, agent_id)
            assert persisted.runtime_binding_mode == "capability_only"
            assert not db.query(PlatformMigrationCheckpoint).filter_by(
                stage="shadow_metric"
            ).count()
    finally:
        client.close()
        engine.dispose()
