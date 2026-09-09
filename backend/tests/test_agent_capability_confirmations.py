from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import func, select

from app.models import CapabilityInvocation, Conversation, Message, OntologyEvent, OntologyWorkflow, WorkflowRun
from app.services import agent_capability_confirmation_service as service
from app.services import agent_runtime_adapter, agent_turn_payload_service
from app.services.capability_invoker import CapabilityInvocationError
from .test_agent_runtime_adapter import db, _world


def _preview(db, *, nested: bool = False):
    tenant, user, scenario, llm, _, agent = _world(db)
    event = OntologyEvent(id="confirmation-event", scenario_id=scenario.id, name="Confirmed event", enabled=True)
    workflow = OntologyWorkflow(
        id="confirmation-workflow", scenario_id=scenario.id, name="Review workflow",
        nodes=[
            {"id": "start", "type": "start", "data": {"name": "Start"}},
            {"id": "event", "type": "event", "data": {"event_id": event.id, "payload": {"request": "{{params.request}}"}}},
            {"id": "end", "type": "end", "data": {"name": "End", "config": {"summary": "{{params.request}}"}}},
        ],
        edges=[{"id": "edge-1", "source": "start", "target": "event", "label": ""}, {"id": "edge-2", "source": "event", "target": "end", "label": ""}],
        status="active", enabled=True,
    )
    scope = copy.deepcopy(agent.capability_scope)
    scope["workflows"] = {"mode": "explicit", "selected_ids": [workflow.id]}
    agent.capability_scope = scope
    conversation = Conversation(id="confirmation-conversation", agent_id=agent.id, created_by_user_id=user.id)
    message = Message(id="confirmation-message", conversation_id=conversation.id, role="assistant", content="Preview", stream_finalized=True)
    db.add_all([event, workflow, conversation, message])
    db.commit()
    db.info["llm_trace_context"] = {"assistant_message_id": message.id, "correlation_id": "confirmation-correlation"}
    db.info["action_audit_context"] = {"agent_id": agent.id}
    runtime = agent_runtime_adapter.build_runtime_context(
        db, agent, llm, 
        turn_input=agent_runtime_adapter.AgentTurnInput(idempotency_key="confirmation-turn"),
    )
    value = {"text": "private-input-for-confirmation-test", "items": [{"number": 1}]} if nested else "private-input-for-confirmation-test"
    result = runtime.execute_tool("invoke_capability", {"kind": "workflow", "key": workflow.id, "inputs": {"request": value}})
    document = json.loads(result)
    assert document.get("status") == "awaiting_confirmation", document
    message.tool_results = [{"name": "invoke_capability", "result": result}]
    db.commit()
    return agent, workflow, message, db.get(CapabilityInvocation, document["invocation_id"])


@pytest.mark.parametrize("nested", [False, True])
def test_browser_confirmation_restores_encrypted_input_and_replays_one_task(db, nested):
    agent, _, message, invocation = _preview(db, nested=nested)
    assert "confirmation_token" not in json.dumps(message.tool_results)
    assert "private-input-for-confirmation-test" not in json.dumps(invocation.request_document)
    view = service.get_receipt(db, agent.id, invocation.id, message.id)
    assert view["can_confirm"] is True
    assert "confirmation_token" not in json.dumps(view)
    first = service.confirm(db, agent.id, invocation.id, message.id)
    db.commit()
    replay = service.confirm(db, agent.id, invocation.id, message.id)
    db.commit()
    assert first["status"] == replay["status"] == "running"
    assert first["workflow_run_id"] == replay["workflow_run_id"]
    assert db.scalar(select(func.count(WorkflowRun.id))) == 1
    assert service.get_receipt(db, agent.id, invocation.id, message.id)["can_confirm"] is False


def test_unfinalized_or_unlinked_message_cannot_confirm(db):
    agent, _, message, invocation = _preview(db)
    message.stream_finalized = False
    db.flush()
    assert not service.get_receipt(db, agent.id, invocation.id, message.id)["can_confirm"]
    with pytest.raises(service.AgentCapabilityConfirmationError):
        service.confirm(db, agent.id, invocation.id, message.id)
    message.stream_finalized = True
    message.tool_results = []
    db.flush()
    with pytest.raises(service.AgentCapabilityConfirmationError) as error:
        service.confirm(db, agent.id, invocation.id, message.id)
    assert error.value.status_code == 404
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0


def test_definition_change_and_scope_revocation_reject_confirmation(db):
    agent, workflow, message, invocation = _preview(db)
    workflow.description = "Changed after preview"
    db.flush()
    assert not service.get_receipt(db, agent.id, invocation.id, message.id)["can_confirm"]
    with pytest.raises(CapabilityInvocationError):
        service.confirm(db, agent.id, invocation.id, message.id)
    scope = copy.deepcopy(agent.capability_scope)
    scope["workflows"]["selected_ids"] = []
    agent.capability_scope = scope
    db.flush()
    with pytest.raises(service.AgentCapabilityConfirmationError) as error:
        service.confirm(db, agent.id, invocation.id, message.id)
    assert error.value.status_code == 403
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0


def test_changed_ciphertext_is_rejected_before_execution(db):
    agent, _, message, invocation = _preview(db)
    document = copy.deepcopy(invocation.request_document)
    sealed = document["agent_confirmation_input_v1"]["envelope"]
    text = sealed["ciphertext"]
    sealed["ciphertext"] = ("A" if text[0] != "A" else "B") + text[1:]
    invocation.request_document = document
    db.flush()
    with pytest.raises(agent_turn_payload_service.AgentTurnPayloadError):
        service.confirm(db, agent.id, invocation.id, message.id)
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0


def test_another_conversation_owner_cannot_read_receipt(db):
    agent, _, message, invocation = _preview(db)
    conversation = db.get(Conversation, message.conversation_id)
    conversation.created_by_user_id = "another-owner"
    db.flush()
    with pytest.raises(service.AgentCapabilityConfirmationError) as error:
        service.get_receipt(db, agent.id, invocation.id, message.id)
    assert error.value.status_code == 404
