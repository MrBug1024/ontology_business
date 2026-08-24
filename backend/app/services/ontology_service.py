"""本体服务：图谱构建（schema / instance 两种模式）+ AI 生成本体。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    RelationDataMapping,
    RelationInstance,
)
from . import connector_service, datasource_service, llm_service, permission_service, tenant_service


_NAMESPACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,179}$")
_API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_STORAGE_KINDS = {"foreign_key", "join_table", "object_backed", "none"}
_PROPERTY_TYPES = {
    "string",
    "text",
    "integer",
    "float",
    "number",
    "boolean",
    "date",
    "datetime",
    "json",
}
_CONSTRAINT_KEYS = {
    "const",
    "minimum",
    "maximum",
    "exclusive_minimum",
    "exclusive_maximum",
    "min_length",
    "max_length",
    "pattern",
    "format",
}
_RELATION_CONSTRAINT_KEYS = {
    "symmetric",
    "transitive",
    "irreflexive",
    "asymmetric",
    "antisymmetric",
    "acyclic",
    "inverse_relation_id",
    "source_min_cardinality",
    "source_max_cardinality",
    "target_min_cardinality",
    "target_max_cardinality",
}
_RELATION_BOOLEAN_CONSTRAINTS = {
    "symmetric", "transitive", "irreflexive", "asymmetric", "antisymmetric", "acyclic",
}
_RELATION_CARDINALITY_CONSTRAINTS = {
    "source_min_cardinality", "source_max_cardinality",
    "target_min_cardinality", "target_max_cardinality",
}
_TRANSFORM_OPS = {
    "trim",
    "lower",
    "upper",
    "default",
    "replace",
    "to_string",
    "to_integer",
    "to_float",
    "to_boolean",
}


def validate_namespace(value: str, *, default: str = "default") -> str:
    namespace = str(value or default).strip()
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("命名空间必须以字母开头，且只能包含字母、数字、点、横线和下划线")
    return namespace


def normalize_api_name(
    value: Any = "",
    *,
    display_name: Any = "",
    prefix: str = "resource",
    stable_key: Any = "",
) -> str:
    """Return one canonical, code-safe and deterministic ontology API name.

    Display names are allowed to be Chinese and to change over time.  An
    explicitly supplied API name or compiler key is preferred; otherwise an
    ASCII display name is slugged and non-ASCII-only names receive a stable
    digest.  The result deliberately has a small SQL/SDK-safe vocabulary.
    """

    clean_prefix = re.sub(r"[^a-z0-9]+", "_", str(prefix or "resource").lower()).strip("_")
    if not clean_prefix or not clean_prefix[0].isalpha():
        clean_prefix = "resource"
    source = str(value or stable_key or display_name or "").strip()
    # Split camelCase/PascalCase before collapsing punctuation.
    source = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", source)
    candidate = re.sub(r"[^A-Za-z0-9]+", "_", source).strip("_").lower()
    if not candidate:
        digest_source = str(stable_key or display_name or value or clean_prefix)
        candidate = f"{clean_prefix}_{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()[:12]}"
    if not candidate[0].isalpha():
        candidate = f"{clean_prefix}_{candidate}"
    if len(candidate) > 100:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
        candidate = f"{candidate[:89].rstrip('_')}_{digest}"
    if not _API_NAME_RE.fullmatch(candidate):
        raise ValueError("api_name 必须以小写字母开头，且只能包含小写字母、数字和下划线")
    return candidate


def reserve_api_name(
    used: set[str],
    value: Any = "",
    *,
    display_name: Any = "",
    prefix: str = "resource",
    stable_key: Any = "",
    explicit: bool | None = None,
) -> str:
    """Reserve a deterministic API name in an in-memory compilation scope."""

    requested = bool(str(value or "").strip()) if explicit is None else explicit
    candidate = normalize_api_name(
        value,
        display_name=display_name,
        prefix=prefix,
        stable_key=stable_key,
    )
    if candidate not in used:
        used.add(candidate)
        return candidate
    if requested:
        raise ValueError(f"api_name 已存在：{candidate}")
    seed = str(stable_key or display_name or candidate)
    for counter in range(1, 10_000):
        suffix = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).hexdigest()[:8]
        alternative = f"{candidate[:91].rstrip('_')}_{suffix}"
        if alternative not in used:
            used.add(alternative)
            return alternative
    raise ValueError("无法生成唯一 api_name")


def allocate_resource_api_name(
    db: Session,
    model: Any,
    *,
    scope_field: str,
    scope_id: str,
    value: Any = "",
    display_name: Any = "",
    prefix: str = "resource",
    stable_key: Any = "",
    current: Any = "",
    resource_id: str | None = None,
) -> str:
    """Allocate a scoped API name and enforce immutability on updates."""

    current_value = str(current or "").strip()
    requested_value = str(value or "").strip()
    if current_value:
        canonical_current = normalize_api_name(
            current_value,
            display_name=display_name,
            prefix=prefix,
            stable_key=stable_key or resource_id or current_value,
        )
        if requested_value:
            proposed = normalize_api_name(
                requested_value,
                display_name=display_name,
                prefix=prefix,
                stable_key=stable_key or resource_id or requested_value,
            )
            if proposed != canonical_current:
                raise ValueError("api_name 是稳定标识，创建后不能修改")
        candidate = canonical_current
        explicit = True
    else:
        candidate = normalize_api_name(
            requested_value,
            display_name=display_name,
            prefix=prefix,
            stable_key=stable_key or resource_id,
        )
        explicit = bool(requested_value)

    def conflict(api_name: str) -> bool:
        statement = select(model.id).where(
            getattr(model, scope_field) == scope_id,
            model.api_name == api_name,
        )
        if resource_id:
            statement = statement.where(model.id != resource_id)
        return db.execute(statement.limit(1)).scalar_one_or_none() is not None

    if not conflict(candidate):
        return candidate
    if explicit:
        raise ValueError(f"api_name 已存在：{candidate}")
    seed = str(stable_key or resource_id or display_name or candidate)
    for counter in range(1, 10_000):
        suffix = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).hexdigest()[:8]
        alternative = f"{candidate[:91].rstrip('_')}_{suffix}"
        if not conflict(alternative):
            return alternative
    raise ValueError("无法生成唯一 api_name")


def _stable_side_api_name(
    current: Any,
    requested: Any,
    *,
    display_name: str,
    prefix: str,
    stable_key: str,
) -> str:
    current_value = str(current or "").strip()
    requested_value = str(requested or "").strip()
    if current_value:
        canonical = normalize_api_name(
            current_value,
            display_name=display_name,
            prefix=prefix,
            stable_key=stable_key,
        )
        if requested_value and normalize_api_name(
            requested_value,
            display_name=display_name,
            prefix=prefix,
            stable_key=stable_key,
        ) != canonical:
            raise ValueError("关系端 api_name 是稳定标识，创建后不能修改")
        return canonical
    return normalize_api_name(
        requested_value,
        display_name=display_name,
        prefix=prefix,
        stable_key=stable_key,
    )


def normalize_relation_navigation(
    *,
    relation_name: Any,
    relation_api_name: str,
    source_display_name: Any = "",
    source_api_name: Any = "",
    target_display_name: Any = "",
    target_api_name: Any = "",
    current: Any = None,
) -> dict[str, str]:
    """Normalize the two independently navigable sides of a link type."""

    name = str(relation_name or "关系").strip() or "关系"

    def existing(field: str) -> str:
        if current is None:
            return ""
        if isinstance(current, dict):
            return str(current.get(field) or "").strip()
        return str(getattr(current, field, "") or "").strip()

    source_display = (
        str(source_display_name or "").strip()
        or existing("source_display_name")
        or name
    )[:200]
    reverse_default = f"{name[:196]}（反向）"
    target_display = (
        str(target_display_name or "").strip()
        or existing("target_display_name")
        or reverse_default
    )[:200]
    source_api = _stable_side_api_name(
        existing("source_api_name"),
        source_api_name,
        display_name=source_display,
        prefix="link",
        stable_key=relation_api_name,
    )
    target_api = _stable_side_api_name(
        existing("target_api_name"),
        target_api_name,
        display_name=target_display,
        prefix="inverse_link",
        stable_key=f"inverse_{relation_api_name}",
    )
    if source_api == target_api:
        if existing("target_api_name") or str(target_api_name or "").strip():
            raise ValueError("关系两端的 api_name 不能相同")
        target_api = normalize_api_name(
            f"inverse_{target_api}", prefix="inverse_link", stable_key=relation_api_name
        )
    return {
        "source_display_name": source_display,
        "source_api_name": source_api,
        "target_display_name": target_display,
        "target_api_name": target_api,
    }


def normalize_relation_storage_kind(value: Any, *, current: Any = "") -> str:
    candidate = str(value or current or "none").strip().lower()
    if candidate not in _STORAGE_KINDS:
        raise ValueError(
            "storage_kind 必须为 foreign_key、join_table、object_backed 或 none"
        )
    return candidate


def normalize_relation_constraints(
    constraints: dict[str, Any] | None,
    *,
    relation_type: str = "N:M",
) -> dict[str, Any]:
    """Validate the closed relation-axiom vocabulary and remove empty defaults."""
    if not isinstance(constraints or {}, dict):
        raise ValueError("关系约束必须是对象")
    unknown = sorted(set(constraints or {}) - _RELATION_CONSTRAINT_KEYS)
    if unknown:
        raise ValueError(f"关系约束包含不支持的字段：{'、'.join(unknown)}")
    normalized: dict[str, Any] = {}
    for key in _RELATION_BOOLEAN_CONSTRAINTS:
        value = (constraints or {}).get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"关系约束 {key} 必须是布尔值")
        if value:
            normalized[key] = True
    for key in _RELATION_CARDINALITY_CONSTRAINTS:
        value = (constraints or {}).get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"关系约束 {key} 必须是大于等于 0 的整数")
        normalized[key] = value
    inverse_relation_id = str((constraints or {}).get("inverse_relation_id") or "").strip()
    if inverse_relation_id:
        if len(inverse_relation_id) > 32:
            raise ValueError("逆关系标识长度不能超过 32 个字符")
        normalized["inverse_relation_id"] = inverse_relation_id

    for side in ("source", "target"):
        minimum = normalized.get(f"{side}_min_cardinality")
        maximum = normalized.get(f"{side}_max_cardinality")
        if minimum is not None and maximum is not None and minimum > maximum:
            label = "源对象" if side == "source" else "目标对象"
            raise ValueError(f"{label}最小基数不能大于最大基数")

    relation_type = str(relation_type or "N:M").strip().upper()
    if relation_type not in {"1:1", "1:N", "N:1", "N:M"}:
        raise ValueError("关系基数必须为 1:1、1:N、N:1 或 N:M")
    implicit_source_max = 1 if relation_type in {"1:1", "N:1"} else None
    implicit_target_max = 1 if relation_type in {"1:1", "1:N"} else None
    for side, implicit_max in (("source", implicit_source_max), ("target", implicit_target_max)):
        minimum = normalized.get(f"{side}_min_cardinality")
        if implicit_max is not None and minimum is not None and minimum > implicit_max:
            label = "源对象" if side == "source" else "目标对象"
            raise ValueError(f"{label}最小基数与关系基数 {relation_type} 冲突")
    if normalized.get("symmetric") and normalized.get("asymmetric"):
        raise ValueError("同一关系不能同时设置为对称和非对称")
    if normalized.get("symmetric") and normalized.get("antisymmetric"):
        raise ValueError("同一关系不能同时设置为对称和反对称")
    if normalized.get("asymmetric") or normalized.get("acyclic"):
        normalized["irreflexive"] = True
    return normalized


def validate_relation_constraint_endpoints(
    constraints: dict[str, Any],
    *,
    source_entity_id: str,
    target_entity_id: str,
) -> None:
    # Symmetric edges cannot be represented by one typed relation when its
    # source/range differ, and the current transitive runtime also requires a
    # chainable common endpoint type. Asymmetric, antisymmetric and acyclic
    # properties have no such OWL domain/range restriction. With disjoint
    # runtime endpoint types their reverse/cycle cases are simply impossible,
    # so rejecting those valid axioms would create a false compiler blocker.
    same_type_axioms = ("symmetric", "transitive")
    if source_entity_id != target_entity_id and any(constraints.get(key) for key in same_type_axioms):
        raise ValueError("对称和传递约束要求源/目标为同一对象类型")


def effective_relation_cardinality_limits(
    relation_type: str,
    constraints: dict[str, Any] | None,
) -> dict[str, int | None]:
    normalized = normalize_relation_constraints(constraints, relation_type=relation_type)
    relation_type = str(relation_type or "N:M").upper()
    implicit_source = 1 if relation_type in {"1:1", "N:1"} else None
    implicit_target = 1 if relation_type in {"1:1", "1:N"} else None

    def stricter(explicit: int | None, implicit: int | None) -> int | None:
        if explicit is None:
            return implicit
        if implicit is None:
            return explicit
        return min(explicit, implicit)

    return {
        "source_min": normalized.get("source_min_cardinality"),
        "source_max": stricter(normalized.get("source_max_cardinality"), implicit_source),
        "target_min": normalized.get("target_min_cardinality"),
        "target_max": stricter(normalized.get("target_max_cardinality"), implicit_target),
    }


def validate_inverse_relation(
    db: Session,
    *,
    scenario_id: str,
    relation_id: str | None,
    source_entity_id: str,
    target_entity_id: str,
    constraints: dict[str, Any],
) -> None:
    inverse_id = str(constraints.get("inverse_relation_id") or "")
    if not inverse_id:
        return
    if relation_id and inverse_id == relation_id:
        if source_entity_id != target_entity_id:
            raise ValueError("关系只能在源/目标对象类型相同时把自身声明为逆关系")
        return
    inverse = db.get(OntologyRelation, inverse_id)
    if not inverse or inverse.scenario_id != scenario_id:
        raise ValueError("逆关系不属于当前业务场景")
    if (
        inverse.source_entity_id != target_entity_id
        or inverse.target_entity_id != source_entity_id
    ):
        raise ValueError("逆关系的源/目标对象类型必须与当前关系相反")


def inverse_relation_dependents(
    db: Session,
    *,
    scenario_id: str,
    relation_id: str,
) -> list[OntologyRelation]:
    """Return definitions that explicitly name this relation as inverse."""
    relations = db.execute(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario_id)
    ).scalars().all()
    return [
        relation
        for relation in relations
        if str(
            (relation.constraints if isinstance(relation.constraints, dict) else {})
            .get("inverse_relation_id")
            or ""
        ) == relation_id
    ]


def validate_inverse_relation_dependents(
    db: Session,
    *,
    scenario_id: str,
    relation_id: str,
    source_entity_id: str,
    target_entity_id: str,
) -> None:
    for dependent in inverse_relation_dependents(
        db, scenario_id=scenario_id, relation_id=relation_id
    ):
        if dependent.id == relation_id:
            continue
        if (
            dependent.source_entity_id != target_entity_id
            or dependent.target_entity_id != source_entity_id
        ):
            raise ValueError(
                f"关系“{dependent.name}”把当前关系声明为逆关系；修改后的源/目标不再反向对应"
            )


def _relation_edges(db: Session, relation_id: str) -> list[tuple[str, str]]:
    return [
        (str(source), str(target))
        for source, target in db.execute(
            select(RelationInstance.source_instance_id, RelationInstance.target_instance_id)
            .where(RelationInstance.relation_id == relation_id)
        ).all()
    ]


def validate_relation_instance_create(
    db: Session,
    relation: OntologyRelation,
    *,
    source_instance_id: str,
    target_instance_id: str,
) -> None:
    """Fail closed on every locally decidable asserted-edge invariant."""
    constraints = normalize_relation_constraints(
        relation.constraints or {}, relation_type=relation.relation_type
    )
    edges = _relation_edges(db, relation.id)
    if (source_instance_id, target_instance_id) in edges:
        raise ValueError("相同的关系实例已经存在")
    if constraints.get("irreflexive") and source_instance_id == target_instance_id:
        raise ValueError("反自反关系不能连接对象自身")
    reverse_exists = (target_instance_id, source_instance_id) in edges
    if constraints.get("asymmetric") and reverse_exists:
        raise ValueError("非对称关系不能同时存在反向关系实例")
    if (
        constraints.get("antisymmetric")
        and source_instance_id != target_instance_id
        and reverse_exists
    ):
        raise ValueError("反对称关系不能在两个不同对象间同时存在双向关系实例")
    limits = effective_relation_cardinality_limits(relation.relation_type, constraints)
    outgoing = sum(source == source_instance_id for source, _target in edges)
    incoming = sum(target == target_instance_id for _source, target in edges)
    if limits["source_max"] is not None and outgoing + 1 > limits["source_max"]:
        raise ValueError("源对象已达到该关系允许的最大目标基数")
    if limits["target_max"] is not None and incoming + 1 > limits["target_max"]:
        raise ValueError("目标对象已达到该关系允许的最大来源基数")
    # Transitive + irreflexive entails acyclic: any asserted cycle would make
    # the transitive closure contain a forbidden self edge.
    if constraints.get("acyclic") or (
        constraints.get("transitive") and constraints.get("irreflexive")
    ):
        adjacency: dict[str, set[str]] = {}
        for source, target in edges:
            adjacency.setdefault(source, set()).add(target)
        pending = [target_instance_id]
        visited: set[str] = set()
        while pending:
            node = pending.pop()
            if node == source_instance_id:
                raise ValueError("新增关系实例会形成违反无环/传递反自反约束的环")
            if node in visited:
                continue
            visited.add(node)
            pending.extend(adjacency.get(node, ()))


def validate_existing_relation_graph(
    db: Session,
    relation: OntologyRelation,
    *,
    constraints: dict[str, Any],
    relation_type: str,
) -> None:
    """Reject an edited definition that would relabel existing asserted data as valid."""
    normalized = normalize_relation_constraints(constraints, relation_type=relation_type)
    edges = _relation_edges(db, relation.id)
    edge_set = set(edges)
    if len(edge_set) != len(edges):
        raise ValueError("已有关系实例包含重复边，不能应用新的关系约束")
    if normalized.get("irreflexive") and any(source == target for source, target in edges):
        raise ValueError("已有自连接关系实例违反反自反约束")
    for source, target in edge_set:
        if normalized.get("asymmetric") and (target, source) in edge_set:
            raise ValueError("已有双向关系实例违反非对称约束")
        if normalized.get("antisymmetric") and source != target and (target, source) in edge_set:
            raise ValueError("已有双向关系实例违反反对称约束")
    limits = effective_relation_cardinality_limits(relation_type, normalized)
    outgoing: dict[str, int] = {}
    incoming: dict[str, int] = {}
    for source, target in edges:
        outgoing[source] = outgoing.get(source, 0) + 1
        incoming[target] = incoming.get(target, 0) + 1
    if limits["source_max"] is not None and any(
        count > limits["source_max"] for count in outgoing.values()
    ):
        raise ValueError("已有关系实例超过源对象最大目标基数")
    if limits["target_max"] is not None and any(
        count > limits["target_max"] for count in incoming.values()
    ):
        raise ValueError("已有关系实例超过目标对象最大来源基数")
    if normalized.get("acyclic") or (
        normalized.get("transitive") and normalized.get("irreflexive")
    ):
        adjacency: dict[str, set[str]] = {}
        for source, target in edges:
            adjacency.setdefault(source, set()).add(target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(target) for target in adjacency.get(node, ())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if any(visit(node) for node in list(adjacency)):
            raise ValueError("已有关系实例包含环，不能启用无环或传递反自反约束")


def validate_relation_instance_delete(
    db: Session,
    relation: OntologyRelation,
    instance: RelationInstance,
) -> None:
    constraints = normalize_relation_constraints(
        relation.constraints or {}, relation_type=relation.relation_type
    )
    limits = effective_relation_cardinality_limits(relation.relation_type, constraints)
    edges = [
        (str(source), str(target))
        for source, target in db.execute(
            select(RelationInstance.source_instance_id, RelationInstance.target_instance_id)
            .where(
                RelationInstance.relation_id == relation.id,
                RelationInstance.id != instance.id,
            )
        ).all()
    ]
    outgoing = sum(source == instance.source_instance_id for source, _target in edges)
    incoming = sum(target == instance.target_instance_id for _source, target in edges)
    if limits["source_min"] is not None and outgoing < limits["source_min"]:
        raise ValueError("删除后源对象将低于该关系要求的最小目标基数")
    if limits["target_min"] is not None and incoming < limits["target_min"]:
        raise ValueError("删除后目标对象将低于该关系要求的最小来源基数")


def relation_query_semantics(
    constraints: dict[str, Any] | None,
    *,
    inverse_relation_name: str = "",
) -> list[str]:
    """Describe logical query expansion without claiming inferred rows exist."""
    normalized = normalize_relation_constraints(constraints or {})
    semantics = [
        "普通关系默认可从源端和目标端双向遍历同一条已断言边；无需为反向浏览另建关系",
        "存储与关系实例列表只包含已断言边，不自动物化推理边",
    ]
    if normalized.get("symmetric"):
        semantics.append("查询可按对称语义解释反向边，但不会自动创建反向关系实例")
    if normalized.get("transitive"):
        semantics.append("查询可按传递语义解释可达路径，但不会存储传递闭包")
    if normalized.get("inverse_relation_id"):
        label = inverse_relation_name or normalized["inverse_relation_id"]
        semantics.append(f"查询可将反向边解释为逆关系“{label}”，但不会自动创建对应实例")
    return semantics


def normalize_property_constraints(
    data_type: str,
    constraints: dict[str, Any] | None,
) -> dict[str, Any]:
    kind = str(data_type or "string").strip().lower()
    if kind not in _PROPERTY_TYPES:
        raise ValueError(f"不支持的属性类型：{kind}")
    if not isinstance(constraints or {}, dict):
        raise ValueError("属性约束必须是对象")
    unknown = sorted(set(constraints or {}) - _CONSTRAINT_KEYS)
    if unknown:
        raise ValueError(f"属性约束包含不支持的字段：{'、'.join(unknown)}")
    numeric_keys = {"const", "minimum", "maximum", "exclusive_minimum", "exclusive_maximum"}
    text_keys = {"const", "min_length", "max_length", "pattern", "format"}
    incompatible = (
        (set(constraints or {}) - numeric_keys)
        if kind in {"integer", "float", "number"}
        else (set(constraints or {}) - text_keys)
        if kind in {"string", "text", "date", "datetime"}
        else (set(constraints or {}) - {"const"})
        if kind in {"boolean", "json"}
        else set(constraints or {})
    )
    if incompatible:
        raise ValueError(
            f"{kind} 类型不支持约束：{'、'.join(sorted(incompatible))}"
        )
    result: dict[str, Any] = {}
    for key, value in (constraints or {}).items():
        if key == "const":
            valid_const = (
                isinstance(value, str)
                if kind in {"string", "text", "date", "datetime"}
                else isinstance(value, int) and not isinstance(value, bool)
                if kind == "integer"
                else isinstance(value, (int, float)) and not isinstance(value, bool)
                if kind in {"float", "number"}
                else isinstance(value, bool)
                if kind == "boolean"
                else isinstance(value, (dict, list))
                if kind == "json"
                else False
            )
            if not valid_const:
                raise ValueError(f"属性约束 const 必须符合 {kind} 类型")
        elif key in {"minimum", "maximum", "exclusive_minimum", "exclusive_maximum"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"属性约束 {key} 必须是数字")
        elif key in {"min_length", "max_length"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000:
                raise ValueError(f"属性约束 {key} 必须是 0 到 100000 的整数")
        elif key == "pattern":
            value = str(value)
            if (
                len(value) > 200
                or "(?" in value
                or re.search(r"\\[1-9]", value)
                # Python's stdlib regex engine has no match timeout.  Repeated
                # groups are therefore rejected at definition time rather than
                # trying to distinguish every catastrophic-backtracking shape
                # (for example ``(a?)+`` or ``(a|aa)+``).  Ordinary, unquantified
                # groups and quantified character classes remain available.
                or re.search(r"\)(?:[+*?]|\{\d+(?:,\d*)?\})", value)
                or ".*" in value
                or ".+" in value
            ):
                raise ValueError("pattern 过于复杂；不允许回溯型断言、反向引用、量化分组或无界通配符")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("pattern 不是有效的正则表达式") from exc
        elif key == "format":
            value = str(value)
            if value not in {"email", "uri", "uuid", "date", "date-time"}:
                raise ValueError("format 只能是 email、uri、uuid、date 或 date-time")
        result[key] = value
    if (
        "minimum" in result
        and "maximum" in result
        and result["minimum"] > result["maximum"]
    ):
        raise ValueError("minimum 不能大于 maximum")
    if (
        "min_length" in result
        and "max_length" in result
        and result["min_length"] > result["max_length"]
    ):
        raise ValueError("min_length 不能大于 max_length")
    return result


def validate_entity_definition(payload: Any, *, scenario_namespace: str = "default") -> None:
    namespace = str(getattr(payload, "namespace", "") or scenario_namespace)
    validate_namespace(namespace)
    entity_api_name = str(getattr(payload, "api_name", "") or "").strip()
    if entity_api_name:
        payload.api_name = normalize_api_name(
            entity_api_name,
            display_name=getattr(payload, "name", ""),
            prefix="entity",
        )
    properties = list(getattr(payload, "properties", []) or [])
    names = [str(prop.name).strip() for prop in properties]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("实体属性名不能为空或重复")
    property_api_names: list[str] = []
    for prop in properties:
        api_name = str(getattr(prop, "api_name", "") or "").strip()
        if not api_name:
            continue
        prop.api_name = normalize_api_name(
            api_name,
            display_name=prop.name,
            prefix="property",
        )
        property_api_names.append(prop.api_name)
    if len(property_api_names) != len(set(property_api_names)):
        raise ValueError("同一对象类型中的属性 api_name 不能重复")
    key_count = sum(1 for prop in properties if bool(prop.is_key))
    if key_count > 1:
        raise ValueError("一个实体最多只能有一个主键属性")
    title_count = sum(1 for prop in properties if bool(getattr(prop, "is_title", False)))
    if title_count > 1:
        raise ValueError("一个对象类型最多只能有一个标题属性")
    for prop in properties:
        prop.data_type = str(prop.data_type or "string").strip().lower()
        prop.constraints = normalize_property_constraints(prop.data_type, prop.constraints)
        values = [str(value) for value in (prop.enum_values or [])]
        if prop.is_enum and not values:
            raise ValueError(f"枚举属性“{prop.name}”必须提供至少一个枚举值")
        if len(values) != len(set(values)):
            raise ValueError(f"枚举属性“{prop.name}”包含重复枚举值")
        prop.default_value = normalize_property_default(prop)
    state_property = str(getattr(payload, "state_property", "") or "").strip()
    if state_property:
        candidate = next((prop for prop in properties if prop.name == state_property), None)
        if candidate is None:
            raise ValueError("状态属性必须引用当前实体中的属性")
        if not candidate.is_enum:
            raise ValueError("状态属性必须配置为枚举，才能形成稳定生命周期")


def entity_definition_issues(entity: Any) -> list[str]:
    """Return publication-readiness issues while keeping draft CRUD compatible."""
    if bool(getattr(entity, "is_abstract", False)):
        return []
    properties = list(getattr(entity, "properties", []) or [])
    key_count = sum(bool(getattr(prop, "is_key", False)) for prop in properties)
    title_count = sum(bool(getattr(prop, "is_title", False)) for prop in properties)
    issues: list[str] = []
    if key_count != 1:
        issues.append("具体对象类型必须且只能有一个主键属性")
    if title_count != 1:
        issues.append("具体对象类型必须且只能有一个标题属性")
    return issues


def _validate_property_value(
    prop: Any,
    value: Any,
    *,
    strict_type: bool = True,
) -> None:
    if value is None:
        if bool(getattr(prop, "is_required", False)):
            raise ValueError(f"必填属性“{prop.name}”不能为空")
        return
    kind = str(prop.data_type or "string").lower()
    valid = True
    if kind in {"string", "text", "date", "datetime"}:
        valid = isinstance(value, str) or (
            kind == "date" and isinstance(value, date)
        ) or (kind == "datetime" and isinstance(value, datetime))
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind in {"float", "number"}:
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "json":
        valid = isinstance(value, (dict, list))
    constraints = normalize_property_constraints(
        kind, getattr(prop, "constraints", {}) or {}
    )
    enforce_type = strict_type or bool(constraints) or bool(
        getattr(prop, "is_enum", False)
    )
    if not valid and enforce_type:
        raise ValueError(f"属性“{prop.name}”的值不符合 {kind} 类型")
    if not valid:
        return
    if "const" in constraints and value != constraints["const"]:
        raise ValueError(f"属性“{prop.name}”必须等于固定值")
    if bool(getattr(prop, "is_enum", False)) and str(value) not in {
        str(item) for item in (getattr(prop, "enum_values", []) or [])
    }:
        raise ValueError(f"属性“{prop.name}”不在允许的枚举范围内")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in constraints and value < constraints["minimum"]:
            raise ValueError(f"属性“{prop.name}”小于最小值")
        if "maximum" in constraints and value > constraints["maximum"]:
            raise ValueError(f"属性“{prop.name}”大于最大值")
        if "exclusive_minimum" in constraints and value <= constraints["exclusive_minimum"]:
            raise ValueError(f"属性“{prop.name}”必须大于约束值")
        if "exclusive_maximum" in constraints and value >= constraints["exclusive_maximum"]:
            raise ValueError(f"属性“{prop.name}”必须小于约束值")
    if isinstance(value, str):
        if "min_length" in constraints and len(value) < constraints["min_length"]:
            raise ValueError(f"属性“{prop.name}”长度不足")
        if "max_length" in constraints and len(value) > constraints["max_length"]:
            raise ValueError(f"属性“{prop.name}”长度超限")
        if "pattern" in constraints and re.fullmatch(constraints["pattern"], value) is None:
            raise ValueError(f"属性“{prop.name}”格式不匹配")
        expected_format = constraints.get("format")
        format_valid = True
        if expected_format == "email":
            format_valid = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
        elif expected_format == "uri":
            parsed = urlparse(value)
            format_valid = bool(parsed.scheme and (parsed.netloc or parsed.path))
        elif expected_format == "uuid":
            try:
                UUID(value)
            except (ValueError, AttributeError):
                format_valid = False
        elif expected_format == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                format_valid = False
        elif expected_format == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                format_valid = False
        if not format_valid:
            raise ValueError(f"属性“{prop.name}”不符合 {expected_format} 格式")


def normalize_property_default(prop: Any) -> Any:
    """Coerce legacy text defaults once, then validate the typed JSON value."""
    value = getattr(prop, "default_value", "")
    if value is None or value == "":
        return value
    kind = str(getattr(prop, "data_type", "string") or "string").lower()
    try:
        if isinstance(value, str):
            token = value.strip()
            if kind == "integer":
                value = int(token)
            elif kind in {"float", "number"}:
                value = float(token)
            elif kind == "boolean":
                lowered = token.lower()
                if lowered in {"true", "1", "yes"}:
                    value = True
                elif lowered in {"false", "0", "no"}:
                    value = False
                else:
                    raise ValueError
            elif kind == "json":
                value = json.loads(token)
        _validate_property_value(prop, value, strict_type=True)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"属性“{prop.name}”的默认值不符合 {kind} 类型或约束") from exc
    return value


def normalize_quality(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value or {}, dict):
        raise ValueError("质量信息必须是对象")
    allowed = {"score", "status", "issues", "checked_at", "source"}
    unknown = sorted(set(value or {}) - allowed)
    if unknown:
        raise ValueError(f"质量信息包含不支持的字段：{'、'.join(unknown)}")
    result = dict(value or {})
    if "score" in result:
        score = result["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError("质量分数必须在 0 到 1 之间")
        result["score"] = float(score)
    status = str(result.get("status") or "unknown")
    if status not in {"unknown", "valid", "warning", "invalid"}:
        raise ValueError("质量状态不合法")
    result["status"] = status
    issues = result.get("issues") or []
    if not isinstance(issues, list) or len(issues) > 100:
        raise ValueError("质量问题必须是不超过 100 项的列表")
    result["issues"] = [str(item)[:500] for item in issues]
    for key in ("checked_at", "source"):
        if key in result:
            result[key] = str(result[key])[:200]
    return result


def validate_instance_payload(
    entity: OntologyEntity,
    attributes: dict[str, Any],
    *,
    state: str = "",
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    quality: dict[str, Any] | None = None,
    strict_types: bool = True,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    values = dict(attributes or {})
    for prop in entity.properties:
        default_value = getattr(prop, "default_value", "")
        if prop.name not in values and default_value not in (None, ""):
            values[prop.name] = default_value
        if prop.name not in values:
            if bool(getattr(prop, "is_required", False)):
                raise ValueError(f"缺少必填属性“{prop.name}”")
            continue
        _validate_property_value(
            prop,
            values[prop.name],
            strict_type=strict_types,
        )
    resolved_state = str(state or "").strip()
    state_property = str(getattr(entity, "state_property", "") or "")
    if state_property:
        state_prop = next(
            (prop for prop in entity.properties if prop.name == state_property),
            None,
        )
        attribute_state = values.get(state_property)
        if not resolved_state and attribute_state is not None:
            resolved_state = str(attribute_state)
        if (
            state_prop
            and resolved_state
            and resolved_state
            not in {str(item) for item in (getattr(state_prop, "enum_values", []) or [])}
        ):
            raise ValueError("对象状态不在实体生命周期枚举中")
        if attribute_state is not None and resolved_state != str(attribute_state):
            raise ValueError("对象状态必须与实体状态属性保持一致")
    if valid_from and valid_to:
        try:
            invalid_window = valid_to <= valid_from
        except TypeError as exc:
            raise ValueError("有效期起止时间的时区格式不一致") from exc
        if invalid_window:
            raise ValueError("valid_to 必须晚于 valid_from")
    return values, resolved_state[:120], normalize_quality(quality)


def resolve_instance_display_name(
    entity: Any,
    attributes: dict[str, Any] | None,
    *,
    explicit_name: Any = None,
    fallback: Any = None,
) -> str:
    """Resolve the one server-owned display label for an object instance.

    A title value is authoritative whenever it is present. Numeric zero and
    ``False`` are legitimate business labels, so truthiness must never decide
    whether to keep them. Draft/legacy object types remain compatible by
    retaining an explicit name first and then falling back to the primary key.
    """

    values = attributes if isinstance(attributes, dict) else {}

    def readable(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            return value
        return str(value)

    properties = list(getattr(entity, "properties", []) or [])
    title_property = next(
        (prop for prop in properties if bool(getattr(prop, "is_title", False))),
        None,
    )
    key_property = next(
        (prop for prop in properties if bool(getattr(prop, "is_key", False))),
        None,
    )
    for candidate in (
        values.get(title_property.name) if title_property is not None else None,
        explicit_name,
        values.get(key_property.name) if key_property is not None else None,
        fallback,
    ):
        resolved = readable(candidate)
        if resolved is not None:
            return resolved[:300]
    return str(getattr(entity, "name", "") or "对象")[:300]


def instance_in_runtime_environment(instance: Any, environment: str) -> bool:
    """Keep imported runtime rows isolated while manual scenario facts stay shared."""

    if instance is None:
        return False
    if str(getattr(instance, "source", "manual") or "manual") != "imported":
        return True
    metadata = (
        instance.source_metadata
        if isinstance(getattr(instance, "source_metadata", None), dict)
        else {}
    )
    instance_environment = str(
        metadata.get("runtime_environment") or "dev"
    ).strip().lower() or "dev"
    expected_environment = str(environment or "dev").strip().lower() or "dev"
    return instance_environment == expected_environment


def _runtime_provenance_matches(
    metadata: dict[str, Any],
    definition: Any,
) -> bool:
    """Match one generated fact to the definition currently serving reads.

    Development data is tied to the live resource ids.  A deployed fact also
    has to prove the immutable release that produced it; accepting only the
    environment would let rows from a superseded release leak into a newer
    deployment.
    """

    if definition is None:
        return False
    expected_environment = str(
        getattr(definition, "environment", "") or ""
    ).strip().lower()
    actual_environment = str(
        metadata.get("runtime_environment") or "dev"
    ).strip().lower() or "dev"
    if not expected_environment or actual_environment != expected_environment:
        return False
    if expected_environment == "dev":
        return True
    if str(getattr(definition, "source", "") or "") != "release":
        return False
    expected = {
        "definition_snapshot_id": getattr(definition, "snapshot_id", None),
        "release_id": getattr(definition, "release_id", None),
        "definition_hash": getattr(definition, "definition_hash", None),
    }
    if str(metadata.get("definition_source") or "") != "release":
        return False
    return all(
        bool(value) and str(metadata.get(key) or "") == str(value)
        for key, value in expected.items()
    )


def _fact_belongs_to_runtime_scenario(fact: Any, definition: Any) -> bool:
    scenario = getattr(definition, "scenario", None)
    scenario_id = str(getattr(scenario, "id", "") or "")
    return not scenario_id or str(getattr(fact, "scenario_id", "") or "") == scenario_id


def entity_is_active(entity: Any) -> bool:
    """Return whether an Object Type participates in the active ontology."""

    return str(getattr(entity, "lifecycle_status", "active") or "active") == "active"


def _runtime_collection_ids(definition: Any, collection: str) -> set[str] | None:
    """Return resolved resource IDs, or ``None`` for old lightweight test DTOs."""

    if definition is None or not hasattr(definition, collection):
        return None
    resources = getattr(definition, collection, {}) or {}
    return {str(resource_id) for resource_id in resources}


def instance_in_runtime_definition(instance: Any, definition: Any) -> bool:
    """Return whether an object fact belongs to the current runtime definition.

    Manual objects remain scenario facts. Imported objects must name an object
    mapping in the resolved definition and, outside dev, carry its exact
    immutable release provenance.
    """

    if instance is None or not _fact_belongs_to_runtime_scenario(instance, definition):
        return False
    entity_ids = _runtime_collection_ids(definition, "entities")
    if entity_ids is not None and str(getattr(instance, "entity_id", "") or "") not in entity_ids:
        return False
    if str(getattr(instance, "source", "manual") or "manual") != "imported":
        return True
    metadata = (
        instance.source_metadata
        if isinstance(getattr(instance, "source_metadata", None), dict)
        else {}
    )
    mapping_id = str(metadata.get("mapping_id") or "")
    mappings = getattr(definition, "mappings", {}) if definition is not None else {}
    if not mapping_id or mapping_id not in {str(value) for value in (mappings or {})}:
        return False
    return _runtime_provenance_matches(metadata, definition)


def relation_instance_in_runtime_definition(instance: Any, definition: Any) -> bool:
    """Return whether a relationship fact belongs to the runtime definition.

    Only automatically generated links are versioned by relation mappings;
    manual links keep their ordinary scenario semantics.
    """

    if instance is None or not _fact_belongs_to_runtime_scenario(instance, definition):
        return False
    relation_ids = _runtime_collection_ids(definition, "relations")
    if relation_ids is not None and str(getattr(instance, "relation_id", "") or "") not in relation_ids:
        return False
    if str(getattr(instance, "source", "manual") or "manual") != "mapping":
        return True
    metadata = (
        instance.source_metadata
        if isinstance(getattr(instance, "source_metadata", None), dict)
        else {}
    )
    mapping_id = str(metadata.get("relation_mapping_id") or "")
    mappings = (
        getattr(definition, "relation_mappings", {}) if definition is not None else {}
    )
    if not mapping_id or mapping_id not in {str(value) for value in (mappings or {})}:
        return False
    return _runtime_provenance_matches(metadata, definition)


def normalize_transform_rules(
    entity: OntologyEntity,
    rules: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(rules or {}, dict):
        raise ValueError("转换规则必须是对象")
    property_names = {prop.name for prop in entity.properties}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for property_name, raw_rules in (rules or {}).items():
        property_name = str(property_name)
        if property_name not in property_names:
            raise ValueError(f"转换规则引用了不存在的属性“{property_name}”")
        if not isinstance(raw_rules, list) or len(raw_rules) > 20:
            raise ValueError("每个属性的转换规则必须是不超过 20 项的列表")
        items: list[dict[str, Any]] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise ValueError("转换规则项必须是对象")
            op = str(raw.get("op") or "").strip()
            if op not in _TRANSFORM_OPS:
                raise ValueError(f"不支持的声明式转换操作：{op}")
            allowed_keys = {
                "replace": {"op", "old", "new"},
                "default": {"op", "value"},
            }.get(op, {"op"})
            if set(raw) - allowed_keys:
                raise ValueError(f"转换操作 {op} 包含不允许的参数")
            item: dict[str, Any] = {"op": op}
            if op == "replace":
                item["old"] = str(raw.get("old") or "")[:500]
                if not item["old"]:
                    raise ValueError("replace 转换的 old 不能为空")
                item["new"] = str(raw.get("new") or "")[:500]
            elif op == "default":
                item["value"] = raw.get("value")
            items.append(item)
        normalized[property_name] = items
    return normalized


def apply_transform_rules(value: Any, rules: list[dict[str, Any]]) -> Any:
    result = value
    for rule in rules:
        op = rule["op"]
        if op == "default":
            if result is None or result == "":
                result = rule.get("value")
        elif result is None:
            continue
        elif op == "trim":
            result = str(result).strip()
        elif op == "lower":
            result = str(result).lower()
        elif op == "upper":
            result = str(result).upper()
        elif op == "replace":
            result = str(result).replace(str(rule.get("old") or ""), str(rule.get("new") or ""))
        elif op == "to_string":
            result = str(result)
        elif op == "to_integer":
            result = int(result)
        elif op == "to_float":
            result = float(result)
        elif op == "to_boolean":
            if isinstance(result, bool):
                continue
            token = str(result).strip().lower()
            if token in {"1", "true", "yes", "y", "是"}:
                result = True
            elif token in {"0", "false", "no", "n", "否"}:
                result = False
            else:
                raise ValueError(f"值“{result}”不能转换为布尔值")
    return result


# ──────────────────────────────────────────────
# 图谱构建
# ──────────────────────────────────────────────
def build_graph(
    scenario: BusinessScenario,
    mode: str = "schema",
    *,
    db: Session | None = None,
    environment: str | None = None,
    runtime_definition: Any | None = None,
) -> dict[str, Any]:
    """构建图谱数据。

    mode=schema:   节点=实体类型，边=关系类型（本体层）
    mode=instance: 节点=实例，边=关系实例（数据层，按实体着色）
    """
    # 图谱也是对象读取入口；没有可验证主体时不能退化为返回完整实例图。
    if db is None:
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    runtime_entity_ids = _runtime_collection_ids(runtime_definition, "entities")
    runtime_relation_ids = _runtime_collection_ids(runtime_definition, "relations")
    visible_entities = [
        entity
        for entity in scenario.entities
        if (
            entity.id in runtime_entity_ids
            if runtime_entity_ids is not None
            else entity_is_active(entity)
        )
    ]
    visible_entity_ids = {entity.id for entity in visible_entities}
    visible_relations = [
        relation
        for relation in scenario.relations
        if relation.source_entity_id in visible_entity_ids
        and relation.target_entity_id in visible_entity_ids
        and (
            relation.id in runtime_relation_ids
            if runtime_relation_ids is not None
            else True
        )
    ]

    if mode == "instance":
        inst_map = {
            instance.id: instance
            for instance in scenario.instances
            if (
                instance_in_runtime_definition(instance, runtime_definition)
                if runtime_definition is not None
                else (environment is None or instance_in_runtime_environment(instance, environment))
            )
            and permission_service.check_object(db, instance, "read").allowed
        }
        ent_map = {e.id: e for e in visible_entities}
        for i in inst_map.values():
            ent = ent_map.get(i.entity_id)
            nodes.append(
                {
                    "id": i.id,
                    "kind": "instance",
                    "label": i.name,
                    "entity_id": i.entity_id,
                    "entity_name": ent.name if ent else "",
                    "color": (ent.color if ent else "#64748b"),
                    "size": 14 + min(10, len(i.attributes or {}) * 2),
                    "attrs": permission_service.filter_instance_attributes(db, i),
                }
            )
        rel_map = {r.id: r for r in visible_relations}
        for ri in scenario.relation_instances:
            if runtime_definition is not None and not relation_instance_in_runtime_definition(
                ri, runtime_definition
            ):
                continue
            if ri.source_instance_id not in inst_map or ri.target_instance_id not in inst_map:
                continue
            rel = rel_map.get(ri.relation_id)
            edges.append(
                {
                    "id": ri.id,
                    "source": ri.source_instance_id,
                    "target": ri.target_instance_id,
                    "label": rel.name if rel else "",
                    "relation_type": rel.relation_type if rel else "",
                }
            )
    else:
        for e in visible_entities:
            visible_properties = [
                prop for prop in e.properties if permission_service.can_read_property(db, prop)
            ]
            prop_count = len(visible_properties)
            nodes.append(
                {
                    "id": e.id,
                    "kind": "entity",
                    "label": e.name,
                    "color": e.color or "#6366f1",
                    "abstract": bool(e.is_abstract),
                    "size": 26 + min(26, prop_count * 3),
                    "props": [
                        {"name": p.name, "type": p.data_type, "key": bool(p.is_key)}
                        for p in visible_properties
                    ][:12],
                    "description": e.description or "",
                }
            )
        for r in visible_relations:
            edges.append(
                {
                    "id": r.id,
                    "source": r.source_entity_id,
                    "target": r.target_entity_id,
                    "label": r.name,
                    "relation_type": r.relation_type,
                }
            )

    return {"nodes": nodes, "edges": edges}


def search_instances(
    db: Session,
    scenario: BusinessScenario | None,
    entity_name: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按通用本体语义检索实例，不依赖任何行业字段或表名。"""
    if not scenario:
        return []
    entity_name = (entity_name or "").strip().lower()
    query = (query or "").strip().lower()
    entities = {e.id: e for e in scenario.entities}
    allowed_ids = {
        eid for eid, entity in entities.items()
        if not entity_name or entity_name in entity.name.lower()
    }
    if entity_name and not allowed_ids:
        return []
    rows = db.execute(
        select(OntologyInstance)
        .where(
            OntologyInstance.scenario_id == scenario.id,
            OntologyInstance.entity_id.in_(allowed_ids) if allowed_ids else False,
        )
        .order_by(OntologyInstance.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    results: list[dict[str, Any]] = []
    for instance in rows:
        if not permission_service.check_object(db, instance, "read").allowed:
            continue
        attrs = permission_service.filter_instance_attributes(db, instance)
        haystack = f"{instance.name} {json.dumps(attrs, ensure_ascii=False, default=str)}".lower()
        if query and query not in haystack:
            continue
        entity = entities.get(instance.entity_id)
        results.append(
            {
                "id": instance.id,
                "name": instance.name,
                "entity": entity.name if entity else "",
                "entity_id": instance.entity_id,
                "attributes": attrs,
                "source": instance.source,
                "source_ref": instance.source_ref,
            }
        )
        if len(results) >= max(1, min(int(limit), 200)):
            break
    return results


# ──────────────────────────────────────────────
# AI 生成本体
# ──────────────────────────────────────────────
ONTOLOGY_CONTEXT_MAX_CHARS = 100_000
ONTOLOGY_MAX_OUTPUT_TOKENS = 12_000
ONTOLOGY_PROPERTY_TYPES = {"string", "integer", "float", "boolean", "date", "datetime", "json", "text"}
ONTOLOGY_RELATION_TYPES = {"1:1", "1:N", "N:1", "N:M"}

_GEN_PROMPT = """你是资深业务架构师，擅长为任意行业构建本体（Ontology）模型。
请完整阅读下面的业务描述，设计一套忠实、通用、可扩展的本体模型。输入已在服务端完成完整性边界校验，不要只处理开头部分。

要求：
1. 实体（entities）：覆盖文档明确描述的全部核心业务对象，数量由业务内容决定，不设 8 个上限；不要为了简洁遗漏文档中的稳定业务概念，也不要凭空发明概念。每个实体同时提供稳定、唯一的英文小写 snake_case api_name，中文 name 只作可编辑显示名。
2. 每个实体覆盖文档明确要求的关键属性（properties），数量由业务内容决定。属性名用中文，并提供实体内唯一的英文小写 snake_case api_name；data_type 只能是：string / integer / float / boolean / date / datetime / json / text；需要状态机时使用 is_enum/enum_values，并在实体 state_property 指向该枚举属性。文档明确给出默认值或敏感字段时，分别写入 default_value、is_sensitive；没有依据时不要猜测。
3. 每个具体实体必须恰好有 1 个 is_key=true 的主键属性和 1 个 is_title=true 的标题属性；二者可以是同一属性。主键用于稳定身份，标题用于图谱与 Agent 回答中的可读名称。
4. 关系（relations）：覆盖文档明确描述的实体关系，数量由业务内容决定；relation_type 只能是 1:1 / 1:N / N:1 / N:M。每个关系提供稳定 api_name，以及从源端和目标端导航时各自的 source_display_name/source_api_name、target_display_name/target_api_name；storage_kind 只能是 foreign_key / join_table / object_backed / none，没有物理依据时使用 none。
5. 对名称、枚举、约束或关系方向没有明确依据时采用保守表达，不把数据表或字段机械等同于业务实体。
6. 只输出 JSON，不要输出任何解释文字。

输出格式（严格 JSON）：
{
  "entities": [
    {"name": "业务对象", "api_name": "business_object", "description": "业务领域中的核心对象", "is_abstract": false, "state_property": "",
     "properties": [{"name": "对象ID", "api_name": "object_id", "data_type": "string", "is_key": true, "is_title": false, "is_required": true, "is_enum": false, "enum_values": [], "default_value": "", "constraints": {}, "is_sensitive": false}, {"name": "对象名称", "api_name": "object_name", "data_type": "string", "is_key": false, "is_title": true, "is_required": true, "is_enum": false, "enum_values": [], "default_value": "", "constraints": {}, "is_sensitive": false}, ...]}
  ],
  "relations": [
    {"name": "关联", "api_name": "related_to", "source": "业务对象", "target": "相关对象", "source_display_name": "关联相关对象", "source_api_name": "related_objects", "target_display_name": "被业务对象关联", "target_api_name": "related_from_business_objects", "storage_kind": "none", "relation_type": "1:N", "description": ""}
  ]
}

业务描述：
{description}
"""


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容忍 ```json 包裹、前后杂文与尾随逗号）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 修复常见 LLM JSON 瑕疵：尾随逗号（,} 或 ,]）
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(repaired)


def _ontology_context(description: str) -> str:
    """Return complete bounded input; never hide a tail-truncation from users."""
    context = str(description or "")
    if len(context) > ONTOLOGY_CONTEXT_MAX_CHARS:
        raise ValueError(
            f"本体生成上下文共 {len(context)} 个字符，超过单次生成"
            f" {ONTOLOGY_CONTEXT_MAX_CHARS} 个字符的明确边界；"
            "系统不会静默截断文档，请拆分文档后分批生成并审阅本体草稿"
        )
    return context


def normalize_generated_ontology(
    data: dict[str, Any],
    *,
    existing_entity_names: set[str] | None = None,
) -> dict[str, Any]:
    """把模型输出收敛到平台本体契约，并保留指向已有对象类型的关系。"""
    known_entity_names = {
        str(name).strip() for name in (existing_entity_names or set()) if str(name).strip()
    }
    generated_entity_names: set[str] = set()
    used_entity_api_names: set[str] = set()
    entities: list[dict[str, Any]] = []
    for raw_entity in data.get("entities") or []:
        if not isinstance(raw_entity, dict):
            continue
        name = str(raw_entity.get("name") or "").strip()
        if not name or name in generated_entity_names:
            continue
        generated_entity_names.add(name)
        entity_api_name = reserve_api_name(
            used_entity_api_names,
            raw_entity.get("api_name"),
            display_name=name,
            prefix="entity",
            stable_key=name,
        )
        properties: list[dict[str, Any]] = []
        property_names: set[str] = set()
        property_api_names: set[str] = set()
        key_seen = False
        title_seen = False
        for raw_property in raw_entity.get("properties") or []:
            if not isinstance(raw_property, dict):
                continue
            property_name = str(raw_property.get("name") or "").strip()
            if not property_name or property_name in property_names:
                continue
            data_type = str(raw_property.get("data_type") or "string").strip().lower()
            if data_type not in ONTOLOGY_PROPERTY_TYPES:
                raise ValueError(f"对象类型“{name}”的属性“{property_name}”使用了不支持的数据类型: {data_type}")
            is_key = bool(raw_property.get("is_key", False)) and not key_seen
            key_seen = key_seen or is_key
            is_title = bool(raw_property.get("is_title", False)) and not title_seen
            title_seen = title_seen or is_title
            is_enum = bool(raw_property.get("is_enum", False))
            enum_values = [str(item) for item in (raw_property.get("enum_values") or [])]
            property_api_name = reserve_api_name(
                property_api_names,
                raw_property.get("api_name"),
                display_name=property_name,
                prefix="property",
                stable_key=f"{entity_api_name}.{property_name}",
            )
            properties.append(
                {
                    "name": property_name,
                    "api_name": property_api_name,
                    "data_type": data_type,
                    "description": str(raw_property.get("description") or ""),
                    "is_key": is_key,
                    "is_title": is_title,
                    "is_required": bool(raw_property.get("is_required", False)) or is_key,
                    "is_enum": is_enum,
                    "enum_values": enum_values if is_enum else [],
                    "default_value": raw_property.get("default_value", ""),
                    "constraints": normalize_property_constraints(
                        data_type,
                        raw_property.get("constraints")
                        if isinstance(raw_property.get("constraints"), dict)
                        else {},
                    ),
                    "is_sensitive": bool(raw_property.get("is_sensitive", False)),
                }
            )
            property_names.add(property_name)
        if not key_seen and properties:
            properties[0]["is_key"] = True
            properties[0]["is_required"] = True
        if not title_seen and properties:
            # Legacy/simple generation remains usable: a unique primary key is
            # the deterministic display fallback until a business title is set.
            key_property = next(prop for prop in properties if prop["is_key"])
            key_property["is_title"] = True
        state_property = str(raw_entity.get("state_property") or "").strip()
        if state_property and not any(
            prop["name"] == state_property and prop["is_enum"] for prop in properties
        ):
            state_property = ""
        entities.append(
            {
                "name": name,
                "api_name": entity_api_name,
                "description": str(raw_entity.get("description") or ""),
                "is_abstract": bool(raw_entity.get("is_abstract", False)),
                "state_property": state_property,
                "properties": properties,
            }
        )

    all_entity_names = known_entity_names | generated_entity_names
    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[str, str, str]] = set()
    relation_api_names: set[str] = set()
    for raw_relation in data.get("relations") or []:
        if not isinstance(raw_relation, dict):
            continue
        source = str(raw_relation.get("source") or "").strip()
        target = str(raw_relation.get("target") or "").strip()
        if source not in all_entity_names or target not in all_entity_names:
            continue
        relation_type = str(raw_relation.get("relation_type") or "1:N").strip().upper()
        if relation_type not in ONTOLOGY_RELATION_TYPES:
            raise ValueError(f"关系“{raw_relation.get('name') or f'{source}-{target}'}”使用了不支持的基数: {relation_type}")
        name = str(raw_relation.get("name") or "").strip() or f"{source}-{target}"
        key = (name, source, target)
        if key in relation_keys:
            continue
        relation_keys.add(key)
        relation_api_name = reserve_api_name(
            relation_api_names,
            raw_relation.get("api_name"),
            display_name=name,
            prefix="relation",
            stable_key=f"{source}.{name}.{target}",
        )
        navigation = normalize_relation_navigation(
            relation_name=name,
            relation_api_name=relation_api_name,
            source_display_name=raw_relation.get("source_display_name"),
            source_api_name=raw_relation.get("source_api_name"),
            target_display_name=raw_relation.get("target_display_name"),
            target_api_name=raw_relation.get("target_api_name"),
        )
        relations.append(
            {
                "name": name,
                "api_name": relation_api_name,
                "source": source,
                "target": target,
                **navigation,
                "storage_kind": normalize_relation_storage_kind(
                    raw_relation.get("storage_kind")
                ),
                "relation_type": relation_type,
                "description": str(raw_relation.get("description") or ""),
            }
        )
    return {"entities": entities, "relations": relations}


def generate_ontology(db: Session, scenario: BusinessScenario, description: str) -> dict[str, Any]:
    """调用 LLM 生成本体草稿（不落库），返回 {entities, relations}。"""
    from ..models import LLMConfig

    llm = tenant_service.get_visible(db, LLMConfig, scenario.llm_config_id) if getattr(scenario, "llm_config_id", None) and db.info.get("tenant_id") else None
    if not llm:
        if db.info.get("tenant_id"):
            candidates = llm_service.routable_configs(db, "chat")
            llm = candidates[0] if candidates else None
        else:
            llm = db.execute(
                select(LLMConfig).where(LLMConfig.is_default == True, LLMConfig.enabled == True)  # noqa: E712
            ).scalars().first()
    if not llm:
        raise ValueError("请先在「LLM 配置」中配置并启用一个默认模型")

    # 注意：_GEN_PROMPT 内含 JSON 示例花括号，不能用 str.format（会触发 KeyError），
    # 用 replace 注入业务描述。输入在调用前完成显式边界校验，不能再做切片。
    context = _ontology_context(description)
    last_err: Exception | None = None
    data: dict[str, Any] = {}
    for _ in range(3):
        resp = llm_service.chat(
            llm,
            [
                {"role": "system", "content": "你只输出 JSON。"},
                {"role": "user", "content": _GEN_PROMPT.replace("{description}", context)},
            ],
            temperature=0.3,
            max_tokens=ONTOLOGY_MAX_OUTPUT_TOKENS,
            db=db,
        )
        try:
            data = _extract_json(resp.get("content", ""))
            if data.get("entities"):
                break
            last_err = ValueError("AI 未返回有效实体")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    else:
        raise ValueError(f"AI 多次生成均失败: {last_err}")
    if not data.get("entities"):
        raise ValueError("AI 未返回有效实体，请补充业务描述后重试")
    return normalize_generated_ontology(
        data,
        existing_entity_names={
            str(entity.name)
            for entity in (getattr(scenario, "entities", None) or [])
            if str(getattr(entity, "name", "")).strip()
        },
    )


def apply_generated_ontology(
    db: Session,
    scenario: BusinessScenario,
    data: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, int]:
    """把 AI 生成的本体草稿写入场景（追加，不覆盖已有定义）。"""
    name_map = {e.name: e for e in scenario.entities}
    relation_keys = {(r.name, r.source_entity_id, r.target_entity_id) for r in scenario.relations}
    relation_names = {r.name for r in scenario.relations}
    entities_added = 0
    entities_skipped = 0
    properties_added = 0
    properties_skipped = 0
    for e in data.get("entities", []):
        ent = name_map.get(e["name"])
        if ent is not None:
            if not str(getattr(ent, "api_name", "") or "").strip():
                ent.api_name = allocate_resource_api_name(
                    db,
                    OntologyEntity,
                    scope_field="scenario_id",
                    scope_id=scenario.id,
                    display_name=ent.name,
                    prefix="entity",
                    stable_key=ent.id,
                    resource_id=ent.id,
                )
            entities_skipped += 1
        else:
            api_name = allocate_resource_api_name(
                db,
                OntologyEntity,
                scope_field="scenario_id",
                scope_id=scenario.id,
                value=e.get("api_name"),
                display_name=e["name"],
                prefix="entity",
                stable_key=e.get("api_name") or e["name"],
            )
            ent = OntologyEntity(
                scenario_id=scenario.id,
                name=e["name"],
                api_name=api_name,
                description=e.get("description", ""),
                is_abstract=bool(e.get("is_abstract", False)),
                namespace=scenario.namespace or "default",
                state_property=e.get("state_property", ""),
            )
            db.add(ent)
            db.flush()
            name_map[e["name"]] = ent
            entities_added += 1

        existing_properties = {
            prop.name: prop for prop in (getattr(ent, "properties", None) or [])
        }
        has_key = any(bool(prop.is_key) for prop in existing_properties.values())
        has_title = any(bool(getattr(prop, "is_title", False)) for prop in existing_properties.values())
        for p in e.get("properties", []):
            if p["name"] in existing_properties:
                current_property = existing_properties[p["name"]]
                if not str(getattr(current_property, "api_name", "") or "").strip():
                    current_property.api_name = allocate_resource_api_name(
                        db,
                        OntologyProperty,
                        scope_field="entity_id",
                        scope_id=ent.id,
                        display_name=current_property.name,
                        prefix="property",
                        stable_key=current_property.id,
                        resource_id=current_property.id,
                    )
                properties_skipped += 1
                continue
            is_key = bool(p.get("is_key", False)) and not has_key
            is_title = bool(p.get("is_title", False)) and not has_title
            property_api_name = allocate_resource_api_name(
                db,
                OntologyProperty,
                scope_field="entity_id",
                scope_id=ent.id,
                value=p.get("api_name"),
                display_name=p["name"],
                prefix="property",
                stable_key=p.get("api_name") or f"{ent.api_name}.{p['name']}",
            )
            prop = OntologyProperty(
                entity_id=ent.id,
                name=p["name"],
                api_name=property_api_name,
                data_type=p.get("data_type", "string"),
                description=p.get("description", ""),
                is_key=is_key,
                is_title=is_title,
                is_required=bool(p.get("is_required", False)) or is_key,
                is_enum=bool(p.get("is_enum", False)),
                enum_values=p.get("enum_values") or [],
                default_value=p.get("default_value", ""),
                constraints=p.get("constraints") or {},
                is_sensitive=bool(p.get("is_sensitive", False)),
            )
            db.add(prop)
            existing_properties[p["name"]] = prop
            has_key = has_key or is_key
            has_title = has_title or is_title
            properties_added += 1
    relations_added = 0
    relations_skipped = 0
    for r in data.get("relations", []):
        src, tgt = name_map.get(r["source"]), name_map.get(r["target"])
        if not src or not tgt:
            relations_skipped += 1
            continue
        relation_key = (r["name"], src.id, tgt.id)
        if r["name"] in relation_names or relation_key in relation_keys:
            relations_skipped += 1
            continue
        relation_api_name = allocate_resource_api_name(
            db,
            OntologyRelation,
            scope_field="scenario_id",
            scope_id=scenario.id,
            value=r.get("api_name"),
            display_name=r["name"],
            prefix="relation",
            stable_key=r.get("api_name") or f"{src.api_name}.{r['name']}.{tgt.api_name}",
        )
        navigation = normalize_relation_navigation(
            relation_name=r["name"],
            relation_api_name=relation_api_name,
            source_display_name=r.get("source_display_name"),
            source_api_name=r.get("source_api_name"),
            target_display_name=r.get("target_display_name"),
            target_api_name=r.get("target_api_name"),
        )
        db.add(
            OntologyRelation(
                scenario_id=scenario.id,
                name=r["name"],
                api_name=relation_api_name,
                source_entity_id=src.id,
                target_entity_id=tgt.id,
                **navigation,
                storage_kind=normalize_relation_storage_kind(r.get("storage_kind")),
                relation_type=r.get("relation_type", "1:N"),
                description=r.get("description", ""),
                namespace=scenario.namespace or "default",
            )
        )
        relation_keys.add(relation_key)
        relation_names.add(r["name"])
        relations_added += 1
    if commit:
        db.commit()
    return {
        "entities_added": entities_added,
        "entities_skipped": entities_skipped,
        "properties_added": properties_added,
        "properties_skipped": properties_skipped,
        "relations_added": relations_added,
        "relations_skipped": relations_skipped,
    }


# ──────────────────────────────────────────────
# 数据映射 → 实例导入
# ──────────────────────────────────────────────
def _mapping_context(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
    *,
    data_source: DataSource | None = None,
) -> tuple[DataSource, OntologyEntity]:
    if mapping.scenario_id != scenario.id:
        raise ValueError("映射不属于当前业务场景")
    # 非开发环境可由运行时绑定解析为该环境的物理数据源；映射中的直接 ID
    # 仅保留开发兼容与定义预览用途。
    ds = data_source or db.get(DataSource, mapping.data_source_id)
    if not ds or ds.scenario_id not in (None, scenario.id):
        raise ValueError("映射对应的数据源不存在或不属于当前业务场景")
    if ds.type == "file_bucket":
        raise ValueError("该映射的数据源不是数据库类型")
    ent = getattr(mapping, "entity", None) or db.get(OntologyEntity, mapping.entity_id)
    if not ent or ent.scenario_id != scenario.id:
        raise ValueError("映射对应的实体不存在或不属于当前业务场景")
    if not mapping.table_name.strip():
        raise ValueError("请先选择映射的表")
    return ds, ent


def _quoted_mapping_identifier(data_source_type: str, value: str, *, label: str) -> str:
    identifier = str(value or "")
    if (
        not identifier
        or identifier != identifier.strip()
        or len(identifier) > 300
        or identifier == "*"
        or ";" in identifier
        or any(ord(character) < 32 for character in identifier)
    ):
        raise ValueError(f"{label}格式不合法，请从数据源表结构中重新选择")
    source_type = str(data_source_type or "").strip().lower()
    if source_type == "mysql":
        return "`" + identifier.replace("`", "``") + "`"
    if source_type in {"sqlite", "postgres"}:
        return '"' + identifier.replace('"', '""') + '"'
    raise ValueError("数据映射仅支持 sqlite、postgres 和 mysql 数据源")


def _quoted_mapping_table(table_name: str, data_source_type: str = "sqlite") -> str:
    """Quote an inspected physical table for its connector dialect."""
    value = str(table_name or "")
    parts = value.split(".")
    if len(parts) > 3 or any(not part or part != part.strip() for part in parts):
        raise ValueError("表名格式不合法，请从数据源表结构中重新选择")
    return ".".join(
        _quoted_mapping_identifier(data_source_type, part, label="表名")
        for part in parts
    )


def _quoted_mapping_column(column_name: str, data_source_type: str) -> str:
    return _quoted_mapping_identifier(data_source_type, column_name, label="列名")


def _relation_mapping_value(payload: Any, field: str) -> Any:
    if isinstance(payload, dict):
        return payload.get(field)
    return getattr(payload, field, None)


def _visible_relation_mapping_source(
    db: Session,
    scenario: BusinessScenario,
    source_id: str,
) -> DataSource:
    source = db.get(DataSource, source_id)
    if not source or source.scenario_id not in (None, scenario.id):
        raise ValueError("关系映射的数据源不存在或不属于当前业务场景")
    if str(source.tenant_id or "") != str(scenario.tenant_id or ""):
        raise ValueError("关系映射不能引用其他租户的数据源")
    if db.info.get("tenant_id"):
        visible = tenant_service.get_visible(db, DataSource, source_id)
        if not visible:
            raise ValueError("关系映射的数据源不可访问")
        source = visible
    if source.type == "file_bucket":
        raise ValueError("关系映射只能使用数据库数据源")
    return source


def _table_columns(source: DataSource, table_name: str) -> list[str]:
    _quoted_mapping_table(table_name, source.type)
    tables = datasource_service.list_tables(source)
    normalized = str(table_name or "").strip()
    candidate = next(
        (
            table for table in tables
            if str(table.get("name") or "") == normalized
            or str(table.get("name") or "") == normalized.split(".")[-1]
        ),
        None,
    )
    if candidate is None:
        raise ValueError(f"数据源中不存在表“{normalized}”")
    return [str(column.get("name") or "") for column in candidate.get("columns") or []]


def validate_relation_data_mapping(
    db: Session,
    scenario: BusinessScenario,
    payload: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and derive a closed relation binding from inspected metadata."""
    mode = str(_relation_mapping_value(payload, "mode") or "").strip()
    if mode not in {"source_fk", "target_fk", "join_table"}:
        raise ValueError("关系映射模式只能是源对象外键、目标对象外键或中间表")
    relation_id = str(_relation_mapping_value(payload, "relation_id") or "").strip()
    source_mapping_id = str(_relation_mapping_value(payload, "source_mapping_id") or "").strip()
    target_mapping_id = str(_relation_mapping_value(payload, "target_mapping_id") or "").strip()
    relation = db.get(OntologyRelation, relation_id)
    source_mapping = db.get(DataMapping, source_mapping_id)
    target_mapping = db.get(DataMapping, target_mapping_id)
    if not relation or relation.scenario_id != scenario.id:
        raise ValueError("关系不属于当前业务场景")
    if not source_mapping or source_mapping.scenario_id != scenario.id:
        raise ValueError("源对象映射不属于当前业务场景")
    if not target_mapping or target_mapping.scenario_id != scenario.id:
        raise ValueError("目标对象映射不属于当前业务场景")
    if source_mapping.entity_id != relation.source_entity_id:
        raise ValueError("源对象映射与关系的源对象类型不一致")
    if target_mapping.entity_id != relation.target_entity_id:
        raise ValueError("目标对象映射与关系的目标对象类型不一致")

    endpoints: list[tuple[str, DataMapping]] = [
        ("源对象", source_mapping), ("目标对象", target_mapping)
    ]
    endpoint_columns: dict[str, list[str]] = {}
    for label, mapping in endpoints:
        entity = db.get(OntologyEntity, mapping.entity_id)
        if not entity or entity.scenario_id != scenario.id:
            raise ValueError(f"{label}类型不存在")
        keys = [prop for prop in entity.properties if bool(prop.is_key)]
        if len(keys) != 1:
            raise ValueError(f"{label}类型必须且只能有一个主键属性")
        mapped_key = str((mapping.column_map or {}).get(keys[0].name) or "").strip()
        if not mapped_key:
            raise ValueError(f"{label}映射必须配置主键属性“{keys[0].name}”的源列")
        source = _visible_relation_mapping_source(db, scenario, mapping.data_source_id)
        columns = _table_columns(source, mapping.table_name)
        if mapped_key not in columns:
            raise ValueError(f"{label}主键映射列“{mapped_key}”不存在")
        endpoint_columns[mapping.id] = columns

    foreign_key_column = str(
        _relation_mapping_value(payload, "foreign_key_column") or ""
    ).strip()
    source_key_column = str(
        _relation_mapping_value(payload, "source_key_column") or ""
    ).strip()
    target_key_column = str(
        _relation_mapping_value(payload, "target_key_column") or ""
    ).strip()
    if mode in {"source_fk", "target_fk"}:
        carrier = source_mapping if mode == "source_fk" else target_mapping
        source = _visible_relation_mapping_source(db, scenario, carrier.data_source_id)
        if not foreign_key_column:
            raise ValueError("请选择承载外键的源列")
        _quoted_mapping_column(foreign_key_column, source.type)
        if foreign_key_column not in endpoint_columns[carrier.id]:
            raise ValueError(f"外键列“{foreign_key_column}”不存在于承载侧表中")
        table_name = carrier.table_name
        available_columns = endpoint_columns[carrier.id]
    else:
        join_source_id = str(
            _relation_mapping_value(payload, "join_data_source_id") or ""
        ).strip()
        table_name = str(
            _relation_mapping_value(payload, "join_table_name") or ""
        ).strip()
        if not join_source_id or not table_name:
            raise ValueError("中间表映射必须选择数据源和中间表")
        source = _visible_relation_mapping_source(db, scenario, join_source_id)
        available_columns = _table_columns(source, table_name)
        if not source_key_column or not target_key_column:
            raise ValueError("中间表映射必须选择源对象键列和目标对象键列")
        _quoted_mapping_column(source_key_column, source.type)
        _quoted_mapping_column(target_key_column, source.type)
        missing = [
            column for column in (source_key_column, target_key_column)
            if column not in available_columns
        ]
        if missing:
            raise ValueError(f"中间表中不存在列：{'、'.join(missing)}")
        foreign_key_column = ""

    if mode == "join_table":
        matching_endpoint = next(
            (
                item for item in (source_mapping, target_mapping)
                if item.data_source_id == source.id and item.data_source_binding_key
            ),
            None,
        )
        if matching_endpoint:
            binding_key = matching_endpoint.data_source_binding_key or ""
            binding_ref = connector_service.with_required_capabilities(
                matching_endpoint.data_source_binding_ref or {}, "sql_read"
            )
        else:
            binding = connector_service.runtime_binding_metadata(
                "data_source",
                {"name": source.name, "type": source.type},
                path=f"relation_mapping:{relation.id}",
            )
            binding_key = str(binding["binding_key"])
            binding_ref = connector_service.with_required_capabilities(
                binding["reference"], "sql_read"
            )
    else:
        carrier_mapping = source_mapping if mode == "source_fk" else target_mapping
        binding_key = carrier_mapping.data_source_binding_key or ""
        binding_ref = connector_service.with_required_capabilities(
            carrier_mapping.data_source_binding_ref or {}, "sql_read"
        )
        if not binding_key:
            # Deterministically upgrade legacy object mappings during relation
            # authoring. Preflight remains read-only; the CRUD route persists
            # the same derived fields and creates the dev binding atomically.
            binding = connector_service.runtime_binding_metadata(
                "data_source",
                {"name": source.name, "type": source.type},
                path=(
                    f"scenario:{scenario.id}:entity:{carrier_mapping.entity_id}"
                ),
            )
            binding_key = str(binding["binding_key"])
            binding_ref = connector_service.with_required_capabilities(
                binding["reference"], "sql_read"
            )
    derived = {
        "relation_id": relation.id,
        "source_mapping_id": source_mapping.id,
        "target_mapping_id": target_mapping.id,
        "mode": mode,
        "data_source_id": source.id,
        "data_source_binding_key": binding_key,
        "data_source_binding_ref": binding_ref,
        "table_name": table_name,
        "foreign_key_column": foreign_key_column,
        "source_key_column": source_key_column if mode == "join_table" else "",
        "target_key_column": target_key_column if mode == "join_table" else "",
    }
    preview = {
        "ok": True,
        "message": "关系映射预检通过",
        "mode": mode,
        "relation_name": relation.name,
        "source_entity_name": source_mapping.entity.name if source_mapping.entity else "",
        "target_entity_name": target_mapping.entity.name if target_mapping.entity else "",
        "data_source_id": source.id,
        "data_source_name": source.name,
        "table_name": table_name,
        "available_columns": available_columns,
        "errors": [],
        "warnings": [],
    }
    return derived, preview


def purge_relation_mapping_instances(db: Session, relation_mapping_id: str) -> int:
    """Remove only links produced by one explicit mapping, preserving manual facts."""
    removed = 0
    candidates = db.execute(select(RelationInstance)).scalars().all()
    for instance in candidates:
        metadata = instance.source_metadata if isinstance(instance.source_metadata, dict) else {}
        if str(metadata.get("relation_mapping_id") or "") == str(relation_mapping_id):
            db.delete(instance)
            removed += 1
    return removed


def preview_mapping(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
    limit: int = 20,
    *,
    data_source: DataSource | None = None,
) -> dict[str, Any]:
    """读取映射源表样本并检查属性覆盖，不创建或修改对象实例。"""
    ds, ent = _mapping_context(db, scenario, mapping, data_source=data_source)
    sample_limit = max(1, min(int(limit or 20), 100))
    result = datasource_service.run_query(
        ds,
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name, ds.type)}",
        limit=sample_limit,
    )
    columns = [str(column) for column in result.get("columns", [])]
    available_columns = set(columns)
    col_map = {str(key): str(value) for key, value in (mapping.column_map or {}).items() if value}
    transform_rules = normalize_transform_rules(ent, getattr(mapping, "transform_rules", {}) or {})
    known_properties = {prop.name for prop in ent.properties}
    fields: list[dict[str, Any]] = []
    missing_properties: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    for prop in ent.properties:
        source_column = col_map.get(prop.name, "")
        source_exists = bool(source_column and source_column in available_columns)
        status = "mapped" if source_exists else "missing" if not source_column else "invalid"
        fields.append(
            {
                "property_name": prop.name,
                "data_type": prop.data_type,
                "is_key": prop.is_key,
                "is_title": bool(getattr(prop, "is_title", False)),
                "is_required": prop.is_required,
                "source_column": source_column,
                "source_exists": source_exists,
                "status": status,
                "transform_rules": transform_rules.get(prop.name, []),
            }
        )
        if not source_exists:
            missing_properties.append(prop.name)
            if not source_column:
                message = f"属性“{prop.name}”尚未配置源列"
            else:
                message = f"属性“{prop.name}”引用的源列“{source_column}”不存在"
            if prop.is_key or prop.is_required:
                errors.append(message)
            else:
                warnings.append(message)

    for property_name, source_column in col_map.items():
        if property_name not in known_properties:
            errors.append(f"映射引用了不存在的实体属性“{property_name}”")

    mapped_source_columns = set(col_map.values())
    unmapped_columns = [column for column in columns if column not in mapped_source_columns]
    if unmapped_columns:
        warnings.append(f"源表还有 {len(unmapped_columns)} 个列未映射")
    if not result.get("rows"):
        warnings.append("源表当前没有数据，刷新时不会创建对象")
    if result.get("truncated"):
        warnings.append(f"仅展示前 {sample_limit} 行样本")

    transformed_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(result.get("rows") or [], start=1):
        record = dict(zip(columns, row))
        attributes: dict[str, Any] = {}
        try:
            for property_name, source_column in col_map.items():
                if source_column in record:
                    attributes[property_name] = apply_transform_rules(
                        record[source_column],
                        transform_rules.get(property_name, []),
                    )
            attributes, _state, _quality = validate_instance_payload(
                ent,
                attributes,
                quality={},
                strict_types=True,
            )
            transformed_rows.append(attributes)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"样本第 {row_index} 行转换或类型校验失败：{exc}")

    ok = not errors
    return {
        "mapping_id": mapping.id,
        "entity_name": ent.name,
        "data_source_name": ds.name,
        "table_name": mapping.table_name,
        "ok": ok,
        "message": "映射检查通过" if ok else "映射存在需要修正的问题",
        "columns": columns,
        "sample_rows": result.get("rows", []),
        "transformed_rows": transformed_rows,
        "row_count": int(result.get("row_count", 0)),
        "truncated": bool(result.get("truncated", False)),
        "fields": fields,
        "missing_properties": missing_properties,
        "unmapped_columns": unmapped_columns,
        "warnings": warnings,
        "errors": errors,
    }


def import_instances_from_mapping(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
    limit: int = 50,
    *,
    data_source: DataSource | None = None,
    commit: bool = True,
    environment: str = "dev",
    relation_mappings: list[Any] | None = None,
    relation_data_sources: dict[str, DataSource] | None = None,
    mapping_data_sources: dict[str, DataSource] | None = None,
    runtime_mappings: dict[str, Any] | None = None,
    runtime_relations: dict[str, Any] | None = None,
    mapping_connector_audits: dict[str, dict[str, Any]] | None = None,
    relation_connector_audits: dict[str, dict[str, Any]] | None = None,
    definition_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按数据映射增量同步实例，并写入可审计的来源快照。

    ``source_ref`` 保留短小可读的引用；精确且不会串源的映射标识、运行环境、
    数据源、表和记录键写在 ``source_metadata``。这样同一个实体由多个数据源/
    映射/部署环境同步时也不会互相覆盖，未变记录则可安全复用既有实例。
    """
    runtime_environment = str(environment or "dev").strip().lower() or "dev"
    if runtime_environment != "dev" and getattr(mapping, "entity", None) is None:
        raise ValueError("非开发环境映射缺少发布快照中的对象定义")
    ds, ent = _mapping_context(db, scenario, mapping, data_source=data_source)
    col_map = mapping.column_map or {}
    transform_rules = normalize_transform_rules(ent, getattr(mapping, "transform_rules", {}) or {})
    definition_provenance = dict(definition_provenance or {})
    mapping_connector_audits = mapping_connector_audits or {}
    relation_connector_audits = relation_connector_audits or {}

    def connector_lineage(source: DataSource, audit: dict[str, Any] | None) -> dict[str, Any]:
        safe_audit = dict(audit or {})
        safe_audit.setdefault("kind", "data_source")
        safe_audit.setdefault("environment", runtime_environment)
        safe_audit.setdefault("managed", False)
        safe_audit.setdefault("binding_key", None)
        safe_audit.setdefault("binding_id", None)
        safe_audit.setdefault("connector_id", str(source.id))
        safe_audit.setdefault("connector_name", str(source.name or ""))
        safe_audit.setdefault("adapter_type", str(source.type or ""))
        return {
            "connector_id": str(source.id),
            "connector_type": str(source.type or ""),
            "binding_key": safe_audit.get("binding_key"),
            "binding_id": safe_audit.get("binding_id"),
            "audit": safe_audit,
        }

    object_connector_lineage = connector_lineage(
        ds, mapping_connector_audits.get(str(mapping.id))
    )

    result = datasource_service.run_query(
        ds,
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name, ds.type)}",
        limit=limit,
    )
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    # 主键属性
    key_prop = next((p.name for p in ent.properties if p.is_key), None)
    key_col = col_map.get(key_prop) if key_prop else None
    title_prop = next((p.name for p in ent.properties if getattr(p, "is_title", False)), None)
    title_col = col_map.get(title_prop) if title_prop else None

    created_instances: list[OntologyInstance] = []
    updated_instances = 0
    imported_instances = db.execute(
        select(OntologyInstance).where(
            OntologyInstance.entity_id == ent.id,
            OntologyInstance.source == "imported",
        )
    ).scalars().all()
    # 新版使用 (mapping_id, environment, record_key) 做稳定身份；旧版用
    # table:key 作为开发环境的一次性回退，升级后第一次刷新会补齐元数据，随后
    # 完全按新版键去重。这样共享数据库中的 staging/prod 写入不会覆盖 dev 对象。
    existing_by_identity: dict[tuple[str, str, str], OntologyInstance] = {}
    legacy_by_ref: dict[str, OntologyInstance] = {}
    for instance in imported_instances:
        metadata = instance.source_metadata or {}
        if isinstance(metadata, dict) and metadata.get("mapping_id") and metadata.get("record_key") is not None:
            metadata_environment = str(metadata.get("runtime_environment") or "dev").strip().lower() or "dev"
            existing_by_identity[
                (str(metadata["mapping_id"]), metadata_environment, str(metadata["record_key"]))
            ] = instance
        if runtime_environment == "dev" and instance.source_ref:
            legacy_by_ref[instance.source_ref] = instance

    row_instances: list[OntologyInstance] = []

    for row in rows:
        rec = dict(zip(columns, row))
        attrs: dict[str, Any] = {}
        for prop_name, col in col_map.items():
            if col in rec:
                attrs[prop_name] = apply_transform_rules(
                    rec[col],
                    transform_rules.get(prop_name, []),
                )
        attrs, object_state, object_quality = validate_instance_payload(
            ent,
            attrs,
            quality={
                "score": 1.0,
                "status": "valid",
                "issues": [],
                "source": f"mapping:{mapping.id}",
            },
            strict_types=True,
        )
        if key_prop and attrs.get(key_prop) is not None:
            record_key = str(attrs[key_prop])
        else:
            # 没有映射主键时，用规范化整行哈希代替递增序号。该键对相同源记录稳定，
            # 不会在每次 refresh 时生成重复对象；预览/校验会继续提示应配置主键。
            canonical = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
            record_key = f"row:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}"
        legacy_ref = f"{mapping.table_name}:{record_key}"
        ref = f"{ds.id}:{mapping.table_name}:{record_key}"[:500]
        identity = (mapping.id, runtime_environment, record_key)
        inst = existing_by_identity.get(identity) or legacy_by_ref.get(legacy_ref)
        display = resolve_instance_display_name(
            ent,
            attrs,
            fallback=f"{ent.name}-{len(created_instances) + 1}",
        )
        metadata = {
            "mapping_id": mapping.id,
            "runtime_environment": runtime_environment,
            "data_source_id": ds.id,
            "table_name": mapping.table_name,
            "key_column": key_col or "",
            "title_property": title_prop or "",
            "title_column": title_col or "",
            "record_key": record_key,
            "transform_rules": transform_rules,
            "version": "mapping-v3",
            "definition_snapshot_id": definition_provenance.get("snapshot_id"),
            "release_id": definition_provenance.get("release_id"),
            "definition_hash": str(definition_provenance.get("definition_hash") or ""),
            "definition_source": str(definition_provenance.get("source") or "live"),
            "connector_ref": object_connector_lineage,
        }
        if not inst:
            inst = OntologyInstance(
                scenario_id=scenario.id,
                entity_id=ent.id,
                name=display,
                attributes=attrs,
                source="imported",
                source_ref=ref,
                source_metadata=metadata,
                state=object_state,
                quality=object_quality,
            )
            db.add(inst)
            db.flush()
            existing_by_identity[identity] = inst
            legacy_by_ref[legacy_ref] = inst
            created_instances.append(inst)
        else:
            # 源记录发生变化时更新运行时对象，避免 P0 仅新增不更新导致血缘与对象
            # 值脱节。由于当前 API 的 limit 是安全上限，未出现在本批中的对象不删除。
            if (
                inst.name != display
                or (inst.attributes or {}) != attrs
                or inst.source_ref != ref
                or (inst.source_metadata or {}) != metadata
                or (inst.state or "") != object_state
                or (inst.quality or {}) != object_quality
            ):
                inst.name = display
                inst.attributes = attrs
                inst.source_ref = ref
                inst.source_metadata = metadata
                inst.state = object_state
                inst.quality = object_quality
                updated_instances += 1
        row_instances.append(inst)

    # Only first-class, validated relation mappings may create links. Column
    # names are never compared heuristically and an absent definition means
    # exactly zero generated links.
    rels_created = 0
    if relation_mappings is None:
        relation_mappings = (
            db.execute(
                select(RelationDataMapping).where(
                    RelationDataMapping.scenario_id == scenario.id
                )
            ).scalars().all()
            if runtime_environment == "dev"
            else []
        )
    relation_data_sources = relation_data_sources or {}
    mapping_data_sources = mapping_data_sources or {mapping.id: ds}
    runtime_mappings = runtime_mappings or {
        item.id: item
        for item in db.execute(
            select(DataMapping).where(DataMapping.scenario_id == scenario.id)
        ).scalars().all()
    }
    runtime_relations = runtime_relations or {}

    imported = db.execute(
        select(OntologyInstance).where(
            OntologyInstance.scenario_id == scenario.id,
            OntologyInstance.source == "imported",
        )
    ).scalars().all()
    instances_by_mapping: dict[str, dict[str, OntologyInstance]] = {}
    for imported_instance in imported:
        metadata = (
            imported_instance.source_metadata
            if isinstance(imported_instance.source_metadata, dict)
            else {}
        )
        mapping_id = str(metadata.get("mapping_id") or "")
        metadata_environment = str(
            metadata.get("runtime_environment") or "dev"
        ).strip().lower()
        record_key = metadata.get("record_key")
        if mapping_id and metadata_environment == runtime_environment and record_key is not None:
            instances_by_mapping.setdefault(mapping_id, {})[str(record_key)] = imported_instance

    def mapping_key_contract(runtime_mapping: Any) -> tuple[str, str, list[dict[str, Any]]]:
        entity = getattr(runtime_mapping, "entity", None) or db.get(
            OntologyEntity, str(getattr(runtime_mapping, "entity_id", "") or "")
        )
        properties = list(getattr(entity, "properties", []) or []) if entity else []
        keys = [prop for prop in properties if bool(getattr(prop, "is_key", False))]
        if len(keys) != 1:
            raise ValueError("关系映射端点对象必须且只能有一个主键属性")
        key_name = str(keys[0].name)
        key_column = str((getattr(runtime_mapping, "column_map", {}) or {}).get(key_name) or "")
        if not key_column:
            raise ValueError("关系映射端点对象未映射主键源列")
        rules = normalize_transform_rules(
            entity, getattr(runtime_mapping, "transform_rules", {}) or {}
        ).get(key_name, [])
        return key_name, key_column, rules

    def canonical_mapping_key(raw_value: Any, runtime_mapping: Any) -> str | None:
        if raw_value is None:
            return None
        _name, _column, rules = mapping_key_contract(runtime_mapping)
        transformed = apply_transform_rules(raw_value, rules)
        if transformed is None:
            return None
        return str(transformed)

    for relation_mapping in relation_mappings:
        relation_mapping_id = str(_relation_mapping_value(relation_mapping, "id") or "")
        source_mapping_id = str(
            _relation_mapping_value(relation_mapping, "source_mapping_id") or ""
        )
        target_mapping_id = str(
            _relation_mapping_value(relation_mapping, "target_mapping_id") or ""
        )
        if mapping.id not in {source_mapping_id, target_mapping_id}:
            continue
        mode = str(_relation_mapping_value(relation_mapping, "mode") or "")
        if mode not in {"source_fk", "target_fk", "join_table"}:
            raise ValueError("发布定义包含不支持的关系映射模式")

        # Live definitions are revalidated against current connector metadata.
        # A changed object mapping therefore fails closed until the user fixes
        # and preflights the relation mapping again.
        if isinstance(relation_mapping, RelationDataMapping):
            live_payload = {
                "relation_id": relation_mapping.relation_id,
                "source_mapping_id": source_mapping_id,
                "target_mapping_id": target_mapping_id,
                "mode": mode,
                "foreign_key_column": relation_mapping.foreign_key_column,
                "join_data_source_id": relation_mapping.data_source_id,
                "join_table_name": relation_mapping.table_name,
                "source_key_column": relation_mapping.source_key_column,
                "target_key_column": relation_mapping.target_key_column,
            }
            derived, _preview = validate_relation_data_mapping(db, scenario, live_payload)
            for field in (
                "relation_id", "source_mapping_id", "target_mapping_id", "mode",
                "data_source_id", "table_name", "foreign_key_column",
                "source_key_column", "target_key_column",
            ):
                if str(derived.get(field) or "") != str(getattr(relation_mapping, field) or ""):
                    raise ValueError("关系映射依赖的对象映射已变化，请重新保存并预检")

        relation_id = str(_relation_mapping_value(relation_mapping, "relation_id") or "")
        relation = runtime_relations.get(relation_id) or db.get(OntologyRelation, relation_id)
        if not relation or str(getattr(relation, "scenario_id", scenario.id)) != scenario.id:
            raise ValueError("关系映射引用的关系不属于当前运行定义")
        source_index = instances_by_mapping.get(source_mapping_id, {})
        target_index = instances_by_mapping.get(target_mapping_id, {})
        source_runtime_mapping = runtime_mappings.get(source_mapping_id)
        target_runtime_mapping = runtime_mappings.get(target_mapping_id)
        if not source_runtime_mapping or not target_runtime_mapping:
            raise ValueError("关系映射端点不属于当前运行定义")
        candidate_pairs: list[tuple[str, str]] = []
        relation_scan_complete = True
        if mode in {"source_fk", "target_fk"}:
            carrier_id = source_mapping_id if mode == "source_fk" else target_mapping_id
            carrier_mapping = (
                source_runtime_mapping if mode == "source_fk" else target_runtime_mapping
            )
            opposite_mapping = (
                target_runtime_mapping if mode == "source_fk" else source_runtime_mapping
            )
            carrier_source = mapping_data_sources.get(carrier_id)
            if carrier_source is None:
                if runtime_environment != "dev":
                    raise ValueError("发布关系映射缺少已解析的外键承载侧连接器")
                carrier_source = _visible_relation_mapping_source(
                    db, scenario, str(getattr(carrier_mapping, "data_source_id", "") or "")
                )
            resolved_relation_source = carrier_source
            if mapping.id == carrier_id:
                carrier_columns = list(columns)
                carrier_rows = list(rows)
                relation_scan_complete = not bool(result.get("truncated", False))
            else:
                carrier_result = datasource_service.run_parameterized_query(
                    carrier_source,
                    f"SELECT * FROM {_quoted_mapping_table(str(getattr(carrier_mapping, 'table_name', '') or ''), carrier_source.type)}",
                    {},
                    limit=limit,
                )
                carrier_columns = [str(item) for item in carrier_result.get("columns") or []]
                carrier_rows = list(carrier_result.get("rows") or [])
                relation_scan_complete = not bool(carrier_result.get("truncated", False))
            fk_column = str(
                _relation_mapping_value(relation_mapping, "foreign_key_column") or ""
            )
            if fk_column not in carrier_columns:
                raise ValueError("显式关系映射的外键列不存在于刷新结果")
            _carrier_key_name, carrier_key_column, _carrier_rules = mapping_key_contract(
                carrier_mapping
            )
            if carrier_key_column not in carrier_columns:
                raise ValueError("外键承载侧对象映射的主键列不存在")
            for row in carrier_rows:
                record = dict(zip(carrier_columns, row))
                foreign_value = record.get(fk_column)
                carrier_key = canonical_mapping_key(
                    record.get(carrier_key_column), carrier_mapping
                )
                foreign_key = canonical_mapping_key(foreign_value, opposite_mapping)
                if foreign_value is None or carrier_key is None:
                    continue
                if mode == "source_fk":
                    if foreign_key is not None:
                        candidate_pairs.append((carrier_key, foreign_key))
                else:
                    if foreign_key is not None:
                        candidate_pairs.append((foreign_key, carrier_key))
        else:
            join_source = relation_data_sources.get(relation_mapping_id)
            if join_source is None:
                if runtime_environment != "dev":
                    raise ValueError("发布关系映射缺少已解析的中间表连接器")
                join_source_id = str(
                    _relation_mapping_value(relation_mapping, "data_source_id") or ""
                )
                join_source = _visible_relation_mapping_source(
                    db, scenario, join_source_id
                )
            resolved_relation_source = join_source
            join_table = str(_relation_mapping_value(relation_mapping, "table_name") or "")
            source_key_column = str(
                _relation_mapping_value(relation_mapping, "source_key_column") or ""
            )
            target_key_column = str(
                _relation_mapping_value(relation_mapping, "target_key_column") or ""
            )
            query = (
                f"SELECT {_quoted_mapping_column(source_key_column, join_source.type)}, "
                f"{_quoted_mapping_column(target_key_column, join_source.type)} "
                f"FROM {_quoted_mapping_table(join_table, join_source.type)}"
            )
            join_result = datasource_service.run_parameterized_query(
                join_source, query, {}, limit=limit
            )
            relation_scan_complete = not bool(join_result.get("truncated", False))
            for row in join_result.get("rows") or []:
                if len(row) < 2 or row[0] is None or row[1] is None:
                    continue
                source_key = canonical_mapping_key(row[0], source_runtime_mapping)
                target_key = canonical_mapping_key(row[1], target_runtime_mapping)
                if source_key is not None and target_key is not None:
                    candidate_pairs.append((source_key, target_key))

        locked_relation = db.execute(
            select(OntologyRelation)
            .where(
                OntologyRelation.id == relation_id,
                OntologyRelation.scenario_id == scenario.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if not locked_relation:
            raise ValueError("关系映射引用的持久关系定义不存在")
        existing_pairs = set(
            db.execute(
                select(
                    RelationInstance.source_instance_id,
                    RelationInstance.target_instance_id,
                ).where(RelationInstance.relation_id == relation_id)
            ).all()
        )
        desired_instance_pairs: set[tuple[str, str]] = set()
        resolved_candidates: list[
            tuple[str, str, OntologyInstance, OntologyInstance]
        ] = []
        missing_endpoint_count = 0
        for source_key, target_key in candidate_pairs:
            source_instance = source_index.get(source_key)
            target_instance = target_index.get(target_key)
            if not source_instance or not target_instance:
                missing_endpoint_count += 1
                continue
            resolved_candidates.append(
                (source_key, target_key, source_instance, target_instance)
            )
        relation_mapping_error = ""
        if missing_endpoint_count:
            relation_mapping_error = (
                f"关系映射有 {missing_endpoint_count} 条记录找不到已导入的源/目标对象；"
                "请先刷新两端对象映射"
            )
            if isinstance(relation_mapping, RelationDataMapping):
                relation_mapping.status = "error"
                relation_mapping.last_error = relation_mapping_error
                relation_mapping.last_checked_at = datetime.now().astimezone()
                relation_mapping.last_link_count = 0
            else:
                raise ValueError(relation_mapping_error)
        for source_key, target_key, source_instance, target_instance in resolved_candidates:
            pair = (source_instance.id, target_instance.id)
            desired_instance_pairs.add(pair)
            if pair in existing_pairs:
                continue
            validate_relation_instance_create(
                db,
                relation,
                source_instance_id=source_instance.id,
                target_instance_id=target_instance.id,
            )
            metadata = {
                "relation_mapping_id": relation_mapping_id,
                "relation_id": relation_id,
                "source_mapping_id": source_mapping_id,
                "target_mapping_id": target_mapping_id,
                "mode": mode,
                "runtime_environment": runtime_environment,
                "data_source_id": str(resolved_relation_source.id),
                "data_source_type": str(resolved_relation_source.type),
                "definition_data_source_id": str(
                    _relation_mapping_value(relation_mapping, "data_source_id") or ""
                ),
                "table_name": str(
                    _relation_mapping_value(relation_mapping, "table_name") or ""
                ),
                "source_record_key": source_key,
                "target_record_key": target_key,
                "foreign_key_column": str(
                    _relation_mapping_value(relation_mapping, "foreign_key_column") or ""
                ),
                "source_key_column": str(
                    _relation_mapping_value(relation_mapping, "source_key_column") or ""
                ),
                "target_key_column": str(
                    _relation_mapping_value(relation_mapping, "target_key_column") or ""
                ),
                "version": "relation-mapping-v1",
                "definition_snapshot_id": definition_provenance.get("snapshot_id"),
                "release_id": definition_provenance.get("release_id"),
                "definition_hash": str(definition_provenance.get("definition_hash") or ""),
                "definition_source": str(definition_provenance.get("source") or "live"),
                "connector_ref": connector_lineage(
                    resolved_relation_source,
                    (
                        relation_connector_audits.get(relation_mapping_id)
                        if mode == "join_table"
                        else mapping_connector_audits.get(
                            source_mapping_id if mode == "source_fk" else target_mapping_id
                        )
                    ),
                ),
            }
            db.add(
                RelationInstance(
                    scenario_id=scenario.id,
                    relation_id=relation_id,
                    source_instance_id=source_instance.id,
                    target_instance_id=target_instance.id,
                    source="mapping",
                    source_ref=(
                        f"{relation_mapping_id}:{runtime_environment}:"
                        f"{source_key}:{target_key}"
                    )[:500],
                    source_metadata=metadata,
                )
            )
            db.flush()
            existing_pairs.add(pair)
            rels_created += 1
        if relation_scan_complete:
            generated_links = db.execute(
                select(RelationInstance).where(
                    RelationInstance.relation_id == relation_id,
                    RelationInstance.source == "mapping",
                )
            ).scalars().all()
            for link in generated_links:
                metadata = link.source_metadata if isinstance(link.source_metadata, dict) else {}
                if (
                    str(metadata.get("relation_mapping_id") or "") != relation_mapping_id
                    or str(metadata.get("runtime_environment") or "dev") != runtime_environment
                    or (link.source_instance_id, link.target_instance_id) in desired_instance_pairs
                ):
                    continue
                validate_relation_instance_delete(db, relation, link)
                db.delete(link)
            db.flush()
        if isinstance(relation_mapping, RelationDataMapping):
            relation_mapping.status = "error" if relation_mapping_error else "ok"
            relation_mapping.last_error = relation_mapping_error
            relation_mapping.last_refreshed_at = datetime.now().astimezone()
            generated_links = db.execute(
                select(RelationInstance).where(
                    RelationInstance.relation_id == relation_id,
                    RelationInstance.source == "mapping",
                )
            ).scalars().all()
            relation_mapping.last_link_count = sum(
                1
                for link in generated_links
                if isinstance(link.source_metadata, dict)
                and str(link.source_metadata.get("relation_mapping_id") or "")
                == relation_mapping_id
                and str(link.source_metadata.get("runtime_environment") or "dev")
                == runtime_environment
            )
    # 后台任务需要原子提交“实例/关系 + 映射状态 + 任务终态”，不能在此提前
    # 提交出半完成同步；保留默认提交以兼容现有 seed/服务调用。
    if commit:
        db.commit()
    else:
        db.flush()
    return {
        "instances_created": len(created_instances),
        "instances_updated": updated_instances,
        "relations_created": rels_created,
        "rows_scanned": len(rows),
    }
