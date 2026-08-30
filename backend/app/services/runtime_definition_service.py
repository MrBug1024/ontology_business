"""Resolve the definition that a deployment is actually allowed to execute.

``dev`` remains the authoring environment and reads the live ORM rows.  A
``staging`` or ``prod`` deployment never reads those mutable rows for a new
execution: it resolves the active ``OntologyRelease`` and builds small,
read-only resource DTOs from that release's immutable snapshot instead.

The same resolver is also used for persisted workflow runs.  In that case a
superseded release is still valid when it is explicitly pinned by the run;
changing an environment's active release must not change an in-flight approval
or retry underneath an operator.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    DataMapping,
    RelationDataMapping,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyRelation,
    OntologyRelease,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    ScenarioCapabilityPort,
    WorkflowRun,
)
from . import connector_service, release_service


class RuntimeDefinitionError(ValueError):
    """A deployment cannot safely resolve the requested immutable definition."""


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects in-place changes.

    A ``dict`` subclass is intentional.  Existing v1 serializers and schema
    validators use concrete ``dict`` checks, while callers that need a mutable
    working value can still obtain one with ``copy.deepcopy``.
    """

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("runtime definition values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        copied: dict[Any, Any] = {}
        memo[id(self)] = copied
        copied.update(
            {
                copy.deepcopy(key, memo): copy.deepcopy(value, memo)
                for key, value in self.items()
            }
        )
        return copied


class _FrozenList(list):
    """JSON-compatible sequence that rejects in-place changes."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("runtime definition values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        copied: list[Any] = []
        memo[id(self)] = copied
        copied.extend(copy.deepcopy(value, memo) for value in self)
        return copied


class _RuntimeResource:
    """Detached ORM-shaped DTO sealed after its relationship graph is wired."""

    __slots__ = ("__dict__", "_locked")

    def __init__(self, **values: Any) -> None:
        object.__setattr__(self, "_locked", False)
        self.__dict__.update(values)

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        if name == "__dict__" and object.__getattribute__(self, "_locked"):
            return MappingProxyType(value)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if self._locked:
            raise TypeError("runtime definition resources are immutable")
        self.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        if self._locked:
            raise TypeError("runtime definition resources are immutable")
        try:
            del self.__dict__[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def _seal(self) -> None:
        object.__setattr__(self, "_locked", True)

    def __copy__(self) -> SimpleNamespace:
        # A few legacy execution paths intentionally make a detached working
        # copy before substituting an environment connector.
        return SimpleNamespace(**vars(self))

    def __deepcopy__(self, memo: dict[int, Any]) -> SimpleNamespace:
        copied = SimpleNamespace()
        memo[id(self)] = copied
        copied.__dict__.update(
            {
                key: copy.deepcopy(value, memo)
                for key, value in vars(self).items()
            }
        )
        return copied

    def __repr__(self) -> str:  # pragma: no cover - diagnostic convenience.
        identity = getattr(self, "id", getattr(self, "port_key", ""))
        name = getattr(self, "name", "")
        return f"RuntimeResource(id={identity!r}, name={name!r})"


def _deep_freeze(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Freeze one detached runtime graph while preserving resource cycles."""

    memo = memo if memo is not None else {}
    if value is None or isinstance(
        value,
        (str, bytes, bool, int, float, complex),
    ):
        return value
    existing = memo.get(id(value))
    if existing is not None:
        return existing
    if isinstance(value, _RuntimeResource):
        memo[id(value)] = value
        if not value._locked:
            for key, item in tuple(vars(value).items()):
                value.__dict__[key] = _deep_freeze(item, memo)
            value._seal()
        return value
    if isinstance(value, Mapping):
        # Runtime JSON cannot contain reference cycles.  Resource cycles are
        # handled above, before their list/dict relationship containers close.
        frozen = _FrozenDict(
            (key, _deep_freeze(item, memo)) for key, item in value.items()
        )
        memo[id(value)] = frozen
        return frozen
    if isinstance(value, list):
        frozen = _FrozenList(_deep_freeze(item, memo) for item in value)
        memo[id(value)] = frozen
        return frozen
    if isinstance(value, tuple):
        frozen = tuple(_deep_freeze(item, memo) for item in value)
        memo[id(value)] = frozen
        return frozen
    if isinstance(value, (set, frozenset)):
        frozen = frozenset(_deep_freeze(item, memo) for item in value)
        memo[id(value)] = frozen
        return frozen
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Sequence):
        frozen = tuple(_deep_freeze(item, memo) for item in value)
        memo[id(value)] = frozen
        return frozen
    # Dates, UUIDs, Decimals and SQL scalar wrapper values are immutable.  ORM
    # instances never reach this branch because every runtime row is projected
    # through ``_row_values`` before graph construction.
    return value


def _row_values(value: Any) -> dict[str, Any]:
    """Copy scalar ORM columns or one snapshot object into detached values."""

    if isinstance(value, Mapping):
        return dict(value)
    table = getattr(value, "__table__", None)
    if table is not None:
        return {
            column.key: getattr(value, column.key)
            for column in table.columns
        }
    try:
        return dict(vars(value))
    except TypeError as exc:
        raise RuntimeDefinitionError("运行定义资源无法安全快照") from exc


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True)
class RuntimeDefinition:
    """A fully resolved live or frozen definition for one scenario/environment."""

    scenario: Any
    environment: str
    source: str
    snapshot_id: str | None
    release_id: str | None
    definition_hash: str
    scenario_name: str
    entities: dict[str, Any]
    relations: dict[str, Any]
    actions: dict[str, Any]
    functions: dict[str, Any]
    mappings: dict[str, Any]
    relation_mappings: dict[str, Any]
    rules: dict[str, Any]
    events: dict[str, Any]
    workflows: dict[str, Any]
    capability_ports: dict[str, Any]

    @property
    def is_frozen(self) -> bool:
        return self.source == "release"


def _normalize_environment(value: str | None) -> str:
    try:
        return connector_service.normalize_environment(value or "dev")
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeDefinitionError(str(exc)) from exc


def _runtime_resource(raw: Any, scenario: _RuntimeResource) -> _RuntimeResource:
    """Turn one ORM row or verified snapshot object into a detached draft.

    The DTO deliberately contains no persistence session or mutable ORM state.
    Permission checks still evaluate current grants against a scenario identity
    snapshot, while definition fields stay fixed for this resolve lifecycle.
    """
    data = _row_values(raw)
    data["scenario_id"] = scenario.id
    data["scenario"] = scenario
    return _RuntimeResource(**data)


def _materialize_runtime_graph(
    scenario: BusinessScenario,
    groups: Mapping[str, Mapping[str, Any]],
) -> tuple[_RuntimeResource, dict[str, dict[str, Any]]]:
    """Project ORM/snapshot rows into one detached, deeply immutable graph."""

    scenario_view = _RuntimeResource(**_row_values(scenario))
    entities = {
        str(resource_id): _runtime_resource(raw, scenario_view)
        for resource_id, raw in groups.get("entities", {}).items()
    }
    for resource_id, entity in entities.items():
        raw_entity = groups["entities"][resource_id]
        raw_properties = list(_read(raw_entity, "properties", []) or [])
        entity.lifecycle_status = str(
            getattr(entity, "lifecycle_status", "active") or "active"
        )
        entity.properties = [
            _runtime_resource(
                {
                    **_row_values(raw_property),
                    "entity_id": entity.id,
                },
                scenario_view,
            )
            for raw_property in sorted(
                raw_properties,
                key=lambda item: str(_read(item, "id", "")),
            )
        ]
        for prop in entity.properties:
            prop.entity = entity

    def resources(group: str) -> dict[str, _RuntimeResource]:
        return {
            str(resource_id): _runtime_resource(raw, scenario_view)
            for resource_id, raw in groups.get(group, {}).items()
        }

    relations = resources("relations")
    functions = resources("functions")
    mappings = resources("mappings")
    relation_mappings = resources("relation_mappings")
    actions = resources("actions")
    rules = resources("rules")
    events = resources("events")
    workflows = resources("workflows")
    capability_ports = resources("capability_ports")

    for relation in relations.values():
        relation.source_entity = entities.get(str(relation.source_entity_id))
        relation.target_entity = entities.get(str(relation.target_entity_id))
    for resource in [*mappings.values(), *actions.values(), *rules.values()]:
        resource.entity = entities.get(str(getattr(resource, "entity_id", "") or ""))
    for relation_mapping in relation_mappings.values():
        relation_mapping.relation = relations.get(str(relation_mapping.relation_id))
        relation_mapping.source_mapping = mappings.get(
            str(relation_mapping.source_mapping_id)
        )
        relation_mapping.target_mapping = mappings.get(
            str(relation_mapping.target_mapping_id)
        )
    for port_key, port in capability_ports.items():
        raw_port = groups["capability_ports"][port_key]
        raw_schema = _read(raw_port, "dataset_schema", None)
        if raw_schema is not None:
            port.dataset_schema = _RuntimeResource(**_row_values(raw_schema))
            port.dataset_schema_hash = str(
                getattr(port.dataset_schema, "schema_hash", "") or ""
            )
        else:
            port.dataset_schema = None
            port.dataset_schema_hash = str(
                getattr(port, "dataset_schema_hash", "") or ""
            )

    detached: dict[str, dict[str, Any]] = {
        "entities": entities,
        "relations": relations,
        "functions": functions,
        "mappings": mappings,
        "relation_mappings": relation_mappings,
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": workflows,
        "capability_ports": capability_ports,
    }
    memo: dict[int, Any] = {}
    frozen_groups = {
        group: _deep_freeze(resources_by_id, memo)
        for group, resources_by_id in detached.items()
    }
    frozen_scenario = _deep_freeze(scenario_view, memo)
    return frozen_scenario, frozen_groups


def _live_definition(scenario: BusinessScenario, environment: str, db: Session) -> RuntimeDefinition:
    entities = {
        item.id: item
        for item in db.execute(
            select(OntologyEntity).where(
                OntologyEntity.scenario_id == scenario.id,
                OntologyEntity.lifecycle_status == "active",
            )
        ).scalars().all()
    }
    entity_ids = set(entities)
    relations = {
        item.id: item
        for item in db.execute(
            select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
        ).scalars().all()
        if item.source_entity_id in entity_ids and item.target_entity_id in entity_ids
    }
    relation_ids = set(relations)
    functions = {
        item.id: item
        for item in db.execute(
            select(FunctionDefinition).where(FunctionDefinition.scenario_id == scenario.id)
        ).scalars().all()
    }
    mappings = {
        item.id: item
        for item in db.execute(
            select(DataMapping).where(DataMapping.scenario_id == scenario.id)
        ).scalars().all()
        if item.entity_id in entity_ids
    }
    mapping_ids = set(mappings)
    relation_mappings = {
        item.id: item
        for item in db.execute(
            select(RelationDataMapping).where(RelationDataMapping.scenario_id == scenario.id)
        ).scalars().all()
        if item.relation_id in relation_ids
        and item.source_mapping_id in mapping_ids
        and item.target_mapping_id in mapping_ids
    }
    actions = {
        item.id: item
        for item in db.execute(
            select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)
        ).scalars().all()
        if item.entity_id in entity_ids
    }
    action_ids = set(actions)
    rules = {
        item.id: item
        for item in db.execute(
            select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)
        ).scalars().all()
        if (not item.entity_id or item.entity_id in entity_ids)
        and {
            str(action_id) for action_id in (item.trigger_action_ids or [])
        }.issubset(action_ids)
    }
    rule_ids = set(rules)
    events = {
        item.id: item
        for item in db.execute(
            select(OntologyEvent).where(OntologyEvent.scenario_id == scenario.id)
        ).scalars().all()
    }
    workflows = {
        item.id: item
        for item in db.execute(
            select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
        ).scalars().all()
        if (
            lambda refs: refs["action"].issubset(action_ids)
            and refs["rule"].issubset(rule_ids)
            and refs["event"].issubset(set(events))
        )(
            release_service._workflow_reference_ids({
                "trigger_type": item.trigger_type,
                "trigger_config": item.trigger_config or {},
                "steps": item.steps or [],
                "nodes": item.nodes or [],
            })
        )
    }
    capability_ports = {
        item.id: item
        for item in db.execute(
            select(ScenarioCapabilityPort).where(
                ScenarioCapabilityPort.scenario_id == scenario.id,
                ScenarioCapabilityPort.status == "active",
            )
        ).scalars().all()
    }
    capability_ids = {
        "function": set(functions),
        "action": set(actions),
        "workflow": set(workflows),
    }
    orphaned_ports = sorted(
        str(port.id)
        for port in capability_ports.values()
        if str(port.capability_key or "")
        not in capability_ids.get(str(port.capability_kind or ""), set())
    )
    if orphaned_ports:
        raise RuntimeDefinitionError(
            "活动能力端口引用了不存在或不可执行的所属能力："
            + "、".join(orphaned_ports[:20])
        )
    definition_groups = {
        "entities": entities,
        "relations": relations,
        "functions": functions,
        "mappings": mappings,
        "relation_mappings": relation_mappings,
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": workflows,
        "capability_ports": capability_ports,
    }
    scenario_view, definition_groups = _materialize_runtime_graph(
        scenario,
        definition_groups,
    )
    entities = definition_groups["entities"]
    relations = definition_groups["relations"]
    functions = definition_groups["functions"]
    mappings = definition_groups["mappings"]
    relation_mappings = definition_groups["relation_mappings"]
    actions = definition_groups["actions"]
    rules = definition_groups["rules"]
    events = definition_groups["events"]
    workflows = definition_groups["workflows"]
    capability_ports = definition_groups["capability_ports"]

    # Dev authoring remains mutable between resolves, but each resolve/deployment
    # gets a reproducible optimistic pin and a detached object graph. Hash only
    # definition columns (not refresh/error/runtime state) so a confirmed action
    # is rejected when its meaning changed after preview.
    definition_fields = {
        "entities": (
            "id", "name", "api_name", "namespace", "description", "icon", "color",
            "lifecycle_status", "is_abstract", "state_property",
        ),
        "relations": (
            "id", "name", "api_name", "namespace", "source_entity_id",
            "target_entity_id", "source_display_name", "source_api_name",
            "target_display_name", "target_api_name", "storage_kind",
            "relation_type", "constraints", "description",
        ),
        "functions": ("id", "name", "description", "input_schema", "output_schema", "tags", "visibility", "runtime_kind", "runtime_config"),
        "mappings": ("id", "entity_id", "data_source_id", "data_source_binding_key", "data_source_binding_ref", "table_name", "column_map", "transform_rules"),
        "relation_mappings": (
            "id", "relation_id", "source_mapping_id", "target_mapping_id", "mode",
            "data_source_id", "data_source_binding_key", "data_source_binding_ref",
            "table_name", "foreign_key_column", "source_key_column", "target_key_column",
        ),
        "actions": ("id", "entity_id", "name", "description", "input_schema", "executor_type", "executor_config", "precondition", "postcondition", "enabled", "requires_confirmation", "idempotency_required", "permission_scope", "access_scope"),
        "rules": ("id", "entity_id", "name", "description", "condition", "action_on_match", "trigger_action_ids", "severity", "enabled"),
        "events": ("id", "name", "description", "payload_schema", "trigger_source", "enabled"),
        "workflows": ("id", "name", "description", "trigger_type", "trigger_config", "steps", "nodes", "edges", "status", "enabled", "access_scope"),
        "capability_ports": (
            "id", "capability_kind", "capability_key", "port_key", "name",
            "description", "direction", "role",
            "media_kind", "schema_document", "is_required", "cardinality",
            "binding_policy", "config",
        ),
    }
    digest_payload = {
        group: [
            {field: getattr(item, field, None) for field in definition_fields[group]}
            for _resource_id, item in sorted(resources.items())
        ]
        for group, resources in definition_groups.items()
    }
    # Properties are nested ORM resources and therefore need an explicit,
    # deterministic projection in the live definition hash.  Without this, a
    # property edit would leave an Agent/Action optimistic pin unchanged.
    digest_payload["entities"] = [
        {
            **{field: getattr(entity, field, None) for field in definition_fields["entities"]},
            "properties": [
                {
                    field: getattr(prop, field, None)
                    for field in (
                        "id", "name", "api_name", "data_type", "description", "is_key", "is_title",
                        "is_required", "is_enum", "enum_values", "default_value",
                        "constraints", "is_sensitive",
                    )
                }
                for prop in sorted(entity.properties, key=lambda item: item.id)
            ],
        }
        for _resource_id, entity in sorted(entities.items())
    ]
    digest_payload["capability_ports"] = [
        {
            **{
                field: getattr(port, field, None)
                for field in definition_fields["capability_ports"]
            },
            "dataset_schema_hash": (
                port.dataset_schema.schema_hash if port.dataset_schema else ""
            ),
        }
        for _port_key, port in sorted(capability_ports.items())
    ]
    canonical = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    live_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return RuntimeDefinition(
        scenario=scenario_view,
        environment=environment,
        source="live",
        snapshot_id=None,
        release_id=None,
        definition_hash=live_hash,
        scenario_name=scenario.name,
        entities=entities,
        relations=relations,
        actions=actions,
        functions=functions,
        mappings=mappings,
        relation_mappings=relation_mappings,
        rules=rules,
        events=events,
        workflows=workflows,
        capability_ports=capability_ports,
    )


def _from_snapshot(
    scenario: BusinessScenario,
    environment: str,
    snapshot: OntologySnapshot,
    *,
    release: OntologyRelease | None,
) -> RuntimeDefinition:
    if snapshot.scenario_id != scenario.id or snapshot.tenant_id != scenario.tenant_id:
        raise RuntimeDefinitionError("发布快照不属于当前业务场景")
    required_collections = {
        "entities", "relations", "mappings", "functions", "actions",
        "rules", "events", "workflows",
    }
    missing_collections = sorted(
        key for key in required_collections if key not in (snapshot.content or {})
    )
    if missing_collections:
        raise RuntimeDefinitionError(
            "发布快照缺少 Agent 运行资源：" + "、".join(missing_collections)
        )
    try:
        content = release_service.normalize_snapshot_content(snapshot.content or {})
    except Exception as exc:  # noqa: BLE001 - unsafe historic data must fail closed.
        raise RuntimeDefinitionError("发布快照定义无效，已阻止运行") from exc
    # ``OntologySnapshot.content_hash`` covers the entire normalised artifact,
    # including logical connector requirements.  Runtime provenance must use
    # the same digest; a definition-only hash would incorrectly reject every
    # snapshot that has governed bindings.
    content_hash = release_service.snapshot_hash(content)
    if not snapshot.content_hash or snapshot.content_hash != content_hash:
        raise RuntimeDefinitionError("发布快照校验失败，已阻止运行")
    if (
        int(content.get("capability_contract_version", 1) or 1) < 2
        and bool(content.get("capability_ports"))
    ):
        raise RuntimeDefinitionError("发布快照能力端口缺少明确归属，已阻止运行")
    runtime_content = release_service.active_snapshot_content(content)

    def historic_resource(group: str, item: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(item)
        # Early snapshot contracts did not persist presentation timestamps.
        # A release snapshot is immutable, so its creation time is the only
        # truthful fallback for historic DTO serialization.
        values.setdefault("created_at", snapshot.created_at)
        if group == "functions":
            values.setdefault("updated_at", snapshot.created_at)
        if group == "mappings":
            values.setdefault("environment_status", {})
            values.setdefault("status", "unknown")
            values.setdefault("last_error", "")
            values.setdefault("last_checked_at", None)
            values.setdefault("last_refreshed_at", None)
            values.setdefault("last_row_count", 0)
            values.setdefault("last_imported_count", 0)
        if group == "relation_mappings":
            values.setdefault("status", "unknown")
            values.setdefault("last_error", "")
            values.setdefault("last_checked_at", None)
            values.setdefault("last_refreshed_at", None)
            values.setdefault("last_link_count", 0)
        return values

    scenario_view, frozen_groups = _materialize_runtime_graph(
        scenario,
        {
            group: {
                str(item["id"]): historic_resource(group, item)
                for item in runtime_content.get(group, [])
            }
            for group in (
                "entities",
                "relations",
                "functions",
                "mappings",
                "relation_mappings",
                "actions",
                "rules",
                "events",
                "workflows",
                "capability_ports",
            )
        },
    )

    return RuntimeDefinition(
        scenario=scenario_view,
        environment=environment,
        source="release",
        snapshot_id=snapshot.id,
        release_id=release.id if release else None,
        definition_hash=content_hash,
        scenario_name=str(content["scenario"]["name"]),
        entities=frozen_groups["entities"],
        relations=frozen_groups["relations"],
        actions=frozen_groups["actions"],
        functions=frozen_groups["functions"],
        mappings=frozen_groups["mappings"],
        relation_mappings=frozen_groups["relation_mappings"],
        rules=frozen_groups["rules"],
        events=frozen_groups["events"],
        workflows=frozen_groups["workflows"],
        capability_ports=frozen_groups["capability_ports"],
    )


def _active_release(
    db: Session,
    scenario: BusinessScenario,
    environment: str,
) -> OntologyRelease:
    release = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.tenant_id == scenario.tenant_id,
            OntologyRelease.environment == environment,
            OntologyRelease.status == "released",
        )
        .order_by(OntologyRelease.created_at.desc())
        .limit(1)
    ).scalars().first()
    if not release:
        raise RuntimeDefinitionError(f"{environment} 环境尚未发布该业务场景")
    return release


def resolve_active(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str | None = None,
) -> RuntimeDefinition:
    """Resolve the current definition for a request entering this deployment."""
    if scenario.status == "retired":
        raise RuntimeDefinitionError("业务场景已退役，不能创建新的运行调用")
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        return _live_definition(scenario, normalized_environment, db)
    release = _active_release(db, scenario, normalized_environment)
    snapshot = db.get(OntologySnapshot, release.snapshot_id)
    if not snapshot:
        raise RuntimeDefinitionError(f"{normalized_environment} 环境的发布快照不可用")
    return _from_snapshot(
        scenario,
        normalized_environment,
        snapshot,
        release=release,
    )


def resolve_authoring(
    db: Session,
    scenario: BusinessScenario,
) -> RuntimeDefinition:
    """Return the mutable control-plane definition independently of host env.

    ``RUNTIME_ENVIRONMENT`` describes where this Python process is deployed;
    it must not turn an online installation into a read-only production
    runtime.  Scenario pages, modeling, candidate review and validation setup
    all inspect the tenant's current live definition.  Only an explicit
    invocation or release request may select a staging/prod snapshot.

    Retired scenarios are write-frozen by the scenario permission boundary, so
    their live rows are also the stable control-plane record shown before a
    user restores or permanently deletes the scenario.
    """
    return _live_definition(scenario, "dev", db)


def resolve_retired_history(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str | None = None,
) -> RuntimeDefinition:
    """Resolve read-only history without reopening an executable deployment.

    This function is intentionally separate from ``resolve_active`` and is
    only valid after scenario retirement.  A staging/prod read selects the
    most recently deployed immutable snapshot even after its active pointer
    was withdrawn.  Dev-only scenarios have no deployment snapshot contract;
    their frozen-by-retirement live rows remain available for historic reads.
    """
    if str(scenario.status or "").strip().lower() != "retired":
        raise RuntimeDefinitionError("历史定义读取只适用于已退役业务场景")
    normalized_environment = _normalize_environment(environment)
    release = db.scalar(
        select(OntologyRelease)
        .where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.tenant_id == scenario.tenant_id,
            OntologyRelease.environment == normalized_environment,
            OntologyRelease.status.in_({"released", "superseded", "rolled_back"}),
        )
        .order_by(OntologyRelease.created_at.desc(), OntologyRelease.id.desc())
        .limit(1)
    )
    if release is None:
        if normalized_environment == "dev":
            return _live_definition(scenario, normalized_environment, db)
        raise RuntimeDefinitionError(
            f"{normalized_environment} 环境没有可追溯的历史发布快照"
        )
    snapshot = db.get(OntologySnapshot, release.snapshot_id)
    if snapshot is None:
        raise RuntimeDefinitionError(
            f"{normalized_environment} 环境的历史发布快照不可用"
        )
    return _from_snapshot(
        scenario,
        normalized_environment,
        snapshot,
        release=release,
    )


def resolve_pinned(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    snapshot_id: str | None,
    release_id: str | None,
    definition_hash: str | None,
) -> RuntimeDefinition:
    """Resolve a durable non-dev definition pin for a queued operation.

    A mapping-refresh worker uses this instead of the current active release,
    so a queued read cannot fall forward after a later deployment promotion.
    """
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        raise RuntimeDefinitionError("开发环境不应携带发布定义固定版本")
    return _resolve_pinned_release(
        db,
        scenario,
        environment=normalized_environment,
        snapshot_id=snapshot_id,
        release_id=release_id,
        definition_hash=definition_hash,
    )


def _resolve_pinned_release(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    snapshot_id: str | None,
    release_id: str | None,
    definition_hash: str | None,
) -> RuntimeDefinition:
    """Resolve and integrity-check one immutable release pin."""
    if not snapshot_id or not release_id or not definition_hash:
        raise RuntimeDefinitionError("运行定义快照缺失，已阻止执行")
    release = db.get(OntologyRelease, release_id)
    snapshot = db.get(OntologySnapshot, snapshot_id)
    if not release or not snapshot:
        raise RuntimeDefinitionError("运行固定的发布版本已不可用")
    if (
        release.scenario_id != scenario.id
        or release.tenant_id != scenario.tenant_id
        or release.environment != environment
        or release.snapshot_id != snapshot.id
        or release.status not in {"released", "superseded", "rolled_back"}
    ):
        raise RuntimeDefinitionError("运行固定的发布版本不一致，已阻止执行")
    definition = _from_snapshot(scenario, environment, snapshot, release=release)
    if definition.definition_hash != definition_hash:
        raise RuntimeDefinitionError("运行定义快照完整性校验失败")
    return definition


def resolve_for_run(db: Session, run: WorkflowRun) -> RuntimeDefinition:
    """Resolve the version pinned when a durable run was queued.

    A non-dev run is never allowed to fall forward to whatever environment
    release happens to be active now.  That would change approval/retry meaning
    and could rebind external effects mid-flight.
    """
    scenario = db.get(BusinessScenario, run.scenario_id)
    if not scenario:
        raise RuntimeDefinitionError("工作流所属场景不存在")
    environment = _normalize_environment(run.environment)
    if environment == "dev":
        if run.definition_snapshot_id or run.release_id:
            raise RuntimeDefinitionError("开发环境不应携带发布定义固定版本")
        if not run.definition_hash:
            raise RuntimeDefinitionError("运行定义哈希缺失，已阻止执行")
        definition = _live_definition(scenario, environment, db)
        if definition.definition_hash != run.definition_hash:
            raise RuntimeDefinitionError("运行定义快照完整性校验失败")
        return definition
    return _resolve_pinned_release(
        db,
        scenario,
        environment=environment,
        snapshot_id=run.definition_snapshot_id,
        release_id=run.release_id,
        definition_hash=run.definition_hash,
    )


def resolve_resource(
    definition: RuntimeDefinition,
    kind: str,
    resource_id: str,
) -> Any:
    resources = {
        "action": definition.actions,
        "function": definition.functions,
        "mapping": definition.mappings,
        "relation_mapping": definition.relation_mappings,
        "rule": definition.rules,
        "event": definition.events,
        "workflow": definition.workflows,
    }.get(kind)
    if resources is None:
        raise RuntimeDefinitionError("不支持的运行时资源类型")
    resource = resources.get(str(resource_id))
    if resource is None:
        raise RuntimeDefinitionError(f"{kind} 不存在于当前运行定义")
    return resource


def active_definitions(
    db: Session,
    *,
    environment: str,
) -> Iterable[RuntimeDefinition]:
    """Yield every released scenario definition for a non-dev scheduler tick."""
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        scenarios = db.execute(select(BusinessScenario)).scalars().all()
        return [_live_definition(scenario, normalized_environment, db) for scenario in scenarios]
    releases = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.environment == normalized_environment,
            OntologyRelease.status == "released",
        )
        .order_by(OntologyRelease.created_at.desc())
    ).scalars().all()
    definitions: list[RuntimeDefinition] = []
    for release in releases:
        scenario = db.get(BusinessScenario, release.scenario_id)
        snapshot = db.get(OntologySnapshot, release.snapshot_id)
        if not scenario or not snapshot:
            continue
        try:
            definitions.append(
                _from_snapshot(scenario, normalized_environment, snapshot, release=release)
            )
        except RuntimeDefinitionError:
            # One malformed historic release must not stop unrelated scenarios;
            # it stays unavailable rather than being silently replaced by live.
            continue
    return definitions
