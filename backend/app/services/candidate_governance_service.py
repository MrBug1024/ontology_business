"""Origin-neutral quality governance for scenario definition candidates.

The staging row is deliberately inert; that storage fact is independent from
whether its payload is good enough to become a formal dev definition.  This
module owns the second decision.  It never calls an LLM and never branches on
an industry, scenario name, or candidate origin.

Formalisation reuses the compound model compiler's closed preflight/apply
boundary for ontology resources and the catalog's closed contract for logical
capability ports.  The adapter only converts candidate rows into those trusted
protocols; it does not duplicate their validation or activate a definition.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog_schemas import ScenarioCapabilityPortCreate
from ..models import (
    BusinessScenario,
    DataMapping,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    RelationDataMapping,
    ScenarioCapabilityPort,
    ScenarioModelDraftResource,
)
from . import (
    assistant_capability_modeling_service,
    catalog_service,
    release_service,
    scenario_model_compiler,
    scenario_model_draft_service,
)
from .policies import PolicyViolation


MAX_PROMOTION_BATCH = 200
FORMAL_RESOURCE_KINDS = frozenset({
    "entity",
    "property",
    "relation",
    "mapping",
    "relation_mapping",
    "function",
    "action",
    "rule",
    "event",
    "workflow",
    "capability_port",
})
ACTIVATABLE_RESOURCE_KINDS = frozenset({
    "action", "rule", "event", "workflow", "capability_port",
})
OPEN_LIFECYCLE_STATUSES = frozenset({
    scenario_model_draft_service.PENDING_CONFIRMATION_STATUS,
    "ready_for_review",
    "needs_attention",
    "needs_validation",
    "accepted",
    "deferred",
})
_SECTION_BY_KIND = {
    "entity": "entities",
    "relation": "relations",
    "function": "functions",
    "action": "actions",
    "rule": "rules",
    "event": "events",
    "workflow": "workflows",
    "mapping": "mappings",
    "relation_mapping": "relation_mappings",
}
_DEFAULT_TASK_BY_KIND = {
    "entity": "ontology",
    "property": "ontology",
    "relation": "ontology",
    "mapping": "mapping",
    "relation_mapping": "mapping",
    "function": "capabilities",
    "action": "capabilities",
    "rule": "rules",
    "event": "rules",
    "workflow": "workflows",
    "capability_port": "capabilities",
}

_PORT_PAYLOADS_KEY = "_candidate_capability_port_payloads"
_PORT_EXISTING_IDS_KEY = "_candidate_capability_port_existing_ids"


class CandidateRevisionConflict(ValueError):
    """The caller reviewed an older candidate revision."""


class CandidateNotFound(LookupError):
    """A candidate is absent or outside the authenticated owner scope."""


class CandidatePromotionBlocked(ValueError):
    """No formal mutations were attempted because deterministic quality failed."""

    def __init__(self, blockers: list[dict[str, Any]]) -> None:
        super().__init__("候选定义未通过正式化预检")
        self.blockers = blockers


@dataclass(frozen=True)
class CandidateEvaluation:
    eligible: bool
    blockers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    payload: dict[str, Any] | None
    fingerprint: str
    row_resource_keys: dict[str, str]


def _json_copy(value: Any, fallback: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:  # noqa: BLE001
        return copy.deepcopy(fallback)


def _issue(
    code: str,
    message: str,
    *,
    blocking: bool = True,
    draft_ids: Iterable[str] = (),
    resource_keys: Iterable[str] = (),
    field_path: Iterable[str] = (),
    resolution_hint: str = "",
) -> dict[str, Any]:
    return {
        "code": str(code or "candidate_validation_failed")[:120],
        "message": str(message or "候选定义未通过校验")[:2000],
        "blocking": bool(blocking),
        "draft_ids": sorted({str(value)[:32] for value in draft_ids if str(value)}),
        "affected_change_keys": sorted({
            str(value)[:500] for value in resource_keys if str(value)
        }),
        "field_path": [str(value)[:120] for value in field_path if str(value)][:30],
        "resolution_hint": str(resolution_hint or "")[:2000],
    }


def _bounded_issues(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        normalized = _issue(
            str(raw.get("code") or "candidate_validation_failed"),
            str(raw.get("message") or "候选定义未通过校验"),
            blocking=raw.get("blocking", True) is not False,
            draft_ids=raw.get("draft_ids") or [],
            resource_keys=raw.get("affected_change_keys") or [],
            field_path=raw.get("field_path") or [],
            resolution_hint=str(raw.get("resolution_hint") or ""),
        )
        signature = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        if signature not in seen:
            seen.add(signature)
            result.append(normalized)
    return result[:200]


def source_origin(row: ScenarioModelDraftResource) -> str:
    """Project provenance only; callers must never use it as a quality input."""
    source = str(row.materialization_source or "").strip().casefold()
    if source == "manual" or source.startswith("manual_"):
        return "manual"
    if source.startswith("import"):
        return "imported"
    if source in {
        "compiler", "compiler_sidecar", "compiled_payload", "live_checkpoint",
        "user_checkpoint_edit",
    }:
        return "assistant"
    return "unknown"


def _quality_fingerprint(row: ScenarioModelDraftResource) -> str:
    # Origin is intentionally absent: equal definitions at the same revision
    # receive the same quality identity whether a person or model authored it.
    canonical = {
        "resource_kind": str(row.resource_kind or ""),
        "resource_key": str(row.resource_key or ""),
        "payload": row.payload if isinstance(row.payload, dict) else {},
        "revision": max(int(row.revision or 0), 0),
    }
    return hashlib.sha256(json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def governance_projection(
    row: ScenarioModelDraftResource,
    *,
    include_stored_issues: bool = True,
) -> dict[str, Any]:
    issues = (
        _bounded_issues(row.validation_issues)
        if include_stored_issues
        else []
    )
    blockers = [item for item in issues if item.get("blocking", True)]
    status = str(row.draft_status or "needs_attention")
    kind = str(row.resource_kind or "")
    if kind not in FORMAL_RESOURCE_KINDS:
        blockers.append(_issue(
            "candidate_kind_not_formalizable",
            "该候选当前仅用于建模证据或草稿表达，尚无对应的正式定义类型。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
            resolution_hint="保留为建模候选，或先转换为平台支持的正式定义类型。",
        ))
    if status == "needs_validation" and not blockers:
        blockers.append(_issue(
            "candidate_revalidation_required",
            "候选内容已变化，必须针对当前 revision 重新校验。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
            resolution_hint="调用候选重新校验接口。",
        ))
    elif status == "needs_attention" and not blockers:
        blockers.append(_issue(
            "candidate_needs_attention",
            "候选仍需处理质量问题后才能正式化。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
        ))
    if status == "deferred":
        blockers.append(_issue(
            "candidate_is_deferred",
            "候选已被延期，需重新校验后才能进入正式化流程。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
        ))
    if status in {"resolved", "applied", "superseded"}:
        blockers.append(_issue(
            "candidate_lifecycle_closed",
            "候选生命周期已关闭，不能再次正式化。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
        ))
    blockers = _bounded_issues(blockers)
    if status == "needs_validation":
        validation_status = "not_validated"
    elif blockers:
        validation_status = "invalid"
    else:
        validation_status = "valid"
    lifecycle_status = (
        "formalized" if status == "applied"
        else "resolved" if status == "resolved"
        else "superseded" if status == "superseded"
        else "deferred" if status == "deferred"
        else "candidate"
    )
    return {
        "materialization_source": str(row.materialization_source or ""),
        "source_origin": source_origin(row),
        "validation_status": validation_status,
        "lifecycle_status": lifecycle_status,
        "promotion_eligible": (
            validation_status == "valid"
            and status in OPEN_LIFECYCLE_STATUSES - {"deferred"}
            and kind in FORMAL_RESOURCE_KINDS
        ),
        "promotion_blockers": blockers,
        "activation_status": (
            "inactive" if kind in ACTIVATABLE_RESOURCE_KINDS else "not_applicable"
        ),
        "quality_fingerprint": _quality_fingerprint(row),
    }


def governance_summary(
    rows: Iterable[ScenarioModelDraftResource],
    *,
    include_stored_issues: bool = True,
) -> dict[str, Any]:
    """Return candidate/formalisation counts without mixing source and quality."""
    values = list(rows)
    projections = [
        governance_projection(
            row, include_stored_issues=include_stored_issues
        )
        for row in values
    ]
    by_origin: dict[str, int] = defaultdict(int)
    by_validation: dict[str, int] = defaultdict(int)
    for item in projections:
        by_origin[str(item["source_origin"])] += 1
        by_validation[str(item["validation_status"])] += 1
    return {
        "candidate_count": sum(
            item["lifecycle_status"] in {"candidate", "deferred"}
            for item in projections
        ),
        "formalized_count": sum(
            item["lifecycle_status"] in {"formalized", "resolved"}
            for item in projections
        ),
        "promotion_eligible_count": sum(
            bool(item["promotion_eligible"]) for item in projections
        ),
        "promotion_blocked_count": sum(
            item["lifecycle_status"] in {"candidate", "deferred"}
            and not bool(item["promotion_eligible"])
            for item in projections
        ),
        "by_origin": dict(sorted(by_origin.items())),
        "by_validation": dict(sorted(by_validation.items())),
    }


def _candidate_ref(row: ScenarioModelDraftResource) -> str:
    return f"candidate:{row.id}:r{max(int(row.revision or 0), 0)}"


def _reference(value: Any, direct_value: Any, selected_keys: set[str]) -> Any:
    if isinstance(value, dict):
        return _json_copy(value, {})
    token = str(direct_value or value or "").strip()
    if not token:
        return None
    if token in selected_keys:
        return {"kind": "generated", "key": token}
    return {"kind": "existing", "id": token}


def _required_name(row: ScenarioModelDraftResource, item: dict[str, Any]) -> None:
    if not str(item.get("name") or "").strip():
        raise CandidatePromotionBlocked([_issue(
            "candidate_name_required",
            "正式定义必须包含非空 name。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
            field_path=["payload", "name"],
        )])


def _canonical_item(
    row: ScenarioModelDraftResource,
    *,
    selected_keys: dict[str, set[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kind = str(row.resource_kind or "")
    item = _json_copy(row.payload if isinstance(row.payload, dict) else {}, {})
    item["key"] = str(row.resource_key)
    item["evidence_refs"] = [_candidate_ref(row)]
    warnings: list[dict[str, Any]] = []

    if kind == "entity":
        _required_name(row, item)
        existing_id = str(item.get("existing_id") or "")
        item["operation"] = str(
            item.get("operation") or ("update" if existing_id else "add")
        )
        properties = item.get("properties") if isinstance(item.get("properties"), list) else []
        normalized_properties: list[dict[str, Any]] = []
        for raw_property in properties:
            if not isinstance(raw_property, dict):
                continue
            prop = _json_copy(raw_property, {})
            if not existing_id:
                prop.setdefault("_operation", "add")
            normalized_properties.append(prop)
        item["properties"] = normalized_properties
    elif kind == "relation":
        _required_name(row, item)
        item["source"] = _reference(
            item.get("source"), item.get("source_entity_id"), selected_keys["entity"]
        )
        item["target"] = _reference(
            item.get("target"), item.get("target_entity_id"), selected_keys["entity"]
        )
        if item.get("inverse_relation"):
            item["inverse_relation"] = _reference(
                item.get("inverse_relation"),
                item.get("inverse_relation_id"),
                selected_keys["relation"],
            )
        existing_id = str(item.get("existing_id") or "")
        item["operation"] = str(
            item.get("operation") or ("update" if existing_id else "add")
        )
    elif kind == "function":
        _required_name(row, item)
        # Raw compiler candidates may omit optional Function fields even
        # though their normalized peer received governed defaults. Preserve
        # explicit invalid edits for validation, but fill true omissions.
        item.setdefault("tags", [])
        item.setdefault("visibility", "scenario")
        item.setdefault("runtime_kind", "contract")
        item.setdefault("runtime_config", {})
    elif kind == "action":
        _required_name(row, item)
        item["entity"] = _reference(
            item.get("entity"), item.get("entity_id"), selected_keys["entity"]
        )
        if str(item.get("executor_type") or "unbound") != "unbound" or item.get("executor_config"):
            raise CandidatePromotionBlocked([_issue(
                "action_binding_requires_separate_governance",
                "Action 候选只能先正式化为 unbound 定义；执行器绑定必须走独立治理流程。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
                field_path=["payload", "executor_type"],
                resolution_hint="将 executor_type 设为 unbound 且清空 executor_config。",
            )])
        if item.get("enabled") is True:
            warnings.append(_issue(
                "activation_deferred",
                "Action 已通过定义校验，但正式化不会自动激活。",
                blocking=False,
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            ))
        item["executor_type"] = "unbound"
        item["executor_config"] = {}
        item["enabled"] = False
    elif kind == "rule":
        _required_name(row, item)
        item["entity"] = _reference(
            item.get("entity"), item.get("entity_id"), selected_keys["entity"]
        ) if item.get("entity") or item.get("entity_id") else None
        trigger_values = (
            item.get("trigger_actions")
            if isinstance(item.get("trigger_actions"), list)
            else item.get("trigger_action_ids")
            if isinstance(item.get("trigger_action_ids"), list)
            else []
        )
        item["trigger_actions"] = [
            _reference(value, value, selected_keys["action"])
            for value in trigger_values
        ]
        if item.get("enabled") is True:
            warnings.append(_issue(
                "activation_deferred",
                "Rule 已通过定义校验，但正式化不会自动激活。",
                blocking=False,
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            ))
        item["enabled"] = False
    elif kind == "event":
        _required_name(row, item)
        if item.get("enabled") is True:
            warnings.append(_issue(
                "activation_deferred",
                "Event 已通过定义校验，但正式化不会自动激活。",
                blocking=False,
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            ))
        item["enabled"] = False
    elif kind == "workflow":
        _required_name(row, item)
        nodes: list[dict[str, Any]] = []
        for raw_node in item.get("nodes") or []:
            if not isinstance(raw_node, dict):
                continue
            node = _json_copy(raw_node, {})
            data = dict(node.get("data") or {})
            node_kind = str(node.get("type") or "")
            id_field = {"action": "action_id", "rule": "rule_id", "event": "event_id"}.get(node_kind)
            if id_field and "resource" not in data and data.get(id_field):
                data["resource"] = _reference(
                    None, data.pop(id_field), selected_keys[f"{node_kind}"]
                )
            node["data"] = data
            nodes.append(node)
        item["nodes"] = nodes
        if item.get("steps"):
            raise CandidatePromotionBlocked([_issue(
                "workflow_steps_require_dag_conversion",
                "候选正式化协议只接受已校验的 DAG nodes/edges，不能静默丢弃旧版 steps。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
                field_path=["payload", "steps"],
                resolution_hint="先将线性 steps 转换为 nodes/edges 并重新校验。",
            )])
        if item.get("trigger_type") == "event" and not item.get("trigger_event"):
            trigger_config = dict(item.get("trigger_config") or {})
            event_id = trigger_config.pop("event_id", "")
            item["trigger_config"] = trigger_config
            item["trigger_event"] = _reference(
                None, event_id, selected_keys["event"]
            )
        elif item.get("trigger_type") == "event":
            item["trigger_event"] = _reference(
                item.get("trigger_event"),
                item.get("trigger_event"),
                selected_keys["event"],
            )
        if item.get("enabled") is True or item.get("status") == "active":
            warnings.append(_issue(
                "activation_deferred",
                "Workflow 已通过定义校验，但正式化只会创建停用草稿。",
                blocking=False,
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            ))
        item["status"] = "draft"
        item["enabled"] = False
    elif kind == "mapping":
        item["entity"] = _reference(
            item.get("entity"), item.get("entity_id"), selected_keys["entity"]
        )
        item["data_source"] = _reference(
            item.get("data_source"), item.get("data_source_id"), set()
        )
        item.setdefault("apply_plan", {"mode": "add"})
    elif kind == "relation_mapping":
        item["relation"] = _reference(
            item.get("relation"), item.get("relation_id"), selected_keys["relation"]
        )
        item["source_mapping"] = _reference(
            item.get("source_mapping"), item.get("source_mapping_id"), selected_keys["mapping"]
        )
        item["target_mapping"] = _reference(
            item.get("target_mapping"), item.get("target_mapping_id"), selected_keys["mapping"]
        )
        if str(item.get("mode") or "") == "join_table":
            item["join_data_source"] = _reference(
                item.get("join_data_source"), item.get("join_data_source_id"), set()
            )
        item.setdefault("apply_plan", {"mode": "add"})
    return item, warnings


def _current_property_payload(prop: OntologyProperty) -> dict[str, Any]:
    return {
        "name": prop.name,
        "api_name": prop.api_name or "",
        "data_type": prop.data_type or "string",
        "description": prop.description or "",
        "is_key": bool(prop.is_key),
        "is_title": bool(prop.is_title),
        "is_required": bool(prop.is_required),
        "is_enum": bool(prop.is_enum),
        "enum_values": list(prop.enum_values or []),
        "default_value": _json_copy(prop.default_value, ""),
        "constraints": _json_copy(prop.constraints, {}),
        "is_sensitive": bool(prop.is_sensitive),
        "_operation": "skip",
    }


def _existing_entity_item(entity: OntologyEntity, key: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "existing_id": entity.id,
        "operation": "update",
        "name": entity.name,
        "api_name": entity.api_name or "",
        "description": entity.description or "",
        "is_abstract": bool(entity.is_abstract),
        "state_property": entity.state_property or "",
        "properties": [_current_property_payload(prop) for prop in entity.properties],
        "evidence_refs": list(evidence),
    }


def _entity_for_property(
    db: Session,
    scenario: BusinessScenario,
    row: ScenarioModelDraftResource,
    parent_ref: str,
) -> OntologyEntity | None:
    direct = db.get(OntologyEntity, parent_ref) if parent_ref else None
    if direct and direct.scenario_id == scenario.id:
        return direct
    resolved_draft = db.scalars(
        select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.tenant_id == row.tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == row.created_by_user_id,
            ScenarioModelDraftResource.resource_kind == "entity",
            ScenarioModelDraftResource.resource_key == parent_ref,
            ScenarioModelDraftResource.resolved_resource_id != "",
        ).order_by(ScenarioModelDraftResource.updated_at.desc())
    ).first()
    if resolved_draft:
        entity = db.get(OntologyEntity, resolved_draft.resolved_resource_id)
        if entity and entity.scenario_id == scenario.id:
            return entity
    # Machine identities are safe deterministic references; display-name
    # guessing is deliberately excluded.
    matches = list(db.scalars(select(OntologyEntity).where(
        OntologyEntity.scenario_id == scenario.id,
        OntologyEntity.api_name == parent_ref,
    )).all())
    return matches[0] if len(matches) == 1 else None


def _property_parent_key(row: ScenarioModelDraftResource) -> str:
    payload = row.payload if isinstance(row.payload, dict) else {}
    value = str(payload.get("entity_ref") or "").strip()
    if value:
        return value
    key = str(row.resource_key or "")
    return key.split(":property:", 1)[0] if ":property:" in key else ""


def _property_payload(
    row: ScenarioModelDraftResource,
    *,
    existing_entity: OntologyEntity | None,
) -> dict[str, Any]:
    result = _json_copy(row.payload if isinstance(row.payload, dict) else {}, {})
    result.pop("entity_ref", None)
    if not str(result.get("name") or "").strip():
        raise CandidatePromotionBlocked([_issue(
            "property_name_required",
            "属性候选必须包含非空 name。",
            draft_ids=[row.id],
            resource_keys=[row.resource_key],
            field_path=["payload", "name"],
        )])
    existing: OntologyProperty | None = None
    if existing_entity:
        api_name = str(result.get("api_name") or "")
        name = str(result.get("name") or "")
        matches = [
            prop for prop in existing_entity.properties
            if (api_name and str(prop.api_name or "") == api_name)
            or (not api_name and str(prop.name or "") == name)
        ]
        if len(matches) > 1:
            raise CandidatePromotionBlocked([_issue(
                "property_identity_ambiguous",
                "属性候选在父对象类型中匹配到多个正式属性。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            )])
        existing = matches[0] if matches else None
    result.setdefault("_operation", "update" if existing else "add")
    return result


def _overlay_property(
    entity_item: dict[str, Any],
    property_item: dict[str, Any],
) -> None:
    properties = [
        _json_copy(item, {})
        for item in (entity_item.get("properties") or [])
        if isinstance(item, dict)
    ]
    api_name = str(property_item.get("api_name") or "")
    name = str(property_item.get("name") or "")
    matches = [
        index for index, item in enumerate(properties)
        if (api_name and str(item.get("api_name") or "") == api_name)
        or (not api_name and str(item.get("name") or "") == name)
    ]
    if len(matches) > 1:
        raise CandidatePromotionBlocked([_issue(
            "property_identity_ambiguous",
            "属性候选在对象候选中匹配到多个属性。",
            resource_keys=[str(entity_item.get("key") or "")],
        )])
    if matches:
        properties[matches[0]] = property_item
    else:
        properties.append(property_item)
    entity_item["properties"] = properties


def _build_compound_payload(
    db: Session,
    scenario: BusinessScenario,
    rows: list[ScenarioModelDraftResource],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    selected_keys: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        selected_keys[str(row.resource_kind or "")].add(str(row.resource_key or ""))
    selected_ids = {row.id for row in rows}
    if selected_keys["entity"]:
        property_rows = list(db.scalars(select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.tenant_id == str(scenario.tenant_id or ""),
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id
            == str(rows[0].created_by_user_id or ""),
            ScenarioModelDraftResource.resource_kind == "property",
            ScenarioModelDraftResource.draft_status.in_(
                scenario_model_draft_service.OPEN_DRAFT_STATUSES
            ),
        )).all())
        omitted_edits = [
            row for row in property_rows
            if row.id not in selected_ids
            and _property_parent_key(row) in selected_keys["entity"]
            and (
                int(row.revision or 0) > 0
                or row.draft_status == "needs_validation"
            )
        ]
        if omitted_edits:
            raise CandidatePromotionBlocked([_issue(
                "edited_property_candidate_not_selected",
                "对象候选包含已被单独编辑的属性候选，不能用聚合对象 payload 覆盖该修订。",
                draft_ids=[row.id for row in omitted_edits],
                resource_keys=[row.resource_key for row in omitted_edits],
                resolution_hint="将这些属性候选及其 expected_revision 加入同一原子晋级批次。",
            )])
    items_by_key: dict[str, dict[str, Any]] = {}
    item_section: dict[str, str] = {}
    row_resource_keys: dict[str, str] = {}
    evidence_by_key: dict[str, list[str]] = defaultdict(list)
    warnings: list[dict[str, Any]] = []

    for row in rows:
        kind = str(row.resource_kind or "")
        if kind not in FORMAL_RESOURCE_KINDS:
            raise CandidatePromotionBlocked([_issue(
                "candidate_kind_not_formalizable",
                "该候选类型没有对应的正式定义写入协议。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            )])
        if kind == "property":
            continue
        item, item_warnings = _canonical_item(row, selected_keys=selected_keys)
        if kind == "entity" and str(item.get("existing_id") or ""):
            existing_entity = db.get(
                OntologyEntity, str(item.get("existing_id") or "")
            )
            if existing_entity and existing_entity.scenario_id == scenario.id:
                existing_api_names = {
                    str(prop.api_name or "") for prop in existing_entity.properties
                    if str(prop.api_name or "")
                }
                existing_names = {
                    str(prop.name or "") for prop in existing_entity.properties
                }
                for prop in item.get("properties") or []:
                    if "_operation" in prop:
                        continue
                    prop["_operation"] = (
                        "skip"
                        if (
                            str(prop.get("api_name") or "") in existing_api_names
                            or str(prop.get("name") or "") in existing_names
                        )
                        else "add"
                    )
        key = str(item.get("key") or "")
        if key in items_by_key:
            raise CandidatePromotionBlocked([_issue(
                "candidate_resource_key_duplicate",
                "同一批次包含重复的正式资源 key。",
                draft_ids=[row.id],
                resource_keys=[key],
            )])
        items_by_key[key] = item
        item_section[key] = _SECTION_BY_KIND[kind]
        row_resource_keys[row.id] = key
        evidence_by_key[key].append(_candidate_ref(row))
        warnings.extend(item_warnings)

    for row in rows:
        if row.resource_kind != "property":
            continue
        parent_key = _property_parent_key(row)
        if not parent_key:
            raise CandidatePromotionBlocked([_issue(
                "property_parent_required",
                "属性候选缺少稳定的父对象类型引用。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
                field_path=["payload", "entity_ref"],
            )])
        entity_item = items_by_key.get(parent_key)
        existing_entity: OntologyEntity | None = None
        if entity_item is None:
            existing_entity = _entity_for_property(db, scenario, row, parent_key)
            if existing_entity is None:
                raise CandidatePromotionBlocked([_issue(
                    "property_parent_not_formal",
                    "属性候选的父对象类型尚未正式化，且未包含在本次原子晋级中。",
                    draft_ids=[row.id],
                    resource_keys=[row.resource_key, parent_key],
                    resolution_hint="将父对象候选一并加入批量晋级，或先正式化父对象类型。",
                )])
            entity_item = _existing_entity_item(
                existing_entity, parent_key, [_candidate_ref(row)]
            )
            items_by_key[parent_key] = entity_item
            item_section[parent_key] = "entities"
        else:
            existing_id = str(entity_item.get("existing_id") or "")
            existing_entity = db.get(OntologyEntity, existing_id) if existing_id else None
        prop = _property_payload(row, existing_entity=existing_entity)
        _overlay_property(entity_item, prop)
        row_resource_keys[row.id] = parent_key
        evidence_by_key[parent_key].append(_candidate_ref(row))

    sections = {
        section: []
        for section in (
            "entities", "relations", "functions", "actions", "rules",
            "events", "workflows", "mappings", "relation_mappings",
            "instances", "conceptual_mappings",
        )
    }
    for key in sorted(items_by_key):
        item = items_by_key[key]
        item["evidence_refs"] = sorted(set(evidence_by_key[key]))
        sections[item_section[key]].append(item)

    for section in (
        "entities", "relations", "functions", "actions", "rules", "events",
        "workflows",
    ):
        names = [
            str(item.get("name") or "").strip()
            for item in sections[section]
        ]
        duplicates = sorted({name for name in names if name and names.count(name) > 1})
        if duplicates:
            raise CandidatePromotionBlocked([_issue(
                "candidate_formal_name_duplicate",
                f"同一原子批次的 {section} 包含重复正式名称。",
                resource_keys=[
                    str(item.get("key") or "")
                    for item in sections[section]
                    if str(item.get("name") or "").strip() in duplicates
                ],
                resolution_hint="为候选设置唯一的正式 name 后重新校验。",
            )])

    source_refs = sorted({_candidate_ref(row) for row in rows})
    coverage = []
    for row in sorted(rows, key=lambda value: value.id):
        key = row_resource_keys[row.id]
        coverage.append({
            "source_ref": _candidate_ref(row),
            "status": "modeled",
            "reason": "当前候选 revision 通过统一正式定义治理边界进行校验。",
            "change_keys": [key],
        })
    changes = []
    for key in sorted(items_by_key):
        item = items_by_key[key]
        operation = str(item.get("operation") or "")
        if not operation:
            plan = item.get("apply_plan") if isinstance(item.get("apply_plan"), dict) else {}
            operation = "update" if plan.get("mode") == "update" else "add"
        if operation == "skip":
            operation = "update"
        changes.append({"change_id": key, "operation": operation})
    payload = {
        "schema_version": scenario_model_compiler.SCHEMA_VERSION,
        **sections,
        "changes": changes,
        "unresolved": [],
        "source_refs": source_refs,
        "source_paragraph_count": len(source_refs),
        "source_manifest": [
            {"source_ref": value, "source_kind": "candidate_revision"}
            for value in source_refs
        ],
        "coverage": coverage,
        "coverage_summary": {
            "total": len(coverage),
            "modeled": len(coverage),
            "context": 0,
            "irrelevant": 0,
            "ambiguous": 0,
        },
    }
    return payload, _bounded_issues(warnings), row_resource_keys


def _validate_capability_port_candidates(
    db: Session,
    scenario: BusinessScenario,
    rows: Iterable[ScenarioModelDraftResource],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str]]:
    """Validate logical port contracts without binding them to runtime data."""
    values = list(rows)
    existing_by_key: dict[
        tuple[str, str, str], list[ScenarioCapabilityPort]
    ] = defaultdict(list)
    for port in db.scalars(select(ScenarioCapabilityPort).where(
        ScenarioCapabilityPort.tenant_id == str(scenario.tenant_id or ""),
        ScenarioCapabilityPort.scenario_id == scenario.id,
    )).all():
        config = port.config if isinstance(port.config, dict) else {}
        source = (
            config.get("contract_source")
            if isinstance(config.get("contract_source"), dict)
            else {}
        )
        source_kind = str(
            source.get("resource_kind") or port.capability_kind or ""
        ).strip().lower()
        source_key = str(
            source.get("resource_key") or port.capability_key or ""
        ).strip()
        existing_by_key[
            (source_kind, source_key, str(port.port_key or "").casefold())
        ].append(port)

    payloads: dict[str, dict[str, Any]] = {}
    existing_ids: dict[str, str] = {}
    row_resource_keys: dict[str, str] = {}
    selected_by_key: dict[
        tuple[str, str, str], list[ScenarioModelDraftResource]
    ] = defaultdict(list)
    blockers: list[dict[str, Any]] = []
    for row in values:
        try:
            port = assistant_capability_modeling_service.normalize_port_candidate(
                row.payload
            )
        except (ValueError, TypeError) as exc:
            blockers.append(_issue(
                "capability_port_contract_invalid",
                str(exc) or "能力端口候选未通过逻辑契约校验。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
                field_path=["payload"],
                resolution_hint=(
                    "只保留逻辑端口、JSON Schema 和语义证据；"
                    "删除数据源、资产、数据集或版本绑定并保持 status=draft。"
                ),
            ))
            continue
        folded_key = port.port_key.casefold()
        identity = (port.capability_kind, port.capability_key, folded_key)
        selected_by_key[identity].append(row)
        payloads[row.id] = port.model_dump(mode="json", exclude_none=True)
        row_resource_keys[row.id] = port.port_key
        existing = existing_by_key.get(identity, [])
        if len(existing) > 1:
            blockers.append(_issue(
                "capability_port_identity_ambiguous",
                "场景中存在大小写等价的多个能力端口，无法确定候选更新目标。",
                draft_ids=[row.id],
                resource_keys=[port.port_key],
                resolution_hint="先整理重复端口 key，再重新校验候选。",
            ))
        elif existing:
            current = existing[0]
            if str(current.status or "") != "draft":
                blockers.append(_issue(
                    "capability_port_active_definition_protected",
                    "候选不能覆盖已激活或已退役的能力端口。",
                    draft_ids=[row.id],
                    resource_keys=[port.port_key],
                    resolution_hint="为变更创建新的 draft 端口 key，并通过独立发布流程替换。",
                ))
            elif current.dataset_id or current.dataset_schema_id:
                blockers.append(_issue(
                    "capability_port_bound_draft_protected",
                    "候选不能覆盖已由用户显式绑定 Schema 的 draft 能力端口。",
                    draft_ids=[row.id],
                    resource_keys=[port.port_key],
                    resolution_hint=(
                        "先在资源目录中审阅现有端口绑定，或为建议选择新的端口 key。"
                    ),
                ))
            else:
                existing_ids[row.id] = str(current.id)

    for identity, matching_rows in selected_by_key.items():
        if len(matching_rows) <= 1:
            continue
        blockers.append(_issue(
            "capability_port_key_duplicate",
            "同一原子晋级批次包含大小写等价的重复能力端口 key。",
            draft_ids=[row.id for row in matching_rows],
            resource_keys=[
                row_resource_keys.get(row.id, identity[2])
                for row in matching_rows
            ],
            resolution_hint="每个逻辑端口 key 只保留一个候选。",
        ))
    if blockers:
        raise CandidatePromotionBlocked(_bounded_issues(blockers))
    return payloads, existing_ids, row_resource_keys


def evaluate_candidates(
    db: Session,
    scenario: BusinessScenario,
    rows: Iterable[ScenarioModelDraftResource],
) -> CandidateEvaluation:
    values = sorted(list(rows), key=lambda row: (
        str(row.resource_kind or ""), str(row.resource_key or ""), row.id
    ))
    fingerprint_input = [{
        "id": row.id,
        "kind": row.resource_kind,
        "key": row.resource_key,
        "revision": int(row.revision or 0),
        "payload": row.payload if isinstance(row.payload, dict) else {},
    } for row in values]
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    if not values:
        blockers = [_issue("candidate_selection_empty", "至少选择一个候选定义。")]
        return CandidateEvaluation(False, blockers, [], None, fingerprint, {})
    if len(values) > MAX_PROMOTION_BATCH:
        blockers = [_issue(
            "candidate_batch_too_large",
            f"单次最多晋级 {MAX_PROMOTION_BATCH} 个候选定义。",
            draft_ids=[row.id for row in values],
        )]
        return CandidateEvaluation(False, blockers, [], None, fingerprint, {})
    try:
        port_rows = [
            row for row in values if row.resource_kind == "capability_port"
        ]
        compound_rows = [
            row for row in values if row.resource_kind != "capability_port"
        ]
        port_payloads, port_existing_ids, port_resource_keys = (
            _validate_capability_port_candidates(db, scenario, port_rows)
            if port_rows
            else ({}, {}, {})
        )
        if compound_rows:
            payload, warnings, row_resource_keys = _build_compound_payload(
                db, scenario, compound_rows
            )
            scenario_model_compiler.preflight_scenario_model(
                db, scenario, payload, inspect_mappings=True
            )
        else:
            payload, warnings, row_resource_keys = {}, [], {}
        row_resource_keys.update(port_resource_keys)
        payload[_PORT_PAYLOADS_KEY] = port_payloads
        payload[_PORT_EXISTING_IDS_KEY] = port_existing_ids
        return CandidateEvaluation(
            True, [], warnings, payload, fingerprint, row_resource_keys
        )
    except CandidatePromotionBlocked as exc:
        return CandidateEvaluation(
            False, _bounded_issues(exc.blockers), [], None, fingerprint, {}
        )
    except (PolicyViolation, ValueError, TypeError, KeyError) as exc:
        blockers = [_issue(
            "formal_preflight_failed",
            str(exc) or "候选定义未通过正式化预检。",
            draft_ids=[row.id for row in values],
            resource_keys=[row.resource_key for row in values],
            resolution_hint="修正候选 payload 或将缺失依赖加入同一原子晋级批次后重新校验。",
        )]
        return CandidateEvaluation(False, blockers, [], None, fingerprint, {})


def create_manual_candidate(
    db: Session,
    scenario: BusinessScenario,
    *,
    tenant_id: str,
    created_by_user_id: str,
    resource_kind: str,
    resource_key: str,
    title: str,
    payload: dict[str, Any],
    task_id: str = "",
    source_refs: Iterable[str] = (),
) -> ScenarioModelDraftResource:
    safe_payload = release_service.safe_snapshot_content(
        _json_copy(payload, {})
    )
    serialized = json.dumps(safe_payload, ensure_ascii=False, default=str)
    if len(serialized) > scenario_model_draft_service.MAX_DRAFT_PAYLOAD_CHARS:
        raise ValueError("候选定义超过 1,000,000 字符上限")
    kind = scenario_model_draft_service.normalize_resource_kind(resource_kind)
    if not kind:
        raise ValueError("不支持的候选资源类型")
    key = str(resource_key or "").strip()
    if not key:
        raise ValueError("候选 resource_key 不能为空")
    proposal_id = f"manual-{uuid.uuid4().hex}"[:64]
    identity = hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()
    row = ScenarioModelDraftResource(
        tenant_id=tenant_id,
        scenario_id=scenario.id,
        created_by_user_id=created_by_user_id,
        source_thread_id="",
        source_message_id="",
        compilation_job_id="",
        proposal_id=proposal_id,
        lineage_started_at=datetime.now(timezone.utc),
        task_id=str(task_id or _DEFAULT_TASK_BY_KIND.get(kind, ""))[:80],
        resource_kind=kind,
        resource_key=key[:500],
        resource_identity=identity,
        title=str(
            title
            or safe_payload.get("display_name")
            or safe_payload.get("name")
            or key
        )[:300],
        source_payload=_json_copy(safe_payload, {}),
        payload=_json_copy(safe_payload, {}),
        validation_issues=[_issue(
            "candidate_revalidation_required",
            "人工候选已登记，必须通过与其他来源相同的确定性校验后才能正式化。",
            draft_ids=[],
            resource_keys=[key],
            resolution_hint="调用候选重新校验接口。",
        )],
        source_refs=list(dict.fromkeys(
            str(value)[:300] for value in source_refs if str(value).strip()
        ))[:100],
        materialization_source="manual",
        draft_status="needs_validation",
        enabled=False,
        publishable=False,
        revision=0,
    )
    db.add(row)
    db.flush()
    # Add the durable id to the structured blocker after insertion.
    row.validation_issues = [_issue(
        "candidate_revalidation_required",
        "人工候选已登记，必须通过与其他来源相同的确定性校验后才能正式化。",
        draft_ids=[row.id],
        resource_keys=[key],
        resolution_hint="调用候选重新校验接口。",
    )]
    return row


def _lock_owned_candidate(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    created_by_user_id: str,
    draft_id: str,
) -> ScenarioModelDraftResource:
    row = db.scalars(select(ScenarioModelDraftResource).where(
        ScenarioModelDraftResource.id == draft_id,
        ScenarioModelDraftResource.tenant_id == tenant_id,
        ScenarioModelDraftResource.scenario_id == scenario_id,
        ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
    ).with_for_update()).first()
    if row is None:
        raise CandidateNotFound("候选定义不存在")
    return row


def revalidate_candidate(
    db: Session,
    scenario: BusinessScenario,
    *,
    tenant_id: str,
    created_by_user_id: str,
    draft_id: str,
    expected_revision: int,
) -> tuple[ScenarioModelDraftResource, CandidateEvaluation]:
    row = _lock_owned_candidate(
        db,
        tenant_id=tenant_id,
        scenario_id=scenario.id,
        created_by_user_id=created_by_user_id,
        draft_id=draft_id,
    )
    if int(row.revision or 0) != expected_revision:
        raise CandidateRevisionConflict("候选定义 revision 已变化，请刷新后重试")
    if row.draft_status not in OPEN_LIFECYCLE_STATUSES:
        raise CandidateRevisionConflict("候选定义生命周期已关闭")
    evaluation = evaluate_candidates(db, scenario, [row])
    row.validation_issues = _bounded_issues([
        *evaluation.blockers, *evaluation.warnings,
    ])
    row.draft_status = "ready_for_review" if evaluation.eligible else "needs_attention"
    row.enabled = False
    row.publishable = False
    row.revision = expected_revision + 1
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return row, evaluation


def _find_one_by_name(
    db: Session,
    model: Any,
    scenario_id: str,
    name: str,
) -> Any | None:
    values = list(db.scalars(select(model).where(
        model.scenario_id == scenario_id,
        model.name == name,
    )).all())
    return values[0] if len(values) == 1 else None


def _formal_ids_by_key(
    db: Session,
    scenario: BusinessScenario,
    payload: dict[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    model_by_section = {
        "entities": OntologyEntity,
        "relations": OntologyRelation,
        "functions": FunctionDefinition,
        "actions": OntologyAction,
        "rules": OntologyRule,
        "events": OntologyEvent,
        "workflows": OntologyWorkflow,
    }
    for section, model in model_by_section.items():
        for item in payload.get(section) or []:
            existing_id = str(item.get("existing_id") or "")
            formal = db.get(model, existing_id) if existing_id else _find_one_by_name(
                db, model, scenario.id, str(item.get("name") or "")
            )
            if formal is not None and str(formal.scenario_id) == scenario.id:
                result[str(item.get("key") or "")] = str(formal.id)
    for item in payload.get("mappings") or []:
        plan = item.get("apply_plan") if isinstance(item.get("apply_plan"), dict) else {}
        mapping = db.get(DataMapping, str(plan.get("canonical_id") or ""))
        if mapping is None:
            entity_ref = item.get("entity") if isinstance(item.get("entity"), dict) else {}
            entity_id = (
                result.get(str(entity_ref.get("key") or ""), "")
                if entity_ref.get("kind") == "generated"
                else str(entity_ref.get("id") or "")
            )
            source_ref = item.get("data_source") if isinstance(item.get("data_source"), dict) else {}
            source_id = str(source_ref.get("id") or "")
            candidates = list(db.scalars(select(DataMapping).where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.entity_id == entity_id,
                DataMapping.data_source_id == source_id,
                DataMapping.table_name == str(item.get("table_name") or ""),
            )).all())
            mapping = candidates[0] if len(candidates) == 1 else None
        if mapping is not None and mapping.scenario_id == scenario.id:
            result[str(item.get("key") or "")] = str(mapping.id)
    for item in payload.get("relation_mappings") or []:
        plan = item.get("apply_plan") if isinstance(item.get("apply_plan"), dict) else {}
        relation_mapping = db.get(
            RelationDataMapping, str(plan.get("existing_id") or "")
        )
        if relation_mapping is None:
            relation_ref = item.get("relation") if isinstance(item.get("relation"), dict) else {}
            relation_id = (
                result.get(str(relation_ref.get("key") or ""), "")
                if relation_ref.get("kind") == "generated"
                else str(relation_ref.get("id") or "")
            )
            values = list(db.scalars(select(RelationDataMapping).where(
                RelationDataMapping.scenario_id == scenario.id,
                RelationDataMapping.relation_id == relation_id,
            )).all())
            relation_mapping = values[0] if len(values) == 1 else None
        if relation_mapping is not None and relation_mapping.scenario_id == scenario.id:
            result[str(item.get("key") or "")] = str(relation_mapping.id)
    return result


_CAPABILITY_MODEL_BY_KIND = {
    "function": FunctionDefinition,
    "action": OntologyAction,
    "workflow": OntologyWorkflow,
}


def _resolved_port_capability_key(
    db: Session,
    scenario: BusinessScenario,
    port: ScenarioCapabilityPortCreate,
    *,
    compiler_ids: dict[str, str],
    existing_port_id: str = "",
) -> str:
    model = _CAPABILITY_MODEL_BY_KIND[port.capability_kind]
    candidates: set[str] = set()
    explicit = str(port.capability_key or "").strip()
    compiler_id = str(compiler_ids.get(explicit) or "")
    if compiler_id:
        candidates.add(compiler_id)
    direct = db.get(model, explicit)
    if direct is not None and str(direct.scenario_id or "") == scenario.id:
        candidates.add(str(direct.id))
    if existing_port_id:
        existing = db.get(ScenarioCapabilityPort, existing_port_id)
        if (
            existing is not None
            and existing.scenario_id == scenario.id
            and existing.capability_kind == port.capability_kind
        ):
            candidates.add(str(existing.capability_key))
    resolved_ids = db.scalars(select(
        ScenarioModelDraftResource.resolved_resource_id
    ).where(
        ScenarioModelDraftResource.tenant_id == str(scenario.tenant_id or ""),
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.resource_kind == port.capability_kind,
        ScenarioModelDraftResource.resource_key == explicit,
        ScenarioModelDraftResource.draft_status == "resolved",
        ScenarioModelDraftResource.resolved_resource_id != "",
    )).all()
    candidates.update(str(value) for value in resolved_ids if str(value or "").strip())
    governed = {
        candidate
        for candidate in candidates
        if (
            (target := db.get(model, candidate)) is not None
            and str(target.scenario_id or "") == scenario.id
        )
    }
    if len(governed) != 1:
        raise CandidatePromotionBlocked([_issue(
            "capability_port_owner_unresolved",
            "能力端口所属能力无法由正式 ID 或已解决候选唯一确认，本批次将整体回滚。",
            resource_keys=[port.port_key],
            field_path=["payload", "capability_key"],
            resolution_hint="将所属能力与端口放入同一原子晋级批次，或提供正式能力 ID。",
        )])
    return next(iter(governed))


def _resolved_property_id(
    db: Session,
    scenario: BusinessScenario,
    row: ScenarioModelDraftResource,
    parent_resource_id: str,
) -> str:
    entity = db.get(OntologyEntity, parent_resource_id)
    if not entity or entity.scenario_id != scenario.id:
        return ""
    payload = row.payload if isinstance(row.payload, dict) else {}
    api_name = str(payload.get("api_name") or "")
    name = str(payload.get("name") or "")
    values = [
        prop for prop in entity.properties
        if (api_name and str(prop.api_name or "") == api_name)
        or (not api_name and name and str(prop.name or "") == name)
    ]
    return str(values[0].id) if len(values) == 1 else ""


def promote_candidates(
    db: Session,
    scenario: BusinessScenario,
    *,
    tenant_id: str,
    created_by_user_id: str,
    expected_revisions: dict[str, int],
) -> tuple[list[ScenarioModelDraftResource], dict[str, Any]]:
    if not expected_revisions:
        raise CandidatePromotionBlocked([
            _issue("candidate_selection_empty", "至少选择一个候选定义。")
        ])
    if len(expected_revisions) > MAX_PROMOTION_BATCH:
        raise CandidatePromotionBlocked([_issue(
            "candidate_batch_too_large",
            f"单次最多晋级 {MAX_PROMOTION_BATCH} 个候选定义。",
        )])
    # The scenario lock is the serialization boundary for all formal resources
    # created by this batch.  Validation and writes remain in one transaction.
    locked_scenario = db.scalars(select(BusinessScenario).where(
        BusinessScenario.id == scenario.id,
        BusinessScenario.tenant_id == tenant_id,
    ).with_for_update()).first()
    if locked_scenario is None:
        raise CandidateNotFound("业务场景不存在")
    rows = list(db.scalars(select(ScenarioModelDraftResource).where(
        ScenarioModelDraftResource.id.in_(set(expected_revisions)),
        ScenarioModelDraftResource.tenant_id == tenant_id,
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
    ).with_for_update()).all())
    by_id = {row.id: row for row in rows}
    if set(by_id) != set(expected_revisions):
        raise CandidateNotFound("一个或多个候选定义不存在")
    for draft_id, expected_revision in expected_revisions.items():
        row = by_id[draft_id]
        if int(row.revision or 0) != expected_revision:
            raise CandidateRevisionConflict("候选定义 revision 已变化，请刷新后重试")
        if row.draft_status not in OPEN_LIFECYCLE_STATUSES:
            raise CandidateRevisionConflict("候选定义生命周期已关闭")
    ordered_rows = sorted(rows, key=lambda row: (
        str(row.resource_kind or ""), str(row.resource_key or ""), row.id
    ))
    evaluation = evaluate_candidates(db, locked_scenario, ordered_rows)
    if not evaluation.eligible or evaluation.payload is None:
        raise CandidatePromotionBlocked(evaluation.blockers)

    port_payloads = evaluation.payload.get(_PORT_PAYLOADS_KEY)
    port_payloads = port_payloads if isinstance(port_payloads, dict) else {}
    port_existing_ids = evaluation.payload.get(_PORT_EXISTING_IDS_KEY)
    port_existing_ids = (
        port_existing_ids if isinstance(port_existing_ids, dict) else {}
    )
    compiler_payload = {
        key: _json_copy(value, None)
        for key, value in evaluation.payload.items()
        if key not in {_PORT_PAYLOADS_KEY, _PORT_EXISTING_IDS_KEY}
    }
    compound_rows = [
        row for row in ordered_rows if row.resource_kind != "capability_port"
    ]
    apply_result = (
        scenario_model_compiler.apply_scenario_model(
            db,
            locked_scenario,
            compiler_payload,
            include_resource_ids=True,
        )
        if compound_rows
        else {"counts": {}}
    )
    # Port candidates carry the stable compiler resource key while they are
    # inert drafts.  Resolve that logical identity only after the owning
    # Function/Action/Workflow has been persisted in this same atomic batch.
    ids_by_key = _formal_ids_by_key(db, locked_scenario, compiler_payload)
    compiler_ids = (
        apply_result.get("resource_ids")
        if isinstance(apply_result.get("resource_ids"), dict)
        else {}
    )
    port_ids_by_draft: dict[str, str] = {}
    port_counts: dict[str, int] = defaultdict(int)
    for row in ordered_rows:
        if row.resource_kind != "capability_port":
            continue
        raw_port = port_payloads.get(row.id)
        if not isinstance(raw_port, dict):
            raise CandidatePromotionBlocked([_issue(
                "capability_port_preflight_state_missing",
                "能力端口候选缺少已验证的正式化状态，本批次将整体回滚。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            )])
        port_payload: ScenarioCapabilityPortCreate = (
            assistant_capability_modeling_service.normalize_port_candidate(
                raw_port
            )
        )
        resolved_capability_key = _resolved_port_capability_key(
            db,
            locked_scenario,
            port_payload,
            compiler_ids={
                str(key): str(value) for key, value in compiler_ids.items()
            },
            existing_port_id=str(port_existing_ids.get(row.id) or ""),
        )
        if resolved_capability_key != port_payload.capability_key:
            port_payload = port_payload.model_copy(update={
                "capability_key": resolved_capability_key,
            })
        existing_id = str(port_existing_ids.get(row.id) or "")
        if existing_id:
            port = catalog_service.update_capability_port(
                db,
                locked_scenario.id,
                existing_id,
                port_payload,
            )
            port_counts["capability_ports_updated"] += 1
        else:
            port = catalog_service.create_capability_port(
                db,
                locked_scenario.id,
                port_payload,
            )
            port_counts["capability_ports_added"] += 1
        if str(port.status or "") != "draft" or port.dataset_id or port.dataset_schema_id:
            raise CandidatePromotionBlocked([_issue(
                "capability_port_formalization_boundary_failed",
                "能力端口正式化越过了 draft 或无运行绑定边界，本批次将整体回滚。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            )])
        port_ids_by_draft[row.id] = str(port.id)
    db.flush()
    promoted: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for row in ordered_rows:
        parent_key = evaluation.row_resource_keys.get(row.id, "")
        formal_id = (
            port_ids_by_draft.get(row.id, "")
            if row.resource_kind == "capability_port"
            else ids_by_key.get(parent_key, "")
        )
        if row.resource_kind == "property":
            formal_id = _resolved_property_id(
                db, locked_scenario, row, formal_id
            )
        if not formal_id:
            raise CandidatePromotionBlocked([_issue(
                "formal_resource_identity_unresolved",
                "正式定义已通过预检，但无法唯一确认其持久化身份；本批次将整体回滚。",
                draft_ids=[row.id],
                resource_keys=[row.resource_key],
            )])
        row.validation_issues = _bounded_issues(evaluation.warnings)
        row.resolved_resource_id = formal_id[:64]
        row.draft_status = "resolved"
        row.enabled = False
        row.publishable = False
        row.revision = int(row.revision or 0) + 1
        row.updated_at = now
        promoted.append({
            "draft_id": row.id,
            "resource_kind": row.resource_kind,
            "resource_key": row.resource_key,
            "formal_resource_id": formal_id,
            "lifecycle_status": "resolved",
            "activation_status": (
                "inactive"
                if row.resource_kind in ACTIVATABLE_RESOURCE_KINDS
                else "not_applicable"
            ),
            "source_origin": source_origin(row),
            "revision": row.revision,
        })
    db.flush()
    counts = _json_copy(apply_result.get("counts"), {})
    counts.update(port_counts)
    result = {
        "ok": True,
        "atomic": True,
        "promoted": promoted,
        "counts": counts,
        "quality_fingerprint": evaluation.fingerprint,
    }
    return ordered_rows, result
