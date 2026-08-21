"""P2 regression tests for declaration-only governed function resources."""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import BusinessScenario, FunctionDefinition, Tenant, User
from app.routers import scenarios as scenarios_router
from app.services import (
    function_definition_service,
    package_service,
    permission_service,
    release_service,
    runtime_definition_service,
)
from app.services.auth_service import get_current_user


def _contract_schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


class FunctionDefinitionServiceTests(unittest.TestCase):
    def test_normalizes_typed_contract_and_rejects_executable_schema_fields(self) -> None:
        normalized = function_definition_service.normalize_definition(
            {
                "name": "计算订单风险",
                "description": "只声明输入输出",
                "input_schema": _contract_schema({"order_id": {"type": "string"}}, ["order_id"]),
                "output_schema": _contract_schema({"risk": {"type": "string"}}, ["risk"]),
                "tags": ["风险", "订单", "风险"],
                "visibility": "tenant",
            }
        )
        self.assertEqual(normalized["tags"], ["风险", "订单"])
        self.assertEqual(normalized["visibility"], "tenant")
        self.assertEqual(normalized["input_schema"]["type"], "object")

        for unsafe_schema in (
            {"type": "object", "properties": {}, "code": "return 1"},
            {"type": "object", "properties": {}, "$ref": "https://attacker.test/schema"},
        ):
            with self.assertRaises(function_definition_service.FunctionDefinitionError):
                function_definition_service.normalize_definition(
                    {
                        "name": "不安全函数",
                        "input_schema": unsafe_schema,
                        "output_schema": _contract_schema(),
                    }
                )


class FunctionDefinitionRouteTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-functions", name="函数定义租户")
            self.user = User(
                id="user-functions",
                tenant_id=self.tenant.id,
                email="functions.owner@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-functions",
                tenant_id=self.tenant.id,
                name="函数定义场景",
            )
            db.add_all([self.tenant, self.user, self.scenario])
            db.commit()
            permission_service.ensure_organization(db, self.tenant.id, owner_user_id=self.user.id)
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

    def _payload(self, *, name: str = "计算订单风险") -> dict:
        return {
            "name": name,
            "description": "纯声明式风险计算契约",
            "input_schema": _contract_schema(
                {"order_id": {"type": "string"}, "amount": {"type": "number"}},
                ["order_id"],
            ),
            "output_schema": _contract_schema(
                {"risk_level": {"type": "string"}, "score": {"type": "number"}},
                ["risk_level"],
            ),
            "tags": ["订单", "风险"],
            "visibility": "scenario",
        }

    def _create_function(self) -> dict:
        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=self._payload(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _publish_staging(self) -> tuple[str, str]:
        db = self.Session()
        db.info["tenant_id"] = self.tenant.id
        db.info["user_id"] = self.user.id
        try:
            branch = release_service.create_branch(
                db,
                self.scenario.id,
                name="functions/main",
            )
            release = release_service.publish_snapshot(
                db,
                self.scenario.id,
                environment="staging",
                confirmed=True,
                branch_id=branch.id,
            )
            return release.snapshot_id, release.id
        finally:
            db.close()

    def test_crud_exposes_typed_metadata_and_never_accepts_code(self) -> None:
        created = self._create_function()
        self.assertEqual(created["name"], "计算订单风险")
        self.assertEqual(created["input_schema"]["required"], ["order_id"])
        self.assertNotIn("code", created)
        self.assertNotIn("executor", created)

        listed = self.client.get(f"/api/scenarios/{self.scenario.id}/functions")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [created["id"]])
        detail = self.client.get(f"/api/scenarios/{self.scenario.id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["functions"][0]["id"], created["id"])

        updated_payload = self._payload(name="计算订单风险 v2")
        updated = self.client.put(
            f"/api/scenarios/functions/{created['id']}",
            json=updated_payload,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "计算订单风险 v2")

        top_level_code = self._payload()
        top_level_code["code"] = "import os; os.system('bad')"
        self.assertEqual(
            self.client.post(f"/api/scenarios/{self.scenario.id}/functions", json=top_level_code).status_code,
            422,
        )
        schema_code = self._payload()
        schema_code["input_schema"]["script"] = "return bad"
        rejected = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=schema_code,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("不允许可执行字段", rejected.json()["detail"])

        columns = {column["name"] for column in inspect(self.engine).get_columns("function_definitions")}
        self.assertFalse({"code", "script", "executor", "executor_config", "handler"} & columns)

    def test_release_and_package_freeze_function_contract_and_block_direct_delete(self) -> None:
        created = self._create_function()
        snapshot_id, release_id = self._publish_staging()

        db = self.Session()
        try:
            function = db.get(FunctionDefinition, created["id"])
            assert function is not None
            function.name = "开发中函数（不得在 staging 使用）"
            function.output_schema = _contract_schema({"changed": {"type": "boolean"}})
            db.commit()

            scenario = db.get(BusinessScenario, self.scenario.id)
            assert scenario is not None
            frozen = runtime_definition_service.resolve_active(
                db,
                scenario,
                environment="staging",
            )
            released_function = runtime_definition_service.resolve_resource(
                frozen,
                "function",
                created["id"],
            )
            self.assertEqual(frozen.snapshot_id, snapshot_id)
            self.assertEqual(frozen.release_id, release_id)
            self.assertEqual(released_function.name, "计算订单风险")
            self.assertIn("risk_level", released_function.output_schema["properties"])

            package = package_service.export_scenario_package(db, scenario)
            self.assertEqual(package_service.validate_package(package)["valid"], True)
            self.assertEqual(len(package["resources"]["functions"]), 1)
            self.assertNotIn("executor", package["resources"]["functions"][0])
            unsafe_package = copy.deepcopy(package)
            unsafe_package["resources"]["functions"][0]["code"] = "return 1"
            self.assertFalse(package_service.validate_package(unsafe_package)["valid"])
        finally:
            db.close()

        deleted = self.client.delete(f"/api/scenarios/functions/{created['id']}")
        self.assertEqual(deleted.status_code, 409, deleted.text)
        self.assertIn("活动环境发布引用", deleted.json()["detail"])
        verify = self.Session()
        try:
            self.assertIsNotNone(verify.get(FunctionDefinition, created["id"]))
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
