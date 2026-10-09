from copy import deepcopy
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.plugin_coding_schemas import PluginCodingExport, PluginCodingUpdate
from app.services.plugin_coding_worker import CodingStep, apply_step, finish_document, parse_steps
from app.services.plugin_coding_validation import EDITABLE_PATHS, files_hash, validate_files
from app.services.plugin_coding_distribution import marketplace_artifact
from app.services.scenario_package_artifact import build_artifact
from app.services.plugin_coding_identity import adapter_hash, assert_adapter_identity, assert_same_version, snapshot_id
from app.services.plugin_coding_contract import authoring_contract


def manifest():
    return {'package_name': 'scenario-synthetic', 'scenario': {'id': 'scenario'},
            'deployment': {'release_id': 'release', 'definition_hash': 'a' * 64},
            'capabilities': [{'kind': 'function', 'key': 'check'}]}


def sources():
    return {'skills/run-scenario/SKILL.md': '---\nname: run-scenario\ndescription: Run the reviewed scenario\n---\nUse invoke_scenario_capability and get_scenario_receipt.\n',
            'README.md': 'Configure SCENARIO_API_KEY outside this package. Run python -m examples.invoke.\n',
            'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\n\nasync def main():\n    result = await invoke_scenario_capability("function", "check", {"amount": 1})\n    print(result)\n\nif __name__ == "__main__":\n    asyncio.run(main())\n'}


def test_chunked_coding_output_restores_file_content_without_executing_it():
    raw = json.dumps({'kind': 'file', 'path': 'README.md', 'content': 'line1\nline2'})
    steps = list(parse_steps([{'type': 'token', 'content': raw[:14]}, {'type': 'token', 'content': raw[14:]}]))
    assert steps[0].content == 'line1\nline2'
    with pytest.raises(ValidationError):
        list(parse_steps([{'type': 'token', 'content': '{"kind":"plan","private_thinking":"no"}'}]))


def test_failed_model_output_is_reported_even_when_retained_files_are_valid():
    document = {'files': sources(), 'coding_contract': manifest(), 'revision': 3,
                'events': [], 'active_run_id': 'run', 'phase': 'generating'}
    try:
        list(parse_steps([{'type': 'token', 'content': 'invalid-private-model-output'}]))
    except ValidationError as error:
        finish_document(document, error=error)
    assert document['phase'] == 'validation_failed'
    assert any('JSON Lines' in issue for issue in document['validation'])
    assert 'invalid-private-model-output' not in str(document)


def test_transport_failure_does_not_expose_supplier_error_or_claim_completion():
    document = {'files': sources(), 'coding_contract': manifest(), 'revision': 3,
                'events': [], 'active_run_id': 'run', 'phase': 'generating'}
    finish_document(document, error=RuntimeError('private supplier error'))
    assert document['phase'] == 'validation_failed'
    assert document['validation']
    assert 'private supplier error' not in str(document)


def test_coding_parses_public_steps_after_chunked_provider_reasoning_envelope():
    line = json.dumps({'kind': 'file', 'path': 'README.md',
                       'content': 'Keep literal <think> example in this file.'})
    chunks = [{'type': 'token', 'content': part} for part in
              ['<thi', 'nk>private provider body', '</th', 'ink>\n', line]]
    steps = list(parse_steps(chunks))
    assert len(steps) == 1
    assert steps[0].content == 'Keep literal <think> example in this file.'


def test_coding_step_budget_also_bounds_final_unterminated_line():
    line = json.dumps({'kind': 'plan', 'message': 'public step'})
    with pytest.raises(ValueError, match='预算'):
        list(parse_steps([{'type': 'token', 'content': '\n'.join([line] * 21)}]))


def test_late_coding_round_cannot_overwrite_human_revision():
    document = {'active_run_id': 'new', 'phase': 'generating', 'revision': 5, 'files': sources(), 'previous': {}, 'events': []}
    original = deepcopy(document)
    with pytest.raises(ValueError, match='失效'):
        apply_step(document, CodingStep(kind='file', path='README.md', content='obsolete'), 'old')
    assert document == original
    updated = apply_step(document, CodingStep(kind='file', path='README.md', content='new'), 'new')
    assert updated['previous']['README.md'] == document['files']['README.md']
    assert updated['revision'] == 6
    assert document == original


def test_ai_cannot_edit_the_fixed_executor_or_leak_a_credential():
    document = {'active_run_id': 'run', 'phase': 'generating', 'revision': 1, 'files': sources(), 'previous': {}, 'events': []}
    for path, content in [('server.py', 'changed'), ('../escape.py', 'changed'), ('README.md', 'sk-synthetic_forbidden_credential')]:
        with pytest.raises(ValueError):
            apply_step(document, CodingStep(kind='file', path=path, content=content), 'run')


@pytest.mark.parametrize('code', ['import os\n', 'eval("x")', 'import asyncio\nasyncio.__dict__',
                                'from server import remote\n', 'while True: pass\n',
                                'from server import invoke_scenario_capability\ninvoke_scenario_capability("function", "foreign", {})'])
def test_example_rejects_system_execution_and_unselected_capabilities(code):
    files = sources()
    files['examples/invoke.py'] = code
    assert validate_files(files, manifest())


def test_skill_requires_closed_frontmatter_and_description():
    files = sources()
    files['skills/run-scenario/SKILL.md'] = '---\nname: run-scenario\ninvoke_scenario_capability read_scenario_receipt'
    assert validate_files(files, manifest())


def test_skill_uses_the_receipt_tool_actually_registered_by_the_adapter():
    assert not validate_files(sources(), manifest())
    files = sources()
    files['skills/run-scenario/SKILL.md'] = files['skills/run-scenario/SKILL.md'].replace('get_scenario_receipt', 'read_scenario_receipt')
    assert validate_files(files, manifest())


def test_example_requires_await_and_complete_typed_input_for_the_frozen_schema():
    contract = manifest()
    contract['capabilities'][0]['input_schema'] = {'type': 'object', 'properties': {'amount': {'type': 'integer'}},
                                                'required': ['amount'], 'additionalProperties': False}
    assert not validate_files(sources(), contract)
    for old, new in [('await invoke', 'invoke'), ('{"amount": 1}', '{}'), ('{"amount": 1}', '"placeholder"')]:
        files = sources()
        files['examples/invoke.py'] = files['examples/invoke.py'].replace(old, new)
        assert validate_files(files, contract)


def test_authoring_reads_complete_contract_from_exact_release_and_excludes_live_receipt_data(monkeypatch):
    calls = []
    contract = {**manifest()['capabilities'][0], 'definition_hash': 'a' * 64,
        'name': 'Check', 'description': 'Contract only', 'input_schema': {'type': 'object', 'required': ['amount']},
        'output_schema': {'type': 'boolean'}, 'side_effect': False, 'requires_confirmation': False,
        'idempotency_required': False, 'data_ports': [], 'runtime_input': 'must-not-copy'}
    monkeypatch.setattr('app.services.plugin_coding_contract.release_service._scenario_for_manage', lambda *a: ('scenario', None))
    definition = SimpleNamespace(scenario=SimpleNamespace(name='Released scenario', description='Released goal'),
        source='release', release_id='release', snapshot_id='snapshot', definition_hash='a' * 64,
        entities={}, relations={}, actions={}, rules={}, events={}, workflows={},
        functions={contract['key']: SimpleNamespace(id=contract['key'], name=contract['name'],
                                                   description=contract['description'], runtime_kind='threshold')})
    deployment = SimpleNamespace(definition=definition, definition_hash='a' * 64, release_id='release', snapshot_id='snapshot')
    monkeypatch.setattr('app.services.plugin_coding_contract.capability_application_service.resolve_deployment',
                        lambda *args, **kwargs: (deployment, None))
    monkeypatch.setattr('app.services.plugin_coding_contract.release_service._snapshot_for_scenario',
                        lambda *args: SimpleNamespace(content={'scenario': vars(definition.scenario)}))
    def catalog(db, scenario, *, release_id, definition):
        calls.append((scenario, release_id))
        return [contract]
    monkeypatch.setattr('app.services.plugin_coding_contract.capability_application_service.list_capabilities', catalog)
    monkeypatch.setattr('app.services.plugin_coding_contract.capability_application_service._permission_allowed', lambda *args: True)
    result = authoring_contract(None, manifest())
    assert calls == [('scenario', 'release')]
    assert result['capabilities'][0]['input_schema']['required'] == ['amount']
    assert 'runtime_input' not in result['capabilities'][0]
    contract['definition_hash'] = 'b' * 64
    with pytest.raises(ValueError, match='身份'):
        authoring_contract(None, manifest())


def test_reviewed_source_is_exactly_packaged_and_marketplace_paths_install():
    assert not validate_files(sources(), manifest())
    plugin = build_artifact(manifest(), files=sources(), plugin_version='1.2.3')
    assert plugin == build_artifact(manifest(), files=sources(), plugin_version='1.2.3')
    market = marketplace_artifact(plugin, 'scenario-synthetic', '1.2.3')
    with zipfile.ZipFile(io.BytesIO(market)) as archive:
        root = 'scenario-synthetic-marketplace'
        index = json.loads(archive.read(f'{root}/.claude-plugin/marketplace.json'))
        entry = index['plugins'][0]
        path = entry['source'].removeprefix('./')
        descriptor = json.loads(archive.read(f'{root}/{path}/.claude-plugin/plugin.json'))
        assert entry['name'] == descriptor['name']
        assert descriptor['version'] == '1.2.3'
        assert archive.read(f'{root}/{path}/examples/invoke.py').decode() == sources()['examples/invoke.py']


def test_revision_and_review_inputs_are_closed_and_require_file_identity():
    with pytest.raises(ValidationError):
        PluginCodingUpdate(expected_revision=1, request_id='request-id', action='generate', instruction='fix')
    with pytest.raises(ValidationError):
        PluginCodingExport(expected_revision=1, files_hash=files_hash(sources()), confirmed_code_review=False)
    assert set(sources()) == EDITABLE_PATHS


def test_trusted_template_changes_cannot_rebuild_an_old_workspace_silently():
    assert_adapter_identity({'adapter_hash': adapter_hash()})
    with pytest.raises(HTTPException) as conflict:
        assert_adapter_identity({'adapter_hash': 'old-template'})
    assert conflict.value.status_code == 409


def test_plugin_version_identity_rejects_different_content_and_separates_tenants():
    original = SimpleNamespace(proposal={'artifact_hash': 'immutable'})
    assert_same_version(original, {'artifact_hash': 'immutable'})
    with pytest.raises(HTTPException) as conflict:
        assert_same_version(original, {'artifact_hash': 'changed'})
    assert conflict.value.status_code == 409
    assert snapshot_id('tenant', 'name', '1.0.0') != snapshot_id('other', 'name', '1.0.0')
    assert snapshot_id('tenant', 'name', '1.0.0') != snapshot_id('tenant', 'name', '1.0.1')


def test_plugin_coding_routes_reject_oversized_bodies_before_parsing():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.plugin_coding_schemas import MAX_CODING_BODY_BYTES
    client = TestClient(app)
    for path in (f'/api/scenario-releases/{"a" * 32}/plugin-workspaces',
                 f'/api/plugin-workspaces/{"a" * 32}/revisions',
                 f'/api/plugin-workspaces/{"a" * 32}/settings',
                 f'/api/plugin-workspaces/{"a" * 32}/artifact'):
        response = client.post(path, content=b'x' * (MAX_CODING_BODY_BYTES + 1))
        assert response.status_code == 413
