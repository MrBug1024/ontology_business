"""Shared model-facing constraints derived from authoritative tool state."""
from __future__ import annotations


AUTHORITATIVE_DECISION_PROMPT = (
    "【服务端状态约束】工具结果与 Receipt 中明确标记的服务端状态具有权威性。"
    "不得把中间、待处理或不确定状态改写为最终结论，也不得宣称尚未由服务端确认的操作已经完成。"
)


__all__ = ["AUTHORITATIVE_DECISION_PROMPT"]
