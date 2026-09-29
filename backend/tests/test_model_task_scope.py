from copy import deepcopy

import pytest

from app.routers.assistant import _refresh_model_task_states
from app.services.model_task_scope import bind_request_scope
from app.services import scenario_model_compiler as compiler


def function_result():
    return {'functions': [{'key': 'function.total', 'name': 'Total'}],
            'changes': [{'change_id': 'function.total', 'operation': 'add'}],
            'unresolved': [], 'generation': {'mode': 'staged', 'generated_task_ids': ['capabilities']}}


def task_board(payload):
    return _refresh_model_task_states(compiler.attach_model_task_plan(payload))


def test_function_request_is_ready_without_unrequested_ontology_generation():
    payload = function_result()
    original = deepcopy(payload)
    result = task_board(bind_request_scope(payload, {'scope': 'capabilities'}))
    assert payload == original
    assert [task['id'] for task in result['tasks']] == ['capabilities']
    assert result['current_task_id'] == 'capabilities'
    assert result['tasks'][0]['status'] == 'ready'
    assert result['next_action']['type'] == 'confirm_task'
    assert result['next_action']['requires_confirmation'] is True


def test_specific_scope_does_not_remove_real_resource_blockers():
    payload = function_result()
    payload['unresolved'] = [{'code': 'missing_evidence', 'blocking': True,
                              'message': 'Input evidence missing', 'resource_key': 'function.total'}]
    result = task_board(bind_request_scope(payload, {'scope': 'capabilities'}))
    assert result['tasks'][0]['status'] == 'blocked'
    assert result['tasks'][0]['safe_change_count'] == 0
    assert result['unresolved'] == payload['unresolved']


@pytest.mark.parametrize('routing', [{}, {'scope': 'scenario_model'}])
def test_full_or_legacy_plan_keeps_six_stages_and_dependencies(routing):
    result = task_board(bind_request_scope(function_result(), routing))
    assert len(result['tasks']) == 6
    assert result['current_task_id'] == 'ontology'
    assert result['tasks'][0]['status'] == 'awaiting_generation'
    capability = next(task for task in result['tasks'] if task['id'] == 'capabilities')
    assert capability['depends_on']
    assert capability['status'] == 'waiting'


@pytest.mark.parametrize('scope', [[], ['invented'], 'capabilities', [None]])
def test_corrupt_task_scope_is_rejected(scope):
    payload = function_result()
    payload['generation']['requested_task_ids'] = scope
    with pytest.raises(ValueError):
        compiler.build_model_task_plan(payload)
