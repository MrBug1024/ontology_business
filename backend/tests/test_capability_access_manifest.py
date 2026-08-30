"""Capability access manifests keep adapters release-aligned and credential-free."""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import BusinessScenario, FunctionDefinition, Tenant, User
from app.routers import capability_access
from app.services import permission_service
from app.services.auth_service import get_tenant_db


class CapabilityAccessManifestTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-access", name="Access tenant")
            self.owner = User(
                id="owner-access",
                tenant_id=self.tenant.id,
                email="owner-access@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-access",
                tenant_id=self.tenant.id,
                name="Text analysis",
                status="active",
            )
            function = FunctionDefinition(
                id="function-access",
                scenario_id=self.scenario.id,
                name="Score text",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string", "example": "customer row"}},
                    "required": ["text"],
                },
                output_schema={"type": "object"},
                runtime_kind="weighted_score",
                runtime_config={"weights": {}, "bias": 1},
            )
            db.add_all([self.tenant, self.owner, self.scenario, function])
            db.commit()
            permission_service.ensure_organization(db, self.tenant.id, owner_user_id=self.owner.id)
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(capability_access.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["tenant_id"] = self.tenant.id
            request_db.info["user_id"] = self.owner.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def test_rest_and_mcp_share_one_definition_without_runtime_data(self) -> None:
        response = self.client.get(
            f"/api/developer/capability-access/{self.scenario.id}/manifest",
            params={"environment": "dev"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["manifest_version"], "capability-access-manifest/v1")
        self.assertEqual({item["protocol"] for item in payload["adapters"]}, {"rest", "mcp"})
        rest_adapter = next(
            item for item in payload["adapters"] if item["protocol"] == "rest"
        )
        self.assertEqual(
            rest_adapter["managed_input_upload"],
            "/api/external/v2/assets/upload",
        )
        self.assertEqual(rest_adapter["optional_scopes"], ["assets:write"])
        self.assertEqual(payload["deployment"]["definition_source"], "live")
        self.assertEqual(len(payload["deployment"]["definition_hash"]), 64)
        self.assertEqual(len(payload["manifest_id"]), 64)
        self.assertEqual(len(payload["capabilities"]), 1)
        self.assertNotIn("input_schema", payload["capabilities"][0])
        self.assertNotIn("output_schema", payload["capabilities"][0])
        self.assertEqual(len(payload["capabilities"][0]["input_schema_hash"]), 64)
        self.assertEqual(len(payload["capabilities"][0]["output_schema_hash"]), 64)
        serialized = response.text.lower()
        for forbidden in (
            "customer row",
            "data_source_id",
            "dataset_version_id",
            "connector_id",
            "provider_key",
            "runtime_config",
            "password",
            "token_hash",
            "object_key",
            "bucket_name",
            "access_key",
            "secret_key",
        ):
            self.assertNotIn(forbidden, serialized)
        checks = {item["code"]: item["passed"] for item in payload["checks"]}
        self.assertTrue(checks["runtime_bindings_excluded"])
        self.assertTrue(checks["credentials_excluded"])


if __name__ == "__main__":
    unittest.main()
