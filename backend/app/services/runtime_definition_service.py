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

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
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
    WorkflowRun,
)
from . import connector_service, release_service


class RuntimeDefinitionError(ValueError):
    """A deployment cannot safely resolve the requested immutable definition."""


@dataclass(frozen=True)
class RuntimeDefinition:
    """A fully resolved live or frozen definition for one scenario/environment."""

    scenario: BusinessScenario
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

    @property
    def is_frozen(self) -> bool:
        return self.source == "release"


def _normalize_environment(value: str | None) -> str:
    try:
        return connector_service.normalize_environment(value or "dev")
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeDefinitionError(str(exc)) from exc


def _runtime_resource(raw: dict[str, Any], scenario: BusinessScenario) -> SimpleNamespace:
    """Turn verified snapshot JSON into the small ORM-shaped surface we need.

    The DTO deliberately contains no persistence session or mutable ORM state.
    Permission checks still evaluate against the current scenario and grants,
    while definition fields stay frozen at the release snapshot.
    """
    data = dict(raw)
    data["scenario_id"] = scenario.id
    data["scenario"] = scenario
    return SimpleNamespace(**data)


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
    }

    # Dev remains mutable, but a dry-run still needs a reproducible optimistic
    # pin.  Hash only definition columns (not refresh/error/runtime state) so a
    # confirmed action is rejected when its meaning changed after preview.
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
    canonical = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    live_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return RuntimeDefinition(
        scenario=scenario,
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
    runtime_content = release_service.active_snapshot_content(content)
    entities = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("entities", [])
    }
    for entity in entities.values():
        entity.lifecycle_status = str(
            getattr(entity, "lifecycle_status", "active") or "active"
        )
        entity.properties = [
            _runtime_resource(
                {**property_data, "entity_id": entity.id}, scenario
            )
            for property_data in list(getattr(entity, "properties", []) or [])
        ]
        for prop in entity.properties:
            prop.entity = entity
    relations = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("relations", [])
    }
    for relation in relations.values():
        relation.source_entity = entities.get(str(relation.source_entity_id))
        relation.target_entity = entities.get(str(relation.target_entity_id))

    mappings = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("mappings", [])
    }
    relation_mappings = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("relation_mappings", [])
    }
    functions = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("functions", [])
    }
    actions = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("actions", [])
    }
    rules = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("rules", [])
    }
    events = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("events", [])
    }
    workflows = {
        str(item["id"]): _runtime_resource(item, scenario)
        for item in runtime_content.get("workflows", [])
    }
    for resource in [*mappings.values(), *actions.values(), *rules.values()]:
        resource.entity = entities.get(str(getattr(resource, "entity_id", "") or ""))
    for relation_mapping in relation_mappings.values():
        relation_mapping.relation = relations.get(str(relation_mapping.relation_id))
        relation_mapping.source_mapping = mappings.get(str(relation_mapping.source_mapping_id))
        relation_mapping.target_mapping = mappings.get(str(relation_mapping.target_mapping_id))

    return RuntimeDefinition(
        scenario=scenario,
        environment=environment,
        source="release",
        snapshot_id=snapshot.id,
        release_id=release.id if release else None,
        definition_hash=content_hash,
        scenario_name=str(content["scenario"]["name"]),
        entities=entities,
        relations=relations,
        actions=actions,
        functions=functions,
        mappings=mappings,
        relation_mappings=relation_mappings,
        rules=rules,
        events=events,
        workflows=workflows,
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
