from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler
from app.services import workflow_service
from app.services.policies import PolicyViolation


def _fixture():
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[],
        data_mappings=[], relation_data_mappings=[])
    raw = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    raw['schema_version'] = compiler.SCHEMA_VERSION
    contract = {'version': 1, 'entity_ids': [], 'input_bindings': [], 'output_node_id': 'end',
                'output_schema': {'type': 'object', 'properties': {'answer': {'type': 'integer'}},
                                  'required': ['answer'], 'additionalProperties': False}}
    raw['workflows'] = [{'key': 'summary', 'name': 'Summary', 'evidence_refs': ['doc:p1'],
        'confidence': 1, 'trigger_type': 'manual', 'trigger_config': {'ontology_contract': contract},
        'nodes': [{'id': 'start', 'type': 'start'},
                  {'id': 'summarize', 'type': 'llm', 'data': {'prompt': 'Summarize {{params.text}}'}},
                  {'id': 'end', 'type': 'end', 'data': {'output': '{{summarize.parsed}}'}}],
        'edges': [{'source': 'start', 'target': 'summarize'}, {'source': 'summarize', 'target': 'end'}]}]
    sources = {'paragraphs': [{'ref': 'doc:p1', 'text': 'Summarize submitted data.'}],
               'documents': [], 'fingerprint': 'a' * 64}
    return scenario, raw, sources


def test_compiled_output_contract_roundtrips_and_rejects_wrong_runtime_type(monkeypatch):
    scenario, raw, sources = _fixture()
    expected = deepcopy(raw['workflows'][0]['trigger_config']['ontology_contract'])
    result = compiler.normalize_scenario_model(None, scenario, raw, source_bundle=sources)
    assert result['unresolved'] == []
    workflow = SimpleNamespace(**result['workflows'][0])
    assert workflow.trigger_config['ontology_contract'] == expected
    monkeypatch.setattr(workflow_service, '_resolve_llm', lambda *args: object())
    monkeypatch.setattr(workflow_service.llm_service, 'chat', lambda *args, **kwargs: {'content': '{"answer":"wrong type"}'})
    steps = workflow_service._execute_dag(None, workflow, {'text': 'Synthetic text'},
        execution_id='synthetic', approved_node_ids=set(), resumed_results={}, attempt=1,
        source_run_id=None, runtime_definition=SimpleNamespace(entities={}, relations={}, is_frozen=False),
        deadline_at=None)
    assert steps[-1]['status'] == 'failed'
    assert '不符合已声明契约' in steps[-1]['error']


@pytest.mark.parametrize('mutation', ['missing', 'unknown_entity', 'unknown_output', 'extra_field'])
def test_invalid_generated_workflow_contract_is_blocked(mutation):
    scenario, raw, sources = _fixture()
    cfg = raw['workflows'][0]['trigger_config']
    if mutation == 'missing':
        cfg.clear()
    elif mutation == 'unknown_entity':
        cfg['ontology_contract']['entity_ids'] = ['other-scenario-object']
    elif mutation == 'unknown_output':
        cfg['ontology_contract']['output_node_id'] = 'absent'
    else:
        cfg['ontology_contract']['external_loader'] = 'unsupported'
    result = compiler.normalize_scenario_model(None, scenario, raw, source_bundle=sources)
    assert any(issue['blocking'] and issue['code'] == 'invalid_workflow_contract' for issue in result['unresolved'])


def test_property_access_outside_template_is_not_structured_output():
    _, raw, _ = _fixture()
    workflow = raw['workflows'][0]
    workflow['nodes'][-1]['data']['output'] = {'answer': '{{summarize.parsed}}.answer'}
    with pytest.raises(PolicyViolation, match='属性路径'):
        workflow_service.validate_workflow_definition(workflow['nodes'], workflow['edges'])
