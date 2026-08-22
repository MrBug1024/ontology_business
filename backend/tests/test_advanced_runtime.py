"""P2 advanced asset, built-in runtime and feedback regression coverage."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import BusinessScenario, FunctionDefinition, Tenant, User
from app.routers import advanced
from app.services import package_service, permission_service
from app.services.auth_service import get_current_user


class AdvancedRuntimeRouteTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-advanced", name="高级运行时租户")
            self.user = User(
                id="user-advanced",
                tenant_id=self.tenant.id,
                email="advanced.owner@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-advanced",
                tenant_id=self.tenant.id,
                name="高级能力场景",
            )
            db.add_all([self.tenant, self.user, self.scenario])
            db.commit()
            permission_service.ensure_organization(db, self.tenant.id, owner_user_id=self.user.id)
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(advanced.router, prefix="/api")

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

    def _create_asset(self, kind: str, name: str, config: dict | None = None) -> dict:
        response = self.client.post(
            f"/api/advanced/scenarios/{self.scenario.id}/assets",
            json={"name": name, "kind": kind, "status": "ready", "config": config or {}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_spatial_records_are_queryable_by_bbox_and_cursor(self) -> None:
        asset = self._create_asset("geospatial", "配送点")
        for lon in (121.47, 121.60):
            response = self.client.post(
                f"/api/advanced/assets/{asset['id']}/records",
                json={
                    "geometry": {"type": "Point", "coordinates": [lon, 31.23]},
                    "payload": {"name": str(lon)},
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
        page = self.client.get(
            f"/api/advanced/assets/{asset['id']}/records",
            params={"bbox": "121.4,31.0,121.5,31.5"},
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(len(page.json()["items"]), 1)
        self.assertEqual(page.json()["next_sequence"], 1)
        cursor = self.client.get(
            f"/api/advanced/assets/{asset['id']}/records",
            params={"after_sequence": 1},
        )
        self.assertEqual(len(cursor.json()["items"]), 1)

    def test_timeseries_optimization_model_and_feedback_are_audited(self) -> None:
        series = self._create_asset("timeseries", "温度序列")
        response = self.client.post(
            f"/api/advanced/assets/{series['id']}/records",
            json={
                "event_time": datetime.now(timezone.utc).isoformat(),
                "payload": {"value": 12.5},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        aggregate = self.client.post(
            f"/api/advanced/assets/{series['id']}/runs?run_type=aggregate",
            json={"params": {"values": [1, 2, 3, 4], "aggregation": "avg"}, "idempotency_key": "agg-1"},
        )
        self.assertEqual(aggregate.status_code, 201, aggregate.text)
        self.assertEqual(aggregate.json()["output_payload"]["value"], 2.5)
        replay = self.client.post(
            f"/api/advanced/assets/{series['id']}/runs?run_type=aggregate",
            json={"params": {"values": [99]}, "idempotency_key": "agg-1"},
        )
        self.assertEqual(replay.json()["id"], aggregate.json()["id"])

        optimizer = self._create_asset(
            "optimization",
            "运输方案",
            {"objective": "score", "direction": "max", "constraints": [{"field": "capacity", "op": ">=", "value": 10}]},
        )
        optimized = self.client.post(
            f"/api/advanced/assets/{optimizer['id']}/runs?run_type=optimize",
            json={"params": {"candidates": [{"id": "a", "score": 0.8, "capacity": 12}, {"id": "b", "score": 0.95, "capacity": 8}]}},
        )
        self.assertEqual(optimized.status_code, 201, optimized.text)
        self.assertEqual(optimized.json()["output_payload"]["selected"]["id"], "a")

        model = self._create_asset("ml_model", "风险模型", {"weights": {"amount": 0.1}, "bias": 2})
        prediction = self.client.post(
            f"/api/advanced/assets/{model['id']}/runs?run_type=predict",
            json={"params": {"features": {"amount": 30}}},
        )
        self.assertEqual(prediction.json()["output_payload"]["prediction"], 5)
        feedback = self.client.post(
            f"/api/advanced/assets/{model['id']}/feedback",
            json={"run_id": prediction.json()["id"], "label": "人工复核", "score": 0.9},
        )
        self.assertEqual(feedback.status_code, 201, feedback.text)
        summary = self.client.get(f"/api/advanced/assets/{model['id']}/summary")
        self.assertEqual(summary.json()["feedback_count"], 1)

    def test_governed_function_uses_only_allowlisted_runtime(self) -> None:
        db = self.Session()
        try:
            function = FunctionDefinition(
                id="function-advanced",
                scenario_id=self.scenario.id,
                name="订单评分",
                input_schema={"type": "object", "required": ["amount"]},
                output_schema={"type": "object"},
                runtime_kind="weighted_score",
                runtime_config={"weights": {"amount": 0.2}, "bias": 1},
            )
            db.add(function)
            db.commit()
        finally:
            db.close()
        response = self.client.post(
            "/api/advanced/functions/function-advanced/run",
            json={"params": {"amount": 10}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["output_payload"]["score"], 3)

        missing = self.client.post(
            "/api/advanced/functions/function-advanced/run",
            json={"params": {}},
        )
        self.assertEqual(missing.status_code, 201)
        self.assertEqual(missing.json()["status"], "failed")
        self.assertIn("缺少必填参数", missing.json()["error"])

    def test_advanced_assets_are_portable_package_resources(self) -> None:
        self._create_asset(
            "simulation",
            "库存模拟",
            {"operations": [{"op": "add", "field": "stock", "value": 2}]},
        )
        db = self.Session()
        try:
            package = package_service.export_scenario_package(db, self.scenario.id)
            self.assertEqual(package["manifest"]["resource_counts"]["advanced_assets"], 1)
            self.assertEqual(package["resources"]["advanced_assets"][0]["kind"], "simulation")
            validation = package_service.validate_package(package)
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(
                validation["normalized"]["resources"]["advanced_assets"][0]["name"],
                "库存模拟",
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
