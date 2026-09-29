from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler


def fixture():
    scenario = SimpleNamespace(id='scene', entities=[], relations=[], function_definitions=[],
        actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    raw = {section: [] for section in compiler._MODEL_OUTPUT_RESOURCE_SECTIONS}
    raw.update(schema_version=compiler.SCHEMA_VERSION, unresolved=[], coverage=[])
    item = {'key': 'semantic_mapping.subjects', 'mapping_key': 'subjects', 'entity_id': 'entity',
        'dataset_schema_id': 'schema', 'dataset_relation_id': 'relation',
        'fields': [{'ontology_property_id': 'property', 'dataset_field_id': 'field', 'is_required': True}],
        'evidence_refs': ['doc:p1'], 'confidence': 1}
    raw['semantic_mappings'] = [item]
    source = {'documents': [], 'paragraphs': [{'ref': 'doc:p1', 'text': 'Map the subject identifier.'}]}
    return scenario, raw, source


def test_compiler_preserves_catalog_mapping_as_governed_candidate():
    scenario, raw, source = fixture()
    # Deterministic compilation keeps references; live Catalog authority is rechecked at preflight.
    result = compiler.normalize_scenario_model(None, scenario, raw, source_bundle=source)
    assert not result['unresolved']
    assert result['semantic_mappings'][0]['fields'][0]['ontology_property_id'] == 'property'
    candidate = next(item for item in result['draft_candidates'] if item['resource_kind'] == 'semantic_mapping')
    assert candidate['task_id'] == 'mapping'
    assert 'semantic_mappings' in compiler.model_task_sections('mapping')


def test_catalog_mapping_rejects_physical_location_and_empty_fields():
    scenario, raw, source = fixture()
    for patch in ({'fields': []}, {'sql': 'select 1'}, {'status': 'active'}, {'dataset_version_id': 'runtime'}):
        invalid = deepcopy(raw)
        invalid['semantic_mappings'][0].update(patch)
        result = compiler.normalize_scenario_model(None, scenario, invalid, source_bundle=source)
        assert any(issue['code'] == 'invalid_semantic_mapping' for issue in result['unresolved'])
        assert not result['semantic_mappings']


def test_mapping_validation_does_not_echo_untrusted_extra_values():
    from app.services.semantic_mapping_authoring import normalize
    _, raw, _ = fixture()
    raw['semantic_mappings'][0]['sql'] = 'sensitive-extra-value'
    with pytest.raises(ValueError) as error:
        normalize(raw['semantic_mappings'][0])
    assert 'sensitive-extra-value' not in str(error.value)
    assert 'sql' in str(error.value)


def test_authoring_catalog_supplies_real_property_identity_for_mapping():
    from app.models import BusinessScenario, OntologyEntity, OntologyProperty
    prop = OntologyProperty(id='property-id', api_name='subject_key', name='Identifier', data_type='string')
    scene = BusinessScenario(id='scene', name='Synthetic', entities=[
        OntologyEntity(id='entity-id', name='Subject', properties=[prop])])
    actual = compiler._existing_catalog(scene)['entities'][0]['properties'][0]
    assert actual['id'] == 'property-id'
    assert actual['api_name'] == 'subject_key'


@pytest.mark.parametrize('invalid_count', [1, 2])
def test_mapping_feedback_keeps_exact_invalid_candidate_keys_with_shared_evidence(invalid_count):
    scenario, raw, source = fixture()
    for index in range(invalid_count):
        invalid = deepcopy(raw['semantic_mappings'][0])
        invalid.update(key=f'semantic_mapping.invalid_{index}', fields=[])
        raw['semantic_mappings'].append(invalid)
    result = compiler.normalize_scenario_model(None, scenario, raw, source_bundle=source)
    issues = [item for item in result['unresolved'] if item['code'] == 'invalid_semantic_mapping']
    assert len(issues) == invalid_count
    assert {tuple(item['affected_change_keys']) for item in issues} == {(f'semantic_mapping.invalid_{i}',) for i in range(invalid_count)}
    valid = next(item for item in result['draft_candidates'] if item['resource_key'] == 'semantic_mapping.subjects')
    assert valid['validation_issues'] == []
    for item in result['draft_candidates']:
        if item['resource_key'].startswith('semantic_mapping.invalid_'):
            assert item['validation_issues'][0]['affected_change_keys'] == [item['resource_key']]


def test_model_reported_mapping_issue_cannot_claim_other_candidates_are_safe():
    payload = {'coverage': []}
    resources = {'a': {'evidence_refs': ['doc:p1']}, 'b': {'evidence_refs': ['doc:p1']}}
    issue = {'code': 'document_reported_issue', 'reported_code': 'invalid_semantic_mapping',
        'affected_change_keys': ['a'], 'source_refs': ['doc:p1']}
    assert compiler._issue_affected_resource_keys(payload, issue, resources) == {'a', 'b'}
