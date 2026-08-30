"""Persistence for inert, scene-level resources produced by AI compilation."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..models import BusinessScenario, DataSource, ScenarioModelDraftResource
from . import datasource_service, release_service


MAX_DRAFT_PAYLOAD_CHARS = 1_000_000
# Prior drafts are advisory context, while the current message and attachments
# are authoritative for the new turn.  A tighter budget prevents repeated
# historical runs from multiplying provider chunks and making a fresh request
# wait minutes before its own evidence is processed.
MAX_WORKING_DRAFT_CONTEXT_CHARS = 24_000
PENDING_CONFIRMATION_STATUS = "pending_confirmation"
APPLYABLE_DRAFT_STATUSES = frozenset({
    PENDING_CONFIRMATION_STATUS,
    "ready_for_review",
})
SCENARIO_VISIBLE_DRAFT_STATUSES = frozenset({
    "accepted", "ready_for_review", "needs_attention", "needs_validation",
    "deferred",
})
EDITABLE_DRAFT_STATUSES = frozenset({
    PENDING_CONFIRMATION_STATUS,
    *SCENARIO_VISIBLE_DRAFT_STATUSES,
})
OPEN_DRAFT_STATUSES = frozenset({
    PENDING_CONFIRMATION_STATUS,
    *SCENARIO_VISIBLE_DRAFT_STATUSES,
})
RESOURCE_KINDS = frozenset({
    "entity",
    "property",
    "relation",
    "instance",
    "mapping",
    "conceptual_mapping",
    "relation_mapping",
    "function",
    "action",
    "rule",
    "event",
    "workflow",
    "capability_port",
})
FORMAL_RESOURCE_KINDS = RESOURCE_KINDS - {"instance", "conceptual_mapping"}
AUTO_REPAIR_MAPPING_ISSUE_CODES = frozenset({
    "MAPPING_DEFERRED_NO_DATA_SOURCE",
    "MAPPING_MISSING_DATA_SOURCE",
    "MAPPING_DATA_SOURCE_MISSING",
    "DATA_SOURCE_DEPENDENCY",
})
AUTO_REPAIR_SCHEMA_ISSUE_CODE = "AUTO_REPAIR_DATA_SOURCE_SCHEMA_UNVALIDATED"
AUTO_REPAIR_AMBIGUITY_ISSUE_CODE = "AUTO_REPAIR_DATA_SOURCE_AMBIGUOUS"
AUTO_REPAIR_BINDING_ISSUE_CODE = "AUTO_REPAIRED_DATA_SOURCE_BINDING"
_FORMAL_SECTIONS = (
    "entities", "relations", "functions", "actions", "rules", "events",
    "workflows", "mappings", "relation_mappings",
)

_KIND_ALIASES = {
    "entities": "entity",
    "object_type": "entity",
    "object_types": "entity",
    "properties": "property",
    "relations": "relation",
    "object_instance": "instance",
    "object_instances": "instance",
    "instances": "instance",
    "mappings": "mapping",
    "conceptual_mappings": "conceptual_mapping",
    "relation_mappings": "relation_mapping",
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
    "capability_ports": "capability_port",
    "ports": "capability_port",
}
_SECTION_KINDS = {
    "entities": "entity",
    "relations": "relation",
    "instances": "instance",
    "mappings": "mapping",
    "conceptual_mappings": "conceptual_mapping",
    "relation_mappings": "relation_mapping",
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
}
_DEFAULT_TASK_IDS = {
    "entity": "ontology",
    "property": "ontology",
    "relation": "ontology",
    "instance": "instances",
    "mapping": "mapping",
    "conceptual_mapping": "mapping",
    "relation_mapping": "mapping",
    "function": "capabilities",
    "action": "capabilities",
    "rule": "rules",
    "event": "rules",
    "workflow": "workflows",
    "capability_port": "capabilities",
}


class DraftRevisionConflict(ValueError):
    """Raised when an editor attempts to overwrite a newer working draft."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_copy(value: Any, fallback: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:  # noqa: BLE001
        return copy.deepcopy(fallback)


def normalize_resource_kind(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    normalized = _KIND_ALIASES.get(normalized, normalized)
    return normalized if normalized in RESOURCE_KINDS else ""


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _json_copy(value, {})
    return {"raw_value": _json_copy(value, None)}


def _issue_list(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        issue = {
            "code": str(raw.get("code") or "validation_issue")[:120],
            "message": str(raw.get("message") or "Definition requires review.")[:2000],
            "blocking": raw.get("blocking", True) is not False,
            "source_refs": [
                str(item)[:300]
                for item in (raw.get("source_refs") or [])
                if str(item).strip()
            ][:100],
            "affected_change_keys": [
                str(item)[:500]
                for item in (raw.get("affected_change_keys") or [])
                if str(item).strip()
            ][:100],
            "resolution_hint": str(raw.get("resolution_hint") or "")[:2000],
        }
        signature = json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen:
            seen.add(signature)
            result.append(issue)
    return result


def _merge_issues(*groups: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        merged.extend(_issue_list(group))
    return _issue_list(merged)


def _bounded_source_refs(value: Any) -> tuple[list[str], int]:
    values = value if isinstance(value, list) else []
    deduped = list(dict.fromkeys(
        str(item)[:300] for item in values if str(item).strip()
    ))
    lineage_refs = [
        item for item in deduped if item.startswith("working-draft:")
    ]
    ordinary_refs = [
        item for item in deduped if not item.startswith("working-draft:")
    ]
    return [*lineage_refs, *ordinary_refs[:100]], max(0, len(ordinary_refs) - 100)


def _candidate_key(kind: str, candidate: dict[str, Any], payload: dict[str, Any]) -> str:
    value = (
        candidate.get("resource_key")
        or candidate.get("key")
        or payload.get("key")
        or payload.get("id")
        or payload.get("name")
    )
    key = str(value or "").strip()
    if key:
        return key[:500]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"{kind}:{digest}"


def _identity(kind: str, resource_key: str) -> str:
    return hashlib.sha256(f"{kind}\0{resource_key}".encode("utf-8")).hexdigest()


def _business_names(row: ScenarioModelDraftResource) -> set[str]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    values = (
        payload.get("name"), payload.get("display_name"), row.title,
    )
    return {
        re.sub(r"\s+", "", str(value or "")).casefold()
        for value in values
        if str(value or "").strip()
    }


def _task_for_section(payload: dict[str, Any], section: str, kind: str) -> str:
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        sections = task.get("sections") if isinstance(task.get("sections"), list) else []
        if section in {str(item) for item in sections}:
            return str(task.get("id") or "")[:80]
    return _DEFAULT_TASK_IDS.get(kind, "")


def _candidate_issues(
    model_payload: dict[str, Any],
    *,
    candidate: dict[str, Any],
    resource_key: str,
    task_id: str,
    source_refs: list[str],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = _issue_list(candidate.get("validation_issues"))
    tasks = model_payload.get("tasks") if isinstance(model_payload.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict) or str(task.get("id") or "") != task_id:
            continue
        for issue in _issue_list(task.get("issues")):
            affected = set(issue.get("affected_change_keys") or [])
            if not affected or any(
                value == resource_key
                or value.startswith(f"{resource_key}:")
                or resource_key.startswith(f"{value}:")
                for value in affected
            ):
                issues.append(issue)

    for issue in _issue_list(model_payload.get("unresolved")):
        affected = set(issue.get("affected_change_keys") or [])
        issue_refs = set(issue.get("source_refs") or [])
        applies = bool(
            any(
                value == resource_key
                or value.startswith(f"{resource_key}:")
                or resource_key.startswith(f"{value}:")
                for value in affected
            )
            or (issue_refs and issue_refs.intersection(source_refs))
            or (not affected and not issue_refs)
        )
        if applies:
            issues.append(issue)
    return _issue_list(issues)


def _compiler_candidates(model_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sidecar candidates plus a compatibility fallback for old runs."""
    result: list[dict[str, Any]] = []
    live_checkpoint = bool(model_payload.get("live_checkpoint"))
    sidecar = model_payload.get("draft_candidates")
    if isinstance(sidecar, list):
        for raw in sidecar:
            if isinstance(raw, dict) and normalize_resource_kind(raw.get("resource_kind")):
                item = _json_copy(raw, {})
                item["materialization_source"] = (
                    "live_checkpoint" if live_checkpoint else "compiler_sidecar"
                )
                item["_sidecar_candidate"] = True
                result.append(item)

    for section, kind in _SECTION_KINDS.items():
        values = model_payload.get(section)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            result.append({
                "resource_kind": kind,
                "resource_key": value.get("key") or value.get("id") or value.get("name"),
                "task_id": _task_for_section(model_payload, section, kind),
                "payload": _json_copy(value, {}),
                "evidence_refs": _json_copy(value.get("evidence_refs"), []),
                "validation_issues": [],
                "materialization_source": (
                    "live_checkpoint" if live_checkpoint else "compiled_payload"
                ),
                "_sidecar_candidate": False,
            })
    return result


def _expand_properties(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    if normalize_resource_kind(candidate.get("resource_kind")) != "entity":
        return []
    entity_payload = _payload_dict(candidate.get("payload"))
    properties = entity_payload.get("properties")
    if not isinstance(properties, list):
        return []
    entity_key = str(
        candidate.get("resource_key")
        or entity_payload.get("key")
        or entity_payload.get("name")
        or "entity"
    )
    result: list[dict[str, Any]] = []
    for index, raw_property in enumerate(properties, 1):
        if not isinstance(raw_property, dict):
            continue
        property_name = str(
            raw_property.get("key") or raw_property.get("name") or index
        )
        property_payload = {
            **_json_copy(raw_property, {}),
            "entity_ref": entity_key,
        }
        result.append({
            "resource_kind": "property",
            "resource_key": f"{entity_key}:property:{property_name}",
            "task_id": candidate.get("task_id") or "ontology",
            "payload": property_payload,
            "evidence_refs": candidate.get("evidence_refs") or [],
            "validation_issues": candidate.get("validation_issues") or [],
            "materialization_source": candidate.get("materialization_source") or "compiler",
        })
    return result


def _working_draft_manifest(
    model_payload: dict[str, Any],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    manifest = (
        model_payload.get("source_manifest")
        if isinstance(model_payload.get("source_manifest"), list)
        else []
    )
    for raw in manifest:
        if not isinstance(raw, dict) or raw.get("source_kind") != "working_draft":
            continue
        source_id = str(raw.get("source_id") or "").strip()
        kind = normalize_resource_kind(raw.get("resource_kind"))
        resource_key = str(raw.get("resource_key") or "").strip()
        if source_id and kind and resource_key:
            result[source_id] = {
                "resource_kind": kind,
                "resource_key": resource_key,
            }
    return result


def _working_source_id(source_ref: str) -> str:
    match = re.fullmatch(r"(working-draft:[^:]+:r\d+):p\d+", source_ref)
    return match.group(1) if match else ""


def _property_leaf(resource_key: str) -> str:
    return (
        resource_key.split(":property:", 1)[1]
        if ":property:" in resource_key
        else resource_key
    ).casefold()


def _candidate_source_refs(
    value: Any,
    *,
    resource_kind: str,
    resource_key: str,
    working_manifest: dict[str, dict[str, str]],
) -> list[str]:
    refs = [str(item) for item in (value if isinstance(value, list) else []) if str(item)]
    if not working_manifest:
        return refs
    compatible: list[str] = []
    exact: list[str] = []
    property_matches: list[str] = []
    passthrough: list[str] = []
    for source_ref in refs:
        source_id = _working_source_id(source_ref)
        if not source_id:
            passthrough.append(source_ref)
            continue
        metadata = working_manifest.get(source_id)
        if metadata is None:
            passthrough.append(source_ref)
            continue
        if metadata["resource_kind"] != resource_kind:
            continue
        compatible.append(source_ref)
        manifest_key = metadata["resource_key"]
        if manifest_key == resource_key:
            exact.append(source_ref)
        elif (
            resource_kind == "property"
            and _property_leaf(manifest_key) == _property_leaf(resource_key)
        ):
            property_matches.append(source_ref)
    selected = exact or property_matches or compatible
    return list(dict.fromkeys([*passthrough, *selected]))


def _normalized_candidates(model_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = _compiler_candidates(model_payload)
    raw_candidates.extend(
        property_candidate
        for candidate in list(raw_candidates)
        for property_candidate in _expand_properties(candidate)
    )
    working_manifest = _working_draft_manifest(model_payload)
    # Prefer sidecar entries over compatibility fallback entries with the same
    # identity because the sidecar retains pre-normalization invalid fields.
    by_identity: dict[str, dict[str, Any]] = {}
    for raw in raw_candidates:
        kind = normalize_resource_kind(raw.get("resource_kind"))
        if not kind:
            continue
        candidate_payload = _payload_dict(raw.get("payload"))
        resource_key = _candidate_key(kind, raw, candidate_payload)
        source_refs = _candidate_source_refs(
            raw.get("evidence_refs") or candidate_payload.get("evidence_refs"),
            resource_kind=kind,
            resource_key=resource_key,
            working_manifest=working_manifest,
        )
        if "evidence_refs" in candidate_payload:
            candidate_payload["evidence_refs"] = source_refs
        identity = _identity(kind, resource_key)
        normalized = {
            **raw,
            "resource_kind": kind,
            "resource_key": resource_key,
            "resource_identity": identity,
            "payload": candidate_payload,
            "evidence_refs": source_refs,
        }
        current = by_identity.get(identity)
        if current is None or (
            not bool(current.get("_sidecar_candidate"))
            and bool(normalized.get("_sidecar_candidate"))
        ):
            by_identity[identity] = normalized
    return list(by_identity.values())


def _candidate_names(
    resource_kind: str,
    resource_key: str,
    payload: dict[str, Any],
) -> set[str]:
    values = {
        str(payload.get("name") or ""),
        str(payload.get("display_name") or ""),
        str(payload.get("api_name") or ""),
    }
    if resource_kind == "property":
        values.add(_property_leaf(resource_key))
    return {
        re.sub(r"\s+", "", value).casefold()
        for value in values
        if value.strip()
    }


def _predecessor_from_source_refs(
    source_refs: Iterable[str],
    *,
    resource_kind: str,
    resource_key: str,
    payload: dict[str, Any],
    consumed_revisions: dict[str, int],
    consumed_rows_by_id: dict[str, ScenarioModelDraftResource],
) -> ScenarioModelDraftResource | None:
    candidates: dict[str, ScenarioModelDraftResource] = {}
    for source_ref in source_refs:
        match = re.fullmatch(r"working-draft:([^:]+):r(\d+):p\d+", source_ref)
        if not match:
            continue
        draft_id = str(match.group(1))[:32]
        revision = int(match.group(2))
        row = consumed_rows_by_id.get(draft_id)
        if (
            row is not None
            and row.resource_kind == resource_kind
            and consumed_revisions.get(draft_id) == revision
            and row.revision == revision
        ):
            candidates[row.id] = row
    values = list(candidates.values())
    exact = [row for row in values if row.resource_key == resource_key]
    if len(exact) == 1:
        return exact[0]
    names = _candidate_names(resource_kind, resource_key, payload)
    name_matches = [
        row for row in values
        if names.intersection(_business_names(row))
        or (
            resource_kind == "property"
            and _property_leaf(row.resource_key) in names
        )
    ]
    if len(name_matches) == 1:
        return name_matches[0]
    return values[0] if len(values) == 1 else None


def _lineage_revisions(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for draft_id, revision in value.items():
        try:
            parsed = int(revision)
        except (TypeError, ValueError):
            continue
        if str(draft_id) and parsed >= 0:
            result[str(draft_id)] = parsed
    return result


def _lock_materialization_scope(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
) -> None:
    """Serialize lineage decisions before any proposal-specific rows exist."""
    locked = db.scalar(
        select(BusinessScenario.id)
        .where(
            BusinessScenario.id == scenario_id,
            BusinessScenario.tenant_id == tenant_id,
        )
        .with_for_update()
    ) is not None
    if not locked:
        raise ValueError("场景不存在或不属于当前租户")


def _reconcile_active_lineage(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    proposal_id: str,
    created_by_user_id: str,
    identities: Iterable[str],
    consumed_draft_revisions: dict[str, int],
) -> None:
    """Keep one active candidate per identity without losing concurrent edits."""
    identity_values = {str(value) for value in identities if str(value)}
    if not identity_values:
        return
    rows = list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
            or_(
                ScenarioModelDraftResource.resource_identity.in_(identity_values),
                ScenarioModelDraftResource.id.in_(
                    set(consumed_draft_revisions) or {""}
                ),
                ScenarioModelDraftResource.predecessor_draft_id.in_(
                    set(consumed_draft_revisions) or {""}
                ),
            ),
        )
        .with_for_update()
    ).all())
    current_rows = [row for row in rows if row.proposal_id == proposal_id]
    consumed_rows_by_id = {
        row.id: row for row in rows if row.id in consumed_draft_revisions
    }
    claimed_predecessors = {
        row.predecessor_draft_id for row in current_rows if row.predecessor_draft_id
    }
    for current in current_rows:
        if current.predecessor_draft_id or any(
            predecessor.resource_identity == current.resource_identity
            for predecessor in consumed_rows_by_id.values()
        ):
            continue
        # Never infer replacement from kind/count alone. A same-kind candidate
        # may be a newly requested resource (for example, Order beside Customer),
        # so only a unique stable business-name match may inherit the lineage.
        # Otherwise keep both sides visible and require an explicit resolution.
        candidates = [
            predecessor for predecessor in consumed_rows_by_id.values()
            if predecessor.resource_kind == current.resource_kind
            and predecessor.id not in claimed_predecessors
        ]
        name_matches = [
            predecessor for predecessor in candidates
            if _business_names(predecessor).intersection(_business_names(current))
        ]
        if len(name_matches) == 1:
            predecessor = name_matches[0]
            current.predecessor_draft_id = predecessor.id
            current.predecessor_revision = consumed_draft_revisions[predecessor.id]
            claimed_predecessors.add(predecessor.id)
        elif candidates:
            if current.draft_status != "needs_validation":
                current.draft_status = "needs_attention"
            affected_keys = sorted({
                current.resource_key,
                *(predecessor.resource_key for predecessor in candidates),
            })
            current.validation_issues = _merge_issues(
                current.validation_issues,
                {
                    "code": "AMBIGUOUS_WORKING_DRAFT_SUCCESSOR",
                    "message": "本次候选没有引用具体 working draft，无法安全判断它是新增定义还是已有草稿的后继。",
                    "blocking": True,
                    "affected_change_keys": affected_keys,
                    "resolution_hint": "旧草稿和新候选均已保留；请确认新增关系，或基于指定草稿重新编译。",
                },
            )
            for predecessor in candidates:
                if predecessor.draft_status not in OPEN_DRAFT_STATUSES:
                    continue
                if predecessor.draft_status != "needs_validation":
                    predecessor.draft_status = "needs_attention"
                predecessor.validation_issues = _merge_issues(
                    predecessor.validation_issues,
                    {
                        "code": "AMBIGUOUS_WORKING_DRAFT_LINEAGE",
                        "message": "新的同类候选未声明是否接替本草稿，系统未自动隐藏或覆盖任何一方。",
                        "blocking": True,
                        "affected_change_keys": affected_keys,
                        "resolution_hint": "请确认新候选是新增资源，或从本草稿重新发起定向编译。",
                    },
                )
    for current in current_rows:
        if current.draft_status == "superseded":
            continue
        competing_decision = next(
            (
                row for row in rows
                if row.proposal_id != proposal_id
                and current.predecessor_draft_id
                and row.predecessor_draft_id == current.predecessor_draft_id
                and row.draft_status in {"resolved", "applied"}
                and _utc(row.updated_at) > _utc(current.lineage_started_at)
            ),
            None,
        )
        if competing_decision is not None:
            current.draft_status = "superseded"
            current.superseded_by_proposal_id = competing_decision.proposal_id
            current.validation_issues = _merge_issues(
                current.validation_issues,
                {
                    "code": "LINEAGE_RESOLVED_DURING_COMPILATION",
                    "message": "同一 predecessor 的后继草稿已在本次编译期间被正式解决；本候选不会重新激活该 lineage。",
                    "blocking": True,
                    "affected_change_keys": [current.resource_key],
                    "resolution_hint": "如需继续修改，请从当前正式定义重新发起建模。",
                },
            )
            continue
        consumed_rows = [
            row for row in rows
            if row.proposal_id != proposal_id
            and (
                row.resource_identity == current.resource_identity
                or row.id == current.predecessor_draft_id
                or (
                    bool(current.predecessor_draft_id)
                    and row.predecessor_draft_id == current.predecessor_draft_id
                )
            )
            and row.id in consumed_draft_revisions
        ]
        changed_after_snapshot = next(
            (
                row for row in consumed_rows
                if row.revision != consumed_draft_revisions.get(row.id)
                or row.draft_status in {"resolved", "applied"}
            ),
            None,
        )
        if changed_after_snapshot is not None:
            current.draft_status = "superseded"
            current.superseded_by_proposal_id = changed_after_snapshot.proposal_id
            current.predecessor_draft_id = changed_after_snapshot.id
            current.predecessor_revision = changed_after_snapshot.revision
            current.validation_issues = _merge_issues(
                current.validation_issues,
                {
                    "code": "CONSUMED_DRAFT_CHANGED_DURING_COMPILATION",
                    "message": "被本次编译消费的草稿在编译期间已被修改、解决或关闭；后继候选不会重新激活该定义。",
                    "blocking": True,
                    "affected_change_keys": [current.resource_key],
                    "resolution_hint": "请从当前最新活动草稿重新发起编译。",
                },
            )
            continue
        predecessors = [
            row for row in rows
            if row.proposal_id != proposal_id
            and (
                row.resource_identity == current.resource_identity
                or row.id == current.predecessor_draft_id
                or (
                    bool(current.predecessor_draft_id)
                    and row.predecessor_draft_id == current.predecessor_draft_id
                )
            )
            and row.draft_status in OPEN_DRAFT_STATUSES
        ]
        if not predecessors:
            continue
        predecessors.sort(
            key=lambda row: (_utc(row.lineage_started_at), row.proposal_id, row.id),
            reverse=True,
        )
        newest = predecessors[0]
        consumed = next(
            (
                row for row in predecessors
                if consumed_draft_revisions.get(row.id) == row.revision
            ),
            None,
        )
        protected = next(
            (
                row for row in predecessors
                if row.draft_status == "needs_validation"
                and consumed_draft_revisions.get(row.id) != row.revision
            ),
            None,
        )
        current_order = (_utc(current.lineage_started_at), current.proposal_id)
        newest_order = (_utc(newest.lineage_started_at), newest.proposal_id)
        if protected is not None or newest_order > current_order:
            winner = protected or newest
            current.draft_status = "superseded"
            current.superseded_by_proposal_id = winner.proposal_id
            current.predecessor_draft_id = winner.id
            current.predecessor_revision = winner.revision
            current.validation_issues = _merge_issues(
                current.validation_issues,
                {
                    "code": "SUCCESSOR_CONFLICT_WITH_NEWER_WORKING_DRAFT",
                    "message": (
                        "用户在本次编译快照之后又修改了 working draft；"
                        "本次生成的后继候选仅保留审计记录，不会成为活动草稿。"
                    ),
                    "blocking": True,
                    "affected_change_keys": [current.resource_key],
                    "resolution_hint": (
                        "请基于最新活动草稿重新编译；用户修订已完整保留。"
                    ),
                },
            )
            continue

        if consumed is not None:
            current.predecessor_draft_id = consumed.id
            current.predecessor_revision = consumed.revision
        replaced_proposal_ids = {row.proposal_id for row in predecessors}
        for predecessor in predecessors:
            predecessor.draft_status = "superseded"
            predecessor.superseded_by_proposal_id = proposal_id
            predecessor.validation_issues = _merge_issues(
                predecessor.validation_issues,
                {
                    "code": (
                        "WORKING_DRAFT_CONSUMED_BY_SUCCESSOR"
                        if consumed_draft_revisions.get(predecessor.id) == predecessor.revision
                        else "SUPERSEDED_BY_NEW_PROPOSAL"
                    ),
                    "message": (
                        "该 working draft 的精确 revision 已被后继编译消费。"
                        if consumed_draft_revisions.get(predecessor.id) == predecessor.revision
                        else "更新的编译结果已接管该资源的活动草稿 lineage。"
                    ),
                    "blocking": False,
                    "affected_change_keys": [predecessor.resource_key],
                },
            )
        lineage_changed = True
        while lineage_changed:
            lineage_changed = False
            for ancestor in rows:
                if (
                    ancestor.draft_status == "superseded"
                    and ancestor.superseded_by_proposal_id in replaced_proposal_ids
                    and ancestor.superseded_by_proposal_id != proposal_id
                ):
                    replaced_proposal_ids.add(ancestor.proposal_id)
                    ancestor.superseded_by_proposal_id = proposal_id
                    lineage_changed = True


def materialize_draft_resources(
    db: Session,
    scenario: BusinessScenario,
    proposal: dict[str, Any],
    *,
    source_thread_id: str = "",
    source_message_id: str = "",
    compilation_job_id: str = "",
    created_by_user_id: str | None = None,
    lineage_started_at: datetime | None = None,
    consumed_draft_revisions: dict[str, int] | None = None,
    replace_live_checkpoints: bool = False,
) -> dict[str, Any]:
    """Idempotently materialize every compiler candidate into inert staging."""
    if proposal.get("kind") != "scenario_model":
        return {"resource_count": 0, "issue_count": 0, "resource_ids_by_task": {}}
    model_payload = proposal.get("payload")
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not isinstance(model_payload, dict) or not proposal_id:
        return {"resource_count": 0, "issue_count": 0, "resource_ids_by_task": {}}
    tenant_id = str(scenario.tenant_id or db.info.get("tenant_id") or "").strip()
    if not tenant_id:
        raise ValueError("Scenario model drafts require a tenant-owned scenario")
    owner_user_id = str(created_by_user_id or db.info.get("user_id") or "").strip()
    if not owner_user_id:
        raise ValueError("场景模型草稿必须绑定真实创建用户")

    _lock_materialization_scope(
        db,
        tenant_id=tenant_id,
        scenario_id=scenario.id,
    )

    candidates = _normalized_candidates(model_payload)
    lineage_started_at = lineage_started_at or datetime.now(timezone.utc)
    consumed_revisions = _lineage_revisions(consumed_draft_revisions)
    consumed_rows_by_id = {
        row.id: row
        for row in db.scalars(
            select(ScenarioModelDraftResource)
            .where(
                ScenarioModelDraftResource.tenant_id == tenant_id,
                ScenarioModelDraftResource.scenario_id == scenario.id,
                ScenarioModelDraftResource.created_by_user_id == owner_user_id,
                ScenarioModelDraftResource.id.in_(
                    set(consumed_revisions) or {""}
                ),
            )
            .with_for_update()
        ).all()
    }
    identities = [str(item["resource_identity"]) for item in candidates]
    existing_rows = list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == owner_user_id,
            ScenarioModelDraftResource.proposal_id == proposal_id,
        )
        .with_for_update()
    ).all())
    existing_by_identity = {row.resource_identity: row for row in existing_rows}

    for candidate in candidates:
        kind = str(candidate["resource_kind"])
        resource_key = str(candidate["resource_key"])
        candidate_payload = _payload_dict(candidate.get("payload"))
        serialized = json.dumps(candidate_payload, ensure_ascii=False, default=str)
        if len(serialized) > MAX_DRAFT_PAYLOAD_CHARS:
            candidate_payload = {
                "key": resource_key,
                "name": str(candidate_payload.get("name") or resource_key)[:300],
                "truncated": True,
            }
            candidate["validation_issues"] = [
                *_issue_list(candidate.get("validation_issues")),
                {
                    "code": "draft_payload_too_large",
                    "message": "Generated resource exceeded the staging size limit; review the source document.",
                    "blocking": True,
                },
            ]
        raw_source_refs = (
            candidate.get("evidence_refs")
            or candidate_payload.get("evidence_refs")
            or []
        )
        source_refs, dropped_source_ref_count = _bounded_source_refs(
            raw_source_refs
        )
        if dropped_source_ref_count:
            candidate["validation_issues"] = [
                *_issue_list(candidate.get("validation_issues")),
                {
                    "code": "SOURCE_REFS_TRUNCATED",
                    "message": (
                        f"该资源有 {dropped_source_ref_count} 条普通来源引用未复制到草稿摘要；"
                        "working-draft lineage 引用已全部保留。"
                    ),
                    "blocking": False,
                    "source_refs": source_refs,
                    "resolution_hint": "完整来源仍保留在编译 proposal 的 source manifest 中。",
                },
            ]
        predecessor = _predecessor_from_source_refs(
            source_refs,
            resource_kind=kind,
            resource_key=resource_key,
            payload=candidate_payload,
            consumed_revisions=consumed_revisions,
            consumed_rows_by_id=consumed_rows_by_id,
        )
        predecessor_draft_id = predecessor.id if predecessor is not None else ""
        predecessor_revision = (
            consumed_revisions[predecessor.id] if predecessor is not None else -1
        )
        task_id = str(candidate.get("task_id") or _DEFAULT_TASK_IDS.get(kind, ""))[:80]
        issues = _candidate_issues(
            model_payload,
            candidate=candidate,
            resource_key=resource_key,
            task_id=task_id,
            source_refs=source_refs,
        )
        # Materialization is durable but not yet scene-visible.  Only a
        # candidate without a blocking validation issue may cross the later
        # confirmation boundary.  Broken/salvaged output is immediately
        # truthful about requiring attention instead of masquerading as a
        # confirmable definition.
        draft_status = (
            "needs_attention"
            if str(candidate.get("validation_status") or "").casefold()
            in {"needs_attention", "needs_validation"}
            or any(issue.get("blocking", True) is not False for issue in issues)
            else PENDING_CONFIRMATION_STATUS
        )
        identity = str(candidate["resource_identity"])
        row = existing_by_identity.get(identity)
        if row is None:
            row = ScenarioModelDraftResource(
                tenant_id=tenant_id,
                scenario_id=scenario.id,
                created_by_user_id=owner_user_id,
                source_thread_id=str(source_thread_id or "")[:32],
                source_message_id=str(source_message_id or "")[:32],
                compilation_job_id=str(compilation_job_id or "")[:32],
                proposal_id=proposal_id[:64],
                lineage_started_at=lineage_started_at,
                predecessor_draft_id=predecessor_draft_id,
                predecessor_revision=predecessor_revision,
                task_id=task_id,
                resource_kind=kind,
                resource_key=resource_key,
                resource_identity=identity,
                title=str(
                    candidate.get("display_name")
                    or candidate_payload.get("display_name")
                    or candidate_payload.get("name")
                    or resource_key
                )[:300],
                source_payload=_json_copy(candidate_payload, {}),
                payload=_json_copy(candidate_payload, {}),
                validation_issues=issues,
                source_refs=source_refs,
                materialization_source=str(
                    candidate.get("materialization_source") or "compiler"
                )[:30],
                draft_status=draft_status,
                enabled=False,
                publishable=False,
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
            except IntegrityError:
                row = db.scalars(
                    select(ScenarioModelDraftResource).where(
                        ScenarioModelDraftResource.tenant_id == tenant_id,
                        ScenarioModelDraftResource.scenario_id == scenario.id,
                        ScenarioModelDraftResource.created_by_user_id == owner_user_id,
                        ScenarioModelDraftResource.proposal_id == proposal_id,
                        ScenarioModelDraftResource.resource_identity == identity,
                    )
                ).first()
                if row is None:
                    raise
            existing_by_identity[identity] = row
        else:
            row.source_thread_id = row.source_thread_id or str(source_thread_id or "")[:32]
            row.source_message_id = row.source_message_id or str(source_message_id or "")[:32]
            row.compilation_job_id = row.compilation_job_id or str(compilation_job_id or "")[:32]
            row.task_id = row.task_id or task_id
            row.predecessor_draft_id = (
                row.predecessor_draft_id or predecessor_draft_id
            )
            if row.predecessor_revision < 0 and predecessor_revision >= 0:
                row.predecessor_revision = predecessor_revision
            incoming_source = str(
                candidate.get("materialization_source") or "compiler"
            )[:30]
            # A live checkpoint may be replaced by a later, more complete
            # checkpoint only while nobody has edited it.  User changes bump
            # revision and therefore always win over subsequent model output.
            if row.materialization_source == "live_checkpoint" and row.revision == 0:
                row.title = str(
                    candidate.get("display_name")
                    or candidate_payload.get("display_name")
                    or candidate_payload.get("name")
                    or resource_key
                )[:300]
                row.source_payload = _json_copy(candidate_payload, {})
                row.payload = _json_copy(candidate_payload, {})
                row.validation_issues = issues
                row.source_refs = source_refs
                row.materialization_source = incoming_source
                row.draft_status = draft_status
            else:
                row.validation_issues = _merge_issues(row.validation_issues, issues)
            merged_source_refs, dropped_merged_refs = _bounded_source_refs([
                *[str(value) for value in (row.source_refs or [])],
                *source_refs,
            ])
            row.source_refs = merged_source_refs
            if dropped_merged_refs:
                row.validation_issues = _merge_issues(
                    row.validation_issues,
                    {
                        "code": "SOURCE_REFS_TRUNCATED",
                        "message": f"草稿摘要省略了 {dropped_merged_refs} 条普通来源引用；lineage 引用已保留。",
                        "blocking": False,
                    },
                )
            row.enabled = False
            row.publishable = False

    if replace_live_checkpoints:
        active_identities = set(identities)
        for row in existing_rows:
            if (
                not bool(model_payload.get("live_checkpoint"))
                and row.materialization_source == "live_checkpoint"
                and row.revision > 0
            ):
                row.materialization_source = "user_checkpoint_edit"
            if (
                row.resource_identity not in active_identities
                and row.materialization_source == "live_checkpoint"
                and row.revision == 0
                and row.draft_status in OPEN_DRAFT_STATUSES
            ):
                db.delete(row)

    # A live checkpoint is a temporary page projection, not a successor
    # decision. It must not mutate an existing working-draft lineage before
    # the compiler reaches a final result.
    if not bool(model_payload.get("live_checkpoint")):
        _reconcile_active_lineage(
            db,
            tenant_id=tenant_id,
            scenario_id=scenario.id,
            proposal_id=proposal_id,
            created_by_user_id=owner_user_id,
            identities=identities,
            consumed_draft_revisions=consumed_revisions,
        )

    rows = list(db.scalars(
        select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == owner_user_id,
            ScenarioModelDraftResource.proposal_id == proposal_id,
        )
    ).all())
    return draft_summary(rows)


def discard_pristine_live_checkpoints(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    created_by_user_id: str,
    compilation_job_id: str,
) -> int:
    """Remove untouched projections and promote user-edited checkpoints."""
    rows = list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
            ScenarioModelDraftResource.compilation_job_id == compilation_job_id,
            ScenarioModelDraftResource.materialization_source == "live_checkpoint",
            ScenarioModelDraftResource.draft_status.in_(OPEN_DRAFT_STATUSES),
        )
        .with_for_update()
    ).all())
    deleted = 0
    for row in rows:
        if row.revision == 0:
            db.delete(row)
            deleted += 1
        else:
            row.materialization_source = "user_checkpoint_edit"
    return deleted


def draft_summary(
    rows: Iterable[ScenarioModelDraftResource],
    *,
    include_issue_counts: bool = True,
) -> dict[str, Any]:
    all_values = list(rows)
    # Task blockers and materialization counters describe the active workspace,
    # not historical resolved/superseded rows requested for audit pagination.
    values = [row for row in all_values if row.draft_status in OPEN_DRAFT_STATUSES]
    by_kind = Counter(row.resource_kind for row in values)
    by_status = Counter(row.draft_status for row in values)
    ids_by_task: dict[str, list[str]] = defaultdict(list)
    task_rows: dict[str, list[ScenarioModelDraftResource]] = defaultdict(list)
    for row in values:
        ids_by_task[str(row.task_id or "")].append(row.id)
        task_rows[str(row.task_id or "")].append(row)
    return {
        "resource_count": len(values),
        "historical_resource_count": len(all_values) - len(values),
        # Compact scene-list responses deliberately defer the large diagnostic
        # JSON column.  The detailed endpoint keeps the historical counts.
        "issue_count": (
            sum(len(_issue_list(row.validation_issues)) for row in values)
            if include_issue_counts else 0
        ),
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "resource_ids_by_task": {
            key: sorted(items) for key, items in ids_by_task.items() if key
        },
        "by_task": {
            task_id: {
                "resource_count": len(items),
                "formal_resource_count": sum(
                    item.resource_kind in FORMAL_RESOURCE_KINDS for item in items
                ),
                "draft_only_resource_count": sum(
                    item.resource_kind not in FORMAL_RESOURCE_KINDS for item in items
                ),
                "issue_count": (
                    sum(len(_issue_list(item.validation_issues)) for item in items)
                    if include_issue_counts else 0
                ),
                "needs_attention_count": sum(
                    item.draft_status in {"needs_attention", "needs_validation"}
                    or any(
                        issue.get("blocking", True) is not False
                        for issue in _issue_list(item.validation_issues)
                    )
                    for item in items
                ),
            }
            for task_id, items in sorted(task_rows.items())
            if task_id
        },
    }


def update_working_draft_atomic(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    draft_id: str,
    created_by_user_id: str,
    payload: dict[str, Any],
    expected_revision: int,
) -> ScenarioModelDraftResource:
    """CAS-update one active working copy across every supported database."""
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) > MAX_DRAFT_PAYLOAD_CHARS:
        raise ValueError("场景草稿定义超过 1,000,000 字符上限")
    current = db.scalars(
        select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.id == draft_id,
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
        )
    ).first()
    if current is None:
        raise LookupError("Draft resource does not exist")
    immutable_key = (
        str(current.source_payload.get("key") or "")
        if isinstance(current.source_payload, dict)
        else ""
    )
    if immutable_key and "key" in payload and str(payload.get("key") or "") != immutable_key:
        raise ValueError("场景草稿的稳定 resource key 不可修改；请修改业务名称而不是内部身份")
    issues = _merge_issues(
        current.validation_issues,
        {
            "code": "draft_requires_revalidation",
            "message": "working draft 已被用户修改，提升为正式定义前必须重新校验。",
            "blocking": True,
            "resolution_hint": "请重新运行场景建模校验；在此之前草稿保持停用且不可发布。",
        },
    )
    now = datetime.now(timezone.utc)
    try:
        changed = db.execute(
            update(ScenarioModelDraftResource)
            .where(
                ScenarioModelDraftResource.id == draft_id,
                ScenarioModelDraftResource.tenant_id == tenant_id,
                ScenarioModelDraftResource.scenario_id == scenario_id,
                ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
                ScenarioModelDraftResource.revision == expected_revision,
                ScenarioModelDraftResource.draft_status.in_(
                    EDITABLE_DRAFT_STATUSES
                ),
            )
            .values(
                payload=_json_copy(payload, {}),
                title=str(
                    payload.get("display_name")
                    or payload.get("name")
                    or current.title
                    or current.resource_key
                )[:300],
                validation_issues=issues,
                draft_status="needs_validation",
                enabled=False,
                publishable=False,
                revision=expected_revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        raise DraftRevisionConflict(
            "场景草稿正在被其他请求应用或编辑，请刷新后重试"
        ) from exc
    if changed.rowcount != 1:
        raise DraftRevisionConflict("场景草稿已被更新或关闭，请刷新后重试")
    return db.scalars(
        select(ScenarioModelDraftResource)
        .where(ScenarioModelDraftResource.id == draft_id)
        .execution_options(populate_existing=True)
    ).one()


def resolve_draft_atomic(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    draft_id: str,
    created_by_user_id: str,
    expected_revision: int,
    resolved_resource_id: str,
) -> ScenarioModelDraftResource:
    """CAS-close a working copy after its formal target has been verified."""
    now = datetime.now(timezone.utc)
    try:
        changed = db.execute(
            update(ScenarioModelDraftResource)
            .where(
                ScenarioModelDraftResource.id == draft_id,
                ScenarioModelDraftResource.tenant_id == tenant_id,
                ScenarioModelDraftResource.scenario_id == scenario_id,
                ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
                ScenarioModelDraftResource.revision == expected_revision,
                ScenarioModelDraftResource.draft_status.in_(
                    EDITABLE_DRAFT_STATUSES
                ),
            )
            .values(
                resolved_resource_id=str(resolved_resource_id)[:64],
                draft_status="resolved",
                enabled=False,
                publishable=False,
                revision=expected_revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        raise DraftRevisionConflict(
            "场景草稿正在被其他请求应用或编辑，请刷新后重试"
        ) from exc
    if changed.rowcount != 1:
        raise DraftRevisionConflict("场景草稿已被更新或关闭，请刷新后重试")
    return db.scalars(
        select(ScenarioModelDraftResource)
        .where(ScenarioModelDraftResource.id == draft_id)
        .execution_options(populate_existing=True)
    ).one()


def task_drafts_for_apply(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    proposal_id: str,
    task_id: str,
    created_by_user_id: str,
) -> list[ScenarioModelDraftResource]:
    """Lock the whole task staging set before stale-proposal selection."""
    return list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.proposal_id == proposal_id,
            ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
            ScenarioModelDraftResource.task_id == task_id,
        )
        .with_for_update()
    ).all())


def active_working_draft_context(
    db: Session,
    scenario: BusinessScenario,
) -> list[dict[str, Any]]:
    """Return a bounded set of redacted, latest working revisions.

    Drafts are advisory context for a new request, not a reason to block it.
    When repeated prior runs exceed the compiler context budget, retain the
    freshest complete resource snapshots that fit and leave omitted revisions
    open and untouched for later review.
    """
    tenant_id = str(scenario.tenant_id or db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    if not user_id:
        return []
    rows = list(db.scalars(
        select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == user_id,
            ScenarioModelDraftResource.draft_status.in_(OPEN_DRAFT_STATUSES),
            ScenarioModelDraftResource.materialization_source != "live_checkpoint",
        )
    ).all())
    by_identity: dict[str, ScenarioModelDraftResource] = {}
    for row in rows:
        current = by_identity.get(row.resource_identity)
        row_order = (
            1 if row.draft_status == "needs_validation" else 0,
            row.revision,
            _utc(row.updated_at),
            _utc(row.lineage_started_at),
            row.proposal_id,
        )
        current_order = (
            1 if current and current.draft_status == "needs_validation" else 0,
            current.revision if current else -1,
            _utc(current.updated_at) if current else _utc(None),
            _utc(current.lineage_started_at) if current else _utc(None),
            current.proposal_id if current else "",
        )
        if current is None or row_order > current_order:
            by_identity[row.resource_identity] = row

    candidates: list[tuple[ScenarioModelDraftResource, dict[str, Any], str]] = []
    for row in by_identity.values():
        payload = release_service.safe_snapshot_content(_payload_dict(row.payload))
        source_payload = release_service.safe_snapshot_content(
            _payload_dict(row.source_payload)
        )
        item = {
            "draft_id": row.id,
            "proposal_id": row.proposal_id,
            "task_id": row.task_id,
            "resource_kind": row.resource_kind,
            "resource_key": row.resource_key,
            "title": row.title,
            "payload": payload,
            "source_payload_hash": hashlib.sha256(json.dumps(
                source_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "revision": row.revision,
            "draft_status": row.draft_status,
            "validation_issues": _issue_list(row.validation_issues),
            "source_refs": _bounded_source_refs(row.source_refs)[0],
            "source_thread_id": row.source_thread_id,
            "source_message_id": row.source_message_id,
            "predecessor_draft_id": row.predecessor_draft_id,
            "predecessor_revision": row.predecessor_revision,
        }
        item = release_service.safe_snapshot_content(item)
        canonical = json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        candidates.append((row, item, canonical))

    # Prefer the drafts the user touched most recently.  Selection is by
    # whole snapshot—never truncate JSON in the middle—and the final output is
    # sorted by stable resource identity so its fingerprint remains
    # deterministic for an unchanged database state.
    candidates.sort(
        key=lambda value: (
            _utc(value[0].updated_at),
            value[0].revision,
            _utc(value[0].lineage_started_at),
            value[0].proposal_id,
            value[0].id,
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    total = 0
    for _row, item, canonical in candidates:
        size = len(canonical)
        if size > MAX_WORKING_DRAFT_CONTEXT_CHARS - total:
            continue
        item["snapshot_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        selected.append(item)
        total += size
    selected.sort(
        key=lambda item: (
            str(item.get("resource_kind") or ""),
            str(item.get("resource_key") or ""),
            str(item.get("draft_id") or ""),
        )
    )
    return selected


def has_active_working_drafts(db: Session, scenario: BusinessScenario) -> bool:
    tenant_id = str(scenario.tenant_id or db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    if not user_id:
        return False
    return db.scalars(
        select(ScenarioModelDraftResource.id).where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == user_id,
            ScenarioModelDraftResource.draft_status.in_(OPEN_DRAFT_STATUSES),
            ScenarioModelDraftResource.materialization_source != "live_checkpoint",
        ).limit(1)
    ).first() is not None


def active_working_draft_scopes(
    db: Session,
    scenario: BusinessScenario,
) -> list[str]:
    """Return semantic scopes that have an owned active draft lineage."""
    tenant_id = str(scenario.tenant_id or db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    if not user_id:
        return []
    kinds = set(db.scalars(
        select(ScenarioModelDraftResource.resource_kind).where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == user_id,
            ScenarioModelDraftResource.draft_status.in_(OPEN_DRAFT_STATUSES),
            ScenarioModelDraftResource.materialization_source != "live_checkpoint",
        ).distinct()
    ).all())
    if not kinds:
        return []
    scopes = {"scenario_model"}
    if kinds & {"entity", "property", "relation", "instance"}:
        scopes.add("ontology")
    if kinds & {"mapping", "conceptual_mapping", "relation_mapping"}:
        scopes.add("mapping")
    if kinds & {"function", "action", "rule", "event"}:
        scopes.add("capabilities")
    if "workflow" in kinds:
        scopes.add("workflow")
    return sorted(scopes)


def _generated_keys(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        if value.get("kind") == "generated" and value.get("key"):
            result.add(str(value["key"]))
        for child in value.values():
            result.update(_generated_keys(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_generated_keys(child))
    return result


def _matches_resource(change_key: str, resource_keys: set[str]) -> bool:
    return any(
        change_key == key or change_key.startswith(f"{key}:")
        for key in resource_keys
    )


def exclude_unvalidated_drafts_from_apply_payload(
    payload: dict[str, Any],
    rows: Iterable[ScenarioModelDraftResource],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove edited staging candidates and their generated dependants.

    This is a selection step only.  The remaining graph still passes through
    the ordinary compiler preflight; no validation rule is weakened here.
    """
    drafts = list(rows)
    if not drafts:
        return copy.deepcopy(payload), {
            "draft_preserved": False,
            "excluded_draft_ids": [],
            "excluded_resource_keys": [],
            "safe_change_count": 0,
        }
    result = copy.deepcopy(payload)
    resources = {
        str(item.get("key")): item
        for section in _FORMAL_SECTIONS
        for item in (
            result.get(section) if isinstance(result.get(section), list) else []
        )
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    blocked: set[str] = set()
    for row in drafts:
        key = str(row.resource_key or "")
        if row.resource_kind == "property":
            # The editable payload is untrusted lineage: a user may remove or
            # change entity_ref.  Exclude the immutable proposal parent instead.
            source = row.source_payload if isinstance(row.source_payload, dict) else {}
            entity_ref = str(source.get("entity_ref") or "")
            if not entity_ref and ":property:" in key:
                entity_ref = key.split(":property:", 1)[0]
            if entity_ref:
                blocked.add(entity_ref)
        elif key in resources:
            blocked.add(key)

    changed = True
    while changed:
        changed = False
        for key, item in resources.items():
            if key not in blocked and _generated_keys(item).intersection(blocked):
                blocked.add(key)
                changed = True

    for section in _FORMAL_SECTIONS:
        values = result.get(section)
        if isinstance(values, list):
            result[section] = [
                item for item in values
                if not isinstance(item, dict)
                or str(item.get("key") or "") not in blocked
            ]
    changes = result.get("changes") if isinstance(result.get("changes"), list) else []
    result["changes"] = [
        item for item in changes
        if isinstance(item, dict)
        and not _matches_resource(str(item.get("change_id") or ""), blocked)
    ]
    unresolved = result.get("unresolved")
    retained_issues: list[dict[str, Any]] = []
    for issue in (unresolved if isinstance(unresolved, list) else []):
        if not isinstance(issue, dict):
            continue
        affected = {
            str(value) for value in (issue.get("affected_change_keys") or [])
            if str(value)
        }
        if affected and all(_matches_resource(value, blocked) for value in affected):
            continue
        retained_issues.append(issue)
    result["unresolved"] = retained_issues
    coverage = result.get("coverage")
    safe_coverage: list[dict[str, Any]] = []
    for raw in (coverage if isinstance(coverage, list) else []):
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        change_keys = [
            str(value) for value in (item.get("change_keys") or [])
            if not _matches_resource(str(value), blocked)
        ]
        item["change_keys"] = change_keys
        if item.get("status") == "modeled" and not change_keys:
            item["status"] = "context"
            item["reason"] = (
                "该段对应的 working draft 已被用户修改，重新校验前不写入正式模型。"
            )
        safe_coverage.append(item)
    result["coverage"] = safe_coverage
    result["coverage_summary"] = {
        "total": len(safe_coverage),
        "modeled": sum(item.get("status") == "modeled" for item in safe_coverage),
        "context": sum(item.get("status") == "context" for item in safe_coverage),
        "irrelevant": sum(item.get("status") == "irrelevant" for item in safe_coverage),
        "ambiguous": sum(item.get("status") == "ambiguous" for item in safe_coverage),
    }
    effective_changes = [
        item for item in result["changes"]
        if item.get("operation") in {"add", "update", "delete"}
    ]
    return result, {
        "draft_preserved": True,
        "excluded_draft_ids": sorted(row.id for row in drafts),
        "excluded_resource_keys": sorted(blocked),
        "edited_draft_count": len(drafts),
        "safe_change_count": len(effective_changes),
    }


def mark_task_outcome(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    proposal_id: str,
    task_id: str,
    created_by_user_id: str,
    task_status: str,
    applied_change_keys: Iterable[str] = (),
    excluded_resource_keys: Iterable[str] = (),
) -> None:
    """Synchronize staging status after a governed task decision."""
    rows = list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.proposal_id == proposal_id,
            ScenarioModelDraftResource.task_id == task_id,
            ScenarioModelDraftResource.created_by_user_id == created_by_user_id,
        )
        .with_for_update()
    ).all())
    applied_keys = {str(value) for value in applied_change_keys if str(value)}
    excluded_keys = {str(value) for value in excluded_resource_keys if str(value)}
    for row in rows:
        if row.draft_status in {"resolved", "superseded"}:
            continue
        if row.draft_status not in APPLYABLE_DRAFT_STATUSES:
            # A task decision must never reinterpret an edited, conflicted,
            # deferred, resolved, or historical row as applied/accepted merely
            # because its stale proposal still carries a matching change key.
            row.enabled = False
            row.publishable = False
            continue
        if task_status in {"deferred", "skipped"}:
            row.draft_status = "deferred"
        elif task_status == "drafted_with_gaps":
            # No governed definition was written.  Keep every candidate inert
            # and visibly awaiting validation; ``accepted`` would falsely
            # suggest the candidate had crossed the authoring boundary.
            row.draft_status = "needs_attention"
        elif task_status in {"applied", "partially_applied"}:
            excluded = any(
                row.resource_key == value
                or row.resource_key.startswith(f"{value}:")
                or value.startswith(f"{row.resource_key}:")
                for value in excluded_keys
            )
            matches = bool(applied_keys) and not excluded and any(
                value == row.resource_key
                or value.startswith(f"{row.resource_key}:")
                or row.resource_key.startswith(f"{value}:")
                for value in applied_keys
            )
            if row.resource_kind in FORMAL_RESOURCE_KINDS and matches:
                row.draft_status = "applied"
            elif row.resource_kind in FORMAL_RESOURCE_KINDS:
                # A formal-shaped candidate that was not among the actual
                # governed writes remains work to validate, even when another
                # independent change in the same task applied successfully.
                row.draft_status = "needs_attention"
            else:
                # Instances and conceptual mappings are staging-only by
                # design.  ``accepted`` records the user's explicit decision
                # without making them runnable or publishable.
                row.draft_status = "accepted"
        row.enabled = False
        row.publishable = False


def _compact_match_text(value: Any) -> str:
    """Normalize user-facing source labels for conservative auto-binding."""
    return re.sub(r"[\s_./\\-]+", "", str(value or "").casefold())


def _auto_repair_source_candidates(
    row: ScenarioModelDraftResource,
    source: DataSource,
) -> tuple[int, str]:
    """Score one missing mapping binding without guessing across sources."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    source_name = _compact_match_text(source.name)
    if not source_name:
        return 0, ""
    labels = [
        payload.get("data_source_name"),
        payload.get("source_name"),
        payload.get("source_label"),
        payload.get("table_name"),
        payload.get("table"),
    ]
    generic = {
        "",
        "待根据附件确认的数据来源",
        "待补充数据来源",
        "未指定数据源",
        "unknown",
    }
    normalized_labels = [
        _compact_match_text(value)
        for value in labels
        if _compact_match_text(value) not in generic
    ]
    if any(label == source_name for label in normalized_labels):
        return 100, "exact"
    if any(source_name in label or label in source_name for label in normalized_labels):
        return 80, "label"
    if source.type == "file_bucket" and not normalized_labels:
        # A scenario with one file bucket has an unambiguous default source.
        return 20, "single-file-bucket"
    return 0, ""


def _parsed_file_schema(source: DataSource) -> dict[str, Any] | None:
    """Extract conservative table/header evidence from successfully parsed files."""
    files = [
        item
        for item in list(source.files or [])
        if item.status == "parsed"
        and bool(str(item.parsed_text or "").strip())
        and not str(item.error or "").strip()
    ]
    if not files:
        return None

    tables: dict[str, set[str]] = defaultdict(set)

    def delimited_columns(line: str) -> set[str]:
        stripped = line.strip().strip("|").strip()
        delimiter = (
            "|" if "|" in stripped
            else "\t" if "\t" in stripped
            else "," if "," in stripped
            else ""
        )
        if not delimiter:
            return set()
        values = {item.strip() for item in stripped.split(delimiter) if item.strip()}
        if len(values) < 2 or all(re.fullmatch(r"[-: ]+", item) for item in values):
            return set()
        return values

    for item in files:
        filename = str(item.filename or "").strip()
        stem = re.sub(r"\.[^.]+$", "", filename)
        aliases = {value for value in (filename, stem) if value}
        current_aliases = set(aliases)
        header_found_for_section = False
        collected_columns: set[str] = set()
        for raw_line in str(item.parsed_text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            sheet_match = re.fullmatch(
                r"(?:#{1,6}\s*)?工作表\s*[:：]\s*(.+)",
                line,
                flags=re.IGNORECASE,
            )
            if sheet_match:
                sheet_name = sheet_match.group(1).strip()
                current_aliases = aliases | ({sheet_name} if sheet_name else set())
                header_found_for_section = False
                continue
            if header_found_for_section:
                continue
            columns = delimited_columns(line)
            if not columns:
                continue
            header_found_for_section = True
            collected_columns.update(columns)
            for alias in current_aliases:
                tables[alias].update(columns)
        for alias in aliases:
            # A parsed file is still useful table-identity evidence even when
            # its parser could not expose a delimited header row.
            tables[alias].update(collected_columns)
    return {"kind": "file", "tables": dict(tables)}


def _database_schema(source: DataSource) -> dict[str, Any]:
    """Inspect one connector through the governed, credential-free schema path."""
    tables = datasource_service.list_tables(source)
    catalog: dict[str, set[str]] = {}
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        table_name = str(table.get("name") or "").strip()
        if not table_name:
            continue
        catalog[table_name] = {
            str(column.get("name") or "").strip()
            for column in (table.get("columns") or [])
            if isinstance(column, dict) and str(column.get("name") or "").strip()
        }
    return {"kind": "database", "tables": catalog}


def _mapping_source_references(payload: dict[str, Any]) -> set[str]:
    references: set[str] = set()
    for key in (
        "data_source_id",
        "data_source_ref",
        "data_source",
        "join_data_source_id",
        "join_data_source_ref",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("data_source_id")
        normalized = str(value or "").strip()
        if normalized:
            references.add(normalized)
    return references


def _mapping_schema_contract(payload: dict[str, Any]) -> tuple[str, set[str], str]:
    table_values = {
        str(payload.get(key) or "").strip()
        for key in ("table_name", "table", "join_table_name")
        if str(payload.get(key) or "").strip()
    }
    if len(table_values) != 1:
        reason = "草稿未提供可核验的物理表名" if not table_values else "草稿包含冲突的物理表引用"
        return "", set(), reason

    physical_columns: set[str] = set()
    raw_map = payload.get("column_map")
    if isinstance(raw_map, dict):
        physical_columns.update(
            str(value or "").strip()
            for value in raw_map.values()
            if str(value or "").strip()
        )
    for key in ("foreign_key_column", "source_key_column", "target_key_column"):
        value = str(payload.get(key) or "").strip()
        if value:
            physical_columns.add(value)
    if not physical_columns:
        return next(iter(table_values)), set(), "草稿未提供可核验的物理字段映射"
    return next(iter(table_values)), physical_columns, ""


def _validate_mapping_schema(
    payload: dict[str, Any],
    schema: dict[str, Any] | None,
) -> tuple[bool, str]:
    table_name, required_columns, contract_error = _mapping_schema_contract(payload)
    if contract_error:
        return False, contract_error
    if schema is None:
        return False, "数据源连接可达，但受控表结构读取失败"

    tables = schema.get("tables") if isinstance(schema, dict) else {}
    tables = tables if isinstance(tables, dict) else {}
    available: set[str] | None = None
    if schema.get("kind") == "file":
        matches = [
            set(columns or set())
            for candidate, columns in tables.items()
            if _compact_match_text(candidate) == _compact_match_text(table_name)
        ]
        if len(matches) == 1:
            available = matches[0]
        elif len(matches) > 1 and all(item == matches[0] for item in matches[1:]):
            available = matches[0]
    elif table_name in tables:
        available = set(tables.get(table_name) or set())
    if available is None:
        return False, f"受控结构中不存在物理表“{table_name}”"
    missing = sorted(required_columns - available)
    if missing:
        return False, f"物理表“{table_name}”缺少字段：{'、'.join(missing)}"
    return True, ""


def auto_repair_data_source_drafts(
    db: Session,
    source: DataSource,
    *,
    validated_source_id: str | None = None,
    created_by_user_id: str | None = None,
) -> dict[str, Any]:
    """Repair missing logical mapping bindings after a source becomes usable.

    This is deliberately small and deterministic.  The caller must identify
    the exact source whose connection/schema validation just succeeded, and
    that source is reloaded and scope-checked before any issue is changed.  It
    never invents columns or imports rows.  The repaired draft remains inert;
    its source blocker is removed only after the mapped table and every
    physical column are proven by governed schema evidence.
    """
    source_id = str(source.id or "").strip()
    scenario_id = str(source.scenario_id or "").strip()
    tenant_id = str(db.info.get("tenant_id") or source.tenant_id or "").strip()
    user_id = str(created_by_user_id or db.info.get("user_id") or "").strip()
    empty_result = {
        "repaired_count": 0,
        "draft_ids": [],
        "source_id": source_id,
    }
    if (
        not source_id
        or str(validated_source_id or "").strip() != source_id
        or not scenario_id
        or not tenant_id
        or not user_id
    ):
        return empty_result

    # Do not trust an arbitrary ORM object or a stale pre-update instance as
    # proof that a connector is usable.  The router supplies the exact source
    # id only after its validation operation succeeds; this locked reload then
    # proves that the same source still exists in the caller's tenant/scenario
    # and that the successful status has been flushed to durable state.
    persisted_source = db.scalar(
        select(DataSource)
        .where(
            DataSource.id == source_id,
            DataSource.tenant_id == tenant_id,
            DataSource.scenario_id == scenario_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        persisted_source is None
        or str(source.tenant_id or "").strip() != tenant_id
        or str(persisted_source.type or "") != str(source.type or "")
        or persisted_source.status != "ok"
    ):
        return empty_result

    source = persisted_source

    rows = list(db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario_id,
            ScenarioModelDraftResource.created_by_user_id == user_id,
            ScenarioModelDraftResource.resource_kind.in_(
                {"mapping", "conceptual_mapping", "relation_mapping"}
            ),
            ScenarioModelDraftResource.draft_status.in_(OPEN_DRAFT_STATUSES),
        )
        .with_for_update()
    ).all())
    available_sources = list(db.scalars(
        select(DataSource).where(
            DataSource.tenant_id == tenant_id,
            DataSource.scenario_id == scenario_id,
            DataSource.status == "ok",
        )
        .order_by(DataSource.created_at, DataSource.id)
    ).all())
    eligible_sources: list[DataSource] = []
    source_schema: dict[str, Any] | None = None
    for candidate in available_sources:
        if candidate.type == "file_bucket":
            file_schema = _parsed_file_schema(candidate)
            if file_schema is None:
                continue
            eligible_sources.append(candidate)
            if candidate.id == source.id:
                source_schema = file_schema
            continue
        eligible_sources.append(candidate)
        if candidate.id == source.id:
            try:
                source_schema = _database_schema(candidate)
            except Exception:  # noqa: BLE001 - keep the useful identity, not the blocker.
                # The just-tested connector passed SELECT 1, so its identity may
                # still be useful, but no source blocker can be cleared without
                # the governed schema inspection below succeeding.
                source_schema = None
    if source.id not in {candidate.id for candidate in eligible_sources}:
        return empty_result
    repaired: list[str] = []

    def keep_issue(issue: dict[str, Any]) -> bool:
        code = str(issue.get("code") or "").upper()
        message = str(issue.get("message") or "").upper()
        if code in AUTO_REPAIR_MAPPING_ISSUE_CODES:
            return False
        # Older compiler revisions wrapped the same dependency issue in
        # `document_reported_issue`; remove that wrapper too once the
        # source is available so the assistant does not keep reporting a
        # resolved missing-source blocker.
        return not (
            code == "DOCUMENT_REPORTED_ISSUE"
            and any(issue_code in message for issue_code in AUTO_REPAIR_MAPPING_ISSUE_CODES)
        )

    def without_stale_auto_repair_issues(
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            issue
            for issue in issues
            if str(issue.get("code") or "").upper()
            not in {
                AUTO_REPAIR_SCHEMA_ISSUE_CODE,
                AUTO_REPAIR_AMBIGUITY_ISSUE_CODE,
                AUTO_REPAIR_BINDING_ISSUE_CODE,
            }
        ]

    def mark_ambiguous(
        row: ScenarioModelDraftResource,
        issues: list[dict[str, Any]],
        winners: list[DataSource],
    ) -> None:
        names = "、".join(sorted({str(item.name or item.id) for item in winners}))
        row.validation_issues = _merge_issues(
            without_stale_auto_repair_issues(issues),
            {
                "code": AUTO_REPAIR_AMBIGUITY_ISSUE_CODE,
                "message": f"多个数据源同等匹配该草稿：{names}。系统不会按测试顺序猜测绑定。",
                "blocking": True,
                "resolution_hint": "请在草稿中明确选择唯一 data_source_id 后重新验证。",
            },
        )
        row.draft_status = "needs_attention"
        row.enabled = False
        row.publishable = False

    for row in rows:
        payload = _payload_dict(row.payload)
        issues = _issue_list(row.validation_issues)
        cleaned_issues = [issue for issue in issues if keep_issue(issue)]
        retry_issue_codes = {
            str(issue.get("code") or "").upper()
            for issue in issues
        } & {AUTO_REPAIR_SCHEMA_ISSUE_CODE, AUTO_REPAIR_AMBIGUITY_ISSUE_CODE}
        if len(cleaned_issues) == len(issues) and not retry_issue_codes:
            continue
        referenced_source_ids = _mapping_source_references(payload)
        if referenced_source_ids:
            # Never reinterpret or clear a dependency for another, foreign, or
            # nonexistent source merely because some source in the scenario
            # has just become usable.
            if referenced_source_ids != {source.id}:
                continue
        else:
            scores = [
                (*_auto_repair_source_candidates(row, candidate), candidate)
                for candidate in eligible_sources
            ]
            scores = [item for item in scores if item[0] > 0]
            if not scores:
                continue
            top_score = max(item[0] for item in scores)
            winners = [item[2] for item in scores if item[0] == top_score]
            if len(winners) != 1:
                if source.id in {winner.id for winner in winners}:
                    mark_ambiguous(row, issues, winners)
                continue
            if winners[0].id != source.id:
                continue
            payload["data_source_id"] = source.id
            payload["data_source_ref"] = source.id
            payload["data_source_name"] = source.name
            if not str(payload.get("source_label") or "").strip():
                payload["source_label"] = source.name

        if source.type == "file_bucket" and not str(payload.get("table_name") or "").strip():
            validated_files = [
                item
                for item in list(source.files or [])
                if item.status == "parsed"
                and bool(str(item.parsed_text or "").strip())
                and not str(item.error or "").strip()
            ]
            if len(validated_files) == 1:
                payload["table_name"] = re.sub(
                    r"\.[^.]+$", "", str(validated_files[0].filename or "")
                )
        row.payload = release_service.safe_snapshot_content(payload)
        schema_ok, schema_error = _validate_mapping_schema(
            payload,
            source_schema,
        )
        if schema_ok:
            final_issues = without_stale_auto_repair_issues(cleaned_issues)
            row.validation_issues = _merge_issues(
                final_issues,
                {
                    "code": AUTO_REPAIR_BINDING_ISSUE_CODE,
                    "message": f"已依据受控表结构自动绑定数据源“{source.name}”。",
                    "blocking": False,
                    "resolution_hint": "数据源、物理表和字段均已核验。",
                },
            )
            if any(issue.get("blocking", True) for issue in row.validation_issues):
                row.draft_status = "needs_attention"
            else:
                row.draft_status = "accepted"
        else:
            # Binding a useful identity is reversible and helps the user, but
            # the original missing-source blocker stays until the table and
            # every physical column are proven by governed schema evidence.
            row.validation_issues = _merge_issues(
                without_stale_auto_repair_issues(issues),
                {
                    "code": AUTO_REPAIR_BINDING_ISSUE_CODE,
                    "message": f"已暂存数据源“{source.name}”的绑定，物理结构尚未通过核验。",
                    "blocking": False,
                    "resolution_hint": "修正表名或字段映射后重新执行数据源测试。",
                },
                {
                    "code": AUTO_REPAIR_SCHEMA_ISSUE_CODE,
                    "message": schema_error,
                    "blocking": True,
                    "resolution_hint": "必须从受控结构读取中确认真实表及全部物理字段。",
                },
            )
            row.draft_status = "needs_attention"
        row.enabled = False
        row.publishable = False
        repaired.append(row.id)
    return {
        "repaired_count": len(repaired),
        "draft_ids": repaired,
        "source_id": source_id,
    }
