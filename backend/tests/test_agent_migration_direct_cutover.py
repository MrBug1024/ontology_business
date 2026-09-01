from __future__ import annotations

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


def _world(
    db: Session,
    key: str,
    *,
    mode: str = "legacy",
    with_llm: bool = True,
) -> Agent:
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
        runtime_binding_mode=mode,
    )
    db.add_all([tenant, user, scenario, llm, function, agent])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return agent


def _close(db: Session) -> None:
    engine = db.info["test_engine"]
    db.close()
    engine.dispose()


@pytest.mark.parametrize("mode", ["legacy", "shadow", "prefer_capability"])
def test_historical_agent_directly_cuts_over_after_capability_readiness(mode: str) -> None:
    db = _db()
    try:
        agent = _world(db, f"ready-{mode}", mode=mode)

        gate = agent_migration_service.evaluate_migration_gate(db, agent.id)
        assert gate["passed"] is True
        assert all(
            gate["readiness"][axis]["ready"]
            for axis in ("definition", "validation", "release", "runtime")
        )
        assert gate["readiness"]["runtime"]["missing"] == []

        result = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="capability_only",
            reason="Capability readiness verified",
            idempotency_key=f"cutover-{mode}",
        )
        db.commit()

        assert result["runtime_binding_mode"] == "capability_only"
        assert result["gate"]["passed"] is True
        assert result["events"][0]["from_mode"] == mode
        assert result["events"][0]["to_mode"] == "capability_only"
        assert result["events"][0]["readiness_fingerprint"]
        assert result["events"][0]["readiness"]["runtime"]["missing"] == []
    finally:
        _close(db)


def test_direct_cutover_fails_closed_without_capability_readiness_or_event() -> None:
    db = _db()
    try:
        agent = _world(db, "not-ready", with_llm=False)

        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.change_agent_mode(
                db,
                agent.id,
                target_mode="capability_only",
                reason="Attempt before readiness",
                idempotency_key="not-ready-attempt",
            )

        assert blocked.value.code == "migration_readiness_failed"
        assert agent.runtime_binding_mode == "legacy"
        assert db.scalar(
            select(func.count())
            .select_from(PlatformMigrationCheckpoint)
            .where(PlatformMigrationCheckpoint.stage == "agent_mode_event")
        ) == 0
    finally:
        _close(db)


@pytest.mark.parametrize("target", ["legacy", "shadow", "prefer_capability"])
def test_service_rejects_every_non_capability_target(target: str) -> None:
    db = _db()
    try:
        agent = _world(db, f"target-{target}")
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.change_agent_mode(
                db,
                agent.id,
                target_mode=target,
                reason="Do not revive a historical runtime",
            )
        assert blocked.value.code == "invalid_target_mode"
        assert agent.runtime_binding_mode == "legacy"
    finally:
        _close(db)
