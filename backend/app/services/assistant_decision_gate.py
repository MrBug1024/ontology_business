"""Deterministic decision gate for document-driven business modelling.

The language model may propose resources, but it does not decide whether a
resource is safe to formalize.  This module turns the compiler's evidence,
coverage and unresolved records into a small, auditable contract consumed by
the API and the assistant UI.
"""
from __future__ import annotations

from typing import Any


_RESOURCE_SECTIONS = (
    "entities",
    "relations",
    "instances",
    "mappings",
    "relation_mappings",
    "conceptual_mappings",
    "functions",
    "actions",
    "rules",
    "events",
    "workflows",
)


def _rows(payload: dict[str, Any], section: str) -> list[dict[str, Any]]:
    value = payload.get(section)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("unresolved")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _coverage(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("coverage")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _risk_codes(payload: dict[str, Any]) -> list[str]:
    codes: set[str] = set()
    for issue in _issues(payload):
        code = str(issue.get("code") or issue.get("reported_code") or "").upper()
        if any(token in code for token in ("CONFLICT", "SIDE_EFFECT", "EXTERNAL", "APPROVAL", "UNSAFE")):
            codes.add(code[:100])
    for item in _rows(payload, "actions"):
        if item.get("side_effecting") is True or item.get("requires_confirmation") is True:
            codes.add("SIDE_EFFECTING_ACTION")
    for item in _rows(payload, "workflows"):
        if item.get("side_effecting") is True or item.get("requires_confirmation") is True:
            codes.add("SIDE_EFFECTING_WORKFLOW")
    return sorted(codes)


def build_decision_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, JSON-safe modelling decision.

    ``formalize`` means the affected safe resources can be written to the
    formal working model after the existing explicit user confirmation.
    ``clarify`` means one or more source-backed questions must be answered
    before those affected resources can be treated as reliable.  A side
    effect or external action remains ``candidate_review`` even when its
    input is otherwise complete.
    """
    issues = _issues(payload)
    coverage = _coverage(payload)
    blocking = [item for item in issues if item.get("blocking", True) is not False]
    ambiguous_coverage = [item for item in coverage if str(item.get("status") or "").casefold() == "ambiguous"]
    modeled_coverage = [item for item in coverage if str(item.get("status") or "").casefold() == "modeled"]
    resource_counts = {
        section: len(_rows(payload, section))
        for section in _RESOURCE_SECTIONS
        if _rows(payload, section)
    }
    risk_codes = _risk_codes(payload)
    missing_evidence_resources = [
        f"{section}:{item.get('key') or item.get('name') or 'unnamed'}"
        for section in _RESOURCE_SECTIONS
        for item in _rows(payload, section)
        if not isinstance(item.get("evidence_refs"), list) or not any(
            str(value).strip() for value in (item.get("evidence_refs") or [])
        )
    ]
    question_items: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in blocking + ambiguous_coverage:
        code = str(item.get("code") or "DOCUMENT_AMBIGUITY")[:100]
        message = str(item.get("message") or item.get("reason") or "资料存在需要确认的业务含义")[:500]
        refs = tuple(str(value) for value in (item.get("source_refs") or []) if str(value))[:8]
        key = ("source", refs) if refs else (code, (message,))
        if key in seen:
            continue
        seen.add(key)
        question_items.append({
            "code": code,
            "message": message,
            "source_refs": list(refs),
            "affected_change_keys": [
                str(value) for value in (item.get("affected_change_keys") or []) if str(value)
            ][:20],
            "resolution_hint": str(item.get("resolution_hint") or "请补充业务规则、枚举或对象关系的明确说明。")[:500],
        })

    if (resource_counts and not coverage) or missing_evidence_resources:
        question_items.append({
            "code": "MISSING_EVIDENCE",
            "message": "部分候选资源缺少可追溯的资料引用，无法判断其业务含义是否可靠。",
            "source_refs": [],
            "affected_change_keys": missing_evidence_resources[:20],
            "resolution_hint": "请补充建模资料或让顾问重新检索授权来源后再继续。",
        })

    if question_items:
        mode = "clarify"
        reason_codes = [
            "MISSING_EVIDENCE" if any(item.get("code") == "MISSING_EVIDENCE" for item in question_items)
            else "BLOCKING_AMBIGUITY"
        ]
    elif risk_codes:
        mode = "candidate_review"
        reason_codes = ["SIDE_EFFECT_OR_EXTERNAL_RISK"]
    else:
        mode = "formalize"
        reason_codes = ["EVIDENCE_BACKED_AND_VALIDATED"]

    total = len(coverage)
    evidence_ratio = round(len(modeled_coverage) / total, 4) if total else 0.0
    return {
        "version": "decision-gate.v1",
        "mode": mode,
        "reason_codes": reason_codes,
        "blocking_question_count": len(question_items),
        "blocking_issue_count": len(blocking),
        "ambiguous_coverage_count": len(ambiguous_coverage),
        "missing_evidence_resource_count": len(missing_evidence_resources),
        "evidence_coverage": {
            "total": total,
            "modeled": len(modeled_coverage),
            "ambiguous": len(ambiguous_coverage),
            "context": sum(1 for item in coverage if str(item.get("status") or "").casefold() == "context"),
            "irrelevant": sum(1 for item in coverage if str(item.get("status") or "").casefold() == "irrelevant"),
            "modeled_ratio": evidence_ratio,
        },
        "resource_counts": resource_counts,
        "risk_codes": risk_codes,
        "questions": question_items[:12],
        "safe_to_formalize": mode == "formalize",
        "human_review_required": mode != "formalize",
        "explanation": (
            "已完成证据覆盖和确定性校验；安全资源可在用户确认后进入正式工作模型。"
            if mode == "formalize"
            else "存在影响业务含义的未决问题；先回答问题再继续正式建模。"
            if mode == "clarify"
            else "涉及副作用或外部业务事实；保持候选状态并要求人工审核。"
        ),
    }


def attach_decision_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the gate without mutating the compiler-owned input."""
    result = dict(payload)
    result["decision_gate"] = build_decision_gate(result)
    return result
