"""Validation and selection for governed semantic audit rule specifications."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any


SPEC_VERSION = "semantic-audit/v1"
ASSESSMENT_MODES = frozenset({"automatic", "assisted", "manual"})
SPEC_FIELDS = frozenset(
    {
        "spec_version",
        "rule_code",
        "domain",
        "issue_type",
        "assessment_mode",
        "source_use",
        "basis",
        "reference_example",
        "first_listed_year",
        "required_evidence",
        "query_template",
    }
)


class SemanticAuditRuleError(ValueError):
    """A semantic audit rule is malformed or cannot be selected safely."""


def _text(value: Any, label: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise SemanticAuditRuleError(f"{label} is invalid")
    return normalized


def _template_input_names(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 20:
        raise SemanticAuditRuleError("audit rule query template is too deeply nested")
    if isinstance(value, Mapping):
        if "$input" in value:
            if set(value) != {"$input"}:
                raise SemanticAuditRuleError(
                    "audit rule input references cannot contain other fields"
                )
            return {_text(value["$input"], "audit rule query input", maximum=100)}
        names: set[str] = set()
        for key, child in value.items():
            _text(key, "audit rule query field", maximum=100)
            names.update(_template_input_names(child, depth=depth + 1))
        return names
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            raise SemanticAuditRuleError("audit rule query template is too large")
        names: set[str] = set()
        for child in value:
            names.update(_template_input_names(child, depth=depth + 1))
        return names
    if value is None or isinstance(value, (str, bool, int)):
        return set()
    if isinstance(value, float) and math.isfinite(value):
        return set()
    raise SemanticAuditRuleError("audit rule query template contains an invalid value")


def template_input_names(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        raise SemanticAuditRuleError("semantic audit rule query template must be an object")
    return _template_input_names(value)


def normalize_spec(rule: Any) -> Mapping[str, Any] | None:
    condition = getattr(rule, "condition", None)
    if not isinstance(condition, Mapping):
        return None
    if str(condition.get("spec_version") or "") != SPEC_VERSION:
        return None
    if set(condition) - SPEC_FIELDS:
        raise SemanticAuditRuleError(
            "semantic audit rule contains unsupported fields"
        )
    rule_code = _text(condition.get("rule_code"), "audit rule code", maximum=120)
    mode = _text(
        condition.get("assessment_mode"),
        "audit assessment mode",
        maximum=20,
    )
    if mode not in ASSESSMENT_MODES:
        raise SemanticAuditRuleError(
            "semantic audit rule assessment mode is invalid"
        )
    required_evidence = condition.get("required_evidence", [])
    if (
        not isinstance(required_evidence, list)
        or len(required_evidence) > 50
        or any(not isinstance(item, str) or not item.strip() for item in required_evidence)
    ):
        raise SemanticAuditRuleError(
            "semantic audit rule evidence requirements are invalid"
        )
    query_template = condition.get("query_template")
    if query_template is not None:
        template_input_names(query_template)
    if mode == "automatic" and query_template is None:
        raise SemanticAuditRuleError(
            "automatic semantic audit rules require a query template"
        )
    return {
        **dict(condition),
        "rule_code": rule_code,
        "assessment_mode": mode,
        "required_evidence": [str(item).strip() for item in required_evidence],
    }


def _selector_identity(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def resolve_rule(definition: Any, selector: Any) -> tuple[Any, Mapping[str, Any]]:
    requested = _selector_identity(_text(selector, "audit rule selector", maximum=240))
    candidates: list[tuple[Any, Mapping[str, Any], set[str]]] = []
    for rule in (getattr(definition, "rules", {}) or {}).values():
        if not bool(getattr(rule, "enabled", False)):
            continue
        spec = normalize_spec(rule)
        if spec is None:
            continue
        identities = {
            _selector_identity(getattr(rule, "id", "")),
            _selector_identity(getattr(rule, "name", "")),
            _selector_identity(spec["rule_code"]),
        }
        identities.discard("")
        candidates.append((rule, spec, identities))
    exact = [(rule, spec) for rule, spec, values in candidates if requested in values]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SemanticAuditRuleError("audit rule selector is ambiguous")
    if len(requested) >= 4:
        partial = [
            (rule, spec)
            for rule, spec, values in candidates
            if any(requested in value or value in requested for value in values)
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise SemanticAuditRuleError("audit rule selector is ambiguous")
    raise SemanticAuditRuleError(
        "audit rule is unavailable in the resolved definition"
    )


__all__ = [
    "SPEC_VERSION",
    "SemanticAuditRuleError",
    "normalize_spec",
    "resolve_rule",
    "template_input_names",
]
