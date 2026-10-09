"""Observe AI construction quality gates with synthetic, in-memory definitions.

No model, database session, external I/O or business record is used. Synthetic
repair responses exercise the real repair policy and compiler, not an LLM.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "backend/app/distillation_schemas.py",
    "backend/app/services/distillation_proposal_service.py",
    "backend/app/services/distillation_service.py",
    "backend/app/services/distillation_conversation_worker.py",
    "backend/app/services/distillation_conversation_tools.py",
    "backend/app/services/distillation_conversation_service.py",
    "backend/app/services/distillation_model_coverage.py",
    "backend/app/services/scenario_model_compiler.py",
    "backend/app/services/scenario_model_quality_service.py",
)


def scenario() -> SimpleNamespace:
    return SimpleNamespace(
        id="synthetic", namespace="test", entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[],
        data_mappings=[], relation_data_mappings=[],
    )


def empty_output() -> dict:
    from app.services import scenario_model_compiler as compiler

    return {
        "schema_version": compiler.SCHEMA_VERSION,
        **{key: [] for key in (
            *compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, "coverage", "unresolved",
        )},
    }


def normalize(raw: dict, source: dict) -> dict:
    from app.services import scenario_model_compiler as compiler

    return compiler.normalize_scenario_model(
        None, scenario(), raw, source_bundle=source,
    )


def summary(result: dict) -> dict:
    candidates = [
        item for item in result["draft_candidates"] if item["resource_kind"] == "entity"
    ]
    return {
        "normalized_entity_count": len(result["entities"]),
        "blocking_codes": dict(Counter(
            item["code"] for item in result["unresolved"] if item.get("blocking")
        )),
        "entity_candidate_statuses": dict(Counter(
            item["validation_status"] for item in candidates
        )),
        "promotion_eligible_entity_count": sum(
            item["promotion_eligible"] for item in candidates
        ),
        "safe_change_count": result["applyability"]["safe_change_count"],
        "blocked_change_count": result["applyability"]["blocked_change_count"],
    }


def entity_batch(*, complete: int, shared_source: bool) -> tuple[dict, dict]:
    raw = empty_output()
    paragraphs = []
    for index in range(10):
        ref = "doc:p1" if shared_source else f"doc:p{index + 1}"
        entity = {
            "key": f"record_{index}", "name": f"Record {index}",
            "evidence_refs": [ref], "confidence": 1,
            "properties": [{
                "name": "id", "data_type": "string", "is_key": True,
                "is_title": True,
            }] if index < complete else [],
        }
        raw["entities"].append(entity)
        if not shared_source or index == 0:
            paragraphs.append({
                "ref": ref, "text": "Each record has one unique string identifier id.",
            })
            raw["coverage"].append({
                "source_ref": ref, "status": "modeled", "reason": "Records defined",
                "change_keys": [],
            })
        raw["coverage"][-1]["change_keys"].append(entity["key"])
    return raw, {"paragraphs": paragraphs, "documents": [], "fingerprint": "b" * 64}


def observe_distillation() -> dict:
    from pydantic import ValidationError
    from app.distillation_schemas import DistillationDocument, Entity
    from app.services.distillation_proposal_service import normalize_proposal
    from app.services.distillation_service import validate_document

    names = [{"key": f"record_{i}", "name": f"Record {i}"} for i in range(10)]
    document = DistillationDocument.model_validate({"entities": names})
    validate_document(None, document, None)
    populated = document.model_copy(update={
        "beneficiary": "Synthetic reviewer", "pain": "Repeated manual comparison",
        "desired_outcome": "Verified comparison", "success_metric": "Exact expected fields",
        "decision": "continue", "decision_reason": "Synthetic reviewed decision",
    })
    replacement = normalize_proposal({}, populated)
    try:
        Entity.model_validate({"key": "record", "name": "Record", "attributes": [{
            "name": "amount", "data_type": "number", "is_required": True,
        }]})
    except ValidationError:
        typed_attributes_accepted = False
    else:
        typed_attributes_accepted = True
    return {
        "name_only_entity_count": len(document.entities),
        "empty_attribute_entity_count": sum(not entity.attributes for entity in document.entities),
        "empty_identity_entity_count": sum(not entity.identity for entity in document.entities),
        "document_schema_and_current_safety_validator_accept": True,
        "typed_attribute_record_accepted": typed_attributes_accepted,
        "empty_replacement_proposal": {
            "accepted": True, "before_entities": len(populated.entities),
            "after_entities": len(replacement.entities),
            "after_value_fields": {key: getattr(replacement, key) for key in (
                "beneficiary", "pain", "desired_outcome", "success_metric",
            )},
            "decision_preserved": replacement.decision == populated.decision,
        },
    }


def semantic_fixture() -> tuple[dict, dict]:
    from app.services import scenario_model_compiler as compiler
    from app.services.distillation_model_coverage import source_entities

    attributes = [
        "Identifier (unique)", "Amount: required non-negative number",
        "Occurred at: required UTC datetime",
    ]
    content = json.dumps({"entities": [{
        "key": "source_record", "name": "Record", "attributes": attributes,
    }], "decision": "continue"})
    source = compiler.build_source_bundle("", [], complete_handoffs=[{
        "id": "handoff", "filename": "Synthetic governed contract", "status": "parsed",
        "parsed_text": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "usage_plane": "modeling_material",
    }])
    ref = next(iter(source_entities(source["paragraphs"])))[0]
    raw = empty_output()
    raw["entities"] = [{
        "key": "record", "name": "Record", "evidence_refs": [ref], "confidence": 1,
        "properties": [
            {"name": "id", "data_type": "string", "is_key": True, "is_title": True},
            {"name": "amount", "data_type": "string"},
            {"name": "when", "data_type": "string"},
        ],
        "source_entity_bindings": [{
            "source_ref": ref, "source_key": "source_record",
            "attribute_map": dict(zip(attributes, ("id", "amount", "when"), strict=True)),
        }],
    }]
    raw["coverage"] = [{
        "source_ref": paragraph["ref"], "status": "modeled" if paragraph["ref"] == ref else "context",
        "reason": "Entity or decision", "change_keys": ["record"] if paragraph["ref"] == ref else [],
    } for paragraph in source["paragraphs"]]
    return raw, source


def observe_semantic_repair() -> dict:
    from app.services import scenario_model_quality_service as quality

    raw, source = semantic_fixture()
    compiled = normalize(raw, source)
    calls = []
    quality.repair_candidates(
        raw, compiled, prompt="Synthetic source", prompt_limit=100_000,
        generate=lambda prompt: calls.append(prompt) or raw,
        normalize=lambda candidate: normalize(candidate, source),
    )
    properties = {prop["name"]: prop for prop in compiled["entities"][0]["properties"]}
    checks = {
        "amount_numeric": properties["amount"]["data_type"] in {"number", "float", "integer"},
        "amount_required": properties["amount"]["is_required"],
        "amount_non_negative": properties["amount"]["constraints"].get("minimum") == 0,
        "when_datetime": properties["when"]["data_type"] == "datetime",
        "when_required": properties["when"]["is_required"],
    }
    positive = deepcopy(raw)
    positive["entities"][0]["properties"][1].update(
        data_type="number", is_required=True, constraints={"minimum": 0},
    )
    positive["entities"][0]["properties"][2].update(data_type="datetime", is_required=True)
    positive_result = normalize(positive, source)
    positive_properties = {p["name"]: p for p in positive_result["entities"][0]["properties"]}
    return {
        **summary(compiled), "repair_generation_calls": len(calls),
        "source_requirements": ["amount numeric", "amount required", "amount non-negative", "when datetime", "when required"],
        "actual_properties": {name: {key: properties[name][key] for key in (
            "data_type", "is_required", "constraints",
        )} for name in ("amount", "when")},
        "independent_business_checks": checks,
        "independent_business_requirements_satisfied": sum(checks.values()),
        "correct_semantics_positive_control": {
            **summary(positive_result),
            "amount": {key: positive_properties["amount"][key] for key in ("data_type", "is_required", "constraints")},
            "when": {key: positive_properties["when"][key] for key in ("data_type", "is_required", "constraints")},
        },
        "interpretation": "Attribute-name coverage passes; semantic requirements are not compiler blockers.",
    }


def observe_omitted_source_entities() -> dict:
    from app.services import scenario_model_compiler as compiler
    from app.services.distillation_model_coverage import source_entities

    content = json.dumps({"entities": [{
        "key": f"source_{i}", "name": f"Record {i}", "attributes": ["Unique identifier"],
    } for i in range(10)], "decision": "continue"})
    source = compiler.build_source_bundle("Build all ten entities", [], complete_handoffs=[{
        "id": "handoff", "filename": "Synthetic ten-entity contract", "status": "parsed",
        "parsed_text": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "usage_plane": "modeling_material",
    }])
    expected = source_entities(source["paragraphs"])
    ref = next(ref for ref, key in expected if key == "source_0")
    raw = empty_output()
    raw["entities"] = [{
        "key": "record_0", "name": "Record 0", "evidence_refs": [ref], "confidence": 1,
        "properties": [{"name": "id", "data_type": "string", "is_key": True, "is_title": True}],
        "source_entity_bindings": [{"source_ref": ref, "source_key": "source_0",
                                    "attribute_map": {"Unique identifier": "id"}}],
    }]
    raw["coverage"] = [{
        "source_ref": p["ref"], "status": "modeled" if p["ref"] == ref else "context",
        "reason": "Entity or decision", "change_keys": ["record_0"] if p["ref"] == ref else [],
    } for p in source["paragraphs"]]
    return {
        **summary(normalize(raw, source)),
        "source_entity_inventory_count": len({key for _, key in expected}),
        "generated_entity_count": len(raw["entities"]),
        "interpretation": "A paragraph marked modeled does not require every source entity to be generated.",
    }


def observe_nested_repair_regression() -> dict:
    from app.services import scenario_model_quality_service as quality

    raw, source = entity_batch(complete=10, shared_source=True)
    raw["entities"] = raw["entities"][:1]
    raw["entities"][0]["properties"].append({"name": "amount", "data_type": "number"})
    source["paragraphs"][0]["text"] = "Record 0 has a unique id and a numeric amount; define its workflow."
    raw["workflows"] = [{
        "key": "flow", "name": "Synthetic flow", "evidence_refs": ["doc:p1"],
        "confidence": 1, "trigger_type": "manual", "trigger_config": {},
        "nodes": [], "edges": [],
    }]
    raw["coverage"][0]["change_keys"] = ["record_0", "flow"]
    fixed = deepcopy(raw)
    fixed["entities"][0]["properties"] = fixed["entities"][0]["properties"][:1]
    fixed["workflows"][0].update(
        nodes=[{"id": "start", "type": "start", "name": "Start", "data": {}},
               {"id": "end", "type": "end", "name": "End", "data": {"output": {}}}],
        edges=[{"source": "start", "target": "end", "label": ""}],
    )
    initial = normalize(raw, source)
    calls = []
    repaired = quality.repair_candidates(
        raw, initial, prompt=source["paragraphs"][0]["text"], prompt_limit=100_000,
        generate=lambda prompt: calls.append(prompt) or fixed,
        normalize=lambda candidate: normalize(candidate, source),
    )
    return {
        "initial": summary(initial), "after": summary(repaired),
        "repair_generation_calls": len(calls),
        "original_property_names": [p["name"] for p in initial["entities"][0]["properties"]],
        "retained_property_names": [p["name"] for p in repaired["entities"][0]["properties"]],
        "repair_response_selected": repaired is not initial,
        "interpretation": "Synthetic response fixes workflow but drops an evidenced nested property; real policy selects it.",
    }


def collect_observations() -> dict:
    batches = {}
    for label, complete, shared in (
        ("ten_name_only", 0, False), ("ten_complete_positive_control", 10, True),
        ("nine_complete_one_incomplete_shared_source", 9, True),
        ("nine_complete_one_incomplete_separate_sources", 9, False),
    ):
        raw, source = entity_batch(complete=complete, shared_source=shared)
        batches[label] = summary(normalize(raw, source))
    return {
        "probe_contract": "ai-construction-quality-audit/v1",
        "python_version": sys.version.split()[0],
        "source_sha256": {name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                          for name in SOURCE_FILES},
        "observations": {
            "distillation_sparse_and_replacement": observe_distillation(),
            "entity_batch_quality_and_attribution": batches,
            "semantic_coverage_and_repair": observe_semantic_repair(),
            "omitted_source_entity_inventory": observe_omitted_source_entities(),
            "nested_definition_preservation_in_repair": observe_nested_repair_regression(),
        },
        "limits": [
            "No real LLM, PostgreSQL, browser, publication or plugin was exercised.",
            "Repair responses are supplied synthetically; this does not measure actual model success rates.",
            "Eligibility is the compiler projection, not persisted promotion or business execution.",
            "Sparse documents may be legitimate investigation drafts; no readiness stage was supplied.",
        ],
    }


def main() -> int:
    sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        observations = collect_observations()
    except Exception as exc:
        print(json.dumps({"audit_setup_failed": type(exc).__name__}))
        return 1
    print(json.dumps(observations, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
