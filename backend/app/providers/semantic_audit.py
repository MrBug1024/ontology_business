"""Versioned semantic assessment Provider and its frozen v1 compatibility adapter."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from .semantic_dataset_query import (
    SemanticDatasetQueryProvider,
    _PreparedQueryExtension,
    _QueryBinding,
    _query_output_schema,
    _template_input_names,
    _text,
)


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
DECISION_STATES = (
    "candidate_detected",
    "candidate_detected_pending_review",
    "additional_evidence_required",
    "manual_review_required",
    "no_candidate_detected",
)


class SemanticAuditProviderError(ValueError):
    """A semantic assessment definition cannot be interpreted safely."""


def _audit_text(value: Any, label: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise SemanticAuditProviderError(f"{label} is invalid")
    return normalized


def _template_inputs(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 20:
        raise SemanticAuditProviderError("assessment query template is too deeply nested")
    if isinstance(value, Mapping):
        if "$input" in value:
            if set(value) != {"$input"}:
                raise SemanticAuditProviderError(
                    "assessment input references cannot contain other fields"
                )
            return {_audit_text(value["$input"], "assessment query input", maximum=100)}
        names: set[str] = set()
        for key, child in value.items():
            _audit_text(key, "assessment query field", maximum=100)
            names.update(_template_inputs(child, depth=depth + 1))
        return names
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            raise SemanticAuditProviderError("assessment query template is too large")
        names: set[str] = set()
        for child in value:
            names.update(_template_inputs(child, depth=depth + 1))
        return names
    if value is None or isinstance(value, (str, bool, int)):
        return set()
    if isinstance(value, float) and math.isfinite(value):
        return set()
    raise SemanticAuditProviderError("assessment query template contains an invalid value")


def template_input_names(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        raise SemanticAuditProviderError("semantic assessment query template must be an object")
    return _template_inputs(value)


def normalize_spec(rule: Any) -> Mapping[str, Any] | None:
    condition = getattr(rule, "condition", None)
    if not isinstance(condition, Mapping):
        return None
    if str(condition.get("spec_version") or "") != SPEC_VERSION:
        return None
    if set(condition) - SPEC_FIELDS:
        raise SemanticAuditProviderError(
            "semantic assessment rule contains unsupported fields"
        )
    rule_code = _audit_text(
        condition.get("rule_code"), "assessment rule code", maximum=120
    )
    mode = _audit_text(
        condition.get("assessment_mode"), "assessment mode", maximum=20
    )
    if mode not in ASSESSMENT_MODES:
        raise SemanticAuditProviderError("semantic assessment mode is invalid")
    required_evidence = condition.get("required_evidence", [])
    if (
        not isinstance(required_evidence, list)
        or len(required_evidence) > 50
        or any(not isinstance(item, str) or not item.strip() for item in required_evidence)
    ):
        raise SemanticAuditProviderError(
            "semantic assessment evidence requirements are invalid"
        )
    query_template = condition.get("query_template")
    if query_template is not None:
        template_input_names(query_template)
    if mode == "automatic" and query_template is None:
        raise SemanticAuditProviderError(
            "automatic semantic assessment rules require a query template"
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
    requested = _selector_identity(
        _audit_text(selector, "assessment rule selector", maximum=240)
    )
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
        raise SemanticAuditProviderError("assessment rule selector is ambiguous")
    if len(requested) >= 4:
        partial = [
            (rule, spec)
            for rule, spec, values in candidates
            if any(requested in value or value in requested for value in values)
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise SemanticAuditProviderError("assessment rule selector is ambiguous")
    raise SemanticAuditProviderError(
        "assessment rule is unavailable in the resolved definition"
    )


def _empty_query_result() -> dict[str, Any]:
    return {
        "records": [],
        "columns": [],
        "row_count": 0,
        "truncated": False,
        "offset": 0,
        "next_offset": None,
    }


def _assessment_result(
    rule: Any,
    spec: Mapping[str, Any],
    *,
    state: str,
    result: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    return {
        "audit_rule": {
            "id": str(getattr(rule, "id", "")),
            "name": str(getattr(rule, "name", "")),
            "code": str(spec["rule_code"]),
            "domain": str(spec.get("domain") or ""),
            "issue_type": str(spec.get("issue_type") or ""),
            "assessment_mode": str(spec["assessment_mode"]),
        },
        "decision_state": state,
        "basis": str(spec.get("basis") or ""),
        "required_evidence": list(spec.get("required_evidence") or []),
        **dict(result or _empty_query_result()),
    }


@dataclass(frozen=True, slots=True)
class _SemanticAuditQueryExtension:
    selector_input: str

    def prepare(
        self,
        definition: Any,
        function: Any,
        inputs: Mapping[str, Any],
    ) -> _PreparedQueryExtension:
        rule, spec = resolve_rule(definition, inputs.get(self.selector_input))
        query_template = spec.get("query_template")
        if query_template is None:
            state = (
                "manual_review_required"
                if str(spec["assessment_mode"]) == "manual"
                else "additional_evidence_required"
            )
            return _PreparedQueryExtension(
                query_template=None,
                terminal_output=_assessment_result(
                    rule,
                    spec,
                    state=state,
                    result=None,
                ),
            )
        input_names = template_input_names(query_template)
        input_schema = getattr(function, "input_schema", None)
        properties = (
            input_schema.get("properties") if isinstance(input_schema, Mapping) else None
        )
        if not isinstance(properties, Mapping) or not input_names.issubset(properties):
            raise SemanticAuditProviderError(
                "assessment rule query inputs must be declared by the function input schema"
            )
        return _PreparedQueryExtension(
            query_template=query_template,
            context=(rule, spec),
        )

    def finalize(
        self,
        context: Any,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        rule, spec = context
        if int(result.get("row_count") or 0) > 0:
            state = (
                "candidate_detected"
                if str(spec["assessment_mode"]) == "automatic"
                else "candidate_detected_pending_review"
            )
        else:
            state = "no_candidate_detected"
        return _assessment_result(rule, spec, state=state, result=result)


def _selector_binding(value: Any, *, input_schema: Any) -> tuple[dict[str, Any], _SemanticAuditQueryExtension]:
    if not isinstance(value, Mapping) or set(value) != {
        "selector_input",
        "spec_version",
    }:
        raise SemanticAuditProviderError(
            "assessment binding must declare only selector_input and spec_version"
        )
    if str(value.get("spec_version") or "") != SPEC_VERSION:
        raise SemanticAuditProviderError("semantic assessment version is unsupported")
    selector_input = _text(
        value.get("selector_input"), "assessment rule selector input", maximum=100
    )
    properties = (
        input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    )
    selector_schema = (
        properties.get(selector_input) if isinstance(properties, Mapping) else None
    )
    if not isinstance(selector_schema, Mapping) or selector_schema.get("type") != "string":
        raise SemanticAuditProviderError(
            "assessment selector must be a declared string input"
        )
    normalized = {"selector_input": selector_input, "spec_version": SPEC_VERSION}
    return normalized, _SemanticAuditQueryExtension(selector_input=selector_input)


def legacy_dataset_query_binding(
    value: Any,
    *,
    input_schema: Any,
) -> tuple[dict[str, Any], _SemanticAuditQueryExtension]:
    """Interpret the frozen v1 dataset-query binding without enabling new writes."""

    return _selector_binding(value, input_schema=input_schema)


def _audit_output_schema() -> dict[str, Any]:
    query = _query_output_schema()
    properties = dict(query["properties"])
    properties.update(
        {
            "audit_rule": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "code": {"type": "string"},
                    "domain": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "assessment_mode": {
                        "type": "string",
                        "enum": sorted(ASSESSMENT_MODES),
                    },
                },
                "required": [
                    "id",
                    "name",
                    "code",
                    "domain",
                    "issue_type",
                    "assessment_mode",
                ],
                "additionalProperties": False,
            },
            "decision_state": {"type": "string", "enum": list(DECISION_STATES)},
            "basis": {"type": "string"},
            "required_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 50,
            },
        }
    )
    return {
        "type": "object",
        "properties": properties,
        "required": [
            "audit_rule",
            "decision_state",
            "basis",
            "required_evidence",
            *query["required"],
        ],
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class SemanticAuditProvider(SemanticDatasetQueryProvider):
    """Resolve governed v1 assessment rules and evaluate bounded query candidates."""

    provider_key: ClassVar[str] = "builtin.semantic-audit"
    provider_version: ClassVar[str] = "1.0.0"

    def definition_manifest(self) -> Mapping[str, Any]:
        return {
            "capability_kind": self.capability_kind,
            "display_name": "语义规则判定",
            "description": "根据版本化规则选择器执行有界只读查询，并返回 Provider 权威判定状态。",
            "config_schema": {
                "type": "object",
                "properties": {
                    "semantic_mapping_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 64},
                        "minItems": 1,
                        "maxItems": 50,
                        "uniqueItems": True,
                    },
                    "selector_input": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "spec_version": {"type": "string", "const": SPEC_VERSION},
                },
                "required": [
                    "semantic_mapping_ids",
                    "selector_input",
                    "spec_version",
                ],
                "additionalProperties": False,
            },
            "default_config": {
                "semantic_mapping_ids": [],
                "selector_input": "rule_selector",
                "spec_version": SPEC_VERSION,
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "rule_selector": {
                        "type": "string",
                        "minLength": 1,
                        "description": "受治理规则的稳定 ID、名称或代码。",
                    }
                },
                "required": ["rule_selector"],
                "additionalProperties": False,
            },
            "output_schema": _audit_output_schema(),
            "input_schema_mode": "editable",
            "output_schema_mode": "fixed",
            "ui_schema": {
                "semantic_mapping_ids": {
                    "control": "semantic_mapping_multiselect",
                    "label": "判定对象映射",
                    "help": "只选择当前场景已激活的语义映射。",
                    "placeholder": "选择已激活的语义映射",
                },
                "selector_input": {
                    "control": "text",
                    "label": "规则选择器输入字段",
                    "help": "该字段必须在输入 Schema 中声明为字符串。",
                },
                "spec_version": {
                    "control": "text",
                    "label": "规则契约版本",
                },
            },
            "deprecated": False,
            "migration_message": "",
        }

    def _normalize_provider_config(
        self,
        function: Any,
        provider_config: Any,
        *,
        compatibility_mode: bool,
    ) -> tuple[dict[str, Any], _QueryBinding]:
        if not isinstance(provider_config, Mapping) or set(provider_config) != {
            "semantic_mapping_ids",
            "selector_input",
            "spec_version",
        }:
            raise SemanticAuditProviderError(
                "semantic assessment Provider config is invalid"
            )
        base_config, base_binding = SemanticDatasetQueryProvider._normalize_provider_config(
            self,
            function,
            {"semantic_mapping_ids": provider_config.get("semantic_mapping_ids")},
            compatibility_mode=False,
        )
        selector_binding, extension = _selector_binding(
            {
                "selector_input": provider_config.get("selector_input"),
                "spec_version": provider_config.get("spec_version"),
            },
            input_schema=getattr(function, "input_schema", None),
        )
        return {
            **base_config,
            **selector_binding,
        }, _QueryBinding(
            mapping_ids=base_binding.mapping_ids,
            extension=extension,
        )


__all__ = [
    "ASSESSMENT_MODES",
    "DECISION_STATES",
    "SPEC_VERSION",
    "SemanticAuditProvider",
    "SemanticAuditProviderError",
    "legacy_dataset_query_binding",
    "normalize_spec",
    "resolve_rule",
    "template_input_names",
]
