"""Trusted, versioned Provider adapter for deterministic audit execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace
from typing import Any, ClassVar

from sqlalchemy.orm import Session

from ...models import ConnectorBinding, DataSource, DatasetVersion
from ...services import connector_service, permission_service
from ...services.capability_agent_extensions import (
    AgentProviderToolError,
    AgentToolSpec,
    GroundingResult,
    LegacyCapabilityMatch,
)
from ...services.capability_contracts import (
    Actor,
    CapabilityRef,
    Request,
    ResolvedDataHandle,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
)
from . import grounding, service


PROVIDER_KEY = grounding.PROVIDER_KEY
PROVIDER_VERSION = grounding.PROVIDER_VERSION
_LEGACY_NAMESPACES = ("medical_audit",)


@dataclass(frozen=True, slots=True)
class CompatibilityManifest:
    """Provider-owned legacy selection and aliases; never read by the kernel."""

    scenario_namespaces: tuple[str, ...]
    tool_aliases: tuple[AgentToolSpec, ...]


COMPATIBILITY_MANIFEST = CompatibilityManifest(
    scenario_namespaces=_LEGACY_NAMESPACES,
    tool_aliases=(
        AgentToolSpec(
            name="run_medical_audit",
            description=(
                "执行版本化、确定性的医保违规审计。只选择受控 strategy 并传业务参数；"
                "不能传 SQL、表名、列名或数据源 id。结果包含全量命中计数和金额、证据口径及分页游标；"
                "truncated=true 时保持相同参数并使用 next_offset 读取下一页。"
            ),
            parameters=service.tool_schema(),
        ),
    ),
)


class MedicalAuditProviderError(ValueError):
    """The versioned Provider definition or runtime binding is invalid."""


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _functions(definition: Any) -> Mapping[str, Any]:
    resources = _read(definition, "functions", {}) or {}
    if not isinstance(resources, Mapping):
        raise MedicalAuditProviderError("resolved definition function catalog is invalid")
    return resources


def _function_resource(
    capability: CapabilityRef,
    deployment: ResolvedDeployment,
) -> Any:
    if capability.kind != "function":
        raise MedicalAuditProviderError("provider capability must be a function definition")
    resource = _functions(deployment.definition).get(capability.resource_id)
    if resource is None:
        raise MedicalAuditProviderError("provider function is absent from the definition")
    return resource


def _provider_config(function: Any) -> Mapping[str, Any]:
    if str(_read(function, "runtime_kind", "") or "") != "provider":
        raise MedicalAuditProviderError("function is not bound to a trusted Provider")
    runtime_config = _read(function, "runtime_config", {}) or {}
    if not isinstance(runtime_config, Mapping):
        raise MedicalAuditProviderError("Provider runtime config must be an object")
    if str(runtime_config.get("provider_key") or "").strip().casefold() != PROVIDER_KEY:
        raise MedicalAuditProviderError("function is bound to another Provider")
    if str(runtime_config.get("provider_version") or "").strip() != PROVIDER_VERSION:
        raise MedicalAuditProviderError("function requires another Provider version")
    config = runtime_config.get("provider_config") or {}
    if not isinstance(config, Mapping):
        raise MedicalAuditProviderError("Provider config must be an object")
    port_key = str(config.get("input_port_key") or "").strip().casefold()
    mapping_ids = config.get("mapping_ids")
    if (
        not port_key
        or not isinstance(mapping_ids, Sequence)
        or isinstance(mapping_ids, (str, bytes, bytearray))
    ):
        raise MedicalAuditProviderError(
            "Provider config requires input_port_key and mapping_ids"
        )
    normalized_ids = tuple(dict.fromkeys(str(value or "").strip() for value in mapping_ids))
    if not normalized_ids or any(not value for value in normalized_ids):
        raise MedicalAuditProviderError("Provider mapping_ids are invalid")
    return MappingProxyType(
        {
            "input_port_key": port_key,
            "mapping_ids": normalized_ids,
        }
    )


def _provider_input_port(
    capability: CapabilityRef,
    deployment: ResolvedDeployment,
    port_key: str,
) -> Any:
    resources = _read(deployment.definition, "capability_ports", {}) or {}
    values = resources.values() if isinstance(resources, Mapping) else resources
    matches = [
        port
        for port in values
        if str(_read(port, "capability_kind", "") or "").casefold() == capability.kind
        and str(_read(port, "capability_key", "") or "") == capability.resource_id
        and str(_read(port, "port_key", "") or "").casefold() == port_key
        and str(_read(port, "direction", "input") or "input").casefold() == "input"
        and str(_read(port, "binding_policy", "none") or "none").casefold() != "none"
    ]
    if len(matches) != 1:
        raise MedicalAuditProviderError(
            "Provider input_port_key must resolve to exactly one active managed input port"
        )
    return matches[0]


def _bound_functions(context: Any) -> tuple[Any, ...]:
    result: list[Any] = []
    for function in getattr(context, "functions", ()) or ():
        try:
            config = _provider_config(function)
        except MedicalAuditProviderError:
            continue
        if config:
            result.append(function)
    return tuple(result)


def _property_access(db: Session, definition: Any) -> service.MedicalAuditAccessPolicy:
    allowed: set[str] = set()
    entities = _read(definition, "entities", {}) or {}
    values = entities.values() if isinstance(entities, Mapping) else entities
    for entity in values:
        entity_api_name = str(_read(entity, "api_name", "") or "").strip()
        if entity_api_name not in {"medical_charge_line", "medical_encounter"}:
            continue
        for prop in _read(entity, "properties", []) or []:
            property_api_name = str(_read(prop, "api_name", "") or "").strip()
            if property_api_name and permission_service.can_read_property(db, prop):
                allowed.add(f"{entity_api_name}.{property_api_name}")
    return service.access_policy(sorted(allowed))


def _selected_mappings(definition: Any, mapping_ids: Sequence[str]) -> tuple[Any, ...]:
    resources = _read(definition, "mappings", {}) or {}
    if not isinstance(resources, Mapping):
        raise MedicalAuditProviderError("resolved definition mapping catalog is invalid")
    selected: list[Any] = []
    for mapping_id in mapping_ids:
        mapping = resources.get(mapping_id)
        if mapping is None:
            raise MedicalAuditProviderError(
                "Provider mapping is absent from the resolved definition"
            )
        selected.append(mapping)
    return tuple(selected)


def _source_for_handle(
    db: Session,
    deployment: ResolvedDeployment,
    handle: ResolvedDataHandle,
) -> Any:
    if handle.binding_kind in {"dataset_version", "dataset_head"}:
        version_id = str(handle.version_id or handle.reference_id or "").strip()
        version = db.get(DatasetVersion, version_id)
        if (
            version is None
            or str(version.tenant_id) != deployment.tenant_id
            or str(version.status or "").lower() != "ready"
            or str(version.content_hash or "").lower() != handle.signature
        ):
            raise MedicalAuditProviderError(
                "managed dataset version changed after runtime resolution"
            )
        return SimpleNamespace(
            id=version.id,
            tenant_id=deployment.tenant_id,
            name=f"managed:{handle.port_key}",
            type="dataset",
            connector_revision=0,
            config={
                "dataset_id": version.dataset_id,
                "dataset_version_id": version.id,
            },
        )
    if handle.binding_kind == "connector_binding":
        binding = db.get(ConnectorBinding, handle.reference_id)
        if (
            binding is None
            or str(binding.tenant_id) != deployment.tenant_id
            or str(binding.scenario_id) != deployment.scenario_id
            or str(binding.environment or "").lower() != deployment.environment
            or str(binding.connector_kind or "").lower() != "data_source"
            or str(binding.health_status or "").lower() != "healthy"
            or str(binding.connector_signature or "").lower() != handle.signature
        ):
            raise MedicalAuditProviderError(
                "managed connector changed after runtime resolution"
            )
        source = db.get(DataSource, binding.connector_id)
        if source is None or str(source.tenant_id) != deployment.tenant_id:
            raise MedicalAuditProviderError("managed data connector is unavailable")
        return source
    raise MedicalAuditProviderError("Provider input port requires a dataset or connector")


def _rebind_mappings(mappings: Sequence[Any], source: Any) -> tuple[Any, ...]:
    rebound: list[Any] = []
    for mapping in mappings:
        values = {
            # Frozen runtime definitions expose mapping proxies.  The audit
            # contract only reads these JSON-like values and immediately
            # covers them with its own fingerprint, so copying is unnecessary
            # (and ``deepcopy`` cannot handle MappingProxyType values).
            key: _read(mapping, key)
            for key in (
                "id",
                "entity_id",
                "data_source_binding_key",
                "data_source_binding_ref",
                "table_name",
                "column_map",
                "transform_rules",
                "status",
                "last_error",
            )
        }
        values["definition_data_source_id"] = str(
            _read(mapping, "definition_data_source_id", _read(mapping, "data_source_id", ""))
            or ""
        )
        values["data_source_id"] = str(source.id)
        rebound.append(SimpleNamespace(**values))
    return tuple(rebound)


def _runtime_mapping_contract(
    db: Session,
    deployment: ResolvedDeployment,
    data_context: RuntimeDataContext,
    config: Mapping[str, Any],
) -> service.MedicalAuditMappingContract:
    handle = data_context.get(str(config["input_port_key"]))
    if handle is None:
        raise MedicalAuditProviderError("Provider input port is not resolved")
    source = _source_for_handle(db, deployment, handle)
    mappings = _rebind_mappings(
        _selected_mappings(deployment.definition, config["mapping_ids"]),
        source,
    )
    return service.resolve_mapping_contract(
        [source],
        mappings,
        definition=deployment.definition,
    )


def _runtime_provenance(
    deployment: ResolvedDeployment,
    data_context: RuntimeDataContext,
) -> Mapping[str, Any]:
    return {
        "definition_hash": deployment.definition_hash,
        "deployment_fingerprint": deployment.fingerprint,
        "data_context_fingerprint": data_context.fingerprint,
        "data_handles": [dict(handle.audit_fact()) for handle in data_context.handles],
    }


def _governed_result_evidence(
    result: Mapping[str, Any],
    handle: ResolvedDataHandle,
) -> dict[str, Any]:
    """Project internal query evidence onto protocol-safe governed facts."""

    projected = dict(result)
    internal_evidence = dict(projected.get("evidence") or {})
    internal_lineage = dict(projected.get("lineage") or {})
    governed_reference = {
        "binding_kind": handle.binding_kind,
        "reference_id": handle.reference_id,
        "version_id": handle.version_id,
        "signature": handle.signature,
    }

    semantic_properties = sorted(
        {
            str(value).strip()
            for value in internal_lineage.get("resolved_column_properties", ())
            if str(value).strip()
        }
    )
    internal_mapping_contract = internal_lineage.get("mapping_contract")
    mapping_contract = (
        {
            key: internal_mapping_contract[key]
            for key in (
                "contract_version",
                "fingerprint",
                "mapping_ids",
                "definition",
            )
            if key in internal_mapping_contract
        }
        if isinstance(internal_mapping_contract, Mapping)
        else {}
    )

    # Physical query plans remain Provider-private.  The protocol-facing
    # projection deliberately uses a whitelist so later domain evidence fields
    # cannot silently expose table names, column names, connector revisions, or
    # the internal DataSource identity through REST/MCP receipts.
    evidence = {
        key: internal_evidence[key]
        for key in (
            "matching",
            "rule",
            "parameters",
            "amount_basis",
            "duration_basis",
        )
        if key in internal_evidence
    }
    evidence.update(
        {
            "source_id": handle.reference_id,
            "source_name": f"managed:{handle.port_key}",
            "governed_reference": governed_reference,
            "semantic_properties": semantic_properties,
            "mapping_contract_fingerprint": mapping_contract.get(
                "fingerprint", ""
            ),
        }
    )
    lineage = {
        key: internal_lineage[key]
        for key in (
            "schema_version",
            "audit_version",
            "request",
            "record_fields",
            "summary_fields",
            "resolved_column_properties",
            "property_refs",
        )
        if key in internal_lineage
    }
    lineage.update(
        {
            "source_id": handle.reference_id,
            "mapping_contract": mapping_contract,
            "governed_reference": governed_reference,
        }
    )
    projected["evidence"] = evidence
    projected["lineage"] = lineage
    return projected


@dataclass(frozen=True, slots=True)
class MedicalAuditAgentExtension:
    context: Any
    ready: bool
    provider_key: ClassVar[str] = PROVIDER_KEY
    provider_version: ClassVar[str] = PROVIDER_VERSION

    @property
    def _tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in COMPATIBILITY_MANIFEST.tool_aliases)

    def agent_tools(self) -> Sequence[AgentToolSpec]:
        return COMPATIBILITY_MANIFEST.tool_aliases if self.ready else ()

    def _mapping_contract(self) -> service.MedicalAuditMappingContract:
        return service.resolve_mapping_contract(
            self.context.data_sources,
            self.context.mappings,
            definition=self.context.runtime_definition,
        )

    def _access_policy(self) -> service.MedicalAuditAccessPolicy:
        return _property_access(self.context.db, self.context.runtime_definition)

    def execute_agent_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if name not in self._tool_names or not self.ready:
            raise AgentProviderToolError(
                "DIRECT_TOOL_DISABLED",
                "当前 Provider 工具未在此运行定义中启用。",
            )
        try:
            return service.run_medical_audit(
                self._mapping_contract(),
                arguments,
                property_access=self._access_policy(),
            )
        except service.MedicalAuditError as exc:
            raise AgentProviderToolError(
                exc.code,
                exc.message,
                retryable=exc.retryable,
            ) from None

    def authorize_historic_tool_result(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> bool:
        if name not in self._tool_names or not self.ready:
            return False
        try:
            return service.authorize_historic_result(
                self._mapping_contract(),
                arguments,
                result,
                property_access=self._access_policy(),
            )
        except service.MedicalAuditError:
            return False

    @staticmethod
    def _shadow_business_result(result: Any) -> Any:
        if not isinstance(result, Mapping):
            raise MedicalAuditProviderError("Provider shadow result must be an object")
        # Invocation provenance is checked independently by the kernel.  The
        # compatibility comparison covers the Provider's business result and
        # intentionally excludes legacy-versus-governed provenance envelopes.
        return {
            key: value
            for key, value in result.items()
            if key not in {"evidence", "lineage", "provider", "grounding"}
        }

    def match_legacy_capability(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> LegacyCapabilityMatch | None:
        if not self.authorize_historic_tool_result(name, arguments, result):
            return None
        functions = _bound_functions(self.context)
        if len(functions) != 1:
            # One alias cannot prove which target was selected when multiple
            # Provider functions share it.  A future manifest may add an
            # explicit target selector; ambiguity always fails closed.
            return None
        return LegacyCapabilityMatch(
            owner_key=PROVIDER_KEY,
            owner_version=PROVIDER_VERSION,
            capability_kind="function",
            capability_key=str(_read(functions[0], "id", "")),
            inputs=dict(arguments),
            comparison_result=self._shadow_business_result(result),
        )

    def normalize_capability_shadow_result(
        self,
        match: LegacyCapabilityMatch,
        result: Any,
    ) -> Any:
        if (
            match.owner_key != PROVIDER_KEY
            or match.owner_version != PROVIDER_VERSION
        ):
            raise MedicalAuditProviderError("Provider shadow match is not owned here")
        return self._shadow_business_result(result)

    def verify_shadow_data_context(
        self,
        match: LegacyCapabilityMatch,
        data_context: RuntimeDataContext,
    ) -> str | None:
        """Prove the managed handle resolves to the legacy mapping source."""

        if (
            match.owner_key != PROVIDER_KEY
            or match.owner_version != PROVIDER_VERSION
            or match.capability_kind != "function"
        ):
            return None
        functions = [
            function
            for function in _bound_functions(self.context)
            if str(_read(function, "id", "")) == match.capability_key
        ]
        if len(functions) != 1:
            return None
        try:
            config = _provider_config(functions[0])
            legacy_contract = self._mapping_contract()
        except (MedicalAuditProviderError, service.MedicalAuditError):
            return None
        handle = data_context.get(str(config["input_port_key"]))
        if handle is None:
            return None
        source = legacy_contract.source
        source_type = str(_read(source, "type", "") or "").strip().casefold()
        source_config = _read(source, "config", {}) or {}
        if not isinstance(source_config, Mapping):
            return None
        resolved_identity: dict[str, Any]
        if handle.binding_kind in {"dataset_version", "dataset_head"}:
            version_id = str(handle.version_id or "").strip()
            version = self.context.db.get(DatasetVersion, version_id)
            if (
                source_type != "dataset"
                or version is None
                or str(version.tenant_id) != str(self.context.tenant_id)
                or str(version.status or "").casefold() != "ready"
                or str(version.content_hash or "").casefold() != handle.signature
                or str(source_config.get("dataset_version_id") or "") != version.id
                or (
                    source_config.get("dataset_id") is not None
                    and str(source_config.get("dataset_id") or "")
                    != str(version.dataset_id)
                )
            ):
                return None
            resolved_identity = {
                "kind": "dataset_version",
                "version_id": version.id,
                "dataset_id": version.dataset_id,
                "signature": handle.signature,
            }
        elif handle.binding_kind == "connector_binding":
            binding = self.context.db.get(ConnectorBinding, handle.reference_id)
            scenario = getattr(self.context, "scenario", None)
            environment = str(
                _read(self.context.runtime_definition, "environment", "") or ""
            ).casefold()
            if (
                binding is None
                or scenario is None
                or str(binding.tenant_id) != str(self.context.tenant_id)
                or str(binding.scenario_id) != str(scenario.id)
                or str(binding.environment or "").casefold() != environment
                or str(binding.connector_kind or "").casefold() != "data_source"
                or str(binding.connector_id) != str(legacy_contract.source_id)
                or str(binding.health_status or "").casefold() != "healthy"
                or str(binding.connector_signature or "").casefold()
                != handle.signature
                or connector_service.connector_signature("data_source", source)
                != handle.signature
            ):
                return None
            resolved_identity = {
                "kind": "connector_binding",
                "binding_id": binding.id,
                "connector_id": binding.connector_id,
                "signature": handle.signature,
            }
        else:
            return None
        return canonical_hash(
            {
                "contract": "medical-audit-shadow-data-equivalence/v1",
                "provider_key": PROVIDER_KEY,
                "provider_version": PROVIDER_VERSION,
                "capability_key": match.capability_key,
                "port_key": handle.port_key,
                "runtime_data_context_fingerprint": data_context.fingerprint,
                "legacy_mapping_contract_fingerprint": legacy_contract.fingerprint,
                "resolved_identity": resolved_identity,
            },
            domain="provider-shadow-data-equivalence-v1",
        )

    def prepare_grounding(self, user_message: str) -> grounding.GroundingPreparation:
        if not any(term in user_message for term in grounding.AUDIT_INTENT_TERMS):
            return grounding.GroundingPreparation()
        if not self.ready:
            return grounding.GroundingPreparation(facility_lookup_succeeded=False)
        try:
            facilities = service.find_facility_names_in_text(
                self._mapping_contract(),
                user_message,
                property_access=self._access_policy(),
            )
        except Exception:  # noqa: BLE001 - all lookup failures fail closed.
            return grounding.GroundingPreparation(facility_lookup_succeeded=False)
        return grounding.GroundingPreparation(
            authoritative_facilities=tuple(facilities),
            facility_lookup_succeeded=True,
        )

    def ground(
        self,
        user_message: str,
        tool_outcomes: Sequence[Mapping[str, Any]],
        prepared: Any,
    ) -> GroundingResult:
        state = (
            prepared
            if isinstance(prepared, grounding.GroundingPreparation)
            else grounding.GroundingPreparation(facility_lookup_succeeded=False)
        )
        definition_hash = str(
            _read(self.context.runtime_definition, "definition_hash", "") or ""
        )
        return grounding.grounding_result(
            tool_outcomes,
            user_message=user_message,
            tool_names=self._tool_names,
            authoritative_facilities=state.authoritative_facilities,
            facility_lookup_succeeded=state.facility_lookup_succeeded,
            definition_hash=definition_hash,
        )


@dataclass(frozen=True, slots=True)
class MedicalAuditProvider:
    """Standard CapabilityProvider plus a legacy Agent compatibility adapter."""

    _db: Session | None = None
    provider_key: ClassVar[str] = PROVIDER_KEY
    provider_version: ClassVar[str] = PROVIDER_VERSION

    def bind_invocation(self, context: Any) -> "MedicalAuditProvider":
        if not isinstance(context, Session):
            raise MedicalAuditProviderError("Provider requires a database context")
        return replace(self, _db=context)

    def _session(self) -> Session:
        if self._db is None:
            raise MedicalAuditProviderError("Provider invocation is not bound")
        return self._db

    def bind_agent_runtime(self, context: Any) -> MedicalAuditAgentExtension | None:
        scenario = getattr(context, "scenario", None)
        if scenario is None or getattr(context, "runtime_definition", None) is None:
            return None
        explicit = bool(_bound_functions(context))
        namespace = str(getattr(scenario, "namespace", "") or "").strip()
        legacy = namespace in COMPATIBILITY_MANIFEST.scenario_namespaces
        if not explicit and not legacy:
            return None
        ready = True
        try:
            service.resolve_mapping_contract(
                context.data_sources,
                context.mappings,
                definition=context.runtime_definition,
            )
        except service.MedicalAuditError:
            ready = False
        return MedicalAuditAgentExtension(context=context, ready=ready)

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        function = _function_resource(capability, deployment)
        config = _provider_config(function)
        _provider_input_port(capability, deployment, str(config["input_port_key"]))
        schema = _read(function, "input_schema", {}) or {}
        if not isinstance(schema, Mapping):
            raise MedicalAuditProviderError("Provider function input schema is invalid")
        return {
            "input_schema": dict(schema),
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        if actor.tenant_id != deployment.tenant_id:
            raise PermissionError("Provider actor is outside the deployment tenant")
        function = _function_resource(request.capability, deployment)
        config = _provider_config(function)
        contract = _runtime_mapping_contract(
            self._session(),
            deployment,
            data_context,
            config,
        )
        handle = data_context.get(str(config["input_port_key"]))
        if handle is None:
            raise MedicalAuditProviderError("Provider input port is not resolved")
        result = _governed_result_evidence(
            service.run_medical_audit(
                contract,
                request.inputs,
                property_access=_property_access(
                    self._session(), deployment.definition
                ),
            ),
            handle,
        )
        grounding_result = GroundingResult(
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
            verified=True,
            provenance=_runtime_provenance(deployment, data_context),
        )
        return {
            **result,
            "provider": {
                "key": PROVIDER_KEY,
                "version": PROVIDER_VERSION,
            },
            "grounding": grounding_result.as_dict(),
        }


__all__ = [
    "COMPATIBILITY_MANIFEST",
    "CompatibilityManifest",
    "MedicalAuditAgentExtension",
    "MedicalAuditProvider",
    "MedicalAuditProviderError",
    "PROVIDER_KEY",
    "PROVIDER_VERSION",
]
