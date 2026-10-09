from __future__ import annotations

from copy import deepcopy
import json

from app.services.plugin_coding_prompt import coding_messages
from app.services.plugin_coding_tools import execute


def reviewed_discussion():
    return {
        'phase': 'generating', 'round_mode': 'discuss', 'round_base_phase': 'released',
        'validation': [], 'coding_contract': {'scenario': {'name': 'Synthetic business scenario'},
            'capabilities': [{'kind': 'function', 'key': 'synthetic_internal_key', 'name': 'Check current request'}]},
        'files': {}, 'manifest': {'acceptance': {'kind': 'pending_human_business_acceptance', 'cases': []}},
        'acceptance_request': {'confirmed_business_acceptance': True, 'acceptance_cases': [
            {'receipt_id': 'synthetic-private-receipt', 'private_case_data': 'must-not-enter-model-status'}]},
    }


def test_discussion_context_uses_current_review_record_instead_of_pending_template():
    document = reviewed_discussion()
    original = deepcopy(document)
    context = json.loads(coding_messages(document, 'Explain this plugin', native_tools=True)[1]['content'])
    state = context['project_state']
    assert state['phase'] == 'generating'
    assert state['source_phase'] == 'released'
    assert state['code_reviewed'] is True
    assert state['deterministic_validation']['recorded_issues'] == []
    assert state['business_acceptance']['confirmed_by_human'] is True
    assert state['business_acceptance']['recorded_case_count'] == 1
    assert state['business_acceptance']['record_present'] is True
    assert 'synthetic-private-receipt' not in str(state)
    assert 'must-not-enter-model-status' not in str(state)
    assert document == original


def test_contract_inspection_reports_current_authoritative_project_state():
    document = reviewed_discussion()
    result = execute(document, 'inspect_scenario_contract', {})
    assert result['project_state']['source_phase'] == 'released'
    assert result['project_state']['business_acceptance']['record_present'] is True
    assert result['contract'] == document['coding_contract']


def test_deterministic_source_check_does_not_erase_historical_business_acceptance():
    result = execute(reviewed_discussion(), 'validate_plugin_project', {})
    assert result['valid'] is False  # No candidate files were provided in this fixture.
    assert result['business_accepted'] is True
    assert result['code_executed'] is False
    assert result['project_state']['business_acceptance']['record_present'] is True


def test_missing_acceptance_record_is_unknown_even_when_template_claims_acceptance():
    document = reviewed_discussion()
    document['acceptance_request'] = {}
    document['manifest']['acceptance'] = {'kind': 'human_business_accepted', 'cases': [{'id': 'claimed'}]}
    state = execute(document, 'inspect_scenario_contract', {})['project_state']
    assert state['business_acceptance']['record_present'] is False
    assert state['business_acceptance']['confirmed_by_human'] is False
    assert state['business_acceptance']['recorded_case_count'] == 0


def test_modified_source_retains_acceptance_record_without_claiming_current_code_review():
    document = reviewed_discussion()
    document.update(phase='generating', round_base_phase='draft', validation=['A recorded source issue'])
    state = execute(document, 'inspect_scenario_contract', {})['project_state']
    assert state['code_reviewed'] is False
    assert state['deterministic_validation']['recorded_issues'] == ['A recorded source issue']
    assert state['business_acceptance']['record_present'] is True


def test_new_coding_round_does_not_claim_the_previous_review_covers_in_progress_source():
    document = reviewed_discussion()
    document['round_mode'] = 'generate'
    state = execute(document, 'inspect_scenario_contract', {})['project_state']
    assert state['source_phase'] == 'generating'
    assert state['code_reviewed'] is False
    assert state['business_acceptance']['record_present'] is True
