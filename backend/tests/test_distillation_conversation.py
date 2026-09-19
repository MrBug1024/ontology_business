from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.distillation_conversation_schemas import ResourceSelection
from app.distillation_schemas import DistillationDocument, Evidence
from app.services.distillation_conversation_tools import (
    AskArguments,
    definitions,
    effective_tool_keys,
    execute,
)
from app.services.distillation_proposal_service import normalize_proposal
from app.services.distillation_artifact_service import generate_artifacts


def test_model_proposals_preserve_human_sources_decision_and_network_grants():
    original = DistillationDocument(decision="stop", decision_reason="Value has not been established",
        evidence=[{"key": "interview", "title": "Human interview"}],
        target_systems=[{"key": "portal", "name": "Portal", "base_url": "https://example.com",
            "purpose": "Read overview", "allowed_paths": ["/overview"]}])
    proposed = original.model_dump()
    proposed.update(decision="continue", decision_reason="AI says so", evidence=[], target_systems=[])
    proposed["assertions"] = [{"key": "claim", "statement": "Not verified", "status": "fact", "evidence_refs": ["interview"]}]
    result = normalize_proposal(proposed, original)
    assert result.decision == "stop"
    assert result.decision_reason == original.decision_reason
    assert result.evidence == original.evidence
    assert result.target_systems == original.target_systems
    assert result.assertions[0].status == "inference"
    artifacts = generate_artifacts("Business", 1, result)
    assert all("target_systems" not in item["content"] for item in artifacts)
    assert all("https://example.com" not in item["content"] for item in artifacts)


def test_questions_have_unique_ids_and_at_most_three_items():
    question = {"id": "result", "title": "Result", "question": "Who confirms?", "reason": "Evidence boundary", "options": []}
    with pytest.raises(ValidationError, match="不能重复"):
        AskArguments(message="Please clarify", questions=[question, question])
    with pytest.raises(ValidationError):
        AskArguments(message="Please clarify", questions=[{**question, "id": f"result_{index}"} for index in range(4)])


def test_proposal_restores_observed_sources_before_validating_citations():
    source = Evidence(key="observed", title="Actual observation")
    arguments = {"message": "Review the inference", "document": {
        "assertions": [{"key": "finding", "statement": "Needs review", "evidence_refs": ["observed"]}]}}
    result = execute(None, "propose_document", arguments, DistillationDocument(), None, observations=[source])
    assert result.proposal.evidence == [source]
    assert result.proposal.assertions[0].evidence_refs == ["observed"]
    arguments["document"]["assertions"][0]["evidence_refs"] = ["invented"]
    with pytest.raises(ValidationError, match="不存在的证据"):
        execute(None, "propose_document", arguments, DistillationDocument(), None, observations=[source])


def test_tools_cannot_read_unselected_sources_or_arbitrary_targets():
    document = DistillationDocument()
    for name, arguments in (("read_evidence", {"evidence_key": "foreign"}),
        ("read_target_system", {"target_key": "foreign", "page_path": "/"}),
        ("execute_sql", {"query": "select 1"})):
        with pytest.raises(ValueError):
            execute(None, name, arguments, document, None, observations=[])
    assert {item["function"]["name"] for item in definitions()} == {
        "list_evidence", "read_evidence", "read_current_document", "review_business",
        "read_target_system", "ask_human", "propose_document", "list_library_sources",
        "list_library_files", "read_library_source", "read_attachment",
        "open_business_system", "inspect_business_page", "navigate_business_page", "fill_business_query",
        "click_business_control", "login_business_system", "read_database_sample", "compare_database_samples", "record_human_statement"}


def test_explicit_tool_selection_limits_schemas_and_preserves_human_stopping_points():
    selected = effective_tool_keys(["list_evidence"])
    assert set(selected) == {"list_evidence", "ask_human", "propose_document"}
    assert {item["function"]["name"] for item in definitions(selected)} == set(selected)
    with pytest.raises(ValueError, match="未选择"):
        execute(None, "read_current_document", {}, DistillationDocument(), None, observations=[], allowed_tool_keys=selected)

    assert ResourceSelection().investigation_tool_keys is None
    assert ResourceSelection(investigation_tool_keys=[]).investigation_tool_keys == []
    with pytest.raises(ValidationError, match="始终可用"):
        ResourceSelection(investigation_tool_keys=["ask_human"])
