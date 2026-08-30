"""Derive inert capability-port and data-role suggestions from compiler output.

The assistant may use documents and catalog structure to understand a business
scene, but those sources are evidence, not a permanent runtime binding.  This
module is deliberately deterministic and provider-independent: it turns an
already-normalized function contract into a draft ``ScenarioCapabilityPort``
shape and keeps all source observations in a separate evidence sidecar.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

from ..catalog_schemas import ScenarioCapabilityPortCreate
from . import catalog_service


CAPABILITY_MODELING_VERSION = 2

# These keys identify platform storage or binding identities.  They may exist
# in source catalogs while compiling, but never in a suggested logical port or
# its evidence metadata.  JSON Schema documents are treated as user contracts,
# so a business property that happens to share one of these names is allowed.
PHYSICAL_BINDING_KEYS = frozenset({
    "asset_id",
    "asset_version_id",
    "bucket_file_id",
    "connector_binding_id",
    "data_asset_id",
    "data_asset_version_id",
    "data_source_id",
    "dataset_head_id",
    "dataset_id",
    "dataset_schema_id",
    "dataset_version_id",
    "scenario_dataset_binding_id",
})

_PORT_FIELDS = frozenset(ScenarioCapabilityPortCreate.model_fields)
_SEMANTIC_SECTIONS = (
    "entities",
    "relations",
    "functions",
    "actions",
    "rules",
    "events",
    "workflows",
)
_RESOURCE_KIND_BY_SECTION = {
    "entities": "entity",
    "relations": "relation",
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
}

_CAPABILITY_SECTIONS = (
    ("functions", "function"),
    ("actions", "action"),
    ("workflows", "workflow"),
)
_MANAGED_PORT_EVIDENCE_KINDS = frozenset({
    "versioned_data",
    "document_attachment",
    "reference",
    "rules",
    "connector",
})
_MANAGED_PORT_FIELDS = frozenset({
    "port_key",
    "name",
    "description",
    "direction",
    "role",
    "media_kind",
    "schema_document",
    "is_required",
    "cardinality",
    "binding_policy",
    "binding_kinds",
    "evidence_kind",
    "evidence_refs",
    "confidence",
})
_MEDIA_BINDING_KINDS = {
    "dataset": ("dataset_head", "dataset_version"),
    "document": ("asset_version",),
    "artifact": ("asset_version",),
    "connector": ("connector_binding",),
}


def _json_copy(value: Any, fallback: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:  # noqa: BLE001 - compiler sidecars must remain inert.
        return copy.deepcopy(fallback)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _confidence(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number != number:  # NaN
        number = default
    return round(max(0.0, min(number, 1.0)), 6)


def _physical_reference_paths(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> list[str]:
    """Find physical binding fields outside declarative JSON Schema nodes."""
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            child_path = (*path, str(raw_key))
            empty_optional_port_link = (
                not path
                and key in {"dataset_id", "dataset_schema_id"}
                and child in (None, "")
            )
            if key in PHYSICAL_BINDING_KEYS and not empty_optional_port_link:
                found.append(".".join(child_path))
            if key != "schema_document":
                found.extend(_physical_reference_paths(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                _physical_reference_paths(child, path=(*path, str(index)))
            )
    return found


def normalize_port_candidate(payload: Any) -> ScenarioCapabilityPortCreate:
    """Validate one editable port candidate without accepting runtime binding ids."""
    if not isinstance(payload, dict):
        raise ValueError("能力端口候选 payload 必须是 JSON 对象")
    physical_paths = _physical_reference_paths(payload)
    if physical_paths:
        raise ValueError(
            "能力端口候选不得固化物理数据或运行绑定："
            + "、".join(physical_paths[:20])
        )
    if "confidence" in payload:
        try:
            raw_confidence = float(payload.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError("能力端口候选 confidence 必须在 0 到 1 之间") from exc
        if not 0 <= raw_confidence <= 1:
            raise ValueError("能力端口候选 confidence 必须在 0 到 1 之间")
    requested_status = str(payload.get("status") or "draft").strip().casefold()
    if requested_status != "draft":
        raise ValueError("能力端口候选正式化后只能保持 draft，不能自动激活")
    port_payload = {
        key: _json_copy(value, None)
        for key, value in payload.items()
        if key in _PORT_FIELDS
    }
    port_payload["dataset_id"] = None
    port_payload["dataset_schema_id"] = None
    port_payload["status"] = "draft"
    port = ScenarioCapabilityPortCreate.model_validate(port_payload)
    catalog_service.validate_catalog_key(port.port_key, "能力端口 key")
    return port


def normalize_managed_data_port_declarations(
    value: Any,
    *,
    resource_kind: str,
    resource_key: str,
    resource_evidence_refs: Iterable[str],
    resource_confidence: float,
) -> list[dict[str, Any]]:
    """Validate explicit managed-data requirements for one capability.

    Function/action JSON schemas describe ordinary typed request and response
    values.  They are deliberately absent from this function: a managed port
    exists only when the source-backed capability declaration names one of the
    governed data dependency kinds below and cites evidence already owned by
    that same resource.
    """
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ValueError("managed_data_ports 必须是数组")
    if len(value) > 50:
        raise ValueError("单个能力最多声明 50 个 managed_data_ports")

    owned_refs = {
        str(ref).strip()
        for ref in resource_evidence_refs
        if str(ref).strip()
    }
    normalized_kind = str(resource_kind or "").strip().casefold()
    if normalized_kind not in {"function", "action", "workflow"}:
        raise ValueError("只有 Function、Action 或 Workflow 可以声明受管数据端口")
    normalized_key = _bounded_text(resource_key, 240)
    if not normalized_key:
        raise ValueError("受管数据端口缺少所属能力 key")

    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"managed_data_ports[{index}] 必须是对象")
        unsupported = sorted(set(raw) - _MANAGED_PORT_FIELDS)
        if unsupported:
            raise ValueError(
                f"managed_data_ports[{index}] 包含未治理字段："
                + "、".join(unsupported[:20])
            )
        physical_paths = _physical_reference_paths(raw)
        if physical_paths:
            raise ValueError(
                f"managed_data_ports[{index}] 不得包含物理资源或运行绑定："
                + "、".join(physical_paths[:20])
            )

        evidence_kind = str(raw.get("evidence_kind") or "").strip().casefold()
        if evidence_kind not in _MANAGED_PORT_EVIDENCE_KINDS:
            raise ValueError(
                f"managed_data_ports[{index}].evidence_kind 必须明确为 "
                "versioned_data、document_attachment、reference、rules 或 connector"
            )
        evidence_refs = list(dict.fromkeys(
            str(ref).strip()
            for ref in (raw.get("evidence_refs") or [])
            if str(ref).strip()
        ))
        if not evidence_refs:
            raise ValueError(f"managed_data_ports[{index}] 必须提供 evidence_refs")
        foreign_refs = sorted(set(evidence_refs) - owned_refs)
        if foreign_refs:
            raise ValueError(
                f"managed_data_ports[{index}] 引用了不属于该能力的证据："
                + "、".join(foreign_refs[:20])
            )

        direction = str(raw.get("direction") or "input").strip().casefold()
        role = str(raw.get("role") or (
            "output" if direction == "output" else "invocation_input"
        )).strip().casefold()
        media_kind = str(raw.get("media_kind") or "").strip().casefold()
        if media_kind not in _MEDIA_BINDING_KINDS:
            raise ValueError(
                f"managed_data_ports[{index}].media_kind 必须是 "
                "dataset、document、artifact 或 connector；普通文本/JSON 属于 typed inputs"
            )
        if direction == "output":
            if role != "output":
                raise ValueError(f"managed_data_ports[{index}] 输出端口必须使用 output 角色")
            if media_kind == "connector" or evidence_kind in {"reference", "rules", "connector"}:
                raise ValueError(f"managed_data_ports[{index}] 输出端口的数据证据类型无效")
            binding_policy = "none"
            is_required = False
            binding_kinds: tuple[str, ...] = ()
        else:
            if direction != "input" or role not in {"invocation_input", "reference", "rules"}:
                raise ValueError(
                    f"managed_data_ports[{index}] 输入端口只能使用 invocation_input、reference 或 rules 角色"
                )
            expected_role = {
                "reference": "reference",
                "rules": "rules",
            }.get(evidence_kind)
            if expected_role and role != expected_role:
                raise ValueError(
                    f"managed_data_ports[{index}] 的 {evidence_kind} 证据必须使用 {expected_role} 角色"
                )
            if evidence_kind == "versioned_data" and media_kind != "dataset":
                raise ValueError(f"managed_data_ports[{index}] 版本化数据必须使用 dataset media_kind")
            if evidence_kind == "document_attachment" and media_kind not in {"document", "artifact"}:
                raise ValueError(f"managed_data_ports[{index}] 文档/附件必须使用 document 或 artifact media_kind")
            if evidence_kind == "connector" and media_kind != "connector":
                raise ValueError(f"managed_data_ports[{index}] connector 依赖必须使用 connector media_kind")
            if evidence_kind in {"reference", "rules"} and media_kind not in {
                "dataset", "document", "artifact", "connector",
            }:
                raise ValueError(f"managed_data_ports[{index}] 参考或规则端口 media_kind 无效")
            binding_policy = str(
                raw.get("binding_policy") or (
                    "release_pinned" if role == "rules" else "per_invocation"
                )
            ).strip().casefold()
            if binding_policy not in {
                "per_invocation", "scenario_default", "release_pinned",
            }:
                raise ValueError(f"managed_data_ports[{index}] 输入端口 binding_policy 无效")
            raw_required = raw.get("is_required", True)
            if not isinstance(raw_required, bool):
                raise ValueError(
                    f"managed_data_ports[{index}].is_required 必须是布尔值"
                )
            is_required = raw_required
            configured_kinds = raw.get("binding_kinds")
            if configured_kinds in (None, "", []):
                binding_kinds = _MEDIA_BINDING_KINDS[media_kind]
            else:
                if isinstance(configured_kinds, str):
                    configured_kinds = [configured_kinds]
                if not isinstance(configured_kinds, list) or not configured_kinds:
                    raise ValueError(f"managed_data_ports[{index}].binding_kinds 必须是非空数组")
                binding_kinds = tuple(sorted({
                    str(item).strip().casefold() for item in configured_kinds if str(item).strip()
                }))
                if not binding_kinds or not set(binding_kinds).issubset(
                    _MEDIA_BINDING_KINDS[media_kind]
                ):
                    raise ValueError(
                        f"managed_data_ports[{index}].binding_kinds 与 media_kind 不兼容"
                    )

        port_key = catalog_service.validate_catalog_key(
            str(raw.get("port_key") or "").strip(),
            f"managed_data_ports[{index}].port_key",
        )
        folded_key = port_key.casefold()
        if folded_key in seen_keys:
            raise ValueError(f"managed_data_ports 中端口 key 重复：{port_key}")
        seen_keys.add(folded_key)
        confidence = _confidence(raw.get("confidence"), default=resource_confidence)
        port = normalize_port_candidate({
            "capability_kind": normalized_kind,
            "capability_key": normalized_key,
            "port_key": port_key,
            "name": _bounded_text(raw.get("name") or port_key, 300),
            "description": _bounded_text(raw.get("description"), 8_000),
            "direction": direction,
            "role": role,
            "media_kind": media_kind,
            "schema_document": _json_copy(raw.get("schema_document"), {}),
            "is_required": is_required,
            "cardinality": str(raw.get("cardinality") or "one").strip().casefold(),
            "binding_policy": binding_policy,
            "status": "draft",
            "config": {
                "allowed_binding_kinds": list(binding_kinds),
                "evidence_kind": evidence_kind,
                "contract_source": {
                    "resource_kind": normalized_kind,
                    "resource_key": normalized_key,
                },
            },
        })
        result.append({
            "port": port.model_dump(mode="json", exclude_none=True),
            "evidence_kind": evidence_kind,
            "evidence_refs": evidence_refs,
            "confidence": confidence,
        })
    return result


def _schema_evidence(
    *,
    resource_kind: str,
    resource_key: str,
    direction: str,
    schema_document: Any,
    evidence_refs: Iterable[str],
    confidence: float,
) -> dict[str, Any]:
    schema = _json_copy(schema_document, {}) if isinstance(schema_document, dict) else {}
    return {
        "evidence_type": "json_schema",
        "resource_kind": resource_kind,
        "resource_key": resource_key,
        "direction": direction,
        "schema_document": schema,
        "schema_hash": _canonical_hash(schema),
        "evidence_refs": list(dict.fromkeys(
            str(ref)[:300] for ref in evidence_refs if str(ref).strip()
        )),
        "confidence": confidence,
    }


def _semantic_evidence(
    *,
    resource_kind: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    return {
        "resource_kind": resource_kind,
        "resource_key": _bounded_text(item.get("key"), 240),
        "name": _bounded_text(item.get("name"), 300),
        "description": _bounded_text(item.get("description"), 2_000),
        "evidence_refs": list(dict.fromkeys(
            str(ref)[:300]
            for ref in (item.get("evidence_refs") or [])
            if str(ref).strip()
        )),
        "confidence": _confidence(item.get("confidence")),
    }


def _entity_schema(item: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw in item.get("properties") or []:
        if not isinstance(raw, dict):
            continue
        name = _bounded_text(raw.get("name"), 300)
        if not name:
            continue
        data_type = str(raw.get("data_type") or "string").strip().casefold()
        schema: dict[str, Any]
        if data_type == "date":
            schema = {"type": "string", "format": "date"}
        elif data_type == "datetime":
            schema = {"type": "string", "format": "date-time"}
        elif data_type in {"string", "number", "integer", "boolean", "null"}:
            schema = {"type": data_type}
        elif data_type == "array":
            schema = {"type": "array", "items": {}}
        elif data_type == "object":
            schema = {"type": "object"}
        else:
            schema = {"type": "string"}
        enum_values = raw.get("enum_values")
        if isinstance(enum_values, list) and enum_values:
            schema["enum"] = _json_copy(enum_values[:500], [])
        properties[name] = schema
        if bool(raw.get("is_required")):
            required.append(name)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _resource_evidence(
    sections: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    schemas: list[dict[str, Any]] = []
    semantics: list[dict[str, Any]] = []
    for section in _SEMANTIC_SECTIONS:
        kind = _RESOURCE_KIND_BY_SECTION[section]
        for item in sections.get(section) or []:
            if not isinstance(item, dict):
                continue
            key = _bounded_text(item.get("key"), 240)
            refs = item.get("evidence_refs") or []
            confidence = _confidence(item.get("confidence"))
            semantics.append(_semantic_evidence(resource_kind=kind, item=item))
            if section == "entities":
                schemas.append(_schema_evidence(
                    resource_kind="entity",
                    resource_key=key,
                    direction="shape",
                    schema_document=_entity_schema(item),
                    evidence_refs=refs,
                    confidence=confidence,
                ))
            elif section == "functions":
                for direction, field in (
                    ("input", "input_schema"),
                    ("output", "output_schema"),
                ):
                    schemas.append(_schema_evidence(
                        resource_kind="function",
                        resource_key=key,
                        direction=direction,
                        schema_document=item.get(field),
                        evidence_refs=refs,
                        confidence=confidence,
                    ))
            elif section == "actions":
                schemas.append(_schema_evidence(
                    resource_kind="action",
                    resource_key=key,
                    direction="input",
                    schema_document=item.get("input_schema"),
                    evidence_refs=refs,
                    confidence=confidence,
                ))
            elif section == "events":
                schemas.append(_schema_evidence(
                    resource_kind="event",
                    resource_key=key,
                    direction="payload",
                    schema_document=item.get("payload_schema"),
                    evidence_refs=refs,
                    confidence=confidence,
                ))
    return schemas, semantics


def _port_suggestions(
    sections: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for section, resource_kind in _CAPABILITY_SECTIONS:
        for item in sections.get(section) or []:
            if not isinstance(item, dict):
                continue
            resource_key = _bounded_text(item.get("key"), 240)
            if not resource_key:
                continue
            semantic = _semantic_evidence(resource_kind=resource_kind, item=item)
            for declaration in item.get("managed_data_ports") or []:
                if not isinstance(declaration, dict) or not isinstance(
                    declaration.get("port"), dict
                ):
                    continue
                port = normalize_port_candidate(declaration["port"])
                if port.port_key.casefold() in seen_keys:
                    raise ValueError(f"能力端口建议 key 重复：{port.port_key}")
                seen_keys.add(port.port_key.casefold())
                evidence_refs = list(dict.fromkeys(
                    str(ref)[:300]
                    for ref in (declaration.get("evidence_refs") or [])
                    if str(ref).strip()
                ))
                confidence = _confidence(
                    declaration.get("confidence"),
                    default=_confidence(item.get("confidence")),
                )
                schema = _json_copy(port.schema_document, {})
                suggestion = {
                    "suggestion_key": f"capability_port.{port.port_key}",
                    "port": port.model_dump(mode="json", exclude_none=True),
                    "schema_evidence": [_schema_evidence(
                        resource_kind=resource_kind,
                        resource_key=resource_key,
                        direction=port.direction,
                        schema_document=schema,
                        evidence_refs=evidence_refs,
                        confidence=confidence,
                    )],
                    "semantic_evidence": [semantic],
                    "evidence_refs": evidence_refs,
                    "confidence": confidence,
                }
                if _physical_reference_paths(suggestion):
                    raise ValueError("能力端口建议包含物理运行绑定")
                suggestions.append(suggestion)
    return suggestions


def _catalog_schema_evidence(mapping_catalog: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_source in mapping_catalog:
        if not isinstance(raw_source, dict):
            continue
        relations: list[dict[str, Any]] = []
        for raw_relation in raw_source.get("tables") or []:
            if not isinstance(raw_relation, dict):
                continue
            name = _bounded_text(raw_relation.get("name"), 300)
            if not name:
                continue
            fields = [
                {
                    "name": _bounded_text(raw_field.get("name"), 300),
                    "type": _bounded_text(raw_field.get("type"), 100),
                    "is_key": bool(raw_field.get("pk")),
                }
                for raw_field in (raw_relation.get("columns") or [])
                if isinstance(raw_field, dict)
                and _bounded_text(raw_field.get("name"), 300)
            ]
            relations.append({"name": name, "fields": fields})
        if not relations:
            continue
        structure = {
            "evidence_type": "catalog_structure",
            "source_type": _bounded_text(raw_source.get("type"), 60),
            "relations": relations,
        }
        fingerprint = _canonical_hash(structure)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append({
            "evidence_key": f"catalog_schema.{fingerprint[:20]}",
            "role": "modeling_evidence",
            "source_kind": "catalog_schema",
            "content_fingerprint": fingerprint,
            "schema_evidence": [structure],
            "semantic_evidence": [],
            "evidence_refs": [],
            "confidence": 1.0,
            "runtime_binding": False,
        })
    return result


def _document_role_suggestions(
    *,
    source_bundle: dict[str, Any],
    schemas: list[dict[str, Any]],
    semantics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs_by_source: dict[str, list[str]] = defaultdict(list)
    for item in source_bundle.get("paragraphs") or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "")
        ref = str(item.get("ref") or "")
        if source_id and ref:
            refs_by_source[source_id].append(ref)
    result: list[dict[str, Any]] = []
    for raw in source_bundle.get("documents") or []:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source_id") or "")
        refs = list(dict.fromkeys(refs_by_source.get(source_id, [])))
        ref_set = set(refs)
        fingerprint = _bounded_text(raw.get("sha256"), 64)
        if len(fingerprint) != 64:
            fingerprint = _canonical_hash({
                "source_kind": raw.get("source_kind"),
                "filename": raw.get("filename"),
                "refs": refs,
            })
        source_schemas = [
            _json_copy(item, {})
            for item in schemas
            if ref_set.intersection(item.get("evidence_refs") or [])
        ]
        source_semantics = [
            _json_copy(item, {})
            for item in semantics
            if ref_set.intersection(item.get("evidence_refs") or [])
        ]
        inferred_confidences = [
            _confidence(item.get("confidence"))
            for item in (*source_schemas, *source_semantics)
        ]
        result.append({
            "evidence_key": f"document.{fingerprint[:20]}",
            "role": "modeling_evidence",
            "source_kind": _bounded_text(raw.get("source_kind"), 40),
            "label": _bounded_text(raw.get("filename"), 300),
            "content_fingerprint": fingerprint,
            "schema_evidence": source_schemas,
            "semantic_evidence": source_semantics,
            "evidence_refs": refs,
            "confidence": (
                round(sum(inferred_confidences) / len(inferred_confidences), 6)
                if inferred_confidences
                else 1.0
            ),
            "runtime_binding": False,
        })
    return result


def build_capability_modeling_sidecar(
    *,
    normalized_sections: dict[str, list[dict[str, Any]]],
    source_bundle: dict[str, Any],
    mapping_catalog: Iterable[Any] = (),
) -> dict[str, Any]:
    """Return protocol-neutral suggestions without mutating any formal model."""
    sections = {
        section: [
            _json_copy(item, {})
            for item in (normalized_sections.get(section) or [])
            if isinstance(item, dict)
        ]
        for section in _SEMANTIC_SECTIONS
    }
    schemas, semantics = _resource_evidence(sections)
    ports = _port_suggestions(sections)
    data_roles = _document_role_suggestions(
        source_bundle=source_bundle,
        schemas=schemas,
        semantics=semantics,
    )
    data_roles.extend(_catalog_schema_evidence(mapping_catalog))
    sidecar = {
        "version": CAPABILITY_MODELING_VERSION,
        "ports": ports,
        "data_roles": data_roles,
        "zero_port_capability": not ports,
        "policy": {
            "source_default_role": "modeling_evidence",
            "runtime_binding_inferred": False,
        },
    }
    physical_paths = _physical_reference_paths(sidecar)
    if physical_paths:
        raise ValueError(
            "顾问能力建模侧车包含物理运行绑定："
            + "、".join(physical_paths[:20])
        )
    return sidecar


def merge_capability_modeling_sidecars(
    current: Any,
    staged: Any,
    *,
    replace_ports: bool,
) -> dict[str, Any]:
    """Merge staged evidence while replacing ports only in capability stage."""
    current_value = current if isinstance(current, dict) else {}
    staged_value = staged if isinstance(staged, dict) else {}
    selected_ports = (
        staged_value.get("ports")
        if replace_ports
        else current_value.get("ports")
    )
    if not isinstance(selected_ports, list):
        selected_ports = (
            staged_value.get("ports")
            if isinstance(staged_value.get("ports"), list)
            else []
        )
    ports: list[dict[str, Any]] = []
    seen_port_keys: set[str] = set()
    for raw in selected_ports:
        if not isinstance(raw, dict) or not isinstance(raw.get("port"), dict):
            continue
        port = normalize_port_candidate({
            **_json_copy(raw["port"], {}),
            "schema_evidence": _json_copy(raw.get("schema_evidence"), []),
            "semantic_evidence": _json_copy(raw.get("semantic_evidence"), []),
            "confidence": raw.get("confidence", 0.0),
        })
        folded_key = port.port_key.casefold()
        if folded_key in seen_port_keys:
            continue
        seen_port_keys.add(folded_key)
        ports.append(_json_copy(raw, {}))

    roles_by_key: dict[str, dict[str, Any]] = {}
    role_order: list[str] = []
    for collection in (
        current_value.get("data_roles"),
        staged_value.get("data_roles"),
    ):
        for raw in collection if isinstance(collection, list) else []:
            if not isinstance(raw, dict):
                continue
            evidence_key = _bounded_text(raw.get("evidence_key"), 120)
            if not evidence_key:
                evidence_key = f"evidence.{_canonical_hash(raw)[:20]}"
            if evidence_key not in roles_by_key:
                roles_by_key[evidence_key] = _json_copy(raw, {})
                role_order.append(evidence_key)
                continue
            target = roles_by_key[evidence_key]
            for field in ("schema_evidence", "semantic_evidence"):
                merged: list[dict[str, Any]] = []
                seen: set[str] = set()
                for item in [
                    *(target.get(field) or []),
                    *(raw.get(field) or []),
                ]:
                    if not isinstance(item, dict):
                        continue
                    signature = _canonical_hash(item)
                    if signature not in seen:
                        seen.add(signature)
                        merged.append(_json_copy(item, {}))
                target[field] = merged
            target["evidence_refs"] = list(dict.fromkeys(
                str(ref)[:300]
                for ref in [
                    *(target.get("evidence_refs") or []),
                    *(raw.get("evidence_refs") or []),
                ]
                if str(ref).strip()
            ))
            target["confidence"] = max(
                _confidence(target.get("confidence")),
                _confidence(raw.get("confidence")),
            )
            target["role"] = "modeling_evidence"
            target["runtime_binding"] = False

    sidecar = {
        "version": CAPABILITY_MODELING_VERSION,
        "ports": ports,
        "data_roles": [roles_by_key[key] for key in role_order],
        "zero_port_capability": not ports,
        "policy": {
            "source_default_role": "modeling_evidence",
            "runtime_binding_inferred": False,
        },
    }
    physical_paths = _physical_reference_paths(sidecar)
    if physical_paths:
        raise ValueError(
            "分阶段顾问能力建模侧车包含物理运行绑定："
            + "、".join(physical_paths[:20])
        )
    return sidecar


def capability_port_draft_candidates(
    sidecar: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project validated suggestions into the existing inert candidate lane."""
    result: list[dict[str, Any]] = []
    for raw in sidecar.get("ports") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("port"), dict):
            continue
        port = normalize_port_candidate({
            **_json_copy(raw.get("port"), {}),
            "schema_evidence": _json_copy(raw.get("schema_evidence"), []),
            "semantic_evidence": _json_copy(raw.get("semantic_evidence"), []),
            "evidence_refs": _json_copy(raw.get("evidence_refs"), []),
            "confidence": raw.get("confidence", 0.0),
        })
        payload = {
            **port.model_dump(mode="json", exclude_none=True),
            "schema_evidence": _json_copy(raw.get("schema_evidence"), []),
            "semantic_evidence": _json_copy(raw.get("semantic_evidence"), []),
            "evidence_refs": _json_copy(raw.get("evidence_refs"), []),
            "confidence": _confidence(raw.get("confidence")),
        }
        result.append({
            "resource_kind": "capability_port",
            "resource_key": port.port_key,
            "task_id": "capabilities",
            "display_name": port.name,
            "payload": payload,
            "evidence_refs": list(payload["evidence_refs"]),
            "validation_issues": [],
            "validation_status": "ready",
            "formal_candidate": True,
            "promotion_eligible": True,
            "activation_status": "inactive",
            "enabled": False,
            "publishable": False,
        })
    return result
