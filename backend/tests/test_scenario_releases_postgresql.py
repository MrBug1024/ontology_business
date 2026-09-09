"""Real migration, authenticated payload preservation and release concurrency."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.models import AgentTurnRun, Conversation, Message, OntologyBranch, OntologyRelease, OntologySnapshot, OntologyWorkflow, WorkflowRun
from app.schemas import ChatRequest
from app.services import agent_turn_payload_service, agent_turn_service, release_service, scenario_release_service, workflow_payload_service
from .access_postgresql import isolated_access_database
from .test_agent_runtime_adapter import _world


pytestmark = pytest.mark.skipif(os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1", reason="requires a self-created PostgreSQL database")


def _upgrade():
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")


def _insert_legacy(db, model, **values):
    table = sa.Table(model.__tablename__, sa.MetaData(), autoload_with=db.connection())
    for column in table.c:
        current = model.__table__.c.get(column.name)
        if current is not None and current.default is not None:
            column.default = current.default
    db.execute(table.insert().values(environment="dev", **values))


def test_migration_rejects_ambiguous_releases_and_preserves_encrypted_input_and_replay():
    with isolated_access_database("20260907_26") as (url, admin):
        engine = sa.create_engine(url)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with factory() as db:
                tenant, user, scenario, _, _, agent = _world(db, "release-migrate")
                request = ChatRequest(message="Migration input", idempotency_key="migration-turn")
                conversation = Conversation(id="migration-conversation", agent_id=agent.id, created_by_user_id=user.id, title="Migration")
                db.add(conversation)
                db.flush()
                user_message = Message(id="migration-user-message", conversation_id=conversation.id, role="user", content=request.message)
                assistant_message = Message(id="migration-agent-message", conversation_id=conversation.id, role="assistant", content="", stream_finalized=False)
                db.add_all([user_message, assistant_message])
                db.flush()
                queued = {"id": "migration-turn-run"}
                legacy = {**request.model_dump(mode="json", exclude_none=True), "environment": "dev"}
                encoded = json.dumps({"contract": "agent-turn-request/v1", "tenant_id": tenant.id,
                    "user_id": user.id, "agent_id": agent.id, "parent_run_id": "", "request": legacy},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                fingerprint = hashlib.sha256(encoded).hexdigest()
                context = {"contract": "agent-turn-payload-context/v1", "run_id": queued["id"],
                    "tenant_id": tenant.id, "requested_by_user_id": user.id, "agent_id": agent.id,
                    "conversation_id": conversation.id, "request_fingerprint": fingerprint}
                sealed = agent_turn_payload_service.seal_payload({k: v for k, v in legacy.items() if k != "message"}, context=context)
                _insert_legacy(db, AgentTurnRun, id=queued["id"], tenant_id=tenant.id, requested_by_user_id=user.id,
                    agent_id=agent.id, conversation_id=conversation.id, user_message_id=user_message.id,
                    assistant_message_id=assistant_message.id, idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint, request_payload=sealed.envelope,
                    request_summary=sealed.summary, request_digest=sealed.digest)
                workflow = OntologyWorkflow(id="migration-workflow", scenario_id=scenario.id, name="Migration workflow",
                    status="active", enabled=True, nodes=[], edges=[], steps=[])
                db.add(workflow)
                db.flush()
                run = WorkflowRun(id="migration-run", scenario_id=scenario.id, workflow_id=workflow.id,
                    created_by_user_id=user.id, definition_hash="a" * 64, definition_source="live", dedupe_key="dev:migration-key")
                old_context = workflow_payload_service.payload_context(run_id=run.id, scenario_id=scenario.id,
                    workflow_id=workflow.id, environment="dev", definition_hash=run.definition_hash)
                sealed = workflow_payload_service.seal_payload({"business_value": "unchanged"}, context=old_context)
                run.input_payload, run.input_summary, run.input_digest = sealed.envelope, sealed.summary, sealed.digest
                _insert_legacy(db, WorkflowRun, id=run.id, scenario_id=scenario.id, workflow_id=workflow.id,
                    created_by_user_id=user.id, definition_hash=run.definition_hash, definition_source="live",
                    dedupe_key=run.dedupe_key, input_payload=run.input_payload,
                    input_summary=run.input_summary, input_digest=run.input_digest)
                branch = OntologyBranch(id="migration-branch", tenant_id=tenant.id, scenario_id=scenario.id, name="main")
                db.add(branch)
                db.flush()
                content = release_service.capture_snapshot_content(db, scenario)
                snapshot = OntologySnapshot(id="migration-snapshot", tenant_id=tenant.id, scenario_id=scenario.id,
                    branch_id=branch.id, kind="merge", content=content, content_hash=release_service.snapshot_hash(content))
                db.add(snapshot)
                db.commit()
                original_cipher = dict(run.input_payload)
            with admin.begin() as connection:
                releases = sa.Table("ontology_releases", sa.MetaData(), autoload_with=connection)
                for index, label in enumerate(("dev", "prod")):
                    connection.execute(releases.insert().values(id=f"migration-release-{index}", tenant_id=tenant.id,
                        scenario_id=scenario.id, branch_id=branch.id, snapshot_id=snapshot.id, environment=label,
                        status="released", notes="", connector_audit=[], created_by_user_id=user.id,
                        withdraw_reason="", created_at=datetime.now(timezone.utc)))
            with pytest.raises(RuntimeError, match="Business identity conflict in ontology_releases"):
                _upgrade()
            with admin.begin() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260907_26"
                assert connection.scalar(sa.text("SELECT input_payload FROM workflow_runs WHERE id='migration-run'")) == original_cipher
                connection.execute(releases.delete().where(releases.c.id == "migration-release-1"))
            _upgrade()
            with factory() as db:
                db.info.update(tenant_id=tenant.id, user_id=user.id)
                run = db.get(WorkflowRun, "migration-run")
                assert workflow_payload_service.open_workflow_run_input(run) == {"business_value": "unchanged"}
                assert run.dedupe_key == "migration-key" and run.input_payload != original_cipher
                replay = agent_turn_service.enqueue_turn(db, agent.id, request)
                assert replay["id"] == queued["id"]
                assert db.scalar(sa.select(sa.func.count()).select_from(AgentTurnRun)) == 1
                assert db.get(OntologyRelease, "migration-release-0").enabled
                for table in ("workflow_runs", "agent_turn_runs", "dataset_heads", "connector_bindings", "ontology_releases"):
                    assert "environment" not in {item["name"] for item in sa.inspect(db.connection()).get_columns(table)}
        finally:
            engine.dispose()


@pytest.mark.parametrize("status", ["failed", "accepted"])
def test_deleted_conversation_audit_is_preserved_but_live_payloads_must_authenticate(status):
    with isolated_access_database("20260907_26") as (url, admin):
        engine = sa.create_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with factory() as db:
                tenant, user, _, _, _, agent = _world(db, "deleted-conversation")
                context = {"contract": "agent-turn-payload-context/v1", "run_id": "deleted-turn",
                    "tenant_id": tenant.id, "requested_by_user_id": user.id, "agent_id": agent.id,
                    "conversation_id": "already-deleted-conversation", "request_fingerprint": "a" * 64}
                sealed = agent_turn_payload_service.seal_payload({"environment": "dev"}, context=context)
                _insert_legacy(db, AgentTurnRun, id="deleted-turn", tenant_id=tenant.id, requested_by_user_id=user.id,
                    agent_id=agent.id, status=status, idempotency_key="deleted-turn", request_fingerprint="a" * 64,
                    request_payload=sealed.envelope, request_summary=sealed.summary, request_digest=sealed.digest)
                db.commit()
            if status == "accepted":
                with pytest.raises(RuntimeError, match="Encrypted runtime payload migration failed"):
                    _upgrade()
                with admin.connect() as connection:
                    assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260907_26"
                    assert connection.scalar(sa.text("SELECT request_payload FROM agent_turn_runs WHERE id='deleted-turn'")) == sealed.envelope
            else:
                _upgrade()
                with factory() as db:
                    db.info.update(tenant_id=tenant.id, user_id=user.id)
                    run = db.get(AgentTurnRun, "deleted-turn")
                    assert (run.request_payload, run.request_summary, run.request_digest) == (sealed.envelope, sealed.summary, sealed.digest)
                    assert run.request_fingerprint == "a" * 64 and run.status == status
                    with pytest.raises(agent_turn_service.AgentTurnConflict, match="执行上下文已失效"):
                        agent_turn_service.retry_turn(db, run.id, expected_revision=run.revision, idempotency_key="retry-deleted-turn")
        finally:
            engine.dispose()


def test_concurrent_enable_allows_one_explicit_version_and_audit_is_append_only():
    with isolated_access_database() as (url, _admin):
        engine = sa.create_engine(url)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with factory() as db:
                tenant, user, scenario, _, _, _ = _world(db, "release-race")
                first = scenario_release_service.create_release(db, scenario.id, confirmed=True)
                second = scenario_release_service.create_release(db, scenario.id, confirmed=True)

            def enable(release_id):
                with factory() as db:
                    db.info.update(tenant_id=tenant.id, user_id=user.id)
                    try:
                        scenario_release_service.change_release(db, scenario.id, release_id, expected_revision=1, action="enable")
                        return "enabled"
                    except release_service.ReleaseConflictError:
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(enable, (first.id, second.id))) == ["conflict", "enabled"]
            with factory() as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(OntologyRelease).where(OntologyRelease.enabled.is_(True))) == 1
                assert db.scalar(sa.text("SELECT has_table_privilege(current_user, 'release_lifecycle_events', 'INSERT')"))
                assert not db.scalar(sa.text("SELECT has_table_privilege(current_user, 'release_lifecycle_events', 'UPDATE')"))
                assert not db.scalar(sa.text("SELECT has_table_privilege(current_user, 'release_lifecycle_events', 'DELETE')"))
        finally:
            engine.dispose()
