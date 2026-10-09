"""Observe scenario construction/delivery gaps using synthetic, in-memory inputs.

This diagnostic does not start the application, create a database session,
invoke a model, contact external systems, or alter business records. Rejections
are observations rather than a failing regression-test expectation.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
import sys
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "backend/app/services/scenario_model_compiler.py",
    "backend/app/services/capability_readiness_service.py",
    "backend/app/services/builtin_capability_providers.py",
    "backend/app/services/policies.py",
    "backend/app/services/function_definition_service.py",
    "backend/app/services/release_service.py",
    "backend/app/services/scenario_model_evaluator.py",
    "backend/app/providers/semantic_audit.py",
)


def observe(operation: Callable[[], object]) -> dict[str, object]:
    try:
        operation()
    except ValueError as exc:
        return {"accepted": False, "reason": str(exc)}
    return {"accepted": True}


def compile_synthetic_function(
    *, include_structural_context: bool = False, omit_execution: bool = False,
) -> dict:
    from app.services import scenario_model_compiler as compiler

    scenario = SimpleNamespace(
        id="synthetic", namespace="test", entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[],
        data_mappings=[], relation_data_mappings=[],
    )
    raw = {
        key: []
        for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, "coverage", "unresolved")
    }
    raw["schema_version"] = compiler.SCHEMA_VERSION
    raw["functions"] = [{
        "key": "days", "name": "Calculate days", "evidence_refs": ["doc:p1"],
        "confidence": 1, "runtime_kind": "contract", "runtime_config": {},
        "input_schema": {
            "type": "object", "properties": {
                "start": {"type": "string", "format": "date"},
                "end": {"type": "string", "format": "date"},
            }, "required": ["start", "end"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object", "properties": {"days": {"type": "integer"}},
            "required": ["days"], "additionalProperties": False,
        },
    }]
    raw["coverage"] = [{
        "source_ref": "doc:p1", "status": "modeled",
        "reason": "Date calculation modeled", "change_keys": ["days"],
    }]
    paragraphs = [{
        "ref": "doc:p1",
        "text": "Return the exact number of days between the start and end dates.",
    }]
    if omit_execution:
        raw["functions"] = []
        raw["coverage"][0].update(
            status="context", reason="Implementation retained for a later task", change_keys=[],
        )
    if include_structural_context:
        raw["entities"] = [{
            "key": "record", "name": "Record", "evidence_refs": ["doc:p2"],
            "confidence": 1, "properties": [{
                "name": "id", "data_type": "string", "is_key": True, "is_title": True,
            }],
        }]
        raw["workflows"] = [] if omit_execution else [{
            "key": "flow", "name": "Synthetic flow", "evidence_refs": ["doc:p2"],
            "confidence": 1, "trigger_type": "manual", "trigger_config": {},
            "nodes": [
                {"id": "start", "type": "start", "name": "Start", "data": {}},
                {"id": "end", "type": "end", "name": "End",
                 "data": {"output": {"state": "synthetic"}}},
            ], "edges": [{"source": "start", "target": "end", "label": ""}],
        }]
        raw["coverage"].append({
            "source_ref": "doc:p2", "status": "modeled", "reason": "Structural context",
            "change_keys": ["record"] if omit_execution else ["record", "flow"],
        })
        paragraphs.append({
            "ref": "doc:p2",
            "text": "Define a Record with a unique id and a manual flow returning state synthetic.",
        })
    return compiler.normalize_scenario_model(None, scenario, raw, source_bundle={
        "paragraphs": paragraphs, "documents": [], "fingerprint": "a" * 64,
    })


def observe_publication_executors() -> dict[str, object]:
    from app.services.release_service import _assert_publishable_runtime_bindings

    publication = {}
    for executor in ("template", "skill", "http", "script"):
        publication[executor] = observe(
            lambda executor=executor: _assert_publishable_runtime_bindings({
                "actions": [{
                    "id": "synthetic", "name": "Synthetic output", "enabled": True,
                    "executor_type": executor, "executor_config": {},
                }],
            })
        )
    return publication


def observe_structural_evaluator() -> dict[str, object]:
    from app.services.scenario_model_evaluator import evaluate_scenario_model

    gold = compile_synthetic_function(include_structural_context=True)
    missing_execution = compile_synthetic_function(
        include_structural_context=True, omit_execution=True,
    )
    evaluation = evaluate_scenario_model(missing_execution, gold)
    return {
        "gold_compile_blocking_issues": [
            item["code"] for item in gold["unresolved"] if item.get("blocking")
        ],
        "predicted_compile_blocking_issues": [
            item["code"] for item in missing_execution["unresolved"] if item.get("blocking")
        ],
        "gold_function_count": len(gold["functions"]),
        "gold_workflow_count": len(gold["workflows"]),
        "predicted_function_count": len(missing_execution["functions"]),
        "predicted_workflow_count": len(missing_execution["workflows"]),
        "categories": evaluation["categories"],
        "micro_f1": evaluation["metrics"]["micro"]["f1"],
        "evaluation_scope": evaluation["evaluation_scope"],
        "business_meaning_verified": evaluation["business_meaning_verified"],
    }


def collect_observations() -> dict[str, object]:
    # The shared compiler composition loads trusted provider registration.
    from app.services import scenario_model_compiler as compiler
    from app.providers.builtin_function_evaluator import evaluate_function
    from app.providers.semantic_audit import normalize_spec
    from app.services.builtin_capability_providers import OntologyWorkflowProvider
    from app.services.capability_contracts import ResolvedDataHandle, RuntimeDataContext
    from app.services.capability_readiness_service import capability_readiness
    from app.services.function_definition_service import normalize_definition
    from app.services.policies import validate_workflow_graph

    nodes = [
        {"id": "start", "type": "start", "data": {}},
        {"id": "calculate", "type": "function",
         "data": {"function_id": "synthetic-function"}},
        {"id": "end", "type": "end", "data": {}},
    ]
    edges = [
        {"source": "start", "target": "calculate", "label": ""},
        {"source": "calculate", "target": "end", "label": ""},
    ]
    compiled = compile_synthetic_function()
    function = SimpleNamespace(**compiled["functions"][0])
    readiness = capability_readiness("function", function)
    handle = ResolvedDataHandle(
        port_key="records", binding_kind="dataset_version",
        reference_id="synthetic-version", signature="a" * 64,
        version_id="synthetic-version",
    )
    context = RuntimeDataContext(handles=(handle,))
    spec = {
        "spec_version": "semantic-audit/v1", "rule_code": "synthetic_manual",
        "assessment_mode": "manual", "required_evidence": ["proof"],
    }
    threshold = SimpleNamespace(
        runtime_kind="threshold",
        runtime_config={"field": "value", "threshold": 10, "operator": ">"},
        input_schema={
            "type": "object", "properties": {"value": {"type": "number"}},
            "required": ["value"], "additionalProperties": False,
        },
    )
    return {
        "probe_contract": "scenario-delivery-audit/v1",
        "python_version": sys.version.split()[0],
        "source_sha256": {
            name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        },
        "observations": {
            "function_workflow_node": observe(lambda: validate_workflow_graph(nodes, edges)),
            "date_difference_builtin": observe(lambda: normalize_definition({
                **{key: compiled["functions"][0][key] for key in (
                    "name", "input_schema", "output_schema", "runtime_config",
                )}, "runtime_kind": "date_difference",
            })),
            "modeled_but_nonexecutable": {
                "compiled_functions": len(compiled["functions"]),
                "blocking_issues": [
                    item["code"] for item in compiled["unresolved"] if item.get("blocking")
                ],
                "coverage_status": compiled["coverage"][0]["status"],
                "executable": readiness.executable,
                "blocked_reasons": list(readiness.blocked_reasons),
            },
            "workflow_managed_input": observe(
                lambda: OntologyWorkflowProvider()._require_supported_data_context(context)
            ),
            "provider_rule_authoring": {
                "provider_accepts": normalize_spec(SimpleNamespace(condition=spec)) is not None,
                "compiler": observe(lambda: compiler.normalize_rule_condition(spec)),
            },
            "publication_executors": observe_publication_executors(),
            "structural_evaluator_without_execution": observe_structural_evaluator(),
            "positive_threshold_control": evaluate_function(threshold, {"value": 11}),
        },
        "limits": [
            "No real PostgreSQL, MinIO, browser, LLM, release or external client was exercised.",
            "Private service boundaries are observed; this is not an authenticated end-to-end test.",
            "Synthetic schemas and records are not business acceptance evidence.",
        ],
    }


def main() -> int:
    sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        observations = collect_observations()
    except Exception as exc:
        # Import/configuration errors can contain deployment values. Keep this
        # external diagnostic boundary value-free and clearly unsuccessful.
        print(json.dumps({"audit_setup_failed": type(exc).__name__}))
        return 1
    print(json.dumps(observations, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
