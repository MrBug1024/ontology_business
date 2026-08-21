from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ActionExecutionLog,
    BusinessScenario,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyRelease,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from app.routers import operations as operations_router
from app.routers import scenarios as scenarios_router
from app.services import permission_service, release_service, runtime_connector_service
from app.services.lineage_service import build_scenario_lineage


class RuntimeDefinitionReadViewsTests(unittest.TestCase):
    """Read surfaces must retain the definition that a run actually pinned."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-runtime-view", name="运行定义展示租户")
        self.user = User(
            id="user-runtime-view",
            tenant_id=self.tenant.id,
            email="runtime-view@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-runtime-view",
            tenant_id=self.tenant.id,
            name="冻结定义展示",
        )
        self.entity = OntologyEntity(
            id="entity-runtime-view",
            scenario_id=self.scenario.id,
            name="发布对象",
        )
        self.action = OntologyAction(
            id="action-runtime-view",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            name="发布版操作名称",
            executor_type="sql",
            executor_config={},
        )
        self.workflow = OntologyWorkflow(
            id="workflow-runtime-view",
            scenario_id=self.scenario.id,
            name="发布版工作流名称",
            status="active",
            enabled=True,
        )
        self.db.add_all(
            [self.tenant, self.user, self.scenario, self.entity, self.action, self.workflow]
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

        branch = OntologyBranch(
            id="branch-runtime-view",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="runtime-view/main",
            created_by_user_id=self.user.id,
        )
        self.db.add(branch)
        self.db.flush()
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        self.snapshot = OntologySnapshot(
            id="snapshot-runtime-view",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.user.id,
        )
        self.release = OntologyRelease(
            id="release-runtime-view",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            snapshot_id=self.snapshot.id,
            environment="staging",
            status="released",
            created_by_user_id=self.user.id,
        )
        self.db.add_all([self.snapshot, self.release])
        self.db.flush()
        self.run = WorkflowRun(
            id="run-runtime-view",
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="staging",
            definition_snapshot_id=self.snapshot.id,
            release_id=self.release.id,
            definition_hash=self.snapshot.content_hash,
            definition_source="release",
            status="awaiting_approval",
            result={"steps": [{"result": {"log_id": "log-runtime-view"}}]},
        )
        self.approval = WorkflowApprovalRequest(
            id="approval-runtime-view",
            workflow_run_id=self.run.id,
            scenario_id=self.scenario.id,
            node_id="approval-node",
            node_name="发布版审批节点",
            status="pending",
        )
        self.log = ActionExecutionLog(
            id="log-runtime-view",
            scenario_id=self.scenario.id,
            target_type="action",
            target_id=self.action.id,
            # This deliberately disagrees with the snapshot to make sure the
            # read path never presents saved/live labels as frozen evidence.
            target_name="不可作为冻结证据的旧名称",
            environment="staging",
            definition_snapshot_id=self.snapshot.id,
            release_id=self.release.id,
            definition_hash=self.snapshot.content_hash,
            definition_source="release",
            status="success",
        )
        self.db.add_all([self.run, self.approval, self.log])
        self.db.commit()

        # Simulate dev authoring after staging dispatch.  The frozen task and
        # graph must still read names from ``self.snapshot``.
        self.action.name = "后续实时操作名称"
        self.workflow.name = "后续实时工作流名称"
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_task_approval_and_lineage_prefer_frozen_release_resources(self) -> None:
        task = operations_router._run_out(self.db, self.run)
        self.assertEqual(task.workflow_name, "发布版工作流名称")
        self.assertEqual(task.definition_snapshot_id, self.snapshot.id)
        self.assertEqual(task.release_id, self.release.id)
        self.assertEqual(task.definition_source, "release")
        self.assertTrue(task.can_execute)
        self.assertTrue(task.can_approve)

        # These operational views are served by the staging deployment.  The
        # same tenant's dev process must not read their input/result evidence.
        with patch.object(runtime_connector_service, "runtime_environment", return_value="staging"):
            approvals = operations_router.list_approvals(
                scenario_id=self.scenario.id,
                status="pending",
                limit=80,
                db=self.db,
            )
            logs = scenarios_router.list_execution_logs(
                scenario_id=self.scenario.id,
                limit=80,
                db=self.db,
            )
            graph = build_scenario_lineage(self.db, self.scenario.id)
        self.assertEqual([item.workflow_name for item in approvals], ["发布版工作流名称"])
        self.assertEqual([item.id for item in logs], [self.log.id])
        node_by_id = {node["id"]: node for node in graph["nodes"]}
        action_node_id = f"action:{self.action.id}@{self.release.id}"
        workflow_node_id = f"workflow:{self.workflow.id}@{self.release.id}"
        self.assertEqual(node_by_id[action_node_id]["label"], "发布版操作名称")
        self.assertEqual(node_by_id[workflow_node_id]["label"], "发布版工作流名称")
        self.assertNotIn(
            "后续实时操作名称",
            {str(node["label"]) for node in graph["nodes"]},
        )
        self.assertNotIn(
            "后续实时工作流名称",
            {str(node["label"]) for node in graph["nodes"]},
        )
        execution_edge = next(
            edge
            for edge in graph["edges"]
            if edge["source"] == action_node_id
            and edge["target"] == f"action_execution:{self.log.id}"
        )
        self.assertEqual(execution_edge["meta"]["release_id"], self.release.id)
        self.assertEqual(execution_edge["meta"]["definition_snapshot_id"], self.snapshot.id)
        result_edge = next(
            edge
            for edge in graph["edges"]
            if edge["source"] == f"action_execution:{self.log.id}"
            and edge["target"] == f"external_result:{self.log.id}"
        )
        self.assertEqual(result_edge["meta"]["release_id"], self.release.id)
        self.assertEqual(result_edge["meta"]["definition_snapshot_id"], self.snapshot.id)
        self.assertTrue(
            any(
                edge["source"] == workflow_node_id
                and edge["target"] == f"workflow_run:{self.run.id}"
                and edge["kind"] == "queued_as"
                and edge["meta"]["release_id"] == self.release.id
                for edge in graph["edges"]
            )
        )

    def test_corrupt_frozen_evidence_is_hidden_instead_of_using_live_resources(self) -> None:
        self.run.definition_hash = "invalid-run-hash"
        self.log.definition_hash = "invalid-log-hash"
        self.db.commit()

        task = operations_router._run_out(self.db, self.run)
        self.assertEqual(task.workflow_name, "")
        self.assertFalse(task.can_execute)
        self.assertFalse(task.can_approve)
        with patch.object(runtime_connector_service, "runtime_environment", return_value="staging"):
            self.assertEqual(
                operations_router.list_tasks(
                    scenario_id=self.scenario.id,
                    status=None,
                    limit=80,
                    db=self.db,
                ),
                [],
            )
            self.assertEqual(
                operations_router.list_approvals(
                    scenario_id=self.scenario.id,
                    status="pending",
                    limit=80,
                    db=self.db,
                ),
                [],
            )
            with self.assertRaises(HTTPException) as error:
                operations_router._run_for_request(self.db, self.run.id)
            self.assertEqual(error.exception.status_code, 404)
            graph = build_scenario_lineage(self.db, self.scenario.id)
        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertNotIn(f"action_execution:{self.log.id}", node_ids)
        self.assertNotIn(f"workflow_run:{self.run.id}", node_ids)
        labels = {str(node["label"]) for node in graph["nodes"]}
        self.assertNotIn("后续实时操作名称", labels)
        self.assertNotIn("后续实时工作流名称", labels)

    def test_operational_views_are_scoped_to_the_current_deployment(self) -> None:
        """A dev control-plane process cannot inspect or operate staging work."""
        with patch.object(runtime_connector_service, "runtime_environment", return_value="dev"):
            self.assertEqual(
                operations_router.list_tasks(
                    scenario_id=self.scenario.id,
                    status=None,
                    limit=80,
                    db=self.db,
                ),
                [],
            )
            self.assertEqual(
                operations_router.list_approvals(
                    scenario_id=self.scenario.id,
                    status="pending",
                    limit=80,
                    db=self.db,
                ),
                [],
            )
            self.assertEqual(
                scenarios_router.list_execution_logs(
                    scenario_id=self.scenario.id,
                    limit=80,
                    db=self.db,
                ),
                [],
            )
            with self.assertRaises(HTTPException) as error:
                operations_router._run_for_request(self.db, self.run.id)
            self.assertEqual(error.exception.status_code, 404)
            graph = build_scenario_lineage(self.db, self.scenario.id)

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertNotIn(f"action_execution:{self.log.id}", node_ids)
        self.assertNotIn(f"workflow_run:{self.run.id}", node_ids)


if __name__ == "__main__":
    unittest.main()
