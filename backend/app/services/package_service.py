"""Versioned, portable ontology resource packages.

This module deliberately has no HTTP, schema, or persistence side effects.  It is
the safe boundary used by a future package router/governance flow:

* ``export_scenario_package`` reads a scenario into a deterministic JSON package.
* ``validate_package`` accepts untrusted JSON and returns only a redacted,
  normalized preview plus structured diagnostics.
* ``plan_package_import`` compares that preview with a target scenario and is
  strictly read-only.
* ``create_governed_import_proposal`` materializes an applicable package only as
  an immutable release proposal; it never applies the package to a live scenario.

Database IDs, runtime objects, data-source connection configs, and credentials are
intentionally excluded.  Cross-resource references use package-local stable keys;
external systems (data sources, MCP and LLM bindings) are represented as safe
binding requirements instead of copied credentials.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    LLMConfig,
    MCPConfig,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
)
from . import connector_service, function_definition_service, permission_service, release_service


PACKAGE_FORMAT = "ontology-resource-package"
PACKAGE_VERSION = "1.0"
RESOURCE_KINDS = (
    "entities",
    "properties",
    "relations",
    "mappings",
    "functions",
    "actions",
    "rules",
    "events",
    "workflows",
)

# A v1 package created before governed functions existed remains portable.  New
# exports always include this collection; imports that omit it mean no function
# changes rather than an invalid or destructive package.
_OPTIONAL_RESOURCE_KINDS = {"functions"}

_RESOURCE_PREFIXES = {
    "entities": "entity/",
    "properties": "property/",
    "relations": "relation/",
    "mappings": "mapping/",
    "functions": "function/",
    "actions": "action/",
    "rules": "rule/",
    "events": "event/",
    "workflows": "workflow/",
}
_SINGULAR_RESOURCE_NAMES = {
    "entities": "entity",
    "properties": "property",
    "relations": "relation",
    "mappings": "mapping",
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
}
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "entities": ("key", "name"),
    "properties": ("key", "entity_ref", "name"),
    "relations": ("key", "source_entity_ref", "target_entity_ref", "name"),
    "mappings": ("key", "entity_ref", "data_source_ref"),
    "functions": ("key", "name", "input_schema", "output_schema"),
    "actions": ("key", "entity_ref", "name"),
    "rules": ("key", "name"),
    "events": ("key", "name"),
    "workflows": ("key", "name"),
}

# A resource package is a versioned contract, not a generic JSON transport.
# Keep this list aligned with ``_build_resources`` and ``_compile_package_overlay``:
# values below these fields can still be intentionally open declarative JSON (for
# example JSON Schema, Action executor config and workflow node data), but an
# unknown *resource-level* field must never be silently discarded during import.
_RESOURCE_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "entities": frozenset({
        "key", "name", "description", "icon", "color", "is_abstract",
    }),
    "properties": frozenset({
        "key", "entity_ref", "name", "data_type", "description", "is_key",
        "is_required", "is_enum", "enum_values", "default_value", "is_sensitive",
    }),
    "relations": frozenset({
        "key", "name", "source_entity_ref", "target_entity_ref", "relation_type",
        "description",
    }),
    "mappings": frozenset({
        "key", "entity_ref", "data_source_ref", "data_source_binding_key",
        "data_source_binding_ref", "table_name", "column_map",
    }),
    "functions": frozenset({
        "key", "name", "description", "input_schema", "output_schema", "tags", "visibility",
    }),
    "actions": frozenset({
        "key", "entity_ref", "name", "description", "input_schema", "executor_type",
        "executor_config", "precondition", "postcondition", "enabled",
        "requires_confirmation", "idempotency_required", "permission_scope", "access_scope",
    }),
    "rules": frozenset({
        "key", "entity_ref", "name", "description", "condition", "action_on_match",
        "trigger_action_refs", "severity", "enabled",
    }),
    "events": frozenset({
        "key", "name", "description", "payload_schema", "trigger_source", "enabled",
    }),
    "workflows": frozenset({
        "key", "name", "description", "trigger_type", "trigger_config", "steps",
        "nodes", "edges", "status", "enabled", "access_scope",
    }),
}

# Deliberately conservative: retaining an executor detail is useful, retaining a
# credential is not.  Normalization removes punctuation/case before this check so
# ``api-key``, ``API_KEY`` and nested ``authorization`` behave alike.
_SENSITIVE_EXACT_KEYS = {
    "apikey",
    "accesskey",
    "privatekey",
    "password",
    "passwd",
    "secret",
    "clientsecret",
    "authorization",
    "credential",
    "credentials",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "bearer",
}
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"token|password|passwd|secret|authorization)\s*([=:])\s*([^\s,;&]+)"
    ),
    # A credential may sit below a neutral field such as ``header_value`` rather
    # than a key named Authorization.  Cover both common HTTP auth schemes.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)(://[^\s/@:]+:)([^\s/@]+)(@)"),
)

_REFERENCE_ID_FIELDS = {
    "actionid": ("action_ref", "actions", "action"),
    "ruleid": ("rule_ref", "rules", "rule"),
    "eventid": ("event_ref", "events", "event"),
}
_FORBIDDEN_RUNTIME_ID_FIELDS = {
    "entityid",
    "datasourceid",
    "actionid",
    "ruleid",
    "eventid",
    "mcpid",
    "llmconfigid",
    "scenarioid",
    "tenantid",
}


class PackageImportError(ValueError):
    """The uploaded portable package cannot become a governed proposal."""


class PackageImportConflictError(PackageImportError):
    """The target/branch changed or still has unresolved import requirements."""


def _plain_json(value: Any) -> Any:
    """Turn JSON-ish ORM payloads into deterministic, serializable primitives."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain_json(item) for item in value), key=_canonical_json)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _key_token(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff_-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    return normalized or "unnamed"


def _normalized_key_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_key_name(value)
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    return (
        "apikey" in normalized
        or "secret" in normalized
        or "password" in normalized
        or "credential" in normalized
        or normalized.endswith("token")
        or normalized.endswith("accesskey")
        or normalized.endswith("privatekey")
    )


def _redact_string(value: str) -> str:
    text = value
    # Scheme credentials must be removed before generic ``authorization:`` /
    # ``token=`` matching.  Otherwise the generic expression can consume only
    # the word ``Basic`` or ``Bearer`` and leave its following credential intact.
    text = _SENSITIVE_VALUE_PATTERNS[1].sub(r"\1 [REDACTED]", text)
    text = _SENSITIVE_VALUE_PATTERNS[0].sub(r"\1\2[REDACTED]", text)
    text = _SENSITIVE_VALUE_PATTERNS[2].sub("[REDACTED]", text)
    return _SENSITIVE_VALUE_PATTERNS[3].sub(r"\1[REDACTED]\3", text)


def redact_sensitive(value: Any) -> Any:
    """Recursively remove credentials from exports, validation and previews.

    This is public because router/governance code may safely pass diagnostics through
    it before logging.  It preserves shape where feasible (``[REDACTED]`` rather
    than silently deleting a key), which makes missing bindings visible without
    allowing a secret to cross the package boundary.
    """
    value = _plain_json(value)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            safe[key] = "[REDACTED]" if _is_sensitive_key(key) else redact_sensitive(raw_value)
        return safe
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _without_fingerprint(package: Mapping[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(dict(package))
    manifest = copied.get("manifest")
    if isinstance(manifest, Mapping):
        copied["manifest"] = dict(manifest)
        copied["manifest"].pop("fingerprint", None)
    return copied


def _fingerprint_payload(package: Mapping[str, Any]) -> dict[str, Any]:
    """Return the signing payload with legacy optional collections canonicalized.

    Governed function declarations were added after resource-package v1 was
    already in use.  An absent ``resources.functions`` therefore means the same
    thing as an explicitly empty collection: this package makes no function
    changes.  Keep their signatures equivalent so a previously signed v1
    package remains importable after validation materializes the empty list for
    the compiler.  A non-empty collection is deliberately retained and always
    changes the fingerprint.
    """
    copied = _without_fingerprint(package)
    resources = copied.get("resources")
    if isinstance(resources, Mapping):
        canonical_resources = dict(resources)
        if canonical_resources.get("functions") == []:
            canonical_resources.pop("functions", None)
        copied["resources"] = canonical_resources
    return copied


def canonical_package_json(package: Mapping[str, Any]) -> str:
    """Return deterministic UTF-8 JSON text suitable for a download or signature."""
    return _canonical_json(redact_sensitive(package))


def package_fingerprint(package: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint independent of ``manifest.fingerprint``."""
    canonical = _canonical_json(redact_sensitive(_fingerprint_payload(package)))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assign_keys(
    items: list[Any],
    base_for: Callable[[Any], str],
    tie_breaker: Callable[[Any], tuple[Any, ...]] | None = None,
) -> dict[int, str]:
    """Assign deterministic package keys; duplicate semantic names get ``~N``."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        groups[base_for(item)].append(item)
    keys: dict[int, str] = {}
    for base in sorted(groups):
        group = groups[base]
        if tie_breaker:
            group = sorted(group, key=tie_breaker)
        else:
            group = sorted(group, key=lambda item: str(getattr(item, "id", "")))
        duplicate = len(group) > 1
        for index, item in enumerate(group, start=1):
            keys[id(item)] = f"{base}~{index}" if duplicate else base
    return keys


def _source_ref(source: DataSource | None) -> dict[str, Any]:
    if source is None:
        return {"kind": "data_source", "binding_required": True}
    return {
        "name": source.name or "",
        "type": source.type or "",
        # Source configuration and IDs intentionally do not travel in packages.
        "scope": "scenario" if source.scenario_id else "tenant",
    }


def _mapping_binding_metadata(value: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    """Read a portable mapping's optional explicit runtime binding.

    Mapping packages may carry a caller-selected stable key.  Unlike the
    source name/type reference, that key is part of the governed runtime
    contract and must survive export/import unchanged.
    """
    metadata = connector_service.runtime_binding_from_config(value, "data_source")
    if metadata is None:
        return None
    return {
        "binding_key": str(metadata["binding_key"]),
        "reference": connector_service.with_required_capabilities(
            metadata["reference"], "sql_read"
        ),
    }


def _mapping_binding_fields(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    metadata = _mapping_binding_metadata(value)
    if metadata is None:
        return {}
    key_field, ref_field = connector_service.runtime_binding_fields("data_source")
    return {
        key_field: metadata["binding_key"],
        ref_field: metadata["reference"],
    }


def _source_ref_key(source: DataSource | None) -> str:
    ref = _source_ref(source)
    if ref.get("binding_required"):
        return "data-source/unbound"
    return f"data-source/{_key_token(ref.get('name'))}/{_key_token(ref.get('type'))}"


def _binding_placeholder(kind: str) -> dict[str, Any]:
    return {"kind": kind, "binding_required": True}


def _connector_ref(kind: str, connector: MCPConfig | LLMConfig | None) -> dict[str, Any]:
    """Export a safe logical external reference, never a configuration or id."""
    if connector is None:
        return _binding_placeholder(kind)
    if kind == "mcp":
        return {
            "kind": "mcp",
            "name": connector.name or "",
            "adapter": connector.transport or "",
            "required_capabilities": ["tool"],
        }
    capabilities = connector.capabilities if isinstance(connector.capabilities, list) else []
    return {
        "kind": "llm",
        "name": connector.name or "",
        "adapter": connector.provider or "",
        "required_capabilities": [str(item).strip().lower() for item in capabilities if str(item).strip()],
    }


def _portable_value(
    value: Any,
    *,
    internal_refs: dict[str, dict[str, str]],
    source_refs: dict[str, DataSource],
    mcp_refs: dict[str, MCPConfig],
    llm_refs: dict[str, LLMConfig],
) -> Any:
    """Redact a nested value and replace runtime IDs with portable references."""
    value = redact_sensitive(value)

    def walk(current: Any) -> Any:
        if isinstance(current, Mapping):
            portable: dict[str, Any] = {}
            for raw_key, raw_value in current.items():
                key = str(raw_key)
                normalized = _normalized_key_name(key)
                if normalized == "datasourceid":
                    source = source_refs.get(str(raw_value))
                    portable["data_source_ref"] = _source_ref(source)
                elif normalized == "mcpid":
                    # MCP credentials/configuration are never part of an ontology package.
                    portable["mcp_ref"] = _connector_ref("mcp", mcp_refs.get(str(raw_value)))
                elif normalized == "llmconfigid":
                    portable["llm_ref"] = _connector_ref("llm", llm_refs.get(str(raw_value)))
                elif normalized in _REFERENCE_ID_FIELDS:
                    ref_name, collection, singular = _REFERENCE_ID_FIELDS[normalized]
                    portable[ref_name] = internal_refs[collection].get(
                        str(raw_value), _binding_placeholder(singular)
                    )
                elif normalized == "triggeractionids":
                    values = raw_value if isinstance(raw_value, list) else []
                    portable["trigger_action_refs"] = [
                        internal_refs["actions"].get(str(item), _binding_placeholder("action"))
                        for item in values
                    ]
                else:
                    portable[key] = walk(raw_value)
            return portable
        if isinstance(current, list):
            return [walk(item) for item in current]
        return current

    return walk(value)


def _scenario_rows(
    db: Session,
    scenario_id: str,
    tenant_id: str | None,
) -> dict[str, list[Any]]:
    """Load only declarative resources; never export objects, logs or credentials."""
    with db.no_autoflush:
        entities = db.execute(
            select(OntologyEntity).where(OntologyEntity.scenario_id == scenario_id)
        ).scalars().all()
        entity_ids = [entity.id for entity in entities]
        properties = (
            db.execute(
                select(OntologyProperty).where(OntologyProperty.entity_id.in_(entity_ids))
            ).scalars().all()
            if entity_ids
            else []
        )
        relations = db.execute(
            select(OntologyRelation).where(OntologyRelation.scenario_id == scenario_id)
        ).scalars().all()
        mappings = db.execute(
            select(DataMapping).where(DataMapping.scenario_id == scenario_id)
        ).scalars().all()
        functions = db.execute(
            select(FunctionDefinition).where(FunctionDefinition.scenario_id == scenario_id)
        ).scalars().all()
        actions = db.execute(
            select(OntologyAction).where(OntologyAction.scenario_id == scenario_id)
        ).scalars().all()
        rules = db.execute(
            select(OntologyRule).where(OntologyRule.scenario_id == scenario_id)
        ).scalars().all()
        events = db.execute(
            select(OntologyEvent).where(OntologyEvent.scenario_id == scenario_id)
        ).scalars().all()
        workflows = db.execute(
            select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario_id)
        ).scalars().all()
        # A stale/manual mapping could point at a public source owned by another
        # tenant.  Such a source may be usable at runtime under ACL, but its name,
        # type and configuration relationship must not be copied into a package.
        # Query all *safe-scope* candidates because Actions can validly reference a
        # data source that has no DataMapping; only referenced entries are emitted.
        source_tenant_clause = (
            DataSource.tenant_id == tenant_id
            if tenant_id is not None
            else DataSource.tenant_id.is_(None)
        )
        sources = db.execute(
            select(DataSource).where(
                source_tenant_clause,
                or_(DataSource.scenario_id == scenario_id, DataSource.scenario_id.is_(None)),
            )
        ).scalars().all()
        mcps = db.execute(
            select(MCPConfig).where(MCPConfig.tenant_id == tenant_id)
        ).scalars().all()
        llms = db.execute(
            select(LLMConfig).where(LLMConfig.tenant_id == tenant_id)
        ).scalars().all()
    return {
        "entities": entities,
        "properties": properties,
        "relations": relations,
        "mappings": mappings,
        "functions": functions,
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": workflows,
        "sources": sources,
        "mcps": mcps,
        "llms": llms,
    }


def _build_resources(
    db: Session,
    scenario_id: str,
    *,
    tenant_id: str | None,
    include_runtime_ids: bool = False,
) -> dict[str, list[dict[str, Any]]] | tuple[
    dict[str, list[dict[str, Any]]], dict[str, dict[str, str]]
]:
    """Create resource-only package payloads for a scenario, in stable key order."""
    rows = _scenario_rows(db, scenario_id, tenant_id)
    entities: list[OntologyEntity] = rows["entities"]
    properties: list[OntologyProperty] = rows["properties"]
    relations: list[OntologyRelation] = rows["relations"]
    mappings: list[DataMapping] = rows["mappings"]
    functions: list[FunctionDefinition] = rows["functions"]
    actions: list[OntologyAction] = rows["actions"]
    rules: list[OntologyRule] = rows["rules"]
    events: list[OntologyEvent] = rows["events"]
    workflows: list[OntologyWorkflow] = rows["workflows"]
    source_by_id = {source.id: source for source in rows["sources"]}
    mcp_by_id = {config.id: config for config in rows["mcps"]}
    llm_by_id = {config.id: config for config in rows["llms"]}

    entity_keys = _assign_keys(
        entities,
        lambda entity: f"entity/{_key_token(entity.name)}",
        lambda entity: (str(entity.name), str(entity.description), str(entity.id)),
    )
    entity_key_by_id = {entity.id: entity_keys[id(entity)] for entity in entities}
    property_keys = _assign_keys(
        properties,
        lambda prop: f"property/{entity_key_by_id.get(prop.entity_id, 'entity/unresolved')}/{_key_token(prop.name)}",
        lambda prop: (str(prop.name), str(prop.data_type), str(prop.description), str(prop.id)),
    )
    relation_keys = _assign_keys(
        relations,
        lambda relation: (
            f"relation/{entity_key_by_id.get(relation.source_entity_id, 'entity/unresolved')}/"
            f"{_key_token(relation.name)}/{entity_key_by_id.get(relation.target_entity_id, 'entity/unresolved')}"
        ),
        lambda relation: (
            str(relation.name),
            str(relation.relation_type),
            str(relation.description),
            str(relation.id),
        ),
    )
    mapping_keys = _assign_keys(
        mappings,
        lambda mapping: (
            f"mapping/{entity_key_by_id.get(mapping.entity_id, 'entity/unresolved')}/"
            f"{_source_ref_key(source_by_id.get(mapping.data_source_id))}/{_key_token(mapping.table_name)}"
        ),
        lambda mapping: (
            str(mapping.table_name),
            _canonical_json(mapping.column_map or {}),
            str(mapping.id),
        ),
    )
    function_keys = _assign_keys(
        functions,
        lambda function: f"function/{_key_token(function.name)}",
        lambda function: (str(function.name), str(function.description), str(function.id)),
    )
    action_keys = _assign_keys(
        actions,
        lambda action: f"action/{entity_key_by_id.get(action.entity_id, 'entity/unresolved')}/{_key_token(action.name)}",
        lambda action: (str(action.name), str(action.description), str(action.id)),
    )
    rule_keys = _assign_keys(
        rules,
        lambda rule: f"rule/{entity_key_by_id.get(rule.entity_id, 'global')}/{_key_token(rule.name)}",
        lambda rule: (str(rule.name), str(rule.description), str(rule.id)),
    )
    event_keys = _assign_keys(
        events,
        lambda event: f"event/{_key_token(event.name)}",
        lambda event: (str(event.name), str(event.description), str(event.id)),
    )
    workflow_keys = _assign_keys(
        workflows,
        lambda workflow: f"workflow/{_key_token(workflow.name)}",
        lambda workflow: (str(workflow.name), str(workflow.description), str(workflow.id)),
    )

    internal_refs = {
        "entities": {entity.id: entity_keys[id(entity)] for entity in entities},
        "actions": {action.id: action_keys[id(action)] for action in actions},
        "rules": {rule.id: rule_keys[id(rule)] for rule in rules},
        "events": {event.id: event_keys[id(event)] for event in events},
    }

    resources: dict[str, list[dict[str, Any]]] = {
        "entities": [
            {
                "key": entity_keys[id(entity)],
                "name": entity.name,
                "description": entity.description or "",
                "icon": entity.icon or "box",
                "color": entity.color or "#4f46e5",
                "is_abstract": bool(entity.is_abstract),
            }
            for entity in entities
        ],
        "properties": [
            {
                "key": property_keys[id(prop)],
                "entity_ref": entity_key_by_id.get(prop.entity_id, "entity/unresolved"),
                "name": prop.name,
                "data_type": prop.data_type or "string",
                "description": prop.description or "",
                "is_key": bool(prop.is_key),
                "is_required": bool(prop.is_required),
                "is_enum": bool(prop.is_enum),
                "enum_values": _plain_json(prop.enum_values or []),
                "default_value": prop.default_value or "",
                "is_sensitive": bool(prop.is_sensitive),
            }
            for prop in properties
        ],
        "relations": [
            {
                "key": relation_keys[id(relation)],
                "name": relation.name,
                "source_entity_ref": entity_key_by_id.get(relation.source_entity_id, "entity/unresolved"),
                "target_entity_ref": entity_key_by_id.get(relation.target_entity_id, "entity/unresolved"),
                "relation_type": relation.relation_type or "1:N",
                "description": relation.description or "",
            }
            for relation in relations
        ],
        "mappings": [
            {
                "key": mapping_keys[id(mapping)],
                "entity_ref": entity_key_by_id.get(mapping.entity_id, "entity/unresolved"),
                "data_source_ref": _source_ref(source_by_id.get(mapping.data_source_id)),
                **_mapping_binding_fields(
                    {
                        "data_source_binding_key": mapping.data_source_binding_key or "",
                        "data_source_binding_ref": mapping.data_source_binding_ref or {},
                    }
                ),
                "table_name": mapping.table_name or "",
                "column_map": _plain_json(mapping.column_map or {}),
            }
            for mapping in mappings
        ],
        "functions": [
            {
                "key": function_keys[id(function)],
                "name": function.name,
                "description": function.description or "",
                "input_schema": _plain_json(function.input_schema or {}),
                "output_schema": _plain_json(function.output_schema or {}),
                "tags": _plain_json(function.tags or []),
                "visibility": function.visibility or "scenario",
            }
            for function in functions
        ],
        "actions": [
            {
                "key": action_keys[id(action)],
                "entity_ref": entity_key_by_id.get(action.entity_id, "entity/unresolved"),
                "name": action.name,
                "description": action.description or "",
                # JSON Schema property names are domain data, not platform
                # references.  Redact keys such as api_key but never rewrite a
                # legitimate business parameter named ``action_id``.
                "input_schema": redact_sensitive(_plain_json(action.input_schema or {})),
                "executor_type": action.executor_type or "sql",
                "executor_config": _portable_value(
                    action.executor_config or {},
                    internal_refs=internal_refs,
                    source_refs=source_by_id,
                    mcp_refs=mcp_by_id,
                    llm_refs=llm_by_id,
                ),
                "precondition": action.precondition or "",
                "postcondition": action.postcondition or "",
                "enabled": bool(action.enabled),
                "requires_confirmation": bool(action.requires_confirmation),
                "idempotency_required": bool(action.idempotency_required),
                "permission_scope": action.permission_scope or "scenario",
                "access_scope": action.access_scope or "tenant",
            }
            for action in actions
        ],
        "rules": [
            {
                "key": rule_keys[id(rule)],
                "entity_ref": entity_key_by_id.get(rule.entity_id) if rule.entity_id else None,
                "name": rule.name,
                "description": rule.description or "",
                "condition": redact_sensitive(_plain_json(rule.condition or {})),
                "action_on_match": redact_sensitive(rule.action_on_match or ""),
                "trigger_action_refs": [
                    internal_refs["actions"].get(str(action_id), _binding_placeholder("action"))
                    for action_id in (rule.trigger_action_ids or [])
                ],
                "severity": rule.severity or "info",
                "enabled": bool(rule.enabled),
            }
            for rule in rules
        ],
        "events": [
            {
                "key": event_keys[id(event)],
                "name": event.name,
                "description": event.description or "",
                "payload_schema": redact_sensitive(_plain_json(event.payload_schema or {})),
                "trigger_source": redact_sensitive(event.trigger_source or ""),
                "enabled": bool(event.enabled),
            }
            for event in events
        ],
        "workflows": [
            {
                "key": workflow_keys[id(workflow)],
                "name": workflow.name,
                "description": workflow.description or "",
                "trigger_type": workflow.trigger_type or "manual",
                "trigger_config": _portable_value(
                    workflow.trigger_config or {},
                    internal_refs=internal_refs,
                    source_refs=source_by_id,
                    mcp_refs=mcp_by_id,
                    llm_refs=llm_by_id,
                ),
                "steps": _portable_value(
                    workflow.steps or [],
                    internal_refs=internal_refs,
                    source_refs=source_by_id,
                    mcp_refs=mcp_by_id,
                    llm_refs=llm_by_id,
                ),
                "nodes": _portable_value(
                    workflow.nodes or [],
                    internal_refs=internal_refs,
                    source_refs=source_by_id,
                    mcp_refs=mcp_by_id,
                    llm_refs=llm_by_id,
                ),
                "edges": _portable_value(
                    workflow.edges or [],
                    internal_refs=internal_refs,
                    source_refs=source_by_id,
                    mcp_refs=mcp_by_id,
                    llm_refs=llm_by_id,
                ),
                "status": workflow.status or ("active" if workflow.enabled else "disabled"),
                "enabled": bool(workflow.enabled),
                "access_scope": workflow.access_scope or "tenant",
            }
            for workflow in workflows
        ],
    }
    for kind in RESOURCE_KINDS:
        resources[kind].sort(key=lambda item: str(item["key"]))
    if include_runtime_ids:
        # This map is private to the compiler.  It is never returned by export
        # or preview endpoints, which keeps package payloads portable and avoids
        # turning the import flow into a runtime-ID disclosure channel.
        return resources, {
            "entities": {entity_keys[id(item)]: item.id for item in entities},
            "properties": {property_keys[id(item)]: item.id for item in properties},
            "relations": {relation_keys[id(item)]: item.id for item in relations},
            "mappings": {mapping_keys[id(item)]: item.id for item in mappings},
            "functions": {function_keys[id(item)]: item.id for item in functions},
            "actions": {action_keys[id(item)]: item.id for item in actions},
            "rules": {rule_keys[id(item)]: item.id for item in rules},
            "events": {event_keys[id(item)]: item.id for item in events},
            "workflows": {workflow_keys[id(item)]: item.id for item in workflows},
        }
    return redact_sensitive(resources)


def _resolve_scenario(db: Session, scenario_or_id: BusinessScenario | str) -> BusinessScenario:
    if isinstance(scenario_or_id, BusinessScenario):
        return scenario_or_id
    with db.no_autoflush:
        scenario = db.get(BusinessScenario, str(scenario_or_id))
    if scenario is None:
        raise ValueError("业务场景不存在")
    return scenario


def export_scenario_package(
    db: Session,
    scenario_or_id: BusinessScenario | str,
) -> dict[str, Any]:
    """Export declarative scenario resources as deterministic, credential-free JSON.

    Caller-owned ACL checks intentionally stay in the router/governance layer.  This
    function reads only and does not depend on a tenant context, making it suitable
    for an approved background export too.
    """
    scenario = _resolve_scenario(db, scenario_or_id)
    resources = _build_resources(db, scenario.id, tenant_id=scenario.tenant_id)
    package: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "manifest": {
            "name": redact_sensitive(scenario.name or ""),
            "description": redact_sensitive(scenario.description or ""),
            "industry": redact_sensitive(scenario.industry or ""),
            "resource_counts": {kind: len(resources[kind]) for kind in RESOURCE_KINDS},
        },
        "resources": resources,
    }
    package["manifest"]["fingerprint"] = package_fingerprint(package)
    return redact_sensitive(package)


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _as_resource_list(
    resources: dict[str, Any],
    kind: str,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    raw = resources.get(kind)
    if raw is None:
        if kind not in _OPTIONAL_RESOURCE_KINDS:
            errors.append(_issue("missing_collection", f"resources.{kind}", "缺少资源集合"))
        return []
    if not isinstance(raw, list):
        errors.append(_issue("invalid_collection", f"resources.{kind}", "资源集合必须是数组"))
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"resources.{kind}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_issue("invalid_resource", path, "资源必须是对象"))
            continue
        normalized.append(redact_sensitive(dict(item)))
    return normalized


def _validate_resource_collections(
    resources: Mapping[str, Any],
    errors: list[dict[str, str]],
) -> None:
    """Reject resource collections this package version cannot materialize.

    Prior behavior normalized only the known collections, which made a supplied
    collection such as ``permissions`` or ``tests`` vanish from a preview and
    therefore from a proposal.  A package must either be fully understood by
    this version or fail validation with a machine-readable diagnostic.
    """
    for raw_kind in resources:
        kind = str(raw_kind)
        if kind not in RESOURCE_KINDS:
            errors.append(
                _issue(
                    "unsupported_resource_collection",
                    f"resources.{kind}",
                    f"当前资源包版本不支持资源集合: {kind}",
                )
            )


def _validate_resource_fields(
    resources: Mapping[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> None:
    """Reject unsupported top-level fields on a supported resource.

    Nested declarative values remain extensible by their own schemas.  This
    boundary only protects fields that the resource-package compiler would
    otherwise ignore.  Known runtime-ID spellings are left to the more specific
    portable-reference validation below so existing callers retain its explicit
    ``runtime_identifier_forbidden`` diagnostic.
    """
    for kind in RESOURCE_KINDS:
        allowed_fields = _RESOURCE_ALLOWED_FIELDS[kind]
        for index, item in enumerate(resources[kind]):
            path = f"resources.{kind}[{index}]"
            for raw_field in item:
                field = str(raw_field)
                if field in allowed_fields:
                    continue
                if _normalized_key_name(field) in _FORBIDDEN_RUNTIME_ID_FIELDS:
                    continue
                errors.append(
                    _issue(
                        "unsupported_resource_field",
                        f"{path}.{field}",
                        f"当前资源包版本不支持 {kind} 资源字段: {field}",
                    )
                )


def _reference_strings(value: Any, field: str) -> list[str]:
    if isinstance(value, Mapping):
        raw = value.get(field)
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str)]
    return []


def _walk_reference_fields(value: Any) -> list[tuple[str, str, str]]:
    """Return (collection, reference, JSON-like path) for package-local refs."""
    found: list[tuple[str, str, str]] = []

    def walk(current: Any, path: str) -> None:
        if isinstance(current, Mapping):
            for raw_key, child in current.items():
                key = str(raw_key)
                nested_path = f"{path}.{key}" if path else key
                collection = {
                    "action_ref": "actions",
                    "rule_ref": "rules",
                    "event_ref": "events",
                    "entity_ref": "entities",
                }.get(key)
                if collection and isinstance(child, str):
                    found.append((collection, child, nested_path))
                elif key == "trigger_action_refs" and isinstance(child, list):
                    for index, ref in enumerate(child):
                        if isinstance(ref, str):
                            found.append(("actions", ref, f"{nested_path}[{index}]"))
                walk(child, nested_path)
        elif isinstance(current, list):
            for index, child in enumerate(current):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return found


def _validate_resource_shape(
    resources: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> None:
    keys_by_kind: dict[str, set[str]] = {}
    for kind in RESOURCE_KINDS:
        seen: set[str] = set()
        prefix = _RESOURCE_PREFIXES[kind]
        for index, item in enumerate(resources[kind]):
            path = f"resources.{kind}[{index}]"
            for field in _REQUIRED_FIELDS[kind]:
                if item.get(field) in (None, ""):
                    errors.append(_issue("missing_field", f"{path}.{field}", "缺少必填字段"))
            key = item.get("key")
            if not isinstance(key, str) or not key:
                continue
            if not key.startswith(prefix):
                errors.append(_issue("invalid_key", f"{path}.key", f"资源 key 必须以 {prefix} 开头"))
            if key in seen:
                errors.append(_issue("duplicate_key", f"{path}.key", "同一资源集合中 key 重复"))
            seen.add(key)
        keys_by_kind[kind] = seen

    # References must resolve inside the package.  External data source / MCP / LLM
    # bindings are intentionally not package resources and are handled as import
    # conflicts instead of being mistaken for an internal ontology reference.
    for kind in RESOURCE_KINDS:
        for index, item in enumerate(resources[kind]):
            base_path = f"resources.{kind}[{index}]"
            for collection, reference, relative_path in _walk_reference_fields(item):
                if reference not in keys_by_kind[collection]:
                    errors.append(
                        _issue(
                            "unknown_reference",
                            f"{base_path}.{relative_path}",
                            f"引用的 {collection} 资源不存在: {reference}",
                        )
                    )


def _normalize_mapping_binding_fields(
    resources: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> None:
    """Validate and canonicalize optional safe mapping runtime metadata."""
    key_field, ref_field = connector_service.runtime_binding_fields("data_source")
    for index, item in enumerate(resources.get("mappings", [])):
        path = f"resources.mappings[{index}]"
        try:
            fields = _mapping_binding_fields(item)
        except connector_service.ConnectorBindingError as exc:
            errors.append(
                _issue(
                    "invalid_mapping_runtime_binding",
                    path,
                    f"数据映射运行时绑定配置无效: {exc}",
                )
            )
            continue
        # Discard unknown/ref-only representation after extracting the compact
        # supported form.  The package never carries names, IDs or credentials
        # in this runtime compatibility descriptor.
        item.pop(key_field, None)
        item.pop(ref_field, None)
        item.update(fields)


def _normalize_function_fields(
    resources: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> None:
    """Use the same declaration-only schema validator as the CRUD/release path."""
    for index, item in enumerate(resources.get("functions", [])):
        path = f"resources.functions[{index}]"
        try:
            declaration = function_definition_service.normalize_definition(
                {key: value for key, value in item.items() if key != "key"}
            )
        except function_definition_service.FunctionDefinitionError as exc:
            errors.append(_issue("invalid_function_definition", path, str(exc)))
            continue
        item.update(declaration)


def _validate_no_runtime_identifier_fields(
    resources: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> None:
    """Require portable refs in formal resource-reference positions.

    We intentionally do *not* recurse through arbitrary action parameters, rule
    conditions or JSON Schema property names: a domain can legitimately have a
    business field named ``entity_id``.  Only the platform's own reference slots
    are forbidden from carrying an environment-specific ID.
    """
    def forbid_if_present(container: Mapping[str, Any], field: str, path: str) -> None:
        if field in container:
            errors.append(
                _issue(
                    "runtime_identifier_forbidden",
                    f"{path}.{field}",
                    "资源包不能携带运行时 ID；请使用 package-local *_ref 或外部绑定",
                )
            )

    for kind in RESOURCE_KINDS:
        for index, item in enumerate(resources[kind]):
            root = f"resources.{kind}[{index}]"
            for raw_key in item:
                if _normalized_key_name(raw_key) in _FORBIDDEN_RUNTIME_ID_FIELDS:
                    forbid_if_present(item, str(raw_key), root)

            if kind == "actions":
                config = item.get("executor_config")
                if isinstance(config, Mapping):
                    for field in ("data_source_id", "mcp_id", "llm_config_id"):
                        forbid_if_present(config, field, f"{root}.executor_config")

            if kind == "workflows":
                trigger = item.get("trigger_config")
                if isinstance(trigger, Mapping):
                    forbid_if_present(trigger, "event_id", f"{root}.trigger_config")
                for collection in ("steps", "nodes"):
                    values = item.get(collection)
                    if not isinstance(values, list):
                        continue
                    for child_index, node_or_step in enumerate(values):
                        if not isinstance(node_or_step, Mapping):
                            continue
                        target = node_or_step.get("data") if collection == "nodes" else node_or_step
                        if not isinstance(target, Mapping):
                            continue
                        node_type = str(node_or_step.get("type") or target.get("type") or "")
                        raw_field = {
                            "action": "action_id",
                            "rule": "rule_id",
                            "event": "event_id",
                        }.get(node_type)
                        if raw_field:
                            forbid_if_present(target, raw_field, f"{root}.{collection}[{child_index}]")


def validate_package(package: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Validate untrusted package JSON and return a redacted normalized result.

    Invalid input does not raise; callers can show ``errors`` in a proposal UI.
    ``normalized`` is always safe to render/log and is the only package representation
    used by ``plan_package_import``.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(package, Mapping):
        normalized: dict[str, Any] = {"format": "", "version": "", "manifest": {}, "resources": {}}
        errors.append(_issue("invalid_package", "", "资源包必须是 JSON 对象"))
        for kind in RESOURCE_KINDS:
            normalized["resources"][kind] = []
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "normalized": normalized,
            "fingerprint": package_fingerprint(normalized),
        }

    safe = redact_sensitive(dict(package))
    manifest = safe.get("manifest")
    resources_raw = safe.get("resources")
    if not isinstance(manifest, Mapping):
        errors.append(_issue("invalid_manifest", "manifest", "manifest 必须是对象"))
        manifest = {}
    else:
        manifest = dict(manifest)
    if not isinstance(resources_raw, Mapping):
        errors.append(_issue("invalid_resources", "resources", "resources 必须是对象"))
        resources_raw = {}
    else:
        resources_raw = dict(resources_raw)
        _validate_resource_collections(resources_raw, errors)

    normalized_resources: dict[str, list[dict[str, Any]]] = {}
    for kind in RESOURCE_KINDS:
        normalized_resources[kind] = _as_resource_list(resources_raw, kind, errors)
        normalized_resources[kind].sort(key=lambda item: str(item.get("key", "")))
    normalized = {
        "format": safe.get("format", ""),
        "version": safe.get("version", ""),
        "manifest": manifest,
        "resources": normalized_resources,
    }

    if normalized["format"] != PACKAGE_FORMAT:
        errors.append(_issue("unsupported_format", "format", f"仅支持 {PACKAGE_FORMAT}"))
    if normalized["version"] != PACKAGE_VERSION:
        errors.append(_issue("unsupported_version", "version", f"仅支持版本 {PACKAGE_VERSION}"))
    if not isinstance(manifest.get("name"), str) or not manifest.get("name", "").strip():
        errors.append(_issue("missing_field", "manifest.name", "缺少资源包名称"))

    _validate_resource_shape(normalized_resources, errors)
    _validate_resource_fields(normalized_resources, errors)
    _normalize_mapping_binding_fields(normalized_resources, errors)
    _normalize_function_fields(normalized_resources, errors)
    _validate_no_runtime_identifier_fields(normalized_resources, errors)
    actual_fingerprint = package_fingerprint(normalized)
    supplied_fingerprint = manifest.get("fingerprint")
    if supplied_fingerprint:
        if supplied_fingerprint != actual_fingerprint:
            errors.append(_issue("fingerprint_mismatch", "manifest.fingerprint", "资源包指纹不匹配"))
    else:
        warnings.append(_issue("fingerprint_missing", "manifest.fingerprint", "资源包未提供完整性指纹"))

    # Canonicalize the supplied value so a router can return a consistent package
    # preview even when a caller omitted the fingerprint.
    normalized["manifest"]["fingerprint"] = actual_fingerprint
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": redact_sensitive(normalized),
        "fingerprint": actual_fingerprint,
    }


def _require_verified_package_fingerprint(package: Mapping[str, Any] | Any) -> None:
    """Require an uploaded package's original fingerprint at the write boundary.

    Preview intentionally remains useful for a legacy upload with no fingerprint:
    ``validate_package`` reports that condition as a warning and returns a safe
    canonical form.  A governance proposal is different: it records a durable
    artifact, so it must only be created from the exact signed/canonical package
    submitted by the caller.  In particular, do not accept the normalized preview
    merely because normalization can calculate a previously omitted fingerprint.
    """
    if not isinstance(package, Mapping):
        raise PackageImportError("资源包必须是 JSON 对象，不能创建导入提案")
    manifest = package.get("manifest")
    if not isinstance(manifest, Mapping):
        raise PackageImportError("资源包缺少 manifest 完整性指纹，不能创建导入提案")
    supplied = manifest.get("fingerprint")
    if not isinstance(supplied, str) or not supplied.strip():
        raise PackageImportError("资源包缺少完整性指纹，不能创建导入提案")
    if supplied != package_fingerprint(package):
        raise PackageImportError("资源包完整性指纹不匹配，不能创建导入提案")


def _target_data_sources(db: Session, target: BusinessScenario) -> list[DataSource]:
    """Return only same-tenant target/global sources as rebind candidates.

    The router will still perform ACL checks, but the service itself must not turn a
    package preview into a cross-tenant data-source existence oracle.
    """
    tenant_clause = (
        DataSource.tenant_id == target.tenant_id
        if target.tenant_id is not None
        else DataSource.tenant_id.is_(None)
    )
    with db.no_autoflush:
        return db.execute(
            select(DataSource).where(
                tenant_clause,
                or_(DataSource.scenario_id == target.id, DataSource.scenario_id.is_(None)),
            )
        ).scalars().all()


def _strip_duplicate_suffix(key: str) -> str:
    return re.sub(r"~\d+$", "", key)


def _resource_internal_refs(kind: str, item: Mapping[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    direct = {
        "properties": (("entities", "entity_ref"),),
        "relations": (("entities", "source_entity_ref"), ("entities", "target_entity_ref")),
        "mappings": (("entities", "entity_ref"),),
        "actions": (("entities", "entity_ref"),),
        "rules": (("entities", "entity_ref"),),
    }
    for collection, field in direct.get(kind, ()):
        value = item.get(field)
        if isinstance(value, str):
            refs.append((collection, value))
    for collection, reference, _path in _walk_reference_fields(item):
        if (collection, reference) not in refs:
            refs.append((collection, reference))
    return refs


def _external_binding_conflicts(
    db: Session,
    target: BusinessScenario,
    item: Mapping[str, Any],
    data_sources: list[DataSource],
    *,
    environment: str,
    path: str = "",
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve governed bindings, retaining only a safe legacy data-source fallback.

    A v1 package may still refer to a same-tenant data source by name/type.  That
    path remains compatible so upgrades do not abruptly stop existing imports.
    Once an explicit environment binding exists, however, it always wins and its
    health/configuration state is rechecked rather than silently falling back.
    """
    conflicts: list[dict[str, str]] = []
    requirements: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    source_index: dict[tuple[str, str], list[DataSource]] = defaultdict(list)
    for source in data_sources:
        source_index[(str(source.name or ""), str(source.type or ""))].append(source)

    def add(code: str, nested_path: str, message: str, requirement: dict[str, Any]) -> None:
        conflicts.append(_issue(code, nested_path, message))
        if requirement not in requirements:
            requirements.append(requirement)

    def safe_requirement(
        kind: str,
        reference: Mapping[str, Any],
        nested_path: str,
        *,
        binding_key_value: str | None = None,
    ) -> dict[str, Any]:
        return {
            "binding_key": binding_key_value or connector_service.binding_key(kind, reference, nested_path),
            "kind": kind,
            "path": nested_path,
            "reference_label": connector_service.binding_label(kind, reference, nested_path),
            "ref": redact_sensitive(dict(reference)),
            "environment": environment,
        }

    def add_resolved(
        resolution: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        legacy: bool = False,
    ) -> None:
        item = {
            "binding_key": str(resolution["binding_key"]),
            "kind": str(resolution["kind"]),
            "path": str(resolution["path"]),
            "reference_label": str(resolution.get("reference_label") or ""),
            "environment": environment,
            "legacy_fallback": legacy,
            "reference": redact_sensitive(dict(reference)),
        }
        if item not in resolved:
            resolved.append(item)

    def resolve(
        kind: str,
        reference: Any,
        nested_path: str,
        *,
        binding_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(reference, Mapping):
            requirement = safe_requirement(kind, {}, nested_path)
            add(
                "invalid_data_source_ref" if kind == "data_source" else "invalid_connector_ref",
                nested_path,
                "数据源引用必须是对象" if kind == "data_source" else "外部连接器引用必须是对象",
                requirement,
            )
            return
        reference_dict = dict(reference)
        compatibility_reference = (
            dict(binding_metadata.get("reference") or {})
            if isinstance(binding_metadata, Mapping)
            else reference_dict
        )
        binding_key_value = (
            str(binding_metadata.get("binding_key") or "")
            if isinstance(binding_metadata, Mapping)
            else None
        )
        requirement = safe_requirement(
            kind,
            compatibility_reference,
            nested_path,
            binding_key_value=binding_key_value,
        )
        resolution = connector_service.requirement_resolution(
            db,
            target,
            environment=environment,
            kind=kind,
            reference=compatibility_reference,
            path=nested_path,
            binding_key_value=binding_key_value,
        )
        if resolution["resolved"]:
            add_resolved(resolution, reference=compatibility_reference)
            return

        # Legacy v1 data-source packages only carried a portable name/type.  A
        # unique same-tenant target remains safe, but emits a warning that the
        # stronger explicit environment binding has not yet been configured.
        can_fallback = (
            kind == "data_source"
            and binding_metadata is None
            and not bool(reference_dict.get("binding_required"))
            and not bool(resolution.get("configured"))
        )
        source_name = str(reference_dict.get("name") or "")
        source_type = str(reference_dict.get("type") or "")
        candidates = source_index[(source_name, source_type)] if can_fallback else []
        if len(candidates) == 1:
            add_resolved(
                {
                    "binding_key": requirement["binding_key"],
                    "kind": kind,
                    "path": nested_path,
                    "reference_label": requirement["reference_label"],
                },
                reference=compatibility_reference,
                legacy=True,
            )
            warning = _issue(
                "legacy_binding_fallback",
                nested_path,
                "使用兼容的数据源名称/类型匹配；建议在目标环境建立并验证显式连接器绑定",
            )
            if warning not in warnings:
                warnings.append(warning)
            return
        if kind == "data_source" and can_fallback:
            if not candidates:
                add(
                    "missing_data_source",
                    nested_path,
                    f"目标场景未找到数据源: {source_name} ({source_type})",
                    requirement,
                )
            else:
                add(
                    "ambiguous_data_source",
                    nested_path,
                    f"目标场景存在多个同名数据源: {source_name} ({source_type})",
                    requirement,
                )
            return
        add(
            "connector_unavailable" if bool(resolution.get("configured")) else "binding_required",
            nested_path,
            str(resolution.get("reason") or "导入前必须绑定外部资源"),
            requirement,
        )

    def walk(current: Any, current_path: str) -> None:
        if isinstance(current, Mapping):
            for raw_key, child in current.items():
                key = str(raw_key)
                nested_path = f"{current_path}.{key}" if current_path else key
                if key == "data_source_ref":
                    metadata: dict[str, Any] | None = None
                    if current is item:
                        try:
                            metadata = _mapping_binding_metadata(item)
                        except connector_service.ConnectorBindingError:
                            # validate_package reports malformed mapping metadata;
                            # keeping preview non-throwing here avoids duplicate
                            # target-specific diagnostics for an invalid package.
                            metadata = None
                    resolve(
                        "data_source",
                        child,
                        nested_path,
                        binding_metadata=metadata,
                    )
                    continue
                if key in {"mcp_ref", "llm_ref"}:
                    resolve("mcp" if key == "mcp_ref" else "llm", child, nested_path)
                    continue
                walk(child, nested_path)
        elif isinstance(current, list):
            for index, child in enumerate(current):
                walk(child, f"{current_path}[{index}]")

    walk(item, path)
    return conflicts, requirements, resolved, warnings


def _redacted_configuration_paths(kind: str, item: Mapping[str, Any]) -> list[str]:
    """Locate credential placeholders that cannot configure a new runtime item.

    Packages intentionally retain the *shape* of a secret as ``[REDACTED]`` so a
    reviewer can see that something requires attention.  That value must never be
    mistaken for usable credentials when an import creates a new Action or
    workflow.  Existing resources may safely retain their current secret through
    the release snapshot marker mechanism.
    """
    roots: dict[str, tuple[str, ...]] = {
        "actions": ("executor_config",),
        "workflows": ("trigger_config", "steps", "nodes", "edges"),
    }
    paths: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if "[REDACTED]" in value:
                paths.append(path)
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    for root in roots.get(kind, ()):
        if root in item:
            walk(item[root], root)
    return paths


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(
        key for key in keys if key != "key" and before.get(key) != after.get(key)
    )


def _resource_indexes(resources: Mapping[str, list[dict[str, Any]]]) -> tuple[
    dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, int]]
]:
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    base_counts: dict[str, dict[str, int]] = {}
    for kind in RESOURCE_KINDS:
        indexed[kind] = {str(item["key"]): item for item in resources[kind]}
        counts: dict[str, int] = defaultdict(int)
        for item in resources[kind]:
            counts[_strip_duplicate_suffix(str(item["key"]))] += 1
        base_counts[kind] = dict(counts)
    return indexed, base_counts


def plan_package_import(
    db: Session,
    target_scenario_or_id: BusinessScenario | str,
    package: Mapping[str, Any] | Any,
    *,
    environment: str = "dev",
) -> dict[str, Any]:
    """Build a no-write import proposal with deterministic diffs and conflicts.

    The function intentionally does not call ``add``, ``flush``, ``commit`` or
    ``rollback``.  A future approved apply endpoint can consume ``proposal.changes``
    only after the caller has resolved every conflict/binding.
    """
    target = _resolve_scenario(db, target_scenario_or_id)
    try:
        target_environment = connector_service.normalize_environment(environment)
    except connector_service.ConnectorBindingError as exc:
        raise PackageImportError(str(exc)) from exc
    validation = validate_package(package)
    proposal: dict[str, Any] = {
        "mode": "preview",
        "mutates_target": False,
        "ready_to_apply": False,
        "target": {"id": target.id, "name": redact_sensitive(target.name or "")},
        "environment": target_environment,
        "changes": [],
        "conflicts": [],
        "required_bindings": [],
        "resolved_bindings": [],
        "summary": {"create": 0, "update": 0, "unchanged": 0, "conflict": 0},
    }
    result: dict[str, Any] = {
        "valid": validation["valid"],
        # ``valid`` means the package format/references are sound.  ``applicable``
        # additionally requires target-specific bindings/conflicts to be resolved.
        "applicable": False,
        "target_scenario_id": target.id,
        "environment": target_environment,
        "package_fingerprint": validation["fingerprint"],
        "errors": validation["errors"],
        "warnings": validation["warnings"],
        "proposal": proposal,
    }
    if not validation["valid"]:
        proposal["conflicts"] = [
            {
                "resource_type": "package",
                "key": "",
                **error,
            }
            for error in validation["errors"]
        ]
        proposal["summary"]["conflict"] = len(proposal["conflicts"])
        return redact_sensitive(result)

    desired = validation["normalized"]["resources"]
    current = _build_resources(db, target.id, tenant_id=target.tenant_id)
    current_index, current_base_counts = _resource_indexes(current)
    target_sources = _target_data_sources(db, target)
    desired_states: dict[tuple[str, str], str] = {}

    for kind in RESOURCE_KINDS:
        for item in desired[kind]:
            key = str(item["key"])
            conflicts: list[dict[str, str]] = []
            requirements: list[dict[str, Any]] = []
            before = current_index[kind].get(key)
            effective_item: Mapping[str, Any] = item
            # A legacy package does not express a mapping runtime binding.  If
            # the target already has one, absence means "leave it unchanged",
            # not "derive a new key from source name/type".  Use the prior
            # metadata consistently for preview, rebind lookup and compilation.
            if kind == "mappings" and before is not None:
                try:
                    incoming_binding = _mapping_binding_metadata(item)
                    existing_binding = _mapping_binding_metadata(before)
                except connector_service.ConnectorBindingError:
                    incoming_binding = None
                    existing_binding = None
                if incoming_binding is None and existing_binding is not None:
                    effective_item = dict(item)
                    effective_item.update(_mapping_binding_fields(before))
            base_key = _strip_duplicate_suffix(key)
            if current_base_counts[kind].get(base_key, 0) > 1:
                conflicts.append(
                    _issue(
                        "ambiguous_target_match",
                        "key",
                        "目标场景存在多个相同语义 key 的资源，不能安全决定更新对象",
                    )
                )
            for collection, reference in _resource_internal_refs(kind, item):
                if desired_states.get((collection, reference)) == "conflict":
                    conflicts.append(
                        _issue(
                            "blocked_dependency",
                            "reference",
                            f"依赖资源存在冲突: {reference}",
                        )
                    )
            external_conflicts, external_requirements, external_resolved, external_warnings = _external_binding_conflicts(
                db,
                target,
                effective_item,
                target_sources,
                environment=target_environment,
                path=f"resources.{kind}.{key}",
            )
            conflicts.extend(external_conflicts)
            requirements.extend(external_requirements)
            for resolved_binding in external_resolved:
                if resolved_binding not in proposal["resolved_bindings"]:
                    proposal["resolved_bindings"].append(resolved_binding)
            for warning in external_warnings:
                if warning not in result["warnings"]:
                    result["warnings"].append(warning)

            if before is None:
                for redacted_path in _redacted_configuration_paths(kind, effective_item):
                    conflicts.append(
                        _issue(
                            "redacted_configuration",
                            redacted_path,
                            "新资源包含已脱敏的运行配置；请先在目标环境完成凭据/连接器绑定",
                        )
                    )
                    requirement = {
                        "kind": "secret",
                        "path": f"resources.{kind}.{key}.{redacted_path}",
                    }
                    if requirement not in requirements:
                        requirements.append(requirement)
            if conflicts:
                operation = "conflict"
                changed_fields = _changed_fields(before, effective_item) if before else []
            elif before is None:
                operation = "create"
                changed_fields = []
            elif before == effective_item:
                operation = "unchanged"
                changed_fields = []
            else:
                operation = "update"
                changed_fields = _changed_fields(before, effective_item)

            change: dict[str, Any] = {
                "resource_type": _SINGULAR_RESOURCE_NAMES[kind],
                "key": key,
                "operation": operation,
                "changed_fields": changed_fields,
                "after": effective_item,
            }
            if before is not None:
                change["before"] = before
            if conflicts:
                change["conflicts"] = conflicts
                for conflict in conflicts:
                    proposal["conflicts"].append(
                        {"resource_type": change["resource_type"], "key": key, **conflict}
                    )
            for requirement in requirements:
                if requirement not in proposal["required_bindings"]:
                    proposal["required_bindings"].append(requirement)
            proposal["changes"].append(change)
            proposal["summary"][operation] += 1
            desired_states[(kind, key)] = operation

    proposal["ready_to_apply"] = not proposal["conflicts"]
    result["applicable"] = proposal["ready_to_apply"]
    return redact_sensitive(result)


def _new_runtime_id() -> str:
    """Generate a release-snapshot ID without importing an environment ID."""
    return uuid4().hex


def _materialize_source_id(
    source_ref: Any,
    sources_by_name_type: Mapping[tuple[str, str], list[DataSource]],
    *,
    binding_targets: Mapping[tuple[str, str], str],
    path: str,
    binding_key_value: str | None = None,
) -> str:
    if not isinstance(source_ref, Mapping):
        raise PackageImportConflictError("导入前必须绑定数据源")
    target_id = binding_targets.get(
        (
            "data_source",
            binding_key_value or connector_service.binding_key("data_source", source_ref, path),
        )
    )
    if target_id:
        return target_id
    # ``binding_required`` is emitted by a safe export when the source could
    # not be represented portably.  It must block the legacy name/type fallback,
    # but an explicit, revalidated environment binding is sufficient to
    # materialize it.  Checking it before ``binding_targets`` would leave the
    # UI's recovery path permanently blocked.
    if source_ref.get("binding_required") is True:
        raise PackageImportConflictError("导入前必须绑定数据源")
    key = (str(source_ref.get("name") or ""), str(source_ref.get("type") or ""))
    candidates = sources_by_name_type.get(key, [])
    if len(candidates) != 1:
        raise PackageImportConflictError("目标数据源绑定已变化，请重新执行预检")
    return candidates[0].id


def _attach_runtime_binding(
    materialized: dict[str, Any],
    *,
    kind: str,
    reference: Mapping[str, Any],
    path: str,
    binding_targets: Mapping[tuple[str, str], str],
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Persist the safe logical key next to an imported physical connector ID.

    The physical ID is still useful for dev compatibility and snapshot review,
    but it cannot select a different staging/prod target.  These two fields
    retain only the key, adapter and required capabilities—never names,
    endpoints or credentials—and are consumed by the runtime resolver.
    """
    resolved_metadata = (
        {
            "binding_key": str(metadata["binding_key"]),
            "reference": dict(metadata.get("reference") or {}),
        }
        if isinstance(metadata, Mapping)
        else connector_service.runtime_binding_metadata(kind, reference, path)
    )
    identity = (kind, str(resolved_metadata["binding_key"]))
    if identity not in binding_targets:
        # This is the legacy same-name data-source fallback.  Do not pretend it
        # is a governed environment binding; dev keeps the old direct-ID path.
        return
    key_field, ref_field = connector_service.runtime_binding_fields(kind)
    materialized[key_field] = resolved_metadata["binding_key"]
    materialized[ref_field] = resolved_metadata["reference"]


def _materialize_package_value(
    value: Any,
    *,
    entity_ids: Mapping[str, str],
    action_ids: Mapping[str, str],
    rule_ids: Mapping[str, str],
    event_ids: Mapping[str, str],
    sources_by_name_type: Mapping[tuple[str, str], list[DataSource]],
    binding_targets: Mapping[tuple[str, str], str],
    path: str = "",
) -> Any:
    """Replace portable refs with target-local IDs for a release snapshot.

    The input has already gone through ``validate_package``.  Still fail closed
    here: the target could have changed between preview and proposal creation, and
    this function is the last line that prevents a portable binding placeholder
    from becoming a live runtime reference.
    """
    if isinstance(value, str):
        # Release snapshots use a marker rather than the package's textual
        # redaction.  On an update it preserves the existing secret; for a new
        # executable resource release_service rejects it rather than persisting a
        # fake credential.
        if "[REDACTED]" in value:
            return copy.deepcopy(release_service._SECRET_MARKER)
        return value
    if isinstance(value, list):
        return [
            _materialize_package_value(
                item,
                entity_ids=entity_ids,
                action_ids=action_ids,
                rule_ids=rule_ids,
                event_ids=event_ids,
                sources_by_name_type=sources_by_name_type,
                binding_targets=binding_targets,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)

    materialized: dict[str, Any] = {}
    reference_maps: dict[str, tuple[str, Mapping[str, str]]] = {
        "entity_ref": ("entity_id", entity_ids),
        "action_ref": ("action_id", action_ids),
        "rule_ref": ("rule_id", rule_ids),
        "event_ref": ("event_id", event_ids),
    }
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        nested_path = f"{path}.{key}" if path else key
        if key == "data_source_ref":
            if not isinstance(raw_value, Mapping):
                raise PackageImportConflictError("外部数据源引用已失效，请重新执行预检")
            materialized["data_source_id"] = _materialize_source_id(
                raw_value,
                sources_by_name_type,
                binding_targets=binding_targets,
                path=nested_path,
            )
            _attach_runtime_binding(
                materialized,
                kind="data_source",
                reference=raw_value,
                path=nested_path,
                binding_targets=binding_targets,
            )
            continue
        if key in reference_maps:
            target_key, refs = reference_maps[key]
            if not isinstance(raw_value, str) or raw_value not in refs:
                raise PackageImportConflictError("资源包内部引用已失效，请重新执行预检")
            materialized[target_key] = refs[raw_value]
            continue
        if key == "trigger_action_refs":
            if not isinstance(raw_value, list) or any(
                not isinstance(item, str) or item not in action_ids for item in raw_value
            ):
                raise PackageImportConflictError("资源包中的 Action 引用已失效，请重新执行预检")
            materialized["trigger_action_ids"] = [action_ids[item] for item in raw_value]
            continue
        if key in {"mcp_ref", "llm_ref"}:
            kind = "mcp" if key == "mcp_ref" else "llm"
            if not isinstance(raw_value, Mapping):
                raise PackageImportConflictError("外部连接器引用已失效，请重新执行预检")
            target_id = binding_targets.get(
                (kind, connector_service.binding_key(kind, raw_value, nested_path))
            )
            if not target_id:
                raise PackageImportConflictError("外部 MCP/模型环境绑定已变化，请重新执行预检")
            materialized["mcp_id" if kind == "mcp" else "llm_config_id"] = target_id
            _attach_runtime_binding(
                materialized,
                kind=kind,
                reference=raw_value,
                path=nested_path,
                binding_targets=binding_targets,
            )
            continue
        materialized[key] = _materialize_package_value(
            raw_value,
            entity_ids=entity_ids,
            action_ids=action_ids,
            rule_ids=rule_ids,
            event_ids=event_ids,
            sources_by_name_type=sources_by_name_type,
            binding_targets=binding_targets,
            path=nested_path,
        )
    return materialized


def _replace_record(records: list[dict[str, Any]], item_id: str, replacement: dict[str, Any]) -> None:
    for record in records:
        if record.get("id") == item_id:
            record.clear()
            record.update(replacement)
            return
    records.append(replacement)


def _resolved_binding_targets(
    db: Session,
    target: BusinessScenario,
    plan: Mapping[str, Any],
    *,
    environment: str,
) -> dict[tuple[str, str], str]:
    """Repeat every explicit binding lookup at the proposal write boundary."""
    targets: dict[tuple[str, str], str] = {}
    proposal = plan.get("proposal") if isinstance(plan, Mapping) else None
    bindings = proposal.get("resolved_bindings", []) if isinstance(proposal, Mapping) else []
    for item in bindings if isinstance(bindings, list) else []:
        if not isinstance(item, Mapping) or item.get("legacy_fallback"):
            continue
        kind = str(item.get("kind") or "")
        key = str(item.get("binding_key") or "")
        reference = item.get("reference") if isinstance(item.get("reference"), Mapping) else None
        try:
            _binding, connector = connector_service.require_ready_binding(
                db,
                target,
                environment=environment,
                binding_key_value=key,
                kind=kind,
                reference=reference,
            )
        except connector_service.ConnectorBindingError as exc:
            raise PackageImportConflictError(str(exc)) from exc
        targets[(kind, key)] = str(connector.id)
    return targets


def _overlay_connector_requirements(
    content: dict[str, Any],
    plan: Mapping[str, Any],
    *,
    environment: str,
) -> None:
    """Persist only logical requirements, never physical IDs or credentials."""
    existing = content.get("connector_bindings")
    try:
        requirements = connector_service.normalize_snapshot_binding_requirements(existing)
    except connector_service.ConnectorBindingError as exc:
        raise PackageImportConflictError(str(exc)) from exc
    proposal = plan.get("proposal") if isinstance(plan, Mapping) else None
    resolved = proposal.get("resolved_bindings", []) if isinstance(proposal, Mapping) else []
    for item in resolved if isinstance(resolved, list) else []:
        if not isinstance(item, Mapping) or item.get("legacy_fallback"):
            continue
        requirement = {
            "binding_key": str(item.get("binding_key") or ""),
            "kind": str(item.get("kind") or ""),
            "environment": environment,
            "reference_label": str(item.get("reference_label") or ""),
        }
        try:
            normalized = connector_service.normalize_snapshot_binding_requirements([requirement])[0]
        except connector_service.ConnectorBindingError as exc:
            raise PackageImportConflictError(str(exc)) from exc
        requirements = [
            current
            for current in requirements
            if not (
                current["environment"] == normalized["environment"]
                and current["kind"] == normalized["kind"]
                and current["binding_key"] == normalized["binding_key"]
            )
        ]
        requirements.append(normalized)
    if requirements:
        content["connector_bindings"] = connector_service.normalize_snapshot_binding_requirements(requirements)


def _compile_package_overlay(
    db: Session,
    target: BusinessScenario,
    package: Mapping[str, Any] | Any,
    base_content: Mapping[str, Any],
    *,
    environment: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile an applicable portable package into a full branch-head overlay.

    A release snapshot is a desired-state document.  Starting from the complete
    head snapshot is therefore essential: package absence means "leave target
    unchanged", never "delete target resource".  The function only creates an
    in-memory document; the caller decides whether to create an immutable proposal.
    """
    # Unlike preview, proposal compilation must not manufacture integrity metadata
    # for an uploaded package.  Check the caller's original payload before
    # validation replaces/normalizes its manifest fingerprint.
    _require_verified_package_fingerprint(package)
    validation = validate_package(package)
    if not validation["valid"]:
        raise PackageImportError("资源包格式校验未通过，不能创建导入提案")
    plan = plan_package_import(db, target, validation["normalized"], environment=environment)
    if not plan["applicable"]:
        raise PackageImportConflictError("资源包仍有冲突、待绑定项或不可物化的脱敏配置")

    built = _build_resources(
        db, target.id, tenant_id=target.tenant_id, include_runtime_ids=True
    )
    if not isinstance(built, tuple):  # Defensive: this is a private call contract.
        raise PackageImportError("无法建立目标资源索引")
    _current_resources, current_ids = built
    current_mapping_by_key = {
        str(item.get("key") or ""): item
        for item in _current_resources.get("mappings", [])
        if isinstance(item, Mapping)
    }
    desired = validation["normalized"]["resources"]

    # Allocate all target-local IDs first so nested refs can point forward within
    # the package (for example an Action may refer to another Action declared
    # later in the sorted package list).
    ids_by_kind: dict[str, dict[str, str]] = {}
    for kind in RESOURCE_KINDS:
        ids_by_kind[kind] = {
            str(item["key"]): current_ids[kind].get(str(item["key"]), _new_runtime_id())
            for item in desired[kind]
        }

    sources_by_name_type: dict[tuple[str, str], list[DataSource]] = defaultdict(list)
    for source in _target_data_sources(db, target):
        sources_by_name_type[(str(source.name or ""), str(source.type or ""))].append(source)
    binding_targets = _resolved_binding_targets(db, target, plan, environment=environment)

    content = release_service.normalize_snapshot_content(dict(base_content))
    # New imports always start recording mappings in the generated snapshot.  The
    # apply path deliberately treats them as an add/update overlay so this does
    # not make a partial package erase a legacy target mapping.
    content.setdefault("mappings", [])
    content.setdefault("functions", [])
    content.setdefault("entities", [])
    content.setdefault("relations", [])
    content.setdefault("actions", [])
    content.setdefault("rules", [])
    content.setdefault("events", [])
    content.setdefault("workflows", [])
    _overlay_connector_requirements(content, plan, environment=environment)

    entities_by_id = {str(item["id"]): item for item in content["entities"]}
    properties_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for entity in content["entities"]:
        for prop in entity.get("properties", []):
            properties_by_id[str(prop["id"])] = (entity, prop)

    # Entity fields first, because every other kind has an entity reference.
    for item in desired["entities"]:
        item_id = ids_by_kind["entities"][str(item["key"])]
        existing = entities_by_id.get(item_id)
        if existing is None:
            existing = {"id": item_id, "properties": []}
            content["entities"].append(existing)
            entities_by_id[item_id] = existing
        existing.update(
            {
                "id": item_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "icon": item.get("icon", "box"),
                "color": item.get("color", "#4f46e5"),
                "is_abstract": item.get("is_abstract", False),
                "properties": existing.get("properties", []),
            }
        )

    for item in desired["properties"]:
        item_id = ids_by_kind["properties"][str(item["key"])]
        entity_id = ids_by_kind["entities"].get(str(item.get("entity_ref") or ""))
        if not entity_id or entity_id not in entities_by_id:
            raise PackageImportConflictError("属性引用的目标实体已变化，请重新执行预检")
        replacement = {
            "id": item_id,
            "name": item.get("name", ""),
            "data_type": item.get("data_type", "string"),
            "description": item.get("description", ""),
            "is_key": item.get("is_key", False),
            "is_required": item.get("is_required", False),
            "is_enum": item.get("is_enum", False),
            "enum_values": copy.deepcopy(item.get("enum_values", [])),
            "default_value": item.get("default_value", ""),
            "is_sensitive": item.get("is_sensitive", False),
        }
        parent, existing = properties_by_id.get(item_id, (None, None))
        if parent is not None and parent.get("id") != entity_id:
            raise PackageImportConflictError("属性的目标实体已变化，请重新执行预检")
        if existing is None:
            entities_by_id[entity_id].setdefault("properties", []).append(replacement)
            properties_by_id[item_id] = (entities_by_id[entity_id], replacement)
        else:
            existing.clear()
            existing.update(replacement)

    for item in desired["relations"]:
        source_id = ids_by_kind["entities"].get(str(item.get("source_entity_ref") or ""))
        target_id = ids_by_kind["entities"].get(str(item.get("target_entity_ref") or ""))
        if not source_id or not target_id:
            raise PackageImportConflictError("关系的实体引用已变化，请重新执行预检")
        _replace_record(
            content["relations"],
            ids_by_kind["relations"][str(item["key"])],
            {
                "id": ids_by_kind["relations"][str(item["key"])],
                "name": item.get("name", ""),
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relation_type": item.get("relation_type", "1:N"),
                "description": item.get("description", ""),
            },
        )

    for item in desired["mappings"]:
        entity_id = ids_by_kind["entities"].get(str(item.get("entity_ref") or ""))
        if not entity_id:
            raise PackageImportConflictError("数据映射的实体引用已变化，请重新执行预检")
        mapping_id = ids_by_kind["mappings"][str(item["key"])]
        mapping_path = f"resources.mappings.{item['key']}"
        source_ref = item.get("data_source_ref")
        try:
            mapping_binding = _mapping_binding_metadata(item)
            if mapping_binding is None:
                existing_mapping = current_mapping_by_key.get(str(item["key"]))
                if existing_mapping is not None:
                    mapping_binding = _mapping_binding_metadata(existing_mapping)
        except connector_service.ConnectorBindingError as exc:
            raise PackageImportConflictError(f"数据映射运行时绑定配置无效: {exc}") from exc
        mapping_record = {
            "id": mapping_id,
            "entity_id": entity_id,
            "data_source_id": _materialize_source_id(
                source_ref,
                sources_by_name_type,
                binding_targets=binding_targets,
                path=f"{mapping_path}.data_source_ref",
                binding_key_value=(
                    str(mapping_binding["binding_key"]) if mapping_binding is not None else None
                ),
            ),
            "table_name": item.get("table_name", ""),
            "column_map": _materialize_package_value(
                item.get("column_map", {}),
                entity_ids=ids_by_kind["entities"],
                action_ids=ids_by_kind["actions"],
                rule_ids=ids_by_kind["rules"],
                event_ids=ids_by_kind["events"],
                sources_by_name_type=sources_by_name_type,
                binding_targets=binding_targets,
                path=f"{mapping_path}.column_map",
            ),
        }
        if isinstance(source_ref, Mapping):
            _attach_runtime_binding(
                mapping_record,
                kind="data_source",
                reference=source_ref,
                path=f"{mapping_path}.data_source_ref",
                binding_targets=binding_targets,
                metadata=mapping_binding,
            )
        _replace_record(
            content["mappings"],
            mapping_id,
            mapping_record,
        )

    for item in desired["functions"]:
        function_id = ids_by_kind["functions"][str(item["key"])]
        _replace_record(
            content["functions"],
            function_id,
            {
                "id": function_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "input_schema": copy.deepcopy(item.get("input_schema", {})),
                "output_schema": copy.deepcopy(item.get("output_schema", {})),
                "tags": copy.deepcopy(item.get("tags", [])),
                "visibility": item.get("visibility", "scenario"),
            },
        )

    for item in desired["actions"]:
        entity_id = ids_by_kind["entities"].get(str(item.get("entity_ref") or ""))
        if not entity_id:
            raise PackageImportConflictError("Action 的实体引用已变化，请重新执行预检")
        action_id = ids_by_kind["actions"][str(item["key"])]
        action_path = f"resources.actions.{item['key']}"
        _replace_record(
            content["actions"],
            action_id,
            {
                "id": action_id,
                "entity_id": entity_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "input_schema": _materialize_package_value(
                    item.get("input_schema", {}),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{action_path}.input_schema",
                ),
                "executor_type": item.get("executor_type", "sql"),
                "executor_config": _materialize_package_value(
                    item.get("executor_config", {}),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{action_path}.executor_config",
                ),
                "precondition": item.get("precondition", ""),
                "postcondition": item.get("postcondition", ""),
                "enabled": item.get("enabled", True),
                "requires_confirmation": item.get("requires_confirmation", True),
                "idempotency_required": item.get("idempotency_required", True),
                "permission_scope": item.get("permission_scope", "scenario"),
                "access_scope": item.get("access_scope", "tenant"),
            },
        )

    for item in desired["rules"]:
        entity_ref = item.get("entity_ref")
        entity_id = None
        if entity_ref is not None:
            entity_id = ids_by_kind["entities"].get(str(entity_ref))
            if not entity_id:
                raise PackageImportConflictError("规则的实体引用已变化，请重新执行预检")
        rule_id = ids_by_kind["rules"][str(item["key"])]
        rule_path = f"resources.rules.{item['key']}"
        _replace_record(
            content["rules"],
            rule_id,
            {
                "id": rule_id,
                "entity_id": entity_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "condition": _materialize_package_value(
                    item.get("condition", {}),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{rule_path}.condition",
                ),
                "action_on_match": item.get("action_on_match", ""),
                "trigger_action_ids": [
                    ids_by_kind["actions"][reference]
                    for reference in item.get("trigger_action_refs", [])
                ],
                "severity": item.get("severity", "info"),
                "enabled": item.get("enabled", True),
            },
        )

    for item in desired["events"]:
        event_id = ids_by_kind["events"][str(item["key"])]
        event_path = f"resources.events.{item['key']}"
        _replace_record(
            content["events"],
            event_id,
            {
                "id": event_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "payload_schema": _materialize_package_value(
                    item.get("payload_schema", {}),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{event_path}.payload_schema",
                ),
                "trigger_source": item.get("trigger_source", ""),
                "enabled": item.get("enabled", True),
            },
        )

    for item in desired["workflows"]:
        workflow_id = ids_by_kind["workflows"][str(item["key"])]
        workflow_path = f"resources.workflows.{item['key']}"
        _replace_record(
            content["workflows"],
            workflow_id,
            {
                "id": workflow_id,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "trigger_type": item.get("trigger_type", "manual"),
                "trigger_config": _materialize_package_value(
                    item.get("trigger_config", {}),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{workflow_path}.trigger_config",
                ),
                "steps": _materialize_package_value(
                    item.get("steps", []),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{workflow_path}.steps",
                ),
                "nodes": _materialize_package_value(
                    item.get("nodes", []),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{workflow_path}.nodes",
                ),
                "edges": _materialize_package_value(
                    item.get("edges", []),
                    entity_ids=ids_by_kind["entities"],
                    action_ids=ids_by_kind["actions"],
                    rule_ids=ids_by_kind["rules"],
                    event_ids=ids_by_kind["events"],
                    sources_by_name_type=sources_by_name_type,
                    binding_targets=binding_targets,
                    path=f"{workflow_path}.edges",
                ),
                "status": item.get("status", "draft"),
                "enabled": item.get("enabled", True),
                "access_scope": item.get("access_scope", "tenant"),
            },
        )

    return release_service.normalize_snapshot_content(content), plan


def _import_base_matches_live(
    head: OntologySnapshot,
    live_content: Mapping[str, Any],
) -> bool:
    """Compare a branch head to live state while accepting pre-mapping snapshots."""
    head_content = release_service.normalize_snapshot_content(head.content or {})
    live = release_service.normalize_snapshot_content(dict(live_content))
    # Connector requirements are deployment metadata rather than a live ontology
    # row.  Capture cannot reconstruct them from current records, so exclude them
    # from the live-definition comparison while retaining them in the proposal.
    head_content.pop("connector_bindings", None)
    live.pop("connector_bindings", None)
    if "mappings" not in head_content:
        # Old P2 heads predate mapping governance.  They still form a valid base
        # for the first governed import; the generated proposal will capture the
        # current safe mapping definitions instead of deleting them.
        live.pop("mappings", None)
    if "functions" not in head_content:
        # Function contracts were introduced after the initial package/release
        # format.  Their absence in an old head is an overlay compatibility
        # signal, not a request to reject otherwise valid imports.
        live.pop("functions", None)
    return release_service.snapshot_hash(head_content) == release_service.snapshot_hash(live)


def create_governed_import_proposal(
    db: Session,
    target_scenario_or_id: BusinessScenario | str,
    *,
    branch_id: str,
    package: Mapping[str, Any] | Any,
    environment: str = "dev",
    title: str,
    description: str = "",
    submit: bool = True,
):
    """Create a release proposal from a portable package without applying it.

    Validation, binding resolution and the branch-head comparison are repeated at
    the write boundary.  A preview can therefore never be replayed after a target
    or branch changed, and this function deliberately delegates all live mutation
    to the existing independent-review → explicit-merge workflow.
    """
    target = _resolve_scenario(db, target_scenario_or_id)
    try:
        target_environment = connector_service.normalize_environment(environment)
    except connector_service.ConnectorBindingError as exc:
        raise PackageImportError(str(exc)) from exc
    principal = permission_service.require_principal(db)
    if target.tenant_id != principal.tenant_id:
        raise PackageImportConflictError("目标业务场景不可用于资源包导入")
    permission_service.require_scenario_permission(db, target, "write")
    permission_service.require_tenant_permission(db, "manage")

    branch = db.get(OntologyBranch, branch_id)
    if (
        not branch
        or branch.scenario_id != target.id
        or branch.tenant_id != target.tenant_id
        or branch.status != "active"
        or not branch.head_snapshot_id
    ):
        raise PackageImportConflictError("目标发布分支不可用于资源包导入")
    head = db.get(OntologySnapshot, branch.head_snapshot_id)
    if not head or head.scenario_id != target.id or head.tenant_id != target.tenant_id:
        raise PackageImportConflictError("目标发布分支缺少有效快照")

    live = release_service.capture_snapshot_content(db, target)
    if not _import_base_matches_live(head, live):
        raise PackageImportConflictError("目标本体已偏离发布分支基线，请刷新后重新预检")
    base_content = copy.deepcopy(head.content or {})
    if "mappings" not in base_content:
        base_content["mappings"] = copy.deepcopy(live.get("mappings", []))
    if "functions" not in base_content:
        base_content["functions"] = copy.deepcopy(live.get("functions", []))
    compiled, plan = _compile_package_overlay(
        db, target, package, base_content, environment=target_environment
    )
    fingerprint = str(plan["package_fingerprint"])
    summary = plan["proposal"]["summary"]
    audit_note = (
        f"资源包导入审计：指纹 {fingerprint}；"
        f"目标环境 {target_environment}；"
        f"创建 {summary.get('create', 0)}，更新 {summary.get('update', 0)}，"
        f"未变更 {summary.get('unchanged', 0)}。"
    )
    full_description = f"{description.strip()}\n\n{audit_note}".strip()
    try:
        proposal = release_service.create_proposal(
            db,
            branch.id,
            title=title,
            description=full_description,
            content=compiled,
            submit=submit,
            expected_base_snapshot_id=head.id,
        )
    except release_service.ReleaseConflictError as exc:
        raise PackageImportConflictError(str(exc)) from exc
    except release_service.ReleaseValidationError as exc:
        raise PackageImportError(str(exc)) from exc
    return proposal, fingerprint, copy.deepcopy(summary)
