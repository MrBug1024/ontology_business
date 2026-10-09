from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import plugin_coding_contract as coding


def contract_fixture(monkeypatch, *, description='Provide a decision for the current request'):
    live = SimpleNamespace(id='scenario', name='Mutable scenario title', description='Changed live goal')
    frozen = SimpleNamespace(name='Released scenario title', description=description,
                             modeling_documents=['must-not-copy-customer-document'])
    # The runtime graph retains live scenario metadata; only the snapshot text
    # is the semantic authority for this plugin's pinned goal.
    definition = SimpleNamespace(scenario=live, source='release', release_id='release', snapshot_id='snapshot',
        definition_hash='a' * 64, entities={}, relations={}, actions={}, rules={}, events={}, workflows={},
        functions={'check': SimpleNamespace(id='check', name='Check current request', description='Check inputs',
                                           runtime_kind='threshold')})
    deployment = SimpleNamespace(definition=definition, definition_hash='a' * 64,
                                 release_id='release', snapshot_id='snapshot')
    manifest = {'scenario': {'id': 'scenario', 'name': live.name},
                'deployment': {'release_id': 'release', 'snapshot_id': 'snapshot', 'definition_hash': 'a' * 64},
                'capabilities': [{'kind': 'function', 'key': 'check'}]}
    capabilities = [{'kind': 'function', 'key': 'check', 'name': 'Check current request', 'description': 'Check inputs',
                     'definition_hash': 'a' * 64, 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'boolean'},
                     'side_effect': False, 'requires_confirmation': False, 'idempotency_required': False, 'data_ports': []}]
    monkeypatch.setattr(coding.release_service, '_scenario_for_manage', lambda *args: (live, None))
    monkeypatch.setattr(coding.capability_application_service, 'resolve_deployment',
                        lambda db, scenario, *, release_id: (deployment, None))
    def snapshot_for_scenario(db, scenario, snapshot_id):
        assert scenario is live and snapshot_id == 'snapshot'
        return SimpleNamespace(content={'scenario': vars(frozen)})
    monkeypatch.setattr(coding.release_service, '_snapshot_for_scenario', snapshot_for_scenario)
    def list_capabilities(db, scenario, *, release_id, definition=None):
        assert scenario is live
        assert release_id == 'release'
        assert definition is None or definition is deployment.definition
        return capabilities
    monkeypatch.setattr(coding.capability_application_service, 'list_capabilities', list_capabilities)
    monkeypatch.setattr(coding.capability_application_service, '_permission_allowed', lambda *args: True)
    return manifest, live, deployment


def test_coding_goal_comes_from_exact_release_and_ignores_later_live_scenario_edits(monkeypatch):
    manifest, live, deployment = contract_fixture(monkeypatch)
    original = deepcopy(manifest)
    first = coding.authoring_contract(None, manifest)
    live.description = 'A different draft goal must not change this plugin'
    live.name = 'A different draft title'
    second = coding.authoring_contract(None, manifest)
    assert first['scenario'] == second['scenario'] == {
        'id': 'scenario', 'name': 'Released scenario title', 'description': 'Provide a decision for the current request'}
    assert 'modeling_documents' not in str(first)
    assert 'must-not-copy-customer-document' not in str(first)
    assert manifest == original


def test_secret_like_release_description_is_sanitized_before_coding_context(monkeypatch):
    manifest, _, _ = contract_fixture(monkeypatch, description='Bearer synthetic_nonreusable_secret_material')
    value = coding.authoring_contract(None, manifest)
    assert 'synthetic_nonreusable_secret_material' not in str(value)
    assert value['scenario']['description'] == ''


def test_authoring_profile_uses_actual_execute_acl_and_property_read_visibility(monkeypatch):
    manifest, _, deployment = contract_fixture(monkeypatch)
    prop = SimpleNamespace(id='private-property', name='Hidden property', api_name='private_value',
        data_type='string', description='Must remain hidden', is_key=False, is_required=False,
        is_title=False, is_enum=False, enum_values=[])
    deployment.definition.entities = {'object': SimpleNamespace(id='object', api_name='object',
        name='Business object', description='Object meaning', properties=[prop])}
    checks = []
    monkeypatch.setattr(coding.permission_service, 'can_read_property', lambda db, item: False)
    def permission(db, definition, kind, resource, verb):
        checks.append((kind, resource.id, verb))
        return False
    monkeypatch.setattr(coding.capability_application_service, '_permission_allowed', permission)
    result = coding.authoring_contract(None, manifest)
    assert checks == [('function', 'check', 'execute')]
    assert result['scenario_blueprint']['ontology']['objects'][0]['properties'] == []
    assert not result['scenario_blueprint']['capabilities'][0]['invocation_authorized']
    assert result['scenario_blueprint']['capabilities'][0]['available']
    assert 'Must remain hidden' not in str(result)


def test_release_goal_remains_part_of_the_existing_contract_byte_budget(monkeypatch):
    manifest, _, _ = contract_fixture(monkeypatch, description='x' * (128 * 1024))
    with pytest.raises(ValueError, match='预算'):
        coding.authoring_contract(None, manifest)


@pytest.mark.parametrize('field', ['release_id', 'snapshot_id', 'definition_hash'])
def test_coding_context_rejects_a_resolved_goal_from_a_different_release_identity(monkeypatch, field):
    manifest, _, deployment = contract_fixture(monkeypatch)
    setattr(deployment, field, 'different-identity')
    with pytest.raises(ValueError, match='身份'):
        coding.authoring_contract(None, manifest)
