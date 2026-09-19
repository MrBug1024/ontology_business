"""Independent PostgreSQL regressions for investigation authorization and stopping."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session
from app.distillation_conversation_models import DistillationConversationTurn as Turn
from app.distillation_conversation_schemas import TurnCreate
from app.distillation_schemas import ProjectCreate, ProjectUpdate
from app.models import LLMConfig, User
from app.services import distillation_conversation_service as conversation
from app.services import distillation_conversation_worker as worker
from app.services import distillation_service


def _enqueued(isolated):
    workspace = seed_workspace(isolated.admin_engine)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        # Enqueue freezes a real enabled chat+tool configuration even when this
        # test replaces provider I/O to inspect authorization or atomic state.
        db.add(LLMConfig(tenant_id=workspace["tenant_id"], name="Review fixture model",
            model="synthetic-review-model", capabilities=["chat", "tool"], enabled=True))
        project = distillation_service.create_project(db, ProjectCreate(
            name="Scoped investigation", scenario_id=workspace["scenario_id"]))
        db.commit()
        turn = conversation.enqueue(db, project.id, TurnCreate(
            request_id="initial-question", message="Help identify the beneficiary", expected_revision=1))
        db.commit()
        return workspace, project.id, turn.id


def test_existing_investigation_prevents_moving_history_into_another_scenario(isolated_postgresql):
    workspace, project_id, turn_id = _enqueued(isolated_postgresql)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        try:
            with pytest.raises(HTTPException) as failure:
                distillation_service.update_project(db, project_id, ProjectUpdate(
                    name="Moved investigation", scenario_id=workspace["other_scenario_id"], expected_revision=1))
            assert failure.value.status_code == 409
        finally:
            db.rollback()
            conversation.cancel(db, project_id, turn_id)
            db.commit()


def test_completed_human_question_and_waiting_status_are_visible_in_one_commit(isolated_postgresql, monkeypatch):
    _workspace, _project_id, turn_id = _enqueued(isolated_postgresql)
    snapshots = []

    def after_commit(_session):
        with isolated_postgresql.runtime_engine.connect() as connection:
            row = connection.execute(select(Turn.status, Turn.steps).where(Turn.id == turn_id)).one()
            if any(step["tool_name"] == "ask_human" and step["status"] == "succeeded" for step in row.steps):
                snapshots.append(row.status)

    def session_factory():
        db = Session(isolated_postgresql.runtime_engine)
        event.listen(db, "after_commit", after_commit)
        return db

    monkeypatch.setattr(worker, "_model_call", lambda *_args: {"content": "", "tool_calls": [{
        "id": "ask-beneficiary", "function": {"name": "ask_human", "arguments": {
            "message": "Please clarify the beneficiary before we proceed.", "questions": [{
                "id": "beneficiary", "title": "Beneficiary", "question": "Who needs the result?",
                "reason": "We need a measurable business outcome."}]}}}]})
    assert worker.process_next_turn(session_factory=session_factory)
    assert snapshots and set(snapshots) == {"waiting"}


def test_disabled_initiator_cannot_recover_as_anonymous_or_owner(isolated_postgresql, monkeypatch):
    workspace, _project_id, turn_id = _enqueued(isolated_postgresql)
    with isolated_postgresql.admin_engine.begin() as connection:
        connection.execute(update(User).where(User.id == workspace["user_id"]).values(status="disabled"))
    monkeypatch.setattr(worker, "_model_call", lambda *_args: pytest.fail("Disabled actor reached model I/O"))
    assert worker.process_next_turn(session_factory=lambda: Session(isolated_postgresql.runtime_engine))
    with isolated_postgresql.runtime_engine.connect() as connection:
        assert connection.scalar(select(Turn.status).where(Turn.id == turn_id)) == "failed"


def test_pasted_json_credentials_are_rejected_before_queuing(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        project = distillation_service.create_project(db, ProjectCreate(name="No chat credentials"))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            conversation.enqueue(db, project.id, TurnCreate(request_id="quoted-secret", expected_revision=1,
                message='{"password":"synthetic-private-value"}'))
        assert failure.value.status_code == 422
        assert db.scalar(select(Turn.id).where(Turn.project_id == project.id)) is None
