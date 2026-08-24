"""Governed business-data queries for Agent conversations.

The Agent speaks in ontology entities and properties.  This module turns that
request into a server-owned, parameterized query plan.  It deliberately keeps
physical table/column names, join syntax and connector details out of the
model-facing contract while still supporting the operations that real business
work needs: filtering, joining related objects, grouping and aggregation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from . import datasource_service, mapped_query_service, ontology_service


class BusinessQueryError(ValueError):
    """The requested business query cannot be proven against the mapping."""


_AGGREGATIONS = frozenset({"count", "sum", "avg", "min", "max"})
_HAVING_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"})


@dataclass(frozen=True)
class _SourcePlan:
    entity: Any
    mapping: Any
    source: Any
    visible: dict[str, Any]
    transforms: dict[str, tuple[dict[str, Any], ...]]


@dataclass(frozen=True)
class _Column:
    entity_key: str
    entity_name: str
    property_name: str
    column: str
    source_alias: str
    label: str
    expression: str
    transforms: tuple[dict[str, Any], ...]


def _object(value: Any, label: str) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        raise BusinessQueryError(f"{label}必须是对象")
    return dict(value)


def _entity_object(value: Any, label: str) -> dict[str, Any]:
    """Normalize the model-friendly entity shorthand into one entity request.

    The public tool schema accepts either ``{"entity_id": ...}`` /
    ``{"entity_name": ...}`` or a bare display name.  Keeping this small
    compatibility normalization at the semantic boundary prevents a harmless
    model shorthand from falling through to a misleading ``must be object``
    error, while all actual entity resolution remains server-owned.
    """
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise BusinessQueryError(f"{label}必须是非空对象名称或对象引用")
        return {"entity_name": name}
    return _object(value, label)


def _list(value: Any, label: str, *, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise BusinessQueryError(f"{label}必须是不超过 {maximum} 项的列表")
    return value


def _entity_request(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    entity_id = str(value.get("entity_id") or "").strip()
    entity_name = str(value.get("entity_name") or "").strip()
    if not entity_id and not entity_name:
        raise BusinessQueryError(f"{label}必须提供 entity_id 或 entity_name")
    return {"entity_id": entity_id, "entity_name": entity_name}


def _transforms(entity: Any, mapping: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    try:
        normalized = ontology_service.normalize_transform_rules(
            entity, getattr(mapping, "transform_rules", {}) or {}
        )
    except ValueError as exc:
        raise BusinessQueryError(f"对象“{entity.name}”的数据转换规则无效：{exc}") from exc
    return {
        str(name): tuple(rule for rule in rules if isinstance(rule, Mapping))
        for name, rules in normalized.items()
    }


def _source_plan(
    db: Session,
    *,
    definition: Any,
    mappings: Sequence[Any],
    data_sources: Sequence[Any],
    request: Mapping[str, Any],
    label: str,
) -> _SourcePlan:
    try:
        entity = mapped_query_service._entity_from_request(definition, _entity_request(request, label))
        mapping, source = mapped_query_service._resolve_mapping_and_source(
            definition, entity, mappings, data_sources
        )
        visible = mapped_query_service._visible_properties(db, entity)
        if not isinstance(getattr(mapping, "column_map", None), Mapping):
            raise BusinessQueryError(f"对象“{entity.name}”的数据映射无效")
        return _SourcePlan(
            entity=entity,
            mapping=mapping,
            source=source,
            visible=visible,
            transforms=_transforms(entity, mapping),
        )
    except BusinessQueryError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize internal mapping errors.
        raise BusinessQueryError(str(exc)) from exc


def _mapped_property(plan: _SourcePlan, name: Any) -> tuple[str, Any, str]:
    try:
        return mapped_query_service._mapped_property(
            name,
            visible=plan.visible,
            column_map=dict(plan.mapping.column_map),
        )
    except Exception as exc:  # noqa: BLE001 - expose a stable business error.
        raise BusinessQueryError(str(exc)) from exc


def _normalize_filters(plan: _SourcePlan, value: Any) -> list[dict[str, Any]]:
    transformed = {name for name, rules in plan.transforms.items() if rules}
    try:
        raw = _list(value, "过滤条件", maximum=20)
        return [
            mapped_query_service._normalize_filter(
                item,
                visible=plan.visible,
                column_map=dict(plan.mapping.column_map),
                transformed_properties=transformed,
            )
            for item in raw
        ]
    except BusinessQueryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BusinessQueryError(str(exc)) from exc


def _normalize_limit(value: Any) -> int:
    maximum = max(1, int(get_settings().max_query_rows))
    if value is None:
        return min(100, maximum)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise BusinessQueryError(f"limit 必须介于 1 和 {maximum} 之间")
    return value


def _alias(source_type: str, value: str) -> str:
    return mapped_query_service.quote_identifier(source_type, value)


def _column(
    plan: _SourcePlan,
    property_name: Any,
    source_alias: str,
    label: str,
    *,
    allow_transform: bool = False,
) -> _Column:
    name, _prop, physical = _mapped_property(plan, property_name)
    transformed = plan.transforms.get(name, ())
    if transformed and not allow_transform:
        raise BusinessQueryError(
            f"属性“{name}”配置了转换规则，当前跨对象查询不能保证源端计算语义等价"
        )
    source_type = str(plan.source.type)
    expression = f"{_alias(source_type, source_alias)}.{mapped_query_service.quote_identifier(source_type, physical)}"
    return _Column(
        entity_key=str(plan.entity.id),
        entity_name=str(plan.entity.name),
        property_name=name,
        column=physical,
        source_alias=source_alias,
        label=label,
        expression=expression,
        transforms=tuple(transformed),
    )


def _physical_column(
    plan: _SourcePlan,
    physical: Any,
    source_alias: str,
    *,
    property_name: str = "",
) -> _Column:
    """Build a join expression from a server-owned mapping column.

    Relation mappings are authored and validated by the scenario service.  The
    query planner still quotes the value here and keeps it out of the model
    contract, so a malformed historic definition fails closed at execution.
    """
    value = str(physical or "").strip()
    if not value:
        raise BusinessQueryError("关系映射缺少已验证的关联列")
    source_type = str(plan.source.type)
    expression = (
        f"{_alias(source_type, source_alias)}."
        f"{mapped_query_service.quote_identifier(source_type, value)}"
    )
    return _Column(
        entity_key=str(plan.entity.id),
        entity_name=str(plan.entity.name),
        property_name=property_name,
        column=value,
        source_alias=source_alias,
        label="",
        expression=expression,
        transforms=(),
    )


def _key_column(plan: _SourcePlan) -> str:
    keys = [prop for prop in plan.visible.values() if bool(getattr(prop, "is_key", False))]
    if len(keys) != 1:
        raise BusinessQueryError(
            f"对象“{plan.entity.name}”必须且只能有一个可见主键，才能使用关系数据映射"
        )
    key_name = str(keys[0].name)
    physical = (plan.mapping.column_map or {}).get(key_name)
    if not physical:
        raise BusinessQueryError(
            f"对象“{plan.entity.name}”的主键属性“{key_name}”尚未映射，无法使用关系数据映射"
        )
    return str(physical)


def _configured_relation_mapping(
    definition: Any,
    base: _SourcePlan,
    related: _SourcePlan,
) -> Any | None:
    """Resolve one visible, configured relation between two object mappings."""
    mapping_ids = {str(base.mapping.id), str(related.mapping.id)}
    candidates = []
    for item in (getattr(definition, "relation_mappings", {}) or {}).values():
        endpoint_ids = {
            str(getattr(item, "source_mapping_id", "") or ""),
            str(getattr(item, "target_mapping_id", "") or ""),
        }
        if endpoint_ids == mapping_ids:
            candidates.append(item)
    if len(candidates) > 1:
        raise BusinessQueryError(
            f"对象“{base.entity.name}”与“{related.entity.name}”存在多个关系数据映射，无法确定唯一关联"
        )
    return candidates[0] if candidates else None


def _join_columns(
    base: _SourcePlan,
    related: _SourcePlan,
    join: Any,
    *,
    base_alias: str,
    related_alias: str,
) -> tuple[_Column, _Column]:
    join_data = _object(join, "关联条件") if join is not None else {}
    if set(join_data) - {"base_property", "related_property"}:
        raise BusinessQueryError("关联条件只支持 base_property 和 related_property")
    if join_data:
        left = _column(base, join_data.get("base_property"), base_alias, "")
        right = _column(related, join_data.get("related_property"), related_alias, "")
        return left, right

    base_columns = {
        str(column): name
        for name, column in (base.mapping.column_map or {}).items()
        if name in base.visible and column
    }
    related_columns = {
        str(column): name
        for name, column in (related.mapping.column_map or {}).items()
        if name in related.visible and column
    }
    shared = sorted(set(base_columns) & set(related_columns))
    if len(shared) != 1:
        raise BusinessQueryError(
            f"对象“{base.entity.name}”与“{related.entity.name}”无法唯一确定关联字段；"
            "请在数据关系映射中配置关联条件"
        )
    physical = shared[0]
    return (
        _column(base, base_columns[physical], base_alias, ""),
        _column(related, related_columns[physical], related_alias, ""),
    )


def _relation_join_sql(
    definition: Any,
    base: _SourcePlan,
    related: _SourcePlan,
    join: Any,
    *,
    base_alias: str,
    related_alias: str,
    through_alias: str,
) -> list[str]:
    """Compile one governed relationship into one or two JOIN clauses."""
    join_data = _object(join, "关联条件") if join is not None else {}
    if join_data:
        left, right = _join_columns(
            base,
            related,
            join_data,
            base_alias=base_alias,
            related_alias=related_alias,
        )
        source_type = str(base.source.type)
        return [
            f" JOIN {mapped_query_service.quote_table(source_type, related.mapping.table_name)}"
            f" {_alias(source_type, related_alias)} ON {left.expression} = {right.expression}"
        ]

    configured = _configured_relation_mapping(definition, base, related)
    if configured is None:
        left, right = _join_columns(
            base,
            related,
            None,
            base_alias=base_alias,
            related_alias=related_alias,
        )
        source_type = str(base.source.type)
        return [
            f" JOIN {mapped_query_service.quote_table(source_type, related.mapping.table_name)}"
            f" {_alias(source_type, related_alias)} ON {left.expression} = {right.expression}"
        ]

    status = str(getattr(configured, "status", "") or "").lower()
    if status in {"error", "failed", "invalid"}:
        raise BusinessQueryError(
            f"对象“{base.entity.name}”与“{related.entity.name}”的关系数据映射未就绪："
            f"{getattr(configured, 'last_error', '') or '请先修正并刷新关系映射'}"
        )
    if str(getattr(configured, "data_source_id", "") or "") != str(base.source.id):
        raise BusinessQueryError("关系数据映射与对象数据源不一致，已阻止跨源关联")

    source_mapping_id = str(getattr(configured, "source_mapping_id", "") or "")
    target_mapping_id = str(getattr(configured, "target_mapping_id", "") or "")
    base_is_source = str(base.mapping.id) == source_mapping_id
    base_is_target = str(base.mapping.id) == target_mapping_id
    if not (base_is_source or base_is_target):
        raise BusinessQueryError("关系数据映射端点不属于当前查询范围")
    if base_is_source and str(related.mapping.id) != target_mapping_id:
        raise BusinessQueryError("关系数据映射目标端点与当前查询对象不一致")
    if base_is_target and str(related.mapping.id) != source_mapping_id:
        raise BusinessQueryError("关系数据映射源端点与当前查询对象不一致")

    source_type = str(base.source.type)
    mode = str(getattr(configured, "mode", "") or "").lower()
    if mode in {"source_fk", "target_fk"}:
        source_plan = base if base_is_source else related
        target_plan = related if base_is_source else base
        source_alias = base_alias if base_is_source else related_alias
        target_alias = related_alias if base_is_source else base_alias
        if mode == "source_fk":
            carrier_plan, carrier_alias = source_plan, source_alias
            other_plan, other_alias = target_plan, target_alias
        else:
            carrier_plan, carrier_alias = target_plan, target_alias
            other_plan, other_alias = source_plan, source_alias
        fk_expression = _physical_column(
            carrier_plan,
            getattr(configured, "foreign_key_column", ""),
            carrier_alias,
        ).expression
        key_expression = _physical_column(
            other_plan,
            _key_column(other_plan),
            other_alias,
        ).expression
        return [
            f" JOIN {mapped_query_service.quote_table(source_type, related.mapping.table_name)}"
            f" {_alias(source_type, related_alias)} ON {fk_expression} = {key_expression}"
        ]

    if mode == "join_table":
        base_key_expression = _physical_column(
            base,
            _key_column(base),
            base_alias,
        ).expression
        related_key_expression = _physical_column(
            related,
            _key_column(related),
            related_alias,
        ).expression
        join_source_column = str(getattr(configured, "source_key_column", "") or "")
        join_target_column = str(getattr(configured, "target_key_column", "") or "")
        join_table = mapped_query_service.quote_table(
            source_type, getattr(configured, "table_name", "")
        )
        join_alias = _alias(source_type, through_alias)
        first_join_column = join_source_column if base_is_source else join_target_column
        second_join_column = join_target_column if base_is_source else join_source_column
        first_join = (
            f"{join_alias}.{mapped_query_service.quote_identifier(source_type, first_join_column)}"
        )
        second_join = (
            f"{join_alias}.{mapped_query_service.quote_identifier(source_type, second_join_column)}"
        )
        return [
            f" JOIN {join_table} {join_alias} ON {base_key_expression} = {first_join}",
            f" JOIN {mapped_query_service.quote_table(source_type, related.mapping.table_name)}"
            f" {_alias(source_type, related_alias)} ON {second_join} = {related_key_expression}",
        ]

    raise BusinessQueryError("关系数据映射模式不受支持，请重新预检关系配置")


def _filter_sql(
    source_type: str,
    source_alias: str,
    filters: Sequence[Mapping[str, Any]],
    parameters: dict[str, Any],
    prefix: str,
) -> list[str]:
    binary = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    clauses: list[str] = []
    for index, item in enumerate(filters):
        column = f"{_alias(source_type, source_alias)}.{mapped_query_service.quote_identifier(source_type, item['column'])}"
        op = str(item["op"])
        if op == "is_null":
            clauses.append(f"{column} IS NULL")
        elif op == "is_not_null":
            clauses.append(f"{column} IS NOT NULL")
        elif op in {"in", "not_in"}:
            names: list[str] = []
            for value_index, value in enumerate(item["value"]):
                key = f"{prefix}_{index}_{value_index}"
                names.append(f":{key}")
                parameters[key] = value
            clauses.append(f"{column} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(names)})")
        elif op in {"contains", "starts_with", "ends_with"}:
            key = f"{prefix}_{index}"
            parameters[key] = mapped_query_service._like_value(op, item["value"])
            clauses.append(f"{column} LIKE :{key} ESCAPE '!'")
        else:
            key = f"{prefix}_{index}"
            parameters[key] = item["value"]
            clauses.append(f"{column} {binary[op]} :{key}")
    return clauses


def query_business_data(
    db: Session,
    *,
    definition: Any,
    mappings: Sequence[Any],
    data_sources: Sequence[Any],
    args: Any,
) -> dict[str, Any]:
    request = _object(args, "业务查询参数")
    allowed = {
        "base_entity", "base_properties", "base_filters", "related_entities",
        "group_by", "aggregations", "having", "sort", "limit",
    }
    unknown = set(request) - allowed
    if unknown:
        raise BusinessQueryError("业务查询不接受物理表、物理字段、SQL 或未定义参数")
    base_request = _entity_object(request.get("base_entity"), "base_entity")
    base = _source_plan(
        db,
        definition=definition,
        mappings=mappings,
        data_sources=data_sources,
        request=base_request,
        label="base_entity",
    )
    related_raw = _list(request.get("related_entities"), "related_entities", maximum=5)
    related: list[tuple[_SourcePlan, dict[str, Any], str]] = []
    used_entities = {str(base.entity.id)}
    for index, raw in enumerate(related_raw):
        item = _object(raw, f"related_entities[{index}]")
        if set(item) - {"entity_id", "entity_name", "properties", "filters", "join"}:
            raise BusinessQueryError("关联对象包含不支持的参数")
        plan = _source_plan(
            db,
            definition=definition,
            mappings=mappings,
            data_sources=data_sources,
            request=item,
            label=f"related_entities[{index}]",
        )
        if str(plan.entity.id) in used_entities:
            raise BusinessQueryError("业务查询不能重复关联同一个对象类型")
        if str(plan.source.id) != str(base.source.id):
            raise BusinessQueryError("当前业务查询只支持同一数据源内的对象关联")
        used_entities.add(str(plan.entity.id))
        related.append((plan, item, f"r{index}"))

    source_type = str(base.source.type)
    parameters: dict[str, Any] = {}
    aliases: dict[str, str] = {str(base.entity.id): "b"}
    plans: dict[str, _SourcePlan] = {str(base.entity.id): base}
    for plan, _item, alias in related:
        aliases[str(plan.entity.id)] = alias
        plans[str(plan.entity.id)] = plan

    raw_aggregations = _list(request.get("aggregations"), "aggregations", maximum=20)
    base_properties = request.get("base_properties", [])
    if not isinstance(base_properties, list) or len(base_properties) > 50:
        raise BusinessQueryError("base_properties 必须是不超过 50 个属性名的列表")
    if not base_properties and not raw_aggregations:
        raise BusinessQueryError("查询至少需要一个普通属性或聚合结果")
    columns: list[_Column] = [
        _column(base, name, "b", str(name).strip(), allow_transform=True)
        for name in base_properties
    ]
    for plan, item, alias in related:
        properties = item.get("properties", [])
        if not isinstance(properties, list) or len(properties) > 50:
            raise BusinessQueryError("关联对象 properties 必须是不超过 50 个属性名的列表")
        for name in properties:
            label = f"{plan.entity.name}.{str(name).strip()}"
            columns.append(_column(plan, name, alias, label, allow_transform=True))

    where = _filter_sql(
        source_type,
        "b",
        _normalize_filters(base, request.get("base_filters")),
        parameters,
        "b_filter",
    )
    join_sql: list[str] = []
    for index, (plan, item, alias) in enumerate(related):
        join_sql.extend(
            _relation_join_sql(
                definition,
                base,
                plan,
                item.get("join"),
                base_alias="b",
                related_alias=alias,
                through_alias=f"j{index}",
            )
        )
        where.extend(
            _filter_sql(
                source_type,
                alias,
                _normalize_filters(plan, item.get("filters")),
                parameters,
                f"{alias}_filter",
            )
        )

    select_sql: list[str] = []
    output_labels: list[str] = []
    for index, item in enumerate(columns):
        alias_name = f"q_col_{index}"
        select_sql.append(
            f"{item.expression} AS {mapped_query_service.quote_identifier(source_type, alias_name)}"
        )
        output_labels.append(item.label)

    group_sql: list[str] = []
    group_labels: set[str] = set()
    for index, raw in enumerate(_list(request.get("group_by"), "group_by", maximum=20)):
        item = _object(raw, f"group_by[{index}]")
        if set(item) - {"entity_id", "entity_name", "property"}:
            raise BusinessQueryError("group_by 只支持对象和 property")
        key = _entity_request(item, f"group_by[{index}]")
        entity = mapped_query_service._entity_from_request(definition, key)
        plan = plans.get(str(entity.id))
        if plan is None:
            raise BusinessQueryError("group_by 引用了未参与查询的对象类型")
        column = _column(plan, item.get("property"), aliases[str(entity.id)], "")
        group_sql.append(column.expression)
        group_labels.add(column.property_name)

    aggregate_aliases: set[str] = set()
    aggregate_alias_indexes: dict[str, int] = {}
    for index, raw in enumerate(raw_aggregations):
        item = _object(raw, f"aggregations[{index}]")
        if set(item) - {"function", "entity_id", "entity_name", "property", "alias"}:
            raise BusinessQueryError("聚合只支持 function、对象、property 和 alias")
        function = str(item.get("function") or "").strip().lower()
        if function not in _AGGREGATIONS:
            raise BusinessQueryError("聚合函数只支持 count、sum、avg、min、max")
        key = _entity_request(item, f"aggregations[{index}]")
        entity = mapped_query_service._entity_from_request(definition, key)
        plan = plans.get(str(entity.id))
        if plan is None:
            raise BusinessQueryError("聚合引用了未参与查询的对象类型")
        property_name = item.get("property")
        column: _Column | None = None
        if property_name not in (None, ""):
            column = _column(plan, property_name, aliases[str(entity.id)], "")
        elif function != "count":
            raise BusinessQueryError(f"aggregations[{index}] 的 {function} 聚合必须提供 property")
        if function in {"sum", "avg"} and column is not None:
            prop = plan.visible.get(column.property_name)
            if str(getattr(prop, "data_type", "")).lower() not in {"integer", "float", "number"}:
                raise BusinessQueryError(f"属性“{column.property_name}”不是可聚合的数值属性")
        alias = str(item.get("alias") or "").strip()
        if not alias or len(alias) > 80 or alias in output_labels or alias in aggregate_aliases:
            raise BusinessQueryError("聚合 alias 必须唯一且非空")
        aggregate_aliases.add(alias)
        aggregate_alias_indexes[alias] = index
        aggregate_expression = column.expression if column is not None else "*"
        select_sql.append(
            f"{function.upper()}({aggregate_expression}) AS "
            f"{mapped_query_service.quote_identifier(source_type, f'q_agg_{index}')}"
        )
        output_labels.append(alias)

    if not select_sql:
        raise BusinessQueryError("查询至少需要一个属性或聚合结果")
    if group_sql and any(column.expression not in set(group_sql) for column in columns):
        raise BusinessQueryError(
            "使用 group_by 时，返回的普通属性必须全部出现在 group_by 中；"
            "请先查询分组汇总，再单独查询违规明细"
        )
    having_sql: list[str] = []
    for index, raw in enumerate(_list(request.get("having"), "having", maximum=20)):
        item = _object(raw, f"having[{index}]")
        if set(item) - {"alias", "op", "value"}:
            raise BusinessQueryError("having 只支持聚合 alias、op 和 value")
        alias = str(item.get("alias") or "").strip()
        if alias not in aggregate_aliases:
            raise BusinessQueryError("having 只能引用当前查询中的聚合 alias")
        op = str(item.get("op") or "").strip().lower()
        if op not in _HAVING_OPERATORS:
            raise BusinessQueryError("having 运算符只支持 eq、ne、gt、gte、lt、lte、in、not_in")
        value = item.get("value")
        values = value if op in {"in", "not_in"} else [value]
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise BusinessQueryError("having 的过滤值必须是 1 到 100 项的标量列表")
        normalized_values: list[Any] = []
        for item_value in values:
            if isinstance(item_value, bool) or item_value is None or isinstance(item_value, (dict, list)):
                raise BusinessQueryError("having 的过滤值必须是非空标量")
            if op in {"gt", "gte", "lt", "lte"} and not isinstance(item_value, (int, float)):
                raise BusinessQueryError("having 的有序比较值必须是数值")
            normalized_values.append(item_value)
        aggregate_expression = mapped_query_service.quote_identifier(
            source_type, f'q_agg_{aggregate_alias_indexes[alias]}'
        )
        if op in {"in", "not_in"}:
            names = []
            for value_index, item_value in enumerate(normalized_values):
                parameter_name = f"having_{index}_{value_index}"
                parameters[parameter_name] = item_value
                names.append(f":{parameter_name}")
            keyword = "IN" if op == "in" else "NOT IN"
            having_sql.append(f"{aggregate_expression} {keyword} ({', '.join(names)})")
        else:
            parameter_name = f"having_{index}"
            parameters[parameter_name] = normalized_values[0]
            binary = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
            having_sql.append(f"{aggregate_expression} {binary[op]} :{parameter_name}")
    sql = (
        f"SELECT {', '.join(select_sql)}"
        f" FROM {mapped_query_service.quote_table(source_type, base.mapping.table_name)} b"
        + "".join(join_sql)
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_sql:
        sql += " GROUP BY " + ", ".join(group_sql)
    if having_sql:
        if not group_sql:
            raise BusinessQueryError("having 只能用于带 group_by 的聚合查询")
        sql += " HAVING " + " AND ".join(having_sql)

    sort_sql: list[str] = []
    for index, raw in enumerate(_list(request.get("sort"), "sort", maximum=10)):
        item = _object(raw, f"sort[{index}]")
        direction = str(item.get("direction") or "asc").lower()
        if direction not in {"asc", "desc"}:
            raise BusinessQueryError("排序方向只能是 asc 或 desc")
        alias = str(item.get("alias") or "").strip()
        if alias and alias in aggregate_aliases:
            sort_sql.append(
                f"{mapped_query_service.quote_identifier(source_type, f'q_agg_{aggregate_alias_indexes[alias]}')} {direction.upper()}"
            )
            continue
        key = _entity_request(item, f"sort[{index}]")
        entity = mapped_query_service._entity_from_request(definition, key)
        plan = plans.get(str(entity.id))
        if plan is None:
            raise BusinessQueryError("sort 引用了未参与查询的对象类型")
        sort_column = _column(plan, item.get("property"), aliases[str(entity.id)], "")
        sort_sql.append(f"{sort_column.expression} {direction.upper()}")
    if sort_sql:
        sql += " ORDER BY " + ", ".join(sort_sql)
    limit = _normalize_limit(request.get("limit"))
    parameters["bq_limit"] = limit + 1
    sql += " LIMIT :bq_limit"

    try:
        raw = datasource_service.run_parameterized_query(
            base.source, sql, parameters, limit=limit
        )
    except Exception as exc:  # noqa: BLE001 - connector details stay server-side.
        raise BusinessQueryError("业务查询执行失败，请检查数据源连接、数据映射和关联配置") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("rows"), list):
        raise BusinessQueryError("业务查询返回了无效结果")
    raw_columns = list(raw.get("columns") or [])
    expected_columns = [f"q_col_{index}" for index in range(len(columns))] + [
        f"q_agg_{index}"
        for index, _raw in enumerate(raw_aggregations)
    ]
    if raw_columns != expected_columns:
        raise BusinessQueryError("业务查询返回列与固定查询计划不一致")
    records: list[dict[str, Any]] = []
    for row in raw["rows"]:
        if not isinstance(row, (list, tuple)) or len(row) != len(output_labels):
            raise BusinessQueryError("业务查询返回行结构无效")
        projected: dict[str, Any] = {}
        for index, (label, value) in enumerate(zip(output_labels, row, strict=True)):
            if index < len(columns):
                try:
                    value = ontology_service.apply_transform_rules(
                        value, list(columns[index].transforms)
                    )
                except (TypeError, ValueError) as exc:
                    raise BusinessQueryError(
                        f"属性“{columns[index].property_name}”的数据转换失败，请检查映射规则"
                    ) from exc
            projected[label] = value
        records.append(projected)
    return {
        "records": records,
        "columns": output_labels,
        "row_count": len(records),
        "truncated": bool(raw.get("truncated", False)),
        "scope": {
            "entities": [str(base.entity.name)] + [str(plan.entity.name) for plan, _item, _alias in related],
            "data_source_id": str(base.source.id),
            "data_source_connector_revision": int(
                getattr(base.source, "connector_revision", 0) or 0
            ),
            "definition_hash": str(getattr(definition, "definition_hash", "") or ""),
            "mapping_ids": [str(base.mapping.id)] + [str(plan.mapping.id) for plan, _item, _alias in related],
        },
    }
