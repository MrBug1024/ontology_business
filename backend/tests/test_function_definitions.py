"""Regression tests for governed function definitions and their runtime."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    FunctionDefinition,
    FunctionRun,
    LLMConfig,
    Tenant,
    User,
)
from app.routers import functions as functions_router
from app.routers import scenarios as scenarios_router
from app.services import (
    agent_runtime_adapter,
    function_definition_service,
    policies,
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

    def test_schema_valued_additional_properties_preserve_runtime_validation(self) -> None:
        schema = function_definition_service.normalize_schema(
            {
                "type": "object",
                "properties": {},
                "additionalProperties": {"type": "integer", "minimum": 0},
            },
            label="输入 Schema",
        )

        self.assertEqual(
            policies.validate_action_params(schema, {"low": 1, "high": 3}),
            {"low": 1, "high": 3},
        )
        with self.assertRaisesRegex(Exception, "类型错误"):
            policies.validate_action_params(schema, {"high": "3"})


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
        self.app.include_router(functions_router.router, prefix="/api")

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

    def test_function_provider_authoring_uses_server_manifest_and_exact_validation(self) -> None:
        listed = self.client.get(
            f"/api/scenarios/{self.scenario.id}/function-providers"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        manifests = listed.json()
        query_manifest = next(
            item
            for item in manifests
            if item["provider_key"] == "builtin.semantic-dataset-query"
        )
        self.assertEqual(query_manifest["provider_version"], "1.0.0")
        self.assertFalse(query_manifest["config_schema"]["additionalProperties"])

        payload = self._payload(name="受管对象查询")
        payload.update(
            {
                "runtime_kind": "provider",
                "runtime_config": {
                    "provider_key": query_manifest["provider_key"],
                    "provider_version": query_manifest["provider_version"],
                    "provider_config": {"semantic_mapping_ids": ["mapping-test"]},
                },
                "input_schema": query_manifest["input_schema"],
                "output_schema": query_manifest["output_schema"],
            }
        )
        created = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=payload,
        )
        self.assertEqual(created.status_code, 200, created.text)

        wrong_version = json.loads(json.dumps(payload))
        wrong_version["runtime_config"]["provider_version"] = "9.9.9"
        rejected_version = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=wrong_version,
        )
        self.assertEqual(rejected_version.status_code, 400, rejected_version.text)
        self.assertIn("版本不匹配", rejected_version.json()["detail"])

        invalid_config = json.loads(json.dumps(payload))
        invalid_config["runtime_config"]["provider_config"]["unknown"] = True
        rejected_config = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=invalid_config,
        )
        self.assertEqual(rejected_config.status_code, 400, rejected_config.text)
        self.assertIn("unsupported", rejected_config.json()["detail"])

    def test_release_freezes_function_contract_and_blocks_direct_delete(self) -> None:
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

    def test_agent_executes_function_through_capability_receipt(self) -> None:
        payload = self._payload(name="订单加权评分")
        payload.update({
            "input_schema": _contract_schema({"amount": {"type": "number"}}, ["amount"]),
            "output_schema": _contract_schema(
                {"score": {"type": "number"}},
                ["score"],
            ),
            "runtime_kind": "weighted_score",
            "runtime_config": {"weights": {"amount": 0.5}, "bias": 2},
        })
        created = self.client.post(f"/api/scenarios/{self.scenario.id}/functions", json=payload)
        self.assertEqual(created.status_code, 200, created.text)

        db = self.Session()
        db.info["tenant_id"] = self.tenant.id
        db.info["user_id"] = self.user.id
        try:
            agent = Agent(
                tenant_id=self.tenant.id,
                name="订单助手",
                scenario_id=self.scenario.id,
                data_source_ids=[],
                runtime_binding_mode="capability_only",
                capability_scope={
                    "functions": {"mode": "explicit", "selected_ids": [created.json()["id"]]},
                    "actions": {"mode": "explicit", "selected_ids": []},
                    "rules": {"mode": "explicit", "selected_ids": []},
                    "events": {"mode": "explicit", "selected_ids": []},
                    "workflows": {"mode": "explicit", "selected_ids": []},
                },
            )
            db.add(agent)
            db.commit()
            runtime = agent_runtime_adapter.build_runtime_context(
                db,
                agent,
                LLMConfig(name="工具模型"),
            )
            tool_names = {
                tool["function"]["name"] for tool in runtime.build_tools()
            }
            self.assertEqual(
                tool_names,
                {"list_available_capabilities", "invoke_capability"},
            )
            db.info["action_audit_context"] = {"agent_id": agent.id}
            try:
                receipt = json.loads(
                    runtime.execute_tool(
                        "invoke_capability",
                        {
                            "kind": "function",
                            "key": created.json()["id"],
                            "inputs": {"amount": 10},
                        },
                    )
                )
            finally:
                db.info.pop("action_audit_context", None)
            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["output"]["score"], 7)
            invocation = db.get(CapabilityInvocation, receipt["invocation_id"])
            self.assertIsNotNone(invocation)
            self.assertEqual(invocation.capability_kind, "function")
            self.assertEqual(invocation.capability_key, created.json()["id"])
            self.assertEqual(invocation.invocation_source, "agent")
            self.assertEqual(db.query(FunctionRun).count(), 0)
        finally:
            db.rollback()
            db.close()

    def test_function_browser_route_uses_unified_invocation_and_strict_replay(self) -> None:
        payload = self._payload(name="订单加权评分")
        payload.update({
            "input_schema": _contract_schema(
                {"amount": {"type": "number"}},
                ["amount"],
            ),
            "output_schema": _contract_schema(
                {"score": {"type": "number"}},
                ["score"],
            ),
            "runtime_kind": "weighted_score",
            "runtime_config": {"weights": {"amount": 0.2}, "bias": 1},
        })
        created = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=payload,
        )
        self.assertEqual(created.status_code, 200, created.text)
        function_id = created.json()["id"]

        first = self.client.post(
            f"/api/functions/{function_id}/run",
            json={"params": {"amount": 10}, "idempotency_key": "score-order-1"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["status"], "succeeded")
        self.assertEqual(first.json()["output_payload"]["score"], 3)
        self.assertNotIn("asset_id", first.json())

        replay = self.client.post(
            f"/api/functions/{function_id}/run",
            json={"params": {"amount": 10}, "idempotency_key": "score-order-1"},
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["id"], first.json()["id"])

        conflict = self.client.post(
            f"/api/functions/{function_id}/run",
            json={"params": {"amount": 999}, "idempotency_key": "score-order-1"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["detail"]["code"], "idempotency_conflict")

        missing = self.client.post(
            f"/api/functions/{function_id}/run",
            json={"params": {}},
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "input_schema_invalid")

        listed = self.client.get(f"/api/functions/{function_id}/runs")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        self.assertNotEqual(listed.json()[0]["input_payload"], {"amount": 10})
        self.assertIn("hash", listed.json()[0]["input_payload"])

        db_inspector = inspect(self.engine)
        self.assertIn("function_runs", db_inspector.get_table_names())
        columns = {column["name"] for column in db_inspector.get_columns("function_runs")}
        self.assertNotIn("asset_id", columns)
        verify = self.Session()
        try:
            run = verify.get(CapabilityInvocation, first.json()["id"])
            self.assertIsNotNone(run)
            self.assertEqual(run.capability_kind, "function")
            self.assertEqual(run.capability_key, function_id)
            self.assertEqual(verify.query(FunctionRun).count(), 0)
        finally:
            verify.close()

    def test_function_history_filters_legacy_rows_by_tenant(self) -> None:
        created = self.client.post(
            f"/api/scenarios/{self.scenario.id}/functions",
            json=self._payload(),
        )
        self.assertEqual(created.status_code, 200, created.text)
        function_id = created.json()["id"]
        db = self.Session()
        try:
            other_tenant = Tenant(id="tenant-functions-other", name="其他租户")
            other_scenario = BusinessScenario(
                id="scenario-functions-other",
                tenant_id=other_tenant.id,
                name="其他场景",
            )
            other_function = FunctionDefinition(
                id="function-cross-tenant",
                scenario_id=other_scenario.id,
                name="不可见函数",
            )
            db.add_all([other_tenant, other_scenario, other_function])
            db.flush()
            created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.add_all(
                [
                    FunctionRun(
                        id="legacy-owned-run",
                        tenant_id=self.tenant.id,
                        scenario_id=self.scenario.id,
                        function_id=function_id,
                        run_type="function",
                        status="succeeded",
                        output_payload={"risk_level": "low"},
                        created_at=created_at,
                    ),
                    FunctionRun(
                        id="legacy-cross-tenant-run",
                        tenant_id=other_tenant.id,
                        scenario_id=self.scenario.id,
                        function_id=function_id,
                        run_type="function",
                        status="succeeded",
                        output_payload={"risk_level": "secret"},
                        created_at=created_at,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        listed = self.client.get(f"/api/functions/{function_id}/runs")

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["id"] for item in listed.json()],
            ["legacy-owned-run"],
        )
        cross_tenant = self.client.get("/api/functions/function-cross-tenant/runs")
        missing = self.client.get("/api/functions/function-missing/runs")
        self.assertEqual(cross_tenant.status_code, 404, cross_tenant.text)
        self.assertEqual(cross_tenant.json(), missing.json())


if __name__ == "__main__":
    unittest.main()
