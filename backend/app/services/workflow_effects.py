"""Conservative effect classification over governed workflow node types."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


ANALYSIS_NODE_TYPES = frozenset({"start", "end", "llm", "rule", "approval"})


def requires_execution_confirmation(workflow: Any) -> bool:
    nodes = workflow.nodes or workflow.steps or []
    return any(
        not isinstance(node, Mapping) or node.get("type") not in ANALYSIS_NODE_TYPES
        for node in nodes
    )
