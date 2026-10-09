from copy import deepcopy
from types import SimpleNamespace

from app.distillation_schemas import DistillationDocument
from app.services.distillation_proposal_service import normalize_proposal
from app.services import scenario_model_quality_service as quality
from app.services import scenario_model_compiler as compiler


def entity_batch(*, complete, shared_source):
    raw = {'schema_version': compiler.SCHEMA_VERSION,
           **{k: [] for k in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}}
    paragraphs = []
    for i in range(10):
        ref = 'doc:p1' if shared_source else f'doc:p{i + 1}'
        raw['entities'].append({'key': f'record_{i}', 'name': f'Record {i}',
            'evidence_refs': [ref], 'confidence': 1,
            'properties': [{'name': 'id', 'data_type': 'string', 'is_key': True, 'is_title': True}]
            if i < complete else []})
        if not shared_source or i == 0:
            paragraphs.append({'ref': ref, 'text': 'Each record has a unique string identifier.'})
            raw['coverage'].append({'source_ref': ref, 'status': 'modeled', 'reason': 'Records',
                                    'change_keys': []})
        raw['coverage'][-1]['change_keys'].append(f'record_{i}')
    return raw, {'paragraphs': paragraphs, 'documents': [], 'fingerprint': 'a' * 64}


def normalize(raw, source):
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[],
        data_mappings=[], relation_data_mappings=[])
    return compiler.normalize_scenario_model(None, scenario, raw, source_bundle=source)


def test_sparse_proposal_preserves_existing_business_content():
    original = DistillationDocument(beneficiary='Reviewer', desired_outcome='Exact result',
        entities=[{'key': 'record', 'name': 'Record', 'description': 'Source-backed record',
                   'identity': 'Unique identifier', 'attributes': ['Identifier', 'Amount']}])
    sparse = normalize_proposal({'entities': [{'key': 'record', 'name': 'Record'}]}, original)
    assert sparse.beneficiary == 'Reviewer'
    assert sparse.desired_outcome == 'Exact result'
    assert sparse.entities[0].attributes == ['Identifier', 'Amount']
    assert sparse.entities[0].identity == 'Unique identifier'


def test_independent_entities_share_source_without_sharing_structural_blockers():
    raw, source = entity_batch(complete=9, shared_source=True)
    actual = normalize(raw, source)
    candidates = [c for c in actual['draft_candidates'] if c['resource_kind'] == 'entity']
    assert sum(c['promotion_eligible'] for c in candidates) == 9
    assert candidates[-1]['validation_status'] == 'blocked'


def test_repair_rejects_lost_nested_property():
    raw = {'entities': [{'key': 'record', 'name': 'Record', 'properties': [
        {'name': 'id', 'data_type': 'string'}, {'name': 'amount', 'data_type': 'number'}]}],
        'workflows': [{'key': 'flow', 'name': 'Flow'}]}
    initial = {**deepcopy(raw), 'unresolved': [{'code': 'invalid_workflow', 'blocking': True}]}
    proposed = deepcopy(raw)
    proposed['entities'][0]['properties'].pop()
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: proposed, normalize=lambda candidate: {**candidate, 'unresolved': []})
    assert result['entities'][0]['properties'] == raw['entities'][0]['properties']
    assert result['unresolved'][0]['blocking']


def test_repair_rejects_regression_of_valid_required_constraint():
    raw = {'entities': [{'key': 'record', 'name': 'Record', 'properties': [
        {'name': 'amount', 'data_type': 'number', 'is_required': True, 'constraints': {'minimum': 0}}]}]}
    initial = {**deepcopy(raw), 'unresolved': [{'code': 'invalid_workflow', 'blocking': True}]}
    proposed = deepcopy(raw)
    proposed['entities'][0]['properties'][0].update(is_required=False, constraints={})
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: proposed, normalize=lambda candidate: {**candidate, 'unresolved': []})
    assert result['entities'][0]['properties'][0]['is_required']
    assert result['entities'][0]['properties'][0]['constraints'] == {'minimum': 0}


def test_source_conflict_still_blocks_every_actual_consumer():
    raw, source = entity_batch(complete=10, shared_source=True)
    raw['unresolved'] = [{'code': 'SOURCE_CONFLICT', 'message': 'Sources disagree',
                          'source_refs': ['doc:p1'], 'blocking': True,
                          'affected_change_keys': ['record_9']}]
    actual = normalize(raw, source)
    assert not any(c['promotion_eligible'] for c in actual['draft_candidates']
                   if c['resource_kind'] == 'entity')


def test_candidate_with_invalid_generated_dependency_is_not_eligible():
    raw, source = entity_batch(complete=9, shared_source=True)
    raw['relations'] = [{'key': 'links', 'name': 'Links', 'source_ref': 'record_0',
        'target_ref': 'record_9', 'relation_type': '1:N', 'evidence_refs': ['doc:p1'], 'confidence': 1}]
    raw['coverage'][0]['change_keys'].append('links')
    actual = normalize(raw, source)
    relation = next(c for c in actual['draft_candidates'] if c['resource_kind'] == 'relation')
    assert not relation['promotion_eligible']
    assert relation['validation_issues']


def test_generated_errors_do_not_become_questions_when_source_also_has_a_gap():
    from app.services.assistant_decision_gate import build_decision_gate
    gate = build_decision_gate({'unresolved': [
        {'code': 'invalid_workflow', 'message': 'Generated graph is invalid'},
        {'code': 'missing_evidence', 'message': 'Which source identifies the record?'}]})
    assert [q['code'] for q in gate['questions']] == ['missing_evidence']
    assert gate['questions'][0]['completion_condition']


def test_structured_property_requirement_rejects_wrong_semantics_and_accepts_correct():
    import hashlib
    import json
    source_text = json.dumps({'entities': [{'key': 'source', 'name': 'Record',
        'attributes': ['Identifier', 'Amount'], 'property_contracts': [
            {'attribute': 'Amount', 'data_type': 'number', 'is_required': True,
             'is_key': False, 'constraints': {'minimum': 0}, 'enum_values': []}]}]})
    source = compiler.build_source_bundle('', [], complete_handoffs=[{'id': 'handoff',
        'filename': 'Contract', 'status': 'parsed', 'parsed_text': source_text,
        'usage_plane': 'modeling_material', 'content_hash': hashlib.sha256(source_text.encode()).hexdigest()}])
    ref = next(p['ref'] for p in source['paragraphs'] if p.get('structured_handoff'))
    raw, _ = entity_batch(complete=10, shared_source=True)
    raw['entities'] = raw['entities'][:1]
    raw['entities'][0].update(evidence_refs=[ref], source_entity_bindings=[{
        'source_ref': ref, 'source_key': 'source', 'attribute_map': {'Identifier': 'id', 'Amount': 'amount'}}])
    raw['entities'][0]['properties'].append({'name': 'amount', 'data_type': 'string'})
    raw['coverage'] = [{'source_ref': p['ref'], 'status': 'modeled', 'reason': 'Source',
                        'change_keys': ['record_0']} for p in source['paragraphs']]
    result = normalize(raw, source)
    assert any(i['code'] == 'source_property_contract_mismatch' for i in result['unresolved'])
    raw['entities'][0]['properties'][-1].update(data_type='number', is_required=True, constraints={'minimum': 0})
    assert not normalize(raw, source)['unresolved']


def test_typed_distillation_quality_distinguishes_exploration_from_complete_handoff():
    from app.services.distillation_construction_quality import evaluate_document
    document = DistillationDocument(entities=[{'key': 'record', 'name': 'Record'}])
    quality_projection = evaluate_document(document)
    assert quality_projection['complete_entity_count'] == 0
    assert quality_projection['issues'][0]['missing']
    complete = document.model_dump()
    complete.update(desired_outcome='Verified record', success_metric='Expected fields',
                    evidence=[{'key': 'expert', 'title': 'Expert statement'}])
    complete['entities'][0].update(description='Business record', identity='Identifier',
        evidence_refs=['expert'], attributes=['Identifier'], property_contracts=[{
            'attribute': 'Identifier', 'data_type': 'string', 'is_required': True, 'is_key': True}])
    assert evaluate_document(DistillationDocument.model_validate(complete))['construction_complete']


def test_construction_answer_requires_current_question_and_revision():
    import pytest
    from fastapi import HTTPException
    from app.construction_schemas import ConstructionResolution
    from app.services.assistant_decision_gate import attach_decision_gate
    from app.services.construction_resolution_service import resolution_message
    data = attach_decision_gate({'unresolved': [{'code': 'missing_evidence', 'message': 'What uniquely identifies the record?'}]})
    question = data['decision_gate']['questions'][0]
    proposal = {'run_revision': 2, 'payload': data}
    instruction = ConstructionResolution(proposal_id='synthetic', expected_revision=2, action='clarify',
        answers=[{'question_id': question['question_id'], 'answer': 'The signed source defines the identifier.'}])
    prompt = resolution_message(proposal, instruction)
    assert 'The signed source' in prompt
    assert data['decision_gate']['blocking_issue_count'] == 1
    for changes in ({'expected_revision': 1}, {'answers': [{'question_id': 'f' * 24, 'answer': 'Answer'}]}):
        with pytest.raises(HTTPException) as error:
            resolution_message(proposal, instruction.model_copy(update=changes) if 'expected_revision' in changes
                else ConstructionResolution.model_validate({**instruction.model_dump(), **changes}))
        assert error.value.status_code == 409


def test_repair_reports_no_progress_without_claiming_completion():
    raw = {'entities': [{'key': 'record'}]}
    initial = {'unresolved': [{'code': 'invalid_entity'}]}
    result = quality.repair_candidates(raw, initial, prompt='source', prompt_limit=100_000,
        generate=lambda _: raw, normalize=lambda _: {'unresolved': [{'code': 'invalid_entity'}]})
    assert result['repair_summary']['stop_reason'] == 'no_progress'
    assert result['repair_summary']['remaining_blocker_count'] == 1


def test_original_property_contract_remains_blocking_after_payload_erases_metadata():
    from app.services.construction_source_requirements import preserved_requirement_issues
    original = [{'attribute': 'Amount', 'property_name': 'amount', 'data_type': 'number',
        'is_required': True, 'constraints': {'minimum': 0}}]
    edited_properties = [{'name': 'amount', 'data_type': 'string', 'is_required': False}]
    assert preserved_requirement_issues(original, edited_properties)[0]['blocking']


def test_omitted_required_entity_does_not_invalidate_nine_complete_peers():
    import json
    raw, source = entity_batch(complete=10, shared_source=True)
    requirements = [{'key': f'source_{i}', 'name': f'Record {i}', 'attributes': ['Identifier'],
        'property_contracts': [{'attribute': 'Identifier', 'data_type': 'string',
            'is_required': True, 'is_key': True}]} for i in range(10)]
    source['paragraphs'][0].update(structured_handoff=True, source_id='doc',
        text=json.dumps({'source_path': ['entities'], 'value': requirements}))
    raw['entities'].pop()
    raw['coverage'][0]['change_keys'].pop()
    for i, candidate in enumerate(raw['entities']):
        candidate['properties'][0]['is_required'] = True
        candidate['source_entity_bindings'] = [{'source_ref': 'doc:p1', 'source_key': f'source_{i}',
            'attribute_map': {'Identifier': 'id'}}]
    result = normalize(raw, source)
    candidates = [c for c in result['draft_candidates'] if c['resource_kind'] == 'entity']
    assert sum(c['promotion_eligible'] for c in candidates) == 9
    missing = [i for i in result['unresolved'] if i['code'] == 'missing_source_entity']
    assert len(missing) == 1
    assert 'Record 9' in missing[0]['message']


def test_fragmented_handoff_retains_nested_enum_requirement():
    import json
    from app.services.construction_source_requirements import entity_requirements
    fragments = [(['key'], 'record'), (['attributes', 0], 'State'),
        (['property_contracts', 0, 'attribute'], 'State'),
        (['property_contracts', 0, 'data_type'], 'string'),
        (['property_contracts', 0, 'is_required'], True),
        (['property_contracts', 0, 'enum_values', 0], 'open'),
        (['property_contracts', 0, 'enum_values', 1], 'closed')]
    paragraphs = [{'structured_handoff': True, 'source_id': 'doc', 'ref': f'doc:p{i}',
        'text': json.dumps({'source_path': ['entities', 0, *path], 'value': value})}
        for i, (path, value) in enumerate(fragments)]
    requirement = entity_requirements(paragraphs)[('doc', 'record')]
    assert requirement['attributes'] == ['State']
    assert requirement['property_contracts'][0]['enum_values'] == ['open', 'closed']


def test_malformed_structured_contract_fails_with_safe_validation_error():
    import json
    import pytest
    from app.services.construction_source_requirements import entity_requirements
    paragraph = {'structured_handoff': True, 'source_id': 'doc', 'ref': 'doc:p1',
        'text': json.dumps({'source_path': ['entities'], 'value': [{'key': 'record',
            'property_contracts': [{'attribute': 'Amount', 'data_type': 'number',
                'is_required': True, 'constraints': 'invalid contract'}]}]})}
    with pytest.raises(ValueError, match='结构化属性要求无效'):
        entity_requirements([paragraph])


def test_distinct_business_questions_in_one_paragraph_are_not_lost():
    from app.services.assistant_decision_gate import build_decision_gate
    gate = build_decision_gate({'unresolved': [
        {'code': 'source_conflict', 'message': 'Which date is authoritative?', 'source_refs': ['doc:p1']},
        {'code': 'source_conflict', 'message': 'Who can approve exceptions?', 'source_refs': ['doc:p1']}],
        'coverage': [{'status': 'ambiguous', 'source_refs': ['doc:p1'], 'reason': 'Source ambiguous'}]})
    assert [q['message'] for q in gate['questions']] == [
        'Which date is authoritative?', 'Who can approve exceptions?']


def test_generated_missing_citation_is_internal_repair_not_expert_homework():
    from app.services.assistant_decision_gate import build_decision_gate
    raw, source = entity_batch(complete=10, shared_source=True)
    raw['entities'][0]['evidence_refs'] = []
    raw['coverage'][0]['change_keys'].remove('record_0')
    result = normalize(raw, source)
    issue = next(i for i in result['unresolved'] if i['code'] == 'missing_evidence')
    assert quality.is_generated_contract_issue(issue)
    result['unresolved'].append({'code': 'business_ambiguity', 'message': 'Which date is authoritative?'})
    gate = build_decision_gate(result)
    assert [q['code'] for q in gate['questions']] == ['business_ambiguity']
