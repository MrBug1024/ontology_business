from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    BusinessScenario,
    DataSource,
    FunctionDefinition,
    LogicalDataset,
    OntologyBranch,
    OntologyEntity,
    OntologyProperty,
    OntologyRelease,
    OntologySnapshot,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    SemanticMapping,
    Tenant,
    User,
)
from app.routers import catalog
from app.services import connector_service, permission_service
from app.services.auth_service import get_current_user, get_tenant_db


class CatalogApiTests(unittest.TestCase):
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
        with self.Session() as db:
            self.tenant = Tenant(id="tenant-catalog-api", name="Catalog tenant")
            self.user = User(
                id="user-catalog-api",
                tenant_id=self.tenant.id,
                email="catalog-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.other_tenant = Tenant(
                id="tenant-catalog-api-other", name="Other catalog tenant"
            )
            self.other_user = User(
                id="user-catalog-api-other",
                tenant_id=self.other_tenant.id,
                email="catalog-api-other@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-catalog-api",
                tenant_id=self.tenant.id,
                name="Generic capability scenario",
            )
            self.entity = OntologyEntity(
                id="entity-catalog-api",
                scenario_id=self.scenario.id,
                name="Business record",
            )
            self.property = OntologyProperty(
                id="property-catalog-api",
                entity_id=self.entity.id,
                name="Record ID",
                data_type="string",
                is_key=True,
            )
            self.function = FunctionDefinition(
                id="function-catalog-api",
                scenario_id=self.scenario.id,
                name="Process business records",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            )
            db.add_all(
                [
                    self.tenant,
                    self.other_tenant,
                    self.user,
                    self.other_user,
                    self.scenario,
                    self.entity,
                    self.property,
                    self.function,
                ]
            )
            db.commit()
            permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.user.id
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.other_user.id
            )
            db.commit()

        self.current_tenant_id = self.tenant.id
        self.current_user_id = self.user.id
        self.app = FastAPI()
        self.app.include_router(catalog.router, prefix="/api")
        self.app.include_router(catalog.scenario_router, prefix="/api")

        def override_user():
            return SimpleNamespace(
                id=self.current_user_id,
                tenant_id=self.current_tenant_id,
            )

        def override_db():
            db = self.Session()
            db.info["tenant_id"] = self.current_tenant_id
            db.info["user_id"] = self.current_user_id
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_current_user] = override_user
        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _create_dataset_contract(self) -> tuple[dict, dict, dict, dict]:
        dataset = self.client.post(
            "/api/catalog/datasets",
            json={
                "key": "generic.records",
                "name": "Generic records",
                "description": "Reusable tenant data product",
            },
        )
        self.assertEqual(dataset.status_code, 201, dataset.text)
        dataset_json = dataset.json()

        schema = self.client.post(
            f"/api/catalog/datasets/{dataset_json['id']}/schemas",
            json={
                "compatibility": "backward",
                "schema_document": {"purpose": "test contract"},
                "relations": [
                    {
                        "relation_key": "records",
                        "display_name": "Records",
                        "kind": "table",
                        "fields": [
                            {
                                "field_key": "record_id",
                                "source_name": "record_id",
                                "logical_type": "string",
                                "nullable": False,
                                "key_ordinal": 0,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(schema.status_code, 201, schema.text)
        schema_json = schema.json()

        version = self.client.post(
            f"/api/catalog/datasets/{dataset_json['id']}/versions",
            json={
                "schema_id": schema_json["id"],
                "manifest": {"record_count": 2, "source": "managed-test"},
            },
        )
        self.assertEqual(version.status_code, 201, version.text)
        version_json = version.json()

        head = self.client.put(
            f"/api/catalog/datasets/{dataset_json['id']}/heads/dev",
            json={"dataset_version_id": version_json["id"]},
        )
        self.assertEqual(head.status_code, 200, head.text)
        return dataset_json, schema_json, version_json, head.json()

    def test_catalog_binding_and_semantic_mapping_are_scoped_and_revocable(self) -> None:
        asset = self.client.post(
            "/api/catalog/assets",
            json={
                "key": "evidence.sample",
                "name": "Modeling evidence",
                "kind": "file",
                "media_type": "text/csv",
            },
        )
        self.assertEqual(asset.status_code, 201, asset.text)
        self.assertEqual(asset.json()["version_count"], 0)

        dataset, schema, version, head = self._create_dataset_contract()
        binding = self.client.post(
            f"/api/scenarios/{self.scenario.id}/dataset-bindings",
            json={
                "dataset_id": dataset["id"],
                "binding_key": "records.input",
                "environment": "dev",
                "role": "modeling_evidence",
                "binding_mode": "head",
                "dataset_head_id": head["id"],
                "is_required": False,
            },
        )
        self.assertEqual(binding.status_code, 201, binding.text)
        binding_json = binding.json()
        self.assertEqual(binding_json["resolved_dataset_version_id"], version["id"])

        mapping = self.client.post(
            f"/api/scenarios/{self.scenario.id}/semantic-mappings",
            json={
                "scenario_dataset_binding_id": binding_json["id"],
                "entity_id": self.entity.id,
                "dataset_schema_id": schema["id"],
                "dataset_relation_id": schema["relations"][0]["id"],
                "mapping_key": "records.to-business-record",
                "status": "active",
                "fields": [
                    {
                        "ontology_property_id": self.property.id,
                        "dataset_field_id": schema["relations"][0]["fields"][0]["id"],
                        "direction": "input",
                        "is_required": True,
                    }
                ],
            },
        )
        self.assertEqual(mapping.status_code, 201, mapping.text)
        self.assertEqual(mapping.json()["dataset_id"], dataset["id"])

        blocked_delete = self.client.delete(
            f"/api/scenarios/{self.scenario.id}/dataset-bindings/{binding_json['id']}"
        )
        self.assertEqual(blocked_delete.status_code, 409, blocked_delete.text)

        with self.Session() as db:
            db.info["tenant_id"] = self.tenant.id
            db.info["user_id"] = self.user.id
            mapping_row = db.execute(select(SemanticMapping)).scalar_one()
            db.delete(mapping_row)
            db.commit()

        removed = self.client.delete(
            f"/api/scenarios/{self.scenario.id}/dataset-bindings/{binding_json['id']}"
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        with self.Session() as db:
            self.assertIsNotNone(db.get(LogicalDataset, dataset["id"]))
            self.assertIsNone(db.get(ScenarioDatasetBinding, binding_json["id"]))

    def test_dataset_head_compare_and_set_rejects_a_stale_writer(self) -> None:
        dataset, schema, version_a, _head = self._create_dataset_contract()
        version_b_response = self.client.post(
            f"/api/catalog/datasets/{dataset['id']}/versions",
            json={
                "schema_id": schema["id"],
                "parent_version_id": version_a["id"],
                "manifest": {"record_count": 3, "source": "managed-test-b"},
            },
        )
        self.assertEqual(version_b_response.status_code, 201, version_b_response.text)
        version_b = version_b_response.json()

        first_writer = self.client.put(
            f"/api/catalog/datasets/{dataset['id']}/heads/dev",
            json={
                "dataset_version_id": version_b["id"],
                "expected_dataset_version_id": version_a["id"],
            },
        )
        self.assertEqual(first_writer.status_code, 200, first_writer.text)
        self.assertEqual(first_writer.json()["dataset_version_id"], version_b["id"])

        stale_writer = self.client.put(
            f"/api/catalog/datasets/{dataset['id']}/heads/dev",
            json={
                "dataset_version_id": version_a["id"],
                "expected_dataset_version_id": version_a["id"],
            },
        )
        self.assertEqual(stale_writer.status_code, 400, stale_writer.text)
        self.assertIn("刷新后重试", stale_writer.text)

        current = self.client.get(f"/api/catalog/datasets/{dataset['id']}/heads")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()[0]["dataset_version_id"], version_b["id"])

        # Existing clients remain compatible when they intentionally omit CAS.
        unconditional = self.client.put(
            f"/api/catalog/datasets/{dataset['id']}/heads/dev",
            json={"dataset_version_id": version_a["id"]},
        )
        self.assertEqual(unconditional.status_code, 200, unconditional.text)

    def test_catalog_rejects_cross_tenant_dataset_and_secret_documents(self) -> None:
        dataset, _schema, _version, _head = self._create_dataset_contract()
        self.current_tenant_id = self.other_tenant.id
        self.current_user_id = self.other_user.id
        hidden = self.client.get("/api/catalog/datasets")
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertEqual(hidden.json(), [])

        denied = self.client.post(
            f"/api/scenarios/{self.scenario.id}/dataset-bindings",
            json={
                "dataset_id": dataset["id"],
                "binding_key": "cross-tenant",
                "environment": "dev",
                "role": "reference",
                "binding_mode": "pinned",
                "dataset_version_id": "unknown",
            },
        )
        self.assertEqual(denied.status_code, 404, denied.text)

        secret = self.client.post(
            "/api/catalog/datasets",
            json={
                "key": "unsafe.dataset",
                "name": "Unsafe dataset",
                "labels": {"database_password": "must-not-persist"},
            },
        )
        self.assertEqual(secret.status_code, 400, secret.text)
        self.assertNotIn("must-not-persist", secret.text)

    def test_released_capability_port_keeps_its_audit_anchor(self) -> None:
        created = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "released.input",
                "name": "Released input",
                "direction": "input",
                "role": "reference",
                "media_kind": "structured",
                "is_required": False,
                "binding_policy": "none",
                "status": "retired",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        port_id = created.json()["id"]

        with self.Session() as db:
            branch = OntologyBranch(
                id="branch-port-anchor",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="port-anchor",
                created_by_user_id=self.user.id,
            )
            db.add(branch)
            db.flush()
            snapshot = OntologySnapshot(
                id="snapshot-port-anchor",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                branch_id=branch.id,
                kind="baseline",
                content={"capability_ports": [{"id": port_id}]},
                content_hash="a" * 64,
                created_by_user_id=self.user.id,
            )
            db.add(snapshot)
            db.flush()
            db.add(
                OntologyRelease(
                    id="release-port-anchor",
                    tenant_id=self.tenant.id,
                    scenario_id=self.scenario.id,
                    branch_id=branch.id,
                    snapshot_id=snapshot.id,
                    environment="prod",
                    status="released",
                    created_by_user_id=self.user.id,
                )
            )
            db.commit()

        blocked = self.client.delete(
            f"/api/scenarios/{self.scenario.id}/capability-ports/{port_id}"
        )
        self.assertEqual(blocked.status_code, 400, blocked.text)
        with self.Session() as db:
            self.assertIsNotNone(db.get(ScenarioCapabilityPort, port_id))

    def test_capability_ports_are_contracts_not_runtime_data(self) -> None:
        dataset, schema, _version, _head = self._create_dataset_contract()
        payload = {
            "capability_kind": "function",
            "capability_key": self.function.id,
            "port_key": "records.input",
            "name": "Business records",
            "description": "Per-invocation governed records",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "dataset_id": dataset["id"],
            "dataset_schema_id": schema["id"],
            "schema_document": {"type": "array", "items": {"type": "object"}},
            "is_required": True,
            "cardinality": "one",
            "binding_policy": "per_invocation",
            "status": "active",
            "config": {"semantic_requirement": "record collection"},
        }
        created = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json=payload,
        )
        self.assertEqual(created.status_code, 201, created.text)
        port = created.json()
        self.assertEqual(port["dataset_schema_hash"], schema["schema_hash"])
        self.assertNotIn("dataset_version_id", port)
        self.assertNotIn("data_source_id", port)

        listed = self.client.get(
            f"/api/scenarios/{self.scenario.id}/capability-ports"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["port_key"] for item in listed.json()], ["records.input"])

        blocked = self.client.delete(
            f"/api/scenarios/{self.scenario.id}/capability-ports/{port['id']}"
        )
        self.assertEqual(blocked.status_code, 400, blocked.text)

        unsafe = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "unsafe.input",
                "name": "Unsafe input",
                "direction": "input",
                "role": "reference",
                "media_kind": "structured",
                "is_required": False,
                "binding_policy": "none",
                "config": {"dataset_version_id": "runtime-data-must-not-live-here"},
            },
        )
        self.assertEqual(unsafe.status_code, 400, unsafe.text)
        self.assertNotIn("runtime-data-must-not-live-here", unsafe.text)

        retired = self.client.put(
            f"/api/scenarios/{self.scenario.id}/capability-ports/{port['id']}",
            json={**payload, "status": "retired"},
        )
        self.assertEqual(retired.status_code, 200, retired.text)
        removed = self.client.delete(
            f"/api/scenarios/{self.scenario.id}/capability-ports/{port['id']}"
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        with self.Session() as db:
            self.assertIsNone(db.get(ScenarioCapabilityPort, port["id"]))

        zero_data = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "request.message",
                "name": "Request message",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "message",
                "is_required": False,
                "binding_policy": "none",
                "status": "active",
            },
        )
        self.assertEqual(zero_data.status_code, 201, zero_data.text)
        self.assertIsNone(zero_data.json()["dataset_id"])

    def test_connector_binding_options_expose_only_portable_references(self) -> None:
        with self.Session() as db:
            source = DataSource(
                id="source-catalog-option",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="Governed warehouse",
                type="postgres",
                config={"host": "private.invalid", "password": "must-not-leak"},
                status="ok",
            )
            db.add(source)
            db.flush()
            binding = connector_service.upsert_binding(
                db,
                self.scenario,
                environment="dev",
                binding_key_value="warehouse.current",
                kind="data_source",
                connector_id=source.id,
                reference_label="Current warehouse",
                created_by_user_id=self.user.id,
            )
            binding.health_status = "healthy"
            binding.health_message = ""
            binding.connector_signature = connector_service.connector_signature(
                "data_source", source
            )
            db.commit()

        response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/connector-bindings",
            params={"environment": "dev"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [{
            "binding_key": "warehouse.current",
            "label": "Current warehouse",
            "connector_kind": "data_source",
            "environment": "dev",
            "ready": True,
            "blocking_reason": "",
            "capabilities": ["sql_read", "schema"],
            "updated_at": response.json()[0]["updated_at"],
        }])
        encoded = response.text
        self.assertNotIn("source-catalog-option", encoded)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("must-not-leak", encoded)

        self.current_tenant_id = self.other_tenant.id
        self.current_user_id = self.other_user.id
        hidden = self.client.get(
            f"/api/scenarios/{self.scenario.id}/connector-bindings"
        )
        self.assertEqual(hidden.status_code, 404, hidden.text)


if __name__ == "__main__":
    unittest.main()
