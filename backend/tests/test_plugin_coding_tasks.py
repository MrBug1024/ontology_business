from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.plugin_coding_schemas import CodingFile, PluginCodingDraftCreate, PluginCodingReview
from app.services import plugin_authoring_context as context, plugin_coding_export as review
from app.services.plugin_coding_identity import adapter_hash
from app.services.plugin_coding_repair import code_and_repair
from app.services.plugin_project_validation import required_paths, validate_project
from app.services.plugin_native_coding import native_coding_steps
from app.services.plugin_client_contract import client_contract
from app.services.plugin_coding_validation import files_hash, validate_files
from app.services.plugin_coding_worker import CodingStep, apply_step, finish_document, parse_tool_steps
from app.services.scenario_package_artifact import build_artifact


def contract():
    return {'package_name': 'scenario-synthetic', 'host': 'claude_code', 'scenario': {'id': 'scenario'},
            'deployment': {'release_id': 'release', 'definition_hash': 'a' * 64},
            'capabilities': [{'kind': 'function', 'key': 'check', 'input_schema': {
                'type': 'object', 'properties': {'amount': {'type': 'integer'}},
                'required': ['amount'], 'additionalProperties': False}}]}


def files():
    return {'README.md': 'Configure SCENARIO_API_KEY outside this package.\n',
            'skills/run-scenario/SKILL.md': '---\nname: run-scenario\ndescription: Invoke the selected scenario\n---\nUse invoke_scenario_capability and get_scenario_receipt.\n',
            'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\nasync def main():\n    result = await invoke_scenario_capability("function", "check", {"amount": 1})\n    print(result)\nif __name__ == "__main__":\n    asyncio.run(main())\n'}


def document():
    return {'files': files(), 'previous': {}, 'coding_contract': contract(), 'manifest': contract(),
            'revision': 1, 'events': [], 'phase': 'generating', 'active_run_id': 'run',
            'release_id': 'release', 'adapter_hash': adapter_hash(), 'acceptance_request': {},
            'plugin_version': '1.1.0'}


def test_project_build_includes_custom_skills_and_actual_parameterized_client_code():
    source = files()
    source['skills/check-request/SKILL.md'] = '---\nname: check-request\ndescription: Collect current request inputs and invoke the capability\n---\nCall the script with current inputs.\n'
    source['scripts/check_request.py'] = 'from server import invoke_scenario_capability\nasync def check_request(inputs):\n    result = await invoke_scenario_capability("function", "check", inputs)\n    return result\n'
    assert validate_files(source, contract()) == []
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(build_artifact(contract(), files=source))) as archive:
        assert archive.read('scenario-synthetic/scripts/check_request.py').decode() == source['scripts/check_request.py']


def test_untouched_templates_and_unwritten_requested_files_cannot_complete_a_task():
    value = document()
    value['required_paths'] = required_paths('Create scripts/check_request.py and skills/check-request/SKILL.md')
    assert len(validate_project(value)) == 5
    value['authored_paths'] = list(files())
    issues = validate_project(value)
    assert len(issues) == 2
    assert any('scripts/check_request.py' in issue for issue in issues)


def test_new_client_contract_rejects_a_valid_schema_example_missing_required_idempotency():
    value = contract()
    value['capabilities'][0]['idempotency_required'] = True
    source = files()
    assert not validate_files(source, value)  # Immutable legacy package semantics.
    value['client_contract'] = client_contract()
    assert any('idempotency_key' in issue for issue in validate_files(source, value))
    source['examples/invoke.py'] = source['examples/invoke.py'].replace('{"amount": 1})', '{"amount": 1}, idempotency_key="synthetic-current-request")')
    assert not validate_files(source, value)
    assert 'invocation_id' in str(value['client_contract']['receipt_schema'])


def test_custom_plugin_guidance_cannot_invent_an_unsupported_client_environment_variable():
    value = contract()
    value['client_contract'] = client_contract()
    source = files()
    source['README.md'] += 'Set SCENARIO_IDEMPOTENCY_KEY to configure calls.\n'
    assert any('SCENARIO_IDEMPOTENCY_KEY' in issue for issue in validate_files(source, value))


def test_invalid_json_lines_triggers_bounded_safe_repair_without_exposing_model_output():
    state = document()
    calls = []
    def generate(current, instruction):
        calls.append(instruction)
        if len(calls) == 1:
            CodingStep.model_validate_json('private malformed output')
        yield CodingStep(kind='file', path='README.md', content=files()['README.md'])
    def checkpoint(step=None, *, finish=False):
        nonlocal state
        if step:
            state = apply_step(state, step, 'run')
        if finish:
            finish_document(state)
        return deepcopy(state)
    code_and_repair(deepcopy(state), 'Build plugin', generate=generate, checkpoint=checkpoint, step_factory=CodingStep)
    assert len(calls) == 2
    assert 'JSON Lines' in calls[1]
    assert 'private malformed output' not in str(state)
    assert state['phase'] == 'ready_for_review'


def test_native_coding_tools_persist_files_and_keep_private_response_text_out_of_workspace():
    steps = list(parse_tool_steps([
        {'type': 'token', 'content': 'private response text'},
        {'type': 'tool_calls', 'tool_calls': [{'function': {'name': 'submit_plugin_step',
         'arguments': {'kind': 'file', 'path': 'scripts/check_request.py', 'content': 'complete source'}}}]}]))
    assert len(steps) == 1
    assert steps[0].path == 'scripts/check_request.py'
    assert steps[0].content == 'complete source'
    with pytest.raises(ValidationError):
        list(parse_tool_steps([{'type': 'tool_calls', 'tool_calls': [{'function': {'name': 'submit_plugin_step',
            'arguments': {'kind': 'file', 'path': 'README.md', 'content': 'text', 'private_thinking': 'forbidden'}}}]}]))


def test_native_agent_continues_after_plan_and_receives_persistent_tool_acknowledgements():
    conversations = []
    def stream(messages):
        conversations.append(deepcopy(messages))
        step = {'kind': 'plan', 'message': 'Read contracts'} if len(conversations) == 1 else {'kind': 'file', 'path': 'README.md', 'content': 'complete source'} if len(conversations) == 2 else {'kind': 'summary', 'message': 'Ready for checks'}
        yield {'type': 'tool_calls', 'tool_calls': [{'id': f'call-{len(conversations)}', 'function': {'name': 'submit_plugin_step', 'arguments': step}}]}
    steps = list(native_coding_steps([{'role': 'user', 'content': 'Build plugin'}], stream=stream, parse=parse_tool_steps))
    assert [step.kind for step in steps] == ['plan', 'file', 'summary']
    assert conversations[1][-1]['role'] == 'tool'
    assert conversations[1][-1]['tool_call_id'] == 'call-1'
    assert '"persisted": true' in conversations[1][-1]['content']


@pytest.mark.parametrize('path', ['../escape.py', 'server.py', 'references/scenario.json', 'scripts/../escape.py', 'scripts/run.py.exe', 'skills/a/../../README.md'])
def test_project_paths_cannot_replace_trusted_adapter_or_pinned_contract(path):
    with pytest.raises(ValidationError):
        CodingFile(path=path, content='changed')


@pytest.mark.parametrize('source', ['import os\n', 'eval("1")', 'from server import remote\n',
                                 'while True: pass', 'from server import invoke_scenario_capability\nasync def run(inputs):\n    return await invoke_scenario_capability("function", "foreign", inputs)'])
def test_custom_client_code_rejects_environment_execution_and_unselected_capabilities(source):
    project = files()
    project['scripts/custom.py'] = source
    assert validate_files(project, contract())


def test_draft_start_does_not_require_or_invent_business_acceptance(monkeypatch):
    request = PluginCodingDraftCreate(expected_revision=1, request_id='synthetic-request', llm_config_id='model',
        instruction='Create a task plugin', capabilities=[{'kind': 'function', 'key': 'check'}])
    manifest = contract()
    manifest['acceptance'] = {'kind': 'pending_human_business_acceptance', 'cases': []}
    monkeypatch.setattr(context, 'release_context', lambda *args: (None, deepcopy(manifest)))
    _, candidate = context.prepare_authoring(None, 'release', request)
    assert candidate['acceptance']['cases'] == []
    assert candidate['capabilities'] == manifest['capabilities']
    request.capabilities[0].key = 'foreign'
    with pytest.raises(HTTPException) as error:
        context.prepare_authoring(None, 'release', request)
    assert error.value.status_code == 409


def test_valid_code_cannot_be_reviewed_without_business_acceptance(monkeypatch):
    value = document()
    value['phase'] = 'ready_for_review'
    root = SimpleNamespace(proposal=value)
    monkeypatch.setattr(review, 'owned_root', lambda *args, **kwargs: root)
    with pytest.raises(HTTPException) as error:
        review.review_workspace(None, 'workspace', PluginCodingReview(expected_revision=1,
            files_hash=files_hash(value['files']), confirmed_code_review=True))
    assert error.value.status_code == 409
    assert '业务验收' in error.value.detail
    assert root.proposal == value


def test_failed_model_round_cannot_be_reviewed_even_if_retained_files_validate(monkeypatch):
    value = document()
    value['phase'] = 'validation_failed'
    root = SimpleNamespace(proposal=value)
    monkeypatch.setattr(review, 'owned_root', lambda *args, **kwargs: root)
    with pytest.raises(HTTPException) as error:
        review.review_workspace(None, 'workspace', PluginCodingReview(expected_revision=1,
            files_hash=files_hash(value['files']), confirmed_code_review=True))
    assert '本轮编码未完成' in error.value.detail


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_model_receives_real_validation_feedback_and_repair_is_bounded(repair_succeeds):
    state = document()
    state['files']['examples/invoke.py'] = state['files']['examples/invoke.py'].replace('{"amount": 1}', '{}')
    prompts = []
    def generate(current, instruction):
        prompts.append((instruction, current.get('validation', [])))
        content = files()['examples/invoke.py'] if repair_succeeds and len(prompts) > 1 else state['files']['examples/invoke.py']
        yield CodingStep(kind='file', path='examples/invoke.py', content=content)
    def checkpoint(step=None, *, finish=False):
        nonlocal state
        if step:
            state = apply_step(state, step, 'run')
        if finish:
            finish_document(state)
        return deepcopy(state)
    code_and_repair(deepcopy(state), 'Complete original requirement', generate=generate, checkpoint=checkpoint, step_factory=CodingStep)
    assert len(prompts) == (2 if repair_succeeds else 3)
    assert 'amount' in prompts[1][0]
    assert state['phase'] == ('ready_for_review' if repair_succeeds else 'validation_failed')
    assert bool(state['validation']) != repair_succeeds
