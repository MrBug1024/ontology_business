from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import zipfile

from pydantic import ValidationError
from fastapi import HTTPException
import pytest

from app.plugin_coding_schemas import CodingFile
from app.services.plugin_client_contract import client_contract
from app.services.plugin_source_policy import editable_path
from app.services.plugin_delivery_profile import delivery_profile
from app.services.plugin_delivery_artifact import build_artifact
from app.services.scenario_package_artifact import build_artifact as legacy_build_artifact


def manifest() -> dict:
    value = {
        'package_version': 'scenario-plugin.v1', 'package_name': 'scenario-synthetic',
        'host': 'claude_code', 'scenario': {'id': 'scenario', 'name': 'Synthetic goal'},
        'deployment': {'release_id': 'release', 'snapshot_id': 'snapshot', 'definition_hash': 'a' * 64},
        'capabilities': [{'kind': 'function', 'key': 'check', 'name': 'Check current input'}],
        'delivery_profile': delivery_profile(),
    }
    value['scenario_blueprint'] = {
        'version': 'scenario-capability-blueprint.v1', 'completeness': 'complete_authorized_projection',
        'scenario': {**value['scenario'], 'description': 'Evaluate the explicit current input'},
        'deployment': {**value['deployment'], 'definition_source': 'release'},
        'stages': [], 'ontology': {'objects': [], 'relations': []},
        'capabilities': [{
            'kind': 'function', 'key': 'check', 'name': 'Check current input', 'description': '',
            'selected': True, 'available': True, 'dependency': False, 'invocation_supported': True,
            'invocation_authorized': True, 'enabled': True, 'ready': True,
            'semantic': {'role': 'Compute on the platform', 'runtime_kind': 'expression',
                         'trigger_type': None, 'node_counts': [], 'requires_approval': False,
                         'object_keys': [], 'dependencies': [], 'input_bindings': [], 'output_node_keys': []},
        }],
        'coverage': {'selected': [{'kind': 'function', 'key': 'check'}],
                     'available': [{'kind': 'function', 'key': 'check'}],
                     'dependencies': [], 'unselected_available': []},
    }
    return value


def test_new_profile_package_contains_the_exact_read_only_blueprint_and_correct_credential_scopes():
    value = manifest()
    original = deepcopy(value)
    with zipfile.ZipFile(io.BytesIO(build_artifact(value))) as archive:
        path = 'scenario-synthetic/references/scenario-blueprint.json'
        assert path in archive.namelist()
        assert json.loads(archive.read(path)) == value['scenario_blueprint']
        readme = archive.read('scenario-synthetic/README.md').decode()
        assert 'capability:read' in readme and 'capability:invoke' in readme
        assert 'capabilities:read' not in readme and 'capabilities:invoke' not in readme
    assert value == original


def test_blueprint_reference_cannot_be_authored_or_requested_as_an_editable_file():
    path = 'references/scenario-blueprint.json'
    assert not editable_path(path)
    with pytest.raises(ValidationError):
        CodingFile(path=path, content='{"scenario": "invented goal"}')


def test_new_profile_installation_instructions_use_the_real_credential_scope_names():
    with zipfile.ZipFile(io.BytesIO(build_artifact(manifest()))) as archive:
        readme = archive.read('scenario-synthetic/README.md').decode()
        assert 'capability:read' in readme and 'capability:invoke' in readme
        assert 'capabilities:read' not in readme and 'capabilities:invoke' not in readme


def test_coding_receipt_contract_distinguishes_execution_trace_from_declared_business_output():
    guidance = client_contract()['workflow_completion']
    assert 'execution trace' in guidance
    assert 'receipt.output.result.steps' in guidance
    assert 'contract_validation' in guidance
    assert 'declared output nodes' in guidance
    assert 'indeterminate' in guidance
    assert 'receipt.status="succeeded" but receipt.output.status' in guidance
    assert 'same invocation_id' in guidance and 'bounded wait' in guidance
    assert 'do not submit another workflow' in guidance


def test_new_profile_archive_is_reproducible_and_every_generated_reference_is_checksummed():
    package = build_artifact(manifest())
    assert package == build_artifact(manifest())
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        checks = json.loads(archive.read('scenario-synthetic/checksums.json'))
        assert 'references/scenario-blueprint.json' in checks
        for path, digest in checks.items():
            assert hashlib.sha256(archive.read('scenario-synthetic/' + path)).hexdigest() == digest
        skill = archive.read('scenario-synthetic/skills/run-scenario/SKILL.md').decode()
        assert 'client_contract.workflow_completion' in skill
        assert 'semantic.input_bindings' in skill and 'semantic.output_node_keys' in skill
        assert 'contract_validation="passed"' in skill and 'step.result' in skill


@pytest.mark.parametrize('field', ['release_id', 'snapshot_id', 'definition_hash'])
def test_new_profile_rejects_a_blueprint_from_another_frozen_release(field):
    value = manifest()
    value['scenario_blueprint']['deployment'][field] = 'b' * 64 if field == 'definition_hash' else 'another'
    with pytest.raises(ValueError, match='身份'):
        build_artifact(value)


@pytest.mark.parametrize('change', ['scenario', 'selected', 'extra', 'missing', 'duplicate'])
def test_new_profile_rejects_untrusted_or_inconsistent_blueprint_content(change):
    value = manifest()
    blueprint = value['scenario_blueprint']
    if change == 'scenario':
        blueprint['scenario']['id'] = 'another-scenario'
    elif change == 'selected':
        blueprint['coverage']['selected'][0]['key'] = 'unselected'
    elif change == 'extra':
        blueprint['runtime_inputs'] = {'customer_record': 'must-not-package'}
    elif change == 'duplicate':
        blueprint['capabilities'].append(deepcopy(blueprint['capabilities'][0]))
    else:
        value.pop('scenario_blueprint')
    with pytest.raises(ValueError):
        build_artifact(value)


def test_human_readme_is_packaged_exactly_and_generated_reference_cannot_be_overlaid():
    from test_plugin_coding import sources
    from app.services.plugin_coding_source import project_files
    from app.services.plugin_coding_validation import validate_files
    value = manifest()
    source = sources()
    source['README.md'] += 'Reviewed installation advice with capability:read and capability:invoke.\n'
    with zipfile.ZipFile(io.BytesIO(build_artifact(value, files=source))) as archive:
        assert archive.read('scenario-synthetic/README.md').decode() == source['README.md']
    source['references/scenario-blueprint.json'] = '{"scenario": "untrusted goal"}'
    assert validate_files(source, value)
    with pytest.raises(ValueError):
        build_artifact(value, files=source)
    projected = {item['path']: item for item in project_files({'manifest': value, 'files': source, 'plugin_version': '1.0.0'})}
    reference = projected['references/scenario-blueprint.json']
    assert not reference['editable']
    assert json.loads(reference['content']) == value['scenario_blueprint']


def test_legacy_artifact_bytes_and_adapter_identity_remain_exact_even_with_an_old_custom_reference():
    from pathlib import Path
    from app.services.capability_contracts import canonical_hash
    from app.services.plugin_coding_identity import adapter_hash, assert_adapter_identity
    from app.services.scenario_package_artifact import TEMPLATE_ROOT
    from test_plugin_coding import sources
    value = manifest()
    value.pop('delivery_profile')
    value.pop('scenario_blueprint')
    source = sources()
    source['references/scenario-blueprint.json'] = '{"legacy_note": "ordinary authored reference"}'
    assert build_artifact(value, files=source) == legacy_build_artifact(value, files=source)
    expected = canonical_hash({'server': (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8'),
        'builder': Path(TEMPLATE_ROOT.parents[1] / 'app/services/scenario_package_artifact.py').read_text(encoding='utf-8')},
        domain='scenario-plugin-trusted-adapter-v1')
    assert adapter_hash() == expected
    assert_adapter_identity({'manifest': value, 'adapter_hash': expected})


def test_profile_adapter_identity_covers_new_delivery_code_without_invalidating_legacy(monkeypatch):
    from pathlib import Path
    from app.services.plugin_coding_identity import adapter_hash, assert_adapter_identity
    value = manifest()
    legacy = adapter_hash()
    pinned = adapter_hash(value['delivery_profile'])
    assert pinned != legacy
    read_text = Path.read_text
    def changed(path, *args, **kwargs):
        source = read_text(path, *args, **kwargs)
        return source + '\n# Changed trusted reference policy\n' if path.name == 'plugin_delivery_reference.py' else source
    monkeypatch.setattr(Path, 'read_text', changed)
    assert adapter_hash() == legacy
    assert_adapter_identity({'manifest': {}, 'adapter_hash': legacy})
    with pytest.raises(HTTPException) as conflict:
        assert_adapter_identity({'manifest': value, 'adapter_hash': pinned})
    assert conflict.value.status_code == 409


@pytest.mark.parametrize('profile', [None, {}, {'version': 'unsupported-profile'}])
def test_unknown_or_missing_profile_identity_never_falls_back_to_legacy(profile):
    from app.services.plugin_coding_identity import adapter_hash, assert_adapter_identity
    value = manifest()
    value['delivery_profile'] = profile
    with pytest.raises(ValueError):
        build_artifact(value)
    with pytest.raises(HTTPException) as conflict:
        assert_adapter_identity({'manifest': value, 'adapter_hash': adapter_hash()})
    assert conflict.value.status_code == 409


def test_classic_new_package_uses_the_same_server_contract_without_changing_acceptance_gates(monkeypatch):
    from app.services import scenario_package_service as packages, plugin_coding_contract
    from types import SimpleNamespace
    value = manifest()
    original = deepcopy(value)
    for field in ('delivery_profile', 'scenario_blueprint'):
        value.pop(field)
    contract = {**original, 'scenario': original['scenario_blueprint']['scenario'], 'client_contract': client_contract()}
    calls = []
    monkeypatch.setattr(packages, 'prepare_package', lambda *args: (calls.append('acceptance') or 'scenario-synthetic', value))
    monkeypatch.setattr(plugin_coding_contract, 'authoring_contract', lambda *args: calls.append('contract') or contract)
    monkeypatch.setattr(packages, 'check_release_state', lambda *args: calls.append('release_state'))
    _, package = packages.build_package(None, 'release', SimpleNamespace(expected_revision=1))
    assert calls == ['acceptance', 'contract', 'release_state']
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert json.loads(archive.read('scenario-synthetic/references/scenario-blueprint.json')) == original['scenario_blueprint']
        readme = archive.read('scenario-synthetic/README.md').decode()
        assert 'capability:read' in readme and 'capabilities:read' not in readme


def test_review_exports_the_reference_shown_in_the_workspace_and_retains_it_in_the_snapshot(monkeypatch):
    from types import SimpleNamespace
    from app.plugin_coding_schemas import PluginCodingReview
    from app.services import plugin_coding_export as review
    from app.services.plugin_coding_identity import adapter_hash
    from app.services.plugin_coding_validation import files_hash
    from test_plugin_coding import sources
    value = manifest()
    current = deepcopy(value)
    # Current execution permissions may change between authoring and discussion;
    # the protected reference shown to the reviewer is the original projection.
    current['scenario_blueprint']['capabilities'][0]['invocation_authorized'] = False
    source = sources()
    document = {
        'revision': 1, 'files': source, 'manifest': value, 'coding_contract': current,
        'release_id': 'release', 'plugin_version': '1.0.0', 'phase': 'ready_for_review',
        'adapter_hash': adapter_hash(value['delivery_profile']), 'events': [],
        'acceptance_request': {'expected_revision': 1, 'capabilities': [{'kind': 'function', 'key': 'check'}],
            'confirmed_business_acceptance': True, 'acceptance_cases': [
                {'kind': 'function', 'key': 'check', 'role': role, 'invocation_id': token * 32}
                for role, token in [('success', 'a'), ('boundary', 'b'), ('failure', 'c')]]},
    }
    row = SimpleNamespace(thread_id='workspace', proposal=document)
    base_manifest = deepcopy(value)
    base_manifest.pop('delivery_profile')
    base_manifest.pop('scenario_blueprint')
    monkeypatch.setattr(review, 'owned_root', lambda *args, **kwargs: row)
    monkeypatch.setattr(review, 'prepare_package', lambda *args: ('scenario-synthetic', deepcopy(base_manifest)))
    monkeypatch.setattr(review, 'check_release_state', lambda *args: None)
    monkeypatch.setattr(review.permission_service, 'require_principal',
                        lambda *args: SimpleNamespace(tenant_id='tenant', user_id='user'))
    saved = []
    db = SimpleNamespace(get=lambda *args: None, add=saved.append, commit=lambda: None)
    review.review_workspace(db, 'workspace', PluginCodingReview(expected_revision=1,
        files_hash=files_hash(source), confirmed_code_review=True))
    snapshot = saved[0].proposal
    assert snapshot['manifest']['scenario_blueprint'] == value['scenario_blueprint']
    assert snapshot['manifest']['scenario_blueprint'] != current['scenario_blueprint']
    assert snapshot['adapter_hash'] == adapter_hash(value['delivery_profile'])
    package = build_artifact(snapshot['manifest'], files=snapshot['files'], plugin_version=snapshot['plugin_version'])
    assert hashlib.sha256(package).hexdigest() == snapshot['artifact_hash']
    document['manifest']['scenario_blueprint']['scenario']['description'] = 'Later mutable workspace text'
    assert snapshot['manifest']['scenario_blueprint']['scenario']['description'] != 'Later mutable workspace text'


def test_generated_reference_is_not_an_authored_task_requirement_and_legacy_project_does_not_gain_new_gates():
    from app.services.plugin_project_validation import required_paths, validate_project
    from test_plugin_coding import sources
    assert 'references/scenario-blueprint.json' not in required_paths('Read references/scenario-blueprint.json')
    legacy = manifest()
    legacy.pop('delivery_profile')
    legacy.pop('scenario_blueprint')
    source = sources()
    source['references/scenario-blueprint.json'] = '{"legacy_note": "authored before this profile"}'
    assert validate_project({'files': source, 'coding_contract': manifest(), 'manifest': legacy}) == []


def test_authoring_methods_use_portable_skill_metadata_and_distinguish_the_platform_profile():
    from pathlib import Path
    import yaml
    from app.services.plugin_skill_contract import validate_skill
    root = Path(__file__).resolve().parents[1] / 'skills'
    for name in ('plugin-authoring', 'plugin-contract-review'):
        source = (root / name / 'SKILL.md').read_text(encoding='utf-8')
        metadata = yaml.safe_load(source.split('---', 2)[1])
        assert set(metadata).issubset({'name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools'})
        assert metadata['metadata']['version'] == '1.0.0'
        assert validate_skill(f'skills/{name}/SKILL.md', source) == []
        assert 'delivery_profile' in source and 'scenario_blueprint' in source


def test_classic_package_contract_failure_uses_the_existing_safe_conflict_protocol(monkeypatch):
    from app.routers.scenario_packages import export_plugin
    from app.services import scenario_package_service as packages, plugin_coding_contract
    from test_scenario_plugin import request
    value = manifest()
    monkeypatch.setattr(packages, 'prepare_package', lambda *args: ('scenario-synthetic', value))
    def unavailable(*args):
        raise ValueError('synthetic private diagnostics must not be exposed')
    monkeypatch.setattr(plugin_coding_contract, 'authoring_contract', unavailable)
    with pytest.raises(HTTPException) as conflict:
        export_plugin(request(), 'a' * 32, None)
    assert conflict.value.status_code == 409
    assert conflict.value.detail['code'] == 'scenario_package_blocked'
    assert 'private diagnostics' not in str(conflict.value.detail)
