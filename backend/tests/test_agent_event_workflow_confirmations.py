from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ActionExecutionLog,
    Agent,
    BusinessScenario,
    Conversation,
    EventEnvelope,
    LLMConfig,
    Message,
    OntologyEvent,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowRun,
)
from app.routers import agents
from app.schemas import AgentToolConfirmationRequest
from app.services import (
    agent_confirmation_service,
    agent_engine,
    permission_service,
    runtime_definition_service,
)


class AgentEventWorkflowConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-agent-confirm", name="Agent 确认租户")
        self.user = User(
            id="user-agent-confirm",
            tenant_id=self.tenant.id,
            email="agent-confirm@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-agent-confirm",
            tenant_id=self.tenant.id,
            name="Agent 确认场景",
            status="active",
        )
        self.event = OntologyEvent(
            id="event-agent-confirm",
            scenario=self.scenario,
            name="风险已发现",
            payload_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            enabled=True,
        )
        self.workflow = OntologyWorkflow(
            id="workflow-agent-confirm",
            scenario=self.scenario,
            name="风险处置流程",
            trigger_type="event",
            trigger_config={"event_id": self.event.id},
            nodes=[
                {"id": "start", "type": "start", "data": {}},
                {"id": "end", "type": "end", "data": {}},
            ],
            edges=[{"id": "edge", "source": "start", "target": "end"}],
            status="active",
            enabled=True,
        )
        self.agent = Agent(
            id="agent-confirm",
            tenant_id=self.tenant.id,
            scenario=self.scenario,
            name="风险 Agent",
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": []},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": [self.event.id]},
                "workflows": {"mode": "explicit", "selected_ids": [self.workflow.id]},
            },
        )
        self.conversation = Conversation(
            id="conversation-confirm",
            agent=self.agent,
            created_by_user_id=self.user.id,
            title="风险处置",
        )
        self.message = Message(
            id="message-confirm",
            conversation=self.conversation,
            role="assistant",
            content="预演完成。",
            stream_finalized=True,
            tool_calls=[],
            tool_results=[],
        )
        self.db.add_all(
            [
                self.tenant,
                self.user,
                self.scenario,
                self.event,
                self.workflow,
                self.agent,
                self.conversation,
                self.message,
            ]
        )
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _preview(self, target: str) -> dict:
        definition = runtime_definition_service.resolve_active(self.db, self.scenario)
        self.db.info["llm_trace_context"] = {
            "assistant_message_id": self.message.id,
            "correlation_id": f"correlation-{target}",
        }
        self.db.info["action_audit_context"] = {
            "agent_id": self.agent.id,
            "llm_config_id": None,
            "model_name": "test-tool-model",
        }
        if target == "event":
            tool_name = "prepare_event_publish"
            preview = agent_confirmation_service.preview_event_publish(
                self.db,
                definition.events[self.event.id],
                {"project_id": "P-001"},
                runtime_definition=definition,
            )
        else:
            tool_name = "execute_workflow"
            preview = agent_confirmation_service.preview_workflow_run(
                self.db,
                definition.workflows[self.workflow.id],
                {"project_id": "P-001"},
                runtime_definition=definition,
            )
        self.db.refresh(self.message)
        call_id = f"call-{target}"
        self.message.tool_calls = [
            {
                "id": call_id,
                "name": tool_name,
                "arguments": preview["result"]["plan"].get("params")
                or preview["result"]["plan"].get("payload")
                or {},
            }
        ]
        self.message.tool_results = [
            {
                "id": call_id,
                "name": tool_name,
                "result": json.dumps(preview, ensure_ascii=False),
            }
        ]
        self.db.commit()
        return preview

    def _payload(self, preview: dict, **overrides) -> AgentToolConfirmationRequest:
        values = {
            "conversation_id": self.conversation.id,
            "correlation_id": preview["correlation_id"],
            "expected_environment": preview["environment"],
            "expected_definition_snapshot_id": preview["definition_snapshot_id"],
            "expected_release_id": preview["release_id"],
            "expected_definition_hash": preview["definition_hash"],
        }
        values.update(overrides)
        return AgentToolConfirmationRequest(**values)

    def _confirm(self, preview: dict, **overrides) -> dict:
        return agents.confirm_agent_tool_preview(
            self.agent.id,
            preview["log_id"],
            self._payload(preview, **overrides),
            self.db,
        )

    def test_agent_tools_create_durable_previews_without_side_effects(self) -> None:
        self.db.info["llm_trace_context"] = {
            "assistant_message_id": self.message.id,
            "correlation_id": "correlation-agent-tools",
        }
        self.db.info["action_audit_context"] = {
            "agent_id": self.agent.id,
            "llm_config_id": None,
            "model_name": "test-tool-model",
        }
        context = agent_engine.AgentContext(
            self.db,
            self.agent,
            LLMConfig(name="工具测试模型"),
        )

        event_preview = json.loads(context.execute_tool(
            "prepare_event_publish",
            {"event_id": self.event.id, "payload": {"project_id": "P-001"}},
        ))
        workflow_preview = json.loads(context.execute_tool(
            "execute_workflow",
            {"workflow_id": self.workflow.id, "params": {"project_id": "P-001"}},
        ))

        self.assertEqual(event_preview["status"], "confirmation_required")
        self.assertEqual(workflow_preview["status"], "confirmation_required")
        self.assertEqual(self.db.query(ActionExecutionLog).count(), 2)
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)
        for preview in (event_preview, workflow_preview):
            log = self.db.get(ActionExecutionLog, preview["log_id"])
            self.assertEqual(log.agent_id, self.agent.id)
            self.assertEqual(log.agent_message_id, self.message.id)

    def test_event_confirmation_publishes_and_queues_once_then_replays(self) -> None:
        preview = self._preview("event")
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

        response = self._confirm(preview)

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["confirmation_type"], "event")
        self.assertEqual(len(response["result"]["queued_workflow_run_ids"]), 1)
        self.assertEqual(self.db.query(EventEnvelope).count(), 1)
        self.assertEqual(self.db.query(WorkflowRun).count(), 1)
        self.assertEqual(
            self.db.query(ActionExecutionLog).filter_by(parent_action_log_id=preview["log_id"]).count(),
            1,
        )
        self.db.refresh(self.message)
        stored = json.loads(self.message.tool_results[0]["result"])
        self.assertEqual(stored["log_id"], response["log_id"])
        self.assertEqual(stored["parent_preview_log_id"], preview["log_id"])

        replay = self._confirm(preview)
        self.assertEqual(replay["status"], "idempotent_replay")
        self.assertEqual(replay["original_status"], "success")
        self.assertEqual(self.db.query(EventEnvelope).count(), 1)
        self.assertEqual(self.db.query(WorkflowRun).count(), 1)

    def test_workflow_confirmation_enqueues_pinned_run_and_returns_task_link(self) -> None:
        preview = self._preview("workflow")

        response = self._confirm(preview)

        run = self.db.get(WorkflowRun, response["result"]["workflow_run"]["id"])
        self.assertIsNotNone(run)
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.input_params, {"project_id": "P-001"})
        self.assertEqual(run.definition_hash, preview["definition_hash"])
        self.assertEqual(response["result"]["task_url"], f"/tasks?task={run.id}")

    def test_confirmation_rejects_unfinished_stream_wrong_conversation_and_hash(self) -> None:
        preview = self._preview("event")
        self.message.stream_finalized = False
        self.db.commit()
        with self.assertRaises(HTTPException) as unfinished:
            self._confirm(preview)
        self.assertEqual(unfinished.exception.status_code, 409)
        self.assertIn("仍在生成", str(unfinished.exception.detail))

        self.message.stream_finalized = True
        other = Conversation(
            id="conversation-other",
            agent=self.agent,
            created_by_user_id=self.user.id,
            title="其他对话",
        )
        self.db.add(other)
        self.db.commit()
        with self.assertRaises(HTTPException) as wrong_conversation:
            self._confirm(preview, conversation_id=other.id)
        self.assertEqual(wrong_conversation.exception.status_code, 409)

        with self.assertRaises(HTTPException) as wrong_hash:
            self._confirm(preview, expected_definition_hash="0" * 64)
        self.assertEqual(wrong_hash.exception.status_code, 409)
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

    def test_confirmation_rejects_definition_change_and_browser_parameters(self) -> None:
        preview = self._preview("event")
        self.event.description = "预演后发生变化"
        self.db.commit()

        with self.assertRaises(HTTPException) as changed:
            self._confirm(preview)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertIn("定义在预演后已变化", str(changed.exception.detail))
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

        with self.assertRaises(ValidationError):
            AgentToolConfirmationRequest(
                conversation_id=self.conversation.id,
                correlation_id=preview["correlation_id"],
                expected_environment=preview["environment"],
                expected_definition_hash=preview["definition_hash"],
                payload={"project_id": "TAMPERED"},
            )

    def test_confirmation_rejects_agent_capability_revocation(self) -> None:
        for target in ("event", "workflow"):
            preview = self._preview(target)
            scope = dict(self.agent.capability_scope)
            category = "events" if target == "event" else "workflows"
            scope[category] = {"mode": "explicit", "selected_ids": []}
            self.agent.capability_scope = scope
            self.db.commit()

            with self.assertRaises(HTTPException) as revoked:
                self._confirm(preview)
            self.assertEqual(revoked.exception.status_code, 409)
            self.assertIn("已不在当前 Agent 的授权范围", str(revoked.exception.detail))
            self.assertEqual(self.db.query(EventEnvelope).count(), 0)
            self.assertEqual(self.db.query(WorkflowRun).count(), 0)

            scope[category] = {
                "mode": "explicit",
                "selected_ids": [self.event.id if target == "event" else self.workflow.id],
            }
            self.agent.capability_scope = scope
            self.db.commit()


if __name__ == "__main__":
    unittest.main()
