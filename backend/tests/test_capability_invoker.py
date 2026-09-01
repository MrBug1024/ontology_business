from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    BusinessScenario,
    CapabilityInvocation,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    RunInputBinding,
    ScenarioCapabilityPort,
    Tenant,
)
from app.services.capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityContractError,
    CapabilityRef,
    Request,
    ResolvedDeployment,
)
from app.services import capability_invoker as capability_invoker_module
from app.services.capability_invoker import (
    CapabilityInvocationError,
    CapabilityInvoker,
    resolve_capability_contract,
)
from app.services.capability_provider_keys import BUILTIN_PROVIDER_KEYS
from app.services.capability_registry import CapabilityProviderRegistry


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
    deployment: ResolvedDeployment


class RecordingProvider:
    provider_key = "recording-provider"
    provider_version = "1.0.0"

    def __init__(self, contract: dict, *, result=None, failure: Exception | None = None):
        self.contract_document = contract
        self.result = {"ok": True} if result is None else result
        self.failure = failure
        self.calls: list[dict] = []
        self.preview_calls: list[dict] = []

    def contract(self, capability, deployment):
        return self.contract_document

    def invoke(self, request, actor, deployment, data_context):
        self.calls.append(
            {
                "mode": request.mode,
                "tenant_id": actor.tenant_id,
                "definition_hash": deployment.definition_hash,
                "data_context": data_context,
            }
        )
        if self.failure is not None:
            raise self.failure
        return self._result(request, data_context)

    def preview(self, request, actor, deployment, data_context):
        self.preview_calls.append(
            {
                "mode": request.mode,
                "tenant_id": actor.tenant_id,
                "definition_hash": deployment.definition_hash,
                "data_context": data_context,
            }
        )
        return self._result(request, data_context)

    def _result(self, request, data_context):
        if callable(self.result):
            return self.result(request, data_context)
        return self.result


def _world(db: Session, key: str) -> World:
    tenant = Tenant(id=f"t-{key}", name=f"Tenant {key}")
    scenario = BusinessScenario(
        id=f"s-{key}",
        tenant_id=tenant.id,
        name=f"Scenario {key}",
        status="active",
    )
    db.add(tenant)
    db.flush()
    db.add(scenario)
    db.flush()
    deployment = ResolvedDeployment(
        scenario_id=scenario.id,
        tenant_id=tenant.id,
        environment="dev",
        definition_hash="d" * 64,
        definition=SimpleNamespace(
            functions={
                f"capability-{scenario.id}": {
                    "id": f"capability-{scenario.id}",
                    "runtime_kind": "provider",
                    "runtime_config": {
                        "provider_key": "recording-provider",
                        "provider_version": "1.0.0",
                    },
                }
            }
        ),
    )
    return World(tenant, scenario, deployment)


def _registry(provider: RecordingProvider) -> CapabilityProviderRegistry:
    registry = CapabilityProviderRegistry()
    registry.register_instance(provider)
    registry.seal()
    return registry


def _actor(world: World, *, scopes=(), roles=(), tenant_id: str | None = None) -> Actor:
    return Actor(
        actor_type="service",
        principal_id=f"principal-{world.tenant.id}",
        tenant_id=tenant_id or world.tenant.id,
        scopes=tuple(scopes),
        roles=tuple(roles),
    )


def _request(
    world: World,
    *,
    correlation_id: str | None,
    inputs: dict | None = None,
    mode: str = "execute",
    idempotency_key: str | None = None,
    overrides: tuple[BindingOverride, ...] = (),
    confirmation: dict | None = None,
    request_id: str | None = None,
) -> Request:
    return Request(
        capability=CapabilityRef(
            kind="function",
            resource_id=f"capability-{world.scenario.id}",
            provider_key="recording-provider",
        ),
        inputs=inputs or {},
        binding_overrides=overrides,
        confirmation=confirmation or {},
        request_id=request_id,
        mode=mode,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        expected_definition_hash=world.deployment.definition_hash,
        expected_deployment_fingerprint=world.deployment.fingerprint,
    )


def _object_contract(**overrides) -> dict:
    result = {
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "required_roles": [],
        "required_scopes": [],
        "side_effect": False,
        "requires_confirmation": False,
        "idempotency_required": False,
    }
    result.update(overrides)
    return result


def _invoke(
    db: Session,
    world: World,
    provider: RecordingProvider,
    request: Request,
    *,
    actor: Actor | None = None,
):
    return CapabilityInvoker(_registry(provider)).invoke(
        db,
        world.deployment,
        actor or _actor(world),
        request,
        invocation_source="rest",
    )


def _dataset(db: Session, world: World):
    dataset = LogicalDataset(
        id=f"dataset-{world.scenario.id}",
        tenant_id=world.tenant.id,
        key=f"dataset-{world.scenario.id}",
        name="Runtime dataset",
    )
    schema = DatasetSchema(
        id=f"schema-{world.scenario.id}",
        tenant_id=world.tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash="a" * 64,
        compatibility="none",
    )
    version = DatasetVersion(
        id=f"version-{world.scenario.id}",
        tenant_id=world.tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        content_hash="b" * 64,
    )
    port = ScenarioCapabilityPort(
        id=f"port-{world.scenario.id}",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key=f"capability-{world.scenario.id}",
        port_key="records",
        name="Records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=dataset.id,
        dataset_schema_id=schema.id,
        schema_document={"type": "array"},
        is_required=True,
        binding_policy="per_invocation",
        status="active",
    )
    db.add(dataset)
    db.flush()
    db.add(schema)
    db.flush()
    db.add_all([version, port])
    db.flush()
    return version, port


def test_zero_data_provider_audits_only_input_outline_hash_and_provenance(
    db: Session,
) -> None:
    world = _world(db, "zero-invoker")
    contract = _object_contract(
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["instruction", "private_note"],
            "properties": {
                "instruction": {"type": "string"},
                "private_note": {"type": "string"},
            },
        }
    )
    provider = RecordingProvider(
        contract,
        result={
            "accepted": True,
            "access_token": "provider-private-value",
            "authorization": "provider-private-value",
            "cookie": "provider-private-value",
            "database_url": "provider-private-value",
            "dsn": "provider-private-value",
            "token": "provider-private-value",
        },
    )
    request = _request(
        world,
        correlation_id="zero-invocation",
        inputs={
            "instruction": "prepare the result",
            "private_note": "raw-private-value",
        },
    )

    receipt = _invoke(db, world, provider, request)
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)

    assert receipt.status == "succeeded"
    assert receipt.output == {
        "access_token": "[redacted]",
        "accepted": True,
        "authorization": "[redacted]",
        "cookie": "[redacted]",
        "database_url": "[redacted]",
        "dsn": "[redacted]",
        "token": "[redacted]",
    }
    assert receipt.definition_hash == world.deployment.definition_hash
    assert receipt.deployment_fingerprint == world.deployment.fingerprint
    assert receipt.data_context_fingerprint == invocation.data_context_fingerprint
    assert receipt.audit_ref["replayed"] is False
    assert invocation.status == "succeeded"
    assert invocation.started_at is not None
    assert invocation.completed_at is not None
    assert invocation.capability_key == request.capability.resource_id
    assert invocation.principal_id == f"principal-{world.tenant.id}"
    serialized_audit = str(invocation.request_document)
    assert "raw-private-value" not in serialized_audit
    assert "prepare the result" not in serialized_audit
    assert invocation.request_document["structured_inputs"]["hash"]
    assert set(
        invocation.request_document["structured_inputs"]["outline"]["fields"]
    ) == {"instruction", "private_note"}
    assert len(provider.calls) == 1
    assert provider.calls[0]["data_context"].handles == ()


def test_required_dataset_is_fixed_and_included_in_final_input_hash(db: Session) -> None:
    world = _world(db, "dataset-invoker")
    version, port = _dataset(db, world)
    unrelated_port = ScenarioCapabilityPort(
        id=f"unrelated-{port.id}",
        tenant_id=world.tenant.id,
        scenario_id=world.scenario.id,
        capability_kind="function",
        capability_key="another-capability",
        port_key=port.port_key,
        name="Another capability records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=port.dataset_id,
        dataset_schema_id=port.dataset_schema_id,
        schema_document={"type": "array"},
        is_required=True,
        binding_policy="per_invocation",
        status="active",
    )
    db.add(unrelated_port)
    db.flush()
    provider = RecordingProvider(
        _object_contract(required_scopes=["capability:invoke"]),
        result=lambda _request, context: {
            "version": context.get("records").version_id,
        },
    )
    request = _request(
        world,
        correlation_id="dataset-invocation",
        idempotency_key="dataset-key",
        overrides=(
            BindingOverride(
                port_key="records",
                binding_kind="dataset_version",
                reference_id=version.id,
                signature=version.content_hash,
            ),
        ),
    )

    receipt = _invoke(
        db,
        world,
        provider,
        request,
        actor=_actor(world, scopes=("capability:invoke",)),
    )
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)
    binding = db.scalar(
        select(RunInputBinding).where(
            RunInputBinding.invocation_id == receipt.invocation_id
        )
    )

    assert receipt.status == "succeeded"
    assert receipt.output["version"] == version.id
    assert binding.resolved_dataset_version_id == version.id
    assert binding.capability_port_id == port.id
    assert invocation.input_hash
    assert invocation.input_hash != invocation.request_document["structured_inputs"]["hash"]
    assert invocation.data_context_fingerprint == receipt.data_context_fingerprint
    assert provider.calls[0]["data_context"].get("records").signature == version.content_hash
    assert len(provider.calls[0]["data_context"].handles) == 1


def test_tenant_and_scope_are_rejected_before_invocation_audit(db: Session) -> None:
    world = _world(db, "scope-invoker")
    provider = RecordingProvider(
        _object_contract(required_scopes=["capability:invoke"])
    )
    request = _request(world, correlation_id="scope-invocation")

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, request, actor=_actor(world))
    assert captured.value.code == "capability_scope_forbidden"

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            request,
            actor=_actor(
                world,
                scopes=("capability:invoke",),
                tenant_id="another-tenant",
            ),
        )
    assert captured.value.code == "principal_scope_mismatch"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0
    assert provider.calls == []


def test_draft_2020_schema_error_is_structured_and_does_not_echo_value(
    db: Session,
) -> None:
    world = _world(db, "schema-invoker")
    provider = RecordingProvider(
        _object_contract(
            input_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            }
        )
    )
    request = _request(
        world,
        correlation_id="schema-invocation",
        inputs={"count": "private-invalid-value"},
    )

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, request)

    error = captured.value
    assert error.code == "input_schema_invalid"
    assert error.as_dict()["details"]["path"] == ("count",)
    assert "private-invalid-value" not in str(error.as_dict())
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0
    assert provider.calls == []


def test_published_output_schema_is_enforced_without_persisting_invalid_values(
    db: Session,
) -> None:
    world = _world(db, "output-schema-invoker")
    resource = world.deployment.definition.functions[
        f"capability-{world.scenario.id}"
    ]
    resource["output_schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["accepted"],
        "properties": {"accepted": {"type": "boolean"}},
        "additionalProperties": False,
    }
    marker = "RAW-INVALID-PROVIDER-OUTPUT"
    invalid_provider = RecordingProvider(
        _object_contract(),
        result={"accepted": marker, "physical_table": marker},
    )

    failed = _invoke(
        db,
        world,
        invalid_provider,
        _request(world, correlation_id="invalid-output-schema"),
    )
    invocation = db.get(CapabilityInvocation, failed.invocation_id)

    assert failed.status == "failed"
    assert failed.error_code == "output_schema_invalid"
    assert failed.output == {}
    assert invocation is not None
    assert marker not in str(invocation.result_document)
    assert "physical_table" not in str(invocation.result_document)

    valid_provider = RecordingProvider(
        _object_contract(),
        result={"accepted": True},
    )
    succeeded = _invoke(
        db,
        world,
        valid_provider,
        _request(world, correlation_id="valid-output-schema"),
    )
    assert succeeded.status == "succeeded"
    assert succeeded.output == {"accepted": True}


def test_input_size_and_user_field_names_are_safe_for_errors_and_audit(
    db: Session,
) -> None:
    world = _world(db, "input-safety-invoker")
    long_field = "field-" + ("x" * 200)
    provider = RecordingProvider(
        _object_contract(
            input_schema={
                "type": "object",
                "additionalProperties": True,
            }
        )
    )
    receipt = _invoke(
        db,
        world,
        provider,
        _request(
            world,
            correlation_id="safe-outline",
            inputs={
                "authorization": "Bearer private-value",
                long_field: "private-value",
            },
        ),
    )
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)
    serialized_audit = str(invocation.request_document)

    assert "authorization" not in serialized_audit
    assert long_field not in serialized_audit
    assert "Bearer private-value" not in serialized_audit
    assert "private-value" not in serialized_audit

    schema_provider = RecordingProvider(
        _object_contract(
            input_schema={
                "type": "object",
                "properties": {"authorization": {"type": "integer"}},
                "additionalProperties": False,
            }
        )
    )
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            schema_provider,
            _request(
                world,
                correlation_id="safe-schema-path",
                inputs={"authorization": "Bearer private-invalid-value"},
            ),
        )
    assert captured.value.code == "input_schema_invalid"
    assert "authorization" not in str(captured.value.as_dict())
    assert "private-invalid-value" not in str(captured.value.as_dict())

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            _request(
                world,
                correlation_id="oversized-structured-input",
                inputs={"payload": "x" * 1_000_001},
            ),
        )
    assert captured.value.code == "structured_input_too_large"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1


def test_unregistered_provider_is_rejected_without_database_audit(db: Session) -> None:
    world = _world(db, "registry-invoker")
    request = _request(world, correlation_id="unregistered-invocation")
    registry = CapabilityProviderRegistry()

    with pytest.raises(CapabilityInvocationError) as captured:
        CapabilityInvoker(registry).invoke(
            db,
            world.deployment,
            _actor(world),
            request,
            invocation_source="mcp",
        )

    assert captured.value.code == "provider_not_registered"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


@pytest.mark.parametrize(
    "registered_versions",
    [("1.0.0",), ("1.0.0", "2.0.0")],
)
def test_explicit_provider_runtime_without_version_fails_closed_before_resolution(
    db: Session,
    registered_versions: tuple[str, ...],
) -> None:
    world = _world(db, f"missing-provider-version-{len(registered_versions)}")
    resource = world.deployment.definition.functions[
        f"capability-{world.scenario.id}"
    ]
    resource["runtime_config"].pop("provider_version")
    request = _request(world, correlation_id="missing-provider-version")
    registry = CapabilityProviderRegistry()
    for version in registered_versions:
        provider = RecordingProvider(_object_contract())
        provider.provider_version = version
        registry.register_instance(provider)
    registry.seal()

    with pytest.raises(CapabilityInvocationError) as discovery_error:
        resolve_capability_contract(
            db,
            world.deployment,
            request.capability,
            registry=registry,
        )
    with pytest.raises(CapabilityInvocationError) as invocation_error:
        CapabilityInvoker(registry).invoke(
            db,
            world.deployment,
            _actor(world),
            request,
            invocation_source="rest",
        )

    assert discovery_error.value.code == "provider_version_missing"
    assert invocation_error.value.code == "provider_version_missing"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0


def test_explicit_provider_runtime_selects_exact_version_from_multi_version_registry(
    db: Session,
) -> None:
    world = _world(db, "exact-provider-version")
    resource = world.deployment.definition.functions[
        f"capability-{world.scenario.id}"
    ]
    resource["runtime_config"]["provider_version"] = "2.0.0"
    first = RecordingProvider(_object_contract(), result={"version": "1.0.0"})
    second = RecordingProvider(_object_contract(), result={"version": "2.0.0"})
    second.provider_version = "2.0.0"
    registry = CapabilityProviderRegistry()
    registry.register_instance(first)
    registry.register_instance(second)
    registry.seal()
    request = _request(world, correlation_id="exact-provider-version")

    contract = resolve_capability_contract(
        db,
        world.deployment,
        request.capability,
        registry=registry,
    )
    receipt = CapabilityInvoker(registry).invoke(
        db,
        world.deployment,
        _actor(world),
        request,
        invocation_source="rest",
    )

    assert contract["side_effect"] is False
    assert receipt.output == {"version": "2.0.0"}
    assert first.calls == []
    assert len(second.calls) == 1


def test_static_builtin_provider_binding_keeps_legacy_versionless_discovery(
    db: Session,
) -> None:
    world = _world(db, "builtin-version-compatibility")
    resource = world.deployment.definition.functions[
        f"capability-{world.scenario.id}"
    ]
    resource["runtime_kind"] = "weighted_score"
    resource["runtime_config"] = {}
    provider = RecordingProvider(_object_contract())
    provider.provider_key = BUILTIN_PROVIDER_KEYS["function"]
    registry = CapabilityProviderRegistry()
    registry.register_instance(provider)
    registry.seal()
    request = _request(world, correlation_id="builtin-version-compatibility")
    capability = replace(
        request.capability,
        provider_key=BUILTIN_PROVIDER_KEYS["function"],
    )

    contract = resolve_capability_contract(
        db,
        world.deployment,
        capability,
        registry=registry,
    )

    assert contract["side_effect"] is False

    resource["runtime_kind"] = "provider"
    resource["runtime_config"] = {
        "provider_key": BUILTIN_PROVIDER_KEYS["function"],
    }
    with pytest.raises(CapabilityInvocationError) as captured:
        resolve_capability_contract(
            db,
            world.deployment,
            capability,
            registry=registry,
        )
    assert captured.value.code == "provider_version_missing"


def test_request_id_is_independent_from_correlation_and_duplicates_are_safe(
    db: Session,
) -> None:
    world = _world(db, "request-id-invoker")
    provider = RecordingProvider(_object_contract())

    first = _invoke(
        db,
        world,
        provider,
        _request(world, correlation_id="shared-correlation"),
    )
    second = _invoke(
        db,
        world,
        provider,
        _request(world, correlation_id="shared-correlation"),
    )
    first_invocation = db.get(CapabilityInvocation, first.invocation_id)
    second_invocation = db.get(CapabilityInvocation, second.invocation_id)

    assert first.invocation_id != second.invocation_id
    assert first_invocation.request_id != second_invocation.request_id
    assert first_invocation.correlation_id == second_invocation.correlation_id

    explicit_id = "client-request-001"
    explicit = _invoke(
        db,
        world,
        provider,
        _request(
            world,
            correlation_id="explicit-first",
            request_id=explicit_id,
        ),
    )
    assert db.get(CapabilityInvocation, explicit.invocation_id).request_id == explicit_id

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            _request(
                world,
                correlation_id="explicit-second",
                request_id=explicit_id,
            ),
        )
    assert captured.value.code == "request_id_conflict"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 3
    assert len(provider.calls) == 3

    with pytest.raises(CapabilityContractError):
        _request(
            world,
            correlation_id="invalid-request-id",
            request_id="r" * 65,
        )


def test_idempotency_replay_requires_compatible_explicit_request_id(
    db: Session,
) -> None:
    world = _world(db, "request-id-idempotency-invoker")
    provider = RecordingProvider(_object_contract())
    original = _request(
        world,
        correlation_id="request-id-original",
        idempotency_key="request-id-idempotency-key",
        request_id="original-request-id",
    )
    first = _invoke(db, world, provider, original)
    replay = _invoke(
        db,
        world,
        provider,
        _request(
            world,
            correlation_id="request-id-retry",
            idempotency_key="request-id-idempotency-key",
            request_id="original-request-id",
        ),
    )
    assert replay.invocation_id == first.invocation_id

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            _request(
                world,
                correlation_id="request-id-mismatch",
                idempotency_key="request-id-idempotency-key",
                request_id="different-unused-request-id",
            ),
        )
    assert captured.value.code == "idempotency_request_id_conflict"
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("requires_confirmation", "idempotency_required"),
    ((False, True), (True, False)),
)
def test_side_effect_contract_requires_confirmation_and_idempotency(
    db: Session,
    requires_confirmation: bool,
    idempotency_required: bool,
) -> None:
    world = _world(
        db,
        f"invalid-effect-{int(requires_confirmation)}-{int(idempotency_required)}",
    )
    provider = RecordingProvider(
        _object_contract(
            side_effect=True,
            requires_confirmation=requires_confirmation,
            idempotency_required=idempotency_required,
        )
    )
    request = _request(
        world,
        correlation_id="invalid-effect-contract",
        idempotency_key="invalid-effect-key",
        mode="preview",
    )

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, request)

    assert captured.value.code == "provider_contract_invalid"
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 0
    assert provider.preview_calls == []
    assert provider.calls == []


def test_idempotency_reuses_same_receipt_and_rejects_changed_inputs(db: Session) -> None:
    world = _world(db, "idem-invoker")
    provider = RecordingProvider(
        _object_contract(
            input_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            }
        ),
        result={"stable": True},
    )
    first_request = _request(
        world,
        correlation_id="idempotent-first",
        idempotency_key="stable-key",
        inputs={"value": 1},
    )

    first = _invoke(db, world, provider, first_request)
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            _request(
                world,
                correlation_id=None,
                idempotency_key="stable-key",
                inputs={"value": 1},
            ),
        )
    assert captured.value.code == "correlation_id_required"
    replay = _invoke(
        db,
        world,
        provider,
        _request(
            world,
            correlation_id="idempotent-retry",
            idempotency_key="stable-key",
            inputs={"value": 1},
        ),
    )

    assert replay.invocation_id == first.invocation_id
    assert replay.output == first.output
    assert replay.audit_ref["replayed"] is True
    assert len(provider.calls) == 1
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1

    conflicting = _request(
        world,
        correlation_id="idempotent-conflict",
        idempotency_key="stable-key",
        inputs={"value": 2},
    )
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, conflicting)
    assert captured.value.code == "idempotency_conflict"
    assert len(provider.calls) == 1


def test_provider_exception_is_safely_persisted_as_failed_receipt(db: Session) -> None:
    world = _world(db, "failure-invoker")
    provider = RecordingProvider(
        _object_contract(),
        failure=RuntimeError("private credential value must never escape"),
    )
    request = _request(
        world,
        correlation_id="failed-invocation",
        idempotency_key="failed-key",
    )

    receipt = _invoke(db, world, provider, request)
    replay = _invoke(db, world, provider, request)
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)

    assert receipt.status == "failed"
    assert receipt.error_code == "provider_execution_failed"
    assert receipt.error_message == "capability provider execution failed"
    assert "private credential" not in str(receipt)
    assert invocation.status == "failed"
    assert invocation.completed_at is not None
    assert "private credential" not in str(invocation.result_document)
    assert replay.invocation_id == receipt.invocation_id
    assert replay.audit_ref["replayed"] is True
    assert len(provider.calls) == 1


def test_preview_and_confirm_gate_side_effect_and_reuse_fixed_invocation(
    db: Session,
) -> None:
    world = _world(db, "confirm-invoker")

    def result(request, _context):
        return {"phase": request.mode}

    provider = RecordingProvider(
        _object_contract(
            side_effect=True,
            requires_confirmation=True,
            idempotency_required=True,
        ),
        result=result,
    )
    execute = _request(
        world,
        correlation_id="confirmation-invocation",
        idempotency_key="confirmation-key",
        mode="execute",
    )
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, execute)
    assert captured.value.code == "preview_required"
    assert provider.calls == []

    preview_request = _request(
        world,
        correlation_id="confirmation-invocation",
        idempotency_key="confirmation-key",
        mode="preview",
    )
    preview = _invoke(db, world, provider, preview_request)

    assert preview.status == "awaiting_confirmation"
    assert preview.output == {"phase": "preview"}
    assert preview.confirmation["required"] is True
    assert preview.confirmation["expires_at"]
    assert provider.calls == []
    assert [call["mode"] for call in provider.preview_calls] == ["preview"]

    confirm_request = _request(
        world,
        correlation_id="confirmation-invocation",
        idempotency_key="confirmation-key",
        mode="confirm",
        confirmation=dict(preview.confirmation),
    )
    confirmed = _invoke(db, world, provider, confirm_request)
    replay = _invoke(db, world, provider, confirm_request)
    invocation = db.get(CapabilityInvocation, preview.invocation_id)

    assert confirmed.invocation_id == preview.invocation_id
    assert confirmed.status == "succeeded"
    assert confirmed.output == {"phase": "confirm"}
    assert confirmed.confirmation["confirmed"] is True
    assert replay.invocation_id == confirmed.invocation_id
    assert replay.audit_ref["replayed"] is True
    assert [call["mode"] for call in provider.calls] == ["confirm"]
    assert [call["mode"] for call in provider.preview_calls] == ["preview"]
    assert invocation.status == "succeeded"
    assert invocation.completed_at is not None
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1


def test_confirmation_rejects_changed_payload_without_reinvoking_provider(
    db: Session,
) -> None:
    world = _world(db, "confirm-input-invoker")
    provider = RecordingProvider(
        _object_contract(
            input_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            },
            side_effect=True,
            requires_confirmation=True,
            idempotency_required=True,
        )
    )
    preview_request = _request(
        world,
        correlation_id="confirmation-input",
        idempotency_key="confirmation-input-key",
        mode="preview",
        inputs={"value": 1},
    )
    preview = _invoke(db, world, provider, preview_request)
    changed = _request(
        world,
        correlation_id="confirmation-input",
        idempotency_key="confirmation-input-key",
        mode="confirm",
        inputs={"value": 2},
        confirmation=dict(preview.confirmation),
    )

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, changed)

    assert captured.value.code == "confirmation_input_mismatch"
    assert provider.calls == []
    assert len(provider.preview_calls) == 1
    invocation = db.get(CapabilityInvocation, preview.invocation_id)
    assert invocation.status == "awaiting_confirmation"


def test_confirmation_expiry_is_server_bound_and_invalid_token_is_non_mutating(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "confirmation-expiry-invoker")
    provider = RecordingProvider(
        _object_contract(
            side_effect=True,
            requires_confirmation=True,
            idempotency_required=True,
            confirmation_ttl_seconds=30,
        ),
        result=lambda request, _context: {"phase": request.mode},
    )
    issued_at = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(capability_invoker_module, "_now", lambda: issued_at)
    preview_request = _request(
        world,
        correlation_id="confirmation-expiry",
        idempotency_key="confirmation-expiry-key",
        mode="preview",
    )
    preview = _invoke(db, world, provider, preview_request)

    assert preview.status == "awaiting_confirmation"
    assert preview.confirmation["expires_at"] == "2026-08-29T08:00:30Z"
    assert provider.calls == []
    assert len(provider.preview_calls) == 1

    oversized_confirmation = dict(preview.confirmation)
    oversized_confirmation["confirmation_token"] = "a" * 20_000
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            provider,
            _request(
                world,
                correlation_id="confirmation-expiry",
                idempotency_key="confirmation-expiry-key",
                mode="confirm",
                confirmation=oversized_confirmation,
            ),
        )
    assert captured.value.code == "confirmation_too_large"
    assert db.get(CapabilityInvocation, preview.invocation_id).status == (
        "awaiting_confirmation"
    )

    after_expiry = issued_at + timedelta(seconds=31)
    monkeypatch.setattr(capability_invoker_module, "_now", lambda: after_expiry)
    invalid_confirmation = dict(preview.confirmation)
    invalid_confirmation["confirmation_token"] = "0" * 64
    invalid_request = _request(
        world,
        correlation_id="confirmation-expiry",
        idempotency_key="confirmation-expiry-key",
        mode="confirm",
        confirmation=invalid_confirmation,
    )
    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(db, world, provider, invalid_request)
    assert captured.value.code == "confirmation_token_mismatch"
    assert db.get(CapabilityInvocation, preview.invocation_id).status == (
        "awaiting_confirmation"
    )

    expired_preview_replay = _invoke(db, world, provider, preview_request)
    assert expired_preview_replay.status == "timed_out"
    assert expired_preview_replay.error_code == "confirmation_expired"
    assert expired_preview_replay.audit_ref["replayed"] is True

    valid_request = _request(
        world,
        correlation_id="confirmation-expiry",
        idempotency_key="confirmation-expiry-key",
        mode="confirm",
        confirmation=dict(preview.confirmation),
    )
    timed_out = _invoke(db, world, provider, valid_request)
    replay = _invoke(db, world, provider, valid_request)
    invocation = db.get(CapabilityInvocation, preview.invocation_id)

    assert timed_out.status == "timed_out"
    assert timed_out.error_code == "confirmation_expired"
    assert timed_out.confirmation["expired"] is True
    assert timed_out.audit_ref["replayed"] is True
    assert replay.invocation_id == timed_out.invocation_id
    assert replay.audit_ref["replayed"] is True
    assert invocation.status == "timed_out"
    assert provider.calls == []
    assert len(provider.preview_calls) == 1
