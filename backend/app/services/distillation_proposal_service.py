"""One proposal policy for old analysis and conversational investigation."""
from __future__ import annotations

from copy import deepcopy

from ..distillation_schemas import DistillationDocument, Evidence


def normalize_proposal(raw: dict, original: DistillationDocument, *, observations: list[Evidence] | None = None) -> DistillationDocument:
    proposed = deepcopy(raw)
    for key in ("decision", "decision_reason", "target_systems"):
        proposed[key] = original.model_dump()[key]
    # Only a trusted tool's durable observation may add evidence. Model-provided
    # evidence cannot replace human sources or create a fictitious fetch receipt.
    roles = {item.get("key"): item.get("role") for item in proposed.get("evidence", [])}
    proposed["evidence"] = []
    for item in [*original.evidence, *(observations or [])]:
        source = item.model_dump()
        if roles.get(item.key) in {"input", "result", "knowledge", "process", "reference"}:
            source["role"] = roles[item.key]
        proposed["evidence"].append(source)
    originals = [item.model_dump() for item in original.assertions]
    for assertion in proposed.get("assertions", []):
        if assertion.get("status") == "fact" and assertion not in originals:
            assertion["status"] = "inference"
    return DistillationDocument.model_validate(proposed)
