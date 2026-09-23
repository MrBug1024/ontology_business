"""Deterministic, evidence-bounded lineage candidate inference.

This module only reasons over already-authorized sample payloads. It does not
read a connector, resolve a physical path, or persist a relationship. The
result is deliberately a candidate report for human/LLM review, not a formal
definition or a runtime execution plan.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
import math
import re
from typing import Any


MAX_READS = 24
MAX_ROWS_PER_READ = 100
MAX_FIELDS_PER_READ = 20
MIN_SHARED_VALUES = 2
MIN_CONTAINMENT_RATE = 0.6
MAX_CANDIDATES_PER_TABLE_PAIR = 3


def infer_lineage_candidates(reads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Infer high-signal field links across bounded, previously read samples.

    The smaller distinct-value set is used as the containment denominator. It
    prevents a small result sample from looking unrelated to a much larger
    upstream sample merely because the upstream table has many other values.
    Field-name similarity can rank a candidate but cannot create one without
    exact normalized value overlap.
    """
    if not 2 <= len(reads) <= MAX_READS:
        raise ValueError(f"血缘推导需要 2-{MAX_READS} 份已读取样本")

    samples: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for read in reads:
        nested = _nested_samples(read)
        if nested is None:
            skipped.append({
                "evidence_key": str(read.get("evidence_key") or ""),
                "reason": "资料只有结构或文本，没有可用于精确匹配的有界行样本",
            })
            continue
        if not nested:
            skipped.append({
                "evidence_key": str(read.get("evidence_key") or ""),
                "reason": "资料没有可用于精确匹配的有界行样本",
            })
            continue
        samples.extend(nested)

    candidates: list[dict[str, Any]] = []
    compared_field_pairs = 0
    for index, left in enumerate(samples):
        for right in samples[index + 1:]:
            pair_candidates, pair_count = _compare_samples(left, right)
            compared_field_pairs += pair_count
            candidates.extend(pair_candidates)

    candidates.sort(key=lambda item: (-item["confidence"], item["source"]["table"], item["target"]["table"]))
    return {
        "kind": "lineage_candidates",
        "engine": "sample_value_containment/v1",
        "sources": [
            {
                "evidence_key": sample["evidence_key"],
                "table": sample["table"],
                "sample_rows": sample["row_count"],
                "fields": [field["name"] for field in sample["fields"]],
            }
            for sample in samples
        ],
        "candidates": candidates,
        "skipped_sources": skipped,
        "stats": {
            "source_count": len(samples),
            "compared_field_pairs": compared_field_pairs,
            "candidate_count": len(candidates),
        },
        "limitations": [
            "只对已授权、已读取的有界样本做精确文本匹配，不代表全量唯一性。",
            "字段名相似度只用于排序和解释；没有真实值重叠的字段对不会成为候选。",
            "候选关系不证明因果、流程先后或转换逻辑，正式血缘仍需人工核对并引用证据。",
            "样本存在重复键或多匹配时会保留风险指标，不会自动压成唯一外键。",
        ],
    }


def _normalize_sample(read: Mapping[str, Any]) -> dict[str, Any] | None:
    content = read.get("content")
    if not isinstance(content, Mapping) or content.get("kind") != "database_sample":
        return None
    table = content.get("table")
    raw_fields = table.get("fields") if isinstance(table, Mapping) else None
    raw_rows = content.get("rows")
    if not isinstance(table, Mapping) or not isinstance(raw_fields, list) or not isinstance(raw_rows, list):
        return None
    fields: list[dict[str, str]] = []
    field_keys: set[str] = set()
    for item in raw_fields[:MAX_FIELDS_PER_READ]:
        if not isinstance(item, Mapping):
            continue
        field_key = str(item.get("field_key") or "")
        name = str(item.get("name") or "")
        if not field_key or not name or field_key in field_keys:
            continue
        field_keys.add(field_key)
        fields.append({"key": field_key, "name": name})
    if not fields:
        return None
    rows = [row for row in raw_rows[:MAX_ROWS_PER_READ] if isinstance(row, Mapping)]
    if not rows:
        return None
    return {
        "evidence_key": str(read.get("evidence_key") or "未命名资料"),
        "table": str(table.get("name") or read.get("evidence_key") or "未命名表"),
        "role": content.get("role") if content.get("role") in {"input", "result", "process", "knowledge", "reference"} else None,
        "fields": fields,
        "rows": rows,
        "row_count": len(rows),
    }


def _nested_samples(read: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Normalize a standalone sample or a source-scoped landscape report."""
    content = read.get("content")
    if not isinstance(content, Mapping):
        return None
    if content.get("kind") == "data_landscape_source":
        nested = []
        for item in content.get("samples") or []:
            if not isinstance(item, Mapping):
                continue
            sample = _normalize_sample({"evidence_key": read.get("evidence_key"), "content": item})
            if sample is not None:
                nested.append(sample)
        return nested
    if content.get("kind") != "database_sample":
        return None
    sample = _normalize_sample(read)
    return [sample] if sample is not None else []


def _compare_samples(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    pair_candidates: list[dict[str, Any]] = []
    compared = 0
    for left_field in left["fields"]:
        left_values = _value_counter(left["rows"], left_field["key"])
        if not left_values:
            continue
        for right_field in right["fields"]:
            compared += 1
            right_values = _value_counter(right["rows"], right_field["key"])
            if not right_values:
                continue
            shared = set(left_values).intersection(right_values)
            denominator = min(len(left_values), len(right_values))
            containment = len(shared) / denominator if denominator else 0.0
            if len(shared) < MIN_SHARED_VALUES or containment < MIN_CONTAINMENT_RATE:
                continue
            left_coverage = len(shared) / len(left_values)
            right_coverage = len(shared) / len(right_values)
            name_similarity = _name_similarity(left_field["name"], right_field["name"])
            ordered_left, ordered_right = _ordered_endpoints(left, right)
            if ordered_left is left:
                source_field, target_field = left_field, right_field
                source_values, target_values = left_values, right_values
                source_coverage, target_coverage = left_coverage, right_coverage
            else:
                source_field, target_field = right_field, left_field
                source_values, target_values = right_values, left_values
                source_coverage, target_coverage = right_coverage, left_coverage
            source_unique = _unique_ratio(source_values)
            target_unique = _unique_ratio(target_values)
            ambiguity = sum(1 for value in shared if target_values[value] > 1)
            confidence = min(
                0.99,
                0.72 * containment + 0.16 * name_similarity + 0.12 * target_unique,
            )
            direction = "input_to_result" if (
                ordered_left.get("role") == "input" and ordered_right.get("role") == "result"
            ) else "undirected"
            candidate_type = "possible_link"
            key_shape = "one_side_unique" if max(source_unique, target_unique) >= 0.95 and ambiguity == 0 else "non_unique_or_ambiguous"
            pair_candidates.append({
                "source": {
                    "evidence_key": ordered_left["evidence_key"],
                    "table": ordered_left["table"],
                    "field": source_field["name"],
                },
                "target": {
                    "evidence_key": ordered_right["evidence_key"],
                    "table": ordered_right["table"],
                    "field": target_field["name"],
                },
                "relation_type": candidate_type,
                "direction": direction,
                "key_shape": key_shape,
                "confidence": round(confidence, 3),
                "evidence": {
                    "shared_distinct_values": len(shared),
                    "source_distinct_values": len(source_values),
                    "target_distinct_values": len(target_values),
                    "containment_rate": round(containment, 3),
                    "source_coverage": round(source_coverage, 3),
                    "target_coverage": round(target_coverage, 3),
                    "field_name_similarity": round(name_similarity, 3),
                    "source_unique_ratio": round(source_unique, 3),
                    "target_unique_ratio": round(target_unique, 3),
                    "target_ambiguous_value_count": ambiguity,
                },
                "explanation": _explanation(
                    ordered_left["table"], source_field["name"], ordered_right["table"], target_field["name"],
                    containment, source_coverage, target_coverage, ambiguity, direction,
                ),
            })

    pair_candidates.sort(key=lambda item: (-item["confidence"], item["source"]["field"], item["target"]["field"]))
    selected: list[dict[str, Any]] = []
    seen_source_fields: set[str] = set()
    for candidate in pair_candidates:
        source_field = candidate["source"]["field"]
        if source_field in seen_source_fields:
            continue
        seen_source_fields.add(source_field)
        selected.append(candidate)
        if len(selected) >= MAX_CANDIDATES_PER_TABLE_PAIR:
            break
    return selected, compared


def _ordered_endpoints(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if left.get("role") == "input" and right.get("role") == "result":
        return left, right
    if left.get("role") == "result" and right.get("role") == "input":
        return right, left
    return left, right


def _value_counter(rows: Sequence[Mapping[str, Any]], field_key: str) -> Counter[str]:
    values: list[str] = []
    for row in rows:
        value = _canonical_value(row.get(field_key))
        if value is not None:
            values.append(value)
    return Counter(values)


def _canonical_value(value: Any) -> str | None:
    if value is None or isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _unique_ratio(values: Counter[str]) -> float:
    total = sum(values.values())
    return len(values) / total if total else 0.0


def _name_similarity(left: str, right: str) -> float:
    normalize = lambda value: re.sub(r"[\s_\-]+", "", value.strip().lower())
    left_normalized, right_normalized = normalize(left), normalize(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def _explanation(
    left_table: str,
    left_field: str,
    right_table: str,
    right_field: str,
    containment: float,
    left_coverage: float,
    right_coverage: float,
    ambiguity: int,
    direction: str,
) -> str:
    text = (
        f"样本中 {left_table}.{left_field} 与 {right_table}.{right_field} "
        f"有较高精确值包含率（{containment:.0%}），"
        f"两侧覆盖率分别为 {left_coverage:.0%}/{right_coverage:.0%}。"
    )
    if ambiguity:
        return text + f"目标字段存在 {ambiguity} 个重复值组，不能据此确认唯一关联。"
    if direction == "input_to_result":
        return text + "两份证据角色支持从输入到结果的候选方向，但仍需核对转换规则和因果关系。"
    return text + "仍需结合业务语义和更多样本核对方向及因果关系。"


__all__ = ["infer_lineage_candidates"]
