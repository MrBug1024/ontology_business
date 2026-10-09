import json
from types import SimpleNamespace

import pytest

from app.services import scenario_model_compiler as compiler
from app.services.construction_delivery_service import build_delivery


def proposal(relation_type='N:M', constraints=None, reverse=False):
    entities = [{'key': key, 'name': key, 'evidence_refs': ['doc:p1'],
        'source_entity_bindings': [{'source_ref': 'doc:p1', 'source_key': key, 'attribute_map': {}}],
        'properties': [{'name': 'id', 'data_type': 'string', 'is_key': True, 'is_title': True}]}
        for key in ('parent', 'child')]
    raw = {section: [] for section in compiler._MODEL_OUTPUT_RESOURCE_SECTIONS}
    raw.update(schema_version=compiler.SCHEMA_VERSION, entities=entities,
        relations=[{'key': 'link', 'name': 'Link', 'source_ref': 'child' if reverse else 'parent',
            'target_ref': 'parent' if reverse else 'child', 'relation_type': relation_type,
            'constraints': constraints or {}, 'evidence_refs': ['doc:p1']}],
        coverage=[{'source_ref': 'doc:p1', 'status': 'modeled', 'reason': 'All definitions',
            'change_keys': ['parent', 'child', 'link']}], unresolved=[])
    source = {'paragraphs': [{'ref': 'doc:p1', 'source_id': 'doc', 'structured_handoff': True,
        'text': json.dumps({'source_path': ['entities'], 'value': [
            {'key': key, 'name': key, 'attributes': [], 'property_contracts': []}
            for key in ('parent', 'child')]})},
        {'ref': 'doc:p2', 'source_id': 'doc', 'structured_handoff': True,
         'text': json.dumps({'source_path': ['relations'], 'value': [
             {'source': 'parent', 'target': 'child', 'cardinality': 'one_to_many'}]})}],
        'documents': [], 'fingerprint': 'a' * 64}
    raw['coverage'].append({'source_ref': 'doc:p2', 'status': 'modeled', 'reason': 'Relation', 'change_keys': ['link']})
    raw['relations'][0]['evidence_refs'].append('doc:p2')
    return raw, source


def normalize(raw, source):
    scenario = SimpleNamespace(id='test', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[],
        data_mappings=[], relation_data_mappings=[])
    return compiler.normalize_scenario_model(None, scenario, raw, source_bundle=source)


def test_source_relation_rejects_reversed_single_value_limit():
    raw, source = proposal(constraints={'source_max_cardinality': 1})
    actual = normalize(raw, source)
    assert any(issue['code'] == 'source_relation_contract_mismatch' for issue in actual['unresolved'])
    relation = next(c for c in actual['draft_candidates'] if c['resource_kind'] == 'relation')
    assert not relation['promotion_eligible']


@pytest.mark.parametrize('relation_type,constraints,reverse', [
    ('1:N', {}, False), ('N:M', {'target_max_cardinality': 1}, False), ('N:1', {}, True)])
def test_source_relation_accepts_equivalent_limits(relation_type, constraints, reverse):
    raw, source = proposal(relation_type, constraints, reverse)
    actual = normalize(raw, source)
    assert not any(i['code'].startswith('source_relation') for i in actual['unresolved'])
    relation = actual['relations'][0]
    assert relation['_construction_relation_requirements']


def test_delivery_uses_final_governance_instead_of_compiler_optimism():
    result = build_delivery({'draft_candidates': [{'resource_key': 'rel', 'resource_kind': 'relation',
        'promotion_eligible': True, 'payload': {}}], 'candidate_governance': {
        'candidate_results': [{'resource_key': 'rel', 'promotion_eligible': False,
            'validation_issues': [{'code': 'formal_preflight_failed', 'blocking': True}]}]}})
    assert result['validated_definition_count'] == 0
    assert result['blocked_definition_count'] == 1
    assert not result['definition_complete']


def test_final_governance_also_blocks_decision_without_asking_business_again():
    from app.services.construction_delivery_service import apply_governance
    from app.services.assistant_decision_gate import attach_decision_gate
    original = {'relations': [{'key': 'rel', 'evidence_refs': ['doc:p1']}],
        'draft_candidates': [{'resource_key': 'rel', 'resource_kind': 'relation',
            'promotion_eligible': True, 'payload': {}}], 'unresolved': []}
    payload = attach_decision_gate(apply_governance(original, {'candidate_results': [
        {'resource_key': 'rel', 'promotion_eligible': False, 'validation_issues': [
            {'code': 'formal_preflight_failed', 'blocking': True, 'message': 'Invalid constraint'}]}]}))
    assert not payload['decision_gate']['safe_to_formalize']
    assert payload['decision_gate']['questions'] == []
    assert not payload['construction_delivery']['definition_complete']
    assert original['draft_candidates'][0]['promotion_eligible']


def test_candidate_edit_cannot_erase_or_reverse_saved_relation_requirement():
    from app.services.construction_relation_requirements import relation_requirement_issues
    raw, source = proposal('1:N')
    normalized = normalize(raw, source)
    relation = normalized['relations'][0]
    requirement = relation.pop('_construction_relation_requirements')
    relation['relation_type'] = 'N:1'
    assert relation_requirement_issues(requirement, relation)[0]['blocking']
