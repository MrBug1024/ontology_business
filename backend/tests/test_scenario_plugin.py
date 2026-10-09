from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import asyncio

import pytest

from app.scenario_package_schemas import ScenarioPackageBuild
from app.services.scenario_package_acceptance import PackageValidationError, validate_acceptance
from app.services.scenario_package_artifact import build_artifact, TEMPLATE_ROOT
from app.services.scenario_package_service import require_finished_workflows


def request(kind='function'):
    return ScenarioPackageBuild(expected_revision=1, confirmed_business_acceptance=True,
        capabilities=[{'kind': kind, 'key': 'check'}], acceptance_cases=[
            {'kind': kind, 'key': 'check', 'role': role, 'invocation_id': role}
            for role in ['success', 'boundary', 'failure']])


def receipts(kind='function'):
    return {role: SimpleNamespace(release_id='release', definition_hash='a' * 64,
        capability_kind=kind, capability_key='check', status='succeeded', scenario_id='scenario',
        result_document={'output': {'workflow_run_id': 'run'}}) for role in ['success', 'boundary', 'failure']}


def document():
    return {'package_version': 'scenario-plugin.v1', 'package_name': 'scenario-synthetic',
        'deployment': {'release_id': 'release', 'definition_hash': 'a' * 64},
        'scenario': {'id': 'scenario', 'name': 'A business scenario'},
        'capabilities': [{'kind': 'function', 'key': 'check', 'name': 'Threshold'}]}


def test_package_requires_real_distinct_cases_and_pinned_ready_capabilities():
    rows = receipts()
    capabilities = [{'kind': 'function', 'key': 'check', 'ready': True}]
    assert validate_acceptance(capabilities, rows, request(), release_id='release', definition_hash='a' * 64) == capabilities
    for field, value in [('release_id', 'other'), ('definition_hash', 'b' * 64), ('status', 'running')]:
        altered = deepcopy(rows)
        setattr(altered['success'], field, value)
        with pytest.raises(PackageValidationError):
            validate_acceptance(capabilities, altered, request(), release_id='release', definition_hash='a' * 64)
    with pytest.raises(PackageValidationError):
        validate_acceptance([{**capabilities[0], 'ready': False}], rows, request(), release_id='release', definition_hash='a' * 64)
    with pytest.raises(ValueError):
        ScenarioPackageBuild.model_validate({**request().model_dump(), 'acceptance_cases': request().model_dump()['acceptance_cases'][:2]})


def test_enqueued_workflow_is_not_completed_business_acceptance():
    run = SimpleNamespace(release_id='release', definition_hash='a' * 64, scenario_id='scenario', workflow_id='check', status='queued')
    with pytest.raises(PackageValidationError, match='仍在执行'):
        require_finished_workflows(receipts('workflow'), {'run': run}, request('workflow'))
    run.status = 'succeeded'
    require_finished_workflows(receipts('workflow'), {'run': run}, request('workflow'))


def test_plugin_zip_is_reproducible_contains_checksums_and_no_runtime_values():
    artifact = build_artifact(document())
    assert artifact == build_artifact(document())
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        entries = archive.namelist()
        assert 'scenario-synthetic/.claude-plugin/plugin.json' in entries
        assert all('/../' not in path for path in entries)
        checks = json.loads(archive.read('scenario-synthetic/checksums.json'))
        for path, digest in checks.items():
            assert hashlib.sha256(archive.read(f'scenario-synthetic/{path}')).hexdigest() == digest
        config = json.loads(archive.read('scenario-synthetic/.mcp.json'))
        assert config['mcpServers']['scenario']['env']['SCENARIO_API_KEY'] == '${SCENARIO_API_KEY}'
        assert 'inputs' not in json.loads(archive.read('scenario-synthetic/references/scenario.json'))


def adapter():
    spec = importlib.util.spec_from_file_location('trusted_test_scenario_adapter', TEMPLATE_ROOT / 'server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_adapter_pins_every_invocation_and_rejects_other_capabilities(monkeypatch):
    module = adapter()
    monkeypatch.setattr(module, 'manifest', document)
    calls = []
    async def remote(name, arguments):
        calls.append((name, arguments))
        return {'definition_hash': 'a' * 64, 'capability': {'kind': 'function', 'key': 'check'}, 'status': 'succeeded'}
    monkeypatch.setattr(module, 'remote', remote)
    asyncio.run(module.invoke_scenario_capability('function', 'check', {'value': 10}))
    assert calls[0][1]['release_id'] == 'release'
    assert calls[0][1]['expected_definition_hash'] == 'a' * 64
    with pytest.raises(ValueError, match='not included'):
        asyncio.run(module.invoke_scenario_capability('function', 'other', {}))
    assert len(calls) == 1
    with pytest.raises(ValueError, match='pinned'):
        module.checked_receipt(document(), {'definition_hash': 'b' * 64})


def test_plugin_reply_cannot_target_an_unrelated_or_stale_interaction():
    module = adapter()
    receipt = {'delivery': {'interactions': [{'kind': 'workflow_approval', 'id': 'approval', 'status': 'pending', 'revision': 2}]}}
    module.checked_interaction(receipt, kind='approval', interaction_id='approval', expected_revision=2)
    for identity, revision in [('other', 2), ('approval', 1)]:
        with pytest.raises(ValueError, match='revision changed'):
            module.checked_interaction(receipt, kind='approval', interaction_id=identity, expected_revision=revision)


def test_plugin_discovery_consumes_actual_platform_mcp_envelope(monkeypatch):
    from app import agent_mcp_server
    module = adapter()
    monkeypatch.setattr(module, 'manifest', document)
    capability = {**document()['capabilities'][0], 'definition_hash': 'a' * 64}
    monkeypatch.setattr(agent_mcp_server, '_capability_identity', lambda: None)
    monkeypatch.setattr(agent_mcp_server.capability_mcp_service, 'list_capabilities', lambda *a, **kw: [capability])
    async def info(_):
        return None
    async def remote(name, arguments):
        assert name == 'list_capabilities'
        return await agent_mcp_server.list_capabilities(SimpleNamespace(info=info), **arguments)
    monkeypatch.setattr(module, 'remote', remote)
    assert asyncio.run(module.list_scenario_capabilities()) == [capability]


def test_plugin_confirmation_consumes_actual_pending_receipt_shape():
    module = adapter()
    # capability_delivery_service advertises confirmation without an item
    # status; the current receipt status establishes whether it is pending.
    receipt = {'status': 'awaiting_confirmation', 'delivery': {'interactions': [
        {'kind': 'capability_confirmation', 'id': 'preview', 'revision': 1}]}}
    module.checked_interaction(receipt, kind='confirmation', interaction_id='preview', expected_revision=1)
    receipt['status'] = 'succeeded'
    with pytest.raises(ValueError, match='revision changed'):
        module.checked_interaction(receipt, kind='confirmation', interaction_id='preview', expected_revision=1)
