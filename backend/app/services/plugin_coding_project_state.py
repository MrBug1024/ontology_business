"""Read current workspace governance facts without copying acceptance case data."""
from __future__ import annotations

from copy import deepcopy


def project_state(document: dict) -> dict:
    phase = document.get('phase')
    source_phase = (document.get('round_base_phase', phase)
                    if phase == 'generating' and document.get('round_mode') == 'discuss' else phase)
    recorded_issues = document.get('validation')
    acceptance = document.get('acceptance_request', {})
    cases = acceptance.get('acceptance_cases', [])
    confirmed = acceptance.get('confirmed_business_acceptance') is True
    count = len(cases) if isinstance(cases, list) else 0
    # The protected manifest is packaging input, and can retain an old pending
    # template. The application-owned workspace records the completed review.
    return {
        'phase': phase,
        'source_phase': source_phase,
        'code_reviewed': source_phase == 'released',
        'deterministic_validation': {
            'record_present': isinstance(recorded_issues, list),
            'recorded_issues': deepcopy(recorded_issues) if isinstance(recorded_issues, list) else [],
        },
        'business_acceptance': {
            'confirmed_by_human': confirmed,
            'recorded_case_count': count,
            'record_present': confirmed and count > 0,
            'basis': 'workspace.acceptance_request',
        },
        'business_executed_this_round': False,
    }
