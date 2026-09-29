import pytest

from app.services import scenario_model_quality_service as quality


def test_invalid_workflow_is_repaired_with_validation_feedback():
    raw = {"workflows": [{"key": "flow", "nodes": [], "edges": [], "evidence_refs": ["doc:p1"]}]}
    result = {"unresolved": [{"code": "invalid_workflow", "message": "工作流缺少开始节点", "blocking": True}]}
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return {"workflows": [{"key": "flow", "nodes": [{"type": "start"}, {"type": "end"}],
            "edges": [{"source": "start", "target": "end"}], "evidence_refs": ["doc:p1"]}]}

    def normalize(candidate):
        assert candidate["workflows"][0]["nodes"]
        return {**candidate, "unresolved": []}

    repaired = quality.repair_candidates(raw, result, prompt="original evidence",
        prompt_limit=100_000, generate=generate, normalize=normalize)
    assert len(calls) == 1
    assert "工作流缺少开始节点" in calls[0]
    assert "doc:p1" in calls[0]
    assert repaired["workflows"][0]["edges"]
    assert raw["workflows"][0]["nodes"] == []


def test_repair_cannot_drop_definitions_to_appear_successful():
    result = {"unresolved": [{"code": "invalid_workflow"}]}
    calls = []
    repaired = quality.repair_candidates({"workflows": [{"key": "flow"}]}, result,
        prompt="source", prompt_limit=100_000,
        generate=lambda prompt: calls.append(prompt) or {"workflows": []},
        normalize=lambda raw: pytest.fail("Deleted candidate must not be accepted"))
    assert repaired is result
    assert len(calls) == quality.MAX_REPAIR_ATTEMPTS


def test_missing_business_evidence_is_not_repaired_by_guessing():
    result = {"unresolved": [{"code": "missing_evidence", "blocking": True}]}
    assert quality.repair_candidates({}, result, prompt="source", prompt_limit=100_000,
        generate=lambda prompt: pytest.fail("Evidence is required"), normalize=lambda raw: {}) is result


def test_repair_preserves_best_result_and_propagates_budget_failure():
    result = {"unresolved": [{"code": "invalid_workflow"}]}
    def fail(_prompt):
        raise RuntimeError("budget exhausted")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        quality.repair_candidates({}, result, prompt="source", prompt_limit=100_000,
            generate=fail, normalize=lambda raw: {})


def test_repair_cannot_erase_unresolved_business_questions():
    question = {"code": "missing_evidence", "message": "Need a source", "source_refs": ["doc:1"]}
    result = {"unresolved": [{"code": "invalid_workflow"}, question]}
    raw = {"workflows": [{"key": "flow"}]}
    assert quality.repair_candidates(raw, result, prompt="source", prompt_limit=100_000,
        generate=lambda prompt: raw, normalize=lambda raw: {"unresolved": []}) is result


def test_generated_chunk_conflict_can_be_repaired_without_erasing_source_conflict():
    raw = {"entities": [{"key": "subject"}]}
    source_conflict = {"code": "document_reported_issue", "reported_code": "SOURCE_CONFLICT", "message": "Two business sources disagree"}
    result = {"unresolved": [source_conflict, {"code": "document_reported_issue",
        "reported_code": "CHUNK_RESOURCE_CONFLICT", "message": "Generated optional flag disagrees"}]}
    repaired = quality.repair_candidates(raw, result, prompt="source", prompt_limit=100_000,
        generate=lambda prompt: raw, normalize=lambda raw: {"unresolved": [source_conflict]})
    assert repaired["unresolved"] == [source_conflict]


def test_wrong_citation_address_is_repairable_without_inventing_evidence():
    from app.services import scenario_model_compiler as compiler
    raw = {'entities':[{'key':'subject','evidence_refs':['embedded_evidence_key'],'confidence':1}]}
    def normalize(candidate):
        issues = []
        compiler._meta(candidate['entities'][0],key='subject',valid_sources={'document:p1'},unresolved=issues)
        return {**candidate,'unresolved':issues}
    initial = normalize(raw)
    assert [issue['code'] for issue in initial['unresolved']] == ['invalid_evidence_reference']
    fixed = {'entities':[{'key':'subject','evidence_refs':['document:p1'],'confidence':1}]}
    assert quality.repair_candidates(raw,initial,prompt='document:p1 content',prompt_limit=100_000,
        generate=lambda prompt: fixed,normalize=normalize)['unresolved'] == []


def test_existing_catalog_supplies_structural_flags_the_compiler_must_preserve():
    from app.models import BusinessScenario, OntologyEntity, OntologyProperty
    from app.services import scenario_model_compiler as compiler
    prop = OntologyProperty(name='reference',data_type='string',is_key=True,is_title=True,is_required=True,
        is_sensitive=True,constraints={'min_length':2})
    entity = OntologyEntity(id='entity',name='Subject',properties=[prop])
    scenario = BusinessScenario(id='scenario',name='Review',entities=[entity])
    actual = compiler._existing_catalog(scenario)['entities'][0]['properties'][0]
    assert actual['is_sensitive'] is True
    assert actual['constraints'] == {'min_length':2}


def test_empty_provider_output_does_not_create_fake_models():
    from app.services import scenario_model_compiler as compiler
    result = compiler._inert_contract_salvage_payload({}, source_bundle={
        "paragraphs": [{"ref": "doc:p1", "text": "A request"}],
        "documents": [], "fingerprint": "a" * 64,
    })
    assert result["draft_candidates"] == []
    assert result["unresolved"]
    result = compiler._mark_staged_task_result(result, 'ontology')
    assert result['generation']['generated_task_ids'] == []


@pytest.mark.parametrize('failure_code', [
    'COMPILER_PROVIDER_UNAVAILABLE', 'COMPILER_EXECUTION_INTERRUPTED',
])
def test_provider_failure_does_not_claim_business_ambiguity(failure_code):
    from app.services import scenario_model_compiler as compiler
    from app.services.assistant_decision_gate import build_decision_gate
    result = compiler._unavailable_compilation_result(source_bundle={
        'paragraphs': [{'ref': 'doc:p1'}], 'documents': [],
    }, on_progress=None, code=failure_code, message='模型连接失败')
    gate = build_decision_gate(result)
    assert gate['safe_to_formalize'] is False
    assert gate['questions'] == []
    assert gate['reason_codes'] == ['COMPILATION_UNAVAILABLE']
    assert result['coverage_summary']['ambiguous'] == 0
    assert result['draft_candidates'] == []


@pytest.mark.parametrize('scope', ['', 'ontology', 'capabilities', 'workflows'])
def test_every_compiler_prompt_teaches_the_required_protocol_version(monkeypatch, scope):
    from app.services import scenario_model_compiler as compiler
    monkeypatch.setattr(compiler, '_existing_catalog', lambda *args: {})
    prompt = compiler._compiler_prompt(None, message='', paragraphs=[], mapping_catalog=[], task_scope=scope)
    # A provider cannot infer a private version token from a field name.
    assert f'"{compiler.SCHEMA_VERSION}"' in prompt


def test_repair_passes_real_compiler_workflow_and_evidence_validation():
    from types import SimpleNamespace
    from copy import deepcopy
    from app.services import scenario_model_compiler as compiler
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    raw = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    raw['schema_version'] = compiler.SCHEMA_VERSION
    raw['workflows'] = [{'key': 'review', 'name': 'Review', 'evidence_refs': ['doc:p1'], 'confidence': 1,
                         'nodes': [], 'edges': [], 'trigger_type': 'manual'}]
    source = {'paragraphs': [{'ref': 'doc:p1', 'text': 'Summarize submitted text and preserve uncertainties.'}],
              'documents': [], 'fingerprint': 'a' * 64}
    def normalize(candidate):
        return compiler.normalize_scenario_model(None, scenario, candidate, source_bundle=source)
    initial = normalize(raw)
    assert any(item['code'] == 'invalid_workflow' for item in initial['unresolved'])
    candidate = deepcopy(raw)
    candidate['workflows'][0].update(nodes=[{'id': 'start', 'type': 'start'},
        {'id': 'summarize', 'type': 'llm', 'data': {'prompt': 'Summarize {{params.text}} and preserve uncertainties.'}},
        {'id': 'end', 'type': 'end', 'data': {'output': '{{summarize.parsed}}'}}],
        trigger_config={'ontology_contract': {'version': 1, 'entity_ids': [], 'input_bindings': [],
            'output_node_id': 'end', 'output_schema': {'type': 'object',
                'properties': {'summary': {'type': 'string'}}, 'required': ['summary'], 'additionalProperties': False}}},
        edges=[{'source': 'start', 'target': 'summarize'}, {'source': 'summarize', 'target': 'end'}])
    repaired = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda prompt: candidate, normalize=normalize)
    assert repaired['unresolved'] == []
    assert len(repaired['workflows'][0]['nodes']) == 3
    assert repaired['workflows'][0]['nodes'][1]['data']['prompt']
    assert repaired['workflows'][0]['evidence_refs'] == ['doc:p1']


def test_repair_cannot_pass_by_removing_existing_workflow_inputs():
    from copy import deepcopy
    raw = {'workflows': [{'key': 'review', 'name': 'Review', 'nodes': [
        {'id': 'organize', 'type': 'llm', 'data': {'prompt': 'Review {{params.materials}}'}}]}]}
    initial = {**deepcopy(raw), 'unresolved': [{'code': 'invalid_workflow_contract', 'blocking': True}]}
    candidate = deepcopy(raw)
    candidate['workflows'][0]['nodes'][0]['data']['prompt'] = 'Invent a summary'
    repaired = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda prompt: candidate, normalize=lambda value: {**value, 'unresolved': []})
    assert repaired == initial


def test_truncated_repair_retains_validated_candidates_without_restarting_compilation():
    raw = {'entities': [{'key': 'record'}]}
    initial = {'entities': [{'key': 'record', 'name': 'Record'}],
               'unresolved': [{'code': 'incomplete_source_attributes', 'blocking': True, 'message': 'Missing field'}]}
    attempts = []
    def truncated(prompt):
        attempts.append(prompt)
        raise quality.RepairOutputInvalid('truncated')
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=truncated, normalize=lambda _: pytest.fail('A truncated response must not be normalized'))
    assert len(attempts) == 1
    assert result['entities'] == initial['entities']
    assert result['unresolved'][0] == initial['unresolved'][0]
    assert result['unresolved'][-1]['code'] == 'candidate_repair_incomplete'
    assert result['unresolved'][-1]['blocking'] is False


def test_repair_does_not_hide_task_budget_exhaustion():
    from app.services.scenario_model_compiler import CompilationCallBudgetExceeded
    def exhausted(prompt):
        raise CompilationCallBudgetExceeded('Budget exhausted')
    with pytest.raises(CompilationCallBudgetExceeded):
        quality.repair_candidates({'entities': []}, {'unresolved': [{'code': 'invalid_workflow'}]},
            prompt='source', prompt_limit=100_000, generate=exhausted, normalize=lambda value: value)


def test_repair_cannot_rename_a_reused_entity_into_a_new_synonym():
    initial = {'unresolved': [{'code': 'incomplete_source_attributes'}]}
    raw = {'entities': [{'key': 'record', 'name': 'Existing record'}]}
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: {'entities': [{'key': 'record', 'name': 'Source table'}]},
        normalize=lambda _: pytest.fail('Renaming would change the reuse target'))
    assert result is initial


def test_generated_contract_failure_is_blocked_without_fabricating_business_questions():
    from app.services.assistant_decision_gate import build_decision_gate
    gate = build_decision_gate({'entities': [{'key': 'record', 'evidence_refs': ['doc:p1']}],
        'unresolved': [{'code': 'incomplete_source_attributes', 'blocking': True}]})
    assert gate['reason_codes'] == ['CANDIDATE_VALIDATION_FAILED']
    assert gate['safe_to_formalize'] is False
    assert gate['blocking_issue_count'] == 1
    assert gate['blocking_question_count'] == 0
    assert gate['questions'] == []


def test_real_source_gap_keeps_clarification_when_candidate_also_has_technical_errors():
    from app.services.assistant_decision_gate import build_decision_gate
    gate = build_decision_gate({'unresolved': [{'code': 'incomplete_source_attributes'},
        {'code': 'missing_evidence', 'message': 'The source does not identify a business key.'}]})
    assert gate['mode'] == 'clarify'
    assert gate['safe_to_formalize'] is False
    assert any(item['code'] == 'missing_evidence' for item in gate['questions'])
