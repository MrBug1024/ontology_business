from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.distillation_schemas import AnalyzeRequest, DistillationDocument
from app.services import distillation_analysis_service, distillation_service
from app.services.distillation_artifact_service import generate_artifacts, process_diagram


def test_fact_requires_explicit_evidence_and_all_graph_references_resolve():
    with pytest.raises(ValidationError, match="事实必须"):
        DistillationDocument(assertions=[{"key": "claim", "statement": "confirmed", "status": "fact"}])
    with pytest.raises(ValidationError, match="流程连线"):
        DistillationDocument(as_is={"nodes": [], "edges": [{"source": "a", "target": "b"}]})
    with pytest.raises(ValidationError, match="不存在的证据"):
        DistillationDocument(assertions=[{"key": "claim", "evidence_refs": ["missing"]}])
    with pytest.raises(ValidationError, match="实体"):
        DistillationDocument(lineage=[{"source": "a", "target": "b"}])


def test_closed_document_rejects_extra_fields_duplicate_keys_and_unbounded_content():
    with pytest.raises(ValidationError):
        DistillationDocument.model_validate({"password": "synthetic-value"})
    with pytest.raises(ValidationError, match="不能重复"):
        DistillationDocument(entities=[{"key": "a", "name": "A"}, {"key": "a", "name": "B"}])
    with pytest.raises(ValidationError):
        DistillationDocument(pain="x" * 4001)


def test_artifacts_are_real_deterministic_files_with_typed_handoff_and_safe_diagrams():
    document = DistillationDocument(
        beneficiary="使用者", pain="重复处理", desired_outcome="完成业务结果", success_metric="处理时间下降",
        evidence=[{"key": "ev", "title": "访谈", "kind": "observation", "role": "result", "summary": "人工访谈记录"}],
        assertions=[{"key": "claim", "statement": "尚待验证", "status": "hypothesis", "evidence_refs": ["ev"]}],
        as_is={"nodes": [{"key": "end", "name": 'A"]\nclick end "https://example.invalid"', "owner": "审核员", "outcome": "完成"}]},
        entities=[{"key": "request", "name": "请求", "attributes": ["编号"]}, {"key": "result", "name": "结果"}],
        relations=[{"source": "request", "target": "result", "cardinality": "one_to_many", "label": "产生"}],
        lineage=[{"source": "request", "target": "result", "transformation": "校验", "evidence_refs": ["ev"]}],
        decision="adjust", decision_reason="先核实证据", open_questions=["是否解决根因？"],
    )
    artifacts = generate_artifacts("研究", 2, document)
    assert artifacts == generate_artifacts("研究", 2, document)
    assert {item["key"] for item in artifacts} == {"brief", "as_is", "to_be", "er", "lineage", "contract"}
    for artifact in artifacts:
        assert artifact["sha256"] == hashlib.sha256(artifact["content"].encode()).hexdigest()
    assert json.loads(next(item["content"] for item in artifacts if item["key"] == "contract")) == document.model_dump(exclude={"target_systems"})
    brief = artifacts[0]["content"]
    assert "hypothesis" in brief and "待治理" in brief and "不是生产数据绑定" in brief
    assert "角色：result" in brief
    diagram = next(item["content"] for item in artifacts if item["key"] == "as_is")
    assert "\nclick" not in diagram and 'n_end["' in diagram and "&quot;" in diagram


def test_document_rejects_credentials_before_persistence():
    document = DistillationDocument(pain="password=synthetic-credential")
    with pytest.raises(HTTPException) as error:
        distillation_service.validate_document(None, document, None)
    assert error.value.status_code == 422


def test_unconfirmed_relationship_is_preserved_without_fabricating_er_cardinality():
    document = DistillationDocument(entities=[{"key": "request", "name": "请求"},
        {"key": "result", "name": "结果"}], relations=[{"source": "request", "target": "result",
        "label": "候选关联", "rationale": "尚未取得成对样本"}])
    assert document.relations[0].cardinality == "unconfirmed"
    artifacts = {item["key"]: item["content"] for item in generate_artifacts("待核对", 1, document)}
    assert json.loads(artifacts["contract"])["relations"][0]["cardinality"] == "unconfirmed"
    assert "基数待核对" in artifacts["er"]
    assert "||--" not in artifacts["er"] and "}o--" not in artifacts["er"]


def test_analysis_rejects_credential_instructions_before_model_call(monkeypatch):
    def forbidden_call(*args, **kwargs):
        pytest.fail("credentials must never reach the model")

    monkeypatch.setattr(distillation_analysis_service.llm_service, "chat", forbidden_call)
    with pytest.raises(HTTPException) as error:
        distillation_analysis_service.analyze(None, "project", AnalyzeRequest(
            expected_revision=1, instructions="password=synthetic-nonreusable-value",
        ))
    assert error.value.status_code == 422
    assert "synthetic" not in str(error.value.detail)


class AnalysisSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_ai_proposal_preserves_saved_document_and_downgrades_unverified_facts(monkeypatch):
    original = DistillationDocument(beneficiary="原受益者", evidence=[{"key": "ev", "title": "观察"}])
    project = SimpleNamespace(id="p", revision=3, scenario_id=None, document=original.model_dump())
    db = AnalysisSession()
    monkeypatch.setattr(distillation_service, "project", lambda *args, **kwargs: project)
    monkeypatch.setattr(distillation_service, "validate_document", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.permission_service, "refresh_request_authorization", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.llm_service, "routable_configs", lambda *args: [SimpleNamespace()])
    monkeypatch.setattr(distillation_analysis_service, "_collect_materials", lambda *args: ([], [], []))

    def chat(*args, **kwargs):
        assert db.commits == 1
        kwargs["before_provider_call"]()
        proposed = original.model_dump()
        proposed["beneficiary"] = "建议受益者"
        proposed["assertions"] = [{"key": "claim", "statement": "模型猜测", "status": "fact", "evidence_refs": ["ev"]}]
        return {"content": json.dumps(proposed)}

    monkeypatch.setattr(distillation_analysis_service.llm_service, "chat", chat)
    result = distillation_analysis_service.analyze(db, "p", AnalyzeRequest(expected_revision=3))
    assert result.base_revision == 3
    assert result.document.assertions[0].status == "inference"
    assert project.document == original.model_dump()
    assert db.commits == 2


def test_failed_or_stale_analysis_cannot_overwrite_human_edits(monkeypatch):
    original = DistillationDocument()
    project = SimpleNamespace(id="p", revision=1, scenario_id=None, document=original.model_dump())
    db = AnalysisSession()
    monkeypatch.setattr(distillation_service, "project", lambda *args, **kwargs: project)
    monkeypatch.setattr(distillation_service, "validate_document", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.permission_service, "refresh_request_authorization", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.llm_service, "routable_configs", lambda *args: [SimpleNamespace()])
    monkeypatch.setattr(distillation_analysis_service, "_collect_materials", lambda *args: ([], [], []))

    def chat(*args, **kwargs):
        project.revision = 2
        return {"content": original.model_dump_json()}

    monkeypatch.setattr(distillation_analysis_service.llm_service, "chat", chat)
    with pytest.raises(HTTPException) as error:
        distillation_analysis_service.analyze(db, "p", AnalyzeRequest(expected_revision=1))
    assert error.value.status_code == 409
    assert project.document == original.model_dump()


def test_unparseable_model_response_fails_closed(monkeypatch):
    project = SimpleNamespace(id="p", revision=1, scenario_id=None, document=DistillationDocument().model_dump())
    monkeypatch.setattr(distillation_service, "project", lambda *args, **kwargs: project)
    monkeypatch.setattr(distillation_service, "validate_document", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.llm_service, "routable_configs", lambda *args: [SimpleNamespace()])
    monkeypatch.setattr(distillation_analysis_service, "_collect_materials", lambda *args: ([], [], []))
    monkeypatch.setattr(distillation_analysis_service.llm_service, "chat", lambda *args, **kwargs: {"content": "not-json"})
    db = AnalysisSession()
    with pytest.raises(HTTPException) as error:
        distillation_analysis_service.analyze(db, "p", AnalyzeRequest(expected_revision=1))
    assert error.value.status_code == 502
    assert db.rollbacks == 1


def test_analysis_rejects_changed_evidence_identity(monkeypatch):
    project = SimpleNamespace(id="p", revision=1, scenario_id=None, document=DistillationDocument().model_dump())
    monkeypatch.setattr(distillation_service, "project", lambda *args, **kwargs: project)
    monkeypatch.setattr(distillation_service, "validate_document", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.permission_service, "refresh_request_authorization", lambda *args: None)
    monkeypatch.setattr(distillation_analysis_service.llm_service, "routable_configs", lambda *args: [SimpleNamespace()])
    monkeypatch.setattr(distillation_analysis_service, "_collect_materials", lambda *args: ([], [], []))
    identities = iter([{"connector_revision": 1}, {"connector_revision": 2}])
    monkeypatch.setattr(distillation_analysis_service, "capture_evidence_identity", lambda *args: next(identities))
    monkeypatch.setattr(distillation_analysis_service.llm_service, "chat", lambda *args, **kwargs: {
        "content": DistillationDocument().model_dump_json(),
    })
    with pytest.raises(HTTPException) as error:
        distillation_analysis_service.analyze(AnalysisSession(), "p", AnalyzeRequest(expected_revision=1))
    assert error.value.status_code == 409
    assert "证据" in error.value.detail


def test_stop_decision_blocks_compilation_and_handoff_is_fingerprint_bound():
    from app.services import scenario_model_compiler

    handoff = {"id": "distillation:publication", "filename": "业务蒸馏", "status": "parsed",
               "parsed_text": "受益者是使用者，目标是解决核心问题，先验证业务结果。", "usage_plane": "modeling_material",
               "business_decision": "continue"}
    context = {"mapping_catalog": [], "working_drafts": [], "distillation_documents": [handoff]}
    context["fingerprint"] = scenario_model_compiler._context_fingerprint([], [], [handoff])
    bundle = scenario_model_compiler.prepare_source_bundle_preview("请建设能力", [], context)
    assert any(item["source_id"] == handoff["id"] for item in bundle["documents"])
    handoff["parsed_text"] = "改变了不可变证据"
    with pytest.raises(ValueError, match="指纹"):
        scenario_model_compiler.prepare_source_bundle_preview("请建设能力", [], context)
    handoff["business_decision"] = "stop"
    context["fingerprint"] = scenario_model_compiler._context_fingerprint([], [], [handoff])
    with pytest.raises(ValueError, match="停止"):
        scenario_model_compiler.prepare_source_bundle_preview("请建设能力", [], context)


def test_legacy_advisor_provenance_roundtrip_retains_distillation_and_rejects_scope_or_hash(monkeypatch):
    from app.distillation_models import DistillationPublication
    from app.models import DataSource
    from app.routers import assistant
    from app.services import distillation_handoff_service

    artifacts = generate_artifacts("研究", 1, DistillationDocument())
    artifact = artifacts[0]
    source = SimpleNamespace(id="source", scenario_id="scenario", tenant_id="tenant")
    publication = SimpleNamespace(id="publication", artifacts=artifacts, document={"decision": "continue"})
    citation = {"kind": "distillation", "data_source_id": source.id,
                "publication_id": publication.id, "file_content_hash": artifact["sha256"], "filename": "研究"}
    proposal = {"kind": "ontology", "proposal_id": "proposal", "payload": {"entities": []}}
    user_message = SimpleNamespace(id="user_message", thread_id="thread", role="user", content="请建模")
    thread = SimpleNamespace(id="thread", scenario_id="scenario")
    message = SimpleNamespace(id="assistant_message", attachments=[citation], context={})
    message.context[assistant._LEGACY_MODELING_SOURCE_EVIDENCE_KEY] = assistant._legacy_modeling_source_evidence(
        kind="ontology", thread_id=thread.id, assistant_message_id=message.id, proposal=proposal,
        user_message_id=user_message.id, user_message=user_message.content, modeling_material_sources=[citation],
    )
    assert message.context[assistant._LEGACY_MODELING_SOURCE_EVIDENCE_KEY]["modeling_material_sources"][0]["publication_id"] == "publication"

    class CitationSession:
        def get(self, *args):
            return user_message

        def scalar(self, query):
            model = query.column_descriptions[0]["entity"]
            return source if model is DataSource else publication if model is DistillationPublication else None

    monkeypatch.setattr(distillation_handoff_service.permission_service, "require_principal", lambda *args: SimpleNamespace(tenant_id="tenant"))
    monkeypatch.setattr(distillation_service, "authorize_scope", lambda *args, **kwargs: None)
    db = CitationSession()
    assistant._require_legacy_modeling_source_evidence(db, thread, message, proposal)
    assert not assistant._has_invalid_historic_rag_source(db, thread, message)
    source.scenario_id = "another_scenario"
    with pytest.raises(HTTPException) as error:
        assistant._require_legacy_modeling_source_evidence(db, thread, message, proposal)
    assert error.value.status_code == 409
    assert assistant._has_invalid_historic_rag_source(db, thread, message)
    source.scenario_id = "scenario"
    citation["file_content_hash"] = "0" * 64
    with pytest.raises(HTTPException):
        assistant._require_legacy_modeling_source_evidence(db, thread, message, proposal)
