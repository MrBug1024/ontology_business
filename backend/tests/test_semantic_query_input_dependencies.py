from copy import deepcopy
from types import SimpleNamespace as Item

import pytest

from app.providers import semantic_dataset_query as provider
from app.services import input_contract_validator as inputs
from app.services import semantic_mapping_contract_service as contracts


def authoring_mapping():
    fields = [Item(id='unused', ordinal=0, source_name='unmapped', field_key='f0', logical_type='integer'),
              Item(id='code', ordinal=1, source_name='reference', field_key='f1', logical_type='string'),
              Item(id='amount', ordinal=2, source_name='amount', field_key='f2', logical_type='number')]
    return Item(id='mapping', entity_id='entry', mapping_key='entries',
                dataset_relation=Item(fields=fields), identifier_strategy={}, filter_expression={},
                field_mappings=[Item(id=name, ordinal=index, dataset_field_id=name,
                    ontology_property_id=name, direction='input', is_required=True, transform={})
                    for index, name in enumerate(('code', 'amount'))])


def test_authored_mapping_accepts_only_mapped_columns_and_preserves_field_indexes():
    mapping = authoring_mapping()
    result = contracts.contract_from_mapping(mapping)
    observed = {'category': 'table', 'tables': [{'columns': [{'name': 'reference', 'logical_type': 'string'},
                                      {'name': 'amount', 'logical_type': 'number'}], 'record_count': 3}]}
    assert inputs.validate_profile(result['schema_document'], observed).relation_matches == (0,)
    assert result['contract_version'] == 2
    assert [entry['contract_field_index'] for entry in result['fields']] == [0, 1]
    assert contracts._relation_field_index(mapping, 'amount') == 1
    with pytest.raises(contracts.SemanticMappingContractError):
        contracts._relation_field_index(mapping, 'unused')
    observed['tables'][0]['columns'][1]['logical_type'] = 'string'
    with pytest.raises(inputs.InputContractError):
        inputs.validate_profile(result['schema_document'], observed)
    observed['tables'][0]['columns'].pop()
    with pytest.raises(inputs.InputContractError):
        inputs.validate_profile(result['schema_document'], observed)


def test_frozen_v1_mapping_is_not_rewritten_to_new_authoring_semantics():
    mapping = authoring_mapping()
    old = contracts.contract_from_mapping(mapping)
    old['contract_version'] = 1
    old['schema_document'] = {inputs.CONTENT_CONTRACT_KEY:
        inputs.build_tabular_content_contract([mapping.dataset_relation])}
    for item in old['fields']:
        item['contract_field_index'] += 1
    before = deepcopy(old)
    assert contracts.normalize_contract(old) == before
    assert old == before
    with pytest.raises(inputs.InputContractError):
        inputs.validate_profile(old['schema_document'], {'category': 'table', 'tables': [{'columns': [
            {'name': 'reference', 'logical_type': 'string'},
            {'name': 'amount', 'logical_type': 'number'}], 'record_count': 3}]})


def test_invalid_mapping_field_is_rejected_instead_of_dropped():
    mapping = authoring_mapping()
    mapping.field_mappings[0].dataset_field_id = 'outside-relation'
    with pytest.raises(contracts.SemanticMappingContractError):
        contracts.contract_from_mapping(mapping)


def catalog():
    return [{'entity_id': name, 'entity_name': name.title(),
             'properties': [{'property_name': 'reference'}, {'property_name': 'amount'}]}
            for name in ('entry', 'account')]


@pytest.mark.parametrize('query,expected', [
    ({'base_entity': 'Entry', 'base_properties': ['reference']}, {'entry'}),
    ({'base_entity': {'entity_id': 'entry'}, 'aggregations': [
        {'entity_id': 'entry', 'function': 'count', 'alias': 'count'}]}, {'entry'}),
    ({'base_entity': 'Entry', 'related_entities': [
        {'entity_name': 'Account', 'properties': ['amount'],
         'join': {'base_property': 'reference', 'related_property': 'reference'}}]}, {'entry', 'account'}),
])
def test_query_dependencies_include_every_referenced_entity(query, expected):
    assert provider._require_query_property_access(query, catalog(), allow_input_references=False) == expected


@pytest.mark.parametrize('query', [
    {'base_entity': 'Unavailable'},
    {'base_entity': 'Entry', 'base_properties': ['secret']},
    {'base_entity': 'Entry', 'related_entities': [{'entity_name': 'Unavailable'}]},
])
def test_query_dependency_selection_keeps_entity_and_property_access_checks(query):
    with pytest.raises(provider.SemanticDatasetQueryProviderError):
        provider._require_query_property_access(query, catalog(), allow_input_references=False)


def test_real_provider_invocation_only_resolves_referenced_mapping(monkeypatch):
    definition = Item(semantic_mapping_contracts={
        'mapping-entry': {'entity_id': 'entry'}, 'mapping-account': {'entity_id': 'account'}}, scenario=Item())
    service = provider.SemanticDatasetQueryProvider(_db=Item())
    monkeypatch.setattr(provider, 'require_actor_session', lambda *args: None)
    monkeypatch.setattr(provider.capability_readiness_service, 'require_executable', lambda *args, **kwargs: None)
    monkeypatch.setattr(provider.permission_service, 'require_scenario_permission', lambda *args, **kwargs: None)
    monkeypatch.setattr(provider.SemanticDatasetQueryProvider, '_resource', lambda *args:
        (definition, Item(), provider._QueryBinding(('mapping-entry', 'mapping-account'))))
    monkeypatch.setattr(provider.SemanticDatasetQueryProvider, '_semantic_catalog', lambda *args, **kwargs: catalog())
    monkeypatch.setattr(provider.SemanticDatasetQueryProvider, '_dataset_version', lambda *args:
        (Item(id='runtime', dataset_id='dataset'), Item()))
    selected = []
    def resolve(self, **kwargs):
        selected.extend(kwargs['mapping_ids'])
        return definition, ()
    monkeypatch.setattr(provider.SemanticDatasetQueryProvider, '_semantic_mappings', resolve)
    output = {'records': [{'count': 3}], 'columns': ['count'], 'row_count': 1,
              'truncated': False, 'offset': 0, 'next_offset': None}
    monkeypatch.setattr(provider.business_query_service, 'query_business_data', lambda *args, **kwargs: output)
    actual = service.invoke(Item(capability=Item(), inputs={'base_entity': 'Entry',
        'aggregations': [{'entity_id': 'entry', 'function': 'count', 'alias': 'count'}]}), Item(),
        Item(tenant_id='tenant', scenario_id='scenario'), Item())
    assert selected == ['mapping-entry']
    assert actual == output
