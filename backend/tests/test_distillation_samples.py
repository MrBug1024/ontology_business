from __future__ import annotations

import sqlite3

import pytest

from app.distillation_sample_schemas import CompareSamplesArguments, DatabaseSampleArguments
from app.services.distillation_sample_service import compare_rows
from app.services.library_sample_query import catalog, query
from app.services.library_sqlite_adapter import inspect_path


def test_samples_read_actual_rows_reject_arbitrary_identifiers_and_bind_filters(tmp_path):
    path = tmp_path / "research.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, period TEXT, label TEXT, password TEXT)')
        connection.executemany('INSERT INTO records VALUES (?,?,?,?)', [(1, '2026-01', 'completed', 'synthetic'), (2, '2026-02', 'reworked', 'synthetic')])
    tables = catalog(inspect_path(path))
    table = tables[0]
    assert [field["name"] for field in table["fields"]] == ["id", "period", "label"]
    keys = {field["name"]: field["field_key"] for field in table["fields"]}
    args = DatabaseSampleArguments(data_source_id="source", table_key=table["table_key"], limit=1)
    sample = inspect_path(path, sample=args)
    assert sample["has_more"] and len(sample["rows"]) == 1
    assert sample["rows"][0][keys["label"]] == "completed"
    args = DatabaseSampleArguments.model_validate({**args.model_dump(), "filters": [{"field_key": keys["period"], "value": "2026-02"}]})
    filtered = inspect_path(path, sample=args)
    assert filtered["rows"][0][keys["label"]] == "reworked" and not filtered["has_more"]
    args.filters[0].value = "' OR 1=1 --"
    assert inspect_path(path, sample=args)["rows"] == []
    args.table_key = "0" * 32
    with pytest.raises(ValueError, match="引用"):
        inspect_path(path, sample=args)


def test_compound_keys_expose_ambiguity_nulls_and_unmatched_records():
    keys = [str(i) * 32 for i in range(1, 5)]
    left = {"kind": "database_sample", "table": {"fields": [{"field_key": key} for key in keys[:2]]},
        "rows": [{keys[0]: "same", keys[1]: "one"}, {keys[0]: "same", keys[1]: "two"}, {keys[0]: "missing", keys[1]: "one"}, {keys[0]: None, keys[1]: "one"}]}
    right = {"kind": "database_sample", "table": {"fields": [{"field_key": key} for key in keys[2:]]},
        "rows": [{keys[2]: "same", keys[3]: "one"}, {keys[2]: "same", keys[3]: "two"}]}
    args = CompareSamplesArguments(left_evidence_key="left", right_evidence_key="right", fields=[{"left": keys[0], "right": keys[2]}])
    single = compare_rows(left, right, args)
    assert len(single["ambiguous_matches"]) == 2 and single["left_duplicate_key_groups"] == 1
    compound = compare_rows(left, right, args.model_copy(update={"fields": [*args.fields, type(args.fields[0])(left=keys[1], right=keys[3])]}))
    assert compound["unique_matches"] == [{"left_row": 0, "right_row": 0}, {"left_row": 1, "right_row": 1}]
    assert compound["unmatched_left_rows"] == [2] and compound["excluded_left_rows"] == [3]
    assert "不能" in compound["limitations"][1]


def test_source_select_is_closed_and_large_or_secret_cells_are_excluded(tmp_path):
    path = tmp_path / "bounded.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, detail TEXT)')
        connection.executemany('INSERT INTO records VALUES (?,?)', [(1, 'x' * 5000), (2, 'password=ephemeral-synthetic'), (3, 'usable')])
    schema = inspect_path(path)
    table = catalog(schema)[0]
    args = DatabaseSampleArguments(data_source_id="source", table_key=table["table_key"])
    for dialect in ("postgres", "mysql", "sqlite3"):
        statement, values, _metadata = query(schema, args, dialect)
        assert statement.startswith("SELECT SUBSTR(CAST(") and statement.count(";") == 0
        assert values == [31]
    sample = inspect_path(path, sample=args)
    assert len(sample["excluded_cells"]) == 2
    assert "x" * 1001 not in str(sample) and "ephemeral-synthetic" not in str(sample)
