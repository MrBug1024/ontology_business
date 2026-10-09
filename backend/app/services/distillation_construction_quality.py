"""Expose exploration gaps without claiming an incomplete handoff is complete."""
from __future__ import annotations

from ..distillation_schemas import DistillationDocument


def evaluate_document(document: DistillationDocument) -> dict:
    issues = []
    complete = []
    for entity in document.entities:
        requirements = {prop.attribute: prop for prop in entity.property_contracts}
        missing = []
        if not entity.description:
            missing.append("业务用途与含义")
        if not entity.evidence_refs:
            missing.append("可核对的业务依据")
        if not entity.is_abstract and (not entity.identity or not any(p.is_key for p in requirements.values())):
            missing.append("业务身份及对应属性")
        if sum(p.is_key for p in requirements.values()) > 1:
            missing.append("唯一的身份属性（复合身份需明确受治理的合成规则）")
        if not entity.attributes or set(entity.attributes) - set(requirements):
            missing.append("全部必要属性的类型、必填与适用约束")
        if missing:
            issues.append({"entity_key": entity.key, "name": entity.name,
                "missing": missing, "next_step": "先调查已有授权资料；仍缺业务事实时请求最小补充。"})
        else:
            complete.append(entity.key)
    return {"version": "distillation-construction-quality.v1",
            "entity_count": len(document.entities), "complete_entity_keys": complete,
            "complete_entity_count": len(complete), "issues": issues,
            "construction_complete": bool(document.desired_outcome and document.success_metric)
                and not issues and not document.open_questions
                and not any(a.status == "conflict" for a in document.assertions)}
