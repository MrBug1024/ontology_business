from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler


@pytest.mark.parametrize("message", [
    "请修正当前模型，把关联方向改为单向并保留审批步骤。",
    "请只创建一个函数，绑定指定版本的 Provider，不重建其他模型。",
    "继续完善映射，将金额字段映射到 amount，不得使用历史运行数据。",
])
def test_specific_instructions_remain_auditable_sources(message):
    assert compiler._request_business_source(message, has_documents=True) == message


@pytest.mark.parametrize("message", ["继续", "继续优化", "重新编译"])
def test_semantics_free_continuation_does_not_create_business_evidence(message):
    assert compiler._request_business_source(message, has_documents=True) == ""


def test_each_chunk_preserves_the_current_requested_scope(monkeypatch):
    monkeypatch.setattr(compiler, "_existing_catalog", lambda *args: {})
    request = "只建设指定函数，其他附件内容仅作背景。"
    prompt = compiler._compiler_prompt(SimpleNamespace(), message=request,
        paragraphs=[{"ref": "doc:p0001", "text": "Other business context"}],
        mapping_catalog=[], task_scope="ontology", chunk_index=1, chunk_count=2)
    assert request in prompt
    assert "request:p" not in prompt


def test_function_progress_uses_its_actual_topic():
    from app.services.assistant_compilation_job_service import compilation_plan
    plan = compilation_plan(document_count=1, source_count=2, total_characters=100, task_scope="capabilities")
    generation = next(item for item in plan if item["id"] == "ontology")
    assert generation["title"] == "建设业务能力"


def test_persisted_progress_keeps_the_scope_title_when_generic_stage_arrives(monkeypatch):
    from app.services import assistant_compilation_job_service as jobs
    plan = jobs.compilation_plan(document_count=1, source_count=2, total_characters=100, task_scope='capabilities')
    job = SimpleNamespace(status='running', progress={'steps': plan}, llm_calls_used=1, llm_call_budget=5)
    monkeypatch.setattr(jobs, '_fresh_job', lambda *args: job)
    captured = {}
    monkeypatch.setattr(jobs, '_persist_running_values', lambda db, job, values, **kw: captured.update(values))
    jobs.record_progress(None, 'job', step_id='ontology', title='建设本体模型', detail='正在生成')
    progress = captured['progress']
    assert next(item for item in progress['steps'] if item['id'] == 'ontology')['title'] == '建设业务能力'
    assert progress['activities'][-1]['title'] == '建设业务能力'


def test_authored_table_port_contract_is_accepted_by_the_real_validator():
    from app.services.assistant_capability_modeling_service import normalize_managed_data_port_declarations
    from app.services.managed_port_authoring import authoring_context
    declaration = {"port_key": "current_data", "direction": "input", "role": "invocation_input",
        "media_kind": "structured", "binding_policy": "per_invocation", "cardinality": "one",
        "evidence_kind": "versioned_data", "evidence_refs": ["request:p0001"],
        "schema_document": {}, "binding_kinds": ["asset_version", "dataset_version"]}
    result = normalize_managed_data_port_declarations([declaration], resource_kind="function",
        resource_key="query", resource_evidence_refs=["request:p0001"], resource_confidence=1)
    assert result[0]["port"]["role"] == "invocation_input"
    assert 'invocation_input' in authoring_context()
