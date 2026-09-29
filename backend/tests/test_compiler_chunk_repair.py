import json

import pytest

from app.services import scenario_model_compiler as compiler


def _raw(ref):
    return {"schema_version": compiler.SCHEMA_VERSION, "entities": [], "relations": [],
        "instances": [], "functions": [], "actions": [], "rules": [], "events": [],
        "workflows": [], "mappings": [], "relation_mappings": [], "conceptual_mappings": [],
        "unresolved": [], "coverage": [{"source_ref": ref, "status": "context", "reason": "later", "change_keys": []}]}


def test_chunk_scope_error_requests_correction_before_accepting_replacement(monkeypatch):
    responses = iter([_raw("foreign:p1"), _raw("allowed:p1")])
    prompts = []
    def chat(cfg, messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return {"content": json.dumps(next(responses)), "raw": {"choices": [{"finish_reason": "stop"}]}}
    monkeypatch.setattr(compiler.scenario_model_response_service, "chat", chat)
    result = compiler._chat_raw_model(None, None, "source", max_tokens=1000,
        allowed_refs={"allowed:p1"}, request_timeout=1)
    assert result["coverage"][0]["source_ref"] == "allowed:p1"
    assert len(prompts) == 2
    assert "invalid_source_scope" in prompts[1]
    assert "allowed_refs" in prompts[1]


def test_repeated_foreign_source_never_becomes_inert_or_successful_candidate(monkeypatch):
    calls = []
    def chat(*args, **kwargs):
        calls.append(1)
        return {"content": json.dumps(_raw("foreign:p1"))}
    monkeypatch.setattr(compiler.scenario_model_response_service, "chat", chat)
    with pytest.raises(compiler._ChunkSourceScopeViolation):
        compiler._chat_raw_model(None, None, "source", max_tokens=1000,
            allowed_refs={"allowed:p1"}, request_timeout=1, attempts=2)
    assert len(calls) == 2
