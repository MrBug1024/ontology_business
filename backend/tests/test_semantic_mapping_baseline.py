from app.routers import assistant


def test_baseline_drift_keeps_mapping_candidate_but_removes_applicable_resource():
    candidate = {'task_id': 'mapping', 'resource_kind': 'semantic_mapping',
        'resource_key': 'semantic_mapping.subjects', 'payload': {'fields': [{'ontology_property_id': 'p'}]}}
    proposal = {'kind': 'scenario_model', 'payload': {'semantic_mappings': [{'key': 'semantic_mapping.subjects'}],
        'draft_candidates': [candidate], 'tasks': [], 'changes': [{'change_id': 'semantic_mapping.subjects'}]}}
    result = assistant._complete_baseline_changed_proposal(proposal, current_snapshot={'revision': 'current'})
    assert result['payload']['semantic_mappings'] == []
    assert result['payload']['draft_candidates'] == [candidate]
    assert result['requires_confirmation'] is False
    assert result['changes'] == []
    assert proposal['payload']['semantic_mappings']


def test_mapping_gate_checks_evidence_and_distinguishes_stale_definitions():
    from app.services.assistant_decision_gate import build_decision_gate
    payload = {'semantic_mappings': [{'key': 'mapping'}]}
    missing = build_decision_gate(payload)
    assert missing['resource_counts'] == {'semantic_mappings': 1}
    assert not missing['safe_to_formalize']
    assert missing['missing_evidence_resource_count'] == 1
    payload['unresolved'] = [{'code': 'BASELINE_CHANGED_DURING_COMPILATION', 'blocking': True}]
    stale = build_decision_gate(payload)
    assert stale['reason_codes'] == ['SCENARIO_CONTEXT_CHANGED']
    assert stale['questions'] == []
    assert not stale['safe_to_formalize']
    payload['unresolved'].append({'code': 'BUSINESS_AMBIGUITY', 'blocking': True})
    assert build_decision_gate(payload)['mode'] == 'clarify'
