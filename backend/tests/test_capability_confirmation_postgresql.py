"""Preview persistence and confirmation against an isolated migrated PostgreSQL."""
from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models import CapabilityInvocation
from .access_postgresql import isolated_access_database
from .test_capability_invoker import (
    RecordingProvider,
    _invoke,
    _object_contract,
    _request,
    _world,
)
from .test_agent_capability_confirmations import _preview as agent_preview
from .test_scenario_releases_postgresql import _insert_legacy
from app.services import agent_capability_confirmation_service
from app.models import WorkflowRun


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1",
    reason="explicit PostgreSQL opt-in required; creates its own isolated database",
)
def test_preview_survives_commit_and_confirmation_replays_without_execution() -> None:
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        provider = RecordingProvider(
            _object_contract(
                side_effect=True,
                requires_confirmation=True,
                idempotency_required=True,
            ),
            result={"accepted": True},
        )
        try:
            with Session(engine, expire_on_commit=False) as db:
                world = _world(db, "pg-preview")
                request = _request(
                    world,
                    correlation_id="pg-preview",
                    idempotency_key="pg-preview",
                    mode="preview",
                )
                preview = _invoke(db, world, provider, request)
                db.commit()
                invocation_id = preview.invocation_id
                confirmation = dict(preview.confirmation)
                assert preview.status == "awaiting_confirmation"
                assert provider.calls == []

            with Session(engine, expire_on_commit=False) as db:
                assert db.get(CapabilityInvocation, invocation_id).status == "awaiting_confirmation"
                confirmed_request = _request(
                    world,
                    correlation_id="pg-preview",
                    idempotency_key="pg-preview",
                    mode="confirm",
                    confirmation=confirmation,
                )
                confirmed = _invoke(db, world, provider, confirmed_request)
                db.commit()
                assert confirmed.status == "succeeded"
                assert confirmed.invocation_id == invocation_id

            with Session(engine) as db:
                replay = _invoke(db, world, provider, confirmed_request)
                assert replay.status == "succeeded"
                assert replay.invocation_id == invocation_id
                assert replay.audit_ref["replayed"] is True
                assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1
                assert len(provider.calls) == 1

        finally:
            engine.dispose()


@pytest.mark.skipif(os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1", reason="requires a self-created PostgreSQL database")
def test_legacy_status_migration_refuses_to_truncate_pending_confirmation():
    with isolated_access_database("20260907_26") as (url, _admin):
        engine = create_engine(url)
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        try:
            with Session(engine) as db:
                world = _world(db, "legacy-status")
                _insert_legacy(db, CapabilityInvocation, id="legacy-status-preview", tenant_id=world.tenant.id,
                    scenario_id=world.scenario.id, capability_kind="function", capability_key="legacy-status",
                    definition_hash="a" * 64, deployment_fingerprint="b" * 64, data_context_fingerprint="c" * 64,
                    correlation_id="legacy-status", principal_type="service", principal_id="legacy-service",
                    request_id="legacy-status-request", input_hash="d" * 64, status="awaiting_confirmation")
                db.commit()
            with pytest.raises(RuntimeError, match="long statuses remain"):
                command.downgrade(config, "20260907_25")
            with Session(engine) as db:
                record = db.get(CapabilityInvocation, "legacy-status-preview")
                assert record.status == "awaiting_confirmation"
                record.status = "succeeded"
                db.commit()
            command.downgrade(config, "20260907_25")
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT character_maximum_length FROM information_schema.columns WHERE table_schema='public' AND table_name='capability_invocations' AND column_name='status'")) == 20
            command.upgrade(config, "head")
        finally:
            engine.dispose()


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1",
    reason="explicit PostgreSQL opt-in required; creates its own isolated database",
)
def test_agent_human_confirmation_uses_one_durable_workflow_task() -> None:
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        try:
            with Session(engine, expire_on_commit=False) as db:
                agent, _, message, invocation = agent_preview(db, nested=True)
                result = agent_capability_confirmation_service.confirm(db, agent.id, invocation.id, message.id)
                db.commit()
                replay = agent_capability_confirmation_service.confirm(db, agent.id, invocation.id, message.id)
                db.commit()
                assert result["status"] == replay["status"] == "running"
                assert result["workflow_run_id"] == replay["workflow_run_id"]
                assert db.scalar(select(func.count(WorkflowRun.id))) == 1
        finally:
            engine.dispose()
