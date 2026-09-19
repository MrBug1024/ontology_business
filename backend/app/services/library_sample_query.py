"""Server-generated SELECT over introspected, allowed source relations."""
from __future__ import annotations

import hashlib
import json

from ..distillation_sample_schemas import DatabaseSampleArguments
from . import release_service


def handle(value) -> str:
    return hashlib.sha256(b"distillation-source-sample/v1\0" + json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def catalog(tables: list[dict]) -> list[dict]:
    return [{"table_key": handle(table), "name": table["name"], "fields": [
        {"field_key": handle([table["name"], column]), "name": column["name"], "type": column["type"], "primary_key": column.get("pk", False)}
        for column in table["columns"] if not release_service._secret_key(column["name"])]} for table in tables]


def query(tables: list[dict], args: DatabaseSampleArguments, dialect: str):
    selected = next((table for table in catalog(tables) if table["table_key"] == args.table_key), None)
    if selected is None:
        raise ValueError("资料表引用已变化或不存在，请先重新查看可读资料表")
    fields = {field["field_key"]: field for field in selected["fields"]}
    chosen = args.field_keys or list(fields)[:20]
    if not chosen or len(set(chosen)) != len(chosen) or set(chosen).difference(fields):
        raise ValueError("只能读取当前结构中允许的字段")
    if any(item.field_key not in fields for item in args.filters):
        raise ValueError("查询条件引用了不可用字段")
    mark = "`" if dialect == "mysql" else '"'
    quote = lambda value: mark + value.replace(mark, mark + mark) + mark
    placeholder = "?" if dialect == "sqlite3" else "%s"
    cast = "CHAR" if dialect == "mysql" else "TEXT"
    # Bound large cells in the source, before fetching into Python. Clipped
    # values are excluded from equality matching rather than used as false keys.
    columns = ", ".join(f"SUBSTR(CAST({quote(fields[key]['name'])} AS {cast}), 1, 1001)" for key in chosen)
    clauses, values = [], []
    operators = {"eq": "=", "gte": ">=", "lte": "<="}
    for item in args.filters:
        clauses.append(f"{quote(fields[item.field_key]['name'])} {operators[item.operator]} {placeholder}")
        values.append(item.value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    primary = [quote(field["name"]) for field in fields.values() if field["primary_key"]]
    order = " ORDER BY " + ", ".join(primary) if primary else ""
    statement = f"SELECT {columns} FROM {quote(selected['name'])}{where}{order} LIMIT {placeholder}"
    values.append(args.limit + 1)
    return statement, values, {**selected, "fields": [fields[key] for key in chosen], "ordered_by_primary_key": bool(primary)}


def result(rows, metadata, args):
    records, excluded = [], []
    for index, row in enumerate(rows[:args.limit]):
        cells = {}
        for field, value in zip(metadata["fields"], row, strict=True):
            if value is not None and (not isinstance(value, str) or len(value) > 1000 or release_service._looks_like_secret_string(value)):
                excluded.append({"row": index, "field_key": field["field_key"]})
                cells[field["field_key"]] = None
            else:
                cells[field["field_key"]] = value
        records.append(cells)
    return {"kind": "database_sample", "table": metadata, "rows": records, "excluded_cells": excluded,
        "has_more": len(rows) > args.limit, "query": args.model_dump(exclude={"data_source_id"}),
        "limitations": ["仅本次有界读取的样本；字段值以源端文本表示，未证明总体唯一性或完整业务流程。",
            "未指定主键的表返回顺序不保证稳定；超长或疑似凭据单元格已排除。"]}
