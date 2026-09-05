from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    Agent,
    AgentTurnEvent,
    AgentTurnRun,
    AuthSession,
    BusinessScenario,
    Message,
    Tenant,
    User,
)
from app.schemas import ChatRequest
from app.routers import agent_turns, agents
from app.services import agent_turn_payload_service, agent_turn_service, permission_service
from app.services.auth_service import get_tenant_db


@pytest.fixture()
def turn_database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    with session_factory() as db:
        tenant = Tenant(id="turn-tenant", name="Turn tenant")
        user = User(
            id="turn-user",
            tenant_id=tenant.id,
            email="turn-user@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="turn-scenario",
            tenant_id=tenant.id,
            name="Turn scenario",
            status="active",
        )
        agent = Agent(
            id="turn-agent",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Turn agent",
            runtime_binding_mode="capability_only",
            capability_scope={},
        )
        db.add_all([tenant, user, scenario, agent])
        db.commit()
        permission_service.ensure_organization(
            db,
            tenant.id,
            owner_user_id=user.id,
        )
        db.commit()
    try:
        yield session_factory
    finally:
        engine.dispose()


def _database(session_factory):
    db = session_factory()
    db.info["tenant_id"] = "turn-tenant"
    db.info["user_id"] = "turn-user"
    return db


def _payload(*, message: str = "请分析本次资料") -> ChatRequest:
    return ChatRequest(
        message=message,
        environment="dev",
        inputs={"sensitive_field": "must-not-be-plaintext"},
        idempotency_key="turn-request-1",
    )


def test_enqueue_commits_message_before_background_execution_and_is_idempotent(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        first = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        replay = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

        assert first["id"] == replay["id"]
        assert first["status"] == "accepted"
        assert first["conversation_id"]
        assert db.get(Message, first["user_message_id"]).content == "请分析本次资料"
        run = db.get(AgentTurnRun, first["id"])
        assert "must-not-be-plaintext" not in str(run.request_payload)
        assert run.request_digest

        events = list(
            db.scalars(
                select(AgentTurnEvent)
                .where(AgentTurnEvent.run_id == run.id)
                .order_by(AgentTurnEvent.revision)
            )
        )
        assert [(item.revision, item.event_type) for item in events] == [
            (1, "accepted")
        ]


def test_missing_encryption_configuration_rejects_submit_without_persisted_messages(
    turn_database,
) -> None:
    encryption = agent_turn_payload_service.workflow_payload_service
    with _database(turn_database) as db, patch.object(
        encryption,
        "load_keyring",
        side_effect=encryption.WorkflowPayloadError(
            "invalid_key_configuration", "private-configuration-must-not-leak"
        ),
    ):
        with pytest.raises(HTTPException) as rejected:
            agent_turns.create_agent_turn("turn-agent", _payload(), db)
        assert rejected.value.status_code == 503
        assert rejected.value.detail["code"] == "agent_turn_encryption_unavailable"
        assert "private-configuration" not in str(rejected.value.detail)
        assert "草稿已保留" in rejected.value.detail["message"]
        assert db.scalar(select(AgentTurnRun)) is None
        assert db.scalar(select(Message)) is None


def test_same_idempotency_key_with_different_request_conflicts(turn_database) -> None:
    with _database(turn_database) as db:
        agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        with pytest.raises(agent_turn_service.AgentTurnConflict):
            agent_turn_service.enqueue_turn(
                db,
                "turn-agent",
                _payload(message="不同请求"),
            )


def test_encrypted_turn_payload_rejects_tampered_authenticated_context(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        run = db.get(AgentTurnRun, queued["id"])
        tampered_summary = {**run.request_summary, "attachment_count": 999}
        with pytest.raises(agent_turn_payload_service.AgentTurnPayloadError):
            agent_turn_payload_service.open_payload(
                run.request_payload,
                context=agent_turn_service._payload_context(run),
                summary=tampered_summary,
                digest=run.request_digest,
            )


def test_worker_rejects_message_changed_after_turn_acceptance(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        message = db.get(Message, queued["user_message_id"])
        message.content = "已被替换的请求"
        db.commit()

    executor = Mock()
    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert agent_turn_service.process_next_turn(executor)
    executor.assert_not_called()

    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assert run.status == "failed"
        assert run.error_code == "agent_turn_request_changed"


def test_worker_rebuilds_principal_and_fenced_completion_survives_replay(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

    observed: dict[str, str] = {}

    def execute(agent_id, *, message, conversation_id, db, **kwargs):
        principal = permission_service.require_principal(db)
        observed.update(
            agent_id=agent_id,
            message=message,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
        return {
            "answer": "完成",
            "conversation_id": conversation_id,
            "assistant_message_id": kwargs["assistant_message_id"],
            "trace_id": "turn-trace",
            "input_snapshot": {"runtime": {"definition_hash": "a" * 64}},
            "evidence_refs": [],
            "tool_calls": [],
            "tool_results": [],
            "citations": [],
            "runtime": {
                "definition_hash": "a" * 64,
                "deployment_fingerprint": "b" * 64,
            },
        }

    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert agent_turn_service.process_next_turn(execute)
        assert not agent_turn_service.process_next_turn(execute)

    assert observed == {
        "agent_id": "turn-agent",
        "message": "请分析本次资料",
        "conversation_id": queued["conversation_id"],
        "tenant_id": "turn-tenant",
        "user_id": "turn-user",
    }
    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assert run.status == "succeeded"
        assert run.result_document["answer"] == "完成"
        assert run.lease_token == ""
        assert db.get(Message, run.assistant_message_id).content == "完成"


def test_durable_final_message_rolls_back_if_run_finalization_crashes(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

    runtime_context = Mock()
    runtime_context.build_tools.return_value = []
    runtime_context.runtime_definition = SimpleNamespace(
        environment="dev",
        snapshot_id=None,
        release_id=None,
        definition_hash="a" * 64,
    )
    runtime_context.deployment = SimpleNamespace(fingerprint="b" * 64)
    runtime_context.runtime_data_context = SimpleNamespace(fingerprint="c" * 64)
    execution_sessions: list[object] = []
    finalize_sessions: list[object] = []

    def fake_run_agent(*_args, **_kwargs):
        yield {
            "type": "tool_call",
            "data": {
                "id": "atomic-call",
                "name": "invoke_capability",
                "arguments": {},
            },
        }
        yield {
            "type": "tool_result",
            "data": {
                "id": "atomic-call",
                "name": "invoke_capability",
                "result": "bounded-result",
            },
        }
        yield {"type": "token", "data": "原子完成"}

    def execute(agent_id, *, db, **kwargs):
        execution_sessions.append(db)
        return agents.invoke_agent_once(agent_id, db=db, **kwargs)

    def crash_before_terminal_commit(db, *_args, **_kwargs):
        finalize_sessions.append(db)
        raise RuntimeError("simulated crash before terminal commit")

    with (
        patch.object(agent_turn_service, "SessionLocal", turn_database),
        patch.object(agents, "SessionLocal", turn_database),
        patch.object(agents, "_authorization_context", return_value=runtime_context),
        patch.object(agents, "_agent_readiness_missing", return_value=[]),
        patch.object(agents.llm_service, "routable_configs", return_value=[Mock()]),
        patch.object(agents.agent_engine, "ontology_summary_for", return_value=""),
        patch.object(agents.agent_runtime_adapter, "input_snapshot", return_value={}),
        patch.object(agents.agent_runtime_adapter, "evidence_snapshot", return_value=[]),
        patch.object(agents.agent_engine, "run_agent", side_effect=fake_run_agent),
        patch.object(
            agent_turn_service,
            "finalize_turn",
            side_effect=crash_before_terminal_commit,
        ),
    ):
        with pytest.raises(RuntimeError, match="simulated crash"):
            agent_turn_service.process_turn(queued["id"], execute)

    assert len(execution_sessions) == 1
    assert finalize_sessions
    assert all(item is execution_sessions[0] for item in finalize_sessions)
    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assistant = db.get(Message, queued["assistant_message_id"])
        assert run.status == "invoking_tools"
        assert assistant.stream_finalized is False
        assert assistant.tool_results == []
        assert assistant.content == "正在准备受控工具调用。"


def test_expired_lease_can_be_reclaimed_but_stale_token_cannot_finalize(
    turn_database,
) -> None:
    now = datetime.now(timezone.utc)
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        first = agent_turn_service.claim_turn(db, queued["id"], as_of=now)
        assert first is not None
        run = db.get(AgentTurnRun, queued["id"])
        run.lease_expires_at = now - timedelta(seconds=1)
        db.commit()
        second = agent_turn_service.claim_turn(
            db,
            queued["id"],
            as_of=now + timedelta(seconds=1),
        )
        assert second is not None
        assert second.token != first.token
        assert second.generation == first.generation + 1
        assert not agent_turn_service.finalize_turn(
            db,
            queued["id"],
            lease=first,
            status="failed",
            error_code="late_worker",
            error_message="late",
        )


def test_releasing_waiting_turn_preserves_transition_revision(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"])
        assert lease is not None
        run = db.scalar(
            select(AgentTurnRun)
            .where(AgentTurnRun.id == queued["id"])
            .with_for_update()
        )
        agent_turn_service._transition(db, run, "preparing_inputs")
        assert agent_turn_service._release_lease(db, run, lease)

    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        events = list(
            db.scalars(
                select(AgentTurnEvent)
                .where(AgentTurnEvent.run_id == run.id)
                .order_by(AgentTurnEvent.revision)
            )
        )
        assert run.status == "preparing_inputs"
        assert run.revision == 2
        assert [(item.revision, item.event_type) for item in events] == [
            (1, "accepted"),
            (2, "preparing_inputs"),
        ]


def test_expired_turn_lease_cannot_be_revived_by_heartbeat(turn_database) -> None:
    now = datetime.now(timezone.utc)
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"], as_of=now)
        assert lease is not None
        run = db.get(AgentTurnRun, queued["id"])
        run.lease_expires_at = now - timedelta(seconds=1)
        db.commit()

    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert not agent_turn_service._renew_turn_lease(queued["id"], lease)

    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assert agent_turn_service._as_utc(run.lease_expires_at) <= now


def test_cancel_uses_revision_and_is_terminal_before_claim(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        cancelled = agent_turn_service.cancel_turn(
            db,
            queued["id"],
            expected_revision=queued["revision"],
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled["revision"] == queued["revision"] + 1
        with pytest.raises(agent_turn_service.AgentTurnConflict):
            agent_turn_service.cancel_turn(
                db,
                queued["id"],
                expected_revision=queued["revision"],
            )


def test_cancel_after_tool_execution_may_start_finishes_indeterminate(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"])
        assert lease is not None
        run = db.get(AgentTurnRun, queued["id"])
        run.status = "invoking_tools"
        run.revision += 1
        db.commit()

        cancelled = agent_turn_service.cancel_turn(
            db,
            queued["id"],
            expected_revision=run.revision,
        )
        assert cancelled["status"] == "cancel_requested"
        repeated = agent_turn_service.cancel_turn(
            db,
            queued["id"],
            expected_revision=cancelled["revision"],
        )
        assert repeated["status"] == "cancel_requested"
        assert repeated["revision"] == cancelled["revision"]
        assert db.get(AgentTurnRun, queued["id"]).result_document[
            "cancellation_requires_reconciliation"
        ] is True
        assert agent_turn_service.finalize_turn(
            db,
            queued["id"],
            lease=lease,
            status="succeeded",
            result={"answer": "late result"},
        )
        finished = agent_turn_service.get_turn(db, queued["id"])
        assert finished["status"] == "indeterminate"
        assert finished["error"]["code"] == "agent_turn_cancellation_indeterminate"


def test_cancel_after_tool_execution_stays_indeterminate_when_lease_expires(
    turn_database,
) -> None:
    now = datetime.now(timezone.utc)
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"], as_of=now)
        assert lease is not None
        run = db.get(AgentTurnRun, queued["id"])
        run.status = "invoking_tools"
        run.revision += 1
        db.commit()
        cancelled = agent_turn_service.cancel_turn(
            db,
            queued["id"],
            expected_revision=run.revision,
        )
        assert cancelled["status"] == "cancel_requested"
        run = db.get(AgentTurnRun, queued["id"])
        run.lease_expires_at = now - timedelta(seconds=1)
        db.commit()

        assert agent_turn_service.claim_turn(
            db,
            queued["id"],
            as_of=now + timedelta(seconds=1),
        ) is None
        finished = agent_turn_service.get_turn(db, queued["id"])
        assert finished["status"] == "indeterminate"
        assert finished["error"]["code"] == "agent_turn_cancellation_indeterminate"


def test_fenced_transition_does_not_overwrite_cancel_requested(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"])
        assert lease is not None
        run = db.get(AgentTurnRun, queued["id"])
        run.status = "cancel_requested"
        run.cancel_requested_at = datetime.now(timezone.utc)
        run.revision += 1
        db.commit()

        assert not agent_turn_service.transition_claimed_turn(
            db,
            queued["id"],
            lease=lease,
            status="planning",
        )
        current = db.get(AgentTurnRun, queued["id"])
        assert current.status == "cancel_requested"


def test_unknown_executor_error_is_safe_and_indeterminate(turn_database) -> None:
    class VendorFailure(RuntimeError):
        code = "vendor_connection_failed"
        message = "postgresql://private-user:private-password@internal-db/customer"

    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

    def execute(*_args, **_kwargs):
        raise VendorFailure(VendorFailure.message)

    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert agent_turn_service.process_next_turn(execute)

    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assistant = db.get(Message, run.assistant_message_id)
        assert run.status == "indeterminate"
        assert run.error_code == "agent_turn_result_indeterminate"
        assert "private-password" not in run.error_message
        assert "private-password" not in assistant.content


def test_runtime_contract_error_is_safe_and_retryable(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

    def execute(*_args, **_kwargs):
        raise agents.agent_runtime_adapter.AgentRuntimeAdapterError(
            "runtime_input_contract_unsatisfied",
            "上传内容未满足所选能力的基础输入契约",
        )

    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert agent_turn_service.process_next_turn(execute)

    with _database(turn_database) as db:
        run = db.get(AgentTurnRun, queued["id"])
        assistant = db.get(Message, run.assistant_message_id)
        assert run.status == "failed"
        assert run.error_code == "runtime_input_contract_unsatisfied"
        assert run.error_message == "上传内容未满足所选能力的基础输入契约"
        assert assistant.content == "处理失败：上传内容未满足所选能力的基础输入契约"


def test_agent_executor_does_not_persist_unknown_exception_text(turn_database) -> None:
    private_error = "postgresql://private-user:private-password@internal-db/customer"
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        runtime_context = Mock()
        runtime_context.build_tools.return_value = []
        with (
            patch.object(agents, "_authorization_context", return_value=runtime_context),
            patch.object(agents, "_agent_readiness_missing", return_value=[]),
            patch.object(agents.llm_service, "routable_configs", return_value=[Mock()]),
            patch.object(agents.agent_engine, "ontology_summary_for", return_value=""),
            patch.object(agents.agent_runtime_adapter, "input_snapshot", return_value={}),
            patch.object(agents.agent_runtime_adapter, "evidence_snapshot", return_value=[]),
            patch.object(agents.agent_engine, "run_agent", side_effect=RuntimeError(private_error)),
        ):
            with pytest.raises(RuntimeError):
                agents.invoke_agent_once(
                    "turn-agent",
                    message="请分析本次资料",
                    conversation_id=queued["conversation_id"],
                    db=db,
                    user_message_id=queued["user_message_id"],
                    assistant_message_id=queued["assistant_message_id"],
                )
        assistant = db.get(Message, queued["assistant_message_id"])
        assert "private-password" not in assistant.content
        assert "Agent Turn 处理失败" in assistant.content


def test_agent_executor_reauthorizes_in_new_session_before_tool_execution(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(db, queued["id"])
        assert lease is not None
        assert agent_turn_service.transition_claimed_turn(
            db,
            queued["id"],
            lease=lease,
            status="planning",
        )

        runtime_context = Mock()
        runtime_context.build_tools.return_value = []
        boundary_sessions = []
        continued_after_tool_boundary = False
        llm_transaction_states: list[bool] = []

        def fence_session_factory():
            session = turn_database()
            boundary_sessions.append(session)
            return session

        def fake_run_agent(*_args, before_llm_call=None, **_kwargs):
            nonlocal continued_after_tool_boundary
            assert before_llm_call is not None
            before_llm_call()
            llm_transaction_states.append(db.in_transaction())
            with turn_database() as revoke_db:
                user = revoke_db.get(User, "turn-user")
                user.status = "disabled"
                revoke_db.commit()
            yield {
                "type": "tool_call",
                "data": {"id": "blocked", "name": "invoke_capability", "arguments": {}},
            }
            continued_after_tool_boundary = True
            yield {"type": "token", "data": "must not be emitted"}

        with (
            patch.object(agents, "SessionLocal", fence_session_factory),
            patch.object(agents, "_authorization_context", return_value=runtime_context),
            patch.object(agents, "_agent_readiness_missing", return_value=[]),
            patch.object(agents.llm_service, "routable_configs", return_value=[Mock()]),
            patch.object(agents.agent_engine, "ontology_summary_for", return_value=""),
            patch.object(agents.agent_runtime_adapter, "input_snapshot", return_value={}),
            patch.object(agents.agent_runtime_adapter, "evidence_snapshot", return_value=[]),
            patch.object(agents.agent_engine, "run_agent", side_effect=fake_run_agent),
        ):
            with pytest.raises(RuntimeError, match="Agent Turn 执行授权已失效"):
                agents.invoke_agent_once(
                    "turn-agent",
                    message="请分析本次资料",
                    conversation_id=queued["conversation_id"],
                    db=db,
                    user_message_id=queued["user_message_id"],
                    assistant_message_id=queued["assistant_message_id"],
                    turn_run_id=queued["id"],
                    turn_lease_token=lease.token,
                    turn_lease_generation=lease.generation,
                )

        assert llm_transaction_states == [False]
        assert not continued_after_tool_boundary
        assert len(boundary_sessions) >= 2
        assert len({id(session) for session in boundary_sessions}) == len(
            boundary_sessions
        )


def test_retry_persists_parent_lineage_in_one_commit(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        run = db.get(AgentTurnRun, queued["id"])
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.revision += 1
        db.commit()

        with patch.object(db, "commit", wraps=db.commit) as commit:
            retried = agent_turn_service.retry_turn(
                db,
                run.id,
                expected_revision=run.revision,
                idempotency_key="retry-one-commit",
            )

        child = db.get(AgentTurnRun, retried["id"])
        assert commit.call_count == 1
        assert child.parent_run_id == run.id


def test_parent_turn_allows_only_one_direct_retry(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        run = db.get(AgentTurnRun, queued["id"])
        run.status = "failed"
        run.error_code = "test_failure"
        run.error_message = "test"
        run.finished_at = datetime.now(timezone.utc)
        run.revision += 1
        db.commit()

        first = agent_turn_service.retry_turn(
            db,
            run.id,
            expected_revision=run.revision,
            idempotency_key="retry-only-child",
        )
        replay = agent_turn_service.retry_turn(
            db,
            run.id,
            expected_revision=run.revision,
            idempotency_key="retry-only-child",
        )
        assert replay["id"] == first["id"]
        with pytest.raises(agent_turn_service.AgentTurnConflict):
            agent_turn_service.retry_turn(
                db,
                run.id,
                expected_revision=run.revision,
                idempotency_key="retry-second-child",
            )


def test_active_turn_blocks_conversation_deletion(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        with pytest.raises(HTTPException) as blocked:
            agents.delete_conversation(queued["conversation_id"], db)
        assert blocked.value.status_code == 409
        assert db.get(AgentTurnRun, queued["id"]) is not None


def test_http_accepts_immediately_and_sse_resumes_from_persistent_revision(
    turn_database,
) -> None:
    app = FastAPI()
    app.include_router(agent_turns.router, prefix="/api")

    def tenant_db_override():
        with _database(turn_database) as db:
            yield db

    app.dependency_overrides[get_tenant_db] = tenant_db_override
    with TestClient(app) as client:
        accepted = client.post(
            "/api/agents/turn-agent/turns",
            json={
                "message": "立即受理",
                "environment": "dev",
                "idempotency_key": "http-turn-1",
            },
        )
        assert accepted.status_code == 202
        run = accepted.json()
        assert run["status"] == "accepted"

        cancelled = client.post(
            f"/api/agent-turns/{run['id']}/cancel",
            json={"expected_revision": run["revision"]},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        with patch.object(agent_turns, "SessionLocal", turn_database):
            resumed = client.get(
                f"/api/agent-turns/{run['id']}/events",
                headers={"Last-Event-ID": "1"},
            )
        assert resumed.status_code == 200
        assert "id: 2" in resumed.text
        assert '"type":"cancelled"' in resumed.text
    assert "data: [DONE]" in resumed.text


def test_legacy_chat_is_a_durable_sse_observer_without_inline_llm(
    turn_database,
) -> None:
    app = FastAPI()
    app.include_router(agent_turns.router, prefix="/api")

    def tenant_db_override():
        with _database(turn_database) as db:
            yield db

    observed: dict[str, str] = {}

    def finite_events(run_id: str, **_kwargs):
        with _database(turn_database) as db:
            run = db.get(AgentTurnRun, run_id)
            assert run is not None
            assert run.status == "accepted"
            observed["run_id"] = run.id
            observed["idempotency_key"] = run.idempotency_key
        yield ": accepted\n\n"
        yield "data: [DONE]\n\n"

    inline_llm = Mock(side_effect=AssertionError("legacy HTTP must not invoke the LLM"))
    app.dependency_overrides[get_tenant_db] = tenant_db_override
    with (
        TestClient(app) as client,
        patch.object(
            agent_turn_service,
            "require_legacy_chat_readiness",
            return_value=None,
        ),
        patch.object(agent_turns, "_legacy_chat_events", finite_events),
        patch.object(agents.agent_engine, "run_agent", inline_llm),
    ):
        response = client.post(
            "/api/agents/turn-agent/chat",
            json={"message": "兼容入口立即落库", "environment": "dev"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-agent-turn-id"] == observed["run_id"]
    assert response.headers["location"] == f"/api/agent-turns/{observed['run_id']}"
    assert response.headers["deprecation"] == "true"
    assert response.headers["link"] == (
        '</api/agents/turn-agent/turns>; rel="successor-version"'
    )
    assert observed["idempotency_key"].startswith("legacy-chat:")
    assert len(observed["idempotency_key"]) <= 180
    inline_llm.assert_not_called()


def test_closing_legacy_chat_observer_does_not_cancel_durable_turn(
    turn_database,
) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())

    with patch.object(agent_turns, "SessionLocal", turn_database):
        observer = agent_turns._legacy_chat_events(
            queued["id"],
            tenant_id="turn-tenant",
            user_id="turn-user",
            auth_session_id="",
        )
        assert next(observer).startswith(": durable-agent-turn-accepted")
        observer.close()

    with _database(turn_database) as db:
        assert db.get(AgentTurnRun, queued["id"]).status == "accepted"

    executor = Mock(
        return_value={
            "answer": "断流后仍由后台完成",
            "conversation_id": queued["conversation_id"],
            "assistant_message_id": queued["assistant_message_id"],
        }
    )
    with patch.object(agent_turn_service, "SessionLocal", turn_database):
        assert agent_turn_service.process_turn(queued["id"], executor)
    executor.assert_called_once()
    with _database(turn_database) as db:
        assert db.get(AgentTurnRun, queued["id"]).status == "succeeded"


def test_legacy_chat_translates_only_persisted_terminal_state() -> None:
    success = {
        "status": "succeeded",
        "result": {
            "answer": "持久结果",
            "input_snapshot": {"runtime": {"definition_hash": "a" * 64}},
            "citations": [{"citation_id": "C1"}],
            "evidence_refs": [{"kind": "receipt"}],
        },
        "error": None,
    }
    assistant = {
        "content": "持久结果",
        "tool_calls": [{"id": "call-1", "name": "lookup"}],
        "tool_results": [{"id": "call-1", "result": "有界结果"}],
        "citations": [{"citation_id": "C1"}],
        "evidence_refs": [{"kind": "receipt"}],
    }

    body = "".join(agent_turns._legacy_terminal_frames(success, assistant))
    assert '"type":"runtime_decision"' in body
    assert '"type":"tool_call"' in body
    assert '"type":"tool_result"' in body
    assert '"type":"citations"' in body
    assert '"type":"evidence_refs"' in body
    assert '"type":"token","data":"持久结果"' in body
    assert body.endswith('"type":"done","data":"持久结果"}\n\n')

    failed_body = "".join(
        agent_turns._legacy_terminal_frames(
            {
                "status": "failed",
                "result": {},
                "error": {"code": "agent_turn_failed", "message": "安全错误"},
            },
            None,
        )
    )
    assert '"type":"error","data":"安全错误"' in failed_body


def test_sse_stops_safely_when_browser_session_is_revoked(turn_database) -> None:
    with _database(turn_database) as db:
        queued = agent_turn_service.enqueue_turn(db, "turn-agent", _payload())
        db.add(
            AuthSession(
                id="revoked-session",
                user_id="turn-user",
                token_hash="a" * 64,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(agent_turns.router, prefix="/api")

    def tenant_db_override(request: Request):
        request.state.auth_session_id = "revoked-session"
        with _database(turn_database) as db:
            yield db

    app.dependency_overrides[get_tenant_db] = tenant_db_override
    with TestClient(app) as client, patch.object(agent_turns, "SessionLocal", turn_database):
        response = client.get(f"/api/agent-turns/{queued['id']}/events")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "agent_turn_unavailable" in response.text
    assert "revoked-session" not in response.text
