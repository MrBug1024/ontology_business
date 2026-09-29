from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.distillation_schemas import DistillationDocument, ScenarioPublishRequest
from app.services import distillation_publication_service as handoff, distillation_service


def test_scenario_handoff_requires_write_lock_and_current_revision(monkeypatch):
    calls = []
    def state(db, scenario_id, **kwargs):
        calls.append((scenario_id, kwargs))
        return SimpleNamespace(revision=3)
    monkeypatch.setattr(distillation_service, 'scenario_state', state)
    with pytest.raises(HTTPException) as caught:
        handoff.publish_scenario(None, 'scene', ScenarioPublishRequest(
            expected_revision=2, decision='continue', decision_reason='Reviewed'))
    assert caught.value.status_code == 409
    assert calls == [('scene', {'write': True, 'lock': True, 'create': False})]


def test_scenario_handoff_reuses_only_its_own_state_identity(monkeypatch):
    document = DistillationDocument(decision='continue', decision_reason='Reviewed')
    state = SimpleNamespace(id='state', tenant_id='tenant', revision=4, document=document.model_dump())
    monkeypatch.setattr(distillation_service, 'scenario_state', lambda *args, **kwargs: state)
    publication = SimpleNamespace(id='publication')
    statements = []
    def scalar(statement):
        statements.append(statement)
        return publication
    result = handoff.publish_scenario(SimpleNamespace(scalar=scalar), 'scene', ScenarioPublishRequest(
        expected_revision=4, decision='continue', decision_reason='Reviewed'))
    assert result is publication
    params = statements[0].compile().params
    assert 'distillation_scenario_state_id' in params.values()
    assert 'state' in params.values()


def test_scenario_handoff_uses_shared_material_writer_and_explicit_decision(monkeypatch):
    document = DistillationDocument()
    state = SimpleNamespace(id='state', tenant_id='tenant', revision=4, document=document.model_dump())
    monkeypatch.setattr(distillation_service, 'scenario_state', lambda *args, **kwargs: state)
    monkeypatch.setattr(handoff.permission_service, 'require_principal', lambda db: SimpleNamespace(user_id='actor'))
    records = iter([None, SimpleNamespace(name='Business')])
    saved = []
    monkeypatch.setattr(handoff, 'create_publication', lambda db, **kwargs: saved.append(kwargs) or kwargs)
    db = SimpleNamespace(scalar=lambda statement: next(records), flush=lambda: None)
    handoff.publish_scenario(db, 'scene', ScenarioPublishRequest(
        expected_revision=4, decision='adjust', decision_reason='Use the retained process'))
    assert state.revision == 5
    assert saved[0]['project_id'] is None
    assert saved[0]['scenario_state_id'] == 'state'
    assert saved[0]['document'].decision == 'adjust'
    assert saved[0]['revision'] == 5
