from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BusinessScenario,
    OntologyEvent,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowApprovalRequest,
)
from app.services import operations_service, permission_service
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
        run, created = operations_service.enqueue_workflow_run(self.db, workflow, {"case": "A"})
        self.assertTrue(created)
        self.db.commit()

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual([step["node"] for step in run.result["steps"]], ["start", "n1"])
        approval = self.db.query(WorkflowApprovalRequest).filter_by(workflow_run_id=run.id).one()
        self.assertEqual(approval.status, "pending")

        operations_service.decide_approval(self.db, run, approved=True, comment="已核对")
        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")
        self.assertIn("n1", run.approved_node_ids)
        self.assertEqual(run.result["steps"][-1]["node"], "end")

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
        operations_service.retry_run(self.db, run)
        self.assertEqual(run.attempt, 0)
        self.assertEqual(run.approved_node_ids, [])

        operations_service.process_available_runs(self.db)
        self.db.refresh(run)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.attempt, 1)

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
        self.assertEqual(len(operations_service.enqueue_due_schedules(self.db, now=now + timedelta(seconds=11))), 1)

        with self.assertRaises(PolicyViolation):
            operations_service.validate_trigger_config("scheduled", {})
        validate_workflow_graph(*_workflow_nodes("approval"))
