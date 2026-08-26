"""Deterministic ontology-property queries over governed ``DataMapping`` rows.

The caller supplies only semantic names.  Physical data-source ids, tables,
columns and SQL are selected from the current runtime definition and the
Agent's already-authorized mapping boundary.  This keeps the common query path
parameterized and reproducible. Raw SQL remains only in lower-level management
diagnostics and is deliberately not registered as a conversational Agent tool.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from ..config import get_settings
from . import datasource_service, ontology_service, permission_service


FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "contains",
        "starts_with",
        "ends_with",
        "is_null",
        "is_not_null",
    }
)
SORT_DIRECTIONS = frozenset({"asc", "desc"})
QUERY_ARGUMENTS = frozenset(
    {"entity_id", "entity_name", "properties", "filters", "sort", "limit", "offset"}
)
_ORDERED_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_TEXT_OPERATORS = frozenset({"contains", "starts_with", "ends_with"})
_SET_OPERATORS = frozenset({"in", "not_in"})
_NULL_OPERATORS = frozenset({"is_null", "is_not_null"})
_TYPE_ALIASES = {"number": "float"}


class MappedQueryError(ValueError):
    """A semantic query cannot be proven safe against the current mapping."""


@dataclass(frozen=True)
class MappedProperty:
    name: str
    column: str
    definition: Any
    transforms: tuple[dict[str, Any], ...]
    result_alias: str


@dataclass(frozen=True)
class MappedQueryPlan:
    entity: Any
    mapping: Any
    data_source: Any
    definition: Any
    properties: tuple[MappedProperty, ...]
    sql: str
    parameters: dict[str, Any]
    limit: int
    offset: int
    offset_explicit: bool
    normalized_request: dict[str, Any]

    @property
    def lineage(self) -> dict[str, Any]:
        return {
            "mapping_id": str(self.mapping.id),
            "data_source_id": str(self.data_source.id),
            "data_source_name": str(getattr(self.data_source, "name", "") or ""),
            "data_source_connector_revision": int(
                getattr(self.data_source, "connector_revision", 0) or 0
            ),
            "table": str(self.mapping.table_name),
            "definition": {
                "source": str(self.definition.source),
                "environment": str(self.definition.environment),
                "definition_hash": str(self.definition.definition_hash),
                "snapshot_id": self.definition.snapshot_id,
                "release_id": self.definition.release_id,
            },
        }


def _plain_mapping(value: Any, label: str) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        raise MappedQueryError(f"{label}必须是对象")
    return dict(value)


def _safe_identifier(value: Any, label: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise MappedQueryError(f"{label}必须是字符串")
    if not value or len(value) > maximum or value != value.strip():
        raise MappedQueryError(f"{label}为空或长度无效")
    if value == "*" or any(ord(character) < 32 for character in value):
        raise MappedQueryError(f"{label}包含不安全字符")
    return value


def quote_identifier(data_source_type: str, identifier: Any) -> str:
    """Quote one physical identifier for SQLite/PostgreSQL/MySQL.

    Identifier text never comes from the model, but quoting is still mandatory:
    mapping authors may legitimately use spaces/reserved words and a malformed
    governed definition must not create an injection primitive.
    """
    value = _safe_identifier(identifier, "映射列名")
    source_type = str(data_source_type or "").strip().lower()
    if source_type == "mysql":
        return "`" + value.replace("`", "``") + "`"
    if source_type in {"sqlite", "postgres"}:
        return '"' + value.replace('"', '""') + '"'
    raise MappedQueryError("确定性映射查询仅支持 sqlite、postgres 和 mysql 数据源")


def quote_table(data_source_type: str, table_name: Any) -> str:
    """Quote a possibly schema-qualified table, component by component."""
    value = _safe_identifier(table_name, "映射表名")
    parts = value.split(".")
    if len(parts) > 3 or any(not part or part != part.strip() for part in parts):
        raise MappedQueryError("映射表名不是有效的限定标识符")
    return ".".join(quote_identifier(data_source_type, part) for part in parts)


def _entity_from_request(definition: Any, request: Mapping[str, Any]) -> Any:
    entity_id = request.get("entity_id")
    entity_name = request.get("entity_name")
    if entity_id not in (None, "") and not isinstance(entity_id, str):
        raise MappedQueryError("entity_id 必须是字符串")
    if entity_name not in (None, "") and not isinstance(entity_name, str):
        raise MappedQueryError("entity_name 必须是字符串")
    entity_id = str(entity_id or "").strip()
    entity_name = str(entity_name or "").strip()
    if not entity_id and not entity_name:
        raise MappedQueryError("必须提供 entity_id 或 entity_name")

    by_id = definition.entities.get(entity_id) if entity_id else None
    by_name = [
        entity
        for entity in definition.entities.values()
        if str(getattr(entity, "name", "") or "") == entity_name
    ] if entity_name else []
    if entity_id and by_id is None:
        raise MappedQueryError("对象类型不存在或不属于当前运行定义")
    if entity_name and len(by_name) != 1:
        raise MappedQueryError("对象类型名称不存在或不唯一，请改用 entity_id")
    named = by_name[0] if by_name else None
    if by_id is not None and named is not None and str(by_id.id) != str(named.id):
        raise MappedQueryError("entity_id 与 entity_name 指向不同对象类型")
    return by_id or named


def _canonical_mapping(definition: Any, runtime_mapping: Any) -> Any:
    canonical = definition.mappings.get(str(getattr(runtime_mapping, "id", "") or ""))
    if canonical is None:
        raise MappedQueryError("Agent 数据映射不属于当前运行定义")
    for field in ("entity_id", "table_name", "column_map", "transform_rules"):
        if getattr(runtime_mapping, field, None) != getattr(canonical, field, None):
            raise MappedQueryError("Agent 数据映射与当前运行定义不一致")
    if definition.is_frozen:
        definition_source_id = str(
            getattr(runtime_mapping, "definition_data_source_id", "") or ""
        )
        if definition_source_id != str(getattr(canonical, "data_source_id", "") or ""):
            raise MappedQueryError("冻结映射缺少可验证的数据源定义血缘")
    elif str(getattr(runtime_mapping, "data_source_id", "") or "") != str(
        getattr(canonical, "data_source_id", "") or ""
    ):
        raise MappedQueryError("开发环境映射的数据源与当前运行定义不一致")
    return canonical


def _resolve_mapping_and_source(
    definition: Any,
    entity: Any,
    mappings: Sequence[Any],
    data_sources: Sequence[Any],
) -> tuple[Any, Any]:
    candidates = [
        mapping
        for mapping in mappings
        if str(getattr(mapping, "entity_id", "") or "") == str(entity.id)
    ]
    if not candidates:
        raise MappedQueryError("当前 Agent 没有绑定该对象类型的数据映射")
    if len(candidates) != 1:
        raise MappedQueryError("该对象类型存在多个已绑定数据映射，无法确定唯一查询来源")
    mapping = candidates[0]
    _canonical_mapping(definition, mapping)
    source_matches = [
        source
        for source in data_sources
        if str(getattr(source, "id", "") or "")
        == str(getattr(mapping, "data_source_id", "") or "")
    ]
    if len(source_matches) != 1:
        raise MappedQueryError("数据映射没有唯一的 Agent 运行数据源绑定")
    source = source_matches[0]
    scenario = definition.scenario
    if (
        str(getattr(source, "tenant_id", "") or "") != str(scenario.tenant_id)
        or getattr(source, "scenario_id", None) not in (None, scenario.id)
    ):
        raise MappedQueryError("数据映射的数据源不属于当前租户或业务场景")
    if str(getattr(source, "type", "") or "") not in {"sqlite", "postgres", "mysql"}:
        raise MappedQueryError("确定性映射查询仅支持 sqlite、postgres 和 mysql 数据源")
    _safe_identifier(getattr(mapping, "table_name", None), "映射表名")
    return mapping, source


def _visible_properties(db: Session, entity: Any) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for prop in list(getattr(entity, "properties", []) or []):
        name = str(getattr(prop, "name", "") or "")
        if not name or name in properties:
            raise MappedQueryError("对象类型包含空白或重复属性名")
        if permission_service.can_read_property(db, prop):
            properties[name] = prop
    return properties


def _mapped_property(
    property_name: Any,
    *,
    visible: Mapping[str, Any],
    column_map: Mapping[str, Any],
) -> tuple[str, Any, str]:
    if not isinstance(property_name, str) or not property_name.strip():
        raise MappedQueryError("属性名必须是非空字符串")
    name = property_name.strip()
    prop = visible.get(name)
    column = column_map.get(name)
    if prop is None or not isinstance(column, str) or not column.strip():
        # Keep nonexistent, unmapped and ACL-hidden properties indistinguishable.
        raise MappedQueryError(f"属性“{name}”不存在、未映射或无读取权限")
    return name, prop, _safe_identifier(column, f"属性“{name}”的映射列名")


def _property_type(prop: Any) -> str:
    kind = str(getattr(prop, "data_type", "string") or "string").strip().lower()
    return _TYPE_ALIASES.get(kind, kind)


def _normalize_scalar(prop: Any, value: Any) -> Any:
    name = str(getattr(prop, "name", "") or "")
    kind = _property_type(prop)
    if value is None or isinstance(value, (dict, list)):
        raise MappedQueryError(f"属性“{name}”的过滤值必须是非空标量")
    if kind in {"string", "text"}:
        if not isinstance(value, str) or len(value) > 10_000 or "\x00" in value:
            raise MappedQueryError(f"属性“{name}”的过滤值必须是有效文本")
        normalized = value
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是整数")
        normalized = value
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是数值")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是有限数值")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是布尔值")
        normalized = value
    elif kind == "date":
        if not isinstance(value, str):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是 ISO 日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise MappedQueryError(f"属性“{name}”的过滤值必须是 ISO 日期") from exc
        normalized = value
    elif kind == "datetime":
        if not isinstance(value, str):
            raise MappedQueryError(f"属性“{name}”的过滤值必须是 ISO 日期时间")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MappedQueryError(f"属性“{name}”的过滤值必须是 ISO 日期时间") from exc
        normalized = value
    else:
        raise MappedQueryError(f"属性“{name}”的 {kind} 类型不支持结构化过滤")
    if bool(getattr(prop, "is_enum", False)) and str(normalized) not in {
        str(item) for item in (getattr(prop, "enum_values", []) or [])
    }:
        raise MappedQueryError(f"属性“{name}”的过滤值不在枚举范围内")
    return normalized


def _normalize_filter(
    raw: Any,
    *,
    visible: Mapping[str, Any],
    column_map: Mapping[str, Any],
    transformed_properties: set[str],
) -> dict[str, Any]:
    item = _plain_mapping(raw, "过滤条件")
    unknown = set(item) - {"property", "op", "value"}
    if unknown:
        raise MappedQueryError("过滤条件包含不支持的字段")
    name, prop, column = _mapped_property(
        item.get("property"), visible=visible, column_map=column_map
    )
    op = str(item.get("op") or "").strip()
    if op not in FILTER_OPERATORS:
        raise MappedQueryError("过滤运算符不受支持")
    if name in transformed_properties:
        raise MappedQueryError(
            f"属性“{name}”配置了转换规则，当前不能保证源端过滤与本体语义等价"
        )
    kind = _property_type(prop)
    if op in _ORDERED_OPERATORS and kind not in {"integer", "float", "date", "datetime"}:
        raise MappedQueryError(f"属性“{name}”的类型不支持有序比较")
    if op in _TEXT_OPERATORS and kind not in {"string", "text"}:
        raise MappedQueryError(f"属性“{name}”的类型不支持文本匹配")
    if op in _NULL_OPERATORS:
        if "value" in item and item["value"] is not None:
            raise MappedQueryError(f"{op} 运算符不能携带过滤值")
        return {"property": name, "column": column, "op": op}
    if "value" not in item:
        raise MappedQueryError(f"{op} 运算符必须提供过滤值")
    if op in _SET_OPERATORS:
        raw_values = item["value"]
        if not isinstance(raw_values, list) or not 1 <= len(raw_values) <= 100:
            raise MappedQueryError(f"{op} 的过滤值必须是 1 到 100 项的列表")
        values = [_normalize_scalar(prop, value) for value in raw_values]
        return {"property": name, "column": column, "op": op, "value": values}
    value = _normalize_scalar(prop, item["value"])
    if op in _TEXT_OPERATORS and value == "":
        raise MappedQueryError("文本匹配的过滤值不能为空")
    return {"property": name, "column": column, "op": op, "value": value}


def _normalize_sort(
    raw: Any,
    *,
    visible: Mapping[str, Any],
    column_map: Mapping[str, Any],
    transformed_properties: set[str],
) -> dict[str, str]:
    item = _plain_mapping(raw, "排序条件")
    if set(item) - {"property", "direction"}:
        raise MappedQueryError("排序条件包含不支持的字段")
    name, _prop, column = _mapped_property(
        item.get("property"), visible=visible, column_map=column_map
    )
    if name in transformed_properties:
        raise MappedQueryError(
            f"属性“{name}”配置了转换规则，当前不能保证源端排序与本体语义等价"
        )
    direction = str(item.get("direction") or "asc").strip().lower()
    if direction not in SORT_DIRECTIONS:
        raise MappedQueryError("排序方向只能是 asc 或 desc")
    return {"property": name, "column": column, "direction": direction}


def _like_value(op: str, value: str) -> str:
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    if op == "contains":
        return f"%{escaped}%"
    if op == "starts_with":
        return f"{escaped}%"
    return f"%{escaped}"


def _compile_sql(
    source_type: str,
    table_name: str,
    selected: Sequence[MappedProperty],
    filters: Sequence[Mapping[str, Any]],
    sort: Sequence[Mapping[str, str]],
    limit: int,
    offset: int,
) -> tuple[str, dict[str, Any]]:
    select_sql = ", ".join(
        f"{quote_identifier(source_type, item.column)} AS "
        f"{quote_identifier(source_type, item.result_alias)}"
        for item in selected
    )
    sql = f"SELECT {select_sql} FROM {quote_table(source_type, table_name)}"
    clauses: list[str] = []
    params: dict[str, Any] = {}
    binary_operators = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    for index, item in enumerate(filters):
        column = quote_identifier(source_type, item["column"])
        op = item["op"]
        if op == "is_null":
            clauses.append(f"{column} IS NULL")
        elif op == "is_not_null":
            clauses.append(f"{column} IS NOT NULL")
        elif op in _SET_OPERATORS:
            names: list[str] = []
            for value_index, value in enumerate(item["value"]):
                parameter_name = f"mq_{index}_{value_index}"
                names.append(f":{parameter_name}")
                params[parameter_name] = value
            keyword = "IN" if op == "in" else "NOT IN"
            clauses.append(f"{column} {keyword} ({', '.join(names)})")
        else:
            parameter_name = f"mq_{index}"
            value = item["value"]
            if op in _TEXT_OPERATORS:
                clauses.append(f"{column} LIKE :{parameter_name} ESCAPE '!' ")
                params[parameter_name] = _like_value(op, value)
            else:
                clauses.append(f"{column} {binary_operators[op]} :{parameter_name}")
                params[parameter_name] = value
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if sort:
        sql += " ORDER BY " + ", ".join(
            f"{quote_identifier(source_type, item['column'])} {item['direction'].upper()}"
            for item in sort
        )
    params["mq_limit"] = limit + 1
    params["mq_offset"] = offset
    sql += " LIMIT :mq_limit OFFSET :mq_offset"
    return sql, params


def prepare_query(
    db: Session,
    *,
    definition: Any,
    mappings: Sequence[Any],
    data_sources: Sequence[Any],
    args: Any,
) -> MappedQueryPlan:
    """Validate semantic input and compile one immutable, parameterized plan."""
    if definition is None:
        raise MappedQueryError("当前 Agent 没有可验证的运行定义")
    request = _plain_mapping(args, "映射查询参数")
    unknown = set(request) - QUERY_ARGUMENTS
    if unknown:
        raise MappedQueryError(
            "映射查询不接受 SQL、数据源、表、列或其他物理字段"
        )
    entity = _entity_from_request(definition, request)
    mapping, source = _resolve_mapping_and_source(
        definition, entity, mappings, data_sources
    )
    if not isinstance(getattr(mapping, "column_map", None), Mapping):
        raise MappedQueryError("数据映射的属性列配置无效")
    column_map = dict(mapping.column_map)
    visible = _visible_properties(db, entity)
    raw_properties = request.get("properties")
    if not isinstance(raw_properties, list) or not 1 <= len(raw_properties) <= 50:
        raise MappedQueryError("properties 必须是 1 到 50 个属性名的列表")
    selected_parts = [
        _mapped_property(name, visible=visible, column_map=column_map)
        for name in raw_properties
    ]
    selected_names = [item[0] for item in selected_parts]
    if len(selected_names) != len(set(selected_names)):
        raise MappedQueryError("properties 不能包含重复属性")
    try:
        transforms = ontology_service.normalize_transform_rules(
            entity, getattr(mapping, "transform_rules", {}) or {}
        )
    except ValueError as exc:
        raise MappedQueryError(f"数据映射转换规则无效：{exc}") from exc
    transformed_properties = {
        name for name, rules in transforms.items() if rules
    }
    selected = tuple(
        MappedProperty(
            name=name,
            column=column,
            definition=prop,
            transforms=tuple(transforms.get(name, [])),
            result_alias=f"__ontology_{index}",
        )
        for index, (name, prop, column) in enumerate(selected_parts)
    )

    raw_filters = request.get("filters", [])
    if not isinstance(raw_filters, list) or len(raw_filters) > 20:
        raise MappedQueryError("filters 必须是不超过 20 项的列表")
    filters = [
        _normalize_filter(
            item,
            visible=visible,
            column_map=column_map,
            transformed_properties=transformed_properties,
        )
        for item in raw_filters
    ]
    raw_sort = request.get("sort", [])
    if not isinstance(raw_sort, list) or len(raw_sort) > 5:
        raise MappedQueryError("sort 必须是不超过 5 项的列表")
    sort = [
        _normalize_sort(
            item,
            visible=visible,
            column_map=column_map,
            transformed_properties=transformed_properties,
        )
        for item in raw_sort
    ]
    if len({item["property"] for item in sort}) != len(sort):
        raise MappedQueryError("sort 不能包含重复属性")

    max_rows = max(1, int(get_settings().max_query_rows))
    raw_limit = request.get("limit", min(50, max_rows))
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise MappedQueryError("limit 必须是整数")
    if not 1 <= raw_limit <= max_rows:
        raise MappedQueryError(f"limit 必须介于 1 和 {max_rows} 之间")
    offset_explicit = "offset" in request
    raw_offset = request.get("offset", 0)
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
        raise MappedQueryError("offset 必须是非负整数")
    sql, parameters = _compile_sql(
        str(source.type),
        str(mapping.table_name),
        selected,
        filters,
        sort,
        raw_limit,
        raw_offset,
    )
    normalized_request = {
        "entity_id": str(entity.id),
        "entity_name": str(entity.name),
        "properties": selected_names,
        "filters": [
            {key: value for key, value in item.items() if key != "column"}
            for item in filters
        ],
        "sort": [
            {key: value for key, value in item.items() if key != "column"}
            for item in sort
        ],
        "limit": raw_limit,
        "offset": raw_offset,
    }
    return MappedQueryPlan(
        entity=entity,
        mapping=mapping,
        data_source=source,
        definition=definition,
        properties=selected,
        sql=sql,
        parameters=parameters,
        limit=raw_limit,
        offset=raw_offset,
        offset_explicit=offset_explicit,
        normalized_request=normalized_request,
    )


def execute_query(plan: MappedQueryPlan) -> dict[str, Any]:
    """Execute a prepared plan and project rows back to ontology property names."""
    try:
        raw = datasource_service.run_parameterized_query(
            plan.data_source,
            plan.sql,
            plan.parameters,
            limit=plan.limit,
        )
    except Exception as exc:  # noqa: BLE001 - connector diagnostics may contain secrets.
        raise MappedQueryError("映射查询执行失败，请检查数据源连接与映射定义") from exc
    if not isinstance(raw, Mapping):
        raise MappedQueryError("映射查询返回了无效结果")
    aliases = [item.result_alias for item in plan.properties]
    if list(raw.get("columns") or []) != aliases:
        raise MappedQueryError("映射查询返回列与已固定查询计划不一致")
    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > plan.limit:
        raise MappedQueryError("映射查询返回行数无效")
    objects: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) != len(plan.properties):
            raise MappedQueryError("映射查询返回行结构与已固定查询计划不一致")
        projected: dict[str, Any] = {}
        for item, value in zip(plan.properties, raw_row, strict=True):
            try:
                projected[item.name] = ontology_service.apply_transform_rules(
                    value, list(item.transforms)
                )
            except (TypeError, ValueError) as exc:
                raise MappedQueryError(
                    f"属性“{item.name}”的数据转换失败，请检查映射规则或源数据"
                ) from exc
        objects.append(projected)
    truncated = bool(raw.get("truncated", False))
    return {
        "entity": {"id": str(plan.entity.id), "name": str(plan.entity.name)},
        "properties": [item.name for item in plan.properties],
        "objects": objects,
        "row_count": len(objects),
        "truncated": truncated,
        "offset": plan.offset,
        "next_offset": plan.offset + len(objects) if truncated else None,
        "lineage": plan.lineage,
    }


def query_mapped_objects(
    db: Session,
    *,
    definition: Any,
    mappings: Sequence[Any],
    data_sources: Sequence[Any],
    args: Any,
) -> dict[str, Any]:
    plan = prepare_query(
        db,
        definition=definition,
        mappings=mappings,
        data_sources=data_sources,
        args=args,
    )
    return execute_query(plan)


def authorize_historic_result(plan: MappedQueryPlan, result: Any) -> bool:
    """Re-authorize a persisted semantic result without re-running its query."""
    if not isinstance(result, Mapping):
        return False
    expected_names = [item.name for item in plan.properties]
    if result.get("entity") != {"id": str(plan.entity.id), "name": str(plan.entity.name)}:
        return False
    if result.get("properties") != expected_names or result.get("lineage") != plan.lineage:
        return False
    objects = result.get("objects")
    if not isinstance(objects, list) or len(objects) > plan.limit:
        return False
    if any(not isinstance(item, Mapping) or list(item) != expected_names for item in objects):
        return False
    row_count = result.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count != len(objects):
        return False
    truncated = result.get("truncated")
    if not isinstance(truncated, bool):
        return False
    has_offset = "offset" in result
    has_next_offset = "next_offset" in result
    if not has_offset and not has_next_offset:
        return not plan.offset_explicit and plan.offset == 0
    if has_offset != has_next_offset:
        return False
    result_offset = result.get("offset")
    if (
        isinstance(result_offset, bool)
        or not isinstance(result_offset, int)
        or result_offset != plan.offset
    ):
        return False
    expected_next_offset = plan.offset + len(objects) if truncated else None
    next_offset = result.get("next_offset")
    if expected_next_offset is None:
        return next_offset is None
    return (
        isinstance(next_offset, int)
        and not isinstance(next_offset, bool)
        and next_offset == expected_next_offset
    )
