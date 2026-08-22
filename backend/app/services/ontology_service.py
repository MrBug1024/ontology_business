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
    OntologyRelation,
    RelationInstance,
)
from . import datasource_service, llm_service, permission_service, tenant_service


_NAMESPACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,179}$")
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
    "minimum",
    "maximum",
    "exclusive_minimum",
    "exclusive_maximum",
    "min_length",
    "max_length",
    "pattern",
    "format",
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
    numeric_keys = {"minimum", "maximum", "exclusive_minimum", "exclusive_maximum"}
    text_keys = {"min_length", "max_length", "pattern", "format"}
    incompatible = (
        (set(constraints or {}) - numeric_keys)
        if kind in {"integer", "float", "number"}
        else (set(constraints or {}) - text_keys)
        if kind in {"string", "text", "date", "datetime"}
        else set(constraints or {})
    )
    if incompatible:
        raise ValueError(
            f"{kind} 类型不支持约束：{'、'.join(sorted(incompatible))}"
        )
    result: dict[str, Any] = {}
    for key, value in (constraints or {}).items():
        if key in {"minimum", "maximum", "exclusive_minimum", "exclusive_maximum"}:
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
    properties = list(getattr(payload, "properties", []) or [])
    names = [str(prop.name).strip() for prop in properties]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("实体属性名不能为空或重复")
    key_count = sum(1 for prop in properties if bool(prop.is_key))
    if key_count > 1:
        raise ValueError("一个实体最多只能有一个主键属性")
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

    if mode == "instance":
        inst_map = {
            instance.id: instance
            for instance in scenario.instances
            if permission_service.check_object(db, instance, "read").allowed
        }
        ent_map = {e.id: e for e in scenario.entities}
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
        rel_map = {r.id: r for r in scenario.relations}
        for ri in scenario.relation_instances:
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
        for e in scenario.entities:
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
        for r in scenario.relations:
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
ONTOLOGY_RELATION_TYPES = {"1:1", "1:N", "N:M"}

_GEN_PROMPT = """你是资深业务架构师，擅长为任意行业构建本体（Ontology）模型。
请完整阅读下面的业务描述，设计一套忠实、通用、可扩展的本体模型。输入已在服务端完成完整性边界校验，不要只处理开头部分。

要求：
1. 实体（entities）：覆盖文档明确描述的全部核心业务对象，数量由业务内容决定，不设 8 个上限；不要为了简洁遗漏文档中的稳定业务概念，也不要凭空发明概念。
2. 每个实体覆盖文档明确要求的关键属性（properties），数量由业务内容决定。属性名用中文，data_type 只能是：string / integer / float / boolean / date / datetime / json / text；需要状态机时使用 is_enum/enum_values，并在实体 state_property 指向该枚举属性。文档明确给出默认值或敏感字段时，分别写入 default_value、is_sensitive；没有依据时不要猜测。
3. 每个实体必须恰好有 1 个 is_key=true 的主键属性。
4. 关系（relations）：覆盖文档明确描述的实体关系，数量由业务内容决定；relation_type 只能是 1:1 / 1:N / N:M。
5. 对名称、枚举、约束或关系方向没有明确依据时采用保守表达，不把数据表或字段机械等同于业务实体。
6. 只输出 JSON，不要输出任何解释文字。

输出格式（严格 JSON）：
{
  "entities": [
    {"name": "业务对象", "description": "业务领域中的核心对象", "is_abstract": false, "state_property": "",
     "properties": [{"name": "对象ID", "data_type": "string", "is_key": true, "is_required": true, "is_enum": false, "enum_values": [], "default_value": "", "constraints": {}, "is_sensitive": false}, ...]}
  ],
  "relations": [
    {"name": "关联", "source": "业务对象", "target": "相关对象", "relation_type": "1:N", "description": ""}
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
    entities: list[dict[str, Any]] = []
    for raw_entity in data.get("entities") or []:
        if not isinstance(raw_entity, dict):
            continue
        name = str(raw_entity.get("name") or "").strip()
        if not name or name in generated_entity_names:
            continue
        generated_entity_names.add(name)
        properties: list[dict[str, Any]] = []
        property_names: set[str] = set()
        key_seen = False
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
            is_enum = bool(raw_property.get("is_enum", False))
            enum_values = [str(item) for item in (raw_property.get("enum_values") or [])]
            properties.append(
                {
                    "name": property_name,
                    "data_type": data_type,
                    "description": str(raw_property.get("description") or ""),
                    "is_key": is_key,
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
        state_property = str(raw_entity.get("state_property") or "").strip()
        if state_property and not any(
            prop["name"] == state_property and prop["is_enum"] for prop in properties
        ):
            state_property = ""
        entities.append(
            {
                "name": name,
                "description": str(raw_entity.get("description") or ""),
                "is_abstract": bool(raw_entity.get("is_abstract", False)),
                "state_property": state_property,
                "properties": properties,
            }
        )

    all_entity_names = known_entity_names | generated_entity_names
    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[str, str, str]] = set()
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
        relations.append(
            {
                "name": name,
                "source": source,
                "target": target,
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
            entities_skipped += 1
        else:
            ent = OntologyEntity(
                scenario_id=scenario.id,
                name=e["name"],
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
        for p in e.get("properties", []):
            if p["name"] in existing_properties:
                properties_skipped += 1
                continue
            from ..models import OntologyProperty

            is_key = bool(p.get("is_key", False)) and not has_key
            prop = OntologyProperty(
                entity_id=ent.id,
                name=p["name"],
                data_type=p.get("data_type", "string"),
                description=p.get("description", ""),
                is_key=is_key,
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
        db.add(
            OntologyRelation(
                scenario_id=scenario.id,
                name=r["name"],
                source_entity_id=src.id,
                target_entity_id=tgt.id,
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
    ent = db.get(OntologyEntity, mapping.entity_id)
    if not ent or ent.scenario_id != scenario.id:
        raise ValueError("映射对应的实体不存在或不属于当前业务场景")
    if not mapping.table_name.strip():
        raise ValueError("请先选择映射的表")
    return ds, ent


def _quoted_mapping_table(table_name: str) -> str:
    """只接受简单表名或 schema.table，避免映射表名拼接出多语句 SQL。"""
    parts = [part.strip() for part in table_name.split(".")]
    if not parts or len(parts) > 2 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", part) for part in parts):
        raise ValueError("表名格式不合法，请重新选择数据源中的表")
    return ".".join(f'"{part}"' for part in parts)


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
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name)}",
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
) -> dict[str, Any]:
    """按数据映射增量同步实例，并写入可审计的来源快照。

    ``source_ref`` 保留短小可读的引用；精确且不会串源的映射标识、运行环境、
    数据源、表和记录键写在 ``source_metadata``。这样同一个实体由多个数据源/
    映射/部署环境同步时也不会互相覆盖，未变记录则可安全复用既有实例。
    """
    ds, ent = _mapping_context(db, scenario, mapping, data_source=data_source)
    runtime_environment = str(environment or "dev").strip().lower() or "dev"
    col_map = mapping.column_map or {}
    transform_rules = normalize_transform_rules(ent, getattr(mapping, "transform_rules", {}) or {})

    result = datasource_service.run_query(
        ds,
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name)}",
        limit=limit,
    )
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    if not rows:
        # 空表是一次成功的无变更刷新，而不是需要重试的连接器失败。
        # 这也与 preview_mapping 的“暂无数据”提示语义保持一致。
        return {
            "instances_created": 0,
            "instances_updated": 0,
            "relations_created": 0,
            "rows_scanned": 0,
        }

    # 主键属性
    key_prop = next((p.name for p in ent.properties if p.is_key), None)
    key_col = col_map.get(key_prop) if key_prop else None

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
        display = str(rec.get(key_col) or attrs.get(key_prop) or f"{ent.name}-{len(created_instances) + 1}")
        metadata = {
            "mapping_id": mapping.id,
            "runtime_environment": runtime_environment,
            "data_source_id": ds.id,
            "table_name": mapping.table_name,
            "key_column": key_col or "",
            "record_key": record_key,
            "transform_rules": transform_rules,
            "version": "mapping-v2",
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

    # 自动推断关系实例：本表若存在指向其他实体主键列的列，则建立关系
    rels_created = 0
    other_mappings = db.execute(
        select(DataMapping).where(
            DataMapping.scenario_id == scenario.id,
            DataMapping.entity_id != ent.id,
        )
    ).scalars().all()
    for om in other_mappings:
        oent = db.get(OntologyEntity, om.entity_id)
        ods = db.get(DataSource, om.data_source_id)
        if not oent or not ods or ods.type == "file_bucket" or not om.table_name:
            continue
        # 找关系：oent -> ent 或 ent -> oent
        rel = None
        for r in scenario.relations:
            if r.source_entity_id == oent.id and r.target_entity_id == ent.id:
                rel = r
                break
            if r.source_entity_id == ent.id and r.target_entity_id == oent.id:
                rel = r
                break
        if not rel:
            continue
        okey_prop = next((p.name for p in oent.properties if p.is_key), None)
        okey_col = om.column_map.get(okey_prop) if okey_prop else None
        if not okey_col:
            continue
        # 本表中指向 oent 主键的列（列名相同或 okey_col 去掉/加上 _id）
        fk_col = None
        for col in columns:
            if col == okey_col or col == okey_col + "_id" or col.replace("_id", "") == okey_col.replace("_id", ""):
                fk_col = col
                break
        if not fk_col:
            continue
        # 其他实体的已导入实例：key 值 → 实例
        oexisting = db.execute(
            select(OntologyInstance).where(
                OntologyInstance.entity_id == oent.id,
                OntologyInstance.source == "imported",
            )
        ).scalars().all()
        okey_by_value: dict[str, OntologyInstance] = {}
        for inst in oexisting:
            metadata = inst.source_metadata or {}
            metadata_environment = (
                str(metadata.get("runtime_environment") or "dev").strip().lower()
                if isinstance(metadata, dict)
                else ""
            )
            if (
                isinstance(metadata, dict)
                and metadata.get("mapping_id") == om.id
                and metadata_environment == runtime_environment
            ):
                value = metadata.get("record_key")
                if value is not None:
                    okey_by_value[str(value)] = inst
                    continue
            # 兼容尚未刷新过的旧对象来源标识。
            legacy_prefix = f"{om.table_name}:"
            if runtime_environment == "dev" and (inst.source_ref or "").startswith(legacy_prefix):
                okey_by_value[inst.source_ref[len(legacy_prefix):]] = inst

        for row, ent_inst in zip(rows, row_instances):
            rec = dict(zip(columns, row))
            fk_val = rec.get(fk_col)
            if fk_val is None:
                continue
            o_inst = okey_by_value.get(str(fk_val))
            if not ent_inst or not o_inst:
                continue
            if rel.source_entity_id == oent.id:
                s, t = o_inst, ent_inst
            else:
                s, t = ent_inst, o_inst
            exists = db.execute(
                select(RelationInstance).where(
                    RelationInstance.relation_id == rel.id,
                    RelationInstance.source_instance_id == s.id,
                    RelationInstance.target_instance_id == t.id,
                )
            ).scalars().first()
            if not exists:
                db.add(
                    RelationInstance(
                        scenario_id=scenario.id,
                        relation_id=rel.id,
                        source_instance_id=s.id,
                        target_instance_id=t.id,
                    )
                )
                rels_created += 1
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
