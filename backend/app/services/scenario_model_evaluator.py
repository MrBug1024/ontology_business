"""Deterministic set-based evaluation for normalized scenario models.

The evaluator never invokes a model and never reads or writes application
state.  Both inputs must already be normalized by ``scenario_model_compiler``.
It reports exact precision/recall/F1 for the five semantic layers used by the
construction-domain gold set: objects, properties, relations, constraints and
rules.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any

from .scenario_model_compiler import SCHEMA_VERSION


Atom = tuple[str, ...]
CATEGORIES = ("object", "property", "relation", "constraint", "rule")


def _text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _canonical_value(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): normalize(item[key]) for key in sorted(item, key=str)}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, str):
            return _text(item)
        return item

    return json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _canonical_condition(value: Any) -> Any:
    """Normalize commutative logical branches without changing rule meaning."""
    if not isinstance(value, dict):
        return value
    op = _text(value.get("op"))
    if op in {"and", "or"}:
        children = [_canonical_condition(item) for item in value.get("conditions") or []]
        return {
            "op": op,
            "conditions": sorted(children, key=_canonical_value),
        }
    if op == "not":
        return {
            "op": op,
            "conditions": [
                _canonical_condition(item) for item in value.get("conditions") or []
            ],
        }
    result: dict[str, Any] = {
        "field": _text(value.get("field")),
        "op": op,
    }
    if "value" in value:
        result["value"] = value.get("value")
    if "value_field" in value:
        result["value_field"] = _text(value.get("value_field"))
    return result


def _indexes(model: dict[str, Any]) -> dict[str, dict[str, str]]:
    indexes = {
        "entity_key": {},
        "entity_id": {},
        "action_key": {},
        "action_id": {},
    }
    for entity in model.get("entities") or []:
        name = _text(entity.get("name"))
        if entity.get("key"):
            indexes["entity_key"][str(entity["key"])] = name
        if entity.get("existing_id"):
            indexes["entity_id"][str(entity["existing_id"])] = name
    for action in model.get("actions") or []:
        name = _text(action.get("name"))
        if action.get("key"):
            indexes["action_key"][str(action["key"])] = name
        if action.get("existing_id"):
            indexes["action_id"][str(action["existing_id"])] = name
    return indexes


def _resolve_ref(
    ref: Any,
    indexes: dict[str, dict[str, str]],
    resource: str,
) -> str:
    if not isinstance(ref, dict):
        return "@unresolved"
    if ref.get("kind") == "generated":
        key = str(ref.get("key") or "")
        return indexes[f"{resource}_key"].get(key, f"@generated:{_text(key)}")
    if ref.get("kind") == "existing":
        resource_id = str(ref.get("id") or "")
        return indexes[f"{resource}_id"].get(resource_id, f"@existing:{_text(resource_id)}")
    return "@unresolved"


def semantic_atoms(model: dict[str, Any]) -> dict[str, set[Atom]]:
    """Extract comparable semantic atoms from compiler-normalized output."""
    if model.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"评估输入必须是 {SCHEMA_VERSION} 规范化输出")
    atoms: dict[str, set[Atom]] = {category: set() for category in CATEGORIES}
    indexes = _indexes(model)

    for entity in model.get("entities") or []:
        entity_name = _text(entity.get("name"))
        if not entity_name:
            continue
        atoms["object"].add((entity_name,))
        if entity.get("is_abstract"):
            atoms["constraint"].add((entity_name, "@object", "abstract", "true"))
        state_property = _text(entity.get("state_property"))
        if state_property:
            atoms["constraint"].add(
                (entity_name, "@object", "state_property", state_property)
            )
        for prop in entity.get("properties") or []:
            property_name = _text(prop.get("name"))
            if not property_name:
                continue
            atoms["property"].add((entity_name, property_name))
            atoms["constraint"].add(
                (entity_name, property_name, "data_type", _text(prop.get("data_type")))
            )
            for flag in ("is_key", "is_required", "is_enum", "is_sensitive"):
                if prop.get(flag):
                    atoms["constraint"].add((entity_name, property_name, flag, "true"))
            for enum_value in prop.get("enum_values") or []:
                atoms["constraint"].add(
                    (
                        entity_name,
                        property_name,
                        "enum_value",
                        _canonical_value(enum_value),
                    )
                )
            default_value = prop.get("default_value")
            if default_value not in (None, ""):
                atoms["constraint"].add(
                    (
                        entity_name,
                        property_name,
                        "default_value",
                        _canonical_value(default_value),
                    )
                )
            for key, value in sorted((prop.get("constraints") or {}).items()):
                atoms["constraint"].add(
                    (
                        entity_name,
                        property_name,
                        f"constraint:{_text(key)}",
                        _canonical_value(value),
                    )
                )

    for relation in model.get("relations") or []:
        atoms["relation"].add(
            (
                _resolve_ref(relation.get("source"), indexes, "entity"),
                _text(relation.get("name")),
                _resolve_ref(relation.get("target"), indexes, "entity"),
                _text(relation.get("relation_type")).upper(),
            )
        )

    for rule in model.get("rules") or []:
        trigger_actions = sorted(
            _resolve_ref(ref, indexes, "action")
            for ref in rule.get("trigger_actions") or []
        )
        atoms["rule"].add(
            (
                _resolve_ref(rule.get("entity"), indexes, "entity")
                if rule.get("entity")
                else "@scenario",
                _text(rule.get("name")),
                _canonical_value(_canonical_condition(rule.get("condition") or {})),
                _text(rule.get("severity") or "info"),
                _canonical_value(trigger_actions),
            )
        )
    return atoms


def _metric(predicted: set[Atom], expected: set[Atom]) -> dict[str, Any]:
    true_positive = len(predicted & expected)
    false_positive = len(predicted - expected)
    false_negative = len(expected - predicted)
    if not predicted and not expected:
        precision = recall = f1 = 1.0
    else:
        precision = true_positive / len(predicted) if predicted else 0.0
        recall = true_positive / len(expected) if expected else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "predicted_count": len(predicted),
        "gold_count": len(expected),
        "missing": [list(atom) for atom in sorted(expected - predicted)],
        "unexpected": [list(atom) for atom in sorted(predicted - expected)],
    }


def evaluate_scenario_model(
    predicted: dict[str, Any],
    gold: dict[str, Any],
) -> dict[str, Any]:
    """Return deterministic per-layer and aggregate metrics."""
    predicted_atoms = semantic_atoms(predicted)
    gold_atoms = semantic_atoms(gold)
    metrics = {
        category: _metric(predicted_atoms[category], gold_atoms[category])
        for category in CATEGORIES
    }
    all_predicted: set[Atom] = set()
    all_gold: set[Atom] = set()
    for category in CATEGORIES:
        all_predicted.update((category, *atom) for atom in predicted_atoms[category])
        all_gold.update((category, *atom) for atom in gold_atoms[category])
    metrics["micro"] = _metric(all_predicted, all_gold)
    metrics["macro"] = {
        key: sum(metrics[category][key] for category in CATEGORIES) / len(CATEGORIES)
        for key in ("precision", "recall", "f1")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "categories": list(CATEGORIES),
        "metrics": metrics,
    }
