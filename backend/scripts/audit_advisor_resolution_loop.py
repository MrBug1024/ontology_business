"""Observe bounded advisor repair and replanning boundaries without external I/O.

Model replies are supplied synthetically. The current compiler, repair policy
and decision gate run directly; no model or database session is invoked.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from audit_ai_construction_quality import entity_batch, normalize, summary


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "backend/app/services/scenario_model_quality_service.py",
    "backend/app/services/assistant_decision_gate.py",
    "backend/app/services/scenario_model_compiler.py",
    "backend/app/routers/assistant.py",
    "backend/app/services/scenario_model_draft_service.py",
    "frontend/src/views/ScenarioDetail.vue",
)


def repair_observation(raw: dict, source: dict, proposed: dict) -> dict:
    from app.services import scenario_model_quality_service as quality
    from app.services.assistant_decision_gate import build_decision_gate

    original = normalize(raw, source)
    calls = []
    normalizations = []

    def evaluate(candidate):
        normalizations.append(True)
        return normalize(candidate, source)

    final = quality.repair_candidates(
        raw, original, prompt="Synthetic authorized source and clarification",
        prompt_limit=100_000,
        generate=lambda prompt: calls.append(True) or deepcopy(proposed),
        normalize=evaluate,
    )
    return {
        "initial": summary(original), "after": summary(final),
        "proposed_direct_compile": summary(normalize(proposed, source)),
        "generation_calls": len(calls), "repair_normalization_calls": len(normalizations),
        "new_result_selected": final is not original,
        "decision_gate": build_decision_gate(final),
    }


def observe_repair_routes() -> dict:
    raw, source = entity_batch(complete=9, shared_source=False)
    fixed = deepcopy(raw)
    fixed["entities"][-1]["properties"] = [{
        "name": "id", "data_type": "string", "is_key": True, "is_title": True,
    }]
    replan = deepcopy(raw)
    replan["entities"] = replan["entities"][:9]
    replan["coverage"][-1].update(
        status="context", reason="Tentative tenth object is withdrawn in a proposed revised plan",
        change_keys=[],
    )
    return {
        "structural_repair_positive_control": repair_observation(raw, source, fixed),
        "unchanged_failure_after_two_attempts": repair_observation(raw, source, raw),
        "revised_nine_object_plan_in_repair_lane": repair_observation(raw, source, replan),
    }


def observe_clarification_contract() -> dict:
    from app.services.assistant_decision_gate import build_decision_gate

    payload = {
        "entities": [{"key": "record", "name": "Record", "evidence_refs": ["doc:p1"]}],
        "coverage": [{"source_ref": "doc:p1", "status": "ambiguous",
                      "reason": "The source does not establish object identity", "change_keys": ["record"]}],
        "unresolved": [{
            "code": "document_reported_issue", "reported_code": "SOURCE_IDENTITY_UNRESOLVED",
            "message": "The source does not establish object identity", "blocking": True,
            "source_refs": ["doc:p1"], "affected_change_keys": ["record"],
            "resolution_hint": "Provide the business identity basis and an example",
        }],
    }
    gate = build_decision_gate(payload)
    proposed_resolution_fields = {
        "resolution_id", "requested_evidence", "acceptance_criteria",
        "answer_evaluation", "alternatives", "stop_reason",
    }
    return {
        "gate": gate,
        "question_field_names": sorted(gate["questions"][0]),
        "proposed_resolution_fields_absent_here": sorted(
            proposed_resolution_fields - set(gate["questions"][0])
        ),
        "interpretation": "This gate returns clarification text; it does not itself define an answer-resolution contract.",
    }


def main() -> int:
    sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = {
            "probe_contract": "advisor-resolution-loop-audit/v1",
            "python_version": sys.version.split()[0],
            "source_sha256": {
                name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                for name in SOURCE_FILES
            },
            "diagnostic_sha256": {
                name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                for name in (
                    "backend/scripts/audit_advisor_resolution_loop.py",
                    "backend/scripts/audit_ai_construction_quality.py",
                )
            },
            "observations": {
                "repair_routes": observe_repair_routes(),
                "clarification_contract": observe_clarification_contract(),
            },
            "limits": [
                "Replies and alternative plans are synthetic; no actual model behavior is measured.",
                "A structurally valid revised plan does not prove business validity or user authorization.",
                "Question-field observations apply to this decision gate, not every platform component.",
                "No PostgreSQL, browser, formal promotion, release or plugin was exercised.",
            ],
        }
    except Exception as exc:
        print(json.dumps({"audit_setup_failed": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
