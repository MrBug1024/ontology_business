"""Ordinary chat and SSE must enforce the saved construction revision gate."""
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.routers import assistant
from app.schemas import AssistantChatRequest
from app.services import construction_resolution_service as resolution


def request():
    return AssistantChatRequest(message='Explicit synthetic correction', request_id='synthetic-correction',
        scenario_id='synthetic-scene', thread_id='synthetic-thread', mode='draft', draft_kind='scenario_model',
        construction_resolution={'proposal_id': 'synthetic-proposal', 'expected_revision': 1,
            'action': 'replan', 'rationale': 'Retain the original requirements and add an explicit key.'})


class Session:
    info = {}

    def scalars(self, statement):
        proposal = {'kind': 'scenario_model', 'proposal_id': 'synthetic-proposal', 'run_revision': 2}
        return SimpleNamespace(all=lambda: [SimpleNamespace(proposal=proposal)])


@pytest.mark.parametrize('entrypoint', [assistant.chat, assistant.stream_chat])
def test_unattached_construction_correction_rejects_stale_revision_before_model_or_write(monkeypatch, entrypoint):
    monkeypatch.setattr(resolution.permission_service, 'require_principal',
        lambda db: SimpleNamespace(tenant_id='synthetic-tenant', user_id='synthetic-user'))

    def no_model(*args, **kwargs):
        pytest.fail('An obsolete construction correction reached ordinary assistant processing')

    monkeypatch.setattr(assistant, '_scenario', no_model)
    with pytest.raises(HTTPException) as rejected:
        entrypoint(request(), db=Session())
    assert rejected.value.status_code == 409


def test_valid_correction_uses_saved_result_and_keeps_the_original_request_immutable(monkeypatch):
    monkeypatch.setattr(resolution.permission_service, 'require_principal',
        lambda db: SimpleNamespace(tenant_id='synthetic-tenant', user_id='synthetic-user'))
    payload = request()
    db = SimpleNamespace(scalars=lambda statement: SimpleNamespace(all=lambda: [
        SimpleNamespace(proposal={'kind': 'scenario_model', 'proposal_id': 'synthetic-proposal',
            'run_revision': 1, 'payload': {'decision_gate': {'questions': []}}})]))
    prepared = resolution.prepare_request(db, payload)
    assert payload.message == 'Explicit synthetic correction'
    assert prepared.message != payload.message
    assert payload.construction_resolution.rationale in prepared.message
    assert prepared.construction_resolution == payload.construction_resolution
    assert prepared.request_id == payload.request_id


def test_correction_remains_bounded_after_server_resolution(monkeypatch):
    monkeypatch.setattr(resolution, 'prepare_resolution', lambda db, payload: 'x' * 12001)
    with pytest.raises(HTTPException) as rejected:
        resolution.prepare_request(object(), request())
    assert rejected.value.status_code == 422
