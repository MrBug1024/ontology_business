from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler
from app.services import workflow_service
from app.services import workflow_authoring_data as authoring
from app.services.policies import PolicyViolation


def _compiled_workflow(approval_data=None):
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    raw = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    raw['schema_version'] = compiler.SCHEMA_VERSION
    raw['workflows'] = [{'key': 'review', 'name': 'Review', 'evidence_refs': ['doc:p1'], 'confidence': 1,
        'trigger_type': 'manual', 'trigger_config': {'ontology_contract': {
            'version': 1, 'entity_ids': [], 'input_bindings': [], 'output_node_id': 'end',
            'output_schema': {'type': 'object', 'properties': {'answer': {'type': 'string'},
                'count': {'type': 'integer'}}, 'required': ['answer', 'count'], 'additionalProperties': False}}},
        'nodes': [{'id': 'start', 'type': 'start'},
            {'id': 'summary', 'type': 'llm', 'data': {'prompt': 'Summarize {{params.text}}'}},
            {'id': 'end', 'type': 'end', 'data': {'output': {'answer': '{{summary.parsed.answer}}',
                                                          'count': '{{params.count}}'}}}],
        'edges': [{'source': 'start', 'target': 'summary'}, {'source': 'summary', 'target': 'end'}]}]
    if approval_data is not None:
        raw['workflows'][0]['nodes'].insert(2, {'id': 'review', 'type': 'approval', 'data': approval_data})
        raw['workflows'][0]['edges'] = [{'source': 'start', 'target': 'summary'},
            {'source': 'summary', 'target': 'review'}, {'source': 'review', 'target': 'end'}]
    return compiler.normalize_scenario_model(None, scenario, raw, source_bundle={
        'paragraphs': [{'ref': 'doc:p1', 'text': 'Summarize submitted text and return the answer and count.'}],
        'documents': [], 'fingerprint': 'a' * 64})


def test_compiled_workflow_executes_real_input_and_structured_output(monkeypatch):
    payload = _compiled_workflow()
    assert payload['unresolved'] == []
    workflow = SimpleNamespace(**payload['workflows'][0])
    prompts = []
    monkeypatch.setattr(workflow_service, '_resolve_llm', lambda *args: object())
    def chat(_model, messages, **kwargs):
        prompts.append(messages[-1]['content'])
        return {'content': '{"answer":"A concise summary"}'}
    monkeypatch.setattr(workflow_service.llm_service, 'chat', chat)
    results = workflow_service._execute_dag(None, workflow, {'text': 'Source text', 'count': 7},
        execution_id='synthetic-run', approved_node_ids=set(), resumed_results={}, attempt=1,
        source_run_id=None, runtime_definition=SimpleNamespace(entities={}, relations={}, is_frozen=False), deadline_at=None)
    assert prompts == ['Summarize Source text']
    assert [row['status'] for row in results] == ['success', 'success', 'success']
    assert results[-1]['result'] == {'answer': 'A concise summary', 'count': 7}


@pytest.mark.parametrize('node_type,field,value', [
    ('action', 'params', {'amount': '{{params.amount}}'}),
    ('rule', 'record', '{{params.record}}'),
    ('event', 'payload', {'result': [1, True, None]}),
    ('approval', 'instructions', 'Check the supporting evidence.'),
    ('end', 'summary', 'Result: {{params.result}}'),
])
def test_supported_node_values_roundtrip_without_mutation(node_type, field, value):
    original = {field: deepcopy(value), 'arbitrary_code': 'not an executor'}
    actual = authoring.execution_data(node_type, original)
    assert actual == {field: value}
    assert original[field] == value


def test_generated_approval_preserves_the_declared_audience_and_evidence_requirement():
    data = {'instructions': 'Review the request.', 'approver_roles': ['owner', 'admin'],
        'approver_user_ids': ['synthetic-reviewer'], 'requires_evidence': True}
    payload = _compiled_workflow(data)
    assert payload['unresolved'] == []
    approval = next(node for node in payload['workflows'][0]['nodes'] if node['type'] == 'approval')
    assert {key: approval['data'][key] for key in data} == data


@pytest.mark.parametrize('data', [
    {'approver_roles': ['superadmin']},
    {'approver_user_ids': ['x' * 33]},
    {'requires_evidence': 'true'},
])
def test_invalid_generated_approval_policy_is_rejected_instead_of_silently_removed(data):
    with pytest.raises(ValueError):
        authoring.execution_data('approval', data)
    payload = _compiled_workflow(data)
    assert any(issue['code'] == 'invalid_workflow_data' for issue in payload['unresolved'])


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {'x': object()}, 'x' * 33_000],
                         ids=['nan', 'infinity', 'non-json', 'oversized'])
def test_invalid_or_oversized_execution_data_is_rejected(value):
    with pytest.raises(ValueError):
        authoring.execution_data('end', {'output': value})


@pytest.mark.parametrize('template', ['{{input.text}}', '{{end.result}}', '{{summary.parsed}}', '{{params..x}}', '{{params.x + 1}}'])
def test_wrong_template_roots_self_future_and_expressions_fail_shared_validation(template):
    workflow = _compiled_workflow()['workflows'][0]
    workflow['nodes'][1]['data']['prompt'] = template
    with pytest.raises(PolicyViolation):
        workflow_service.validate_workflow_definition(workflow['nodes'], workflow['edges'])


def test_output_cannot_depend_on_a_node_missing_from_an_alternative_branch():
    nodes = [{'id': 'start', 'type': 'start'}, {'id': 'choose', 'type': 'rule'},
             {'id': 'left', 'type': 'llm', 'data': {'prompt': 'left'}},
             {'id': 'right', 'type': 'llm', 'data': {'prompt': 'right'}},
             {'id': 'end', 'type': 'end', 'data': {'output': '{{left.parsed}}'}}]
    edges = [{'source': 'start', 'target': 'choose'},
             {'source': 'choose', 'target': 'left', 'label': 'true'},
             {'source': 'choose', 'target': 'right', 'label': 'false'},
             {'source': 'left', 'target': 'end'}, {'source': 'right', 'target': 'end'}]
    with pytest.raises(PolicyViolation, match='每条路径'):
        workflow_service.validate_workflow_definition(nodes, edges)


def test_missing_node_identity_returns_a_validation_error():
    workflow = _compiled_workflow()['workflows'][0]
    workflow['nodes'].append({'type': 'end'})
    with pytest.raises(PolicyViolation, match='非空 ID'):
        workflow_service.validate_workflow_definition(workflow['nodes'], workflow['edges'])


def test_nonempty_sequential_labels_cannot_silently_stop_execution():
    workflow = _compiled_workflow()['workflows'][0]
    workflow['edges'][0]['label'] = 'continue'
    with pytest.raises(PolicyViolation, match='label 必须为空'):
        workflow_service.validate_workflow_definition(workflow['nodes'], workflow['edges'])


def test_compiled_workflow_pauses_for_approval_and_resumes_without_repeating_model(monkeypatch):
    workflow = _compiled_workflow()['workflows'][0]
    workflow['nodes'].insert(2, {'id': 'review', 'type': 'approval', 'data': {'instructions': 'Review supplied evidence.'}})
    workflow['edges'] = [{'source': 'start', 'target': 'summary'},
                         {'source': 'summary', 'target': 'review'},
                         {'source': 'review', 'target': 'end'}]
    workflow_service.validate_workflow_definition(workflow['nodes'], workflow['edges'])
    monkeypatch.setattr(workflow_service, '_resolve_llm', lambda *args: object())
    calls = []
    def chat(*args, **kwargs):
        calls.append(1)
        return {'content': '{"answer":"Supplied evidence only"}'}
    monkeypatch.setattr(workflow_service.llm_service, 'chat', chat)
    kwargs = dict(execution_id='synthetic-approval', approved_node_ids=set(), resumed_results={},
                  attempt=1, source_run_id=None,
                  runtime_definition=SimpleNamespace(entities={}, relations={}, is_frozen=False), deadline_at=None)
    paused = workflow_service._execute_dag(None, SimpleNamespace(**workflow), {'text': 'Evidence', 'count': 2}, **kwargs)
    assert [row['status'] for row in paused] == ['success', 'success', 'awaiting_approval']
    assert all(row['node'] != 'end' for row in paused)
    kwargs.update(approved_node_ids={'review'}, resumed_results={'summary': paused[1]}, attempt=2)
    resumed = workflow_service._execute_dag(None, SimpleNamespace(**workflow), {'text': 'Evidence', 'count': 2}, **kwargs)
    assert [row['status'] for row in resumed] == ['success', 'success', 'approved', 'success']
    assert resumed[-1]['result'] == {'answer': 'Supplied evidence only', 'count': 2}
    assert calls == [1]
