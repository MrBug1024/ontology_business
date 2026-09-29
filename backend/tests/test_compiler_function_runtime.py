from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler
from app.providers.builtin_function_evaluator import evaluate_function


def _compile(runtime_kind, runtime_config):
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    raw = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    raw['schema_version'] = compiler.SCHEMA_VERSION
    raw['functions'] = [{'key':'check', 'name':'Threshold check', 'evidence_refs':['doc:p1'], 'confidence':1,
        'runtime_kind':runtime_kind, 'runtime_config':runtime_config,
        'input_schema':{'type':'object','properties':{'value':{'type':'number'}},'required':['value'],'additionalProperties':False},
        'output_schema':{'type':'object','properties':{'matched':{'type':'boolean'},'value':{'type':'number'},'threshold':{'type':'number'}},
                         'required':['matched','value','threshold'],'additionalProperties':False}}]
    raw['coverage'] = [{'source_ref':'doc:p1','status':'modeled','reason':'Threshold contract','change_keys':['check']}]
    return compiler.normalize_scenario_model(None,scenario,raw,source_bundle={
        'paragraphs':[{'ref':'doc:p1','text':'Compare value greater than 10 and return its result and threshold.'}],
        'documents':[], 'fingerprint':'a'*64})


def test_compiler_retains_validated_runtime_instead_of_replacing_it_with_contract():
    compiled = _compile('threshold',{'field':'value','threshold':10,'operator':'>'})
    assert compiled['unresolved'] == []
    function = compiled['functions'][0]
    assert function['runtime_kind'] == 'threshold'
    assert evaluate_function(SimpleNamespace(**function),{'value':11}) == {'matched':True,'value':11,'threshold':10}
    assert evaluate_function(SimpleNamespace(**function),{'value':10})['matched'] is False


@pytest.mark.parametrize('kind,config', [('script',{}), ('provider',{
    'provider_key':'untrusted.provider','provider_version':'1.0.0','provider_config':{}})])
def test_compiler_rejects_untrusted_or_unknown_runtime(kind,config):
    compiled = _compile(kind,config)
    assert compiled['functions'] == []
    assert any(issue['code']=='invalid_function' and issue['blocking'] for issue in compiled['unresolved'])


def test_missing_attachment_capability_error_explains_how_to_recover():
    from app.services.agent_runtime_adapter import require_complete_runtime_context, AgentRuntimeAdapterError
    with pytest.raises(AgentRuntimeAdapterError, match='无需重新上传'):
        require_complete_runtime_context(SimpleNamespace(complete=False,context_issues=[{'code':'attachments_not_supported'}]))


def test_authoring_context_exposes_active_semantic_identities_and_respects_field_acl(monkeypatch):
    from app.services import function_authoring_context as context
    props = [SimpleNamespace(id=key, name=key, api_name=key, data_type='string')
             for key in ['visible', 'restricted', 'unmapped']]
    entity = SimpleNamespace(id='entity', name='Entity', api_name='entity', properties=props)
    mapping = SimpleNamespace(id='mapping', mapping_key='records', entity_id=entity.id, status='active',
        field_mappings=[SimpleNamespace(ontology_property_id=key) for key in ['visible', 'restricted']])
    calls = []
    def authorized(db, scenario_id):
        calls.append(scenario_id)
        return [mapping, SimpleNamespace(status='draft')]
    monkeypatch.setattr(context.catalog_service, 'list_semantic_mappings', authorized)
    monkeypatch.setattr(context.permission_service, 'can_read_property', lambda db, prop: prop.id != 'restricted')
    rows = context.available_mapping_context(object(), SimpleNamespace(id='scenario', entities=[entity]))
    assert calls == ['scenario']
    assert rows == [{'id': 'mapping', 'mapping_key': 'records', 'entity_id': 'entity',
        'entity_name': 'Entity', 'entity_api_name': 'entity', 'properties': [
            {'id': 'visible', 'name': 'visible', 'api_name': 'visible', 'data_type': 'string'}]}]


def test_authoring_context_does_not_swallow_scenario_access_denial(monkeypatch):
    from app.services import function_authoring_context as context
    from fastapi import HTTPException
    def denied(*args):
        raise HTTPException(404, 'Not found')
    monkeypatch.setattr(context.catalog_service, 'list_semantic_mappings', denied)
    with pytest.raises(HTTPException) as error:
        context.available_mapping_context(object(), SimpleNamespace(id='foreign'))
    assert error.value.status_code == 404
