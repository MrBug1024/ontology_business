"""External v2 scenario/bootstrap and managed-input option security contract."""
from __future__ import annotations

import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    AuthorizationGrant,
    BusinessScenario,
    ConnectorBinding,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetHead,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LogicalDataset,
    OntologyBranch,
    OntologyRelease,
    OntologySnapshot,
    Organization,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.routers import external_api, external_capabilities
from app.services import connector_service, permission_service, release_service
from app.services.auth_service import get_tenant_db
from sdk import CapabilityClient


class ExternalDiscoveryOptionTests(unittest.TestCase):
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
        with self.Session() as db:
            self.tenant = Tenant(id="tenant-discovery", name="Discovery tenant")
            self.user = User(
                id="user-discovery",
                tenant_id=self.tenant.id,
                email="discovery@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-discovery",
                tenant_id=self.tenant.id,
                name="Generic analysis",
                description="Logical scenario metadata",
                industry="General",
                status="active",
            )
            self.retired_scenario = BusinessScenario(
                id="scenario-retired",
                tenant_id=self.tenant.id,
                name="Retired scenario",
                status="retired",
            )
            self.denied_scenario = BusinessScenario(
                id="scenario-denied",
                tenant_id=self.tenant.id,
                name="Denied scenario",
                status="active",
            )
            self.function = FunctionDefinition(
                id="function-discovery",
                scenario_id=self.scenario.id,
                name="Generic scorer",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                runtime_kind="weighted_score",
                runtime_config={"weights": {"value": 1}},
            )
            self.other_function = FunctionDefinition(
                id="function-other",
                scenario_id=self.scenario.id,
                name="Other capability",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                runtime_kind="weighted_score",
                runtime_config={"weights": {"value": 1}},
            )
            self.unready_function = FunctionDefinition(
                id="function-unready",
                scenario_id=self.scenario.id,
                name="Contract only",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                runtime_kind="contract",
                runtime_config={},
            )
            db.add_all(
                [
                    self.tenant,
                    self.user,
                    self.scenario,
                    self.retired_scenario,
                    self.denied_scenario,
                    self.function,
                    self.other_function,
                    self.unready_function,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db,
                self.tenant.id,
                owner_user_id=self.user.id,
            )

            other_tenant = Tenant(id="tenant-discovery-other", name="Other tenant")
            other_user = User(
                id="user-discovery-other",
                tenant_id=other_tenant.id,
                email="discovery-other@example.test",
                password_hash="test-only",
                status="active",
            )
            self.other_scenario = BusinessScenario(
                id="scenario-discovery-other",
                tenant_id=other_tenant.id,
                name="Other tenant scenario",
                status="active",
            )
            db.add_all([other_tenant, other_user, self.other_scenario])
            db.commit()
            permission_service.ensure_organization(
                db,
                other_tenant.id,
                owner_user_id=other_user.id,
            )

            db.info["tenant_id"] = self.tenant.id
            db.info["user_id"] = self.user.id
            db.add(
                AuthorizationGrant(
                    id="grant-denied-scenario",
                    organization_id=organization.id,
                    user_id=self.user.id,
                    resource_type="scenario",
                    resource_id=self.denied_scenario.id,
                    verb="read",
                    effect="deny",
                    created_by_user_id=self.user.id,
                )
            )
            self._seed_catalog_and_ports(db, other_tenant.id)
            self._publish_frozen_contract(db)
            db.commit()

        self.app = FastAPI()
        self.app.include_router(external_api.management_router, prefix="/api")
        self.app.include_router(external_capabilities.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["tenant_id"] = self.tenant.id
            request_db.info["user_id"] = self.user.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        issued = self.client.post(
            "/api/developer/api-keys",
            json={
                "name": "discovery-client",
                "scopes": ["capabilities:read", "capabilities:invoke"],
                "expires_in_days": 30,
            },
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        self.token = issued.json()["token"]
        self.headers = {"X-API-Key": self.token}

    def _seed_catalog_and_ports(self, db, other_tenant_id: str) -> None:
        self.dataset = LogicalDataset(
            id="dataset-discovery",
            tenant_id=self.tenant.id,
            key="generic.records",
            name="Generic records",
            lifecycle_status="active",
        )
        self.schema = DatasetSchema(
            id="schema-discovery",
            tenant_id=self.tenant.id,
            dataset_id=self.dataset.id,
            schema_version=1,
            schema_hash="a" * 64,
            compatibility="none",
            schema_document={"type": "array"},
        )
        self.version = DatasetVersion(
            id="version-discovery",
            tenant_id=self.tenant.id,
            dataset_id=self.dataset.id,
            schema_id=self.schema.id,
            version_number=1,
            status="ready",
            content_hash="1" * 64,
            manifest={"physical_table": "must-never-leak"},
        )
        self.head = DatasetHead(
            id="head-discovery",
            tenant_id=self.tenant.id,
            dataset_id=self.dataset.id,
            environment="prod",
            dataset_version_id=self.version.id,
        )
        self.dev_head = DatasetHead(
            id="head-discovery-dev",
            tenant_id=self.tenant.id,
            dataset_id=self.dataset.id,
            environment="dev",
            dataset_version_id=self.version.id,
        )
        incompatible_dataset = LogicalDataset(
            id="dataset-incompatible",
            tenant_id=self.tenant.id,
            key="incompatible.records",
            name="Incompatible records",
            lifecycle_status="active",
        )
        incompatible_schema = DatasetSchema(
            id="schema-incompatible",
            tenant_id=self.tenant.id,
            dataset_id=incompatible_dataset.id,
            schema_version=1,
            schema_hash="b" * 64,
            compatibility="none",
            schema_document={"columns": ["private_column"]},
        )
        incompatible_version = DatasetVersion(
            id="version-incompatible",
            tenant_id=self.tenant.id,
            dataset_id=incompatible_dataset.id,
            schema_id=incompatible_schema.id,
            version_number=1,
            status="ready",
            content_hash="2" * 64,
        )
        retired_dataset = LogicalDataset(
            id="dataset-retired",
            tenant_id=self.tenant.id,
            key="retired.records",
            name="Retired records",
            lifecycle_status="retired",
        )
        retired_schema = DatasetSchema(
            id="schema-retired",
            tenant_id=self.tenant.id,
            dataset_id=retired_dataset.id,
            schema_version=1,
            schema_hash="a" * 64,
            compatibility="none",
            schema_document={},
        )
        retired_version = DatasetVersion(
            id="version-retired",
            tenant_id=self.tenant.id,
            dataset_id=retired_dataset.id,
            schema_id=retired_schema.id,
            version_number=1,
            status="ready",
            content_hash="3" * 64,
        )
        foreign_dataset = LogicalDataset(
            id="dataset-foreign",
            tenant_id=other_tenant_id,
            key="foreign.records",
            name="Foreign records",
        )
        foreign_schema = DatasetSchema(
            id="schema-foreign",
            tenant_id=other_tenant_id,
            dataset_id=foreign_dataset.id,
            schema_version=1,
            schema_hash="a" * 64,
            compatibility="none",
            schema_document={},
        )
        foreign_version = DatasetVersion(
            id="version-foreign",
            tenant_id=other_tenant_id,
            dataset_id=foreign_dataset.id,
            schema_id=foreign_schema.id,
            version_number=1,
            status="ready",
            content_hash="4" * 64,
        )
        self.asset = DataAsset(
            id="asset-discovery",
            tenant_id=self.tenant.id,
            key="generic.document",
            name="Generic document",
            kind="file",
            lifecycle_status="active",
        )
        self.asset_version = DataAssetVersion(
            id="asset-version-discovery",
            tenant_id=self.tenant.id,
            asset_id=self.asset.id,
            version_number=1,
            provenance_kind="upload",
            status="ready",
            content_sha256="5" * 64,
            byte_size=12,
            source_locator={"object_key": "private/object/path"},
            version_document={
                "profile": {"columns": ["private_column"]},
                "credential": "must-never-leak",
            },
        )
        self.data_source = DataSource(
            id="source-discovery",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="Private connector target",
            type="postgres",
            config={
                "host": "private-host",
                "password": "credential-must-never-leak",
            },
            status="ok",
        )
        self.connector = ConnectorBinding(
            id="binding-discovery",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            environment="dev",
            binding_key="generic.current-system",
            reference_label="Current governed system",
            connector_kind="data_source",
            connector_id=self.data_source.id,
            health_status="healthy",
        )
        self.dataset_port = ScenarioCapabilityPort(
            id="port-discovery-dataset",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="records",
            name="Records",
            direction="input",
            role="invocation_input",
            media_kind="dataset",
            dataset_id=self.dataset.id,
            dataset_schema_id=self.schema.id,
            schema_document={"type": "array"},
            is_required=False,
            binding_policy="per_invocation",
            status="active",
            config={
                "allowed_binding_kinds": ["dataset_version"],
                "allow_override": True,
            },
        )
        self.asset_port = ScenarioCapabilityPort(
            id="port-discovery-asset",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="document",
            name="Document",
            direction="input",
            role="reference",
            media_kind="document",
            is_required=False,
            binding_policy="per_invocation",
            status="active",
            config={
                "allowed_binding_kinds": ["asset_version"],
                "allow_override": True,
            },
        )
        self.head_port = ScenarioCapabilityPort(
            id="port-discovery-head",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="current-records",
            name="Current records",
            direction="input",
            role="reference",
            media_kind="dataset",
            dataset_id=self.dataset.id,
            dataset_schema_id=self.schema.id,
            is_required=False,
            binding_policy="per_invocation",
            status="active",
            config={
                "allowed_binding_kinds": ["dataset_head"],
                "allow_override": True,
            },
        )
        self.connector_port = ScenarioCapabilityPort(
            id="port-discovery-connector",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="system",
            name="System",
            direction="input",
            role="reference",
            media_kind="connector",
            is_required=False,
            binding_policy="per_invocation",
            status="active",
            config={
                "allowed_binding_kinds": ["connector_binding"],
                "allow_override": True,
            },
        )
        self.locked_port = ScenarioCapabilityPort(
            id="port-discovery-locked",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="fixed-records",
            name="Fixed records",
            direction="input",
            role="reference",
            media_kind="dataset",
            is_required=False,
            binding_policy="scenario_default",
            status="active",
            config={
                "allowed_binding_kinds": ["dataset_version"],
                "allow_override": False,
            },
        )
        self.override_default_port = ScenarioCapabilityPort(
            id="port-discovery-override-default",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.function.id,
            port_key="replaceable-records",
            name="Replaceable records",
            direction="input",
            role="invocation_input",
            media_kind="dataset",
            dataset_id=self.dataset.id,
            dataset_schema_id=self.schema.id,
            is_required=True,
            binding_policy="scenario_default",
            status="active",
            config={
                "allowed_binding_kinds": ["dataset_version"],
                "allow_override": True,
            },
        )
        self.unready_port = ScenarioCapabilityPort(
            id="port-discovery-unready",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="function",
            capability_key=self.unready_function.id,
            port_key="records",
            name="Records",
            direction="input",
            role="reference",
            media_kind="dataset",
            is_required=False,
            binding_policy="per_invocation",
            status="active",
            config={
                "allowed_binding_kinds": ["dataset_version"],
                "allow_override": True,
            },
        )
        db.add_all(
            [
                self.dataset,
                incompatible_dataset,
                retired_dataset,
                foreign_dataset,
                self.asset,
                self.data_source,
            ]
        )
        db.flush()
        db.add_all(
            [
                self.schema,
                incompatible_schema,
                retired_schema,
                foreign_schema,
                self.asset_version,
            ]
        )
        db.flush()
        db.add_all(
            [
                self.version,
                incompatible_version,
                retired_version,
                foreign_version,
            ]
        )
        db.flush()
        db.add_all(
            [
                self.head,
                self.dev_head,
                self.connector,
                self.dataset_port,
                self.asset_port,
                self.head_port,
                self.connector_port,
                self.locked_port,
                self.override_default_port,
                self.unready_port,
            ]
        )
        db.flush()
        self.connector.connector_signature = connector_service.connector_signature(
            "data_source", self.data_source
        )

    def _publish_frozen_contract(self, db) -> None:
        content = release_service.capture_snapshot_content(db, self.scenario)
        branch = OntologyBranch(
            id="branch-discovery",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="main",
            created_by_user_id=self.user.id,
        )
        snapshot = OntologySnapshot(
            id="snapshot-discovery",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            kind="baseline",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.user.id,
        )
        release = OntologyRelease(
            id="release-discovery",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="prod",
            status="released",
            created_by_user_id=self.user.id,
        )
        db.add(branch)
        db.flush()
        db.add(snapshot)
        db.flush()
        db.add(release)
        db.flush()

        # Production must keep the frozen owner and binding kinds even when
        # subsequent dev authoring moves this durable audit anchor elsewhere.
        self.dataset_port.capability_key = self.other_function.id
        self.dataset_port.config = {
            "allowed_binding_kinds": ["asset_version"],
            "allow_override": True,
        }

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _options_url(
        self,
        port_key: str,
        *,
        capability_id: str | None = None,
        scenario_id: str | None = None,
    ) -> str:
        return (
            f"/api/external/v2/scenarios/{scenario_id or self.scenario.id}"
            f"/capabilities/function/{capability_id or self.function.id}"
            f"/ports/{port_key}/managed-input-options"
        )

    def test_v2_scenarios_use_capability_scope_acl_tenant_and_retirement(self) -> None:
        response = self.client.get("/api/external/v2/scenarios", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()], [self.scenario.id])
        encoded = response.text.lower()
        for forbidden in (
            "tenant_id",
            self.retired_scenario.id,
            self.denied_scenario.id,
            self.other_scenario.id,
            "config",
            "credential",
        ):
            self.assertNotIn(forbidden.lower(), encoded)

        no_scope = self.client.post(
            "/api/developer/api-keys",
            json={
                "name": "invoke-only",
                "scopes": ["capabilities:invoke"],
                "expires_in_days": 30,
            },
        )
        self.assertEqual(no_scope.status_code, 201, no_scope.text)
        denied = self.client.get(
            "/api/external/v2/scenarios",
            headers={"X-API-Key": no_scope.json()["token"]},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_prod_options_use_frozen_owner_schema_and_binding_kind(self) -> None:
        response = self.client.get(
            self._options_url("records"),
            params={"environment": "prod"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["binding_kinds"], ["dataset_version"])
        self.assertTrue(payload["allow_override"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["binding_kind"], "dataset_version")
        self.assertEqual(
            payload["items"][0]["managed_input"],
            {
                "port_key": "records",
                "dataset_version_id": self.version.id,
                "expected_signature": self.version.content_hash,
            },
        )
        self.assertEqual(len(payload["definition_hash"]), 64)
        self.assertEqual(len(payload["deployment_fingerprint"]), 64)

        wrong_owner = self.client.get(
            self._options_url("records", capability_id=self.other_function.id),
            params={"environment": "prod"},
            headers=self.headers,
        )
        self.assertEqual(wrong_owner.status_code, 404, wrong_owner.text)
        self.assertEqual(
            wrong_owner.json()["detail"]["code"],
            "runtime_input_port_not_found",
        )

    def test_asset_and_connector_options_are_invocation_ready_and_do_not_leak(self) -> None:
        with self.Session() as db:
            before = db.scalar(select(func.count(DataAssetVersion.id)))

        asset_response = self.client.get(
            self._options_url("document"),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(asset_response.status_code, 200, asset_response.text)
        asset_choice = asset_response.json()["items"]
        self.assertEqual(len(asset_choice), 1)
        self.assertEqual(
            asset_choice[0]["managed_input"],
            {
                "port_key": "document",
                "asset_version_id": self.asset_version.id,
                "expected_signature": self.asset_version.content_sha256,
            },
        )

        head_response = self.client.get(
            self._options_url("current-records"),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(head_response.status_code, 200, head_response.text)
        self.assertEqual(
            head_response.json()["items"][0]["managed_input"],
            {
                "port_key": "current-records",
                "dataset_head_id": self.dev_head.id,
                "expected_signature": self.version.content_hash,
            },
        )

        connector_response = self.client.get(
            self._options_url("system"),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(connector_response.status_code, 200, connector_response.text)
        connector_choice = connector_response.json()["items"]
        self.assertEqual(len(connector_choice), 1)
        self.assertEqual(
            connector_choice[0]["managed_input"],
            {
                "port_key": "system",
                "binding_key": self.connector.binding_key,
                "expected_signature": self.connector.connector_signature,
            },
        )

        encoded = json.dumps(
            [asset_response.json(), head_response.json(), connector_response.json()],
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        for forbidden in (
            "data_source_id",
            "connector_id",
            self.data_source.id,
            "private-host",
            "credential-must-never-leak",
            "must-never-leak",
            "private/object/path",
            "physical_table",
            "private_column",
            "source_locator",
            "version_document",
            "manifest",
            "bucket_name",
            "object_key",
            "password",
            "columns",
        ):
            self.assertNotIn(forbidden.lower(), encoded)

        with self.Session() as db:
            after = db.scalar(select(func.count(DataAssetVersion.id)))
        self.assertEqual(before, after, "read-only discovery must not assetize typed inputs")

    def test_missing_scenario_default_is_nonblocking_when_invocation_override_is_allowed(self) -> None:
        capabilities = self.client.get(
            f"/api/external/v2/scenarios/{self.scenario.id}/capabilities",
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        contract = next(
            item for item in capabilities.json() if item["key"] == self.function.id
        )
        issue = next(
            item
            for item in contract["readiness"]["issues"]
            if item.get("port_key") == self.override_default_port.port_key
        )
        self.assertEqual(issue["code"], "invocation_input_required")
        self.assertFalse(issue["blocking"])
        self.assertTrue(contract["readiness"]["ready"])

        response = self.client.get(
            self._options_url(self.override_default_port.port_key),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["allow_override"])
        self.assertEqual(payload["items"][0]["managed_input"], {
            "port_key": self.override_default_port.port_key,
            "dataset_version_id": self.version.id,
            "expected_signature": self.version.content_hash,
        })

    def test_retired_cross_tenant_port_owner_and_readiness_fail_closed(self) -> None:
        retired = self.client.get(
            self._options_url("records", scenario_id=self.retired_scenario.id),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(retired.status_code, 404, retired.text)

        foreign = self.client.get(
            self._options_url("records", scenario_id=self.other_scenario.id),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        wrong_port = self.client.get(
            self._options_url("document", capability_id=self.other_function.id),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(wrong_port.status_code, 404, wrong_port.text)

        acl_denied = self.client.get(
            self._options_url("records", scenario_id=self.denied_scenario.id),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(acl_denied.status_code, 403, acl_denied.text)

        locked = self.client.get(
            self._options_url("fixed-records"),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(locked.status_code, 409, locked.text)
        self.assertEqual(
            locked.json()["detail"]["code"],
            "runtime_input_override_forbidden",
        )

        unready = self.client.get(
            self._options_url("records", capability_id=self.unready_function.id),
            params={"environment": "dev"},
            headers=self.headers,
        )
        self.assertEqual(unready.status_code, 409, unready.text)
        self.assertEqual(unready.json()["detail"]["code"], "capability_not_ready")

    def test_sdk_bootstraps_scenarios_and_managed_options(self) -> None:
        client = CapabilityClient(
            "https://testserver/api/external/v2",
            self.token,
            http_client=self.client,
        )
        self.assertEqual(
            [item["id"] for item in client.list_scenarios()],
            [self.scenario.id],
        )
        page = client.list_managed_input_options(
            self.scenario.id,
            "function",
            self.function.id,
            "records",
            environment="prod",
            limit=1,
        )
        self.assertEqual(page["items"][0]["managed_input"]["dataset_version_id"], self.version.id)
        with self.assertRaises(ValueError):
            client.list_managed_input_options(
                self.scenario.id,
                "provider",  # type: ignore[arg-type]
                self.function.id,
                "records",
            )


if __name__ == "__main__":
    unittest.main()
