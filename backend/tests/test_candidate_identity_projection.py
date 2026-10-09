from app.services import scenario_model_compiler as compiler
from app.services import scenario_model_draft_service as drafts
from types import SimpleNamespace
import copy
import pytest


def test_review_candidate_keeps_resolved_identity_without_hiding_invalid_fields():
    source = {'key': 'subject', 'name': 'Subject', 'evidence_refs': ['doc:p1'],
              'properties': [{'name': 'Code', 'data_type': 'unsupported', 'constraints': {'invalid': True}}]}
    normalized = {'key': 'entity.subject', 'name': 'Subject', 'existing_id': 'formal-subject',
                  'operation': 'update', 'api_name': 'subject', 'properties': [
                      {'name': 'Code', 'api_name': 'code', '_operation': 'update', 'data_type': 'string'}]}
    issues = [{'code': 'invalid_property', 'message': 'Unsupported type', 'blocking': True,
               'source_refs': ['doc:p1']}]
    candidates = compiler._build_draft_candidates(raw={'entities': [source]},
        normalized_sections={'entities': [normalized]}, issues=issues, valid_sources={'doc:p1'})
    rows = drafts._normalized_candidates({'draft_candidates': candidates, 'entities': [normalized]})
    entity = next(row for row in rows if row['resource_kind'] == 'entity')
    assert entity['payload']['existing_id'] == 'formal-subject'
    assert entity['payload']['operation'] == 'update'
    assert entity['payload']['properties'][0]['data_type'] == 'unsupported'
    assert entity['payload']['properties'][0]['constraints'] == {'invalid': True}
    assert entity['validation_issues'][0]['blocking'] is True
    prop = next(row for row in rows if row['resource_kind'] == 'property')
    assert prop['payload']['entity_ref'] == 'formal-subject'
    assert prop['payload']['api_name'] == 'code'
    assert prop['payload']['_operation'] == 'update'
    assert 'existing_id' not in source


def test_review_relation_keeps_normalized_endpoints_and_invalid_cardinality():
    source = {'key': 'rel', 'name': 'Connects', 'source_ref': 'subject', 'target_ref': 'item',
              'relation_type': 'invalid', 'evidence_refs': ['doc:p1']}
    normalized = {'key': 'relation.rel', 'existing_id': 'formal-rel', 'operation': 'update',
                  'source': {'kind': 'existing', 'id': 'subject-id'},
                  'target': {'kind': 'generated', 'key': 'entity.item'}, 'relation_type': '1:N'}
    rows = compiler._build_draft_candidates(raw={'relations': [source]},
        normalized_sections={'relations': [normalized]}, issues=[], valid_sources={'doc:p1'})
    assert rows[0]['payload']['source'] == normalized['source']
    assert rows[0]['payload']['target'] == normalized['target']
    assert rows[0]['payload']['existing_id'] == 'formal-rel'
    assert rows[0]['payload']['relation_type'] == 'invalid'


def test_unresolved_candidate_does_not_receive_an_invented_identity():
    source = {'key': 'subject', 'name': 'Subject', 'properties': [], 'evidence_refs': ['doc:p1']}
    rows = compiler._build_draft_candidates(raw={'entities': [source]},
        normalized_sections={}, issues=[], valid_sources={'doc:p1'})
    assert 'existing_id' not in rows[0]['payload']
    assert any(issue['blocking'] for issue in rows[0]['validation_issues'])


def _legacy():
    payload = {'name': 'Code', 'entity_ref': 'entity.subject', 'data_type': 'unsupported'}
    row = SimpleNamespace(tenant_id='tenant', scenario_id='scenario', created_by_user_id='actor',
        compilation_job_id='job', proposal_id='proposal', resource_kind='property',
        resource_key='entity.subject:property:Code', materialization_source='compiler_sidecar',
        payload=payload, source_payload=copy.deepcopy(payload))
    job = SimpleNamespace(id='job', tenant_id='tenant', scenario_id='scenario', created_by_user_id='actor',
        status='succeeded', compiler_version='scenario_model.compiler.v33', result={
            'proposal_id':'proposal', 'payload': {'entities': [{'key':'entity.subject',
                'existing_id':'formal-subject', 'properties':[{'name':'Code','api_name':'code','_operation':'update'}]}]}})
    return row, job


def test_legacy_recovery_uses_exact_persisted_identity_and_retains_invalid_editing_value():
    from app.services.candidate_identity_recovery import recovered_payload
    row, job = _legacy()
    fixed = recovered_payload(row, job)
    assert fixed['entity_ref'] == 'formal-subject'
    assert fixed['api_name'] == 'code'
    assert fixed['data_type'] == 'unsupported'
    assert row.payload == row.source_payload


@pytest.mark.parametrize('field,value', [('tenant_id','foreign'),('scenario_id','other'),
    ('created_by_user_id','other'),('id','different'),('status','running'),
    ('compiler_version','scenario_model.compiler.v35')])
def test_legacy_recovery_cannot_use_another_execution_or_unbounded_version(field, value):
    from app.services.candidate_identity_recovery import recovered_payload
    row, job = _legacy()
    setattr(job, field, value)
    assert recovered_payload(row, job) is None


def test_legacy_recovery_preserves_user_edits_and_requires_same_proposal():
    from app.services.candidate_identity_recovery import recovered_payload
    row, job = _legacy()
    row.payload['data_type'] = 'string'
    assert recovered_payload(row, job) is None
    row, job = _legacy()
    job.result['proposal_id'] = 'other'
    assert recovered_payload(row, job) is None


def test_optional_null_constraints_pass_same_formal_relation_validator():
    from app.services.candidate_identity_projection import optional_relation_constraints
    from app.services.ontology_service import normalize_relation_constraints
    raw = {'symmetric': None, 'source_min_cardinality': 1, 'target_max_cardinality': None}
    projected = optional_relation_constraints(raw)
    assert normalize_relation_constraints(projected) == {'source_min_cardinality': 1}
    assert raw['symmetric'] is None
    for invalid in ({'unknown': None}, {'symmetric': 'false'}, {'source_min_cardinality': -1}):
        with pytest.raises(ValueError):
            normalize_relation_constraints(optional_relation_constraints(invalid))


def test_valid_relation_aliases_survive_candidate_formal_preflight():
    from app.services.candidate_identity_projection import project_identity
    from app.services.ontology_service import normalize_relation_constraints
    raw = {'relation_type': 'association', 'constraints': {
        'symmetric': None, 'source_max_cardinality': 'N', 'target_max_cardinality': '1'}}
    normalized = {'relation_type': 'N:M', 'constraints': {'target_max_cardinality': 1}}
    candidate = project_identity('relations', raw, normalized)
    assert candidate['relation_type'] == 'N:M'
    assert normalize_relation_constraints(candidate['constraints'],
        relation_type=candidate['relation_type']) == {'target_max_cardinality': 1}
    assert raw['constraints']['source_max_cardinality'] == 'N'


def test_relation_projection_preserves_invalid_constraints_for_repair():
    from app.services.candidate_identity_projection import project_identity
    raw = {'relation_type': '1:N', 'constraints': {'unknown': 'N'}}
    assert project_identity('relations', raw, {'constraints': {}})['constraints'] == raw['constraints']


def test_rule_retains_resolved_entity_and_preserves_invalid_condition():
    from app.services.candidate_identity_projection import project_identity
    raw = {'entity_ref': 'Display name', 'condition': {'wrong': True}}
    peer = {'entity': {'kind': 'existing', 'id': 'owned-id'}, 'condition': {}}
    result = project_identity('rules', raw, peer)
    assert result['entity'] == peer['entity']
    assert result['condition'] == raw['condition']


def test_workflow_retains_node_identity_without_replacing_execution_data():
    from app.services.candidate_identity_projection import project_identity
    raw = {'nodes': [{'id': 'check', 'type': 'rule', 'data': {'rule_id': 'Display name', 'record': 'invalid'}}]}
    peer = {'nodes': [{'id': 'check', 'type': 'rule', 'data': {'resource': {'kind': 'existing', 'id': 'owned-rule'}}}]}
    result = project_identity('workflows', raw, peer)
    assert result['nodes'][0]['data']['resource'] == peer['nodes'][0]['data']['resource']
    assert result['nodes'][0]['data']['record'] == 'invalid'
