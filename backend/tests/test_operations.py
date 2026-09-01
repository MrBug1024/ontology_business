from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.config import get_settings
from app.models import (
    ActionExecutionLog,
    Agent,
    BusinessScenario,
    LLMConfig,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyRule,
    OntologyWorkflow,
    Skill,
    Tenant,
    User,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from app.routers import scenarios as scenarios_router
from app.schemas import ActionIn
from app.services.agent_engine import AgentContext
from app.services import (
    capability_readiness_service,
    operations_service,
    permission_service,
    workflow_payload_service,
    workflow_service,
)
from app.services.policies import PolicyViolation, validate_workflow_graph


def _workflow_nodes(*types: str) -> tuple[list[dict], list[dict]]:
    nodes = [{"id": "start", "type": "start", "data": {"name": "开始"}}]
    edges: list[dict] = []
    previous = "start"
    for index, node_type in enumerate(types, 1):
        node_id = f"n{index}"
        data = {"name": node_type}
        if node_type == "approval":
            data.update({"instructions": "请确认", "timeout_seconds": 120})
        nodes.append({"id": node_id, "type": node_type, "data": data})
        edges.append({"id": f"e{index}", "source": previous, "target": node_id, "label": ""})
        previous = node_id
    nodes.append({"id": "end", "type": "end", "data": {"name": "结束"}})
    edges.append({"id": f"e{len(types) + 1}", "source": previous, "target": "end", "label": ""})
    return nodes, edges


class OperationsRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-1", name="运营租户")
        self.user = User(
            id="user-1",
            tenant_id=self.tenant.id,
            email="operator@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-1", tenant_id=self.tenant.id, name="运营测试"
        )
        self.db.add_all([self.tenant, self.user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _workflow(self, *, name: str, trigger_type: str = "manual", trigger_config: dict | None = None, node_types: tuple[str, ...] = ()) -> OntologyWorkflow:
        nodes, edges = _workflow_nodes(*node_types)
        workflow = OntologyWorkflow(
            id=f"workflow-{name}",
            scenario_id=self.scenario.id,
            name=name,
            trigger_type=trigger_type,
            trigger_config=trigger_config or {},
            nodes=nodes,
            edges=edges,
            status="active",
            enabled=True,
        )
        self.db.add(workflow)
        self.db.commit()
        return workflow

    def test_approval_pauses_then_resumes_the_same_durable_run(self) -> None:
        workflow = self._workflow(name="approval", node_types=("approval",))
        marker = "customer-secret-approval-input"
        run, created = operations_service.enqueue_workflow_run(
            self.db, workflow, {"case": marker}
        )
        self.assertTrue(created)
        self.db.commit()

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual([step["node"] for step in run.result["steps"]], ["start", "n1"])
        self.assertNotIn(marker, json.dumps(run.result, ensure_ascii=False))
        approval = self.db.query(WorkflowApprovalRequest).filter_by(workflow_run_id=run.id).one()
        self.assertEqual(approval.status, "pending")

        operations_service.decide_approval(self.db, run, approved=True, comment="已核对")
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")
        self.assertIn("n1", run.approved_node_ids)
        self.assertEqual(run.result["steps"][-1]["node"], "end")
        self.assertNotIn(marker, json.dumps(run.result, ensure_ascii=False))
        workflow_logs = self.db.query(ActionExecutionLog).filter_by(
            target_type="workflow",
            target_id=workflow.id,
        ).all()
        self.assertGreaterEqual(len(workflow_logs), 2)
        for log in workflow_logs:
            self.assertNotIn(
                marker,
                json.dumps(
                    {"input_params": log.input_params, "result": log.result},
                    ensure_ascii=False,
                ),
            )
            self.assertTrue(log.input_params["redacted"])
        public_logs = scenarios_router.list_execution_logs(
            self.scenario.id,
            environment=None,
            limit=50,
            db=self.db,
        )
        self.assertTrue(public_logs)
        self.assertTrue(all(item.input_params["redacted"] for item in public_logs))
        self.assertNotIn(
            marker,
            json.dumps(
                [item.model_dump(mode="json") for item in public_logs],
                ensure_ascii=False,
            ),
        )

    def test_workflow_input_is_encrypted_at_rest_and_worker_recovers_exact_payload(self) -> None:
        workflow = self._workflow(name="encrypted-input")
        marker = "never-store-this-customer-value-in-plaintext"
        payload = {"operation": marker, "count": 4}
        run, created = operations_service.enqueue_workflow_run(
            self.db,
            workflow,
            payload,
        )
        self.assertTrue(created)
        self.db.commit()

        stored = self.db.execute(
            select(
                WorkflowRun.input_payload,
                WorkflowRun.input_summary,
                WorkflowRun.input_digest,
            ).where(WorkflowRun.id == run.id)
        ).one()
        self.assertNotIn(marker, json.dumps(stored._asdict(), sort_keys=True))
        self.assertEqual(stored.input_payload["alg"], "A256GCM")
        self.assertEqual(
            workflow_payload_service.public_input_summary(run)["field_count"],
            2,
        )

        with patch.object(
            workflow_service,
            "execute_workflow",
            return_value={"status": "success", "steps": [], "duration_ms": 1},
        ) as execute:
            operations_service.process_available_runs(self.db)
        self.assertEqual(execute.call_args.args[2], payload)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")

        public = scenarios_router._workflow_run_out(self.db, run)
        public_document = public.model_dump(mode="json")
        self.assertNotIn(marker, json.dumps(public_document, sort_keys=True))
        self.assertTrue(public.input_params["redacted"])
        self.assertEqual(public.input_params["field_count"], 2)

    def test_enqueue_fails_closed_without_payload_key_and_writes_no_run(self) -> None:
        workflow = self._workflow(name="missing-payload-key")
        settings = SimpleNamespace(
            workflow_payload_active_key_id="",
            workflow_payload_encryption_keys="",
        )
        with patch.object(
            workflow_payload_service,
            "get_settings",
            return_value=settings,
        ), self.assertRaises(workflow_payload_service.WorkflowPayloadError):
            operations_service.enqueue_workflow_run(
                self.db,
                workflow,
                {"operation": "must-not-persist"},
            )
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)

    def test_legacy_approval_step_resumes_using_its_canonical_node_id(self) -> None:
        workflow = OntologyWorkflow(
            id="workflow-legacy-approval",
            scenario_id=self.scenario.id,
            name="兼容审批",
            steps=[
                {
                    "step": 1,
                    "id": "legacy-approval",
                    "type": "approval",
                    "instructions": "请确认",
                    "timeout_seconds": 120,
                    "on_timeout": "reject",
                }
            ],
            status="active",
            enabled=True,
        )
        self.db.add(workflow)
        self.db.commit()
        run, _ = operations_service.enqueue_workflow_run(self.db, workflow)
        self.db.commit()
        operations_service.process_available_runs(self.db)
        approval = self.db.query(WorkflowApprovalRequest).filter_by(workflow_run_id=run.id).one()
        self.assertEqual(approval.node_id, "legacy-approval")

        operations_service.decide_approval(self.db, run, approved=True)
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")
        self.assertIn("legacy-approval", run.approved_node_ids)

    def test_manual_retry_starts_a_new_attempt_and_reopens_approval(self) -> None:
        workflow = self._workflow(name="retry", node_types=("approval",))
        run, _ = operations_service.enqueue_workflow_run(self.db, workflow)
        self.db.commit()
        operations_service.process_available_runs(self.db)
        operations_service.decide_approval(self.db, run, approved=True)
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")

        run.status = "failed"
        run.attempt = run.max_attempts
        self.db.commit()
        previous_execution_key = run.execution_key
        operations_service.retry_run(self.db, run)
        self.assertEqual(run.attempt, 0)
        self.assertEqual(run.approved_node_ids, [])
        self.assertNotEqual(run.execution_key, previous_execution_key)

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.attempt, 1)

    def test_agent_workflow_tool_requires_user_confirmation_instead_of_enqueuing(self) -> None:
        workflow = self._workflow(name="agent-approval", node_types=("approval",))
        agent = Agent(
            id="agent-operations",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="运营助手",
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": []},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": []},
                "workflows": {"mode": "explicit", "selected_ids": [workflow.id]},
            },
        )
        context = AgentContext(self.db, agent, LLMConfig(name="测试模型"))

        raw_result = context.execute_tool(
            "execute_workflow",
            {"workflow_id": workflow.id, "params": {"case": "agent"}},
        )
        result = json.loads(raw_result)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["workflow_id"], workflow.id)
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)

    def test_event_envelope_enqueues_subscriber_once(self) -> None:
        event = OntologyEvent(id="event-1", scenario_id=self.scenario.id, name="对象已更新")
        self.db.add(event)
        self.db.commit()
        subscriber = self._workflow(
            name="subscriber",
            trigger_type="event",
            trigger_config={"event_id": event.id},
        )

        envelope, queued = operations_service.publish_event(
            self.db, event, {"object_id": "object-1"}, dedupe_key="source-change-1"
        )
        self.db.commit()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].workflow_id, subscriber.id)

        duplicate, duplicate_runs = operations_service.publish_event(
            self.db, event, {"object_id": "object-1"}, dedupe_key="source-change-1"
        )
        self.assertEqual(duplicate.id, envelope.id)
        self.assertEqual(len(duplicate_runs), 1)

        operations_service.process_available_runs(self.db)
        self.db.refresh(queued[0])
        self.assertEqual(queued[0].status, "succeeded")

    def test_interval_schedule_and_trigger_validation(self) -> None:
        workflow = self._workflow(
            name="scheduled",
            trigger_type="scheduled",
            trigger_config={"interval_seconds": 10, "max_attempts": 2, "timeout_seconds": 30},
        )
        now = operations_service.utc_now()
        first = operations_service.enqueue_due_schedules(self.db, now=now)
        self.db.commit()
        self.assertEqual([run.workflow_id for run in first], [workflow.id])
        self.assertEqual(operations_service.enqueue_due_schedules(self.db, now=now + timedelta(seconds=9)), [])
        # The first scheduled run has not completed, so a later tick must not
        # overlap it with a second invocation.
        self.assertEqual(operations_service.enqueue_due_schedules(self.db, now=now + timedelta(seconds=11)), [])
        first[0].status = "succeeded"
        self.db.commit()
        self.assertEqual(len(operations_service.enqueue_due_schedules(self.db, now=now + timedelta(seconds=11))), 1)

        with self.assertRaises(PolicyViolation):
            operations_service.validate_trigger_config("scheduled", {})
        with self.assertRaises(PolicyViolation):
            operations_service.validate_trigger_config("scheduled", {"interval_seconds": 4})
        validate_workflow_graph(*_workflow_nodes("approval"))

    def test_source_less_schedule_and_event_fail_closed_without_owner_fallback(self) -> None:
        scheduled = self._workflow(
            name="unattributed-schedule",
            trigger_type="scheduled",
            trigger_config={"interval_seconds": 10},
        )
        event = OntologyEvent(
            id="event-unattributed",
            scenario_id=self.scenario.id,
            name="外部同步完成",
        )
        self.db.add(event)
        self.db.commit()
        subscriber = self._workflow(
            name="unattributed-subscriber",
            trigger_type="event",
            trigger_config={"event_id": event.id},
        )

        # The worker has no HTTP identity.  Earlier P1 behavior delegated this
        # situation to execution_principal, which selected the tenant owner.
        self.db.info.clear()
        now = operations_service.utc_now()
        scheduled_runs = operations_service.enqueue_due_schedules(self.db, now=now)
        _, event_runs = operations_service.publish_event(
            self.db,
            event,
            {"source": "connector"},
            source="connector",
        )
        self.db.commit()
        self.assertEqual([run.workflow_id for run in scheduled_runs], [scheduled.id])
        self.assertEqual([run.workflow_id for run in event_runs], [subscriber.id])
        self.assertTrue(all(run.created_by_user_id is None for run in [*scheduled_runs, *event_runs]))

        with patch("app.services.workflow_service.execute_workflow") as execute_workflow:
            processed = operations_service.process_available_runs(self.db, now=now + timedelta(seconds=1))

        self.assertEqual({run.id for run in processed}, {run.id for run in [*scheduled_runs, *event_runs]})
        for run in [*scheduled_runs, *event_runs]:
            self.db.refresh(run)
            self.assertEqual(run.status, "failed")
            self.assertIn("不会回退为组织所有者", run.error)
        execute_workflow.assert_not_called()
        # Do not turn a legacy, unbootstrapped five-second schedule into an
        # unbounded stream of identical governance failures.
        self.assertEqual(
            operations_service.enqueue_due_schedules(self.db, now=now + timedelta(seconds=11)),
            [],
        )

    def test_automatic_triggers_reuse_a_real_manual_or_event_origin(self) -> None:
        scheduled = self._workflow(
            name="scheduled-origin",
            trigger_type="scheduled",
            trigger_config={"interval_seconds": 10},
        )
        bootstrap, _ = operations_service.enqueue_workflow_run(
            self.db,
            scheduled,
            {"seed": True},
            trigger_source="manual",
        )
        bootstrap.status = "succeeded"
        self.db.commit()

        source = self._workflow(name="event-origin")
        source_run, _ = operations_service.enqueue_workflow_run(
            self.db,
            source,
            {"seed": "event"},
            trigger_source="manual",
        )
        source_run.status = "succeeded"
        event = OntologyEvent(
            id="event-origin",
            scenario_id=self.scenario.id,
            name="由工作流发布",
        )
        self.db.add(event)
        self.db.commit()
        subscriber = self._workflow(
            name="event-origin-subscriber",
            trigger_type="event",
            trigger_config={"event_id": event.id},
        )

        # Simulate a context-free worker/connector after durable user work has
        # established provenance.  The scheduler and the event chain must keep
        # that exact user rather than choosing a tenant owner by default.
        self.db.info.clear()
        scheduled_runs = operations_service.enqueue_due_schedules(
            self.db,
            now=operations_service.utc_now(),
        )
        _, event_runs = operations_service.publish_event(
            self.db,
            event,
            {"source": "workflow"},
            source="workflow",
            source_run_id=source_run.id,
        )
        self.assertEqual(scheduled_runs[0].created_by_user_id, self.user.id)
        self.assertEqual(event_runs[0].workflow_id, subscriber.id)
        self.assertEqual(event_runs[0].created_by_user_id, self.user.id)

    def test_manual_retry_records_the_actual_retrier(self) -> None:
        workflow = self._workflow(name="retry-origin")
        run, _ = operations_service.enqueue_workflow_run(self.db, workflow)
        run.status = "failed"
        retry_user = User(
            id="user-retry",
            tenant_id=self.tenant.id,
            email="retry@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(retry_user)
        self.db.commit()
        organization = permission_service.organization_for_principal(self.db)
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=retry_user.id,
            role_key="operator",
        )
        self.db.commit()

        self.db.info["user_id"] = retry_user.id
        operations_service.retry_run(self.db, run)
        self.assertEqual(run.created_by_user_id, retry_user.id)

    def test_unresolved_workflow_action_is_rejected_before_any_side_effect(self) -> None:
        entity = OntologyEntity(id="entity-retry", scenario_id=self.scenario.id, name="订单")
        action = OntologyAction(
            id="action-retry",
            scenario_id=self.scenario.id,
            entity_id=entity.id,
            name="外部写入",
            executor_type="http",
            executor_config={"url": "https://example.test/write", "method": "POST"},
            requires_confirmation=False,
            idempotency_required=True,
        )
        nodes = [
            {"id": "start", "type": "start", "data": {"name": "开始"}},
            {
                "id": "write",
                "type": "action",
                "data": {"name": "外部写入", "action_id": action.id, "params": {}},
            },
            {
                "id": "fail",
                "type": "action",
                "data": {"name": "失败节点", "action_id": "missing-action", "params": {}},
            },
            {"id": "end", "type": "end", "data": {"name": "结束"}},
        ]
        edges = [
            {"id": "e1", "source": "start", "target": "write", "label": ""},
            {"id": "e2", "source": "write", "target": "fail", "label": ""},
            {"id": "e3", "source": "fail", "target": "end", "label": ""},
        ]
        workflow = OntologyWorkflow(
            id="workflow-safe-retry",
            scenario_id=self.scenario.id,
            name="安全重试",
            nodes=nodes,
            edges=edges,
            trigger_config={"max_attempts": 2, "retry_backoff_seconds": 1},
            status="active",
            enabled=True,
        )
        self.db.add_all([entity, action, workflow])
        self.db.commit()
        with self.assertRaises(PolicyViolation):
            operations_service.enqueue_workflow_run(self.db, workflow)
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)
        self.assertEqual(self.db.query(ActionExecutionLog).count(), 0)

    def test_event_cycle_is_rejected_and_legacy_cycle_is_suppressed_at_runtime(self) -> None:
        event = OntologyEvent(id="event-cycle", scenario_id=self.scenario.id, name="状态已变更")
        self.db.add(event)
        self.db.commit()
        nodes = [
            {"id": "start", "type": "start", "data": {"name": "开始"}},
            {
                "id": "emit",
                "type": "event",
                "data": {"name": "再次发布", "event_id": event.id, "payload": {}},
            },
            {"id": "end", "type": "end", "data": {"name": "结束"}},
        ]
        edges = [
            {"id": "e1", "source": "start", "target": "emit", "label": ""},
            {"id": "e2", "source": "emit", "target": "end", "label": ""},
        ]
        with self.assertRaises(PolicyViolation):
            operations_service.validate_event_feedback_loops(
                self.db,
                self.scenario.id,
                trigger_type="event",
                trigger_config={"event_id": event.id},
                nodes=nodes,
                steps=[],
            )

        # Simulate an imported pre-validation definition.  The runtime causal
        # chain guard still records, but does not dispatch, the recursive event.
        workflow = OntologyWorkflow(
            id="workflow-cycle",
            scenario_id=self.scenario.id,
            name="遗留循环",
            trigger_type="event",
            trigger_config={"event_id": event.id},
            nodes=nodes,
            edges=edges,
            status="active",
            enabled=True,
        )
        self.db.add(workflow)
        self.db.commit()
        _, queued = operations_service.publish_event(self.db, event, {"source": "test"})
        self.db.commit()
        self.assertEqual(len(queued), 1)
        operations_service.process_available_runs(self.db)
        self.db.refresh(queued[0])
        self.assertEqual(queued[0].status, "succeeded")
        self.assertEqual(self.db.query(WorkflowRun).count(), 1)

    def test_approval_timeout_policy_cancel_and_active_workflow_protection(self) -> None:
        workflow = self._workflow(name="timeout", node_types=("approval",))
        timeout_nodes = [dict(node) for node in workflow.nodes]
        timeout_nodes[1] = {**timeout_nodes[1], "data": {**timeout_nodes[1]["data"], "on_timeout": "timeout"}}
        workflow.nodes = timeout_nodes
        self.db.commit()
        run, _ = operations_service.enqueue_workflow_run(self.db, workflow)
        self.db.commit()
        with self.assertRaises(PolicyViolation):
            operations_service.assert_workflow_mutable(self.db, workflow.id)

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        approval = self.db.query(WorkflowApprovalRequest).filter_by(workflow_run_id=run.id).one()
        expires_at = operations_service._aware(approval.expires_at)
        assert expires_at is not None
        operations_service.expire_stale_operations(self.db, now=expires_at + timedelta(seconds=1))
        self.db.refresh(run)
        self.db.refresh(approval)
        self.assertEqual(run.status, "timed_out")
        self.assertEqual(approval.status, "expired")
        operations_service.assert_workflow_mutable(self.db, workflow.id)

        queued, _ = operations_service.enqueue_workflow_run(self.db, workflow)
        self.db.commit()
        operations_service.cancel_run(self.db, queued, comment="不再需要")
        self.db.refresh(queued)
        self.assertEqual(queued.status, "cancelled")

        with self.assertRaises(PolicyViolation):
            operations_service.validate_approval_nodes(
                [{"type": "approval", "data": {"timeout_seconds": 0}}], []
            )
        with self.assertRaises(PolicyViolation):
            operations_service.validate_approval_nodes(
                [{"type": "approval", "data": {"timeout_seconds": 60, "on_timeout": "ignore"}}], []
            )

    def test_deadline_stops_before_dispatching_next_workflow_node(self) -> None:
        workflow = self._workflow(name="deadline")
        result = workflow_service.execute_workflow(
            self.db,
            workflow,
            {},
            deadline_at=operations_service.utc_now() - timedelta(seconds=1),
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("超时", result["error"])

    def test_workflow_fails_closed_when_referenced_rule_is_disabled(self) -> None:
        rule = OntologyRule(
            id="rule-disabled",
            scenario_id=self.scenario.id,
            name="停用的业务规则",
            condition={"field": "risk", "op": ">=", "value": 80},
            enabled=False,
        )
        workflow = OntologyWorkflow(
            id="workflow-disabled-rule",
            scenario_id=self.scenario.id,
            name="不得绕过停用规则",
            nodes=[
                {"id": "start", "type": "start", "data": {}},
                {
                    "id": "check",
                    "type": "rule",
                    "data": {"rule_id": rule.id, "record": {"risk": 90}},
                },
                {"id": "end", "type": "end", "data": {}},
            ],
            edges=[
                {"id": "e1", "source": "start", "target": "check", "label": ""},
                {"id": "e2", "source": "check", "target": "end", "label": "true"},
                {"id": "e3", "source": "check", "target": "end", "label": "false"},
            ],
            status="active",
            enabled=True,
        )
        self.db.add_all([rule, workflow])
        self.db.commit()

        result = workflow_service.execute_workflow(
            self.db,
            workflow,
            {"risk": 90},
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("规则已停用", result["error"])

    def test_rule_api_false_branch_is_serializable_and_has_no_side_effects(self) -> None:
        rule = OntologyRule(
            id="rule-recursive-false",
            scenario_id=self.scenario.id,
            name="递归条件规则",
            condition={
                "op": "and",
                "conditions": [
                    {"field": "quantity", "op": ">", "value": 2},
                    {
                        "op": "not",
                        "conditions": [
                            {
                                "field": "service_name",
                                "op": "==",
                                "value": "excluded-value",
                            }
                        ],
                    },
                ],
            },
            action_on_match="would-trigger-only-on-match",
            enabled=True,
        )
        self.db.add(rule)
        self.db.commit()
        logs_before = self.db.query(ActionExecutionLog).count()
        runs_before = self.db.query(WorkflowRun).count()

        result = scenarios_router.evaluate_rule(
            rule.id,
            {
                "record": {
                    "quantity": 3,
                    "service_name": "excluded-value",
                }
            },
            db=self.db,
        )

        self.assertFalse(result["matched"])
        self.assertEqual(result["action_on_match"], "")
        self.assertEqual(result["trigger_action_ids"], [])
        self.assertEqual(result["trigger_actions"], [])
        self.assertFalse(result["side_effects_executed"])
        self.assertFalse(jsonable_encoder(result)["matched"])
        self.assertEqual(self.db.query(ActionExecutionLog).count(), logs_before)
        self.assertEqual(self.db.query(WorkflowRun).count(), runs_before)

    def test_native_http_workflow_nodes_are_rejected_and_never_dispatched_by_default(self) -> None:
        nodes, edges = _workflow_nodes("http")
        with patch(
            "app.services.workflow_service.get_settings",
            return_value=SimpleNamespace(allow_unsafe_workflow_nodes=False),
        ):
            with self.assertRaises(PolicyViolation):
                workflow_service.validate_workflow_definition(nodes, edges)

            workflow = OntologyWorkflow(
                id="workflow-unsafe-http",
                scenario_id=self.scenario.id,
                name="遗留原生 HTTP",
                nodes=nodes,
                edges=edges,
                status="active",
                enabled=True,
            )
            self.db.add(workflow)
            self.db.commit()
            with patch("app.services.workflow_service._exec_http") as execute_http:
                result = workflow_service.execute_workflow(self.db, workflow, {})
            self.assertEqual(result["status"], "failed")
            self.assertIn("默认停用", result["error"])
            execute_http.assert_not_called()

    def test_script_action_is_rejected_even_when_unsafe_flag_is_enabled(self) -> None:
        marker = "script-action-secret-must-not-run"
        params: dict[str, str] = {}
        action = SimpleNamespace(
            executor_type="script",
            executor_config={
                "script": (
                    f"params['executed'] = '{marker}'; "
                    f"result = '{marker}'"
                )
            },
        )

        with patch(
            "app.services.workflow_service.get_settings",
            return_value=SimpleNamespace(allow_unsafe_workflow_nodes=True),
        ), self.assertRaises(PolicyViolation) as rejected:
            workflow_service._dispatch_executor(self.db, action, params)

        self.assertEqual(params, {})
        self.assertNotIn(marker, str(rejected.exception))
        self.assertIn("停用", str(rejected.exception))

    def test_script_action_authoring_and_readiness_stay_blocked_when_flag_is_enabled(self) -> None:
        entity = OntologyEntity(
            id="entity-script-disabled",
            scenario_id=self.scenario.id,
            name="Generic object",
        )
        self.db.add(entity)
        self.db.commit()
        payload = ActionIn(
            entity_id=entity.id,
            name="Untrusted script action",
            executor_type="script",
            executor_config={"script": "result = 'must-not-run'"},
        )

        with patch.dict(os.environ, {"ALLOW_UNSAFE_WORKFLOW_NODES": "true"}):
            get_settings.cache_clear()
            try:
                self.assertTrue(get_settings().allow_unsafe_workflow_nodes)
                with self.assertRaises(HTTPException) as create_rejected:
                    scenarios_router.create_action(self.scenario.id, payload, self.db)
                self.assertEqual(create_rejected.exception.status_code, 400)

                historical = OntologyAction(
                    id="action-historical-script",
                    scenario_id=self.scenario.id,
                    entity_id=entity.id,
                    name="Historical script action",
                    executor_type="script",
                    executor_config={"script": "result = 'must-not-run'"},
                    enabled=True,
                )
                self.db.add(historical)
                self.db.commit()

                readiness = capability_readiness_service.capability_readiness(
                    "action",
                    historical,
                    db=self.db,
                )
                self.assertFalse(readiness.executable)
                self.assertTrue(
                    any("停用" in reason for reason in readiness.blocked_reasons)
                )
                with self.assertRaises(HTTPException) as update_rejected:
                    scenarios_router.update_action(historical.id, payload, self.db)
                self.assertEqual(update_rejected.exception.status_code, 400)
                self.db.refresh(historical)
                self.assertEqual(historical.name, "Historical script action")
            finally:
                get_settings.cache_clear()

    def test_script_workflow_node_is_rejected_even_when_unsafe_flag_is_enabled(self) -> None:
        marker = "script-workflow-secret-must-not-run"
        nodes, edges = _workflow_nodes("script")
        nodes[1]["data"]["script"] = (
            f"params['executed'] = '{marker}'; result = '{marker}'"
        )
        workflow = OntologyWorkflow(
            id="workflow-script-disabled",
            scenario_id=self.scenario.id,
            name="脚本节点必须停用",
            nodes=nodes,
            edges=edges,
            status="active",
            enabled=True,
        )
        self.db.add(workflow)
        self.db.commit()
        params: dict[str, str] = {}

        with patch(
            "app.services.workflow_service.get_settings",
            return_value=SimpleNamespace(allow_unsafe_workflow_nodes=True),
        ):
            with self.assertRaises(PolicyViolation) as rejected:
                workflow_service.validate_workflow_definition(nodes, edges)
            result = workflow_service.execute_workflow(self.db, workflow, params)

        self.assertEqual(params, {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["steps"], [])
        self.assertIn("停用", result["error"])
        self.assertNotIn(marker, str(rejected.exception))
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))

    def test_http_actions_reject_local_targets_and_unsafe_request_headers(self) -> None:
        with self.assertRaises(PolicyViolation):
            workflow_service.validate_http_action_config({"url": "https://127.0.0.1/internal"})
        with self.assertRaises(PolicyViolation):
            workflow_service.validate_http_action_config({"url": "https://localhost/internal"})
        with self.assertRaises(PolicyViolation):
            workflow_service.validate_http_action_config(
                {"url": "https://api.example.test", "headers": {"Host": "internal.local"}}
            )

    def test_skill_actions_require_a_managed_enabled_catalog_entry(self) -> None:
        raw_payload = ActionIn(
            entity_id="entity-unused",
            name="遗留本地脚本",
            executor_type="skill",
            executor_config={"skill_name": "legacy", "skill_path": "C:/unsafe"},
        )
        with self.assertRaises(HTTPException):
            scenarios_router._validate_action_executor(self.db, self.scenario.id, raw_payload)

        skill = Skill(
            id="skill-managed",
            tenant_id=self.tenant.id,
            name="受管技能",
            path="C:/catalogued-skill",
            enabled=True,
        )
        self.db.add(skill)
        self.db.commit()
        payload = ActionIn(
            entity_id="entity-unused",
            name="调用受管技能",
            executor_type="skill",
            executor_config={"skill_id": skill.id},
        )
        scenarios_router._validate_action_executor(self.db, self.scenario.id, payload)

        action = OntologyAction(
            id="action-managed-skill",
            scenario_id=self.scenario.id,
            name="调用受管技能",
            executor_type="skill",
            executor_config={"skill_id": skill.id},
        )
        with patch(
            "app.services.workflow_service.skill_service.execute_skill",
            return_value={"status": "success", "stdout": "ok", "stderr": "", "exit_code": 0},
        ) as execute_skill:
            result, audit = workflow_service._dispatch_executor(self.db, action, {"args": ["case-1"]})
        self.assertEqual(result["stdout"], "ok")
        self.assertEqual(audit, [])
        execute_skill.assert_called_once_with(skill, ["case-1"], timeout=60)

        raw_action = OntologyAction(
            id="action-raw-skill",
            scenario_id=self.scenario.id,
            name="遗留技能",
            executor_type="skill",
            executor_config={"skill_name": "legacy", "skill_path": "C:/unsafe"},
        )
        with self.assertRaises(PolicyViolation):
            workflow_service._dispatch_executor(self.db, raw_action, {})

        skill.enabled = False
        self.db.commit()
        with self.assertRaises(PolicyViolation):
            workflow_service._dispatch_executor(self.db, action, {})

    def test_worker_does_not_claim_a_run_from_another_deployment_environment(self) -> None:
        workflow = self._workflow(name="environment-isolation")
        run, created = operations_service.enqueue_workflow_run(self.db, workflow)
        self.assertTrue(created)
        self.db.commit()
        self.assertEqual(run.environment, "dev")

        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ), patch("app.services.workflow_service.execute_workflow") as execute_workflow:
            processed = operations_service.process_available_runs(self.db)

        self.db.refresh(run)
        self.assertEqual(processed, [])
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.environment, "dev")
        execute_workflow.assert_not_called()
