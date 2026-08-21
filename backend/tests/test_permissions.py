from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.models import (
    ActionExecutionLog,
    BusinessScenario,
    DataMapping,
    DataMappingRefreshJob,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from app.routers import lineage, operations, permissions, scenarios
from app.services import permission_service
from app.services.auth_service import get_current_user, get_tenant_db


class PermissionIntegrationTests(unittest.TestCase):
    """真实 FastAPI 路由 + 集中权限服务的跨角色/跨租户联调。"""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-one", name="组织一")
            self.other_tenant = Tenant(id="tenant-two", name="组织二")
            self.owner = User(
                id="owner-one",
                tenant_id=self.tenant.id,
                email="owner@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-one",
                tenant_id=self.tenant.id,
                email="viewer@example.test",
                password_hash="test-only",
                status="active",
            )
            self.operator = User(
                id="operator-one",
                tenant_id=self.tenant.id,
                email="operator@example.test",
                password_hash="test-only",
                status="active",
            )
            self.outsider = User(
                id="outsider-two",
                tenant_id=self.other_tenant.id,
                email="outsider@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-private", tenant_id=self.tenant.id, name="私有场景"
            )
            self.public_scenario = BusinessScenario(
                id="scenario-public",
                tenant_id=self.tenant.id,
                is_public=True,
                name="公共场景",
            )
            self.entity = OntologyEntity(
                id="entity-private", scenario_id=self.scenario.id, name="客户"
            )
            self.mapping_source = DataSource(
                id="source-mapping-async",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="不应在 HTTP 请求中连接的映射源",
                type="sqlite",
                config={"path": "/not-used-by-route.sqlite3"},
            )
            self.mapping = DataMapping(
                id="mapping-async-only",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                data_source_id=self.mapping_source.id,
                table_name="orders",
                column_map={"姓名": "customer_name"},
            )
            self.public_entity = OntologyEntity(
                id="entity-public", scenario_id=self.public_scenario.id, name="公开客户"
            )
            self.public_property = OntologyProperty(
                id="property-public",
                entity_id=self.entity.id,
                name="姓名",
            )
            self.secret_property = OntologyProperty(
                id="property-secret",
                entity_id=self.entity.id,
                name="身份证号",
                is_sensitive=True,
            )
            self.public_scenario_property = OntologyProperty(
                id="property-public-scenario",
                entity_id=self.public_entity.id,
                name="姓名",
            )
            self.instance = OntologyInstance(
                id="object-regular",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="张三",
                attributes={"姓名": "张三", "身份证号": "110101199001011234"},
            )
            self.restricted_instance = OntologyInstance(
                id="object-restricted",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="受限客户",
                attributes={"姓名": "李四", "身份证号": "110101199001015678"},
                access_scope="restricted",
            )
            self.public_instance = OntologyInstance(
                id="object-public",
                scenario_id=self.public_scenario.id,
                entity_id=self.public_entity.id,
                name="公开对象",
                attributes={"姓名": "王五"},
            )
            self.action = OntologyAction(
                id="action-restricted",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="受限操作",
                access_scope="restricted",
                requires_confirmation=False,
                idempotency_required=False,
            )
            self.workflow = OntologyWorkflow(
                id="workflow-restricted",
                scenario_id=self.scenario.id,
                name="受限工作流",
                status="active",
                enabled=True,
                access_scope="restricted",
            )
            self.task_workflow = OntologyWorkflow(
                id="workflow-task-standard",
                scenario_id=self.scenario.id,
                name="普通审批工作流",
                status="active",
                enabled=True,
            )
            self.task_run = WorkflowRun(
                id="run-task-standard",
                scenario_id=self.scenario.id,
                workflow_id=self.task_workflow.id,
                trigger_source="manual",
                status="awaiting_approval",
                created_by_user_id=self.owner.id,
            )
            self.task_approval = WorkflowApprovalRequest(
                id="approval-task-standard",
                workflow_run_id=self.task_run.id,
                scenario_id=self.scenario.id,
                node_id="approval-standard",
                status="pending",
            )
            self.restricted_action_log = ActionExecutionLog(
                id="log-restricted-action",
                scenario_id=self.scenario.id,
                target_type="action",
                target_id=self.action.id,
                target_name=self.action.name,
                input_params={"secret": "不应通过日志泄露"},
                result={"external": "不应通过日志泄露"},
                status="success",
                mode="execute",
            )
            db.add_all(
                [
                    self.tenant,
                    self.other_tenant,
                    self.owner,
                    self.viewer,
                    self.operator,
                    self.outsider,
                    self.scenario,
                    self.public_scenario,
                    self.entity,
                    self.mapping_source,
                    self.mapping,
                    self.public_entity,
                    self.public_property,
                    self.secret_property,
                    self.public_scenario_property,
                    self.instance,
                    self.restricted_instance,
                    self.public_instance,
                    self.action,
                    self.workflow,
                    self.task_workflow,
                    self.task_run,
                    self.task_approval,
                    self.restricted_action_log,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.operator.id, role_key="operator"
            )
            db.commit()
        finally:
            db.close()

        self.current_user_id = self.owner.id
        self.current_tenant_id = self.tenant.id
        self.app = FastAPI()
        self.app.include_router(scenarios.router, prefix="/api")
        self.app.include_router(operations.router, prefix="/api")
        self.app.include_router(operations.operations_router, prefix="/api")
        self.app.include_router(permissions.router, prefix="/api")
        self.app.include_router(lineage.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.current_user_id, tenant_id=self.current_tenant_id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user_id
            request_db.info["tenant_id"] = self.current_tenant_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _as(self, user: User, tenant: Tenant) -> None:
        self.current_user_id = user.id
        self.current_tenant_id = tenant.id

    def _grant(self, *, resource_type: str, resource_id: str, verb: str) -> None:
        self._as(self.owner, self.tenant)
        response = self.client.post(
            "/api/permissions/grants",
            json={
                "role_key": "operator",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "verb": verb,
                "effect": "allow",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_management_api_seeds_roles_and_rejects_cross_tenant_member(self) -> None:
        roles = self.client.get("/api/permissions/roles")
        self.assertEqual(roles.status_code, 200, roles.text)
        self.assertEqual({item["key"] for item in roles.json()}, {"owner", "admin", "operator", "viewer"})

        update = self.client.post(
            "/api/permissions/members",
            json={"user_id": self.viewer.id, "role_key": "viewer"},
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["role_key"], "viewer")

        denied = self.client.post(
            "/api/permissions/members",
            json={"user_id": self.outsider.id, "role_key": "viewer"},
        )
        self.assertEqual(denied.status_code, 403)

        resources = self.client.get(f"/api/permissions/resources/{self.scenario.id}")
        self.assertEqual(resources.status_code, 200, resources.text)
        sensitive = next(item for item in resources.json() if item["id"] == self.secret_property.id)
        self.assertTrue(sensitive["is_sensitive"])

    def test_object_attribute_and_cross_tenant_boundaries_are_enforced(self) -> None:
        self._as(self.viewer, self.tenant)
        detail = self.client.get(f"/api/scenarios/{self.scenario.id}/objects/{self.instance.id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["attributes"], {"姓名": "张三"})

        restricted = self.client.get(
            f"/api/scenarios/{self.scenario.id}/objects/{self.restricted_instance.id}"
        )
        self.assertEqual(restricted.status_code, 403)

        denied_write = self.client.put(
            f"/api/scenarios/instances/{self.instance.id}",
            json={"entity_id": self.entity.id, "name": "被拒绝", "attributes": {"姓名": "张三"}},
        )
        self.assertEqual(denied_write.status_code, 403)

        self._as(self.outsider, self.other_tenant)
        cross_tenant = self.client.get(
            f"/api/scenarios/{self.scenario.id}/objects/{self.instance.id}"
        )
        self.assertEqual(cross_tenant.status_code, 404)

        public_read = self.client.get(
            f"/api/scenarios/{self.public_scenario.id}/objects/{self.public_instance.id}"
        )
        self.assertEqual(public_read.status_code, 200, public_read.text)
        self.assertEqual(public_read.json()["attributes"], {"姓名": "王五"})

    def test_scenario_detail_exposes_server_evaluated_write_capability(self) -> None:
        """详情页的编辑能力必须服从实时 ACL，而不是由客户端猜测角色。"""
        owner_detail = self.client.get(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(owner_detail.status_code, 200, owner_detail.text)
        self.assertTrue(owner_detail.json()["can_write"])

        self._as(self.viewer, self.tenant)
        viewer_detail = self.client.get(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(viewer_detail.status_code, 200, viewer_detail.text)
        self.assertFalse(viewer_detail.json()["can_write"])

        self._as(self.operator, self.tenant)
        operator_detail = self.client.get(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(operator_detail.status_code, 200, operator_detail.text)
        self.assertTrue(operator_detail.json()["can_write"])

        # Explicit deny wins over the operator's default write role.
        self._as(self.owner, self.tenant)
        denied = self.client.post(
            "/api/permissions/grants",
            json={
                "role_key": "operator",
                "resource_type": "scenario",
                "resource_id": self.scenario.id,
                "verb": "write",
                "effect": "deny",
            },
        )
        self.assertEqual(denied.status_code, 200, denied.text)
        self._as(self.operator, self.tenant)
        denied_detail = self.client.get(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(denied_detail.status_code, 200, denied_detail.text)
        self.assertFalse(denied_detail.json()["can_write"])

        # Foreign-tenant public scenes are visible but cannot be modified.
        self._as(self.outsider, self.other_tenant)
        public_detail = self.client.get(f"/api/scenarios/{self.public_scenario.id}")
        self.assertEqual(public_detail.status_code, 200, public_detail.text)
        self.assertFalse(public_detail.json()["can_write"])

    def test_task_rows_expose_server_evaluated_execution_and_approval_capabilities(self) -> None:
        def task_for_current_user() -> dict:
            response = self.client.get(f"/api/tasks?scenario_id={self.scenario.id}")
            self.assertEqual(response.status_code, 200, response.text)
            return next(item for item in response.json() if item["id"] == self.task_run.id)

        owner_task = task_for_current_user()
        self.assertTrue(owner_task["pending_approval"])
        self.assertTrue(owner_task["can_execute"])
        self.assertTrue(owner_task["can_approve"])

        self._as(self.operator, self.tenant)
        operator_task = task_for_current_user()
        self.assertTrue(operator_task["can_execute"])
        self.assertFalse(operator_task["can_approve"])

        self._as(self.viewer, self.tenant)
        viewer_task = task_for_current_user()
        self.assertFalse(viewer_task["can_execute"])
        self.assertFalse(viewer_task["can_approve"])

    def test_attribute_writes_require_explicit_property_access_and_hide_legacy_fields(self) -> None:
        db = self.Session()
        try:
            instance = db.get(OntologyInstance, self.instance.id)
            instance.attributes = {
                "姓名": "张三",
                "身份证号": "110101199001011234",
                "遗留未定义字段": "不可泄露",
            }
            db.commit()
        finally:
            db.close()

        self._as(self.viewer, self.tenant)
        detail = self.client.get(f"/api/scenarios/{self.scenario.id}/objects/{self.instance.id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["attributes"], {"姓名": "张三"})

        # Operators can write ordinary objects, but not individual fields until the
        # field's write ACL is granted.
        self._as(self.operator, self.tenant)
        denied = self.client.put(
            f"/api/scenarios/instances/{self.instance.id}",
            json={"entity_id": self.entity.id, "name": "张三", "attributes": {"姓名": "修改"}},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        self._as(self.owner, self.tenant)
        undefined = self.client.post(
            f"/api/scenarios/{self.scenario.id}/instances",
            json={
                "entity_id": self.entity.id,
                "name": "未知字段对象",
                "attributes": {"未定义字段": "禁止写入"},
            },
        )
        self.assertEqual(undefined.status_code, 422, undefined.text)

        self._grant(resource_type="property", resource_id=self.public_property.id, verb="write")
        self._as(self.operator, self.tenant)
        allowed = self.client.put(
            f"/api/scenarios/instances/{self.instance.id}",
            json={"entity_id": self.entity.id, "name": "张三", "attributes": {"姓名": "已授权修改"}},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["attributes"], {"姓名": "已授权修改"})

    def test_execution_logs_do_not_bypass_action_or_workflow_acl(self) -> None:
        self._as(self.viewer, self.tenant)
        denied_logs = self.client.get(f"/api/scenarios/{self.scenario.id}/execution-logs")
        self.assertEqual(denied_logs.status_code, 200, denied_logs.text)
        self.assertEqual(denied_logs.json(), [])

        self._as(self.owner, self.tenant)
        owner_logs = self.client.get(f"/api/scenarios/{self.scenario.id}/execution-logs")
        self.assertEqual(owner_logs.status_code, 200, owner_logs.text)
        self.assertEqual(owner_logs.json()[0]["id"], self.restricted_action_log.id)
        self.assertEqual(owner_logs.json()[0]["input_params"], {"secret": "不应通过日志泄露"})

    def test_action_and_workflow_execute_only_after_explicit_restricted_grants(self) -> None:
        self._as(self.operator, self.tenant)
        action_denied = self.client.post(
            f"/api/scenarios/actions/{self.action.id}/execute",
            json={"params": {}, "confirm": True, "idempotency_key": "operator-denied"},
        )
        self.assertEqual(action_denied.status_code, 403)
        workflow_denied = self.client.post(
            f"/api/scenarios/workflows/{self.workflow.id}/execute",
            json={"params": {}},
        )
        self.assertEqual(workflow_denied.status_code, 403)

        self._grant(resource_type="action", resource_id=self.action.id, verb="read")
        self._grant(resource_type="action", resource_id=self.action.id, verb="execute")
        self._grant(resource_type="workflow", resource_id=self.workflow.id, verb="execute")

        self._as(self.operator, self.tenant)
        preview = self.client.post(
            f"/api/scenarios/actions/{self.action.id}/execute",
            json={"params": {}, "dry_run": True},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["status"], "dry_run")

        workflow = self.client.post(
            f"/api/scenarios/workflows/{self.workflow.id}/execute",
            json={"params": {}},
        )
        self.assertEqual(workflow.status_code, 202, workflow.text)
        self.assertEqual(workflow.json()["status"], "queued")
        self.assertEqual(workflow.json()["workflow_id"], self.workflow.id)
        self.assertEqual(workflow.json()["input_params"], {})

        # 旧 /execute 是 /runs 的兼容别名：两条 HTTP 入口都只能创建可恢复任务，
        # 绝不能在请求内同步执行工作流。
        canonical = self.client.post(
            f"/api/scenarios/workflows/{self.workflow.id}/runs",
            json={"params": {"source": "canonical"}},
        )
        self.assertEqual(canonical.status_code, 202, canonical.text)
        self.assertEqual(canonical.json()["status"], "queued")
        self.assertEqual(canonical.json()["input_params"], {"source": "canonical"})

        db = self.Session()
        try:
            runs = db.query(WorkflowRun).filter_by(workflow_id=self.workflow.id).all()
            self.assertEqual(len(runs), 2)
            self.assertTrue(all(run.status == "queued" for run in runs))
            self.assertTrue(all(run.created_by_user_id == self.operator.id for run in runs))
        finally:
            db.close()

    def test_legacy_mapping_refresh_and_import_only_enqueue_jobs(self) -> None:
        """Legacy aliases must never synchronously resolve a connector or import objects."""
        db = self.Session()
        try:
            instance_count_before = len(
                db.execute(
                    select(OntologyInstance).where(
                        OntologyInstance.scenario_id == self.scenario.id
                    )
                ).scalars().all()
            )
        finally:
            db.close()

        with patch(
            "app.routers.scenarios.mapping_refresh_service.resolve_mapping_data_source"
        ) as resolve_source, patch(
            "app.routers.scenarios.ontology_service.import_instances_from_mapping"
        ) as import_instances:
            refresh = self.client.post(
                f"/api/scenarios/mappings/{self.mapping.id}/refresh",
                json={"limit": 17},
            )
            legacy_import = self.client.post(
                f"/api/scenarios/mappings/{self.mapping.id}/import",
                json={"limit": 99},
            )

        self.assertEqual(refresh.status_code, 202, refresh.text)
        self.assertEqual(legacy_import.status_code, 202, legacy_import.text)
        self.assertEqual(refresh.json()["status"], "queued")
        self.assertEqual(refresh.json()["limit"], 17)
        self.assertEqual(legacy_import.json()["id"], refresh.json()["id"])
        resolve_source.assert_not_called()
        import_instances.assert_not_called()

        db = self.Session()
        try:
            jobs = db.execute(
                select(DataMappingRefreshJob).where(
                    DataMappingRefreshJob.mapping_id == self.mapping.id
                )
            ).scalars().all()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].status, "queued")
            instance_count_after = len(
                db.execute(
                    select(OntologyInstance).where(
                        OntologyInstance.scenario_id == self.scenario.id
                    )
                ).scalars().all()
            )
            self.assertEqual(instance_count_after, instance_count_before)
        finally:
            db.close()

    def test_missing_context_is_fail_closed(self) -> None:
        db = self.Session()
        try:
            instance = db.get(OntologyInstance, self.instance.id)
            self.assertFalse(permission_service.check_object(db, instance, "read").allowed)
            with self.assertRaises(Exception) as error:
                permission_service.require_object_permission(db, instance, "read")
            self.assertEqual(getattr(error.exception, "status_code", None), 401)
        finally:
            db.close()

    def test_lineage_honors_resource_acl_and_does_not_publish_public_runs(self) -> None:
        self._as(self.viewer, self.tenant)
        graph = self.client.get(f"/api/lineage/scenarios/{self.scenario.id}")
        self.assertEqual(graph.status_code, 200, graph.text)
        self.assertNotIn(
            f"object:{self.restricted_instance.id}",
            {node["id"] for node in graph.json()["nodes"]},
        )

        self._as(self.outsider, self.other_tenant)
        public_graph = self.client.get(f"/api/lineage/scenarios/{self.public_scenario.id}")
        self.assertEqual(public_graph.status_code, 403, public_graph.text)
