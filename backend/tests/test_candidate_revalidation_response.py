"""The batch HTTP response must survive internal governance diagnostics."""
from types import SimpleNamespace

import pytest

from app.routers import scenarios
from app.schemas import ScenarioModelCandidateBatchPromotionRequest


@pytest.mark.parametrize('eligible', [[], ['candidate-a']])
def test_batch_revalidation_returns_public_counts_without_internal_compiler_results(monkeypatch, eligible):
    scene = SimpleNamespace(id='synthetic-scene')
    rows = [SimpleNamespace(id=value) for value in ('candidate-a', 'candidate-b')]
    summary = {'revalidated_count': 2, 'eligible_count': len(eligible),
        'blocked_count': 2 - len(eligible), 'eligible_draft_ids': eligible,
        'candidate_results': [{'resource_key': 'entity.synthetic',
            'promotion_eligible': bool(eligible), 'validation_issues': []}]}
    state = {'commits': 0, 'refreshed': []}

    class Session:
        info = {'user_id': 'synthetic-user'}

        def add(self, row):
            assert row.operation == 'revalidate_model_candidates'

        def commit(self):
            state['commits'] += 1

        def refresh(self, row):
            state['refreshed'].append(row.id)

    def revalidate(db, scenario, **kwargs):
        assert scenario is scene
        assert kwargs['expected_revisions'] == {'candidate-a': 1, 'candidate-b': 2}
        return rows, summary

    monkeypatch.setattr(scenarios, '_scenario_for_request', lambda *args, **kwargs: scene)
    monkeypatch.setattr(scenarios.tenant_service, 'current_tenant_id', lambda db: 'synthetic-tenant')
    monkeypatch.setattr(scenarios.candidate_governance_service, 'revalidate_candidates', revalidate)
    result = scenarios.revalidate_scenario_model_candidates_batch(scene.id,
        ScenarioModelCandidateBatchPromotionRequest(items=[
            {'draft_id': 'candidate-a', 'expected_revision': 1},
            {'draft_id': 'candidate-b', 'expected_revision': 2}]), db=Session())
    assert result.model_dump() == {'ok': True, 'revalidated_count': 2,
        'eligible_count': len(eligible), 'blocked_count': 2 - len(eligible),
        'eligible_draft_ids': eligible}
    assert state == {'commits': 1, 'refreshed': ['candidate-a', 'candidate-b']}
    assert 'candidate_results' in summary
