"""Frozen semantics, visibility and input-plane boundaries share one projection."""
from copy import deepcopy
from types import SimpleNamespace as Resource

import pytest
from pydantic import ValidationError

from app.scenario_capability_blueprint_schemas import ScenarioCapabilityBlueprintOut
from app.services import plugin_authoring_context, plugin_coding_workspace
from app.services.scenario_capability_blueprint import blueprint


def definition_fixture():
    prop = Resource(id='property', api_name='request_id', name='Request identity', data_type='string',
        description='Stable business identity', is_key=True, is_required=True, is_title=True,
        is_enum=False, enum_values=[], constraints={'physical_table': 'must-not-export'})
    sensitive = Resource(**{**vars(prop), 'id': 'sensitive', 'name': 'must-not-export-sensitive-property'})
    entity = Resource(id='entity', api_name='request', name='Request', description='Current business request',
                      properties=[prop, sensitive])
    function = Resource(id='function', name='Compute', description='Compute request result', runtime_kind='threshold',
                        runtime_config={'threshold': 80, 'credential': 'must-not-export-runtime-config'})
    action = Resource(id='action', name='must-not-export-hidden-action', description='Hidden action', entity_id='entity',
                      enabled=False, executor_config={'url': 'must-not-export-executor'})
    rule = Resource(id='rule', name='Decide', description='Decide current request', enabled=False,
                    entity_id='entity', trigger_action_ids=['action'], condition={'customer': 'must-not-export-ast'})
    event = Resource(id='event', name='Requested', description='Business event', enabled=True,
                     payload_schema={'customer': 'must-not-export-payload'})
    workflow = Resource(id='workflow', name='Review', description='Review current request', enabled=True,
        trigger_type='event', steps=[], nodes=[{'id': 'start', 'type': 'start'},
            {'id': 'decision', 'type': 'rule', 'data': {'rule_id': 'rule', 'record': 'must-not-export-node-data'}},
            {'id': 'review', 'type': 'approval', 'data': {'instructions': 'must-not-export-instructions'}},
            {'id': 'end', 'type': 'end', 'data': {'output': 'must-not-export-output'}}],
        edges=[{'source': 'start', 'target': 'decision', 'label': 'must-not-export-edge'}],
        trigger_config={'event_id': 'event', 'ontology_contract': {'version': 1, 'entity_ids': ['entity'],
            'input_bindings': [{'path': 'request', 'entity_id': 'entity', 'many': False, 'partial': False}],
            'output_node_id': 'end', 'output_schema': {'type': 'object'}}})
    definition = Resource(source='release', release_id='release', snapshot_id='snapshot', definition_hash='a' * 64,
        entities={'entity': entity}, relations={'relation': Resource(id='relation', api_name='relates', name='Relates',
            description='Business relation', source_entity_id='entity', target_entity_id='entity', relation_type='1:N')},
        functions={'function': function}, actions={'action': action}, rules={'rule': rule},
        events={'event': event}, workflows={'workflow': workflow}, mappings={'customer': 'must-not-export-mapping'})
    available = [{'kind': kind, 'key': kind, 'readiness': {'ready': kind != 'rule'}}
                 for kind in ('function', 'rule', 'workflow')]
    kwargs = {'scenario': {'id': 'scenario', 'name': 'Released title', 'description': 'Released goal'},
        'deployment': {'release_id': 'release', 'snapshot_id': 'snapshot', 'definition_hash': 'a' * 64},
        'available': available, 'selected': [{'kind': 'workflow', 'key': 'workflow'}],
        'readable_property_keys': {'property'}, 'invocation_authorized': {('function', 'function')}}
    return definition, kwargs


def test_frozen_profile_distinguishes_selected_available_internal_dependencies_and_events():
    definition, kwargs = definition_fixture()
    actual = blueprint(definition, **kwargs)
    rows = {row['kind']: row for row in actual['capabilities']}
    assert actual['coverage']['selected'] == [{'kind': 'workflow', 'key': 'workflow'}]
    assert actual['coverage']['dependencies'] == [
        {'kind': 'event', 'key': 'event'}, {'kind': 'rule', 'key': 'rule'}]
    assert rows['workflow']['selected'] and not rows['function']['selected']
    assert rows['function']['available'] and rows['function']['invocation_authorized']
    assert rows['rule']['available'] and not rows['rule']['enabled'] and not rows['rule']['ready']
    assert not rows['workflow']['invocation_authorized']
    assert rows['event']['dependency'] and not rows['event']['available']
    assert not rows['event']['invocation_supported'] and not rows['event']['invocation_authorized']
    assert 'action' not in rows  # ACL-hidden metadata is never synthesized as a public entry.
    assert rows['rule']['semantic']['dependencies'] == []
    assert not any(ref['key'] == 'action' for row in actual['capabilities'] for ref in row['semantic']['dependencies'])
    assert rows['workflow']['semantic']['requires_approval']
    assert rows['workflow']['semantic']['output_node_keys'] == ['end']
    assert rows['workflow']['semantic']['input_bindings'] == [
        {'path': 'request', 'object_key': 'entity', 'many': False, 'partial': False}]


def test_projection_exports_only_business_semantics_not_runtime_config_algorithms_or_customer_material():
    definition, kwargs = definition_fixture()
    definition.distillation_documents = ['must-not-export-live-material']
    result = blueprint(definition, **kwargs)
    assert 'must-not-export' not in str(result)
    prop = result['ontology']['objects'][0]['properties'][0]
    assert prop['is_key'] and prop['is_required'] and prop['data_type'] == 'string'
    assert result['ontology']['relations'][0]['cardinality'] == '1:N'
    assert '未冻结' in result['stages'][1]['boundary']
    assert '正式调用输入' in result['stages'][0]['boundary']


def test_zero_ontology_is_a_complete_supported_projection_and_not_a_readiness_gap():
    definition, kwargs = definition_fixture()
    definition.entities, definition.relations = {}, {}
    kwargs['selected'] = [{'kind': 'function', 'key': 'function'}]
    kwargs['available'] = kwargs['available'][:1]
    definition.events = {}
    result = blueprint(definition, **kwargs)
    assert result['ontology'] == {'objects': [], 'relations': []}
    assert result['coverage']['dependencies'] == []
    assert result['capabilities'][0]['ready']


@pytest.mark.parametrize('field', ['source', 'release_id', 'snapshot_id', 'definition_hash'])
def test_projection_requires_the_exact_frozen_definition_identity(field):
    definition, kwargs = definition_fixture()
    setattr(definition, field, 'changed')
    with pytest.raises(ValueError, match='发布'):
        blueprint(definition, **kwargs)


def test_full_semantic_projection_over_budget_fails_instead_of_truncating_and_claiming_complete():
    definition, kwargs = definition_fixture()
    definition.entities['entity'].description = 'x' * (129 * 1024)
    with pytest.raises(ValueError, match='预算'):
        blueprint(definition, **kwargs)


def test_missing_frozen_dependency_rejects_instead_of_hiding_an_incomplete_closure():
    definition, kwargs = definition_fixture()
    definition.actions = {}
    with pytest.raises(ValueError, match='依赖缺失'):
        blueprint(definition, **kwargs)


def test_blueprint_dto_rejects_unknown_fields_and_invalid_definition_hash():
    definition, kwargs = definition_fixture()
    result = blueprint(definition, **kwargs)
    result['ontology']['objects'][0]['connection_url'] = 'synthetic'
    with pytest.raises(ValidationError):
        ScenarioCapabilityBlueprintOut.model_validate(result)
    result['ontology']['objects'][0].pop('connection_url')
    result['deployment']['definition_hash'] = 'z' * 64
    with pytest.raises(ValidationError):
        ScenarioCapabilityBlueprintOut.model_validate(result)


def test_discovery_has_no_plugin_selection_and_exposes_the_same_server_contract(monkeypatch):
    sentinel = {'scenario': {}, 'deployment': {}, 'capabilities': [], 'scenario_blueprint': {'coverage': {'selected': []}},
                'delivery_profile': {'version': 'scenario-plugin-delivery-profile.v1'}}
    monkeypatch.setattr(plugin_authoring_context, 'release_context', lambda *args: (None, {}))
    def contract(db, manifest, *, blueprint_selection):
        assert blueprint_selection == []
        return deepcopy(sentinel)
    monkeypatch.setattr(plugin_authoring_context, 'authoring_contract', contract)
    assert plugin_authoring_context.discover_context(None, 'release') == sentinel


def test_legacy_workspace_reports_missing_profile_without_regenerating_its_contract_or_source(monkeypatch):
    document = {'release_id': 'release', 'revision': 3, 'phase': 'released', 'plugin_version': '1.0.0',
        'files': {'README.md': 'Existing source'}, 'events': [], 'validation': [], 'coding_contract': {'capabilities': []},
        'llm_config_id': 'model'}
    original = deepcopy(document)
    monkeypatch.setattr(plugin_coding_workspace, 'assert_adapter_identity', lambda value: None)
    monkeypatch.setattr(plugin_coding_workspace, 'project_files', lambda value: [])
    monkeypatch.setattr(plugin_coding_workspace, 'coding_turns', lambda *args: [])
    monkeypatch.setattr(plugin_coding_workspace, 'latest_session_thread', lambda db, project_id: None)
    actual = plugin_coding_workspace.public_workspace(None, Resource(proposal=document, thread_id='workspace'))
    assert actual['scenario_blueprint'] is None and actual['delivery_profile'] is None
    assert document == original
