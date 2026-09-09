from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    BucketFile,
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetField,
    DatasetHead,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    RunInputBinding,
    ScenarioCapabilityPort,
    Tenant,
)
from app.services.runtime_input_service import (
    RuntimeInputResolutionError,
    resolve_runtime_inputs,
)
from app.services import connector_service, input_contract_validator
from app.services.capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityRef,
    Request,
    ResolvedDeployment,
)


@pytest.fixture(scope="module")
def engine():
    value = create_engine("sqlite://")

    @event.listens_for(value, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(value)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def db(engine):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@dataclass(frozen=True)
class World:
    tenant: Tenant
    scenario: BusinessScenario
    dataset: LogicalDataset
    schema: DatasetSchema
    version_a: DatasetVersion
    version_b: DatasetVersion


def _world(db: Session, key: str) -> World:
    tenant = Tenant(id=f"t-{key}", name=f"Tenant {key}")
    scenario = BusinessScenario(
        id=f"s-{key}",
        tenant_id=tenant.id,
        name=f"Scenario {key}",
        status="active",
    )
    dataset = LogicalDataset(
        id=f"d-{key}",
        tenant_id=tenant.id,
        key=f"dataset-{key}",
        name=f"Dataset {key}",
    )
    schema = DatasetSchema(
        id=f"ds-{key}",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash=(key[0].lower() if key[0].lower() in "abcdef" else "a") * 64,
        compatibility="none",
        schema_document={"type": "array"},
    )
    version_a = DatasetVersion(
        id=f"va-{key}",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        content_hash="1" * 64,
    )
    version_b = DatasetVersion(
        id=f"vb-{key}",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=2,
        status="ready",
        content_hash="2" * 64,
    )
    db.add(tenant)
    db.flush()
    db.add_all([scenario, dataset])
    db.flush()
    db.add(schema)
    db.flush()
    db.add_all([version_a, version_b])
    db.flush()
    return World(tenant, scenario, dataset, schema, version_a, version_b)


def _dataset_port(
    db: Session,
    world: World,
    *,
    key: str = "records",
    role: str = "invocation_input",
    binding_policy: str = "per_invocation",
    required: bool = True,
    cardinality: str = "one",
    config: dict | None = None,
) -> ScenarioCapabilityPort:
    port = ScenarioCapabilityPort(
        id=f"p-{world.scenario.id}-{key}",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key=key,
        name=key.title(),
        direction="input",
        role=role,
        media_kind="dataset",
        dataset_id=world.dataset.id,
        dataset_schema_id=world.schema.id,
        schema_document={"type": "array"},
        is_required=required,
        cardinality=cardinality,
        binding_policy=binding_policy,
        status="active",
        config=config or {},
    )
    db.add(port)
    db.flush()
    return port


def _invoke(
    db: Session,
    world: World,
    *,
    request_id: str,
    overrides=None,
    request_overrides: tuple[BindingOverride, ...] = (),
    environment: str = "dev",
):
    request = Request(
        capability=CapabilityRef(
            kind="function",
            resource_id=f"capability-{world.scenario.id}",
        ),
        binding_overrides=request_overrides,
        correlation_id=request_id,
    )
    deployment = ResolvedDeployment(
        scenario_id=world.scenario.id,
        tenant_id=world.tenant.id,
        definition_hash="d" * 64,
        definition=object(),
    )
    actor = Actor(
        actor_type="service",
        principal_id=f"principal-{world.tenant.id}",
        tenant_id=world.tenant.id,
    )
    return resolve_runtime_inputs(
        db,
        request=request,
        deployment=deployment,
        actor=actor,
        request_id=request_id,
        overrides=overrides,
    )


def test_zero_managed_ports_creates_empty_audited_context(db: Session) -> None:
    world = _world(db, "zero")

    result = _invoke(db, world, request_id="zero-request")

    assert result.runtime_data_context.handles == ()
    assert result.input_bindings == ()
    assert result.invocation.input_hash
    assert result.invocation.capability_kind == "function"
    assert result.invocation.capability_key == f"capability-{world.scenario.id}"
    assert result.invocation.definition_hash == "d" * 64
    assert result.invocation.deployment_fingerprint
    assert (
        result.invocation.data_context_fingerprint
        == result.runtime_data_context.fingerprint
    )
    assert result.invocation.correlation_id == "zero-request"
    assert result.invocation.principal_type == "service"
    assert result.invocation.principal_id == f"principal-{world.tenant.id}"
    assert result.invocation.request_document["managed_inputs"] == []
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1
    assert db.scalar(select(func.count(RunInputBinding.id))) == 0


def test_required_missing_is_structured_and_creates_no_partial_audit(db: Session) -> None:
    world = _world(db, "missing")
    _dataset_port(db, world)

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(db, world, request_id="missing-request")

    error = captured.value
    assert error.code == "required_runtime_inputs_missing"
    assert error.as_dict()["details"]["missing_ports"] == ("records",)
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0
    assert db.scalar(select(func.count(RunInputBinding.id))) == 0


def test_each_invocation_can_pin_version_a_or_b_without_mutating_the_port(
    db: Session,
) -> None:
    world = _world(db, "ab")
    port = _dataset_port(db, world)

    first = _invoke(
        db,
        world,
        request_id="request-a",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
                signature=world.version_a.content_hash,
            ),
        ),
    )
    second = _invoke(
        db,
        world,
        request_id="request-b",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_b.id,
                signature=world.version_b.content_hash,
            ),
        ),
    )
    unsigned = _invoke(
        db,
        world,
        request_id="request-server-signature",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
            ),
        ),
    )

    assert first.context.get("records").version_id == world.version_a.id
    assert second.context.get("records").version_id == world.version_b.id
    assert unsigned.context.get("records").signature == world.version_a.content_hash
    assert first.context.fingerprint != second.context.fingerprint
    assert first.bindings[0].source_dataset_version_id == world.version_a.id
    assert second.bindings[0].source_dataset_version_id == world.version_b.id
    assert port.dataset_id == world.dataset.id
    assert port.dataset_schema_id == world.schema.id


def test_many_port_resolves_and_audits_each_immutable_input(db: Session) -> None:
    world = _world(db, "many")
    _dataset_port(db, world, cardinality="many")

    result = _invoke(
        db,
        world,
        request_id="many-inputs",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
                signature=world.version_a.content_hash,
            ),
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_b.id,
                signature=world.version_b.content_hash,
            ),
        ),
    )

    assert [item.version_id for item in result.context.get_all("records")] == [
        world.version_a.id,
        world.version_b.id,
    ]
    assert [item.ordinal for item in result.bindings] == [0, 1]
    assert [item.source_dataset_version_id for item in result.bindings] == [
        world.version_a.id,
        world.version_b.id,
    ]


def test_content_contract_accepts_renamed_dataset_with_matching_structure(
    db: Session,
) -> None:
    world = _world(db, "content-match")
    port = _dataset_port(db, world)
    port.schema_document = {
        input_contract_validator.CONTENT_CONTRACT_KEY: {
            "version": input_contract_validator.CONTENT_CONTRACT_VERSION,
            "relations": [
                {
                    "fields": [
                        {
                            "name": "record_id",
                            "logical_types": ["string"],
                            "required": True,
                        },
                        {
                            "name": "amount",
                            "logical_types": ["number"],
                            "required": True,
                        },
                    ],
                    "minimum_data_rows": 1,
                    "allow_additional_fields": True,
                }
            ],
            "allow_additional_relations": True,
        }
    }
    renamed_dataset = LogicalDataset(
        id="renamed-content-dataset",
        tenant_id=world.tenant.id,
        key="renamed-content",
        name="Renamed content",
    )
    renamed_schema = DatasetSchema(
        id="renamed-content-schema",
        tenant_id=world.tenant.id,
        dataset_id=renamed_dataset.id,
        schema_version=1,
        schema_hash="e" * 64,
        compatibility="none",
        schema_document={},
    )
    renamed_relation = DatasetRelation(
        id="renamed-content-relation",
        tenant_id=world.tenant.id,
        dataset_id=renamed_dataset.id,
        schema_id=renamed_schema.id,
        relation_key="unrelated_uploaded_name",
        display_name="Completely renamed table",
        kind="table",
        ordinal=0,
    )
    fields = [
        DatasetField(
            id=f"renamed-content-field-{index}",
            tenant_id=world.tenant.id,
            dataset_id=renamed_dataset.id,
            schema_id=renamed_schema.id,
            dataset_relation_id=renamed_relation.id,
            field_key=name,
            source_name=name,
            logical_type=logical_type,
            ordinal=index,
        )
        for index, (name, logical_type) in enumerate(
            (("record_id", "string"), ("amount", "integer"))
        )
    ]
    renamed_version = DatasetVersion(
        id="renamed-content-version",
        tenant_id=world.tenant.id,
        dataset_id=renamed_dataset.id,
        schema_id=renamed_schema.id,
        version_number=1,
        status="ready",
        content_hash="9" * 64,
        record_count=2,
        manifest={
            "relations": {
                "unrelated_uploaded_name": {"row_count": 2},
            }
        },
    )
    db.add(renamed_dataset)
    db.flush()
    db.add(renamed_schema)
    db.flush()
    db.add(renamed_relation)
    db.flush()
    db.add_all([*fields, renamed_version])
    db.flush()

    result = _invoke(
        db,
        world,
        request_id="renamed-content",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=renamed_version.id,
            ),
        ),
    )

    assert result.context.get("records").version_id == renamed_version.id


def test_many_port_accepts_repeated_single_relation_dataset_bundle(
    db: Session,
) -> None:
    world = _world(db, "many-bundle")
    port = _dataset_port(db, world, cardinality="many")
    port.schema_document = {
        input_contract_validator.CONTENT_CONTRACT_KEY: {
            "version": input_contract_validator.CONTENT_CONTRACT_VERSION,
            "relations": [
                {
                    "fields": [
                        {
                            "name": "record_id",
                            "logical_types": ["string"],
                            "required": True,
                        }
                    ],
                    "minimum_data_rows": 1,
                    "allow_additional_fields": True,
                }
            ],
            "allow_additional_relations": True,
        }
    }
    relations: list[DatasetRelation] = []
    for ordinal in range(2):
        relation = DatasetRelation(
            id=f"many-bundle-relation-{ordinal}",
            tenant_id=world.tenant.id,
            dataset_id=world.dataset.id,
            schema_id=world.schema.id,
            relation_key=f"renamed_{ordinal}",
            display_name=f"Renamed {ordinal}",
            kind="table",
            ordinal=ordinal,
        )
        db.add(relation)
        db.flush()
        db.add(
            DatasetField(
                id=f"many-bundle-field-{ordinal}",
                tenant_id=world.tenant.id,
                dataset_id=world.dataset.id,
                schema_id=world.schema.id,
                dataset_relation_id=relation.id,
                field_key="record_id",
                source_name="record_id",
                logical_type="string",
                ordinal=0,
            )
        )
        relations.append(relation)
    world.version_a.record_count = 4
    world.version_a.manifest = {
        "relations": {
            relation.relation_key: {"row_count": 2}
            for relation in relations
        }
    }
    db.flush()

    result = _invoke(
        db,
        world,
        request_id="many-relation-bundle",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
            ),
        ),
    )

    assert result.context.get("records").version_id == world.version_a.id

    port.cardinality = "one"
    db.flush()
    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="one-relation-bundle",
            request_overrides=(
                BindingOverride(
                    port_key="records",
                    binding_kind="dataset_version",
                    reference_id=world.version_a.id,
                ),
            ),
        )
    assert captured.value.code == "content_contract_ambiguous"


def test_released_port_contract_does_not_drift_with_live_port_edits(
    db: Session,
) -> None:
    world = _world(db, "frozen-port")
    live_port = _dataset_port(db, world)
    frozen_port = SimpleNamespace(
        id=live_port.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="records",
        name="Frozen records",
        description="Released contract",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        schema_document={"type": "array"},
        dataset_schema_hash=world.schema.schema_hash,
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        config={},
    )
    definition = SimpleNamespace(
        source="release",
        capability_ports={"records": frozen_port},
    )
    deployment = ResolvedDeployment(
        scenario_id=world.scenario.id,
        tenant_id=world.tenant.id,
        definition_hash="d" * 64,
        definition=definition,
        definition_source="release",
    )
    actor = Actor(
        actor_type="service",
        principal_id=f"principal-{world.tenant.id}",
        tenant_id=world.tenant.id,
    )
    request = Request(
        capability=CapabilityRef(
            kind="function",
            resource_id=f"capability-{world.scenario.id}",
        ),
        correlation_id="frozen-port-request",
        binding_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
            ),
        ),
    )

    live_port.port_key = "edited-live-key"
    live_port.name = "Edited after release"
    live_port.status = "retired"
    live_port.binding_policy = "none"
    db.flush()

    result = resolve_runtime_inputs(
        db,
        request=request,
        deployment=deployment,
        actor=actor,
        request_id="frozen-port-request",
    )

    assert result.context.get("records").version_id == world.version_a.id
    assert result.bindings[0].capability_port_id == live_port.id
    assert result.bindings[0].content_hash == world.version_a.content_hash


def test_cross_tenant_reference_and_unmanaged_payload_are_rejected(
    db: Session,
    caplog,
) -> None:
    owner = _world(db, "owner")
    foreign = _world(db, "foreign")
    _dataset_port(db, owner)

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            owner,
            request_id="cross-tenant",
            request_overrides=(
                BindingOverride(
                    port_key="records",
                    binding_kind="dataset_version",
                    reference_id=foreign.version_a.id,
                    signature=foreign.version_a.content_hash,
                ),
            ),
        )
    assert captured.value.code == "managed_reference_scope_mismatch"
    leaked = "\n".join(
        [
            str(captured.value),
            str(captured.value.as_dict()),
            caplog.text,
            *[
                str(item.request_document)
                for item in db.scalars(select(CapabilityInvocation)).all()
            ],
        ]
    )
    assert foreign.version_a.id not in leaked

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            owner,
            request_id="raw-payload",
            overrides={
                "records": {
                    "dataset_version_id": owner.version_a.id,
                    "sql": "select something",
                }
            },
        )
    assert captured.value.code == "invalid_override_shape"
    assert captured.value.as_dict()["details"]["fields"] == ("sql",)
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


def test_dataset_head_is_frozen_to_ready_version_at_invocation_start(
    db: Session,
) -> None:
    world = _world(db, "head")
    _dataset_port(db, world)
    head = DatasetHead(
        id="head-runtime",
        tenant_id=world.tenant.id,
        dataset_id=world.dataset.id,
        dataset_version_id=world.version_a.id,
    )
    db.add(head)
    db.flush()

    first = _invoke(
        db,
        world,
        request_id="head-a",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_head",
                reference_id=head.id,
                signature=world.version_a.content_hash,
            ),
        ),
    )
    head.dataset_version_id = world.version_b.id
    db.flush()
    second = _invoke(
        db,
        world,
        request_id="head-b",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_head",
                reference_id=head.id,
                signature=world.version_b.content_hash,
            ),
        ),
    )

    assert first.bindings[0].dataset_head_id == head.id
    assert first.bindings[0].resolved_dataset_version_id == world.version_a.id
    assert first.context.get("records").version_id == world.version_a.id
    assert second.bindings[0].resolved_dataset_version_id == world.version_b.id
    assert second.context.get("records").version_id == world.version_b.id
    assert first.bindings[0].binding_document["head_frozen_at_invocation"] is True


def test_modeling_scenario_dataset_is_never_used_as_runtime_input(
    db: Session,
) -> None:
    world = _world(db, "rules-head")
    _dataset_port(
        db,
        world,
        key="policy_rules",
        role="rules",
        binding_policy="scenario_default",
    )
    head = DatasetHead(
        id="head-runtime-rules",
        tenant_id=world.tenant.id,
        dataset_id=world.dataset.id,
        dataset_version_id=world.version_a.id,
    )
    db.add(head)
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(db, world, request_id="rules-head-without-runtime-input")

    assert captured.value.code == "required_runtime_inputs_missing"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


def test_scenario_level_defaults_are_ignored_and_explicit_runtime_input_is_allowed(
    db: Session,
) -> None:
    world = _world(db, "default")
    _dataset_port(db, world, binding_policy="scenario_default")
    connector = ConnectorBinding(
        id="connector-same-key",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        binding_key="records",
        connector_kind="data_source",
        connector_id="opaque-target",
        health_status="healthy",
        connector_signature="3" * 64,
    )
    db.add(connector)
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(db, world, request_id="scenario-default-is-not-runtime-data")
    assert captured.value.code == "required_runtime_inputs_missing"

    result = _invoke(
        db,
        world,
        request_id="explicit-runtime-input",
        request_overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=world.version_a.id,
                signature=world.version_a.content_hash,
            ),
        ),
    )
    assert result.context.get("records").binding_kind == "dataset_version"
    assert result.context.get("records").version_id == world.version_a.id
    assert result.bindings[0].binding_document["resolution_source"] == "invocation_override"


def test_scenario_connector_is_not_implicit_but_explicit_agent_input_is_checked(
    db: Session,
) -> None:
    world = _world(db, "connector")
    port = ScenarioCapabilityPort(
        id="port-connector",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="live_reference",
        name="Live reference",
        direction="input",
        role="reference",
        media_kind="connector",
        is_required=True,
        cardinality="one",
        binding_policy="scenario_default",
        status="active",
    )
    connector = ConnectorBinding(
        id="connector-default",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        binding_key="live_reference",
        connector_kind="data_source",
        connector_id="opaque-target",
        health_status="healthy",
        connector_signature="4" * 64,
    )
    db.add_all([port, connector])
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(db, world, request_id="connector-is-not-an-implicit-default")
    assert captured.value.code == "required_runtime_inputs_missing"

    override = BindingOverride(
        port_key="live_reference",
        binding_kind="connector_binding",
        binding_key="live_reference",
    )
    result = _invoke(
        db,
        world,
        request_id="explicit-agent-connector",
        request_overrides=(override,),
    )

    handle = result.context.get("live_reference")
    assert handle.binding_kind == "connector_binding"
    assert handle.reference_id == connector.id
    assert result.bindings[0].inline_document is None
    assert result.bindings[0].connector_binding_id == connector.id
    assert result.bindings[0].content_hash == "4" * 64

    connector.health_status = "unhealthy"
    db.flush()
    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="connector-unhealthy",
            request_overrides=(override,),
        )
    assert captured.value.code == "managed_reference_not_ready"


def test_typed_connector_binding_key_is_resolved_server_side(db: Session) -> None:
    world = _world(db, "connector-key")
    port = ScenarioCapabilityPort(
        id="port-connector-key",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="live_reference",
        name="Live reference",
        direction="input",
        role="reference",
        media_kind="connector",
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
    )
    connector = ConnectorBinding(
        id="connector-by-key",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        binding_key="current-system",
        connector_kind="data_source",
        connector_id="opaque-target",
        health_status="healthy",
        connector_signature="6" * 64,
    )
    db.add_all([port, connector])
    db.flush()

    result = _invoke(
        db,
        world,
        request_id="connector-key-request",
        request_overrides=(
            BindingOverride(
                port_key="live_reference",
                binding_kind="connector_binding",
                binding_key="current-system",
            ),
        ),
    )

    handle = result.context.get("live_reference")
    assert handle.reference_id == connector.id
    assert handle.signature == connector.connector_signature
    assert result.bindings[0].connector_binding_id == connector.id


def test_connector_content_contract_validates_checked_structure_profile(db: Session) -> None:
    world = _world(db, "connector-contract")
    schema_document = {
        input_contract_validator.CONTENT_CONTRACT_KEY: {
            "version": input_contract_validator.CONTENT_CONTRACT_VERSION,
            "relations": [
                {
                    "fields": [
                        {
                            "name": "record_id",
                            "logical_types": ["integer"],
                            "required": True,
                        }
                    ],
                    "minimum_data_rows": 0,
                    "allow_additional_fields": True,
                }
            ],
            "allow_additional_relations": True,
        }
    }
    port = ScenarioCapabilityPort(
        id="port-connector-contract",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="live_records",
        name="Live records",
        direction="input",
        role="invocation_input",
        media_kind="connector",
        schema_document=schema_document,
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
    )
    profile = {
        "category": "table",
        "tables": [
            {
                "name": "renamed_table",
                "record_count": 3,
                "columns": [
                    {"name": "record_id", "logical_type": "integer"},
                    {"name": "note", "logical_type": "string"},
                ],
            }
        ],
    }
    connector = ConnectorBinding(
        id="connector-contract",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        binding_key="live-records",
        connector_kind="data_source",
        connector_id="opaque-target",
        health_status="healthy",
        connector_signature="7" * 64,
        structure_profile=profile,
        structure_fingerprint=input_contract_validator.structural_fingerprint(profile),
    )
    db.add_all([port, connector])
    db.flush()

    result = _invoke(
        db,
        world,
        request_id="connector-contract-valid",
        request_overrides=(
            BindingOverride(
                port_key="live_records",
                binding_kind="connector_binding",
                binding_key="live-records",
            ),
        ),
    )

    handle = result.context.get("live_records")
    assert handle.signature != connector.connector_signature
    assert len(handle.signature) == 64

    repeated_profile = {
        "category": "table",
        "tables": [
            profile["tables"][0],
            {**profile["tables"][0], "name": "another_renamed_table"},
        ],
    }
    port.cardinality = "many"
    connector.structure_profile = repeated_profile
    connector.structure_fingerprint = input_contract_validator.structural_fingerprint(
        repeated_profile
    )
    db.flush()

    repeated = _invoke(
        db,
        world,
        request_id="connector-contract-many-relations",
        request_overrides=(
            BindingOverride(
                port_key="live_records",
                binding_kind="connector_binding",
                binding_key="live-records",
            ),
        ),
    )
    assert len(repeated.context.get("live_records").signature) == 64

    port.cardinality = "one"
    db.flush()
    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="connector-contract-one-relation",
            request_overrides=(
                BindingOverride(
                    port_key="live_records",
                    binding_kind="connector_binding",
                    binding_key="live-records",
                ),
            ),
        )
    assert captured.value.code == "content_contract_ambiguous"

    incompatible_profile = {
        "category": "table",
        "tables": [
            {
                "name": "another_name",
                "record_count": 3,
                "columns": [{"name": "other_id", "logical_type": "integer"}],
            }
        ],
    }
    connector.structure_profile = incompatible_profile
    connector.structure_fingerprint = input_contract_validator.structural_fingerprint(
        incompatible_profile
    )
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="connector-contract-invalid",
            request_overrides=(
                BindingOverride(
                    port_key="live_records",
                    binding_kind="connector_binding",
                    binding_key="live-records",
                ),
            ),
        )

    assert captured.value.code == "content_contract_missing"


def test_connector_structure_profile_is_bounded_and_type_normalized(monkeypatch) -> None:
    monkeypatch.setattr(
        connector_service.datasource_service,
        "list_tables",
        lambda _connector: [
            {
                "name": "runtime_records",
                "row_count": 2,
                "columns": [
                    {"name": "record_id", "type": "BIGINT"},
                    {"name": "observed_at", "type": "TIMESTAMP WITH TIME ZONE"},
                    {"name": "amount", "type": "NUMERIC(12, 2)"},
                    {"name": "payload", "type": "JSONB"},
                ],
            }
        ],
    )

    profile = connector_service.connector_structure_profile(
        SimpleNamespace(type="postgres")
    )

    assert [
        column["logical_type"] for column in profile["tables"][0]["columns"]
    ] == ["integer", "datetime", "number", "object"]
    assert profile["tables"][0]["record_count"] == 2


def test_expected_signature_is_server_checked(db: Session) -> None:
    world = _world(db, "signature")
    _dataset_port(db, world)

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="stale-signature",
            request_overrides=(
                BindingOverride(
                    port_key="records",
                    binding_kind="dataset_version",
                    reference_id=world.version_a.id,
                    signature="9" * 64,
                ),
            ),
        )

    assert captured.value.code == "managed_reference_changed"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


def test_expired_temporary_attachment_is_rejected_before_audit(db: Session) -> None:
    world = _world(db, "expired-attachment")
    port = ScenarioCapabilityPort(
        id="p-expired-attachment",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="document",
        name="Document",
        direction="input",
        role="invocation_input",
        media_kind="artifact",
        schema_document={"type": "object"},
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
    )
    source = DataSource(
        id="source-expired-attachment",
        tenant_id=world.tenant.id,
        name="Managed storage",
        type="file_bucket",
        config={},
    )
    bucket_file = BucketFile(
        id="file-expired-attachment",
        data_source_id=source.id,
        filename="input.txt",
        stored_path="minio://managed/input.txt",
        size=5,
        content_sha256="5" * 64,
        status="parsed",
    )
    asset = DataAsset(
        id="asset-expired-attachment",
        tenant_id=world.tenant.id,
        key="attachment.expired",
        name="Expired attachment",
        kind="file",
        lifecycle_status="active",
        labels={"temporary": True},
    )
    version = DataAssetVersion(
        id="version-expired-attachment",
        tenant_id=world.tenant.id,
        asset_id=asset.id,
        version_number=1,
        bucket_file_id=bucket_file.id,
        bucket_data_source_id=source.id,
        provenance_kind="upload",
        status="ready",
        content_sha256="5" * 64,
        byte_size=5,
        source_locator={},
        version_document={
            "lifecycle": {
                "purpose": "invocation_attachment",
                "temporary": True,
                "expires_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    )
    db.add_all([port, source])
    db.flush()
    db.add(bucket_file)
    db.flush()
    db.add(asset)
    db.flush()
    db.add(version)
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="expired-attachment",
            overrides=[
                {
                    "port_key": "document",
                    "asset_version_id": version.id,
                }
            ],
        )

    assert captured.value.code == "managed_reference_expired"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


def test_modeling_material_dataset_is_rejected_as_runtime_input(db: Session) -> None:
    world = _world(db, "modeling-plane")
    world.dataset.usage_plane = "modeling_material"
    _dataset_port(db, world)
    db.flush()

    with pytest.raises(RuntimeInputResolutionError) as captured:
        _invoke(
            db,
            world,
            request_id="modeling-plane-runtime",
            overrides=[
                {
                    "port_key": "records",
                    "dataset_version_id": world.version_a.id,
                }
            ],
        )

    assert captured.value.code == "managed_reference_not_runtime_input"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0
