"""REST v2 capability discovery, invocation, and isolation regressions."""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    BusinessScenario,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LogicalDataset,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.routers import external_api, external_capabilities
from app.services import permission_service
from app.services.auth_service import get_tenant_db
from sdk import CapabilityClient


class ExternalCapabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)
        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-cap-api", name="Capability API tenant")
            self.owner = User(
                id="owner-cap-api",
                tenant_id=self.tenant.id,
                email="owner-cap-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-cap-api",
                tenant_id=self.tenant.id,
                name="Generic capability scenario",
                status="active",
            )
            self.function = FunctionDefinition(
                id="function-cap-api",
                scenario_id=self.scenario.id,
                name="Weighted score",
                description="A generic deterministic function",
                input_schema={
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                    "required": ["amount"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"score": {"type": "number"}},
                },
                runtime_kind="weighted_score",
                runtime_config={"weights": {"amount": 0.5}, "bias": 2},
            )
            db.add_all([self.tenant, self.owner, self.scenario, self.function])
            db.commit()
            permission_service.ensure_organization(
                db,
                self.tenant.id,
                owner_user_id=self.owner.id,
            )
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(external_api.management_router, prefix="/api")
        self.app.include_router(external_capabilities.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["tenant_id"] = self.tenant.id
            request_db.info["user_id"] = self.owner.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _issue(self, name: str, scopes: list[str]) -> dict:
        response = self.client.post(
            "/api/developer/api-keys",
            json={
                "name": name,
                "scopes": scopes,
                "expires_in_days": 30,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _capability_url(self) -> str:
        return (
            f"/api/external/v2/scenarios/{self.scenario.id}"
            f"/capabilities/function/{self.function.id}"
        )

    def test_zero_data_capability_is_discoverable_invokable_and_receipted(self) -> None:
        key = self._issue(
            "zero-data-client",
            ["capabilities:read", "capabilities:invoke"],
        )
        headers = {"X-API-Key": key["token"]}

        listed = self.client.get(
            f"/api/external/v2/scenarios/{self.scenario.id}/capabilities",
            params={"environment": "dev"},
            headers=headers,
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        capability = listed.json()[0]
        self.assertEqual(capability["kind"], "function")
        self.assertEqual(capability["key"], self.function.id)
        self.assertTrue(capability["readiness"]["ready"])
        self.assertEqual(capability["data_ports"], [])
        self.assertNotIn("provider_key", listed.text)

        invoked = self.client.post(
            f"{self._capability_url()}/invoke",
            json={"environment": "dev", "inputs": {"amount": 4}},
            headers=headers,
        )
        self.assertEqual(invoked.status_code, 200, invoked.text)
        receipt = invoked.json()
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["output"]["score"], 4)
        self.assertNotIn("provider_key", invoked.text)
        self.assertNotIn("runtime_config", invoked.text)

        queried = self.client.get(
            f"/api/external/v2/invocations/{receipt['invocation_id']}",
            headers=headers,
        )
        self.assertEqual(queried.status_code, 200, queried.text)
        self.assertEqual(queried.json()["output"], receipt["output"])

        other_key = self._issue(
            "other-client",
            ["capabilities:read", "capabilities:invoke"],
        )
        isolated = self.client.get(
            f"/api/external/v2/invocations/{receipt['invocation_id']}",
            headers={"X-API-Key": other_key["token"]},
        )
        self.assertEqual(isolated.status_code, 404, isolated.text)
        self.assertEqual(isolated.json()["detail"]["code"], "invocation_not_found")

    def test_discovery_scopes_same_named_ports_to_the_current_capability(self) -> None:
        db = self.Session()
        try:
            other = FunctionDefinition(
                id="function-cap-api-other",
                scenario_id=self.scenario.id,
                name="Other weighted score",
                input_schema={
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                    "required": ["amount"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                runtime_kind="weighted_score",
                runtime_config={"weights": {"amount": 1}},
            )
            primary_port = ScenarioCapabilityPort(
                id="port-cap-api-primary-reference",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                capability_kind="function",
                capability_key=self.function.id,
                port_key="shared.reference",
                name="Primary reference",
                direction="input",
                role="reference",
                media_kind="structured",
                is_required=False,
                binding_policy="none",
                status="active",
            )
            other_port = ScenarioCapabilityPort(
                id="port-cap-api-other-reference",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                capability_kind="function",
                capability_key=other.id,
                port_key="shared.reference",
                name="Other reference",
                direction="input",
                role="reference",
                media_kind="structured",
                is_required=False,
                binding_policy="none",
                status="active",
            )
            db.add_all([other, primary_port, other_port])
            db.commit()
        finally:
            db.close()

        key = self._issue("scoped-discovery", ["capabilities:read"])
        headers = {"X-API-Key": key["token"]}
        primary = self.client.get(
            self._capability_url(), params={"environment": "dev"}, headers=headers
        )
        secondary = self.client.get(
            f"/api/external/v2/scenarios/{self.scenario.id}"
            f"/capabilities/function/{other.id}",
            params={"environment": "dev"},
            headers=headers,
        )

        self.assertEqual(primary.status_code, 200, primary.text)
        self.assertEqual(secondary.status_code, 200, secondary.text)
        self.assertEqual(
            [item["name"] for item in primary.json()["data_ports"]],
            ["Primary reference"],
        )
        self.assertEqual(
            [item["name"] for item in secondary.json()["data_ports"]],
            ["Other reference"],
        )
        self.assertEqual(
            primary.json()["definition_hash"], secondary.json()["definition_hash"]
        )

    def test_per_invocation_versions_change_data_context_not_definition(self) -> None:
        db = self.Session()
        try:
            dataset = LogicalDataset(
                id="dataset-cap-api",
                tenant_id=self.tenant.id,
                key="runtime-records",
                name="Runtime records",
            )
            schema = DatasetSchema(
                id="schema-cap-api",
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                schema_version=1,
                schema_hash="a" * 64,
                compatibility="none",
                schema_document={"type": "array", "items": {"type": "object"}},
            )
            version_a = DatasetVersion(
                id="version-cap-api-a",
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                schema_id=schema.id,
                version_number=1,
                status="ready",
                content_hash="1" * 64,
            )
            version_b = DatasetVersion(
                id="version-cap-api-b",
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                schema_id=schema.id,
                version_number=2,
                status="ready",
                content_hash="2" * 64,
            )
            port = ScenarioCapabilityPort(
                id="port-cap-api",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                capability_kind="function",
                capability_key=self.function.id,
                port_key="records",
                name="Records",
                direction="input",
                role="invocation_input",
                media_kind="dataset",
                dataset_id=dataset.id,
                dataset_schema_id=schema.id,
                schema_document={"type": "array", "items": {"type": "object"}},
                is_required=True,
                cardinality="one",
                binding_policy="per_invocation",
                status="active",
            )
            db.add(dataset)
            db.flush()
            db.add(schema)
            db.flush()
            db.add_all([version_a, version_b, port])
            db.commit()
        finally:
            db.close()

        key = self._issue(
            "changing-input-client",
            ["capabilities:read", "capabilities:invoke"],
        )
        headers = {"X-API-Key": key["token"]}
        discovered = self.client.get(
            self._capability_url(),
            params={"environment": "dev"},
            headers=headers,
        )
        self.assertEqual(discovered.status_code, 200, discovered.text)
        contract = discovered.json()
        self.assertTrue(contract["readiness"]["ready"])
        self.assertEqual(
            contract["output_schema"],
            {
                "type": "object",
                "properties": {"score": {"type": "number"}},
            },
        )
        self.assertEqual(
            contract["readiness"]["issues"][0]["code"],
            "invocation_input_required",
        )
        self.assertFalse(contract["readiness"]["issues"][0]["blocking"])
        self.assertEqual(contract["data_ports"][0]["key"], "records")
        self.assertEqual(
            contract["data_ports"][0]["binding_kinds"],
            ["dataset_head", "dataset_version"],
        )
        self.assertTrue(contract["data_ports"][0]["allow_override"])
        self.assertNotIn("dataset_id", discovered.text)
        self.assertNotIn("dataset_schema_id", discovered.text)

        missing = self.client.post(
            f"{self._capability_url()}/invoke",
            json={"environment": "dev", "inputs": {"amount": 2}},
            headers=headers,
        )
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertEqual(
            missing.json()["detail"]["code"],
            "required_runtime_inputs_missing",
        )

        receipts: list[dict] = []
        for version_id in ("version-cap-api-a", "version-cap-api-b"):
            invoked = self.client.post(
                f"{self._capability_url()}/invoke",
                json={
                    "environment": "dev",
                    "inputs": {"amount": 2},
                    "managed_inputs": [
                        {"port_key": "records", "dataset_version_id": version_id}
                    ],
                },
                headers=headers,
            )
            self.assertEqual(invoked.status_code, 200, invoked.text)
            receipts.append(invoked.json())

        self.assertEqual(receipts[0]["definition_hash"], receipts[1]["definition_hash"])
        self.assertEqual(
            receipts[0]["deployment_fingerprint"],
            receipts[1]["deployment_fingerprint"],
        )
        self.assertNotEqual(
            receipts[0]["data_context_fingerprint"],
            receipts[1]["data_context_fingerprint"],
        )

    def test_external_scopes_and_structured_errors_are_stable(self) -> None:
        read_key = self._issue("read-only", ["capabilities:read"])
        headers = {"X-API-Key": read_key["token"]}
        denied = self.client.post(
            f"{self._capability_url()}/invoke",
            json={"environment": "dev", "inputs": {"amount": 1}},
            headers=headers,
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        broad_key = self._issue(
            "schema-client",
            ["capabilities:read", "capabilities:invoke"],
        )
        invalid = self.client.post(
            f"{self._capability_url()}/invoke",
            json={"environment": "dev", "inputs": {"amount": "private-invalid"}},
            headers={"X-API-Key": broad_key["token"]},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["detail"]["code"], "input_schema_invalid")
        self.assertNotIn("private-invalid", invalid.text)

    def test_v2_sdk_is_a_thin_wrapper_over_discovery_invoke_and_receipts(self) -> None:
        key = self._issue(
            "sdk-capability-client",
            ["capabilities:read", "capabilities:invoke"],
        )
        client = CapabilityClient(
            "https://testserver/api/external/v2",
            key["token"],
            http_client=self.client,
        )
        self.assertFalse(hasattr(client, "identity"))
        self.assertFalse(hasattr(client, "list_objects"))
        capabilities = client.list_capabilities(self.scenario.id, environment="dev")
        self.assertEqual([item["key"] for item in capabilities], [self.function.id])
        contract = client.get_capability(
            self.scenario.id,
            "function",
            self.function.id,
            environment="dev",
        )
        with self.assertRaises(ValueError):
            client.get_capability(
                self.scenario.id,
                "provider",  # type: ignore[arg-type]
                self.function.id,
                environment="dev",
            )
        with self.assertRaises(ValueError):
            client.invoke_capability(
                self.scenario.id,
                "query",  # type: ignore[arg-type]
                self.function.id,
                environment="dev",
            )
        receipt = client.invoke_capability(
            self.scenario.id,
            "function",
            self.function.id,
            environment="dev",
            inputs={"amount": 6},
            expected_definition_hash=contract["definition_hash"],
            expected_deployment_fingerprint=contract["deployment_fingerprint"],
        )
        self.assertEqual(receipt["output"]["score"], 5)
        self.assertEqual(
            client.get_invocation_receipt(receipt["invocation_id"])["output"],
            receipt["output"],
        )


if __name__ == "__main__":
    unittest.main()
