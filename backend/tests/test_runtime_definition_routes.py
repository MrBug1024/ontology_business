"""Route-level regression tests for frozen staging runtime definitions.

These tests deliberately mutate the live rows after creating a release.  A
staging request may use the live row only to locate the owning scenario; all
definition fields and durable provenance must come from the released snapshot.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    ActionExecutionLog,
    BusinessScenario,
    DataMapping,
    DataSource,
    EventEnvelope,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRelation,
    OntologyRelease,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowRun,
)
from app.routers import scenarios as scenarios_router
from app.services import connector_service, permission_service, release_service
from app.services.auth_service import get_current_user


class RuntimeDefinitionRouteTests(unittest.TestCase):
    """Staging routes must resolve immutable releases rather than live ORM rows."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)

        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-runtime", name="运行时测试租户")
            self.user = User(
                id="user-runtime",
                tenant_id=self.tenant.id,
                email="runtime-owner@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-runtime",
                tenant_id=self.tenant.id,
                name="运行时发布场景",
                status="active",
            )
            self.entity = OntologyEntity(
                id="entity-runtime",
                scenario_id=self.scenario.id,
                name="订单",
            )
            self.key = OntologyProperty(
                id="property-runtime-key",
                entity_id=self.entity.id,
                name="订单编号",
                is_key=True,
                is_title=True,
                is_required=True,
            )
            self.source = DataSource(
                id="source-runtime",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="发布版订单源",
                type="sqlite",
                config={},
            )
            self.mapping = DataMapping(
                id="mapping-runtime",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                data_source_id=self.source.id,
                data_source_binding_key="runtime-orders",
                data_source_binding_ref={"adapter": "sqlite"},
                table_name="orders",
                column_map={},
            )
            self.relation = OntologyRelation(
                id="relation-runtime",
                scenario_id=self.scenario.id,
                name="订单关联",
                source_entity_id=self.entity.id,
                target_entity_id=self.entity.id,
            )
            self.action = OntologyAction(
                id="action-runtime",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="发布版操作 A",
                executor_type="sql",
                # Staging execution must use the governed logical binding even
                # for a side-effect-free dry run.
                executor_config={
                    "sql": "SELECT 'release-a'",
                    "data_source_binding_key": "runtime-orders",
                    "data_source_binding_ref": {"adapter": "sqlite"},
                },
                requires_confirmation=False,
                idempotency_required=False,
            )
            self.rule = OntologyRule(
                id="rule-runtime",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="发布版规则 A",
                condition={},
            )
            self.event = OntologyEvent(
                id="event-runtime",
                scenario_id=self.scenario.id,
                name="发布版事件 A",
                enabled=True,
            )
            self.workflow = OntologyWorkflow(
                id="workflow-runtime",
                scenario_id=self.scenario.id,
                name="发布版工作流 A",
                trigger_type="manual",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "开始"}},
                    {"id": "end", "type": "end", "data": {"label": "结束"}},
                ],
                edges=[{"id": "start-end", "source": "start", "target": "end"}],
                status="active",
                enabled=True,
            )
            self.subscriber = OntologyWorkflow(
                id="workflow-subscriber",
                scenario_id=self.scenario.id,
                name="发布版事件订阅工作流 A",
                trigger_type="event",
                trigger_config={"event_id": self.event.id},
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "开始"}},
                    {"id": "end", "type": "end", "data": {"label": "结束"}},
                ],
                edges=[{"id": "start-end", "source": "start", "target": "end"}],
                status="active",
                enabled=True,
            )
            db.add_all(
                [
                    self.tenant,
                    self.user,
                    self.scenario,
                    self.entity,
                    self.key,
                    self.source,
                    self.mapping,
                    self.relation,
                    self.action,
                    self.rule,
                    self.event,
                    self.workflow,
                    self.subscriber,
                ]
            )
            db.commit()
            permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.user.id
            )
            binding = connector_service.upsert_binding(
                db,
                self.scenario,
                environment="staging",
                binding_key_value="runtime-orders",
                kind="data_source",
                connector_id=self.source.id,
                created_by_user_id=self.user.id,
            )
            # This route test exercises immutable release resolution, not an
            # external connection probe.  Persist the same checked state a
            # successful health check would create.
            binding.health_status = "healthy"
            binding.health_message = ""
            binding.checked_at = datetime.now(timezone.utc)
            binding.connector_signature = connector_service.connector_signature(
                "data_source", self.source
            )
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(scenarios_router.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.user.id, tenant_id=self.tenant.id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.user.id
            request_db.info["tenant_id"] = self.tenant.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _staging_settings(self):
        return patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        )

    def _create_staging_release(self) -> tuple[str, str]:
        """Capture release A, then turn its mutable live rows into version B."""
        db = self.Session()
        try:
            scenario = db.get(BusinessScenario, self.scenario.id)
            self.assertIsNotNone(scenario)
            branch = OntologyBranch(
                id="branch-runtime",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="runtime-main",
                created_by_user_id=self.user.id,
            )
            db.add(branch)
            db.flush()
            content = release_service.capture_snapshot_content(db, scenario)
            connector_audit = release_service._require_snapshot_connectors(
                db,
                scenario,
                content,
                environment="staging",
            )
            snapshot = OntologySnapshot(
                id="snapshot-runtime-a",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                branch_id=branch.id,
                kind="merge",
                content=content,
                content_hash=release_service.snapshot_hash(content),
                created_by_user_id=self.user.id,
            )
            db.add(snapshot)
            db.flush()
            branch.head_snapshot_id = snapshot.id
            release = OntologyRelease(
                id="release-runtime-a",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                branch_id=branch.id,
                snapshot_id=snapshot.id,
                environment="staging",
                status="released",
                connector_audit=connector_audit,
                created_by_user_id=self.user.id,
            )
            db.add(release)

            # Simulate a later dev edit.  The route must keep accepting an
            # existing resource id but it must not use any of these B fields.
            action = db.get(OntologyAction, self.action.id)
            event = db.get(OntologyEvent, self.event.id)
            workflow = db.get(OntologyWorkflow, self.workflow.id)
            subscriber = db.get(OntologyWorkflow, self.subscriber.id)
            action.name = "开发中操作 B（不得运行）"
            action.enabled = False
            event.name = "开发中事件 B（不得发布）"
            event.enabled = False
            workflow.name = "开发中工作流 B（不得运行）"
            workflow.status = "disabled"
            workflow.enabled = False
            subscriber.name = "开发中订阅工作流 B（不得运行）"
            subscriber.trigger_config = {"event_id": "other-live-event"}
            subscriber.status = "disabled"
            subscriber.enabled = False
            db.commit()
            return snapshot.id, release.id
        finally:
            db.close()

    def test_staging_routes_execute_the_released_snapshot_and_record_provenance(self) -> None:
        snapshot_id, release_id = self._create_staging_release()

        with self._staging_settings():
            action_response = self.client.post(
                f"/api/scenarios/actions/{self.action.id}/execute",
                json={"params": {}, "dry_run": True},
            )
            self.assertEqual(action_response.status_code, 200, action_response.text)
            self.assertEqual(
                action_response.json()["result"]["plan"]["action_name"],
                "发布版操作 A",
            )
            self.assertEqual(action_response.json()["definition_source"], "release")
            self.assertEqual(action_response.json()["definition_snapshot_id"], snapshot_id)
            self.assertEqual(action_response.json()["release_id"], release_id)

            workflow_response = self.client.post(
                f"/api/scenarios/workflows/{self.workflow.id}/runs",
                json={"params": {"source": "route-test"}},
            )
            self.assertEqual(workflow_response.status_code, 202, workflow_response.text)
            self.assertEqual(workflow_response.json()["workflow_name"], "发布版工作流 A")
            self.assertEqual(workflow_response.json()["definition_source"], "release")
            self.assertEqual(workflow_response.json()["definition_snapshot_id"], snapshot_id)
            self.assertEqual(workflow_response.json()["release_id"], release_id)

            event_response = self.client.post(
                f"/api/scenarios/events/{self.event.id}/publish",
                json={"payload": {"source": "route-test"}},
            )
            self.assertEqual(event_response.status_code, 200, event_response.text)
            self.assertEqual(event_response.json()["name"], "发布版事件 A")
            self.assertEqual(event_response.json()["definition_source"], "release")
            self.assertEqual(event_response.json()["definition_snapshot_id"], snapshot_id)
            self.assertEqual(event_response.json()["release_id"], release_id)
            self.assertEqual(len(event_response.json()["queued_workflow_run_ids"]), 1)

        db = self.Session()
        try:
            action_log = db.query(ActionExecutionLog).one()
            self.assertEqual(action_log.target_name, "发布版操作 A")
            self.assertEqual(action_log.environment, "staging")
            self.assertEqual(action_log.definition_source, "release")
            self.assertEqual(action_log.definition_snapshot_id, snapshot_id)
            self.assertEqual(action_log.release_id, release_id)

            manual_run = db.query(WorkflowRun).filter_by(workflow_id=self.workflow.id).one()
            self.assertEqual(manual_run.environment, "staging")
            self.assertEqual(manual_run.definition_source, "release")
            self.assertEqual(manual_run.definition_snapshot_id, snapshot_id)
            self.assertEqual(manual_run.release_id, release_id)

            envelope = db.query(EventEnvelope).one()
            self.assertEqual(envelope.name, "发布版事件 A")
            self.assertEqual(envelope.environment, "staging")
            self.assertEqual(envelope.definition_source, "release")
            self.assertEqual(envelope.definition_snapshot_id, snapshot_id)
            self.assertEqual(envelope.release_id, release_id)

            subscriber_run = (
                db.query(WorkflowRun)
                .filter_by(event_envelope_id=envelope.id, workflow_id=self.subscriber.id)
                .one()
            )
            self.assertEqual(subscriber_run.definition_source, "release")
            self.assertEqual(subscriber_run.definition_snapshot_id, snapshot_id)
            self.assertEqual(subscriber_run.release_id, release_id)
        finally:
            db.close()

    def test_staging_routes_fail_closed_when_the_scenario_has_no_release(self) -> None:
        with self._staging_settings():
            action_response = self.client.post(
                f"/api/scenarios/actions/{self.action.id}/execute",
                json={"params": {}, "dry_run": True},
            )
            workflow_response = self.client.post(
                f"/api/scenarios/workflows/{self.workflow.id}/runs",
                json={"params": {}},
            )
            event_response = self.client.post(
                f"/api/scenarios/events/{self.event.id}/publish",
                json={"payload": {}},
            )

        # The event route currently wraps service errors as a generic 400,
        # while action/workflow use 409.  All three must reject before a live
        # action/log/envelope/run can be created.
        self.assertEqual(action_response.status_code, 409, action_response.text)
        self.assertEqual(workflow_response.status_code, 409, workflow_response.text)
        self.assertEqual(event_response.status_code, 400, event_response.text)
        for response in (action_response, workflow_response, event_response):
            self.assertIn("尚未发布", response.text)

        db = self.Session()
        try:
            self.assertEqual(db.query(ActionExecutionLog).count(), 0)
            self.assertEqual(db.query(EventEnvelope).count(), 0)
            self.assertEqual(db.query(WorkflowRun).count(), 0)
        finally:
            db.close()

    def test_direct_crud_delete_cannot_remove_active_release_lookup_anchors(self) -> None:
        """The ordinary CRUD routes must not bypass release deletion guards."""
        self._create_staging_release()

        for path, resource_id, label in (
            ("entities", self.entity.id, "实体"),
            ("relations", self.relation.id, "关系"),
            ("mappings", self.mapping.id, "数据映射"),
            ("actions", self.action.id, "Action"),
            ("rules", self.rule.id, "规则"),
            ("events", self.event.id, "事件"),
            ("workflows", self.workflow.id, "工作流"),
        ):
            response = self.client.delete(f"/api/scenarios/{path}/{resource_id}")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("活动环境发布引用", response.json()["detail"])
            self.assertIn(label, response.json()["detail"])

        scenario_response = self.client.delete(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(scenario_response.status_code, 409, scenario_response.text)
        self.assertIn("活动环境发布引用", scenario_response.json()["detail"])

        db = self.Session()
        try:
            self.assertIsNotNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNotNone(db.get(OntologyEntity, self.entity.id))
            self.assertIsNotNone(db.get(OntologyRelation, self.relation.id))
            self.assertIsNotNone(db.get(DataMapping, self.mapping.id))
            self.assertIsNotNone(db.get(OntologyAction, self.action.id))
            self.assertIsNotNone(db.get(OntologyRule, self.rule.id))
            self.assertIsNotNone(db.get(OntologyEvent, self.event.id))
            self.assertIsNotNone(db.get(OntologyWorkflow, self.workflow.id))
        finally:
            db.close()
