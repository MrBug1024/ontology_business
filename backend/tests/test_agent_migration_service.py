from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    FunctionDefinition,
    LLMConfig,
    PlatformMigrationCheckpoint,
    PlatformMigrationRun,
    Tenant,
    User,
)
from app.services import (
    agent_capability_service,
    agent_migration_service,
    permission_service,
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.info["test_engine"] = engine
    return db


def _world(db: Session, key: str, *, with_llm: bool = True) -> tuple[Tenant, User, Agent]:
    tenant = Tenant(id=f"tenant-{key}", name=f"Tenant {key}")
    user = User(
        id=f"user-{key}",
        tenant_id=tenant.id,
        email=f"{key}@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id=f"scenario-{key}", tenant_id=tenant.id, name=f"Scenario {key}"
    )
    llm = LLMConfig(
        id=f"llm-{key}",
        tenant_id=tenant.id,
        name="Validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    function = FunctionDefinition(
        id=f"function-{key}",
        scenario_id=scenario.id,
        name="Deterministic score",
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
    scope["functions"] = {"mode": "explicit", "selected_ids": [function.id]}
    agent = Agent(
        id=f"agent-{key}",
        tenant_id=tenant.id,
        name="Validation Agent",
        scenario_id=scenario.id,
        llm_config_id=llm.id if with_llm else None,
        capability_scope=scope,
        data_source_ids=[],
        runtime_binding_mode="legacy",
    )
    db.add_all([tenant, user, scenario, llm, function, agent])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return tenant, user, agent


def _close(db: Session) -> None:
    engine = db.info["test_engine"]
    db.close()
    engine.dispose()


def test_cutover_event_is_idempotent_and_conflicting_reuse_is_rejected() -> None:
    db = _db()
    try:
        _tenant, _user, agent = _world(db, "idempotent")
        first = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="capability_only",
            reason="Readiness verified",
            idempotency_key="same-cutover",
        )
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(PlatformMigrationCheckpoint)
            .where(PlatformMigrationCheckpoint.stage == "agent_mode_event")
        )

        replay = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="capability_only",
            reason="Readiness verified",
            idempotency_key="same-cutover",
        )
        assert replay["events"][0] == first["events"][0]
        assert db.scalar(
            select(func.count())
            .select_from(PlatformMigrationCheckpoint)
            .where(PlatformMigrationCheckpoint.stage == "agent_mode_event")
        ) == count

        with pytest.raises(agent_migration_service.AgentMigrationError) as conflict:
            agent_migration_service.change_agent_mode(
                db,
                agent.id,
                target_mode="capability_only",
                reason="Different request",
                idempotency_key="same-cutover",
            )
        assert conflict.value.code == "migration_idempotency_conflict"
    finally:
        _close(db)


def test_failed_readiness_writes_no_event_and_same_key_can_retry_after_fix() -> None:
    db = _db()
    try:
        _tenant, _user, agent = _world(db, "retry", with_llm=False)
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.change_agent_mode(
                db,
                agent.id,
                target_mode="capability_only",
                reason="Wait for model",
                idempotency_key="retry-after-ready",
            )
        assert blocked.value.code == "migration_readiness_failed"
        assert db.scalar(
            select(func.count())
            .select_from(PlatformMigrationCheckpoint)
            .where(PlatformMigrationCheckpoint.stage == "agent_mode_event")
        ) == 0

        agent.llm_config_id = "llm-retry"
        migrated = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="capability_only",
            reason="Wait for model",
            idempotency_key="retry-after-ready",
        )
        assert migrated["runtime_binding_mode"] == "capability_only"
        assert len(migrated["events"]) == 1
    finally:
        _close(db)


def test_historical_shadow_checkpoint_remains_read_only_across_v2_ledger() -> None:
    db = _db()
    try:
        tenant, _user, agent = _world(db, "historical")
        clock = datetime.now(timezone.utc)
        old_run = PlatformMigrationRun(
            id="old-shadow-ledger",
            migration_name=f"agent-cutover:{tenant.id}",
            plan_digest="a" * 64,
            source_fingerprint="b" * 64,
            status="running",
            current_phase="verify",
            manifest={
                "contract": "agent-capability-migration/v1",
                "tenant_id": tenant.id,
            },
            started_at=clock,
            updated_at=clock,
            last_error="",
        )
        historical_payload = {
            "contract": "agent-shadow-observation/v1",
            "agent_id": agent.id,
            "observation_id": "historical-only",
            "gate_eligible": True,
        }
        old_checkpoint = PlatformMigrationCheckpoint(
            run_id=old_run.id,
            stage="shadow_metric",
            item_key=f"{agent.id}:historical-only",
            status="complete",
            payload_sha256="c" * 64,
            payload=historical_payload,
            completed_at=clock,
        )
        db.add_all([old_run, old_checkpoint])
        db.commit()

        status = agent_migration_service.agent_migration_status(db, agent.id)

        assert status["contract"] == "agent-capability-migration/v2"
        assert status["shadow_observations_read_only"] is True
        assert status["shadow_observations"] == [historical_payload]
        assert {item["contract"] for item in status["ledger_runs"]} == {
            "agent-capability-migration/v1",
            "agent-capability-migration/v2",
        }
        assert not hasattr(agent_migration_service, "refresh_shadow_observations")
        assert not hasattr(agent_migration_service, "execute_server_shadow_validation")
        assert not hasattr(agent_migration_service, "record_server_shadow_probe")
    finally:
        _close(db)


def test_agent_migration_preserves_tenant_and_generic_update_guards() -> None:
    db = _db()
    try:
        tenant_a, user_a, agent_a = _world(db, "acl-a")
        tenant_b, user_b, _agent_b = _world(db, "acl-b")
        db.info["tenant_id"] = tenant_b.id
        db.info["user_id"] = user_b.id
        with pytest.raises(agent_migration_service.AgentMigrationError) as missing:
            agent_migration_service.change_agent_mode(
                db,
                agent_a.id,
                target_mode="capability_only",
                reason="Cross-tenant attempt",
            )
        assert missing.value.code == "agent_not_found"

        db.info["tenant_id"] = tenant_a.id
        db.info["user_id"] = user_a.id
        with pytest.raises(agent_migration_service.AgentMigrationError) as guarded:
            agent_migration_service.assert_direct_mode_update_allowed(
                agent_a, "capability_only"
            )
        assert guarded.value.code == "agent_mode_migration_required"
        agent_migration_service.assert_direct_mode_update_allowed(agent_a, "legacy")
    finally:
        _close(db)
