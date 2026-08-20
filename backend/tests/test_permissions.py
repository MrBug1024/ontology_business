from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.models import (
    BusinessScenario,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyWorkflow,
    Tenant,
    User,
)
from app.routers import lineage, permissions, scenarios
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
                    self.public_entity,
                    self.public_property,
                    self.secret_property,
                    self.public_scenario_property,
                    self.instance,
                    self.restricted_instance,
                    self.public_instance,
                    self.action,
                    self.workflow,
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
        self.assertEqual(workflow.status_code, 200, workflow.text)
        self.assertEqual(workflow.json()["status"], "success")

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
