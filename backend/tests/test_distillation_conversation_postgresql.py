"""Real PostgreSQL conversation lifecycle, isolation, recovery and adoption tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.distillation_conversation_models import DistillationConversationTurn as Turn
from app.distillation_conversation_schemas import ResourceSelection, TurnCreate
from app.distillation_models import DistillationProject
from app.config import SKILLS_DIR
from app.distillation_schemas import DistillationDocument, Evidence, ProjectCreate, ProjectUpdate
from app.models import LLMConfig, MCPConfig, OrganizationMember, Skill
from app.services import distillation_conversation_service as service, distillation_conversation_worker as worker
from app.services import distillation_conversation_lease as leases, distillation_service, llm_service
from app.services import distillation_resource_service as resources, mcp_resource_service, distillation_mcp_evidence_service
from distillation_scripted_llm import call, install_scripted_llm
from isolated_postgresql import isolated_database, isolated_postgresql, seed_workspace, tenant_session


def setup_project(isolated, document=None, *, shared=False):
    workspace = seed_workspace(isolated.admin_engine)
    # Some lifecycle tests only exercise durable enqueue/lease behavior.  The
    # fallback keeps those paths valid without displacing the scripted model
    # installed by tests that actually invoke a provider.
    with Session(isolated.admin_engine) as db:
        db.add(LLMConfig(
            tenant_id=workspace["tenant_id"],
            name="Test investigation fallback",
            model="test-investigation-fallback",
            capabilities=["chat", "tool"],
            enabled=True,
            routing_priority=1_000,
        ))
        db.commit()
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = distillation_service.create_project(db, ProjectCreate(name="Investigation",
            scenario_id=None if shared else workspace["scenario_id"], document=document or DistillationDocument()))
        db.commit()
        project_id = row.id
    return workspace, project_id, sessionmaker(bind=isolated.runtime_engine)


def enqueue(isolated, workspace, project_id, request_id="request_one", message="What is the real business outcome?", revision=1):
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(request_id=request_id, message=message, expected_revision=revision))
        db.commit()
        return row.id


def read(isolated, turn_id):
    with Session(isolated.runtime_engine) as db:
        return service.public_turn(db.get(Turn, turn_id))


def test_empty_project_conversation_clarifies_then_proposes_without_writing_until_adoption(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    try:
        first = enqueue(isolated, workspace, project_id)
        assert worker.process_next_turn(session_factory=factory)
        first_turn = read(isolated, first)
        assert first_turn.status == "waiting"
        assert [item.tool_name for item in first_turn.steps] == ["list_evidence", "review_business", "ask_human"]
        assert len(first_turn.questions) == 1
        assert first_turn.proposal is None
        assert not worker.process_next_turn(session_factory=factory)
        second = enqueue(isolated, workspace, project_id, "request_two", "The requester confirms that the issue is resolved")
        assert worker.process_next_turn(session_factory=factory)
        second_turn = read(isolated, second)
        assert second_turn.status == "succeeded"
        assert second_turn.proposal.desired_outcome
        with tenant_session(isolated.runtime_engine, workspace) as db:
            project = db.get(DistillationProject, project_id)
            assert project.revision == 1 and not project.document["desired_outcome"]
            page = service.list_turns(db, project_id, 1, None)
            assert page.has_more and page.turns[0].id == second
            assert service.list_turns(db, project_id, 1, 2).turns[0].id == first
            applied = service.apply(db, project_id, second, 1)
            db.commit()
            assert applied.revision == 2
            assert applied.document["decision"] == "undecided"
            assert service.apply(db, project_id, second, 1).revision == 2
            with pytest.raises(HTTPException) as undecided:
                distillation_service.publish(db, project_id, 2)
            assert undecided.value.status_code == 422
            reviewed = DistillationDocument.model_validate(applied.document).model_copy(update={
                "decision": "continue", "decision_reason": "Human approved a bounded pilot"})
            distillation_service.update_project(db, project_id, ProjectUpdate(name=applied.name,
                scenario_id=workspace["scenario_id"], expected_revision=2, document=reviewed))
            db.commit()
            publication = distillation_service.publish(db, project_id, 3)
            db.commit()
            assert publication.project_revision == 3 and len(publication.artifacts) == 7
    finally:
        restore()


def test_request_id_and_single_active_turn_are_serialized_across_connections(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup_project(isolated)
    barrier = Barrier(2)
    def submit(request_id):
        barrier.wait(timeout=10)
        try:
            return enqueue(isolated, workspace, project_id, request_id)
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["same_request", "same_request"]))
    assert results[0] == results[1]
    with pytest.raises(HTTPException) as mismatch:
        enqueue(isolated, workspace, project_id, "same_request", "Different body")
    assert mismatch.value.status_code == 409
    with pytest.raises(HTTPException) as active:
        enqueue(isolated, workspace, project_id, "different_request")
    assert active.value.status_code == 409
    with tenant_session(isolated.runtime_engine, workspace) as db:
        service.cancel(db, project_id, results[0])
        db.commit()


def test_oversized_tool_batch_is_not_executed_and_model_can_correct_it(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    turn_id = enqueue(isolated, workspace, project_id)
    requests = []

    def model(_cfg, messages, **_kwargs):
        requests.append(messages)
        if len(requests) == 1:
            return {"content": "", "tool_calls": [call("list_evidence", {}) for _ in range(6)]}
        feedback = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        assert len(feedback) == 6
        assert all(item["status"] == "not_executed" and item["max_calls"] == 4 for item in feedback)
        return {"content": "", "tool_calls": [call("propose_document", {
            "message": "请核对阶段建议", "document": {"open_questions": ["需要真实历史结果"]}})]}

    monkeypatch.setattr(llm_service, "chat", model)
    assert worker.process_next_turn(session_factory=factory)
    result = read(isolated, turn_id)
    assert result.status == "succeeded"
    assert [step.tool_name for step in result.steps] == ["propose_document"]
    assert result.proposal.open_questions == ["需要真实历史结果"]
    assert result.applied_revision is None


def configured_resources(isolated, workspace):
    suffix = uuid4().hex
    with Session(isolated.admin_engine) as db:
        llm = LLMConfig(tenant_id=workspace["tenant_id"], name=f"Selected model {suffix}",
            model="selected-investigation-model", capabilities=["chat", "tool"], enabled=True)
        skill = Skill(tenant_id=workspace["tenant_id"], name=f"Selected skill {suffix}",
            description="A trusted method package", source="builtin", path=str(SKILLS_DIR / "data-analyzer"), enabled=True)
        mcp = MCPConfig(tenant_id=workspace["tenant_id"], name=f"Selected MCP {suffix}",
            transport="streamable_http", url="https://private.example.invalid/mcp", enabled=True)
        db.add_all([llm, skill, mcp])
        db.commit()
        return llm.id, skill.id, mcp.id


def test_turn_freezes_safe_selected_resources_and_worker_uses_selected_llm(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    llm_id, skill_id, mcp_id = configured_resources(isolated, workspace)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(request_id="selected_resources", expected_revision=1,
            message="Investigate the configured resources", resource_selection=ResourceSelection(
                llm_config_id=llm_id, skill_ids=[skill_id], mcp_ids=[mcp_id])))
        db.commit()
        turn_id = row.id
        snapshot = row.context["resource_selection"]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["llm"]["id"] == llm_id
    assert snapshot["skills"][0]["id"] == skill_id
    assert snapshot["mcps"][0]["id"] == mcp_id
    assert "private.example.invalid" not in serialized and "/trusted/" not in serialized
    assert "api_key" not in serialized and "headers" not in serialized

    calls = []
    def model(cfg, messages, **_kwargs):
        calls.append(cfg.id)
        assert any(item["role"] == "system" and "read_selected_skill" in item["content"] for item in messages)
        assert {item["function"]["name"] for item in _kwargs["tools"]}.issuperset(
            {"read_selected_skill", "list_mcp_resources", "read_mcp_resource"})
        return {"content": "已按本轮受管配置完成调查。", "tool_calls": []}
    monkeypatch.setattr(llm_service, "chat", model)

    assert worker.process_next_turn(session_factory=factory)
    assert calls == [llm_id]
    assert read(isolated, turn_id).status == "succeeded"


def test_worker_reads_selected_skill_and_mcp_materials_before_asking_human(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    llm_id, skill_id, mcp_id = configured_resources(isolated, workspace)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(request_id="actual_resource_reads", expected_revision=1,
            message="Investigate and ask about gaps", resource_selection=ResourceSelection(
                llm_config_id=llm_id, skill_ids=[skill_id], mcp_ids=[mcp_id], investigation_tool_keys=[])))
        db.commit()
        turn_id = row.id
    remote_calls = []
    def list_resources(cfg):
        remote_calls.append(("list", cfg.id))
        return {"resources": [{"uri": "memo://bounded-source", "name": "Prior result", "description": "An observed source"}], "has_more": False}
    def read_resource(cfg, uri):
        remote_calls.append(("read", cfg.id, uri))
        return {"text": "The prior result names no responsible owner.", "read_only": True}
    monkeypatch.setattr(mcp_resource_service, "list_resources", list_resources)
    monkeypatch.setattr(mcp_resource_service, "read_resource", read_resource)
    rounds = []
    def model(_cfg, messages, **kwargs):
        rounds.append(messages)
        names = {item["function"]["name"] for item in kwargs["tools"]}
        assert names == {"read_selected_skill", "list_mcp_resources", "read_mcp_resource", "ask_human", "propose_document"}
        if len(rounds) == 1:
            return {"content": "", "tool_calls": [call("read_selected_skill", {"skill_id": skill_id}),
                call("list_mcp_resources", {"mcp_id": mcp_id})]}
        if len(rounds) == 2:
            result = json.loads(messages[-1]["content"])
            return {"content": "", "tool_calls": [call("read_mcp_resource", {
                "mcp_id": mcp_id, "resource_key": result["resources"][0]["resource_key"]})]}
        assert "no responsible owner" in messages[-1]["content"]
        return {"content": "", "tool_calls": [call("ask_human", {"message": "资料没有说明负责人，需要你确认。",
            "questions": [{"id": "owner", "title": "负责人", "question": "谁确认最终结果？", "reason": "已读资料未记录负责人", "options": []}]})]}
    monkeypatch.setattr(llm_service, "chat", model)
    with factory() as db:
        lease = leases.claim(db)
        db.commit()
    worker.execute_claim(lease, factory)
    result = read(isolated, turn_id)
    assert result.status == "waiting" and result.proposal is None
    assert [item.tool_name for item in result.steps] == ["read_selected_skill", "list_mcp_resources", "read_mcp_resource", "ask_human"]
    assert all(item.status == "succeeded" for item in result.steps)
    mcp_receipt = result.steps[2].mcp
    assert mcp_receipt and mcp_receipt.read_only
    assert "no responsible owner" in mcp_receipt.summary
    evidence_key = mcp_receipt.evidence_key
    assert remote_calls == [("list", mcp_id), ("list", mcp_id), ("read", mcp_id, "memo://bounded-source")]
    with Session(isolated.runtime_engine) as db:
        row = db.get(Turn, turn_id)
        assert len(row.context["mcp_resource_keys"][mcp_id]) == 1
        checkpoint = json.dumps(row.checkpoint, ensure_ascii=False)
        assert "描述性统计" in checkpoint and "no responsible owner" in checkpoint
        assert "memo://" not in checkpoint and "private.example.invalid" not in checkpoint
        assert db.get(DistillationProject, project_id).revision == 1
        assert row.context["mcp_reads"][0]["evidence"]["key"] == evidence_key

    # A human answer starts a new turn. The frozen MCP evidence survives the
    # clarification, can be reread without contacting the provider, and is
    # adopted into the document and immutable handoff provenance.
    followup_calls = []
    def followup_model(_cfg, messages, **_kwargs):
        followup_calls.append(messages)
        if len(followup_calls) == 1:
            history = json.dumps(messages, ensure_ascii=False)
            assert evidence_key in history and "no responsible owner" in history
            return {"content": "", "tool_calls": [call("read_evidence", {"evidence_key": evidence_key})]}
        observed = json.loads(messages[-1]["content"])
        assert observed["observation"]["text"] == "The prior result names no responsible owner."
        document = DistillationDocument(beneficiary="Result owner", pain="Missing responsibility", desired_outcome="An accountable result",
            success_metric="Every result has one owner", evidence=[{**observed["evidence"], "summary": "Model attempted to overwrite the observation"}],
            assertions=[{"key": "missing_owner", "statement": "The source omits the responsible owner", "status": "fact", "evidence_refs": [evidence_key]}])
        return {"content": "", "tool_calls": [call("propose_document", {"message": "形成待采用建议。", "document": document.model_dump()})]}
    monkeypatch.setattr(llm_service, "chat", followup_model)
    second_id = enqueue(isolated, workspace, project_id, "answer_owner", "The accountable team lead confirms results")
    assert worker.process_next_turn(session_factory=factory)
    second = read(isolated, second_id)
    assert second.status == "succeeded" and second.proposal
    assert second.proposal.evidence[0].key == evidence_key
    assert second.proposal.evidence[0].summary == mcp_receipt.summary
    assert second.proposal.assertions[0].status == "inference"
    assert len(remote_calls) == 3
    with tenant_session(isolated.runtime_engine, workspace) as db:
        applied = service.apply(db, project_id, second_id, 1)
        db.commit()
        approved = DistillationDocument.model_validate(applied.document).model_copy(update={
            "decision": "continue", "decision_reason": "Human approved the bounded investigation"})
        distillation_service.update_project(db, project_id, ProjectUpdate(name=applied.name,
            scenario_id=workspace["scenario_id"], expected_revision=2, document=approved))
        db.commit()
        publication = distillation_service.publish(db, project_id, 3)
        db.commit()
        provenance = json.loads(distillation_service.artifact_content(publication, "provenance")["content"])
        observed = provenance["evidence"][0]
        assert observed["basis"] == "server_readonly_mcp_observation"
        assert observed["receipt"]["evidence_key"] == evidence_key
        assert observed["observation"]["text"] == "The prior result names no responsible owner."
        assert "memo://" not in json.dumps(provenance) and "private.example.invalid" not in json.dumps(provenance)
        assert publication.document["evidence"][0]["mcp_read"]["identity_sha256"] == mcp_receipt.identity_sha256
    with tenant_session(isolated.runtime_engine, workspace) as db:
        with pytest.raises(HTTPException) as wrong_scenario:
            distillation_mcp_evidence_service.resolve_read(db, approved.evidence[0], workspace["other_scenario_id"])
        assert wrong_scenario.value.status_code == 404
        forged = approved.evidence[0].model_copy(update={"mcp_read": approved.evidence[0].mcp_read.model_copy(
            update={"identity_sha256": "0" * 64})})
        with pytest.raises(HTTPException) as forged_receipt:
            distillation_mcp_evidence_service.resolve_read(db, forged, workspace["scenario_id"])
        assert forged_receipt.value.status_code == 422
    foreign = seed_workspace(isolated.admin_engine)
    with tenant_session(isolated.runtime_engine, foreign) as db:
        with pytest.raises(HTTPException) as wrong_tenant:
            distillation_mcp_evidence_service.resolve_read(db, approved.evidence[0], foreign["scenario_id"])
        assert wrong_tenant.value.status_code == 404


def test_worker_rejects_unselected_mcp_without_contacting_it(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    turn_id = enqueue(isolated, workspace, project_id)
    calls = []
    monkeypatch.setattr(mcp_resource_service, "list_resources", lambda cfg: calls.append(cfg.id))
    monkeypatch.setattr(llm_service, "chat", lambda *_args, **_kwargs: {
        "content": "", "tool_calls": [call("list_mcp_resources", {"mcp_id": "unselected"})]})
    assert worker.process_next_turn(session_factory=factory)
    assert read(isolated, turn_id).status == "failed" and calls == []


def test_connector_change_during_mcp_read_rejects_result_before_checkpoint(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    llm_id, _skill_id, mcp_id = configured_resources(isolated, workspace)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(request_id="changed_during_read", expected_revision=1,
            message="Read authorized material", resource_selection=ResourceSelection(llm_config_id=llm_id, mcp_ids=[mcp_id])))
        db.commit()
        turn_id = row.id
    def changing_list(_cfg):
        with Session(isolated.admin_engine) as db:
            db.get(MCPConfig, mcp_id).url = "https://changed.example.invalid/mcp"
            db.commit()
        return {"resources": [{"uri": "memo://late-result", "name": "Late source", "description": ""}], "has_more": False}
    monkeypatch.setattr(mcp_resource_service, "list_resources", changing_list)
    monkeypatch.setattr(llm_service, "chat", lambda *_args, **_kwargs: {
        "content": "", "tool_calls": [call("list_mcp_resources", {"mcp_id": mcp_id})]})
    assert worker.process_next_turn(session_factory=factory)
    assert read(isolated, turn_id).status == "failed"
    with Session(isolated.runtime_engine) as db:
        row = db.get(Turn, turn_id)
        assert "mcp_resource_keys" not in row.context
        assert "Late source" not in json.dumps(row.checkpoint)


def test_global_resource_catalog_and_selection_enforce_enabled_tenant_and_trusted_package(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup_project(isolated)
    llm_id, skill_id, mcp_id = configured_resources(isolated, workspace)
    foreign_workspace = seed_workspace(isolated.admin_engine)
    foreign_llm, foreign_skill, foreign_mcp = configured_resources(isolated, foreign_workspace)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        catalog = resources.resource_catalog(db)
        assert llm_id in {item.id for item in catalog.models}
        assert skill_id in {item.id for item in catalog.skills}
        assert mcp_id in {item.id for item in catalog.mcps}
        serialized = catalog.model_dump_json()
        assert all(value not in serialized for value in [foreign_llm, foreign_skill, foreign_mcp, "private.example.invalid", str(SKILLS_DIR)])
        for selection in (ResourceSelection(skill_ids=[foreign_skill]), ResourceSelection(mcp_ids=[foreign_mcp]), ResourceSelection(llm_config_id=foreign_llm)):
            with pytest.raises(HTTPException) as failure:
                service.resolve_resource_selection(db, selection)
            assert failure.value.status_code == 409
    with Session(isolated.admin_engine) as db:
        db.get(MCPConfig, mcp_id).enabled = False
        db.get(Skill, skill_id).path = "/outside/trusted/package"
        db.commit()
    with tenant_session(isolated.runtime_engine, workspace) as db:
        catalog = resources.resource_catalog(db)
        assert mcp_id not in {item.id for item in catalog.mcps}
        assert skill_id not in {item.id for item in catalog.skills}
        for selection in (ResourceSelection(skill_ids=[skill_id]), ResourceSelection(mcp_ids=[mcp_id])):
            with pytest.raises(HTTPException) as failure:
                service.resolve_resource_selection(db, selection)
            assert failure.value.status_code == 409


def test_selected_investigation_tools_bound_provider_and_reject_unselected_calls(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(
            request_id="tool_selection",
            expected_revision=1,
            message="Only inspect known evidence",
            resource_selection=ResourceSelection(investigation_tool_keys=["list_evidence"]),
        ))
        db.commit()
        turn_id = row.id
        snapshot = row.context["resource_selection"]
    assert snapshot["version"] == 3
    assert snapshot["investigation_tools"] == {
        "mode": "selected",
        "selected_tool_keys": ["list_evidence"],
        "effective_tool_keys": ["list_evidence", "ask_human", "propose_document"],
    }

    offered = []
    def model(_cfg, _messages, **kwargs):
        offered.append({item["function"]["name"] for item in kwargs["tools"]})
        return {"content": "", "tool_calls": [call("read_current_document", {})]}

    monkeypatch.setattr(llm_service, "chat", model)
    assert worker.process_next_turn(session_factory=factory)
    assert offered == [{"list_evidence", "ask_human", "propose_document"}]
    row = read(isolated, turn_id)
    assert row.status == "failed" and row.steps == []


@pytest.mark.parametrize("changed_resource", ["llm", "skill", "mcp"])
def test_worker_fails_closed_when_frozen_selected_resource_changes(isolated_postgresql, monkeypatch, changed_resource):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    llm_id, skill_id, mcp_id = configured_resources(isolated, workspace)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = service.enqueue(db, project_id, TurnCreate(request_id=f"changed_{changed_resource}", expected_revision=1,
            message="Investigate a frozen selection", resource_selection=ResourceSelection(
                llm_config_id=llm_id, skill_ids=[skill_id], mcp_ids=[mcp_id])))
        db.commit()
        turn_id = row.id
    with Session(isolated.admin_engine) as db:
        if changed_resource == "llm":
            db.get(LLMConfig, llm_id).model = "changed-investigation-model"
        elif changed_resource == "skill":
            db.get(Skill, skill_id).description = "Changed trusted package description"
        else:
            db.get(MCPConfig, mcp_id).url = "https://changed.example.invalid/mcp"
        db.commit()

    calls = []
    monkeypatch.setattr(llm_service, "chat", lambda cfg, *_args, **_kwargs: calls.append(cfg.id))
    assert worker.process_next_turn(session_factory=factory)
    assert calls == []
    assert read(isolated, turn_id).status == "failed"


def test_expired_worker_is_fenced_and_cancellation_rejects_late_result(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    turn_id = enqueue(isolated, workspace, project_id)
    with factory() as db:
        first = leases.claim(db)
        db.commit()
    with factory() as db:
        assert leases.claim(db) is None
        db.rollback()
    with Session(isolated.admin_engine) as db:
        db.get(Turn, turn_id).lease_expires_at = leases.now() - timedelta(seconds=1)
        db.commit()
    with factory() as db:
        second = leases.claim(db)
        db.commit()
    assert second.generation == first.generation + 1
    with pytest.raises(leases.LeaseLost):
        worker._finish(first, factory, "succeeded", message="Late first worker")
    with tenant_session(isolated.runtime_engine, workspace) as db:
        assert service.cancel(db, project_id, turn_id).status == "cancelled"
        db.commit()
    with pytest.raises(leases.LeaseLost):
        worker._finish(second, factory, "succeeded", message="Late cancelled worker")
    assert read(isolated, turn_id).assistant_message == ""


def test_waiting_question_is_atomic_and_batched_proposal_is_never_executed(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    turn_id = enqueue(isolated, workspace, project_id)
    try:
        def model(_cfg, _messages, **kwargs):
            assert not kwargs["db"].in_transaction()
            return {"content": "", "tool_calls": [call("ask_human", {"message": "Please decide", "questions": [
                {"id": "scope", "title": "Scope", "question": "Which outcome?", "reason": "Human choice", "options": []}]}),
                call("propose_document", {"message": "Illegal continuation", "document": DistillationDocument(pain="AI answered itself").model_dump()})]}
        monkeypatch.setattr(llm_service, "chat", model)
        assert worker.process_next_turn(session_factory=factory)
        row = read(isolated, turn_id)
        assert row.status == "waiting" and row.questions[0].id == "scope"
        assert row.proposal is None and [step.tool_name for step in row.steps] == ["ask_human"]
        assert all(step.status == "succeeded" for step in row.steps)
        assert not worker.process_next_turn(session_factory=factory)
    finally:
        restore()


def test_worker_revalidates_membership_and_edited_project_before_returning_results(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    for revoke in (False, True):
        workspace, project_id, factory = setup_project(isolated)
        restore = install_scripted_llm(isolated.admin_engine, workspace)
        turn_id = enqueue(isolated, workspace, project_id)
        try:
            def model(_cfg, _messages, **_kwargs):
                with Session(isolated.admin_engine) as db:
                    if revoke:
                        db.get(OrganizationMember, workspace["member_id"]).status = "suspended"
                    else:
                        db.get(DistillationProject, project_id).revision = 2
                    db.commit()
                return {"content": "Should not be delivered", "tool_calls": []}
            monkeypatch.setattr(llm_service, "chat", model)
            assert worker.process_next_turn(session_factory=factory)
            result = read(isolated, turn_id)
            assert result.status == "failed" and not result.assistant_message
        finally:
            restore()


def test_conversation_rejects_cross_tenant_access_and_project_scope_rebinding(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup_project(isolated)
    foreign = seed_workspace(isolated.admin_engine)
    turn_id = enqueue(isolated, workspace, project_id)
    with tenant_session(isolated.runtime_engine, foreign) as db:
        for action in (lambda: service.get_turn(db, project_id, turn_id),
            lambda: service.list_turns(db, project_id, 20, None), lambda: service.cancel(db, project_id, turn_id)):
            with pytest.raises(HTTPException) as error:
                action()
            assert error.value.status_code == 404
    with tenant_session(isolated.runtime_engine, workspace) as db:
        with pytest.raises(HTTPException) as error:
            distillation_service.update_project(db, project_id, ProjectUpdate(name="Moved",
                scenario_id=workspace["other_scenario_id"], expected_revision=1))
        assert error.value.status_code == 409
        db.rollback()
        service.cancel(db, project_id, turn_id)
        db.commit()


def test_web_observation_is_real_durable_evidence_and_fake_receipt_is_rejected(isolated_postgresql, monkeypatch):
    from app.services import distillation_target_service

    isolated = isolated_postgresql
    document = DistillationDocument(beneficiary="Requester", pain="No verified outcome", desired_outcome="Resolved issue",
        success_metric="Resolution time", decision="continue", decision_reason="Human-approved pilot",
        target_systems=[{"key": "portal", "name": "Portal",
        "base_url": "https://example.com", "purpose": "Read overview", "allowed_paths": ["/overview"]}])
    workspace, project_id, factory = setup_project(isolated, document, shared=True)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    turn_id = enqueue(isolated, workspace, project_id)
    snapshot = {"target_key": "portal", "url": "https://example.com/overview", "title": "Overview",
        "status": "observed", "text": "A request may be submitted.", "content_sha256": "a" * 64,
        "retrieved_at": leases.now().isoformat(), "limitations": ["Anonymous single-page observation"],
        "read_only": True, "visible_fields": [], "allowed_links": []}
    monkeypatch.setattr(distillation_target_service, "read_target_system", lambda *_args: snapshot)
    earlier_evidence_key = ""
    try:
        def model(_cfg, messages, **_kwargs):
            if any("待澄清问题：" in item.get("content", "") for item in messages):
                if messages[-1]["role"] != "tool":
                    return {"content": "", "tool_calls": [call("read_evidence", {"evidence_key": earlier_evidence_key})]}
                assert "A request may be submitted." in messages[-1]["content"]
                return {"content": "", "tool_calls": [call("propose_document", {"message": "Review earlier page observation", "document": document.model_dump()})]}
            if messages[-1]["role"] != "tool":
                return {"content": "", "tool_calls": [call("read_target_system", {"target_key": "portal", "page_path": "/overview"})]}
            return {"content": "", "tool_calls": [call("ask_human", {"message": "Confirm page interpretation", "questions": [
                {"id": "page", "title": "Page", "question": "Does this reflect the intended process?", "reason": "Single-page limit", "options": []}]})]}
        monkeypatch.setattr(llm_service, "chat", model)
        assert worker.process_next_turn(session_factory=factory)
        assert read(isolated, turn_id).status == "waiting"
        earlier_evidence_key = "web_" + read(isolated, turn_id).steps[0].id[:20]
        def forbid_second_fetch(*_args):
            raise AssertionError("Follow-up must read saved evidence, not repeat HTTP")
        monkeypatch.setattr(distillation_target_service, "read_target_system", forbid_second_fetch)
        followup_id = enqueue(isolated, workspace, project_id, "confirmed_page", "Yes, preserve this observation")
        assert worker.process_next_turn(session_factory=factory)
        turn = read(isolated, followup_id)
        assert turn.status == "succeeded"
        assert [item.tool_name for item in turn.steps] == ["read_evidence", "propose_document"]
        evidence = turn.proposal.evidence[0]
        assert evidence.investigation_source.turn_id == turn_id
        with tenant_session(isolated.runtime_engine, workspace) as db:
            service.apply(db, project_id, followup_id, 1)
            db.commit()
            publication = distillation_service.publish(db, project_id, 2)
            db.commit()
            provenance = distillation_service.artifact_content(publication, "provenance")["content"]
            assert "server_readonly_web_observation" in provenance
            assert "A request may be submitted." in provenance
            copied = distillation_service.create_project(db, ProjectCreate(name="Scoped copy",
                scenario_id=workspace["scenario_id"], document=turn.proposal))
            db.commit()
            copied_publication = distillation_service.publish(db, copied.id, 1)
            db.commit()
            assert turn_id in distillation_service.artifact_content(copied_publication, "provenance")["content"]
            fake = evidence.model_copy(update={"investigation_source": evidence.investigation_source.model_copy(update={"step_id": uuid4().hex})})
            with pytest.raises(HTTPException) as failure:
                distillation_service.validate_document(db, DistillationDocument(evidence=[fake]), workspace["scenario_id"])
            assert failure.value.status_code == 422
    finally:
        restore()


def test_crashed_worker_resumes_completed_read_checkpoint_without_repeating_the_tool(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    turn_id = enqueue(isolated, workspace, project_id)
    checkpoint = worker._checkpoint
    class SimulatedCrash(BaseException):
        pass
    def crashing_checkpoint(lease, session_factory, messages):
        checkpoint(lease, session_factory, messages)
        if messages[-1]["role"] == "tool":
            raise SimulatedCrash()
    def model(_cfg, messages, **_kwargs):
        if messages[-1]["role"] == "tool":
            return {"content": "Resumed from the saved read", "tool_calls": []}
        return {"content": "", "tool_calls": [call("read_current_document", {})]}
    monkeypatch.setattr(llm_service, "chat", model)
    monkeypatch.setattr(worker, "_checkpoint", crashing_checkpoint)
    try:
        with pytest.raises(SimulatedCrash):
            worker.process_next_turn(session_factory=factory)
        assert read(isolated, turn_id).status == "running"
        with Session(isolated.admin_engine) as db:
            db.get(Turn, turn_id).lease_expires_at = leases.now() - timedelta(seconds=1)
            db.commit()
        monkeypatch.setattr(worker, "_checkpoint", checkpoint)
        assert worker.process_next_turn(session_factory=factory)
        result = read(isolated, turn_id)
        assert result.status == "succeeded" and len(result.steps) == 1
        with factory() as db:
            row = db.get(Turn, turn_id)
            assert row.attempt == 2 and row.model_calls == 2
    finally:
        restore()


def test_stale_adoption_never_overwrites_a_human_edit(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    turn_id = enqueue(isolated, workspace, project_id)
    monkeypatch.setattr(llm_service, "chat", lambda *_args, **_kwargs: {"content": "", "tool_calls": [
        call("propose_document", {"message": "Suggested outcome", "document": DistillationDocument(pain="Suggested pain").model_dump()})]})
    try:
        assert worker.process_next_turn(session_factory=factory)
        with tenant_session(isolated.runtime_engine, workspace) as db:
            distillation_service.update_project(db, project_id, ProjectUpdate(name="Human edit",
                scenario_id=workspace["scenario_id"], expected_revision=1, document=DistillationDocument(pain="Human pain")))
            db.commit()
            with pytest.raises(HTTPException) as stale:
                service.apply(db, project_id, turn_id, 2)
            assert stale.value.status_code == 409
            db.rollback()
            assert db.get(DistillationProject, project_id).document["pain"] == "Human pain"
            assert db.get(Turn, turn_id).applied_revision is None
    finally:
        restore()


def test_conversation_runtime_contract_and_empty_migration_roundtrip_refuse_history_loss():
    from scripts.verify_postgresql_runtime import _verify_distillation_conversation_contract

    with isolated_database() as isolated:
        with isolated.runtime_engine.connect() as connection:
            report = _verify_distillation_conversation_contract(connection)
            assert report["active_turn_unique"] is True
        isolated.migrate("20260918_36", downgrade=True)
        isolated.migrate(isolated.head)
        workspace, project_id, _factory = setup_project(isolated)
        enqueue(isolated, workspace, project_id)
        with pytest.raises(RuntimeError, match="history exists"):
            isolated.migrate("20260918_36", downgrade=True)
