from __future__ import annotations

import json
import unittest
from unittest.mock import patch

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
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyRelease,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from app.routers import agents
from app.schemas import AgentToolConfirmationRequest
from app.services import (
    agent_confirmation_service,
    agent_engine,
    capability_readiness_service,
    operations_service,
    permission_service,
    release_service,
    runtime_definition_service,
    workflow_service,
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
        self.branch = OntologyBranch(
            id="branch-agent-confirm",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="main",
            created_by_user_id=self.user.id,
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
                self.branch,
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
        self.release = self._publish_current_snapshot(
            "snapshot-agent-confirm-a",
            "release-agent-confirm-a",
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _preview(self, target: str, *, definition=None) -> dict:
        definition = definition or runtime_definition_service.resolve_execution(
            self.db, self.scenario
        )
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

    def _publish_current_snapshot(
        self,
        snapshot_id: str,
        release_id: str,
    ) -> OntologyRelease:
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        snapshot = OntologySnapshot(
            id=snapshot_id,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=self.branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.user.id,
        )
        self.db.add(snapshot)
        self.db.flush()
        self.branch.base_snapshot_id = self.branch.base_snapshot_id or snapshot.id
        self.branch.head_snapshot_id = snapshot.id
        previous = getattr(self, "release", None)
        if previous is not None:
            previous.status = "superseded"
        release = OntologyRelease(
            id=release_id,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=self.branch.id,
            snapshot_id=snapshot.id,
            environment="dev",
            status="released",
            created_by_user_id=self.user.id,
        )
        self.db.add(release)
        self.db.commit()
        return release

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

    def _configure_workflow_approval(
        self,
        *,
        node_name: str = "经理审批",
        action_id: str | None = None,
    ) -> None:
        nodes = [
            {"id": "start", "type": "start", "data": {"name": "开始"}},
            {
                "id": "manager-approval",
                "type": "approval",
                "data": {
                    "name": node_name,
                    "instructions": "请核对影响范围后决定是否批准。",
                    "timeout_seconds": 120,
                },
            },
        ]
        edges = [
            {
                "id": "start-to-approval",
                "source": "start",
                "target": "manager-approval",
            }
        ]
        previous = "manager-approval"
        if action_id:
            nodes.append(
                {
                    "id": "automatic-action",
                    "type": "action",
                    "data": {
                        "name": "自动操作",
                        "action_id": action_id,
                        "params": {},
                    },
                }
            )
            edges.append(
                {
                    "id": "approval-to-action",
                    "source": previous,
                    "target": "automatic-action",
                }
            )
            previous = "automatic-action"
        nodes.append({"id": "end", "type": "end", "data": {"name": "结束"}})
        edges.append({"id": "to-end", "source": previous, "target": "end"})
        self.workflow.nodes = nodes
        self.workflow.edges = edges
        self.db.commit()
        self.release = self._publish_current_snapshot(
            f"snapshot-agent-approval-{node_name}",
            f"release-agent-approval-{node_name}",
        )

    def _queue_agent_workflow_approval(
        self,
        *,
        node_name: str = "经理审批",
    ) -> tuple[WorkflowRun, WorkflowApprovalRequest]:
        self._configure_workflow_approval(node_name=node_name)
        preview = self._preview("workflow")
        confirmation = self._confirm(preview)
        run = self.db.get(WorkflowRun, confirmation["result"]["workflow_run"]["id"])
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.agent_conversation_id, self.conversation.id)
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        approval = self.db.query(WorkflowApprovalRequest).filter_by(
            workflow_run_id=run.id,
            status="pending",
        ).one()
        return run, approval

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

    def test_live_workflow_confirmation_executes_without_a_release(self) -> None:
        self.db.delete(self.release)
        self.db.commit()
        definition = runtime_definition_service.resolve_authoring(self.db, self.scenario)
        preview = self._preview("workflow", definition=definition)

        response = self._confirm(preview)
        run = self.db.get(WorkflowRun, response["result"]["workflow_run"]["id"])
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(
            run.definition_source,
            runtime_definition_service.LIVE_PINNED_RUN_SOURCE,
        )
        self.assertIsNone(run.release_id)
        self.assertEqual(run.definition_hash, definition.definition_hash)

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")

    def test_live_approval_is_cancelled_when_its_definition_changes(self) -> None:
        self.db.delete(self.release)
        self._configure_workflow_approval()
        self.db.commit()
        definition = runtime_definition_service.resolve_authoring(self.db, self.scenario)
        preview = self._preview("workflow", definition=definition)
        response = self._confirm(preview)
        run = self.db.get(WorkflowRun, response["result"]["workflow_run"]["id"])
        self.assertIsNotNone(run)
        assert run is not None
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        approval = self.db.query(WorkflowApprovalRequest).filter_by(
            workflow_run_id=run.id,
            status="pending",
        ).one()
        self.assertEqual(run.status, "awaiting_approval")

        self.workflow.description = "审批等待期间的定义变更"
        self.db.commit()
        with self.assertRaisesRegex(
            operations_service.PolicyViolation,
            "待审批任务已取消",
        ):
            operations_service.decide_approval(self.db, run, approved=True)

        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "cancelled")
        self.assertIn("运行定义已变化", run.error)
        self.assertEqual(approval.status, "cancelled")

    def test_live_approval_drift_does_not_bypass_approve_permission(self) -> None:
        self.db.delete(self.release)
        self._configure_workflow_approval()
        self.db.commit()
        definition = runtime_definition_service.resolve_authoring(self.db, self.scenario)
        preview = self._preview("workflow", definition=definition)
        response = self._confirm(preview)
        run = self.db.get(WorkflowRun, response["result"]["workflow_run"]["id"])
        self.assertIsNotNone(run)
        assert run is not None
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        approval = self.db.query(WorkflowApprovalRequest).filter_by(
            workflow_run_id=run.id,
            status="pending",
        ).one()

        viewer = User(
            id="viewer-live-approval-drift",
            tenant_id=self.tenant.id,
            email="viewer-live-approval-drift@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(viewer)
        self.db.commit()
        organization = permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=viewer.id,
            role_key="viewer",
        )
        self.workflow.description = "无权审批前的定义变更"
        self.db.info["user_id"] = viewer.id
        self.db.commit()

        with self.assertRaisesRegex(
            operations_service.PolicyViolation,
            "没有审批该工作流的权限",
        ):
            operations_service.decide_approval(self.db, run, approved=True)

        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(approval.status, "pending")

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
        self.release = self._publish_current_snapshot(
            "snapshot-agent-confirm-b",
            "release-agent-confirm-b",
        )

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

    def test_text_confirmation_executes_the_current_event_preview(self) -> None:
        preview = self._preview("event")

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "confirmed")
        self.assertEqual(outcome["preview_log_id"], preview["log_id"])
        self.assertEqual(self.db.query(EventEnvelope).count(), 1)
        self.db.refresh(self.message)
        stored = json.loads(self.message.tool_results[0]["result"])
        self.assertEqual(stored["parent_preview_log_id"], preview["log_id"])

    def test_text_approval_approves_and_resumes_the_agent_workflow_run(self) -> None:
        run, approval = self._queue_agent_workflow_approval()

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认批准 经理审批",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "approved")
        self.assertEqual(outcome["approval_id"], approval.id)
        self.assertEqual(outcome["workflow_run_id"], run.id)
        self.assertEqual(outcome["run_status"], "queued")
        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "queued")
        self.assertEqual(approval.status, "approved")

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")
        self.assertIn("manager-approval", run.approved_node_ids)

    def test_text_approval_rejects_by_exact_workflow_name(self) -> None:
        run, approval = self._queue_agent_workflow_approval()

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认驳回 风险处置流程",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "rejected")
        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "rejected")
        self.assertEqual(approval.status, "rejected")

    def test_text_approval_rejects_cross_conversation_negation_and_ambiguity(self) -> None:
        run, approval = self._queue_agent_workflow_approval()
        other_conversation = Conversation(
            id="conversation-other-approval",
            agent=self.agent,
            created_by_user_id=self.user.id,
            title="其他审批对话",
        )
        self.db.add(other_conversation)
        self.db.commit()

        cross_conversation = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=other_conversation,
            text="确认批准 经理审批",
        )
        negative = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="不批准 经理审批",
        )
        mixed = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认批准或驳回 经理审批",
        )

        self.assertEqual(cross_conversation["status"], "no_pending_approval")
        self.assertEqual(negative["status"], "approval_not_decided")
        self.assertEqual(mixed["status"], "ambiguous_approval")
        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(approval.status, "pending")

    def test_text_approval_requires_one_pending_node_in_the_current_conversation(self) -> None:
        self._configure_workflow_approval()
        definition = runtime_definition_service.resolve_execution(self.db, self.scenario)
        first, _ = operations_service.enqueue_workflow_run(
            self.db,
            self.workflow,
            {"project_id": "P-001"},
            created_by_user_id=self.user.id,
            runtime_definition=definition,
            agent_conversation_id=self.conversation.id,
        )
        second, _ = operations_service.enqueue_workflow_run(
            self.db,
            self.workflow,
            {"project_id": "P-002"},
            created_by_user_id=self.user.id,
            runtime_definition=definition,
            agent_conversation_id=self.conversation.id,
        )
        self.db.commit()
        operations_service.process_available_runs(self.db, limit=2)

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认批准",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "ambiguous_approval")
        self.db.refresh(first)
        self.db.refresh(second)
        self.assertEqual(first.status, "awaiting_approval")
        self.assertEqual(second.status, "awaiting_approval")

    def test_text_approval_requires_the_workflow_approve_permission(self) -> None:
        self._configure_workflow_approval()
        operator = User(
            id="operator-agent-approval",
            tenant_id=self.tenant.id,
            email="operator-agent-approval@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(operator)
        self.db.commit()
        organization = permission_service.organization_for_principal(self.db)
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=operator.id,
            role_key="operator",
        )
        operator_conversation = Conversation(
            id="conversation-operator-approval",
            agent=self.agent,
            created_by_user_id=operator.id,
            title="操作员审批对话",
        )
        self.db.add(operator_conversation)
        self.db.commit()
        self.db.info["user_id"] = operator.id
        definition = runtime_definition_service.resolve_execution(self.db, self.scenario)
        run, _ = operations_service.enqueue_workflow_run(
            self.db,
            self.workflow,
            {"project_id": "P-003"},
            created_by_user_id=operator.id,
            runtime_definition=definition,
            agent_conversation_id=operator_conversation.id,
        )
        self.db.commit()
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        approval = self.db.query(WorkflowApprovalRequest).filter_by(
            workflow_run_id=run.id,
            status="pending",
        ).one()

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=operator_conversation,
            text="确认批准 经理审批",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "approval_failed")
        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(approval.status, "pending")

    def test_explicit_approval_node_pauses_before_an_automatic_action(self) -> None:
        entity = OntologyEntity(
            id="entity-agent-approval-gate",
            scenario=self.scenario,
            name="审批对象",
            is_abstract=True,
        )
        action = OntologyAction(
            id="action-agent-approval-gate",
            scenario=self.scenario,
            entity=entity,
            name="无需单独确认的操作",
            executor_type="sql",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            requires_confirmation=False,
            enabled=True,
        )
        self.db.add_all([entity, action])
        self.db.commit()

        with (
            patch.object(capability_readiness_service, "require_executable"),
            patch.object(workflow_service, "execute_action") as execute_action,
        ):
            self._configure_workflow_approval(action_id=action.id)
            preview = self._preview("workflow")
            confirmation = self._confirm(preview)
            run = self.db.get(
                WorkflowRun,
                confirmation["result"]["workflow_run"]["id"],
            )
            self.assertIsNotNone(run)
            operations_service.process_available_runs(self.db)

        assert run is not None
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        execute_action.assert_not_called()

    def test_text_confirmation_executes_an_action_without_browser_parameters(self) -> None:
        entity = OntologyEntity(
            id="entity-agent-confirm-action",
            scenario=self.scenario,
            name="Project",
            is_abstract=True,
        )
        action = OntologyAction(
            id="action-agent-confirm",
            scenario=self.scenario,
            entity=entity,
            name="Update project state",
            executor_type="sql",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            requires_confirmation=True,
            enabled=True,
        )
        self.db.add_all([entity, action])
        scope = dict(self.agent.capability_scope)
        scope["actions"] = {"mode": "explicit", "selected_ids": [action.id]}
        self.agent.capability_scope = scope
        self.db.commit()
        self.release = self._publish_current_snapshot(
            "snapshot-agent-confirm-action",
            "release-agent-confirm-action",
        )
        definition = runtime_definition_service.resolve_execution(self.db, self.scenario)
        preview = ActionExecutionLog(
            scenario_id=self.scenario.id,
            target_type="action",
            target_id=action.id,
            target_name=action.name,
            input_params={"project_id": "P-001"},
            status="dry_run",
            mode="dry_run",
            environment=definition.environment,
            definition_snapshot_id=definition.snapshot_id,
            release_id=definition.release_id,
            definition_hash=definition.definition_hash,
            definition_source=definition.source,
            actor_type="agent",
            actor_user_id=self.user.id,
            agent_id=self.agent.id,
            correlation_id="text-action-confirm",
            agent_message_id=self.message.id,
            result={"plan": {"action_id": action.id, "action_name": action.name}},
        )
        self.db.add(preview)
        self.db.flush()
        self.message.tool_calls = [{
            "id": "call-action-confirm",
            "name": "execute_action",
            "arguments": {"action_id": action.id, "params": {"project_id": "P-001"}},
        }]
        self.message.tool_results = [{
            "id": "call-action-confirm",
            "name": "execute_action",
            "result": json.dumps({"status": "dry_run", "log_id": preview.id}),
        }]
        self.db.commit()

        with (
            patch.object(capability_readiness_service, "require_executable"),
            patch.object(workflow_service, "_dispatch_executor", return_value=({"updated": True}, [])),
        ):
            outcome = agent_confirmation_service.confirm_text_reply(
                self.db,
                agent=self.agent,
                conversation=self.conversation,
                text="确认执行 Update project state",
            )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "confirmed")
        self.assertEqual(outcome["preview_log_id"], preview.id)
        self.assertEqual(
            self.db.query(ActionExecutionLog)
            .filter_by(parent_action_log_id=preview.id, status="success")
            .count(),
            1,
        )
        self.db.refresh(self.message)
        stored = json.loads(self.message.tool_results[0]["result"])
        self.assertEqual(stored["parent_action_log_id"], preview.id)

    def test_text_confirmation_does_not_treat_an_ambiguous_phrase_as_approval(self) -> None:
        preview = self._preview("event")

        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认当前状态",
        )

        self.assertIsNone(outcome)
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)
        self.assertEqual(self.db.get(ActionExecutionLog, preview["log_id"]).status, "dry_run")

    def test_text_confirmation_requires_an_exact_target_and_rejects_negation(self) -> None:
        preview = self._preview("event")
        preview_log = self.db.get(ActionExecutionLog, preview["log_id"])
        assert preview_log is not None
        preview_log.target_name = "Delete Target"
        self.db.commit()

        self.assertTrue(
            agent_confirmation_service._matches_confirmation_target(
                preview_log, "  delete   target  "
            )
        )
        self.assertTrue(
            agent_confirmation_service._matches_confirmation_target(
                preview_log, "EVENT-AGENT-CONFIRM"
            )
        )
        self.assertFalse(
            agent_confirmation_service._matches_confirmation_target(preview_log, "delete")
        )

        partial = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认发布 Delete",
        )
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial["status"], "ambiguous")
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

        preview_log.target_name = "删除"
        self.db.commit()
        for negative_text in ("确认执行不删除", "确认执行 不删除"):
            negative = agent_confirmation_service.confirm_text_reply(
                self.db,
                agent=self.agent,
                conversation=self.conversation,
                text=negative_text,
            )
            self.assertIsNone(negative)
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

        exact = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认发布 EVENT-AGENT-CONFIRM",
        )
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact["status"], "confirmed")
        self.assertEqual(self.db.query(EventEnvelope).count(), 1)

    def test_text_confirmation_retries_the_same_preview_after_ambiguity(self) -> None:
        preview = self._preview("event")
        ambiguous_text = "确认发布 Missing Target"
        ambiguous_user = Message(
            conversation_id=self.conversation.id,
            role="user",
            content=ambiguous_text,
        )
        self.db.add(ambiguous_user)
        self.db.commit()

        ambiguous = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text=ambiguous_text,
        )
        self.assertIsNotNone(ambiguous)
        assert ambiguous is not None
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="assistant",
                content=ambiguous["message"],
                stream_finalized=True,
            )
        )
        self.db.commit()

        exact_text = "确认发布 风险已发现"
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="user",
                content=exact_text,
            )
        )
        self.db.commit()
        retry = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text=exact_text,
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry["status"], "confirmed")
        self.assertEqual(retry["preview_log_id"], preview["log_id"])
        self.assertEqual(self.db.query(EventEnvelope).count(), 1)

    def test_text_confirmation_does_not_follow_a_stale_ambiguity_continuation(self) -> None:
        self._preview("event")
        ambiguous_text = "确认发布 Missing Target"
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="user",
                content=ambiguous_text,
            )
        )
        self.db.commit()
        ambiguous = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text=ambiguous_text,
        )
        self.assertIsNotNone(ambiguous)
        assert ambiguous is not None
        self.db.add_all(
            [
                Message(
                    conversation_id=self.conversation.id,
                    role="assistant",
                    content=ambiguous["message"],
                    stream_finalized=True,
                ),
                Message(
                    conversation_id=self.conversation.id,
                    role="user",
                    content="先继续讨论其他问题",
                ),
                Message(
                    conversation_id=self.conversation.id,
                    role="assistant",
                    content="这是后续的普通回答。",
                    stream_finalized=True,
                ),
                Message(
                    conversation_id=self.conversation.id,
                    role="user",
                    content="确认发布 风险已发现",
                ),
            ]
        )
        self.db.commit()

        stale = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认发布 风险已发现",
        )

        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual(stale["status"], "no_pending")
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

    def test_text_confirmation_without_a_pending_preview_is_a_safe_noop(self) -> None:
        outcome = agent_confirmation_service.confirm_text_reply(
            self.db,
            agent=self.agent,
            conversation=self.conversation,
            text="确认执行",
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["status"], "no_pending")
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)

    def test_automatic_action_executes_when_confirmation_is_disabled(self) -> None:
        entity = OntologyEntity(
            id="entity-agent-automatic-action",
            scenario=self.scenario,
            name="AutomaticProject",
            is_abstract=True,
        )
        action = OntologyAction(
            id="action-agent-automatic",
            scenario=self.scenario,
            entity=entity,
            name="Update automatically",
            executor_type="sql",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            requires_confirmation=False,
            enabled=True,
        )
        self.db.add_all([entity, action])
        scope = dict(self.agent.capability_scope)
        scope["actions"] = {"mode": "explicit", "selected_ids": [action.id]}
        self.agent.capability_scope = scope
        self.db.commit()
        self.release = self._publish_current_snapshot(
            "snapshot-agent-confirm-automatic",
            "release-agent-confirm-automatic",
        )
        context = agent_engine.AgentContext(
            self.db,
            self.agent,
            LLMConfig(name="test-tool-model"),
        )
        executed = {
            "status": "success",
            "target_id": action.id,
            "target_name": action.name,
            "result": {"updated": True},
            "requires_confirmation": False,
        }

        with (
            patch.object(capability_readiness_service, "require_executable"),
            patch.object(workflow_service, "execute_action", return_value=executed) as execute_action,
        ):
            result = json.loads(context.execute_tool("execute_action", {"action_id": action.id, "params": {}}))

        self.assertEqual(result["status"], "success")
        self.assertFalse(result["requires_confirmation"])
        self.assertNotEqual(result["status"], "dry_run")
        self.assertTrue(execute_action.call_args.kwargs["confirm"])
        self.assertFalse(execute_action.call_args.kwargs["dry_run"])
        self.assertTrue(execute_action.call_args.kwargs["idempotency_key"])
        summary = agent_engine._truthful_final_content(
            "The action finished.",
            user_message="generate a report",
            tool_outcomes=[{
                "name": "execute_action",
                "arguments": {"action_id": action.id, "params": {}},
                "result": json.dumps(result),
            }],
        )
        self.assertIn("已执行 1 个未启用人工确认的操作", summary)
        self.assertNotIn("未生成可确认预演", summary)


if __name__ == "__main__":
    unittest.main()
