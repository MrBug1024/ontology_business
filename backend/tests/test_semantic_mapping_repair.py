from copy import deepcopy

import pytest

from app.services import scenario_model_quality_service as quality


@pytest.mark.parametrize('change', ['drop', 'rebind', 'required', 'append'])
def test_repair_preserves_validated_mapping_bindings(change):
    fields = [
        {'ontology_property_id': 'property_a', 'dataset_field_id': 'source_a', 'is_required': True},
        {'ontology_property_id': 'property_b', 'dataset_field_id': 'source_b', 'is_required': False},
    ]
    raw = {'semantic_mappings': [{'key': 'mapping', 'fields': fields}]}
    initial = {**deepcopy(raw), 'unresolved': [{'code': 'invalid_workflow'}]}
    revised = deepcopy(raw)
    changed = revised['semantic_mappings'][0]['fields']
    if change == 'drop':
        changed.pop()
    elif change == 'rebind':
        changed[0]['dataset_field_id'] = 'source_c'
    elif change == 'required':
        changed[0]['is_required'] = False
    else:
        changed.append({'ontology_property_id': 'property_c', 'dataset_field_id': 'source_c', 'is_required': False})
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: revised, normalize=lambda candidate: {**candidate, 'unresolved': []})
    if change == 'append':
        assert result['unresolved'] == []
        assert len(result['semantic_mappings'][0]['fields']) == 3
    else:
        assert result is initial
    assert raw['semantic_mappings'][0]['fields'] == fields


def test_invalid_mapping_fields_can_be_corrected_when_not_in_validated_result():
    raw = {'semantic_mappings': [{'key': 'mapping', 'fields': [
        {'ontology_property_id': 'missing', 'dataset_field_id': 'source_a'}]}]}
    initial = {'semantic_mappings': [], 'unresolved': [{'code': 'invalid_semantic_mapping'}]}
    revised = deepcopy(raw)
    revised['semantic_mappings'][0]['fields'][0]['ontology_property_id'] = 'property_a'
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: revised, normalize=lambda candidate: {**candidate, 'unresolved': []})
    assert result['unresolved'] == []
    assert result['semantic_mappings'][0]['fields'][0]['ontology_property_id'] == 'property_a'
