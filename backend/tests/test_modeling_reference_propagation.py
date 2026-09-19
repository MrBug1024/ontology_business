"""Every compilation strategy consumes the same frozen non-evidence methods."""
import copy
from types import SimpleNamespace

import pytest

from app.routers import assistant
from app.services import modeling_reference_contract as references
from app.services import scenario_model_compiler as compiler
from app.services import ontology_service, workflow_service
from app.schemas import AssistantChatRequest
from test_assistant_resource_context import reference_document


def prepared(document):
    return {"mapping_catalog": [], "columns_by_table": {}, "working_drafts": [], "distillation_documents": [],
        "modeling_references": document, "fingerprint": compiler._context_fingerprint([], [], [], document)}


def test_reference_fingerprint_changes_without_adding_business_sources():
    first = prepared(reference_document())
    changed = prepared(reference_document("Ask a different question before proposing a relationship."))
    assert first["fingerprint"] != changed["fingerprint"]
    bundle = compiler.prepare_source_bundle_preview("An order belongs to a customer.", [], first)
    assert all("business facts support" not in item["text"] for item in bundle["paragraphs"])
    assert len(bundle["documents"]) == 1
    restored = assistant._restore_durable_prepared_context(assistant._durable_prepared_context(first))
    assert restored["modeling_references"] == first["modeling_references"]
    assert restored["fingerprint"] == first["fingerprint"]
    tampered = copy.deepcopy(first)
    tampered["modeling_references"]["references"][0]["content"] = "Different method"
    with pytest.raises(ValueError, match="指纹"):
        compiler.prepare_source_bundle_preview("An order belongs to a customer.", [], tampered)


def test_same_request_id_cannot_silently_adopt_changed_reference_content():
    payload = AssistantChatRequest(message="Inspect this model", skill_ids=["method"])
    first = assistant._assistant_route_fingerprint(payload, scope_key="scope", modeling_references=reference_document())
    changed = assistant._assistant_route_fingerprint(payload, scope_key="scope", modeling_references=reference_document("Different modeling method."))
    assert first != changed


@pytest.mark.parametrize("service,name", [(ontology_service, "generate_ontology"), (workflow_service, "generate_workflow")])
def test_legacy_builders_use_selected_model_and_distinct_method_context(monkeypatch, service, name):
    selected = SimpleNamespace(id="selected-model")
    scenario = SimpleNamespace(id="scenario", llm_config_id="different-model", description="Business description")
    db = SimpleNamespace(info={"tenant_id": "workspace"}, execute=lambda _stmt: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(service.llm_service, "routable_configs", lambda *_args: [selected])
    captured = []
    class StopAfterModelCall(Exception): pass
    def chat(model, messages, **_kwargs):
        captured.append((model, messages))
        raise StopAfterModelCall()
    monkeypatch.setattr(service.llm_service, "chat", chat)
    with pytest.raises(StopAfterModelCall):
        getattr(service, name)(db, scenario, "Customer belongs to an organization.",
            modeling_references=reference_document(), llm_config_id="selected-model")
    assert captured[0][0] is selected
    assert "business facts support each relationship" in captured[0][1][0]["content"]
    assert "business facts support each relationship" not in captured[0][1][1]["content"]


def test_legacy_builder_rejects_unavailable_explicit_model_without_fallback(monkeypatch):
    monkeypatch.setattr(ontology_service.llm_service, "routable_configs", lambda *_args: [SimpleNamespace(id="default-model")])
    monkeypatch.setattr(ontology_service.llm_service, "chat", lambda *_args, **_kwargs: pytest.fail("no fallback permitted"))
    with pytest.raises(ValueError, match="所选 AI 模型"):
        ontology_service.generate_ontology(SimpleNamespace(), SimpleNamespace(), "Business", llm_config_id="disabled-model")


def test_scenario_and_mapping_drafts_receive_distinct_methods(monkeypatch):
    selected = SimpleNamespace(id="selected-model")
    monkeypatch.setattr(assistant, "_llm", lambda _db: selected)
    captured = []
    def chat(_model, messages, **_kwargs):
        captured.append(messages)
        return {"content": '{"name":"Case handling","description":"Business scope","industry":""}'}
    monkeypatch.setattr(assistant.llm_service, "chat", chat)
    result = assistant._generate_scenario_draft(object(), "Business scope", modeling_references=reference_document())
    assert result["name"] == "Case handling"
    monkeypatch.setattr(assistant, "_mapping_catalog", lambda *_args: ([{"name": "cases"}], {}))
    monkeypatch.setattr(assistant, "_validate_mapping_draft", lambda _db, _scenario, raw, **_kwargs: raw)
    scenario = SimpleNamespace(entities=[SimpleNamespace(id="entity", name="Case", properties=[])])
    assistant._generate_mapping_draft(object(), scenario, "Business scope", modeling_references=reference_document())
    assert len(captured) == 2
    for messages in captured:
        assert "business facts support each relationship" in messages[0]["content"]
        assert "business facts support each relationship" not in messages[1]["content"]


def test_direct_compilation_receives_methods_separately_from_business_paragraphs(monkeypatch):
    document = reference_document()
    captured = []
    monkeypatch.setattr(compiler.permission_service, "require_scenario_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(compiler, "_existing_catalog", lambda *_args: {})
    monkeypatch.setattr(compiler, "normalize_scenario_model", lambda *_args, **_kwargs: {"unresolved": [{"blocking": True}]})
    def chat(_llm, messages, **_kwargs):
        captured.append(messages)
        return {"content": "{}"}
    monkeypatch.setattr(compiler.llm_service, "chat", chat)
    compiler.compile_scenario_model(SimpleNamespace(), SimpleNamespace(), message="An order belongs to a customer.",
        documents=[], llm=object(), prepared_context=prepared(document))
    assert len(captured) == 1
    prompt = captured[0][1]["content"]
    assert "business facts support each relationship" in prompt
    assert "独立建模方法与接口契约参考" in prompt
    assert prompt.index("独立建模方法与接口契约参考") < prompt.index("待逐段编译的业务语义来源")


def test_recursive_truncation_preserves_method_reference_for_both_child_chunks(monkeypatch):
    captured = []
    context = references.prompt(reference_document())
    monkeypatch.setattr(compiler, "_existing_catalog", lambda *_args: {})
    def chat(_db, _llm, prompt, **_kwargs):
        captured.append(prompt)
        if len(captured) == 1:
            raise compiler._CompilerOutputTruncated("split")
        return {}
    monkeypatch.setattr(compiler, "_chat_raw_model", chat)
    result = compiler._extract_chunk_models_recursively(object(), object(), message="Compile", llm=object(),
        mapping_catalog=[], paragraphs=[{"ref": "A1", "text": "Order belongs to customer."}, {"ref": "A2", "text": "Customer has a name."}],
        chunk_label="1", chunk_count=1, modeling_reference_context=context)
    assert result == [{}, {}]
    assert len(captured) == 3
    assert all("business facts support each relationship" in prompt for prompt in captured)


def test_parallel_chunk_session_receives_the_same_method_context(monkeypatch):
    captured = []
    class ChunkSession:
        info = {}
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, *_args): return SimpleNamespace(id="configured")
    monkeypatch.setattr(compiler, "Session", ChunkSession)
    monkeypatch.setattr(compiler, "_existing_catalog", lambda *_args: {})
    monkeypatch.setattr(compiler, "_chat_raw_model", lambda _db, _llm, prompt, **_kwargs: captured.append(prompt) or {})
    compiler._extract_chunk_once_in_isolated_session(object(), {}, "scenario", SimpleNamespace(id="configured"),
        message="Compile", mapping_catalog=[], paragraphs=[{"ref": "A1", "text": "An order belongs to a customer."}],
        chunk_label="1", chunk_count=1, call_budget=None, request_timeout=None, on_progress=None, task_scope="",
        modeling_reference_context=references.prompt(reference_document()))
    assert "business facts support each relationship" in captured[0]


@pytest.mark.parametrize("reason", ["large_input", "truncated", "timeout"])
def test_direct_fallback_passes_frozen_methods_to_chunk_compilation(monkeypatch, reason):
    observed = []
    monkeypatch.setattr(compiler.permission_service, "require_scenario_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(compiler, "_existing_catalog", lambda *_args: {})
    def fallback(*_args, **kwargs):
        observed.append(kwargs["modeling_reference_context"])
        return {"expected": "fallback"}
    def chat(*_args, **_kwargs):
        if reason == "timeout": raise TimeoutError("synthetic timeout")
        return {"content": "{}", "raw": {"choices": [{"finish_reason": "length"}]}}
    monkeypatch.setattr(compiler, "_compile_scenario_model_in_chunks", fallback)
    monkeypatch.setattr(compiler.llm_service, "chat", chat)
    if reason == "large_input": monkeypatch.setattr(compiler, "DIRECT_CHUNK_SOURCE_CHARS", 1)
    result = compiler.compile_scenario_model(SimpleNamespace(), SimpleNamespace(), message="An order belongs to a customer.",
        documents=[], llm=object(), prepared_context=prepared(reference_document()))
    assert result == {"expected": "fallback"}
    assert len(observed) == 1
    assert "business facts support each relationship" in observed[0]
