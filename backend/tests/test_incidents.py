"""P1 Incident / Case 中心的 API、审计与 ACL 回归测试。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import AuthorizationGrant, BusinessScenario, Tenant, User
from app.routers import incidents
from app.services import permission_service
from app.services.auth_service import get_current_user, get_tenant_db


class IncidentCenterApiTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-incidents", name="运营租户")
            self.other_tenant = Tenant(id="tenant-incidents-other", name="外部租户")
            self.owner = User(
                id="owner-incidents",
                tenant_id=self.tenant.id,
                email="owner-incidents@example.test",
                password_hash="test-only",
                status="active",
            )
            self.operator = User(
                id="operator-incidents",
                tenant_id=self.tenant.id,
                email="operator-incidents@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-incidents",
                tenant_id=self.tenant.id,
                email="viewer-incidents@example.test",
                password_hash="test-only",
                status="active",
            )
            self.outsider = User(
                id="outsider-incidents",
                tenant_id=self.other_tenant.id,
                email="outsider-incidents@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-incidents",
                tenant_id=self.tenant.id,
                name="订单异常运营",
                status="active",
            )
            self.public_foreign_scenario = BusinessScenario(
                id="scenario-incidents-public-foreign",
                tenant_id=self.other_tenant.id,
                name="外部公开场景",
                is_public=True,
            )
            db.add_all(
                [
                    self.tenant,
                    self.other_tenant,
                    self.owner,
                    self.operator,
                    self.viewer,
                    self.outsider,
                    self.scenario,
                    self.public_foreign_scenario,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.operator.id, role_key="operator"
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            db.commit()
            self.organization_id = organization.id
        finally:
            db.close()

        self.current_user_id = self.owner.id
        self.current_tenant_id = self.tenant.id
        self.app = FastAPI()
        self.app.include_router(incidents.router, prefix="/api")

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

    def _create_case(self) -> dict:
        response = self.client.post(
            f"/api/incidents/scenarios/{self.scenario.id}",
            json={
                "title": "订单金额异常",
                "description": "订单 ORD-001 金额超过阈值。",
                "severity": "critical",
                "source": "rule",
                "source_ref": "rule:amount-threshold",
                "assignee_user_id": self.operator.id,
                "context": {"order_id": "ORD-001", "amount": 12800},
                "comment": "规则自动创建",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_case_lifecycle_records_durable_history(self) -> None:
        created = self._create_case()
        self.assertEqual(created["status"], "open")
        self.assertEqual(created["history_count"], 1)
        self.assertEqual(created["created_by_user_id"], self.owner.id)
        self.assertEqual(created["assignee_user_id"], self.operator.id)

        listed = self.client.get(f"/api/incidents/scenarios/{self.scenario.id}")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [created["id"]])

        updated = self.client.patch(
            f"/api/incidents/{created['id']}",
            json={
                "title": "订单金额异常待复核",
                "severity": "high",
                "context": {"order_id": "ORD-001", "amount": 12800, "review": True},
                "comment": "已补充复核上下文",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["history_count"], 2)
        self.assertEqual(updated.json()["severity"], "high")

        acknowledged = self.client.post(
            f"/api/incidents/{created['id']}/acknowledge",
            json={"comment": "已接单"},
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertEqual(acknowledged.json()["status"], "acknowledged")
        self.assertEqual(acknowledged.json()["acknowledged_by_user_id"], self.owner.id)

        resolved = self.client.post(
            f"/api/incidents/{created['id']}/resolve",
            json={"resolution": "人工复核后确认正常，已关闭。", "comment": "复核完成"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["status"], "resolved")
        self.assertEqual(resolved.json()["resolved_by_user_id"], self.owner.id)
        self.assertEqual(resolved.json()["history_count"], 4)

        history = self.client.get(f"/api/incidents/{created['id']}/history")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(
            [item["action"] for item in history.json()],
            ["created", "updated", "acknowledged", "resolved"],
        )
        self.assertEqual(history.json()[-1]["changes"]["resolution"], "人工复核后确认正常，已关闭。")

        immutable = self.client.patch(
            f"/api/incidents/{created['id']}", json={"title": "不应再改"}
        )
        self.assertEqual(immutable.status_code, 409, immutable.text)

    def test_case_routes_enforce_scenario_acl_and_tenant_boundary(self) -> None:
        created = self._create_case()
        db = self.Session()
        try:
            db.add_all(
                [
                    AuthorizationGrant(
                        organization_id=self.organization_id,
                        user_id=self.viewer.id,
                        resource_type="scenario",
                        resource_id=self.scenario.id,
                        verb="read",
                        effect="deny",
                        created_by_user_id=self.owner.id,
                    ),
                    AuthorizationGrant(
                        organization_id=self.organization_id,
                        user_id=self.viewer.id,
                        resource_type="scenario",
                        resource_id=self.scenario.id,
                        verb="write",
                        effect="deny",
                        created_by_user_id=self.owner.id,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        self._as(self.viewer, self.tenant)
        self.assertEqual(
            self.client.get(f"/api/incidents/scenarios/{self.scenario.id}").status_code,
            403,
        )
        self.assertEqual(self.client.get(f"/api/incidents/{created['id']}").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/api/incidents/{created['id']}/acknowledge", json={"comment": "越权"}
            ).status_code,
            403,
        )

        self._as(self.outsider, self.other_tenant)
        self.assertEqual(self.client.get(f"/api/incidents/{created['id']}").status_code, 404)
        # Public scenario definitions do not make operational Case records public.
        self._as(self.owner, self.tenant)
        self.assertEqual(
            self.client.get(
                f"/api/incidents/scenarios/{self.public_foreign_scenario.id}"
            ).status_code,
            403,
        )

    def test_invalid_assignee_and_resolve_contract_are_rejected(self) -> None:
        invalid = self.client.post(
            f"/api/incidents/scenarios/{self.scenario.id}",
            json={"title": "无效负责人", "assignee_user_id": self.outsider.id},
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)

        created = self._create_case()
        missing_resolution = self.client.post(
            f"/api/incidents/{created['id']}/resolve", json={"resolution": "   "}
        )
        self.assertEqual(missing_resolution.status_code, 422, missing_resolution.text)
