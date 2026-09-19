from __future__ import annotations
import pytest
from pydantic import ValidationError
from app.distillation_schemas import DistillationDocument
from app.services.distillation_handoff_service import require_compilation_decision
from app.services.distillation_proposal_service import normalize_proposal


def test_case_cannot_invent_evidence_or_process_nodes():
    with pytest.raises(ValidationError, match="案例.*证据"):
        DistillationDocument(historical_cases=[{"key": "sample", "title": "历史结果", "result_refs": ["invented"]}])
    with pytest.raises(ValidationError, match="现状流程"):
        DistillationDocument(historical_cases=[{"key": "sample", "title": "历史结果", "steps": [{"node_key": "missing"}]}])



def test_adoptable_proposal_can_resolve_questions_through_conversation():
    original = DistillationDocument(open_questions=["结果由谁确认？"])
    raw = original.model_dump()
    raw["open_questions"] = []
    raw["as_is"] = {"nodes": [{"key": "confirm", "name": "结果确认", "owner": "请求人"}], "edges": []}
    proposed = normalize_proposal(raw, original)
    assert proposed.open_questions == []
    assert proposed.as_is.nodes[0].owner == "请求人"
    assert original.open_questions == ["结果由谁确认？"]


def test_handoff_respects_business_decision_without_topic_checklist():
    require_compilation_decision([{"business_decision": "continue"}])
    for decision in ("stop", "undecided"):
        with pytest.raises(ValueError, match="停止或未决"):
            require_compilation_decision([{"business_decision": decision}])
