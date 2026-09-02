from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import semantic_audit_rule_service


def _rule(
    rule_id: str,
    name: str,
    code: str,
    *,
    mode: str = "assisted",
    query_template: dict | None = None,
    extra: dict | None = None,
) -> SimpleNamespace:
    condition = {
        "spec_version": semantic_audit_rule_service.SPEC_VERSION,
        "rule_code": code,
        "domain": "test",
        "issue_type": "test-finding",
        "assessment_mode": mode,
        "source_use": "data-screening",
        "basis": "test basis",
        "reference_example": "",
        "first_listed_year": "2026",
        "required_evidence": ["source record"],
    }
    if query_template is not None:
        condition["query_template"] = query_template
    condition.update(extra or {})
    return SimpleNamespace(
        id=rule_id,
        name=name,
        enabled=True,
        condition=condition,
    )


def test_resolve_rule_accepts_stable_code_name_and_unambiguous_partial_name() -> None:
    first = _rule("rule-1", "Daily quantity threshold review", "daily-threshold")
    second = _rule("rule-2", "Cross-record correlation review", "cross-record")
    definition = SimpleNamespace(rules={first.id: first, second.id: second})

    assert semantic_audit_rule_service.resolve_rule(definition, "daily-threshold")[0] is first
    assert semantic_audit_rule_service.resolve_rule(definition, "Cross record")[0] is second


def test_manual_rule_is_formal_but_does_not_claim_automatic_evaluation() -> None:
    rule = _rule("rule-manual", "On-site evidence review", "onsite-review", mode="manual")

    spec = semantic_audit_rule_service.normalize_spec(rule)

    assert spec is not None
    assert spec["assessment_mode"] == "manual"
    assert "query_template" not in spec


def test_automatic_rule_requires_a_bounded_query_and_closed_spec() -> None:
    missing_query = _rule(
        "rule-auto",
        "Automatic threshold",
        "automatic-threshold",
        mode="automatic",
    )
    with pytest.raises(
        semantic_audit_rule_service.SemanticAuditRuleError,
        match="require a query template",
    ):
        semantic_audit_rule_service.normalize_spec(missing_query)

    unknown_field = _rule(
        "rule-extra",
        "Untrusted extension",
        "untrusted-extension",
        extra={"python_path": "package.module:function"},
    )
    with pytest.raises(
        semantic_audit_rule_service.SemanticAuditRuleError,
        match="unsupported fields",
    ):
        semantic_audit_rule_service.normalize_spec(unknown_field)


@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), float("-inf")])
def test_query_template_rejects_non_finite_numbers(invalid_number: float) -> None:
    rule = _rule(
        "rule-non-finite",
        "Non-finite threshold",
        "non-finite-threshold",
        query_template={"filter": {"value": invalid_number}},
    )

    with pytest.raises(
        semantic_audit_rule_service.SemanticAuditRuleError,
        match="invalid value",
    ):
        semantic_audit_rule_service.normalize_spec(rule)
