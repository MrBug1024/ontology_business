from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.distillation_schemas import DistillationDocument
from app.services import distillation_conversation_service as conversations
from app.services import scenario_discovery_context as discovery


def _frozen(scope='shared'):
    return {
        'version': 'scenario-discovery-context.v1',
        'scenario': {'id': 'scene', 'name': 'Scene', 'description': 'Meaning'}, 'revision': 3,
        'business': {key: '' for key in ('beneficiary', 'pain', 'desired_outcome', 'success_metric',
            'scope', 'non_goals', 'decision_reason')} | {'decision': 'undecided', 'open_questions': []},
        'handoff': {'status': 'missing', 'publication_id': None, 'publication_revision': None},
        'construction': {'can_continue': False, 'reason': 'Not handed off'},
        'processes': {key: {'nodes': [], 'total_nodes': 0, 'has_more': False} for key in ('as_is', 'to_be')},
        'historical_cases': {'items': [], 'total_count': 0, 'has_more': False},
        'materials': {'sources': [{'data_source_id': 'source', 'name': 'Known material', 'type': 'file_bucket',
            'scope': scope, 'resource_scope': 'modeling', 'content_read': False}], 'has_more': False, 'next_offset': None},
        'boundaries': [],
    }


def _directory_db(monkeypatch, rows=(), *, shared_allowed=False):
    scenario = SimpleNamespace(id='scene', tenant_id='tenant')
    monkeypatch.setattr(discovery.permission_service, 'require_principal', lambda db: SimpleNamespace(tenant_id='tenant'))
    monkeypatch.setattr(discovery.tenant_service, 'require_scenario', lambda db, key: scenario)
    monkeypatch.setattr(discovery.permission_service, 'require_scenario_permission', lambda *args: None)
    def tenant_permission(*args):
        if not shared_allowed:
            raise HTTPException(403, '共享范围已撤销')
    monkeypatch.setattr(discovery.permission_service, 'require_tenant_permission', tenant_permission)
    statements = []
    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: list(rows))
    return SimpleNamespace(info={}, execute=execute), statements


def _row(**changes):
    return SimpleNamespace(**({'id': 'source', 'name': 'Known material', 'type': 'file_bucket',
                              'scenario_id': 'scene'} | changes))


def test_frozen_shared_directory_rejects_revoked_workspace_read(monkeypatch):
    db, statements = _directory_db(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        discovery.assert_frozen_material_directory(db, 'scene', _frozen())
    assert caught.value.status_code == 409
    assert 'IS NULL' not in str(statements[0])


def test_scenario_directory_remains_usable_when_only_shared_read_is_denied(monkeypatch):
    frozen = _frozen('scenario')
    db, statements = _directory_db(monkeypatch, [_row()])
    discovery.assert_frozen_material_directory(db, 'scene', frozen)
    assert len(statements) == 1
    assert 'IS NULL' not in str(statements[0])
    assert frozen == _frozen('scenario')


@pytest.mark.parametrize('rows', [[], [_row(name='Renamed')], [_row(type='dataset')], [_row(scenario_id=None)]])
def test_frozen_directory_rejects_deleted_renamed_retyped_or_relocated_source(monkeypatch, rows):
    db, _statements = _directory_db(monkeypatch, rows, shared_allowed=True)
    with pytest.raises(HTTPException) as caught:
        discovery.assert_frozen_material_directory(db, 'scene', _frozen('scenario'))
    assert caught.value.status_code == 409


def test_frozen_directory_rejects_a_different_scenario_before_catalog_read(monkeypatch):
    db, statements = _directory_db(monkeypatch, [_row()])
    with pytest.raises(HTTPException) as caught:
        discovery.assert_frozen_material_directory(db, 'another-scene', _frozen('scenario'))
    assert caught.value.status_code == 409
    assert not statements


@pytest.mark.parametrize('invalid', ['duplicate', 'oversized', 'unknown_field'])
def test_frozen_directory_rejects_ambiguous_or_unbounded_snapshot(monkeypatch, invalid):
    snapshot = _frozen('scenario')
    source = snapshot['materials']['sources'][0]
    if invalid == 'duplicate':
        snapshot['materials']['sources'].append(source.copy())
    elif invalid == 'oversized':
        snapshot['materials']['sources'] = [source.copy() for _ in range(21)]
    else:
        snapshot['materials']['sources'][0]['parsed_text'] = 'Must never reach AI'
    db, statements = _directory_db(monkeypatch, [_row()])
    with pytest.raises(HTTPException) as caught:
        discovery.assert_frozen_material_directory(db, 'scene', snapshot)
    assert caught.value.status_code == 409
    assert not statements


def _turn_context(monkeypatch):
    document = DistillationDocument().model_dump()
    row = SimpleNamespace(project_id='project', base_revision=2, model_calls=0, context={
        'scenario_id': 'scene', 'document': document, 'evidence_identity': {},
        'scenario_baseline': {'state_revision': 3}, 'scenario_discovery': _frozen(),
    })
    monkeypatch.setattr(conversations.distillation_service, 'project', lambda *args, **kwargs:
        SimpleNamespace(scenario_id='scene', document=document))
    monkeypatch.setattr(conversations.distillation_service, 'assert_revision', lambda *args: None)
    monkeypatch.setattr(conversations.distillation_service, 'scenario_state', lambda *args, **kwargs:
        SimpleNamespace(revision=3, document=document))
    monkeypatch.setattr(conversations, 'capture_evidence_identity', lambda *args: {})
    monkeypatch.setattr(conversations.attachments, 'assert_available', lambda *args: None)
    return row


def test_conversation_context_reauthorizes_even_unread_directory_entries(monkeypatch):
    db, _statements = _directory_db(monkeypatch)
    row = _turn_context(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        conversations.assert_current_context(db, row)
    assert caught.value.status_code == 409


def test_model_is_not_called_when_frozen_directory_access_has_been_revoked(monkeypatch):
    from app.services import distillation_conversation_worker as worker

    db, _statements = _directory_db(monkeypatch)
    row = _turn_context(monkeypatch)
    monkeypatch.setattr(worker.leases, 'owned', lambda *args, **kwargs: row)
    monkeypatch.setattr(worker.conversations, 'ensure_turn_resource_selection',
        lambda *args: pytest.fail('Model setup must wait for directory authorization'))
    monkeypatch.setattr(worker.llm_service, 'chat_stream',
        lambda *args, **kwargs: pytest.fail('Revoked directory reached the model'))
    @contextmanager
    def session_factory():
        yield db
    with pytest.raises(HTTPException) as caught:
        worker._model_call(None, session_factory, [{'role': 'user', 'content': 'Continue'}])
    assert caught.value.status_code == 409


def test_legacy_turn_without_discovery_snapshot_keeps_existing_behavior(monkeypatch):
    db, statements = _directory_db(monkeypatch)
    row = _turn_context(monkeypatch)
    del row.context['scenario_discovery']
    conversations.assert_current_context(db, row)
    assert not statements
