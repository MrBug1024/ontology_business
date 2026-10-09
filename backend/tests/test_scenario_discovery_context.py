from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.distillation_schemas import DistillationDocument, ProcessGraph, ProcessNode


def _setup(monkeypatch, document=None, publication_document=None, *, write=True):
    from app.services import scenario_discovery_context as service

    document = document or DistillationDocument()
    scenario = SimpleNamespace(id='scene', tenant_id='tenant', name='Scenario', description='Meaning', status='active')
    principal = SimpleNamespace(tenant_id='tenant')
    calls = []
    monkeypatch.setattr(service.permission_service, 'require_principal', lambda db: principal)
    monkeypatch.setattr(service.tenant_service, 'require_scenario', lambda db, key: scenario)
    monkeypatch.setattr(service.permission_service, 'require_scenario_permission', lambda *args: calls.append('authorized'))
    monkeypatch.setattr(service.permission_service, 'check_scenario', lambda *args: SimpleNamespace(allowed=write))
    monkeypatch.setattr(service.distillation_service, 'scenario_state', lambda *args, **kwargs:
        calls.append(kwargs) or SimpleNamespace(revision=7, document=document.model_dump()))
    monkeypatch.setattr(service.distillation_library_service, 'list_sources', lambda *args:
        {'sources': [{'data_source_id': 'material', 'name': 'Historical material', 'type': 'file_bucket', 'scope': 'scenario'}],
         'has_more': True, 'next_offset': 20})
    monkeypatch.setattr(service.distillation_service, 'modeling_documents', lambda *args, **kwargs:
        [{'business_decision': document.decision}])
    publication = None if publication_document is None else SimpleNamespace(
        id='handoff', project_revision=3, document=publication_document.model_dump())
    db = SimpleNamespace(scalar=lambda statement: publication)
    return service, db, scenario, calls


def test_adopted_business_meaning_is_available_before_handoff_without_runtime_data(monkeypatch):
    document = DistillationDocument(beneficiary='Experts', pain='Slow decisions', desired_outcome='Review reliably',
        success_metric='Every decision has evidence', non_goals='Never submit automatically',
        scope='Review only', decision='undecided', open_questions=['Who confirms?'])
    service, db, _scenario, calls = _setup(monkeypatch, document)
    result = service.context_for_scenario(db, 'scene')
    assert result.revision == 7
    assert result.business.desired_outcome == document.desired_outcome
    assert result.business.non_goals == document.non_goals
    assert result.business.success_metric == document.success_metric
    assert result.business.open_questions == ['Who confirms?']
    assert result.handoff.status == 'missing'
    assert not result.construction.can_continue
    assert result.materials.sources[0].content_read is False
    assert result.materials.sources[0].resource_scope == 'modeling'
    assert result.materials.has_more
    assert calls == ['authorized', {'write': False, 'create': False}]
    assert '正式运行输入' in service.prompt_context(result)


@pytest.mark.parametrize('decision,write,expected', [
    ('continue', True, True), ('adjust', True, True), ('stop', True, False),
    ('undecided', True, False), ('continue', False, False),
])
def test_handoff_eligibility_uses_exact_document_and_server_permission(monkeypatch, decision, write, expected):
    document = DistillationDocument(decision=decision, desired_outcome='Outcome', success_metric='Metric')
    service, db, _scenario, _calls = _setup(monkeypatch, document, document, write=write)
    result = service.context_for_scenario(db, 'scene')
    # Project revision and retained scenario revision are independent identities.
    assert result.handoff.status == 'current'
    assert result.handoff.publication_revision == 3
    assert result.revision == 7
    assert result.construction.can_continue is expected
    assert result.construction.reason


def test_adopted_changes_do_not_claim_the_old_handoff_is_current(monkeypatch):
    old = DistillationDocument(decision='continue', desired_outcome='Old outcome')
    new = old.model_copy(update={'non_goals': 'No automatic submission'})
    service, db, _scenario, _calls = _setup(monkeypatch, new, old)
    result = service.context_for_scenario(db, 'scene')
    assert result.handoff.status == 'stale'
    assert not result.construction.can_continue
    assert result.business.non_goals == 'No automatic submission'


def test_current_handoff_cannot_override_another_projects_stop_decision(monkeypatch):
    document = DistillationDocument(decision='continue')
    service, db, _scenario, _calls = _setup(monkeypatch, document, document)
    monkeypatch.setattr(service.distillation_service, 'modeling_documents', lambda *args, **kwargs:
        [{'business_decision': 'continue'}, {'business_decision': 'stop'}])
    result = service.context_for_scenario(db, 'scene')
    assert result.handoff.status == 'current'
    assert not result.construction.can_continue
    assert '暂缓或未决' in result.construction.reason


def test_damaged_handoff_contract_fails_closed(monkeypatch):
    document = DistillationDocument(decision='continue')
    service, db, _scenario, _calls = _setup(monkeypatch, document, document)
    def damaged(*args, **kwargs):
        raise HTTPException(409, '资料完整性校验失败')
    monkeypatch.setattr(service.distillation_service, 'modeling_documents', damaged)
    with pytest.raises(HTTPException) as caught:
        service.context_for_scenario(db, 'scene')
    assert caught.value.status_code == 409


def test_context_denial_happens_before_reading_state_or_materials(monkeypatch):
    service, db, _scenario, _calls = _setup(monkeypatch)
    def denied(*args):
        raise HTTPException(403, '无权访问')
    monkeypatch.setattr(service.permission_service, 'require_scenario_permission', denied)
    monkeypatch.setattr(service.distillation_service, 'scenario_state', lambda *args, **kwargs: pytest.fail('state disclosed'))
    monkeypatch.setattr(service.distillation_library_service, 'list_sources', lambda *args: pytest.fail('materials disclosed'))
    with pytest.raises(HTTPException) as caught:
        service.context_for_scenario(db, 'scene')
    assert caught.value.status_code == 403


def test_private_discovery_does_not_expand_foreign_public_scenario_context(monkeypatch):
    service, db, scenario, _calls = _setup(monkeypatch)
    scenario.tenant_id = 'foreign'
    with pytest.raises(HTTPException) as caught:
        service.context_for_scenario(db, 'scene')
    assert caught.value.status_code == 404
    assert service.advisor_context(db, scenario) == ''


def test_empty_state_stays_read_only_and_process_projection_reports_its_bound(monkeypatch):
    nodes = [ProcessNode(key=f'step_{index}', name=f'Step {index}', outcome='Outcome') for index in range(30)]
    service, db, _scenario, _calls = _setup(monkeypatch,
        DistillationDocument(to_be=ProcessGraph(nodes=nodes)))
    result = service.context_for_scenario(db, 'scene')
    assert len(result.processes.to_be.nodes) == service.MAX_PROCESS_NODES
    assert result.processes.to_be.total_nodes == 30
    assert result.processes.to_be.has_more
    monkeypatch.setattr(service.distillation_service, 'scenario_state', lambda *args, **kwargs: None)
    result = service.context_for_scenario(db, 'scene')
    assert result.revision is None
    assert result.business.desired_outcome == ''
    assert result.handoff.status == 'missing'


def test_prompt_scrubs_secret_like_business_text_and_keeps_core_meaning_first(monkeypatch):
    document = DistillationDocument(desired_outcome='Target', non_goals='Never send', success_metric='Evidence required',
        pain='api_key=synthetic_discovery_private_token')
    service, db, _scenario, _calls = _setup(monkeypatch, document)
    result = service.context_for_scenario(db, 'scene')
    prompt = service.prompt_context(result)
    assert 'Target' in prompt[:4000] and 'Never send' in prompt[:4000] and 'Evidence required' in prompt[:4000]
    assert 'synthetic_discovery_private_token' not in result.model_dump_json()
    assert 'synthetic_discovery_private_token' not in prompt


def test_advisor_receives_adopted_goals_and_directory_even_before_any_handoff(monkeypatch):
    from app.routers import assistant

    service, db, scenario, _calls = _setup(monkeypatch,
        DistillationDocument(desired_outcome='Understand adopted goal', non_goals='Do not submit'))
    scenario.industry = ''
    monkeypatch.setattr(assistant.tenant_service, 'current_tenant_id', lambda db: 'tenant')
    result = assistant._scenario_context(db, scenario)
    assert 'Understand adopted goal' in result[:8000]
    assert 'Do not submit' in result[:8000]
    assert 'Historical material' in result
    assert '"content_read": false' in result
    assert '"status": "missing"' in result


def test_context_endpoint_uses_the_shared_read_projection(monkeypatch):
    from app.routers import business_distillation

    service, db, _scenario, _calls = _setup(monkeypatch)
    expected = service.context_for_scenario(db, 'scene')
    calls = []
    monkeypatch.setattr(service, 'context_for_scenario', lambda received_db, scenario_id:
        calls.append((received_db, scenario_id)) or expected)
    assert business_distillation.get_scenario_context('scene', db) == expected
    assert calls == [(db, 'scene')]


def test_distillation_first_prompt_receives_the_frozen_directory_and_adopted_goal(monkeypatch):
    from app.services import distillation_conversation_worker as worker

    service, db, _scenario, _calls = _setup(monkeypatch,
        DistillationDocument(desired_outcome='Adopted stage goal'))
    context = service.context_for_scenario(db, 'scene')
    row = SimpleNamespace(project_id='project', turn_number=1, message='Discuss the next step',
        context={'scenario_discovery': context.model_dump(), 'document': DistillationDocument().model_dump()})
    monkeypatch.setattr(worker.conversations, 'resource_reference_message', lambda row: 'Trusted methods')
    db.scalars = lambda statement: SimpleNamespace(all=lambda: [])
    messages = worker._initial_messages(db, row)
    prompt = '\n'.join(message['content'] for message in messages)
    assert 'Adopted stage goal' in prompt
    assert 'Historical material' in prompt
    assert '未读取正文' in prompt
    assert '"content_read": false' in prompt


def test_long_discovery_document_still_has_a_bounded_prompt(monkeypatch):
    business = {field: 'text' * 1000 for field in ('beneficiary', 'pain', 'desired_outcome',
        'success_metric', 'scope', 'non_goals', 'decision_reason')}
    node_fields = {field: 'text' * 150 for field in ('outcome', 'trigger', 'inputs', 'rule', 'exceptions')}
    nodes = [ProcessNode(key=f'step_{index}', name='名' * 200, owner='人' * 200, **node_fields) for index in range(12)]
    document = DistillationDocument(**business, open_questions=['text' * 250] * 40,
        as_is=ProcessGraph(nodes=nodes), to_be=ProcessGraph(nodes=nodes))
    service, db, _scenario, _calls = _setup(monkeypatch, document)
    prompt = service.prompt_context(service.context_for_scenario(db, 'scene'))
    assert len(prompt) <= service.MAX_PROMPT_CHARS
    assert '"open_question_count": 40' in prompt
    assert '"total_nodes": 12' in prompt
