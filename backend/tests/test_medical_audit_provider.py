from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    BusinessScenario,
    BucketFile,
    CapabilityInvocation,
    ConnectorBinding,
    DataMapping,
    DataSource,
    DatasetField,
    DatasetFragment,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LogicalDataset,
    OntologyBranch,
    OntologyEntity,
    OntologyProperty,
    OntologyRelease,
    OntologySnapshot,
    RunInputBinding,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.providers.medical_audit.provider import (
    MedicalAuditAgentExtension,
    MedicalAuditProvider,
    PROVIDER_KEY,
    PROVIDER_VERSION,
)
from app.providers.medical_audit import service as medical_audit_service
from app.services import (
    capability_application_service,
    connector_service,
    dataset_query_service,
    datasource_service,
    function_definition_service,
    permission_service,
    release_service,
    runtime_definition_service,
)
from app.config import get_settings
from app.services.capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityRef,
    ResolvedDataHandle,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_json,
)
from app.services.capability_invoker import (
    CapabilityInvocationError,
    CapabilityInvoker,
    resolve_capability_contract,
)
from app.services.capability_agent_extensions import (
    AgentProviderExtensionError,
    GroundingResult,
    LegacyCapabilityMatch,
    bind_agent_providers,
)
from app.services.capability_registry import (
    CapabilityProviderRegistry,
    CapabilityRegistryError,
)


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{(tmp_path / 'provider.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["charge_threshold"],
            },
            "service_name": {"type": "string"},
            "threshold": {"type": "number"},
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["strategy", "service_name", "threshold"],
        "additionalProperties": False,
    }


def _provider_config(*, version: str = PROVIDER_VERSION) -> dict:
    return {
        "provider_key": PROVIDER_KEY,
        "provider_version": version,
        "provider_config": {
            "input_port_key": "records",
            "mapping_ids": ["mapping-charge"],
        },
    }


def _definition(*, tenant_id: str, scenario_id: str, version: str = PROVIDER_VERSION):
    properties = [
        SimpleNamespace(id=f"property-{api_name}", name=api_name, api_name=api_name)
        for api_name in (
            "charge_line_id",
            "encounter_id",
            "facility_name",
            "service_name",
            "quantity",
            "charged_amount",
        )
    ]
    entity = SimpleNamespace(
        id="entity-charge",
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        api_name="medical_charge_line",
        properties=properties,
    )
    mapping = SimpleNamespace(
        id="mapping-charge",
        entity_id=entity.id,
        data_source_id="definition-source",
        data_source_binding_key="records",
        data_source_binding_ref={"adapter": "dataset"},
        table_name="charges",
        column_map={item.name: item.api_name for item in properties},
        transform_rules={"charge_line_id": [{"op": "to_string"}]},
        status="ready",
        last_error="",
    )
    function = SimpleNamespace(
        id="function-audit",
        name="Governed deterministic audit",
        runtime_kind="provider",
        runtime_config=_provider_config(version=version),
        input_schema=_schema(),
    )
    port = SimpleNamespace(
        id="port-records",
        capability_kind="function",
        capability_key=function.id,
        port_key="records",
        direction="input",
        role="invocation_input",
        media_kind="structured",
        binding_policy="per_invocation",
        config={
            "allowed_binding_kinds": [
                "dataset_version",
                "dataset_head",
                "connector_binding",
            ]
        },
    )
    definition_hash = "d" * 64
    return SimpleNamespace(
        scenario=SimpleNamespace(id=scenario_id, tenant_id=tenant_id),
        source="live",
        environment="dev",
        definition_hash=definition_hash,
        snapshot_id=None,
        release_id=None,
        functions={function.id: function},
        entities={entity.id: entity},
        mappings={mapping.id: mapping},
        capability_ports={port.id: port},
    )


def _registry() -> CapabilityProviderRegistry:
    registry = CapabilityProviderRegistry()
    registry.register_instance(MedicalAuditProvider())
    registry.seal()
    return registry


def test_provider_contract_rejects_missing_declared_input_port(db: Session) -> None:
    definition = _definition(
        tenant_id="tenant-missing-port",
        scenario_id="scenario-missing-port",
    )
    definition.capability_ports = {}
    deployment = ResolvedDeployment(
        scenario_id="scenario-missing-port",
        tenant_id="tenant-missing-port",
        environment="dev",
        definition_hash=definition.definition_hash,
        definition=definition,
    )

    with pytest.raises(CapabilityInvocationError) as captured:
        resolve_capability_contract(
            db,
            deployment,
            CapabilityRef(kind="function", resource_id="function-audit"),
            registry=_registry(),
        )
    assert captured.value.code == "provider_contract_failed"


def test_live_unknown_mapping_health_remains_fail_closed() -> None:
    definition = _definition(
        tenant_id="tenant-live-health",
        scenario_id="scenario-live-health",
    )
    mapping = definition.mappings["mapping-charge"]
    mapping.status = "unknown"
    source = SimpleNamespace(
        id="definition-source",
        name="Current managed source",
        type="dataset",
        connector_revision=0,
        config={},
    )

    with pytest.raises(
        medical_audit_service.MedicalAuditError,
        match="当前运行映射未就绪",
    ):
        medical_audit_service.resolve_mapping_contract(
            [source],
            [mapping],
            definition=definition,
        )


def test_live_missing_mapping_health_remains_fail_closed() -> None:
    definition = _definition(
        tenant_id="tenant-live-missing-health",
        scenario_id="scenario-live-missing-health",
    )
    mapping = definition.mappings["mapping-charge"]
    mapping.status = None
    source = SimpleNamespace(
        id="definition-source",
        name="Current managed source",
        type="dataset",
        connector_revision=0,
        config={},
    )

    with pytest.raises(
        medical_audit_service.MedicalAuditError,
        match="当前运行映射未就绪",
    ):
        medical_audit_service.resolve_mapping_contract(
            [source],
            [mapping],
            definition=definition,
        )


class _VersionProbeProvider:
    provider_key = "test.versioned-provider"

    def __init__(self, version: str) -> None:
        self.provider_version = version

    def contract(self, capability, deployment):
        return {
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def invoke(self, request, actor, deployment, data_context):
        return {
            "provider_version": self.provider_version,
            "release_id": deployment.release_id,
            "definition_hash": deployment.definition_hash,
        }

    def bind_agent_runtime(self, _context):
        return _VersionProbeAgentExtension(self.provider_version)


class _VersionProbeAgentExtension:
    provider_key = _VersionProbeProvider.provider_key

    def __init__(self, provider_version: str) -> None:
        self.provider_version = provider_version

    def agent_tools(self):
        return ()

    def execute_agent_tool(self, name, arguments):
        raise AssertionError((name, arguments))

    def authorize_historic_tool_result(self, name, arguments, result):
        return False

    def match_legacy_capability(self, name, arguments, result):
        return None

    def normalize_capability_shadow_result(self, match, result):
        return result

    def verify_shadow_data_context(self, match, data_context):
        return None

    def prepare_grounding(self, user_message):
        return None

    def ground(self, user_message, tool_outcomes, prepared):
        return GroundingResult(
            provider_key=self.provider_key,
            provider_version=self.provider_version,
            verified=False,
        )


_PHYSICAL_TABLE_MARKER = "slice_physical_table_marker"
_PHYSICAL_AMOUNT_MARKER = "slice_physical_amount_marker"


_DATASET_COLUMNS = (
    ("charge_line_id", "string", "VARCHAR"),
    ("encounter_id", "string", "VARCHAR"),
    ("facility_name", "string", "VARCHAR"),
    ("service_name", "string", "VARCHAR"),
    ("quantity", "number", "DOUBLE"),
    (_PHYSICAL_AMOUNT_MARKER, "number", "DOUBLE"),
)


def _semantic_property_name(physical_name: str) -> str:
    return "total_amount" if physical_name == _PHYSICAL_AMOUNT_MARKER else physical_name


def _field_contract() -> list[dict]:
    return [
        {
            "name": name,
            "physical_type": physical_type,
            "nullable": False,
            "key_ordinal": 0 if name == "charge_line_id" else None,
            "ordinal": ordinal,
        }
        for ordinal, (name, _logical_type, physical_type) in enumerate(
            _DATASET_COLUMNS
        )
    ]


def _write_parquet(path: Path, rows: list[dict]) -> tuple[str, int]:
    schema = pa.schema(
        [
            pa.field("charge_line_id", pa.string(), nullable=False),
            pa.field("encounter_id", pa.string(), nullable=False),
            pa.field("facility_name", pa.string(), nullable=False),
            pa.field("service_name", pa.string(), nullable=False),
            pa.field("quantity", pa.float64(), nullable=False),
            pa.field(_PHYSICAL_AMOUNT_MARKER, pa.float64(), nullable=False),
        ]
    )
    parquet.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _seed_published_dataset_slice(
    db: Session,
    *,
    tmp_path: Path,
) -> dict:
    tenant = Tenant(id="tenant-provider-slice", name="Provider slice tenant")
    user = User(
        id="user-provider-slice",
        tenant_id=tenant.id,
        email="provider-slice@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-provider-slice",
        tenant_id=tenant.id,
        name="Published provider slice",
        namespace="generic-governed-slice",
        status="active",
    )
    db.add_all([tenant, user, scenario])
    db.flush()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id

    entity = OntologyEntity(
        id="entity-provider-charge",
        scenario_id=scenario.id,
        name="Governed charge record",
        api_name="medical_charge_line",
        namespace=scenario.namespace,
        lifecycle_status="active",
    )
    properties = [
        OntologyProperty(
            id=f"property-{_semantic_property_name(name)}",
            entity_id=entity.id,
            name=_semantic_property_name(name),
            api_name=_semantic_property_name(name),
            data_type=logical_type,
            is_key=name == "charge_line_id",
            is_required=True,
            is_sensitive=False,
        )
        for name, logical_type, _physical_type in _DATASET_COLUMNS
    ]
    definition_source = DataSource(
        id="source-provider-definition",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Definition-time logical source",
        type="dataset",
        config={},
        status="ok",
    )
    mapping = DataMapping(
        id="mapping-charge",
        scenario_id=scenario.id,
        entity_id=entity.id,
        data_source_id=definition_source.id,
        data_source_binding_key="records",
        data_source_binding_ref={"adapter": "dataset"},
        table_name=_PHYSICAL_TABLE_MARKER,
        column_map={
            _semantic_property_name(name): name
            for name, *_rest in _DATASET_COLUMNS
        },
        transform_rules={"charge_line_id": [{"op": "to_string"}]},
        status="ready",
    )
    function = FunctionDefinition(
        id="function-provider-slice",
        scenario_id=scenario.id,
        name="Published deterministic analysis",
        input_schema=_schema(),
        output_schema={"type": "object", "additionalProperties": True},
        runtime_kind="provider",
        runtime_config=_provider_config(),
    )
    db.add_all([entity, definition_source])
    db.flush()
    db.add_all([*properties, mapping, function])
    db.flush()

    field_contract = _field_contract()
    schema_document = {
        "relations": {_PHYSICAL_TABLE_MARKER: field_contract},
        "derived_relations": {},
    }
    dataset = LogicalDataset(
        id="dataset-provider-slice",
        tenant_id=tenant.id,
        key="provider-slice-records",
        name="Provider invocation records",
        created_by_user_id=user.id,
    )
    dataset_schema = DatasetSchema(
        id="schema-provider-slice",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash=dataset_query_service._canonical_json_sha256(schema_document),
        compatibility="none",
        schema_document=schema_document,
        created_by_user_id=user.id,
    )
    relation = DatasetRelation(
        id="relation-provider-physical-records",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=dataset_schema.id,
        relation_key=_PHYSICAL_TABLE_MARKER,
        display_name="Governed records",
        kind="table",
        ordinal=0,
    )
    fields = [
        DatasetField(
            id=f"field-{name}",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=dataset_schema.id,
            dataset_relation_id=relation.id,
            field_key=name,
            source_name=name,
            logical_type=logical_type,
            physical_type=physical_type,
            nullable=False,
            ordinal=ordinal,
            key_ordinal=0 if name == "charge_line_id" else None,
        )
        for ordinal, (name, logical_type, physical_type) in enumerate(
            _DATASET_COLUMNS
        )
    ]
    storage = DataSource(
        id="source-provider-storage",
        tenant_id=tenant.id,
        name="Managed fragment storage",
        type="file_bucket",
        config={"storage_backend": "minio"},
        status="ok",
    )
    port = ScenarioCapabilityPort(
        id="port-provider-records",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="records",
        name="Invocation records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=dataset.id,
        dataset_schema_id=dataset_schema.id,
        schema_document=schema_document,
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
        created_by_user_id=user.id,
    )
    db.add_all([dataset, storage])
    db.flush()
    db.add(dataset_schema)
    db.flush()
    db.add_all([relation, port])
    db.flush()
    db.add_all(fields)
    db.flush()

    rows_by_label = {
        "a": [
            {
                "charge_line_id": "a-violation-1",
                "encounter_id": "a-encounter-1",
                "facility_name": "Alpha Hospital",
                "service_name": "governed-service",
                "quantity": 3.0,
                _PHYSICAL_AMOUNT_MARKER: 10.0,
            },
            {
                "charge_line_id": "a-compliant-1",
                "encounter_id": "a-encounter-2",
                "facility_name": "Alpha Hospital",
                "service_name": "governed-service",
                "quantity": 1.0,
                _PHYSICAL_AMOUNT_MARKER: 5.0,
            },
        ],
        "b": [
            {
                "charge_line_id": f"b-violation-{index}",
                "encounter_id": f"b-encounter-{index}",
                "facility_name": "Beta Hospital",
                "service_name": "governed-service",
                "quantity": float(index + 2),
                _PHYSICAL_AMOUNT_MARKER: float(index * 10),
            }
            for index in range(1, 4)
        ]
        + [
            {
                "charge_line_id": "b-compliant-1",
                "encounter_id": "b-encounter-4",
                "facility_name": "Beta Hospital",
                "service_name": "governed-service",
                "quantity": 1.0,
                _PHYSICAL_AMOUNT_MARKER: 5.0,
            }
        ],
    }
    versions: list[DatasetVersion] = []
    paths_by_sha: dict[str, Path] = {}
    evidence_ids: dict[str, tuple[str, ...]] = {}
    relation_schema_hash = dataset_query_service._canonical_json_sha256(
        field_contract
    )
    for version_number, label in enumerate(("a", "b"), start=1):
        rows = rows_by_label[label]
        path = tmp_path / f"provider-slice-{label}.parquet"
        content_hash, byte_size = _write_parquet(path, rows)
        version = DatasetVersion(
            id=f"version-provider-{label}",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=dataset_schema.id,
            version_number=version_number,
            status="ready",
            record_count=len(rows),
            fragment_count=1,
            byte_size=byte_size,
            content_hash=content_hash,
            manifest={
                "relations": {
                    _PHYSICAL_TABLE_MARKER: {
                        "row_count": len(rows),
                        "byte_size": byte_size,
                        "content_sha256": content_hash,
                        "schema_hash": relation_schema_hash,
                    }
                },
                "derived_relations": {},
            },
            created_by_user_id=user.id,
        )
        bucket_file = BucketFile(
            id=f"file-provider-{label}",
            data_source_id=storage.id,
            filename=path.name,
            stored_path=f"minio://provider-slice/{path.name}",
            storage_provider="minio",
            bucket_name="provider-slice",
            object_key=f"fragments/{path.name}",
            object_version_id=f"object-version-{label}",
            size=byte_size,
            mime="application/vnd.apache.parquet",
            content_sha256=content_hash,
            status="parsed",
        )
        fragment = DatasetFragment(
            id=f"fragment-provider-{label}",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            dataset_version_id=version.id,
            dataset_relation_id=relation.id,
            schema_id=dataset_schema.id,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=storage.id,
            ordinal=0,
            format="parquet",
            status="ready",
            row_count=len(rows),
            byte_size=byte_size,
            content_sha256=content_hash,
        )
        db.add_all([version, bucket_file])
        db.flush()
        db.add(fragment)
        db.flush()
        versions.append(version)
        paths_by_sha[content_hash] = path
        evidence_ids[label] = tuple(
            row["charge_line_id"] for row in rows if row["quantity"] > 2
        )

    branch = OntologyBranch(
        id="branch-provider-slice",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="main",
        created_by_user_id=user.id,
    )
    db.add(branch)
    db.flush()
    content = release_service.capture_snapshot_content(db, scenario)
    snapshot = OntologySnapshot(
        id="snapshot-provider-slice",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        branch_id=branch.id,
        kind="merge",
        content=content,
        content_hash=release_service.snapshot_hash(content),
        created_by_user_id=user.id,
    )
    db.add(snapshot)
    db.flush()
    branch.base_snapshot_id = snapshot.id
    branch.head_snapshot_id = snapshot.id
    release = OntologyRelease(
        id="release-provider-slice",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        branch_id=branch.id,
        snapshot_id=snapshot.id,
        environment="staging",
        status="released",
        created_by_user_id=user.id,
    )
    db.add(release)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return {
        "tenant": tenant,
        "user": user,
        "scenario": scenario,
        "function": function,
        "versions": tuple(versions),
        "paths_by_sha": paths_by_sha,
        "evidence_ids": evidence_ids,
        "snapshot": snapshot,
        "release": release,
    }


def _postgres_integration_config() -> tuple[dict, str]:
    if os.environ.get("RUN_PROVIDER_POSTGRES_INTEGRATION") != "1":
        pytest.skip(
            "set RUN_PROVIDER_POSTGRES_INTEGRATION=1 to run the real connector test"
        )
    settings = get_settings()
    url = make_url(settings.database_url)
    integration_user = settings.postgresql_admin_user or url.username or ""
    integration_password = (
        settings.postgresql_admin_password
        if settings.postgresql_admin_user
        else url.password or ""
    )
    config = {
        "host": url.host or "",
        "port": int(url.port or 5432),
        "database": url.database or "",
        "user": integration_user,
        "password": integration_password,
    }
    if not all(str(config[key] or "").strip() for key in ("host", "database", "user")):
        pytest.skip("configured PostgreSQL connection is incomplete")
    marker = f"provider-secret-marker-{uuid4().hex}"
    config["secret_marker"] = marker
    return config, marker


def _seed_published_connector_slice(
    db: Session,
    *,
    connector_config: dict,
    table_name: str,
) -> dict:
    tenant = Tenant(id="tenant-provider-live", name="Provider live tenant")
    user = User(
        id="user-provider-live",
        tenant_id=tenant.id,
        email="provider-live@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-provider-live",
        tenant_id=tenant.id,
        name="Published provider live connector",
        namespace="generic-governed-live",
        status="active",
    )
    db.add_all([tenant, user, scenario])
    db.flush()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id

    entity = OntologyEntity(
        id="entity-provider-live-charge",
        scenario_id=scenario.id,
        name="Governed live charge record",
        api_name="medical_charge_line",
        namespace=scenario.namespace,
        lifecycle_status="active",
    )
    properties = [
        OntologyProperty(
            id=f"property-live-{_semantic_property_name(name)}",
            entity_id=entity.id,
            name=_semantic_property_name(name),
            api_name=_semantic_property_name(name),
            data_type=logical_type,
            is_key=name == "charge_line_id",
            is_required=True,
            is_sensitive=False,
        )
        for name, logical_type, _physical_type in _DATASET_COLUMNS
    ]
    source = DataSource(
        id="source-provider-live",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Managed live connector",
        type="postgres",
        config=connector_config,
        status="ok",
    )
    mapping = DataMapping(
        id="mapping-charge",
        scenario_id=scenario.id,
        entity_id=entity.id,
        data_source_id=source.id,
        data_source_binding_key="records.live",
        data_source_binding_ref={"adapter": "postgres"},
        table_name=table_name,
        column_map={
            _semantic_property_name(name): name
            for name, *_rest in _DATASET_COLUMNS
        },
        transform_rules={"charge_line_id": [{"op": "to_string"}]},
        status="ready",
    )
    function = FunctionDefinition(
        id="function-provider-live",
        scenario_id=scenario.id,
        name="Published live deterministic analysis",
        input_schema=_schema(),
        output_schema={"type": "object", "additionalProperties": True},
        runtime_kind="provider",
        runtime_config=_provider_config(),
    )
    port = ScenarioCapabilityPort(
        id="port-provider-live-records",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="records",
        name="Live invocation records",
        direction="input",
        role="invocation_input",
        media_kind="connector",
        schema_document={"type": "object", "additionalProperties": True},
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
        created_by_user_id=user.id,
    )
    db.add_all([entity, source])
    db.flush()
    db.add_all([*properties, mapping, function, port])
    db.flush()

    branch = OntologyBranch(
        id="branch-provider-live",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="main",
        created_by_user_id=user.id,
    )
    db.add(branch)
    db.flush()
    content = release_service.capture_snapshot_content(db, scenario)
    snapshot = OntologySnapshot(
        id="snapshot-provider-live",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        branch_id=branch.id,
        kind="merge",
        content=content,
        content_hash=release_service.snapshot_hash(content),
        created_by_user_id=user.id,
    )
    db.add(snapshot)
    db.flush()
    branch.base_snapshot_id = snapshot.id
    branch.head_snapshot_id = snapshot.id
    release = OntologyRelease(
        id="release-provider-live",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        branch_id=branch.id,
        snapshot_id=snapshot.id,
        environment="staging",
        status="released",
        created_by_user_id=user.id,
    )
    db.add(release)
    db.flush()
    binding = ConnectorBinding(
        id="binding-provider-live",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        environment="staging",
        binding_key="records.live",
        reference_label="Managed live records",
        connector_kind="data_source",
        connector_id=source.id,
        health_status="healthy",
        connector_signature=connector_service.connector_signature(
            "data_source", source
        ),
        created_by_user_id=user.id,
    )
    db.add(binding)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return {
        "tenant": tenant,
        "user": user,
        "scenario": scenario,
        "source": source,
        "function": function,
        "binding": binding,
        "snapshot": snapshot,
        "release": release,
    }


def _seed_versions(db: Session, *, key: str):
    tenant = Tenant(id=f"tenant-{key}", name=f"Tenant {key}")
    scenario = BusinessScenario(
        id=f"scenario-{key}",
        tenant_id=tenant.id,
        name=f"Scenario {key}",
        status="active",
    )
    dataset = LogicalDataset(
        id=f"dataset-{key}",
        tenant_id=tenant.id,
        key=f"records-{key}",
        name="Invocation records",
    )
    schema = DatasetSchema(
        id=f"schema-{key}",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash="a" * 64,
        compatibility="none",
        schema_document={"type": "array"},
    )
    versions = (
        DatasetVersion(
            id=f"version-a-{key}",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=1,
            status="ready",
            content_hash="1" * 64,
        ),
        DatasetVersion(
            id=f"version-b-{key}",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=2,
            status="ready",
            content_hash="2" * 64,
        ),
    )
    port = ScenarioCapabilityPort(
        id=f"port-{key}",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key="function-audit",
        port_key="records",
        name="Invocation records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=dataset.id,
        dataset_schema_id=schema.id,
        schema_document={"type": "array"},
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
    )
    db.add(tenant)
    db.flush()
    db.add_all([scenario, dataset])
    db.flush()
    db.add(schema)
    db.flush()
    db.add_all([*versions, port])
    db.flush()
    return tenant, scenario, versions


def _shadow_extension(
    db: Session,
    *,
    tenant: Tenant,
    scenario: BusinessScenario,
    source: DataSource,
    adapter: str,
) -> tuple[MedicalAuditAgentExtension, LegacyCapabilityMatch]:
    definition = _definition(tenant_id=tenant.id, scenario_id=scenario.id)
    definition_mapping = definition.mappings["mapping-charge"]
    runtime_mapping = SimpleNamespace(**vars(definition_mapping))
    runtime_mapping.definition_data_source_id = definition_mapping.data_source_id
    runtime_mapping.data_source_id = source.id
    runtime_mapping.data_source_binding_ref = {"adapter": adapter}
    context = SimpleNamespace(
        db=db,
        tenant_id=tenant.id,
        scenario=scenario,
        runtime_definition=definition,
        functions=tuple(definition.functions.values()),
        data_sources=[source],
        mappings=[runtime_mapping],
    )
    extension = MedicalAuditAgentExtension(context=context, ready=True)
    match = LegacyCapabilityMatch(
        owner_key=PROVIDER_KEY,
        owner_version=PROVIDER_VERSION,
        capability_kind="function",
        capability_key="function-audit",
        inputs={"strategy": "charge_threshold"},
        comparison_result={},
    )
    return extension, match


def test_shadow_data_proof_rejects_dataset_version_drift(db: Session) -> None:
    tenant, scenario, versions = _seed_versions(db, key="shadow-dataset")
    version_a, version_b = versions
    source = DataSource(
        id="source-shadow-dataset",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Pinned legacy dataset",
        type="dataset",
        config={
            "dataset_id": version_a.dataset_id,
            "dataset_version_id": version_a.id,
        },
        status="ok",
    )
    db.add(source)
    db.flush()
    extension, match = _shadow_extension(
        db,
        tenant=tenant,
        scenario=scenario,
        source=source,
        adapter="dataset",
    )
    context_a = RuntimeDataContext(
        (
            ResolvedDataHandle(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=version_a.id,
                version_id=version_a.id,
                signature=version_a.content_hash,
            ),
        )
    )
    context_b = RuntimeDataContext(
        (
            ResolvedDataHandle(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=version_b.id,
                version_id=version_b.id,
                signature=version_b.content_hash,
            ),
        )
    )

    proof_a = extension.verify_shadow_data_context(match, context_a)
    assert proof_a is not None and len(proof_a) == 64
    assert extension.verify_shadow_data_context(match, context_b) is None

    source.config = {
        "dataset_id": version_b.dataset_id,
        "dataset_version_id": version_b.id,
    }
    db.flush()
    assert extension.verify_shadow_data_context(match, context_a) is None
    proof_b = extension.verify_shadow_data_context(match, context_b)
    assert proof_b is not None and proof_b != proof_a


def test_shadow_data_proof_rejects_connector_binding_drift(db: Session) -> None:
    tenant = Tenant(id="tenant-shadow-connector", name="Shadow connector tenant")
    scenario = BusinessScenario(
        id="scenario-shadow-connector",
        tenant_id=tenant.id,
        name="Shadow connector scenario",
        status="active",
    )
    source = DataSource(
        id="source-shadow-connector",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Legacy connector",
        type="postgres",
        config={
            "host": "db.internal",
            "port": 5432,
            "database": "governed",
            "user": "runtime",
        },
        status="ok",
    )
    replacement = DataSource(
        id="source-shadow-replacement",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Replacement connector",
        type="postgres",
        config={
            "host": "db.internal",
            "port": 5432,
            "database": "replacement",
            "user": "runtime",
        },
        status="ok",
    )
    db.add_all([tenant, scenario])
    db.flush()
    db.add_all([source, replacement])
    db.flush()
    signature = connector_service.connector_signature("data_source", source)
    binding = ConnectorBinding(
        id="binding-shadow-connector",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        environment="dev",
        binding_key="records",
        reference_label="Governed records",
        connector_kind="data_source",
        connector_id=source.id,
        health_status="healthy",
        connector_signature=signature,
    )
    db.add(binding)
    db.flush()
    extension, match = _shadow_extension(
        db,
        tenant=tenant,
        scenario=scenario,
        source=source,
        adapter="postgres",
    )
    context = RuntimeDataContext(
        (
            ResolvedDataHandle(
                port_key="records",
                binding_kind="connector_binding",
                reference_id=binding.id,
                signature=signature,
            ),
        )
    )

    proof = extension.verify_shadow_data_context(match, context)
    assert proof is not None and len(proof) == 64
    assert extension.verify_shadow_data_context(match, RuntimeDataContext()) is None

    original_config = dict(source.config)
    source.config = {**original_config, "database": "drifted"}
    db.flush()
    assert extension.verify_shadow_data_context(match, context) is None

    source.config = original_config
    binding.connector_id = replacement.id
    db.flush()
    assert extension.verify_shadow_data_context(match, context) is None


def test_provider_binding_is_normalized_and_changes_live_definition_hash(db: Session) -> None:
    tenant = Tenant(id="tenant-provider-hash", name="Provider hash tenant")
    scenario = BusinessScenario(
        id="scenario-provider-hash",
        tenant_id=tenant.id,
        name="Provider hash scenario",
        status="active",
    )
    function = FunctionDefinition(
        id="function-provider-hash",
        scenario_id=scenario.id,
        name="Versioned provider function",
        input_schema=_schema(),
        output_schema={"type": "object", "properties": {}},
        runtime_kind="provider",
        runtime_config=_provider_config(),
    )
    db.add_all([tenant, scenario, function])
    db.flush()

    first = runtime_definition_service.resolve_active(db, scenario, environment="dev")
    function.runtime_config = _provider_config(version="1.0.1")
    db.flush()
    second = runtime_definition_service.resolve_active(db, scenario, environment="dev")

    assert first.definition_hash != second.definition_hash
    assert first.functions[function.id].runtime_config["provider_version"] == PROVIDER_VERSION
    assert second.functions[function.id].runtime_config["provider_version"] == "1.0.1"

    normalized = function_definition_service.normalize_definition(
        {
            "name": function.name,
            "description": "",
            "input_schema": _schema(),
            "output_schema": {"type": "object", "properties": {}},
            "runtime_kind": "provider",
            "runtime_config": _provider_config(),
        }
    )
    assert normalized["runtime_config"] == _provider_config()
    with pytest.raises(function_definition_service.FunctionDefinitionError):
        function_definition_service.normalize_definition(
            {
                **normalized,
                "runtime_config": {
                    **_provider_config(),
                    "provider_config": {"password": "must-not-be-persisted"},
                },
            }
        )


def test_provider_bound_function_is_discovered_by_generic_application_service(
    db: Session,
) -> None:
    tenant = Tenant(id="tenant-provider-discovery", name="Provider discovery tenant")
    user = User(
        id="user-provider-discovery",
        tenant_id=tenant.id,
        email="provider-discovery@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-provider-discovery",
        tenant_id=tenant.id,
        name="Provider discovery scenario",
        status="active",
    )
    function = FunctionDefinition(
        id="function-provider-discovery",
        scenario_id=scenario.id,
        name="Provider-backed function",
        input_schema=_schema(),
        output_schema={"type": "object", "properties": {}},
        runtime_kind="provider",
        runtime_config=_provider_config(),
    )
    port = ScenarioCapabilityPort(
        id="port-provider-discovery",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="records",
        name="Invocation records",
        direction="input",
        role="invocation_input",
        media_kind="structured",
        schema_document={"type": "object"},
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
        config={
            "allowed_binding_kinds": [
                "dataset_version",
                "dataset_head",
                "connector_binding",
            ]
        },
    )
    db.add_all([tenant, user, scenario, function, port])
    db.flush()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id

    catalog = capability_application_service.list_capabilities(
        db,
        scenario,
        environment="dev",
    )
    item = next(value for value in catalog if value["key"] == function.id)

    assert item["kind"] == "function"
    assert item["input_schema"] == _schema()
    assert item["readiness"]["ready"] is True
    assert item["readiness"]["issues"] == [{
        "axis": "runtime",
        "blocking": False,
        "code": "invocation_input_required",
        "message": "required managed input must be supplied with the invocation",
        "port_key": "records",
    }]


def test_provider_version_mismatch_fails_before_invocation_audit(db: Session) -> None:
    tenant, scenario, _versions = _seed_versions(db, key="version-mismatch")
    definition = _definition(
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        version="9.9.9",
    )
    deployment = ResolvedDeployment(
        scenario_id=scenario.id,
        tenant_id=tenant.id,
        environment="dev",
        definition_hash=definition.definition_hash,
        definition=definition,
    )

    with pytest.raises(CapabilityInvocationError) as captured:
        resolve_capability_contract(
            db,
            deployment,
            CapabilityRef(kind="function", resource_id="function-audit"),
            registry=_registry(),
        )
    assert captured.value.code == "provider_version_mismatch"


def test_frozen_releases_resolve_exact_versions_from_same_provider_key(
    db: Session,
) -> None:
    tenant = Tenant(id="tenant-versioned-provider", name="Versioned provider tenant")
    db.add(tenant)
    db.flush()

    published = []
    for label, version in (("v1", "1.0.0"), ("v2", "2.0.0")):
        scenario = BusinessScenario(
            id=f"scenario-provider-{label}",
            tenant_id=tenant.id,
            name=f"Provider release {label}",
            namespace="generic-version-probe",
            status="active",
        )
        function = FunctionDefinition(
            id=f"function-provider-{label}",
            scenario_id=scenario.id,
            name=f"Version probe {label}",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            runtime_kind="provider",
            runtime_config={
                "provider_key": _VersionProbeProvider.provider_key,
                "provider_version": version,
                "provider_config": {"contract": "version-probe/v1"},
            },
        )
        db.add_all([scenario, function])
        db.flush()
        branch = OntologyBranch(
            id=f"branch-provider-{label}",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="main",
        )
        db.add(branch)
        db.flush()
        content = release_service.capture_snapshot_content(db, scenario)
        snapshot = OntologySnapshot(
            id=f"snapshot-provider-{label}",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            branch_id=branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
        )
        db.add(snapshot)
        db.flush()
        branch.base_snapshot_id = snapshot.id
        branch.head_snapshot_id = snapshot.id
        release = OntologyRelease(
            id=f"release-provider-{label}",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="staging",
            status="released",
        )
        db.add(release)
        db.commit()
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment="staging",
        )
        with pytest.raises(TypeError):
            definition.functions[function.id].runtime_config["provider_version"] = (
                "changed"
            )
        published.append((scenario, function, release, definition, version))

    registry = CapabilityProviderRegistry()
    registry.register_instance(_VersionProbeProvider("1.0.0"))
    registry.register_instance(_VersionProbeProvider("2.0.0"))
    registry.seal()
    invoker = CapabilityInvoker(registry)
    db.info["tenant_id"] = tenant.id
    receipts = []
    for scenario, function, release, definition, version in published:
        extensions = bind_agent_providers(
            SimpleNamespace(
                runtime_definition=definition,
                functions=tuple(definition.functions.values()),
                actions=(),
                workflows=(),
            ),
            registry=registry,
        )
        assert [
            (extension.provider_key, extension.provider_version)
            for extension in extensions
        ] == [(_VersionProbeProvider.provider_key, version)]
        deployment = ResolvedDeployment(
            scenario_id=scenario.id,
            tenant_id=tenant.id,
            environment="staging",
            definition_hash=definition.definition_hash,
            definition=definition,
            definition_source="release",
            snapshot_id=definition.snapshot_id,
            release_id=definition.release_id,
        )
        receipt = invoker.invoke(
            db,
            deployment,
            Actor(
                actor_type="service",
                principal_id="versioned-provider-verifier",
                tenant_id=tenant.id,
            ),
            Request(
                capability=CapabilityRef(
                    kind="function",
                    resource_id=function.id,
                ),
                inputs={},
                correlation_id=f"version-probe-{version}",
                expected_definition_hash=deployment.definition_hash,
                expected_deployment_fingerprint=deployment.fingerprint,
            ),
            invocation_source="internal",
        )
        assert receipt.status == "succeeded"
        assert receipt.output == {
            "provider_version": version,
            "release_id": release.id,
            "definition_hash": definition.definition_hash,
        }
        receipts.append(receipt)

    assert receipts[0].definition_hash != receipts[1].definition_hash
    with pytest.raises(CapabilityRegistryError):
        registry.resolve(_VersionProbeProvider.provider_key)

    legacy_context = SimpleNamespace(
        runtime_definition=SimpleNamespace(functions={}, actions={}, workflows={}),
        functions=(),
        actions=(),
        workflows=(),
    )
    assert bind_agent_providers(legacy_context, registry=registry) == ()

    unknown_function = SimpleNamespace(
        runtime_config={
            "provider_key": _VersionProbeProvider.provider_key,
            "provider_version": "9.9.9",
        }
    )
    with pytest.raises(AgentProviderExtensionError):
        bind_agent_providers(
            SimpleNamespace(
                runtime_definition=SimpleNamespace(
                    functions={"unknown": unknown_function},
                    actions={},
                    workflows={},
                ),
                functions=(unknown_function,),
                actions=(),
                workflows=(),
            ),
            registry=registry,
        )


def test_same_definition_invokes_dataset_versions_a_and_b_without_cross_evidence(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeded = _seed_published_dataset_slice(db, tmp_path=tmp_path)
    tenant = seeded["tenant"]
    user = seeded["user"]
    scenario = seeded["scenario"]
    function = seeded["function"]
    versions = seeded["versions"]
    paths_by_sha = seeded["paths_by_sha"]
    deployment, _inputs = capability_application_service.resolve_deployment(
        db,
        scenario,
        environment="staging",
    )
    assert deployment.definition_source == "release"
    assert deployment.definition.source == "release"
    assert deployment.release_id == seeded["release"].id
    assert deployment.snapshot_id == seeded["snapshot"].id
    assert deployment.definition.release_id == seeded["release"].id
    assert deployment.definition.mappings["mapping-charge"].status == "unknown"

    actor = Actor(
        actor_type="user",
        principal_id=user.id,
        tenant_id=tenant.id,
        user_id=user.id,
    )
    LocalSession = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(dataset_query_service, "SessionLocal", LocalSession)
    monkeypatch.setattr(
        dataset_query_service,
        "get_settings",
        lambda: SimpleNamespace(
            dataset_query_timeout_seconds=10.0,
            dataset_query_max_concurrency=2,
            dataset_duckdb_memory_limit_bytes=128 * 1024 * 1024,
            dataset_duckdb_threads=1,
            dataset_duckdb_temp_directory=str(tmp_path / "duckdb-temp"),
            dataset_duckdb_max_temp_directory_bytes=128 * 1024 * 1024,
        ),
    )

    def local_fragment(fragment):
        path = paths_by_sha[fragment.content_sha256]
        return SimpleNamespace(path=path, release=lambda: None)

    monkeypatch.setattr(
        dataset_query_service,
        "_acquire_cached_fragment",
        local_fragment,
    )
    invoker = CapabilityInvoker(_registry())
    receipts = []
    for index, version in enumerate(versions):
        request = Request(
            capability=CapabilityRef(
                kind="function",
                resource_id=function.id,
            ),
            inputs={
                "strategy": "charge_threshold",
                "service_name": "governed-service",
                "threshold": 2,
                "limit": 20,
                "offset": 0,
            },
            binding_overrides=(
                BindingOverride(
                    port_key="records",
                    binding_kind="dataset_version",
                    reference_id=version.id,
                    signature=version.content_hash,
                ),
            ),
            correlation_id=f"provider-slice-{index}",
            expected_definition_hash=deployment.definition_hash,
            expected_deployment_fingerprint=deployment.fingerprint,
        )
        receipts.append(
            capability_application_service.invoke(
                db,
                scenario,
                actor,
                request,
                environment="staging",
                invocation_source="rest",
                invoker=invoker,
            )
        )

    first, second = receipts
    assert first.status == second.status == "succeeded", (
        (first.error_code, first.error_message),
        (second.error_code, second.error_message),
    )
    assert first.definition_hash == second.definition_hash == deployment.definition_hash
    assert first.deployment_fingerprint == second.deployment_fingerprint
    assert first.data_context_fingerprint != second.data_context_fingerprint
    assert first.output["summary"]["violation_count"] == 1
    assert second.output["summary"]["violation_count"] == 3
    assert first.output["summary"]["violation_amount"] == 10
    assert second.output["summary"]["violation_amount"] == 60
    assert {
        record["charge_line_id"] for record in first.output["records"]
    } == set(seeded["evidence_ids"]["a"])
    assert {
        record["charge_line_id"] for record in second.output["records"]
    } == set(seeded["evidence_ids"]["b"])

    for receipt, selected, excluded in (
        (first, versions[0], versions[1]),
        (second, versions[1], versions[0]),
    ):
        serialized = canonical_json(receipt.output)
        grounding = receipt.output["grounding"]
        handle = grounding["provenance"]["data_handles"][0]
        assert receipt.output["evidence"]["source_id"] == selected.id
        assert receipt.output["evidence"]["source_name"] == "managed:records"
        assert "tables" not in receipt.output["evidence"]
        assert "resolved_columns" not in receipt.output["evidence"]
        assert "medical_charge_line.total_amount" in receipt.output["evidence"][
            "semantic_properties"
        ]
        assert receipt.output["evidence"][
            "mapping_contract_fingerprint"
        ] == receipt.output["lineage"]["mapping_contract"]["fingerprint"]
        assert handle["reference_id"] == selected.id
        assert handle["version_id"] == selected.id
        assert handle["signature"] == selected.content_hash
        assert receipt.output["evidence"]["governed_reference"] == {
            "binding_kind": "dataset_version",
            "reference_id": selected.id,
            "version_id": selected.id,
            "signature": selected.content_hash,
        }
        assert grounding["provenance"]["definition_hash"] == deployment.definition_hash
        assert grounding["provenance"]["data_context_fingerprint"] == (
            receipt.data_context_fingerprint
        )
        assert receipt.output["lineage"]["mapping_contract"]["definition"] == {
            "source": "release",
            "environment": "staging",
            "definition_hash": deployment.definition_hash,
            "snapshot_id": seeded["snapshot"].id,
            "release_id": seeded["release"].id,
        }
        assert excluded.id not in serialized
        assert excluded.content_hash not in serialized
        excluded_label = "b" if selected.id.endswith("-a") else "a"
        assert all(
            charge_line_id not in serialized
            for charge_line_id in seeded["evidence_ids"][excluded_label]
        )

        invocation = db.get(CapabilityInvocation, receipt.invocation_id)
        assert invocation is not None
        run_binding = db.scalar(
            select(RunInputBinding).where(
                RunInputBinding.invocation_id == receipt.invocation_id
            )
        )
        assert run_binding is not None
        serialized_audit = canonical_json(
            {
                "request": invocation.request_document,
                "result": invocation.result_document,
                "binding": run_binding.binding_document,
            }
        )
        for public_document in (serialized, serialized_audit):
            assert selected.id in public_document
            assert selected.content_hash in public_document
            assert excluded.id not in public_document
            assert excluded.content_hash not in public_document
            assert all(
                charge_line_id not in public_document
                for charge_line_id in seeded["evidence_ids"][excluded_label]
            )
            assert _PHYSICAL_TABLE_MARKER not in public_document
            assert _PHYSICAL_AMOUNT_MARKER not in public_document
            assert "source-provider-definition" not in public_document
            assert "source-provider-storage" not in public_document

    bindings = db.scalars(
        select(RunInputBinding).order_by(RunInputBinding.created_at, RunInputBinding.id)
    ).all()
    assert {binding.resolved_dataset_version_id for binding in bindings} == {
        versions[0].id,
        versions[1].id,
    }


def test_real_connector_binding_invocation_keeps_credentials_behind_governed_reference(
    db: Session,
) -> None:
    connector_config, secret_marker = _postgres_integration_config()
    table_name = f"provider_live_{uuid4().hex}"
    seeded = _seed_published_connector_slice(
        db,
        connector_config=connector_config,
        table_name=table_name,
    )
    source = seeded["source"]
    external_engine = datasource_service.get_engine(source)
    table_created = False
    try:
        with external_engine.begin() as connection:
            connection.exec_driver_sql(
                f'CREATE TABLE "{table_name}" ('
                'charge_line_id TEXT NOT NULL, encounter_id TEXT NOT NULL, '
                'facility_name TEXT NOT NULL, service_name TEXT NOT NULL, '
                'quantity DOUBLE PRECISION NOT NULL, '
                f'"{_PHYSICAL_AMOUNT_MARKER}" DOUBLE PRECISION NOT NULL)'
            )
            table_created = True
            connection.execute(
                text(
                    f'INSERT INTO "{table_name}" '
                    '(charge_line_id, encounter_id, facility_name, service_name, '
                    f'quantity, "{_PHYSICAL_AMOUNT_MARKER}") VALUES '
                    '(:charge_line_id, :encounter_id, :facility_name, :service_name, '
                    ':quantity, :charged_amount)'
                ),
                [
                    {
                        "charge_line_id": "live-violation-1",
                        "encounter_id": "live-encounter-1",
                        "facility_name": "Live Hospital",
                        "service_name": "governed-service",
                        "quantity": 3.0,
                        "charged_amount": 20.0,
                    },
                    {
                        "charge_line_id": "live-violation-2",
                        "encounter_id": "live-encounter-2",
                        "facility_name": "Live Hospital",
                        "service_name": "governed-service",
                        "quantity": 4.0,
                        "charged_amount": 30.0,
                    },
                    {
                        "charge_line_id": "live-compliant-1",
                        "encounter_id": "live-encounter-3",
                        "facility_name": "Live Hospital",
                        "service_name": "governed-service",
                        "quantity": 1.0,
                        "charged_amount": 5.0,
                    },
                ],
            )

        tenant = seeded["tenant"]
        user = seeded["user"]
        scenario = seeded["scenario"]
        function = seeded["function"]
        binding = seeded["binding"]
        deployment, _inputs = capability_application_service.resolve_deployment(
            db,
            scenario,
            environment="staging",
        )
        request = Request(
            capability=CapabilityRef(kind="function", resource_id=function.id),
            inputs={
                "strategy": "charge_threshold",
                "service_name": "governed-service",
                "threshold": 2,
                "limit": 20,
                "offset": 0,
            },
            binding_overrides=(
                BindingOverride(
                    port_key="records",
                    binding_kind="connector_binding",
                    reference_id=binding.id,
                    signature=binding.connector_signature,
                ),
            ),
            correlation_id="provider-live-connector",
            expected_definition_hash=deployment.definition_hash,
            expected_deployment_fingerprint=deployment.fingerprint,
        )
        receipt = capability_application_service.invoke(
            db,
            scenario,
            Actor(
                actor_type="user",
                principal_id=user.id,
                tenant_id=tenant.id,
                user_id=user.id,
            ),
            request,
            environment="staging",
            invocation_source="rest",
            invoker=CapabilityInvoker(_registry()),
        )
        assert receipt.status == "succeeded", (
            receipt.error_code,
            receipt.error_message,
        )
        assert receipt.output["summary"]["violation_count"] == 2
        assert receipt.output["summary"]["violation_amount"] == 50
        assert {
            record["charge_line_id"] for record in receipt.output["records"]
        } == {"live-violation-1", "live-violation-2"}
        assert receipt.output["evidence"]["source_id"] == binding.id
        assert receipt.output["evidence"]["source_name"] == "managed:records"
        assert "connector_revision" not in receipt.output["evidence"]
        assert "tables" not in receipt.output["evidence"]
        assert "resolved_columns" not in receipt.output["evidence"]
        assert "medical_charge_line.total_amount" in receipt.output["evidence"][
            "semantic_properties"
        ]
        assert receipt.output["evidence"][
            "mapping_contract_fingerprint"
        ] == receipt.output["lineage"]["mapping_contract"]["fingerprint"]
        assert receipt.output["evidence"]["governed_reference"] == {
            "binding_kind": "connector_binding",
            "reference_id": binding.id,
            "version_id": None,
            "signature": binding.connector_signature,
        }

        run_binding = db.scalar(
            select(RunInputBinding).where(
                RunInputBinding.invocation_id == receipt.invocation_id
            )
        )
        assert run_binding is not None
        assert run_binding.source_kind == "connector_binding"
        assert run_binding.connector_binding_id == binding.id
        assert run_binding.source_dataset_version_id is None
        assert run_binding.resolved_dataset_version_id is None
        assert db.scalars(select(DatasetVersion)).all() == []

        invocation = db.get(CapabilityInvocation, receipt.invocation_id)
        assert invocation is not None
        serialized_output = canonical_json(receipt.output)
        serialized_audit = canonical_json(
            {
                "request": invocation.request_document,
                "result": invocation.result_document,
                "binding": run_binding.binding_document,
            }
        )
        for serialized in (serialized_output, serialized_audit):
            assert binding.id in serialized
            assert binding.connector_signature in serialized
            assert source.id not in serialized
            assert secret_marker not in serialized
            assert table_name not in serialized
            assert _PHYSICAL_AMOUNT_MARKER not in serialized
            if connector_config["password"]:
                assert connector_config["password"] not in serialized
    finally:
        if table_created:
            with external_engine.begin() as connection:
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
        datasource_service.invalidate_engine(source)
