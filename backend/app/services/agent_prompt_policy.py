"""Shared model-facing constraints derived from authoritative tool state."""
from __future__ import annotations


AUTHORITATIVE_DECISION_PROMPT = (
    "【结论状态约束】工具结果中的 decision_state 是服务端权威。"
    "candidate_detected、candidate_detected_pending_review、additional_evidence_required "
    "或 manual_review_required 只能表述为候选、疑点、待补证或待复核，"
    "不得宣称事实已确认、违规已成立或操作已完成；只有服务端明确返回最终状态时才能作最终结论。"
)


__all__ = ["AUTHORITATIVE_DECISION_PROMPT"]
