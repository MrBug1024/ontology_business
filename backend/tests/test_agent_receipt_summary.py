from __future__ import annotations

import json

from app.services.agent_receipt_summary import MAX_SUMMARY_BYTES, MAX_SUMMARY_FIELDS, output_summary


def test_large_result_summary_keeps_declared_counts_and_states_without_free_text_or_rows() -> None:
    schema = {"properties": {
        "total": {"type": "integer"},
        "complete": {"type": "boolean"},
        "state": {"type": "string", "enum": ["pending_review", "final"]},
        "notes": {"type": "string"},
        "records": {"type": "array"},
    }}
    result = {"total": 107, "complete": True, "state": "pending_review",
              "notes": "private text", "records": [{"value": "private row"}], "undeclared": 42}
    assert output_summary(result, schema) == {
        "complete": True, "state": "pending_review", "total": 107,
    }


def test_summary_rejects_type_confusion_and_undeclared_state_values() -> None:
    schema = {"properties": {
        "count": {"type": "integer"}, "amount": {"type": "number"},
        "state": {"type": "string", "enum": ["ready"]},
    }}
    assert output_summary({"count": True, "amount": float("nan"), "state": "private"}, schema) == {}
    assert output_summary({"count": 3}, None) == {}
    assert output_summary({"count": 3}, {"properties": []}) == {}


def test_summary_has_field_and_byte_bounds() -> None:
    keys = [f"metric_{index:03d}" for index in range(100)]
    schema = {"properties": {key: {"type": "number"} for key in keys}}
    summary = output_summary({key: 10 ** 200 for key in keys}, schema)
    assert 0 < len(summary) <= MAX_SUMMARY_FIELDS
    assert len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) <= MAX_SUMMARY_BYTES
