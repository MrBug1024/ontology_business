"""Reuse the exact durable outputs that a human approved, including LLM output."""
from __future__ import annotations

import copy
from typing import Any

from .policies import PolicyViolation


def approved_results(result: dict[str, Any], approved_node_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not approved_node_ids:
        return {}
    steps = result.get("steps") or []
    approved_boundary = -1
    for index, step in enumerate(steps):
        node_id = str(step.get("node") or (step.get("result") or {}).get("node_id") or "")
        if step.get("type") == "approval" and node_id in approved_node_ids:
            approved_boundary = index
    if approved_boundary < 0:
        raise PolicyViolation("审批前的执行结果不可用，已阻止重新生成已审内容")
    cached: dict[str, dict[str, Any]] = {}
    for step in steps[:approved_boundary]:
        if step.get("status") not in {"success", "matched", "not_matched", "published", "approved"}:
            raise PolicyViolation("审批前的执行结果不完整，无法恢复")
        key = str(step.get("node") or f"step_{step.get('step')}")
        cached[key] = copy.deepcopy(step)
    return cached
