"""Regression coverage for immutable staging/prod runtime definitions.

These tests deliberately use the service boundary rather than the release
routes: a worker must still be safe after an already-approved deployment is
superseded or dev authoring moves on.  They cover the exact seam where mutable
ORM definitions used to leak into a non-dev runtime.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    ActionExecutionLog,
    BusinessScenario,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyProperty,
    OntologyRelease,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowRun,
)
from app.services import (
    capability_readiness_service,
    operations_service,
    permission_service,
    release_service,
    runtime_connector_service,
    runtime_definition_service,
    workflow_service,
)


class RuntimeDefinitionIsolationTests(unittest.TestCase):
    """A staging worker executes its queued release, not current live rows."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.db: Session = self.Session()

        self.tenant = Tenant(id="tenant-runtime-definition", name="运行定义组织")
        self.owner = User(
            id="owner-runtime-definition",
            tenant_id=self.tenant.id,
            email="owner-runtime-definition@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-runtime-definition",
            tenant_id=self.tenant.id,
            name="冻结运行定义场景",
        )
        self.entity = OntologyEntity(
            id="entity-runtime-definition",
            scenario_id=self.scenario.id,
            name="运行对象",
        )
        self.entity_key = OntologyProperty(
            id="property-runtime-definition-id",
            entity_id=self.entity.id,
            name="对象ID",
            is_key=True,
            is_title=True,
            is_required=True,
        )
        # The action is intentionally a script without a body.  It gives the
        # idempotency test a safe, deterministic failure after the durable log
        # has been created; no network or host execution is involved.
        self.action = OntologyAction(
            id="action-runtime-definition",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            name="冻结操作 A",
            executor_type="script",
            executor_config={},
            input_schema={},
            enabled=True,
            requires_confirmation=False,
            idempotency_required=True,
        )
        self.workflow = OntologyWorkflow(
            id="workflow-runtime-definition",
            scenario_id=self.scenario.id,
            name="冻结流程 A",
            description="release A workflow",
            trigger_type="manual",
            trigger_config={},
            steps=[],
            nodes=[
                {"id": "start", "type": "start", "data": {"name": "开始"}},
                {"id": "end", "type": "end", "data": {"name": "结束"}},
            ],
            edges=[{"id": "e1", "source": "start", "target": "end", "label": ""}],
            status="active",
            enabled=True,
        )
        self.branch = OntologyBranch(
            id="branch-runtime-definition",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="main",
            description="runtime definition test branch",
            created_by_user_id=self.owner.id,
        )
        self.db.add_all(
            [
                self.tenant,
                self.owner,
                self.scenario,
                self.entity,
                self.entity_key,
                self.action,
                self.workflow,
                self.branch,
            ]
        )
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.owner.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _staging_settings(self):
        return patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        )

    def _dev_settings(self):
        return patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="dev"),
        )

    def test_prod_authoring_uses_live_definition_before_first_release(self) -> None:
        """A fresh production scene remains editable, but cannot execute yet."""
        authoring = runtime_definition_service.resolve_authoring(
            self.db,
            self.scenario,
            environment="prod",
        )

        self.assertEqual(authoring.environment, "prod")
        self.assertEqual(authoring.source, "live")
        self.assertIn(self.entity.id, authoring.entities)
        self.assertIn(self.action.id, authoring.actions)
        self.assertEqual(
            runtime_definition_service.resolve_active(
                self.db, self.scenario, environment="prod"
            ).source,
            "live",
        )

        with self.assertRaisesRegex(
            runtime_definition_service.RuntimeDefinitionError,
            "尚未发布可执行定义",
        ):
            runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment="prod",
            )

    def _capture_snapshot(self, snapshot_id: str) -> OntologySnapshot:
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        snapshot = OntologySnapshot(
            id=snapshot_id,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=self.branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.owner.id,
        )
        self.db.add(snapshot)
        self.db.flush()
        if not self.branch.base_snapshot_id:
            self.branch.base_snapshot_id = snapshot.id
        self.branch.head_snapshot_id = snapshot.id
        self.db.commit()
        return snapshot

    def _release(self, release_id: str, snapshot: OntologySnapshot, *, status: str = "released") -> OntologyRelease:
        release = OntologyRelease(
            id=release_id,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=self.branch.id,
            snapshot_id=snapshot.id,
            environment="staging",
            status=status,
            created_by_user_id=self.owner.id,
        )
        self.db.add(release)
        self.db.commit()
        return release

    def _move_live_definition_to_b(self) -> OntologySnapshot:
        workflow = self.db.get(OntologyWorkflow, self.workflow.id)
        action = self.db.get(OntologyAction, self.action.id)
        self.assertIsNotNone(workflow)
        self.assertIsNotNone(action)
        workflow.name = "可变流程 B"
        workflow.description = "live/branch B workflow"
        action.name = "可变操作 B"
        self.db.commit()
        return self._capture_snapshot("snapshot-runtime-b")

    def _release_a(self) -> tuple[OntologySnapshot, OntologyRelease]:
        snapshot = self._capture_snapshot("snapshot-runtime-a")
        return snapshot, self._release("release-runtime-a", snapshot)

    def test_staging_active_definition_stays_on_a_after_live_and_branch_move_to_b(self) -> None:
        snapshot_a, release_a = self._release_a()
        snapshot_b = self._move_live_definition_to_b()

        self.assertEqual(self.branch.head_snapshot_id, snapshot_b.id)
        self.assertEqual(self.db.get(OntologyWorkflow, self.workflow.id).name, "可变流程 B")

        with self._staging_settings():
            definition = runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment=runtime_connector_service.runtime_environment(),
            )

        frozen_workflow = runtime_definition_service.resolve_resource(
            definition, "workflow", self.workflow.id
        )
        frozen_action = runtime_definition_service.resolve_resource(
            definition, "action", self.action.id
        )
        self.assertEqual(definition.environment, "staging")
        self.assertEqual(definition.source, "release")
        self.assertEqual(definition.snapshot_id, snapshot_a.id)
        self.assertEqual(definition.release_id, release_a.id)
        self.assertEqual(frozen_workflow.name, "冻结流程 A")
        self.assertEqual(frozen_workflow.description, "release A workflow")
        self.assertEqual(frozen_action.name, "冻结操作 A")

    def test_staging_run_keeps_release_a_after_release_switch_and_records_provenance(self) -> None:
        snapshot_a, release_a = self._release_a()
        with self._staging_settings():
            definition_a = runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
            workflow_a = runtime_definition_service.resolve_resource(
                definition_a, "workflow", self.workflow.id
            )
            run, created = operations_service.enqueue_workflow_run(
                self.db,
                workflow_a,
                {"ticket": "T-A"},
                dedupe_key="dispatch-a",
                created_by_user_id=self.owner.id,
                runtime_definition=definition_a,
            )
            self.db.commit()

        self.assertTrue(created)
        self.assertEqual(run.release_id, release_a.id)
        self.assertEqual(run.definition_snapshot_id, snapshot_a.id)
        self.assertEqual(run.definition_source, "release")

        snapshot_b = self._move_live_definition_to_b()
        release_a.status = "superseded"
        release_b = self._release("release-runtime-b", snapshot_b)
        self.assertEqual(release_b.status, "released")

        original_execute = workflow_service.execute_workflow
        with self._staging_settings(), patch(
            "app.services.workflow_service.execute_workflow", wraps=original_execute
        ) as execute:
            processed = operations_service.process_available_runs(self.db)

        self.assertEqual([item.id for item in processed], [run.id])
        execute.assert_called_once()
        called_workflow = execute.call_args.args[1]
        called_definition = execute.call_args.kwargs["runtime_definition"]
        self.assertEqual(called_workflow.name, "冻结流程 A")
        self.assertEqual(called_workflow.description, "release A workflow")
        self.assertEqual(called_definition.snapshot_id, snapshot_a.id)
        self.assertEqual(called_definition.release_id, release_a.id)
        self.assertEqual(called_definition.source, "release")
        self.assertEqual(execute.call_args.kwargs["runtime_environment"], "staging")

        self.db.refresh(run)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.release_id, release_a.id)
        self.assertEqual(run.definition_snapshot_id, snapshot_a.id)
        self.assertEqual(run.definition_hash, called_definition.definition_hash)
        self.assertEqual(run.definition_source, "release")

        log = self.db.execute(
            select(ActionExecutionLog)
            .where(
                ActionExecutionLog.target_type == "workflow",
                ActionExecutionLog.target_id == self.workflow.id,
            )
            .order_by(ActionExecutionLog.created_at.desc())
        ).scalars().first()
        self.assertIsNotNone(log)
        self.assertEqual(log.environment, "staging")
        self.assertEqual(log.release_id, release_a.id)
        self.assertEqual(log.definition_snapshot_id, snapshot_a.id)
        self.assertEqual(log.definition_hash, called_definition.definition_hash)
        self.assertEqual(log.definition_source, "release")

    def test_same_workflow_dedupe_is_global_across_deployments_and_unready_action_is_blocked(self) -> None:
        snapshot_a, release_a = self._release_a()
        with self._staging_settings():
            staging_definition = runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
            staging_workflow = runtime_definition_service.resolve_resource(
                staging_definition, "workflow", self.workflow.id
            )
            staging_run, created = operations_service.enqueue_workflow_run(
                self.db,
                staging_workflow,
                {"ticket": "dedupe"},
                dedupe_key="same-delivery",
                created_by_user_id=self.owner.id,
                runtime_definition=staging_definition,
            )
            duplicate, duplicate_created = operations_service.enqueue_workflow_run(
                self.db,
                staging_workflow,
                {"ticket": "dedupe"},
                dedupe_key="same-delivery",
                created_by_user_id=self.owner.id,
                runtime_definition=staging_definition,
            )
            self.db.commit()
            staging_action = runtime_definition_service.resolve_resource(
                staging_definition, "action", self.action.id
            )
            with self.assertRaises(capability_readiness_service.CapabilityNotReady):
                workflow_service.execute_action(
                    self.db,
                    staging_action,
                    {},
                    idempotency_key="same-mutation",
                    runtime_environment="staging",
                    runtime_definition=staging_definition,
                )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, staging_run.id)
        self.assertEqual(staging_run.dedupe_key, "same-delivery")
        self.assertEqual(staging_run.release_id, release_a.id)

        with self._dev_settings():
            dev_definition = runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
            dev_workflow = runtime_definition_service.resolve_resource(
                dev_definition, "workflow", self.workflow.id
            )
            dev_action = runtime_definition_service.resolve_resource(
                dev_definition, "action", self.action.id
            )
            dev_run, dev_created = operations_service.enqueue_workflow_run(
                self.db,
                dev_workflow,
                {"ticket": "dedupe"},
                dedupe_key="same-delivery",
                created_by_user_id=self.owner.id,
                runtime_definition=dev_definition,
            )
            self.db.commit()
            with self.assertRaises(capability_readiness_service.CapabilityNotReady):
                workflow_service.execute_action(
                    self.db,
                    dev_action,
                    {},
                    idempotency_key="same-mutation",
                    runtime_environment="dev",
                    runtime_definition=dev_definition,
                )

        self.assertFalse(dev_created)
        self.assertEqual(dev_run.id, staging_run.id)
        self.assertEqual(dev_run.dedupe_key, "same-delivery")
        self.assertEqual(dev_run.definition_source, "release")
        self.assertEqual(dev_run.release_id, release_a.id)
        self.assertEqual(
            set(
                self.db.execute(
                    select(WorkflowRun.dedupe_key).where(
                        WorkflowRun.workflow_id == self.workflow.id
                    )
                ).scalars()
            ),
            {"same-delivery"},
        )
        action_logs = self.db.execute(
            select(ActionExecutionLog)
            .where(
                ActionExecutionLog.target_type == "action",
                ActionExecutionLog.target_id == self.action.id,
            )
            .order_by(ActionExecutionLog.environment.asc())
        ).scalars().all()
        self.assertEqual(action_logs, [])

    def test_resolve_for_run_rejects_a_tampered_definition_hash(self) -> None:
        snapshot, release = self._release_a()
        run = WorkflowRun(
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="staging",
            definition_snapshot_id=snapshot.id,
            release_id=release.id,
            definition_hash="0" * 64,
            definition_source="release",
        )

        with self.assertRaisesRegex(
            runtime_definition_service.RuntimeDefinitionError,
            "完整性校验失败",
        ):
            runtime_definition_service.resolve_for_run(self.db, run)

    def test_live_pinned_run_uses_authoring_only_while_its_hash_matches(self) -> None:
        definition = runtime_definition_service.resolve_authoring(
            self.db,
            self.scenario,
            environment="prod",
        )
        run = WorkflowRun(
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="prod",
            definition_snapshot_id=None,
            release_id=None,
            definition_hash=definition.definition_hash,
            definition_source=runtime_definition_service.LIVE_PINNED_RUN_SOURCE,
        )

        resolved = runtime_definition_service.resolve_for_run(self.db, run)
        self.assertEqual(resolved.source, "live")
        self.assertEqual(resolved.definition_hash, definition.definition_hash)
        self.assertIsNone(resolved.release_id)

        self.workflow.name = "排队后已修改的流程"
        self.db.commit()
        with self.assertRaisesRegex(
            runtime_definition_service.RuntimeDefinitionError,
            "任务排队后已变化",
        ):
            runtime_definition_service.resolve_for_run(self.db, run)

    def test_historic_live_run_remains_blocked_without_the_new_pin_marker(self) -> None:
        definition = runtime_definition_service.resolve_authoring(self.db, self.scenario)
        run = WorkflowRun(
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="dev",
            definition_hash=definition.definition_hash,
            definition_source="live",
        )

        with self.assertRaisesRegex(
            runtime_definition_service.RuntimeDefinitionError,
            "请重新提交任务",
        ):
            runtime_definition_service.resolve_for_run(self.db, run)

    def test_resolve_for_run_uses_the_same_release_status_guard_as_resolve_pinned(self) -> None:
        snapshot, release = self._release_a()
        definition = runtime_definition_service.resolve_execution(
            self.db,
            self.scenario,
            environment="staging",
        )
        run = WorkflowRun(
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="staging",
            definition_snapshot_id=snapshot.id,
            release_id=release.id,
            definition_hash=definition.definition_hash,
            definition_source="release",
        )

        for allowed_status in ("released", "superseded", "rolled_back"):
            release.status = allowed_status
            self.db.commit()
            resolved_run = runtime_definition_service.resolve_for_run(self.db, run)
            resolved_pin = runtime_definition_service.resolve_pinned(
                self.db,
                self.scenario,
                environment="staging",
                snapshot_id=snapshot.id,
                release_id=release.id,
                definition_hash=definition.definition_hash,
            )
            self.assertEqual(resolved_run.definition_hash, definition.definition_hash)
            self.assertEqual(resolved_pin.definition_hash, definition.definition_hash)

        release.status = "draft"
        self.db.commit()
        for resolver in (
            lambda: runtime_definition_service.resolve_for_run(self.db, run),
            lambda: runtime_definition_service.resolve_pinned(
                self.db,
                self.scenario,
                environment="staging",
                snapshot_id=snapshot.id,
                release_id=release.id,
                definition_hash=definition.definition_hash,
            ),
        ):
            with self.assertRaisesRegex(
                runtime_definition_service.RuntimeDefinitionError,
                "发布版本不一致",
            ):
                resolver()

    def test_dev_run_uses_the_same_released_snapshot_as_other_deployments(self) -> None:
        snapshot, release = self._release_a()
        definition = runtime_definition_service.resolve_execution(
            self.db,
            self.scenario,
            environment="dev",
        )
        self.assertEqual(definition.source, "release")
        self.assertEqual(definition.environment, "dev")
        self.assertEqual(definition.snapshot_id, snapshot.id)
        self.assertEqual(definition.release_id, release.id)
        run = WorkflowRun(
            scenario_id=self.scenario.id,
            workflow_id=self.workflow.id,
            environment="dev",
            definition_snapshot_id=snapshot.id,
            release_id=release.id,
            definition_hash=definition.definition_hash,
            definition_source="release",
        )
        resolved = runtime_definition_service.resolve_for_run(self.db, run)
        self.assertEqual(resolved.definition_hash, definition.definition_hash)

        run.definition_hash = "tampered"
        with self.assertRaisesRegex(
            runtime_definition_service.RuntimeDefinitionError,
            "完整性校验失败",
        ):
            runtime_definition_service.resolve_for_run(self.db, run)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
