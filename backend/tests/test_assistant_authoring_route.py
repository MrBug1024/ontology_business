from app.services.assistant_orchestrator import (
    AssistantSemanticDecision, route_assistant_decision, unexecuted_authoring_notice,
    initial_model_task_scope,
)
import pytest
from app.services.assistant_orchestrator import _capability_tools, _decision_from_capability_call


def test_function_tool_selection_reaches_capabilities_without_rebuilding_ontology():
    tool = next(item['function'] for item in _capability_tools()
                if item['function']['name'] == 'compile_scenario_model')
    assert 'scope' in tool['parameters']['required']
    decision = _decision_from_capability_call({'tool_calls': [{'function': {
        'name': 'compile_scenario_model', 'arguments': {'goal': 'create',
        'scope': 'capabilities', 'confidence': 'high', 'reason': 'Only create a function'}}}]})
    plan = route_assistant_decision(decision)
    assert plan.intent == 'scenario_model'
    assert initial_model_task_scope(plan) == 'capabilities'


def test_new_tool_discovery_has_one_authoring_route_and_legacy_calls_still_resolve():
    names = {item['function']['name'] for item in _capability_tools()}
    assert not names & {'draft_ontology', 'draft_mapping', 'draft_workflow'}
    assert 'compile_scenario_model' in names
    legacy = _decision_from_capability_call({'tool_calls': [{'function': {
        'name': 'draft_mapping', 'arguments': {'goal': 'create', 'confidence': 'high'}}}]})
    assert initial_model_task_scope(route_assistant_decision(legacy)) == 'mapping'


def test_rule_and_workflow_request_keeps_both_tasks_and_starts_with_rule():
    from app.services.model_task_scope import bind_request_scope
    decision = _decision_from_capability_call({'tool_calls': [{'function': {
        'name': 'compile_scenario_model', 'arguments': {'goal': 'create',
        'scope': 'scenario_model', 'task_ids': ['workflows', 'rules'],
        'confidence': 'high', 'reason': 'Create rule then dependent workflow'}}}]})
    plan = route_assistant_decision(decision)
    assert initial_model_task_scope(plan) == 'rules'
    assert bind_request_scope({}, plan.public_context())['generation']['requested_task_ids'] == ['rules', 'workflows']


def test_rules_have_a_real_compilation_scope():
    plan = route_assistant_decision(AssistantSemanticDecision(goal='create', scope='rules',
        confidence='high', reason='Create a rule'))
    assert plan.intent == 'scenario_model'
    assert initial_model_task_scope(plan) == 'rules'


@pytest.mark.parametrize('scope', [None, 'general', 'execute', ''])
def test_model_tool_missing_or_invalid_scope_does_not_default_to_ontology(scope):
    with pytest.raises(ValueError, match='建模主题'):
        _decision_from_capability_call({'tool_calls': [{'function': {
            'name': 'compile_scenario_model', 'arguments': {'goal': 'create',
            'scope': scope, 'confidence': 'high', 'reason': 'synthetic'}}}]})


def test_uncertain_authoring_is_not_reported_as_completed_construction():
    plan = route_assistant_decision(AssistantSemanticDecision(goal='create', scope='scenario_model',
        confidence='medium', reason='建设完整模型'))
    assert plan.intent == 'chat'
    assert '尚未启动建模' in unexecuted_authoring_notice(plan)


def test_explicit_draft_continuation_enters_compiler_and_questions_remain_answers():
    for goal, confidence, expected in [('continue_work', 'high', 'scenario_model'), ('answer', 'high', 'chat')]:
        plan = route_assistant_decision(AssistantSemanticDecision(goal=goal, scope='scenario_model',
            confidence=confidence, reason='synthetic'), has_active_model_drafts=True)
        assert plan.intent == expected
        assert unexecuted_authoring_notice(plan) == ''


def test_specific_authoring_scopes_use_the_durable_compiler_without_widening_scope():
    for scope, task in [('ontology','ontology'), ('mapping','mapping'), ('workflow','workflows'),
                        ('capabilities','capabilities'), ('scenario_model','ontology')]:
        plan = route_assistant_decision(AssistantSemanticDecision(goal='create', scope=scope,
            confidence='high', reason='Create the requested definitions'))
        assert plan.intent == 'scenario_model'
        assert plan.capability == 'compile_scenario_model'
        assert initial_model_task_scope(plan) == task


def test_compiler_route_preserves_readonly_and_scope_boundaries():
    for goal, confidence, mode, preferred in [('answer','high','ask','auto'),
            ('create','medium','ask','auto'), ('create','high','execute','auto'),
            ('create','high','draft','mapping')]:
        plan = route_assistant_decision(AssistantSemanticDecision(goal=goal, scope='ontology',
            confidence=confidence, reason='synthetic'), mode=mode, preferred_scope=preferred)
        assert plan.intent == 'chat'


@pytest.mark.parametrize('scope,task', [('workflow', 'workflows'), ('ontology', 'ontology'), ('mapping', 'mapping')])
def test_explicit_scenario_model_resolution_accepts_declared_subscope(scope, task):
    plan = route_assistant_decision(AssistantSemanticDecision(goal='create', scope=scope,
        confidence='high', reason='Refine only the explicitly selected task'),
        mode='draft', preferred_scope='scenario_model')
    assert plan.intent == 'scenario_model'
    assert initial_model_task_scope(plan) == task
