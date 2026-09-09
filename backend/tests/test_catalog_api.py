from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
    OntologyRule,
    OntologyRelease,
    OntologySnapshot,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    SemanticMapping,
    Tenant,
    User,
)
from app.routers import catalog
from app.services import (
    business_query_service,
    connector_service,
    permission_service,
    release_service,
)
from app.providers.semantic_audit import SemanticAuditProvider
from app.providers.semantic_dataset_query import (
    SemanticDatasetQueryProvider,
    SemanticDatasetQueryProviderError,
)
from app.services import runtime_definition_service
from app.services.capability_contracts import (
    Actor,
    CapabilityRef,
    DataPort,
    Request,
    ResolvedDataHandle,
    ResolvedDeployment,
    RuntimeDataContext,
)
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

    def _create_dataset_contract(
        self,
        *,
        usage_plane: str = "modeling_material",
    ) -> tuple[dict, dict, dict, dict]:
        dataset = self.client.post(
            "/api/catalog/datasets",
            json={
                "key": "generic.records",
                "name": "Generic records",
                "description": "Reusable tenant data product",
                "usage_plane": usage_plane,
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
            f"/api/catalog/datasets/{dataset_json['id']}/head",
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
                "usage_plane": "modeling_material",
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

    def test_semantic_dataset_provider_uses_schema_only_mapping_with_structurally_compatible_runtime_data(self) -> None:
        modeling_dataset = self.client.post(
            "/api/catalog/datasets",
            json={
                "key": "generic.modeling-records",
                "name": "Generic modeling records",
                "description": "Schema-only modeling material",
                "usage_plane": "modeling_material",
            },
        )
        self.assertEqual(modeling_dataset.status_code, 201, modeling_dataset.text)
        self.assertEqual(modeling_dataset.json()["version_count"], 0)
        modeling_dataset = modeling_dataset.json()
        modeling_schema = self.client.post(
            f"/api/catalog/datasets/{modeling_dataset['id']}/schemas",
            json={
                "compatibility": "backward",
                "schema_document": {"purpose": "schema-only modeling contract"},
                "relations": [
                    {
                        "relation_key": "authored_sheet_name",
                        "display_name": "Authored sheet name",
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
        self.assertEqual(modeling_schema.status_code, 201, modeling_schema.text)
        modeling_schema = modeling_schema.json()
        runtime_dataset, runtime_schema, version, runtime_head = self._create_dataset_contract(
            usage_plane="invocation_input"
        )
        self.assertNotEqual(
            modeling_schema["schema_hash"], runtime_schema["schema_hash"]
        )
        mapping = self.client.post(
            f"/api/scenarios/{self.scenario.id}/semantic-mappings",
            json={
                "entity_id": self.entity.id,
                "dataset_schema_id": modeling_schema["id"],
                "dataset_relation_id": modeling_schema["relations"][0]["id"],
                "mapping_key": "records.business-record",
                "status": "active",
                "fields": [
                    {
                        "ontology_property_id": self.property.id,
                        "dataset_field_id": modeling_schema["relations"][0]["fields"][0]["id"],
                        "direction": "input",
                        "is_required": True,
                    }
                ],
            },
        )
        self.assertEqual(mapping.status_code, 201, mapping.text)
        mapping = mapping.json()
        self.assertIsNone(mapping["scenario_dataset_binding_id"])
        self.assertEqual(mapping["dataset_id"], modeling_dataset["id"])

        with self.Session() as db:
            db.info["tenant_id"] = self.tenant.id
            db.info["user_id"] = self.user.id
            function = db.get(FunctionDefinition, self.function.id)
            function.runtime_kind = "provider"
            function.runtime_config = {
                "provider_key": SemanticDatasetQueryProvider.provider_key,
                "provider_version": SemanticDatasetQueryProvider.provider_version,
                "provider_config": {"semantic_mapping_ids": [mapping["id"]]},
            }
            db.commit()
            definition = runtime_definition_service.resolve_authoring(
                db,
                db.get(BusinessScenario, self.scenario.id),
            )
            handle = ResolvedDataHandle(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=version["id"],
                version_id=version["id"],
                signature=version["content_hash"],
            )
            data_context = RuntimeDataContext(handles=(handle,))
            deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash=definition.definition_hash,
                definition=definition,
                data_ports=(
                    DataPort(
                        key="records",
                        modality="dataset",
                        schema={},
                        schema_hash=runtime_schema["schema_hash"],
                        binding_kinds=("dataset_version",),
                        override_policy="managed-reference",
                    ),
                ),
                data_context=data_context,
            )
            provider = SemanticDatasetQueryProvider().bind_invocation(db)
            contract = provider.contract(
                CapabilityRef(
                    kind="function",
                    resource_id=self.function.id,
                    provider_key=SemanticDatasetQueryProvider.provider_key,
                ),
                deployment,
            )
            catalog = contract["input_schema"]["x-ontology-catalog"]
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["entity_id"], self.entity.id)
            self.assertEqual(catalog[0]["entity_name"], "Business record")
            self.assertTrue(catalog[0]["entity_api_name"])
            self.assertEqual(catalog[0]["semantic_mapping_id"], mapping["id"])
            self.assertEqual(len(catalog[0]["properties"]), 1)
            self.assertEqual(
                {
                    key: catalog[0]["properties"][0][key]
                    for key in (
                        "property_name",
                        "description",
                        "data_type",
                        "is_key",
                    )
                },
                {
                    "property_name": "Record ID",
                    "description": "",
                    "data_type": "string",
                    "is_key": True,
                },
            )
            self.assertTrue(catalog[0]["properties"][0]["api_name"])
            base_entity_schema = contract["input_schema"]["properties"]["base_entity"]
            self.assertEqual(
                base_entity_schema["oneOf"][1]["enum"],
                ["Business record"],
            )
            self.assertEqual(
                contract["input_schema"]["properties"]["base_properties"]["items"]["enum"],
                ["Record ID"],
            )
            request = Request(
                capability=CapabilityRef(
                    kind="function",
                    resource_id=self.function.id,
                    provider_key=SemanticDatasetQueryProvider.provider_key,
                ),
                inputs={
                    "base_entity": "Business record",
                    "base_properties": ["Record ID"],
                },
            )
            actor = Actor(
                actor_type="user",
                principal_id=self.user.id,
                tenant_id=self.tenant.id,
                user_id=self.user.id,
            )

            def compile_query(session, **kwargs):
                plan = business_query_service.prepare_query(session, **kwargs)
                self.assertIn('"b"."record_id"', plan.sql)
                return {
                    "records": [{"Record ID": "R-1"}],
                    "columns": ["Record ID"],
                    "row_count": 1,
                    "truncated": False,
                    "offset": 0,
                    "next_offset": None,
                }

            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_query,
            ) as query:
                result = provider.invoke(request, actor, deployment, data_context)

            self.assertEqual(result["records"], [{"Record ID": "R-1"}])
            call = query.call_args.kwargs
            self.assertEqual(call["data_sources"][0].config["dataset_version_id"], version["id"])
            self.assertEqual(call["mappings"][0].table_name, "records")
            self.assertEqual(call["mappings"][0].column_map, {"Record ID": "record_id"})
            self.assertEqual(
                call["data_sources"][0].config["dataset_id"], runtime_dataset["id"]
            )

            head_context = RuntimeDataContext(
                handles=(
                    ResolvedDataHandle(
                        port_key="records",
                        binding_kind="dataset_head",
                        reference_id=runtime_head["id"],
                        version_id=version["id"],
                        signature=version["content_hash"],
                    ),
                )
            )
            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_query,
            ) as head_query:
                head_result = provider.invoke(request, actor, deployment, head_context)
            self.assertEqual(head_result["records"], [{"Record ID": "R-1"}])
            self.assertEqual(
                head_query.call_args.kwargs["data_sources"][0].config[
                    "dataset_version_id"
                ],
                version["id"],
            )

            function.input_schema = {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "Business record identifier",
                    }
                },
                "required": ["record_id"],
                "additionalProperties": False,
            }
            function.runtime_config = {
                "provider_key": SemanticDatasetQueryProvider.provider_key,
                "provider_version": SemanticDatasetQueryProvider.provider_version,
                "provider_config": {
                    "semantic_mapping_ids": [mapping["id"]],
                    "query_template": {
                        "base_entity": {"entity_name": "Business record"},
                        "base_properties": ["Record ID"],
                        "base_filters": [
                            {
                                "property": "Record ID",
                                "op": "eq",
                                "value": {"$input": "record_id"},
                            }
                        ],
                    },
                },
            }
            db.commit()
            templated_definition = runtime_definition_service.resolve_authoring(
                db,
                db.get(BusinessScenario, self.scenario.id),
            )
            templated_deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash=templated_definition.definition_hash,
                definition=templated_definition,
                data_ports=deployment.data_ports,
                data_context=data_context,
            )
            templated_contract = provider.contract(
                CapabilityRef(
                    kind="function",
                    resource_id=self.function.id,
                    provider_key=SemanticDatasetQueryProvider.provider_key,
                ),
                templated_deployment,
            )
            self.assertEqual(
                templated_contract["input_schema"]["properties"],
                function.input_schema["properties"],
            )
            templated_request = Request(
                capability=request.capability,
                inputs={"record_id": "R-1"},
            )

            def compile_template(session, **kwargs):
                self.assertEqual(
                    kwargs["args"]["base_filters"][0]["value"],
                    "R-1",
                )
                return compile_query(session, **kwargs)

            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_template,
            ):
                templated_result = provider.invoke(
                    templated_request,
                    actor,
                    templated_deployment,
                    data_context,
                )
            self.assertEqual(templated_result["records"], [{"Record ID": "R-1"}])

            audit_rule = OntologyRule(
                id="rule-semantic-audit",
                scenario_id=self.scenario.id,
                name="Duplicate record review",
                description="Find governed duplicate-record candidates.",
                condition={
                    "spec_version": "semantic-audit/v1",
                    "rule_code": "duplicate-record",
                    "domain": "data-quality",
                    "issue_type": "duplicate",
                    "assessment_mode": "assisted",
                    "source_use": "data-screening",
                    "basis": "Governed test policy",
                    "reference_example": "",
                    "first_listed_year": "2026",
                    "required_evidence": ["source record"],
                    "query_template": {
                        "base_entity": {"entity_name": "Business record"},
                        "base_properties": ["Record ID"],
                        "base_filters": [
                            {
                                "property": "Record ID",
                                "op": "eq",
                                "value": {"$input": "record_id"},
                            }
                        ],
                    },
                },
                severity="warning",
                enabled=True,
            )
            db.add(audit_rule)
            function.input_schema = {
                "type": "object",
                "properties": {
                    "rule_selector": {"type": "string", "minLength": 1},
                    "record_id": {"type": "string", "minLength": 1},
                },
                "required": ["rule_selector", "record_id"],
                "additionalProperties": False,
            }
            function.runtime_config = {
                "provider_key": SemanticAuditProvider.provider_key,
                "provider_version": SemanticAuditProvider.provider_version,
                "provider_config": {
                    "semantic_mapping_ids": [mapping["id"]],
                    "selector_input": "rule_selector",
                    "spec_version": "semantic-audit/v1",
                },
            }
            db.commit()
            audit_definition = runtime_definition_service.resolve_authoring(
                db,
                db.get(BusinessScenario, self.scenario.id),
            )
            audit_deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash=audit_definition.definition_hash,
                definition=audit_definition,
                data_ports=deployment.data_ports,
                data_context=data_context,
            )
            audit_provider = SemanticAuditProvider().bind_invocation(db)
            audit_capability = CapabilityRef(
                kind="function",
                resource_id=self.function.id,
                provider_key=SemanticAuditProvider.provider_key,
            )
            audit_contract = audit_provider.contract(
                audit_capability,
                audit_deployment,
            )
            self.assertEqual(
                audit_contract["input_schema"]["properties"],
                function.input_schema["properties"],
            )

            def compile_audit(session, **kwargs):
                self.assertEqual(kwargs["args"]["base_filters"][0]["value"], "R-1")
                return compile_query(session, **kwargs)

            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_audit,
            ):
                audit_result = audit_provider.invoke(
                    Request(
                        capability=audit_capability,
                        inputs={
                            "rule_selector": "duplicate-record",
                            "record_id": "R-1",
                        },
                    ),
                    actor,
                    audit_deployment,
                    data_context,
                )
            self.assertEqual(
                audit_result["decision_state"],
                "candidate_detected_pending_review",
            )
            self.assertEqual(audit_result["audit_rule"]["code"], "duplicate-record")
            self.assertEqual(audit_result["records"], [{"Record ID": "R-1"}])

            # Frozen releases created before the dedicated Provider keep their
            # exact v1 interpreter, but new authoring no longer emits this shape.
            function.runtime_config = {
                "provider_key": SemanticDatasetQueryProvider.provider_key,
                "provider_version": SemanticDatasetQueryProvider.provider_version,
                "provider_config": {
                    "semantic_mapping_ids": [mapping["id"]],
                    "rule_query": {
                        "selector_input": "rule_selector",
                        "spec_version": "semantic-audit/v1",
                    },
                },
            }
            db.commit()
            legacy_definition = runtime_definition_service.resolve_authoring(
                db,
                db.get(BusinessScenario, self.scenario.id),
            )
            legacy_deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash=legacy_definition.definition_hash,
                definition=legacy_definition,
                data_ports=deployment.data_ports,
                data_context=data_context,
            )
            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_audit,
            ):
                legacy_result = provider.invoke(
                    Request(
                        capability=request.capability,
                        inputs={
                            "rule_selector": "duplicate-record",
                            "record_id": "R-1",
                        },
                    ),
                    actor,
                    legacy_deployment,
                    data_context,
                )
            self.assertEqual(
                legacy_result["decision_state"],
                "candidate_detected_pending_review",
            )

            frozen_content = release_service.capture_snapshot_content(
                db,
                db.get(BusinessScenario, self.scenario.id),
            )
            portable_contract = next(
                item
                for item in frozen_content["semantic_mapping_contracts"]
                if item["id"] == mapping["id"]
            )
            portable_text = repr(portable_contract)
            self.assertNotIn(modeling_dataset["id"], portable_text)
            self.assertNotIn(modeling_schema["id"], portable_text)
            self.assertNotIn("authored_sheet_name", portable_text)
            frozen_snapshot = OntologySnapshot(
                id="snapshot-semantic-contract",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                kind="merge",
                content=frozen_content,
                content_hash=release_service.snapshot_hash(frozen_content),
                created_by_user_id=self.user.id,
            )
            db.add(frozen_snapshot)
            db.flush()
            frozen_definition = runtime_definition_service._from_snapshot(
                db.get(BusinessScenario, self.scenario.id),
                frozen_snapshot,
                release=None,
            )
            db.get(SemanticMapping, mapping["id"]).status = "retired"
            db.commit()
            frozen_deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash=frozen_definition.definition_hash,
                definition=frozen_definition,
                data_ports=deployment.data_ports,
                data_context=data_context,
            )
            with patch(
                "app.providers.semantic_dataset_query.business_query_service.query_business_data",
                side_effect=compile_audit,
            ):
                frozen_result = provider.invoke(
                    Request(
                        capability=request.capability,
                        inputs={
                            "rule_selector": "duplicate-record",
                            "record_id": "R-1",
                        },
                    ),
                    actor,
                    frozen_deployment,
                    data_context,
                )
            self.assertEqual(
                frozen_result["decision_state"],
                "candidate_detected_pending_review",
            )

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
            f"/api/catalog/datasets/{dataset['id']}/head",
            json={
                "dataset_version_id": version_b["id"],
                "expected_dataset_version_id": version_a["id"],
            },
        )
        self.assertEqual(first_writer.status_code, 200, first_writer.text)
        self.assertEqual(first_writer.json()["dataset_version_id"], version_b["id"])

        stale_writer = self.client.put(
            f"/api/catalog/datasets/{dataset['id']}/head",
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
            f"/api/catalog/datasets/{dataset['id']}/head",
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
                "usage_plane": "generated_output",
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
                    enabled=True,
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
        self.assertEqual(
            port["schema_document"]["x-platform-input-contract"]["version"],
            "tabular-content/v1",
        )
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

        invalid_contract = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "invalid.contract",
                "name": "Invalid contract",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "structured",
                "schema_document": {
                    "x-platform-input-contract": {
                        "version": "unsupported/v9",
                        "relations": [],
                    }
                },
                "is_required": False,
                "binding_policy": "none",
                "status": "draft",
            },
        )
        self.assertEqual(invalid_contract.status_code, 400, invalid_contract.text)
        self.assertIn("内容契约无效", invalid_contract.json()["detail"])

    def test_schema_backed_port_contract_is_canonical_without_blocking_manual_contracts(
        self,
    ) -> None:
        dataset, schema, _version, _head = self._create_dataset_contract()
        base_payload = {
            "capability_kind": "function",
            "capability_key": self.function.id,
            "name": "Canonical records",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "dataset_id": dataset["id"],
            "dataset_schema_id": schema["id"],
            "is_required": True,
            "cardinality": "one",
            "binding_policy": "per_invocation",
            "status": "active",
        }
        generated = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={**base_payload, "port_key": "canonical.generated"},
        )
        self.assertEqual(generated.status_code, 201, generated.text)
        canonical_contract = generated.json()["schema_document"][
            "x-platform-input-contract"
        ]

        exact = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                **base_payload,
                "port_key": "canonical.exact",
                "schema_document": {
                    "type": "array",
                    "x-platform-input-contract": canonical_contract,
                },
            },
        )
        self.assertEqual(exact.status_code, 201, exact.text)
        self.assertEqual(
            exact.json()["schema_document"]["x-platform-input-contract"],
            canonical_contract,
        )

        mismatched_contract = {
            **canonical_contract,
            "relations": [
                {
                    **canonical_contract["relations"][0],
                    "minimum_data_rows": 2,
                }
            ],
        }
        mismatched = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                **base_payload,
                "port_key": "canonical.mismatched",
                "schema_document": {
                    "x-platform-input-contract": mismatched_contract,
                },
            },
        )
        self.assertEqual(mismatched.status_code, 400, mismatched.text)
        self.assertIn("规范契约完全一致", mismatched.json()["detail"])

        manual = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "manual.contract",
                "name": "Manual contract",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "structured",
                "schema_document": {
                    "x-platform-input-contract": mismatched_contract,
                },
                "is_required": False,
                "binding_policy": "none",
                "status": "active",
            },
        )
        self.assertEqual(manual.status_code, 201, manual.text)
        self.assertIsNone(manual.json()["dataset_schema_id"])
        self.assertEqual(
            manual.json()["schema_document"]["x-platform-input-contract"],
            mismatched_contract,
        )

    def test_modeling_catalog_rejects_invocation_dataset_at_every_write_boundary(self) -> None:
        dataset, schema, _version, head = self._create_dataset_contract(
            usage_plane="invocation_input"
        )

        listed = self.client.get(
            "/api/catalog/datasets",
            params={"usage_plane": "modeling_material"},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [])

        binding = self.client.post(
            f"/api/scenarios/{self.scenario.id}/dataset-bindings",
            json={
                "dataset_id": dataset["id"],
                "binding_key": "invalid.modeling",
                "role": "modeling_evidence",
                "binding_mode": "head",
                "dataset_head_id": head["id"],
                "is_required": False,
            },
        )
        self.assertEqual(binding.status_code, 400, binding.text)

        port = self.client.post(
            f"/api/scenarios/{self.scenario.id}/capability-ports",
            json={
                "capability_kind": "function",
                "capability_key": self.function.id,
                "port_key": "invalid.records",
                "name": "Invalid records",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
                "dataset_id": dataset["id"],
                "dataset_schema_id": schema["id"],
                "is_required": True,
                "cardinality": "one",
                "binding_policy": "per_invocation",
            },
        )
        self.assertEqual(port.status_code, 400, port.text)

        with self.Session() as db:
            invalid_binding = ScenarioDatasetBinding(
                id="binding-invalid-modeling-plane",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                dataset_id=dataset["id"],
                binding_key="bypassed.invalid.modeling",
                role="modeling_evidence",
                binding_mode="head",
                dataset_head_id=head["id"],
                is_required=False,
                status="active",
                config={},
            )
            invalid_mapping = SemanticMapping(
                id="mapping-invalid-modeling-plane",
                tenant_id=self.tenant.id,
                dataset_id=dataset["id"],
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                scenario_dataset_binding_id=invalid_binding.id,
                dataset_schema_id=schema["id"],
                dataset_relation_id=schema["relations"][0]["id"],
                mapping_key="bypassed.invalid.mapping",
                status="active",
                identifier_strategy={},
                filter_expression={},
            )
            invalid_port = ScenarioCapabilityPort(
                id="port-invalid-modeling-plane",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                capability_kind="function",
                capability_key=self.function.id,
                port_key="bypassed.invalid.records",
                name="Bypassed invalid records",
                direction="input",
                role="invocation_input",
                media_kind="dataset",
                dataset_id=dataset["id"],
                dataset_schema_id=schema["id"],
                schema_document={"type": "array"},
                is_required=True,
                cardinality="one",
                binding_policy="per_invocation",
                status="active",
                config={},
            )
            db.add_all([invalid_binding, invalid_mapping, invalid_port])
            db.commit()

            provider = SemanticDatasetQueryProvider().bind_invocation(db)
            deployment = ResolvedDeployment(
                scenario_id=self.scenario.id,
                tenant_id=self.tenant.id,
                definition_hash="f" * 64,
                definition=SimpleNamespace(entities={}),
            )
            with self.assertRaisesRegex(
                SemanticDatasetQueryProviderError,
                "unavailable",
            ):
                provider._semantic_catalog(
                    definition=deployment.definition,
                    deployment=deployment,
                    mapping_ids=(invalid_mapping.id,),
                )
            with self.assertRaisesRegex(
                release_service.ReleaseValidationError,
                "非建模资料",
            ):
                release_service.capture_snapshot_content(
                    db,
                    db.get(BusinessScenario, self.scenario.id),
                )

        listed_mappings = self.client.get(
            f"/api/scenarios/{self.scenario.id}/semantic-mappings"
        )
        self.assertEqual(listed_mappings.status_code, 200, listed_mappings.text)
        self.assertEqual(listed_mappings.json(), [])

        mapping = self.client.post(
            f"/api/scenarios/{self.scenario.id}/semantic-mappings",
            json={
                "scenario_dataset_binding_id": "binding-invalid-modeling-plane",
                "entity_id": self.entity.id,
                "dataset_schema_id": schema["id"],
                "dataset_relation_id": schema["relations"][0]["id"],
                "mapping_key": "invalid.mapping",
                "status": "active",
                "fields": [],
            },
        )
        self.assertEqual(mapping.status_code, 400, mapping.text)

    def test_asset_catalog_can_be_filtered_by_authoritative_usage_plane(self) -> None:
        modeling = self.client.post(
            "/api/catalog/assets",
            json={
                "key": "modeling.asset",
                "name": "Modeling asset",
                "usage_plane": "modeling_material",
            },
        )
        invocation = self.client.post(
            "/api/catalog/assets",
            json={
                "key": "invocation.asset",
                "name": "Invocation asset",
                "usage_plane": "invocation_input",
            },
        )
        self.assertEqual(modeling.status_code, 201, modeling.text)
        self.assertEqual(invocation.status_code, 201, invocation.text)

        listed = self.client.get(
            "/api/catalog/assets",
            params={"usage_plane": "modeling_material"},
        )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [modeling.json()["id"]])

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
            params={},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [{
            "binding_key": "warehouse.current",
            "label": "Current warehouse",
            "connector_kind": "data_source",
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
