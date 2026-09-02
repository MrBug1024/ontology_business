"""Trusted adapters from ontology resources to the capability kernel."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from sqlalchemy.orm import Session

from ..models import ActionExecutionLog, CapabilityInvocation
from . import (
    capability_readiness_service,
    function_runtime_service,
    operations_service,
    permission_service,
    runtime_definition_service,
    workflow_service,
)
from .capability_contracts import (
    Actor,
    CapabilityRef,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
)
from .capability_provider_keys import (
    BUILTIN_PROVIDER_KEYS,
    derive_provider_execution_key,
)
from .capability_registry import ProviderRecovery
from .provider_actor_service import ProviderActorError, require_actor_session


class BuiltinCapabilityProviderError(ValueError):
    """A built-in resource cannot cross the trusted provider boundary."""


def _safe_definition(deployment: ResolvedDeployment) -> Any:
    definition = deployment.definition
    if not isinstance(definition, runtime_definition_service.RuntimeDefinition):
        raise BuiltinCapabilityProviderError(
            "built-in provider requires a resolved runtime definition"
        )
    if (
        definition.scenario.id != deployment.scenario_id
        or definition.scenario.tenant_id != deployment.tenant_id
        or definition.environment != deployment.environment
        or definition.definition_hash != deployment.definition_hash
        or definition.snapshot_id != deployment.snapshot_id
        or definition.release_id != deployment.release_id
    ):
        raise BuiltinCapabilityProviderError(
            "resolved runtime definition does not match the deployment pin"
        )
    return definition


def _resource(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
    expected_kind: str,
) -> tuple[Any, Any]:
    if capability.kind != expected_kind:
        raise BuiltinCapabilityProviderError("capability kind does not match provider")
    definition = _safe_definition(deployment)
    try:
        resource = runtime_definition_service.resolve_resource(
            definition,
            expected_kind,
            capability.resource_id,
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise BuiltinCapabilityProviderError(
            "capability is unavailable in the resolved runtime definition"
        ) from exc
    return definition, resource


def _session_user_id(db: Session, actor: Actor) -> str | None:
    try:
        return require_actor_session(db, actor)
    except ProviderActorError as exc:
        raise BuiltinCapabilityProviderError(str(exc)) from exc


def _structured_input_audit(inputs: Mapping[str, Any]) -> dict[str, Any]:
    value_types = sorted(
        {
            "null"
            if value is None
            else "boolean"
            if isinstance(value, bool)
            else "integer"
            if isinstance(value, int)
            else "number"
            if isinstance(value, float)
            else "string"
            if isinstance(value, str)
            else "object"
            if isinstance(value, Mapping)
            else "array"
            if isinstance(value, (list, tuple))
            else "unknown"
            for value in inputs.values()
        }
    )
    return {
        "contract": "capability-structured-input-audit/v1",
        "field_count": len(inputs),
        "input_hash": canonical_hash(
            inputs,
            domain="builtin-provider-structured-input-v1",
        ),
        "value_types": value_types,
    }


def _rule_condition_fields(condition: Any) -> list[str]:
    fields: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("field", "value_field"):
                field = str(value.get(key) or "").strip()
                if field:
                    fields.add(field)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)

    visit(condition)
    return sorted(fields)


def _rule_property_schema(prop: Any) -> dict[str, Any]:
    kind = str(getattr(prop, "data_type", "") or "").strip().lower()
    schema: dict[str, Any]
    if kind == "date":
        schema = {"type": "string", "format": "date"}
    elif kind == "datetime":
        schema = {"type": "string", "format": "date-time"}
    elif kind in {"float", "number"}:
        schema = {"type": "number"}
    elif kind == "integer":
        schema = {"type": "integer"}
    elif kind == "boolean":
        schema = {"type": "boolean"}
    elif kind in {"string", "text"}:
        schema = {"type": "string"}
    else:
        schema = {}
    enum_values = list(getattr(prop, "enum_values", None) or [])
    if bool(getattr(prop, "is_enum", False)) and enum_values:
        schema["enum"] = enum_values
    return schema


def _rule_input_schema(definition: Any, rule: Any) -> dict[str, Any]:
    properties_by_field: dict[str, Any] = {}
    entity_id = str(getattr(rule, "entity_id", "") or "")
    entity = definition.entities.get(entity_id) if entity_id else None
    for prop in (getattr(entity, "properties", None) or []):
        for field in {
            str(getattr(prop, "name", "") or "").strip(),
            str(getattr(prop, "api_name", "") or "").strip(),
        }:
            if field:
                properties_by_field[field] = prop
    fields = _rule_condition_fields(getattr(rule, "condition", {}) or {})
    return {
        "type": "object",
        "properties": {
            "record": {
                "type": "object",
                "properties": {
                    field: _rule_property_schema(properties_by_field.get(field))
                    for field in fields
                },
                "required": fields,
                "additionalProperties": False,
            }
        },
        "required": ["record"],
        "additionalProperties": False,
    }


@contextmanager
def _invocation_lineage(
    db: Session,
    request: Request,
    actor: Actor,
    *,
    parent_action_log_id: str | None = None,
) -> Iterator[None]:
    previous_lineage = db.info.get("action_lineage_context")
    previous_trace = db.info.get("llm_trace_context")
    lineage = dict(previous_lineage) if isinstance(previous_lineage, Mapping) else {}
    trace = dict(previous_trace) if isinstance(previous_trace, Mapping) else {}
    lineage["correlation_id"] = request.correlation_id
    lineage["capability_principal_type"] = actor.actor_type
    lineage["capability_principal_hash"] = canonical_hash(
        {
            "principal_id": actor.principal_id,
            "tenant_id": actor.tenant_id,
        },
        domain="capability-action-principal-v1",
    )
    trace["correlation_id"] = request.correlation_id
    if parent_action_log_id:
        lineage["parent_action_log_id"] = parent_action_log_id
    else:
        lineage.pop("parent_action_log_id", None)
    db.info["action_lineage_context"] = lineage
    db.info["llm_trace_context"] = trace
    try:
        yield
    finally:
        if previous_lineage is None:
            db.info.pop("action_lineage_context", None)
        else:
            db.info["action_lineage_context"] = previous_lineage
        if previous_trace is None:
            db.info.pop("llm_trace_context", None)
        else:
            db.info["llm_trace_context"] = previous_trace


def _preview_log(
    db: Session,
    deployment: ResolvedDeployment,
    actor: Actor,
    request: Request,
    *,
    target_type: str,
) -> ActionExecutionLog:
    preview_invocation_id = str(
        request.confirmation.get("preview_invocation_id") or ""
    ).strip()
    invocation = db.get(CapabilityInvocation, preview_invocation_id)
    result_document = (
        invocation.result_document
        if invocation is not None and isinstance(invocation.result_document, dict)
        else {}
    )
    output = result_document.get("output", {})
    preview_log_id = (
        str(output.get("preview_log_id") or "").strip()
        if isinstance(output, Mapping)
        else ""
    )
    preview = db.get(ActionExecutionLog, preview_log_id) if preview_log_id else None
    session_user_id = _session_user_id(db, actor)
    if (
        invocation is None
        or invocation.tenant_id != deployment.tenant_id
        or invocation.scenario_id != deployment.scenario_id
        or invocation.capability_kind != request.capability.kind
        or invocation.capability_key != request.capability.resource_id
        or invocation.definition_hash != deployment.definition_hash
        or invocation.deployment_fingerprint != deployment.fingerprint
        or preview is None
        or preview.scenario_id != deployment.scenario_id
        or preview.target_type != target_type
        or preview.target_id != request.capability.resource_id
        or preview.mode != "dry_run"
        or preview.status != "dry_run"
        or (preview.input_params or {}) != _structured_input_audit(request.inputs)
        or preview.environment != deployment.environment
        or preview.definition_snapshot_id != deployment.snapshot_id
        or preview.release_id != deployment.release_id
        or preview.definition_hash != deployment.definition_hash
        or (session_user_id is not None and preview.actor_user_id != session_user_id)
    ):
        raise BuiltinCapabilityProviderError(
            "provider preview audit does not match the confirmed invocation"
        )
    return preview


@dataclass(frozen=True, slots=True)
class _BuiltinProvider:
    _db: Session | None = None

    provider_key: ClassVar[str]
    provider_version: ClassVar[str] = "1.0.0"
    capability_kind: ClassVar[str]

    def bind_invocation(self, context: Any) -> _BuiltinProvider:
        if not isinstance(context, Session):
            raise BuiltinCapabilityProviderError(
                "built-in provider requires a database invocation context"
            )
        return replace(self, _db=context)

    def _session(self) -> Session:
        if self._db is None:
            raise BuiltinCapabilityProviderError("built-in provider is not bound")
        return self._db

    def _require_supported_data_context(
        self,
        data_context: RuntimeDataContext,
    ) -> None:
        if not isinstance(data_context, RuntimeDataContext):
            raise BuiltinCapabilityProviderError(
                "built-in provider requires a resolved runtime data context"
            )
        # These executors accept structured values, while runtime handles are
        # logical references.  A typed materializer must own that translation.
        if data_context.handles:
            raise BuiltinCapabilityProviderError(
                f"built-in {self.capability_kind} provider does not support "
                "managed runtime inputs"
            )

    def _ready_resource(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> tuple[Any, Any]:
        definition, resource = _resource(
            deployment,
            capability,
            self.capability_kind,
        )
        capability_readiness_service.require_executable(
            self.capability_kind,
            resource,
            definition=definition,
            db=self._session(),
        )
        return definition, resource


@dataclass(frozen=True, slots=True)
class FunctionDefinitionProvider(_BuiltinProvider):
    provider_key: ClassVar[str] = BUILTIN_PROVIDER_KEYS["function"]
    capability_kind: ClassVar[str] = "function"

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        _definition, function = _resource(
            deployment,
            capability,
            self.capability_kind,
        )
        return {
            "input_schema": workflow_service.normalize_parameter_schema(
                function.input_schema or {}
            ),
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def preview(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        definition, function = self._ready_resource(request.capability, deployment)
        _session_user_id(self._session(), actor)
        decision = permission_service.check_scenario(
            self._session(), definition.scenario, "read"
        )
        if not decision.allowed:
            raise PermissionError("function preview is not permitted")
        return {
            "function_id": function.id,
            "function_name": function.name,
            "preview": True,
            "side_effects_skipped": True,
        }

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        definition, function = self._ready_resource(request.capability, deployment)
        _session_user_id(self._session(), actor)
        permission_service.require_scenario_permission(
            self._session(),
            definition.scenario,
            "write",
            message="function execution is not permitted",
        )
        return function_runtime_service.execute_function(function, request.inputs)


@dataclass(frozen=True, slots=True)
class OntologyRuleProvider(_BuiltinProvider):
    provider_key: ClassVar[str] = BUILTIN_PROVIDER_KEYS["rule"]
    capability_kind: ClassVar[str] = "rule"

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        definition, rule = _resource(
            deployment,
            capability,
            self.capability_kind,
        )
        return {
            "input_schema": _rule_input_schema(definition, rule),
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def _evaluate(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        definition, rule = self._ready_resource(request.capability, deployment)
        _session_user_id(self._session(), actor)
        permission_service.require_scenario_permission(
            self._session(),
            definition.scenario,
            "read",
            message="rule evaluation is not permitted",
        )
        record = request.inputs.get("record")
        if not isinstance(record, Mapping):
            raise BuiltinCapabilityProviderError("rule record must be an object")
        return workflow_service.evaluate_rule(
            rule,
            dict(record),
            db=self._session(),
            runtime_definition=definition,
        )

    def preview(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        return self._evaluate(request, actor, deployment, data_context)

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        return self._evaluate(request, actor, deployment, data_context)


@dataclass(frozen=True, slots=True)
class OntologyActionProvider(_BuiltinProvider):
    provider_key: ClassVar[str] = BUILTIN_PROVIDER_KEYS["action"]
    capability_kind: ClassVar[str] = "action"

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        _definition, action = _resource(
            deployment,
            capability,
            self.capability_kind,
        )
        return {
            "input_schema": workflow_service.normalize_parameter_schema(
                action.input_schema or {}
            ),
            "required_roles": [],
            "required_scopes": [],
            "side_effect": True,
            # The capability boundary is stricter than legacy direct Action
            # routes: every side effect is previewed, confirmed, and idempotent.
            "requires_confirmation": True,
            "idempotency_required": True,
        }

    def preview(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        definition, action = self._ready_resource(request.capability, deployment)
        _session_user_id(self._session(), actor)
        with _invocation_lineage(self._session(), request, actor):
            response = workflow_service.preview_action(
                self._session(),
                action,
                dict(request.inputs),
                runtime_environment=definition.environment,
                runtime_definition=definition,
                commit=False,
                audit_input_params=_structured_input_audit(request.inputs),
                include_preview_input_values=False,
            )
        result = response.get("result", {})
        plan = result.get("plan", {}) if isinstance(result, Mapping) else {}
        return {
            "action_id": action.id,
            "action_name": action.name,
            "executor_type": action.executor_type,
            "parameter_count": int(plan.get("parameter_count") or 0),
            "preview_log_id": response.get("log_id"),
            "side_effects_skipped": True,
            "status": "dry_run",
        }

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        if request.mode != "confirm":
            raise BuiltinCapabilityProviderError(
                "action execution requires a confirmed provider invocation"
            )
        definition, action = self._ready_resource(request.capability, deployment)
        preview = _preview_log(
            self._session(),
            deployment,
            actor,
            request,
            target_type="action",
        )
        with _invocation_lineage(
            self._session(),
            request,
            actor,
            parent_action_log_id=preview.id,
        ):
            response = workflow_service.execute_action(
                self._session(),
                action,
                dict(request.inputs),
                confirm=True,
                dry_run=False,
                idempotency_key=derive_provider_execution_key(
                    request,
                    actor,
                    deployment,
                ),
                enforce_policy=True,
                runtime_environment=definition.environment,
                runtime_definition=definition,
                audit_input_params=_structured_input_audit(request.inputs),
                external_idempotency_required=True,
            )
        status = str(response.get("status") or "")
        original_status = str(response.get("original_status") or "")
        if status != "success" and not (
            status == "idempotent_replay" and original_status == "success"
        ):
            raise BuiltinCapabilityProviderError("action execution did not succeed")
        return {
            "action_execution_log_id": response.get("log_id"),
            "idempotent_replay": status == "idempotent_replay",
            "result": response.get("result", {}),
            "status": "succeeded",
        }

    def recover(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> ProviderRecovery:
        self._require_supported_data_context(data_context)
        if request.mode != "confirm":
            return ProviderRecovery(state="indeterminate")
        definition, action = _resource(
            deployment,
            request.capability,
            self.capability_kind,
        )
        preview = _preview_log(
            self._session(),
            deployment,
            actor,
            request,
            target_type="action",
        )
        recovery = workflow_service.recover_action_execution(
            self._session(),
            action,
            parent_action_log_id=preview.id,
            execution_key=derive_provider_execution_key(
                request,
                actor,
                deployment,
            ),
            expected_input_audit=_structured_input_audit(request.inputs),
            runtime_environment=definition.environment,
            runtime_definition=definition,
        )
        state = str(recovery.get("state") or "indeterminate")
        if state == "succeeded":
            return ProviderRecovery(
                state="succeeded",
                output=recovery.get("output", {}),
            )
        if state == "failed":
            return ProviderRecovery(
                state="failed",
                error_code=str(recovery.get("error_code") or "action_execution_failed"),
            )
        return ProviderRecovery(state="indeterminate")


@dataclass(frozen=True, slots=True)
class OntologyWorkflowProvider(_BuiltinProvider):
    provider_key: ClassVar[str] = BUILTIN_PROVIDER_KEYS["workflow"]
    capability_kind: ClassVar[str] = "workflow"

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        definition, workflow = _resource(
            deployment,
            capability,
            self.capability_kind,
        )
        return {
            "input_schema": workflow_service.workflow_parameter_schema(
                workflow,
                tuple(definition.actions.values()),
            ),
            "required_roles": [],
            "required_scopes": [],
            "side_effect": True,
            "requires_confirmation": True,
            "idempotency_required": True,
        }

    def preview(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        definition, workflow = self._ready_resource(request.capability, deployment)
        _session_user_id(self._session(), actor)
        decision = permission_service.check_workflow(
            self._session(), workflow, "read"
        )
        if not decision.allowed:
            raise PermissionError("workflow preview is not permitted")
        return {
            "node_count": len(list(workflow.nodes or [])),
            "preview": True,
            "side_effects_skipped": True,
            "step_count": len(list(workflow.steps or [])),
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
        }

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        self._require_supported_data_context(data_context)
        if request.mode != "confirm":
            raise BuiltinCapabilityProviderError(
                "workflow enqueue requires a confirmed provider invocation"
            )
        definition, workflow = self._ready_resource(request.capability, deployment)
        created_by_user_id = _session_user_id(self._session(), actor)
        run, created = operations_service.enqueue_workflow_run(
            self._session(),
            workflow,
            dict(request.inputs),
            trigger_source="manual",
            dedupe_key=derive_provider_execution_key(
                request,
                actor,
                deployment,
            ),
            created_by_user_id=created_by_user_id,
            runtime_definition=definition,
        )
        return {
            "created": created,
            "execution_key": run.execution_key,
            "status": run.status,
            "task_url": f"/tasks?task={run.id}",
            "workflow_run_id": run.id,
        }


def builtin_capability_providers() -> tuple[_BuiltinProvider, ...]:
    return (
        FunctionDefinitionProvider(),
        OntologyRuleProvider(),
        OntologyActionProvider(),
        OntologyWorkflowProvider(),
    )


__all__ = [
    "BuiltinCapabilityProviderError",
    "FunctionDefinitionProvider",
    "OntologyActionProvider",
    "OntologyRuleProvider",
    "OntologyWorkflowProvider",
    "builtin_capability_providers",
]
