from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.scenario_model_draft_service import active_working_draft_context


def _row(identity, kind, revision):
    return SimpleNamespace(id=identity, resource_identity=identity, resource_key=identity,
        resource_kind=kind, revision=revision, draft_status='needs_validation' if revision else 'ready_for_review',
        updated_at=datetime.now(timezone.utc), lineage_started_at=datetime.now(timezone.utc), proposal_id='proposal',
        payload={'name': identity}, source_payload={}, task_id='ontology', title=identity,
        validation_issues=[], source_refs=[], source_thread_id='thread', source_message_id='message',
        predecessor_draft_id=None, predecessor_revision=None, materialization_source='compiler_sidecar')


def test_context_keeps_entities_and_user_edits_without_duplicate_property_sidecars():
    rows = [_row('entity', 'entity', 0), _row('duplicate_property','property',0), _row('edited_property','property',1)]
    db = SimpleNamespace(info={'tenant_id':'tenant','user_id':'actor'},
        scalars=lambda query: SimpleNamespace(all=lambda: rows))
    context = active_working_draft_context(db,SimpleNamespace(id='scenario',tenant_id='tenant'))
    assert {item['resource_key'] for item in context} == {'entity','edited_property'}
    assert len(rows) == 3


def test_full_handoff_does_not_turn_unedited_ai_output_into_new_evidence(monkeypatch):
    from app.services import scenario_model_compiler as compiler, distillation_service
    monkeypatch.setattr(compiler.permission_service, 'require_scenario_permission', lambda *args, **kwargs: None)
    monkeypatch.setattr(compiler, '_mapping_catalog', lambda *args: ([], {}))
    generated = {'draft_id':'generated','resource_key':'one','revision':0}
    edited = {'draft_id':'edited','resource_key':'two','revision':1}
    monkeypatch.setattr(compiler.scenario_model_draft_service, 'active_working_draft_context', lambda *args: [generated, edited])
    monkeypatch.setattr(distillation_service, 'modeling_documents', lambda *args, **kwargs: [{'business_decision':'continue'}])
    context = compiler.prepare_compilation_context(None,SimpleNamespace(id='scenario'))
    assert context['working_drafts'] == [edited]
    assert context['consumed_draft_revisions'] == {'edited':1}
    monkeypatch.setattr(distillation_service, 'modeling_documents', lambda *args, **kwargs: [])
    assert compiler.prepare_compilation_context(None,SimpleNamespace(id='scenario'))['working_drafts'] == [generated, edited]
