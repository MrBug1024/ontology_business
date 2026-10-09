from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import zipfile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.plugin_coding_schemas import PluginCodingDraftCreate
from test_plugin_delivery_profile import manifest as claude_manifest
from test_plugin_coding import sources


def test_codex_authoring_request_accepts_explicit_supported_host_only():
    fields = dict(expected_revision=1, capabilities=[{'kind': 'function', 'key': 'check'}],
                  request_id='synthetic-request', llm_config_id='model', instruction='Build scenario client')
    assert PluginCodingDraftCreate(target='codex', **fields).target == 'codex'
    assert PluginCodingDraftCreate(**fields).target == 'claude_code'
    with pytest.raises(ValidationError):
        PluginCodingDraftCreate(target='unverified_host', **fields)


def test_codex_context_and_draft_share_host_specific_profile_and_package_identity(monkeypatch):
    from app.services import plugin_authoring_context as authoring
    from app.services import plugin_coding_contract as coding
    from test_plugin_coding_contract import contract_fixture
    manifest, _, _ = contract_fixture(monkeypatch)
    manifest.update(package_name='scenario-release', host='claude_code')
    monkeypatch.setattr(authoring, 'release_context', lambda *args: (None, deepcopy(manifest)))
    value = authoring.discover_context(None, 'release', 'codex')
    assert value['delivery_profile']['host']['key'] == 'codex'
    request = PluginCodingDraftCreate(target='codex', expected_revision=1,
        capabilities=[{'kind': 'function', 'key': 'check'}], request_id='synthetic-request',
        llm_config_id='model', instruction='Build scenario client')
    name, draft = authoring.prepare_authoring(None, 'release', request)
    assert name == 'scenario-release-codex' and draft['host'] == 'codex'
    assert coding.authoring_contract(None, draft)['delivery_profile'] == value['delivery_profile']


def test_context_http_query_rejects_unverified_host_and_preserves_default(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import plugin_coding as router
    from app.services import plugin_authoring_context as authoring
    from test_plugin_coding_contract import contract_fixture
    fixture, _, _ = contract_fixture(monkeypatch)
    fixture['deployment']['definition_source'] = 'release'
    monkeypatch.setattr(authoring, 'release_context', lambda *args: (None, deepcopy(fixture)))
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_tenant_db] = lambda: None
    with TestClient(app) as client:
        path = '/scenario-releases/' + 'a' * 32 + '/plugin-context'
        assert client.get(path + '?target=unverified').status_code == 422
        for query, host in [('', 'claude_code'), ('?target=codex', 'codex')]:
            result = client.get(path + query)
            assert result.status_code == 200
            assert result.json()['delivery_profile']['host']['key'] == host


@pytest.mark.parametrize('marker', ['${CLAUDE_PLUGIN_ROOT}', 'claude plugin install', 'codex plugin install'])
def test_codex_authored_sources_reject_wrong_host_variables_and_commands(marker):
    from app.services.plugin_coding_validation import validate_files
    candidate = sources()
    candidate['README.md'] += marker
    assert any('宿主' in issue for issue in validate_files(candidate, codex_manifest()))


def codex_manifest():
    from app.services.plugin_host_profile import delivery_profile
    value = claude_manifest()
    value.update(host='codex', package_name='scenario-synthetic-codex', delivery_profile=delivery_profile('codex'))
    return value


def test_codex_package_contains_host_manifest_and_trusted_client_without_credentials():
    from app.services.plugin_host_artifact import build_artifact
    value = codex_manifest()
    original = deepcopy(value)
    artifact = build_artifact(value)
    assert artifact == build_artifact(value)
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        root = value['package_name'] + '/'
        metadata = json.loads(archive.read(root + '.codex-plugin/plugin.json'))
        assert metadata['name'] == value['package_name']
        assert metadata['skills'] == './skills/' and metadata['mcpServers'] == './.mcp.json'
        assert metadata['interface']['displayName']
        config = json.loads(archive.read(root + '.mcp.json'))
        assert config['mcpServers']['scenario']['args'] == ['server.py']
        assert config['mcpServers']['scenario']['cwd'] == './'
        assert config['mcpServers']['scenario']['env'] == {}
        assert config['mcpServers']['scenario']['env_vars'] == ['SCENARIO_MCP_URL', 'SCENARIO_API_KEY']
        assert root + '.claude-plugin/plugin.json' not in archive.namelist()
        assert json.loads(archive.read(root + 'references/scenario-blueprint.json')) == value['scenario_blueprint']
        skill = archive.read(root + 'skills/run-scenario/SKILL.md').decode()
        assert 'CLAUDE_PLUGIN_ROOT' not in skill and 'get_scenario_receipt' in skill
        readme = archive.read(root + 'README.md').decode()
        assert 'codex plugin marketplace add' in readme
        assert 'capability:read' in readme and 'capability:invoke' in readme
        assert 'codex plugin install' not in readme
        checks = json.loads(archive.read(root + 'checksums.json'))
        assert all(hashlib.sha256(archive.read(root + path)).hexdigest() == digest for path, digest in checks.items())
    assert value == original


@pytest.mark.parametrize('change', ['host', 'profile', 'missing_profile', 'release'])
def test_codex_package_rejects_cross_host_or_unpinned_identity(change):
    from app.services.plugin_host_artifact import build_artifact
    value = codex_manifest()
    if change == 'host':
        value['host'] = 'claude_code'
    elif change == 'profile':
        value['delivery_profile']['host']['key'] = 'claude_code'
    elif change == 'missing_profile':
        value.pop('delivery_profile')
    else:
        value['scenario_blueprint']['deployment']['release_id'] = 'other'
    with pytest.raises(ValueError):
        build_artifact(value)


def test_host_dispatch_preserves_legacy_and_v1_claude_bytes_and_adapter_identity():
    from app.services.plugin_host_artifact import build_artifact
    from app.services.plugin_delivery_artifact import build_artifact as v1_builder
    from app.services.plugin_coding_identity import adapter_hash
    value = claude_manifest()
    assert build_artifact(value, files=sources()) == v1_builder(value, files=sources())
    assert adapter_hash() == 'bc7a56afefbb592a31539918a491413cb4df3fcb0fe657229724e3d3cf07ca38'
    assert adapter_hash(value['delivery_profile']) == '675964b7773ea3b70054ed6e9584cd2cc4247fbc401439c9f337664debc62a99'
    value.pop('delivery_profile')
    value.pop('scenario_blueprint')
    assert build_artifact(value, files=sources()) == v1_builder(value, files=sources())


def test_codex_restore_uses_exact_reviewed_bytes_and_detects_tampering():
    from app.services.plugin_host_artifact import build_artifact
    from app.services.plugin_artifact_catalog import restore_artifact
    from app.services.plugin_coding_identity import adapter_hash, assert_adapter_identity
    value = codex_manifest()
    candidate = sources()
    artifact = build_artifact(value, files=candidate)
    document = dict(manifest=value, files=candidate, plugin_version='1.0.0',
                    artifact_hash=hashlib.sha256(artifact).hexdigest(), adapter_hash=adapter_hash(value['delivery_profile']))
    assert restore_artifact(document) == artifact
    broken = deepcopy(document)
    broken['manifest']['host'] = 'claude_code'
    with pytest.raises(HTTPException):
        assert_adapter_identity(broken)
    document['files']['README.md'] += 'Changed after review'
    with pytest.raises(HTTPException, match='409'):
        restore_artifact(document)


def installer_module():
    from app.services.plugin_host_installation import installer_bytes
    module = ModuleType('synthetic_codex_installer')
    exec(compile(installer_bytes('codex'), '<trusted-codex-installer>', 'exec'), module.__dict__)
    return module


def codex_marketplace():
    from app.services.plugin_host_artifact import build_artifact
    from app.services.plugin_host_distribution import marketplace_artifact
    value = codex_manifest()
    return marketplace_artifact(build_artifact(value), value['package_name'], '1.0.0', host='codex')


def test_codex_marketplace_installer_checks_identity_and_keeps_source_exact(tmp_path):
    installer = installer_module()
    package = 'scenario-synthetic-codex'
    archive = tmp_path / 'marketplace.zip'
    archive.write_bytes(codex_marketplace())
    root = installer.extract_archive(archive, tmp_path / 'source', package + '-marketplace')
    plugin = installer.check_marketplace(root, package, package + '-marketplace')
    original = (plugin / '.mcp.json').read_bytes()
    local = installer.prepare_local_copy(root, tmp_path / 'installation', tmp_path / 'venv/python', package)
    assert (plugin / '.mcp.json').read_bytes() == original
    configured = json.loads((local / 'plugins' / package / '.mcp.json').read_text())
    assert configured['mcpServers']['scenario']['command'] == str((tmp_path / 'venv/python').resolve())
    assert configured['mcpServers']['scenario']['args'] == ['server.py']
    assert configured['mcpServers']['scenario']['cwd'] == './'
    (plugin / 'server.py').write_text('changed', encoding='utf-8')
    with pytest.raises(installer.InstallationError, match='校验'):
        installer.check_marketplace(root, package, package + '-marketplace')


def test_codex_installation_checks_supported_cli_and_never_runs_business(tmp_path, monkeypatch):
    installer = installer_module()
    package = 'scenario-synthetic-codex'
    data = codex_marketplace()
    monkeypatch.setattr(installer, 'download_archive', lambda url, digest, path, allow: path.write_bytes(data))
    monkeypatch.setattr(installer.venv.EnvBuilder, 'create', lambda self, path: None)
    commands = []
    monkeypatch.setattr(installer, 'run_checked', lambda arguments, **kwargs: commands.append(arguments))
    args = SimpleNamespace(marketplace_url='https://source.example.invalid/marketplace.zip',
        sha256=hashlib.sha256(data).hexdigest(), package_name=package,
        marketplace_name=package + '-marketplace', allow_local_http=False)
    target = installer.install_locked(args, tmp_path / 'installation', tmp_path, 'synthetic-codex')
    record = json.loads((target / 'installation.json').read_text())
    assert record['status'] == 'installed' and record['plugin_installed'] is True
    assert record['business_execution_verified'] is False
    assert commands[-2][:4] == ['synthetic-codex', 'plugin', 'marketplace', 'add']
    assert commands[-1][:3] == ['synthetic-codex', 'plugin', 'add']
    assert all('install' not in args[1:3] for args in commands if args[0] == 'synthetic-codex')
    assert 'SCENARIO_API_KEY' not in installer.PROCESS_ENVIRONMENT


def test_codex_failed_cli_install_never_records_completion(tmp_path, monkeypatch):
    import subprocess
    installer = installer_module()
    package = 'scenario-synthetic-codex'
    data = codex_marketplace()
    monkeypatch.setattr(installer, 'download_archive', lambda url, digest, path, allow: path.write_bytes(data))
    monkeypatch.setattr(installer.venv.EnvBuilder, 'create', lambda self, path: None)
    def fail_at_add(arguments, **kwargs):
        if arguments[:3] == ['synthetic-codex', 'plugin', 'add']:
            raise subprocess.CalledProcessError(1, arguments)
    monkeypatch.setattr(installer, 'run_checked', fail_at_add)
    args = SimpleNamespace(marketplace_url='https://source.example.invalid/marketplace.zip',
        sha256=hashlib.sha256(data).hexdigest(), package_name=package,
        marketplace_name=package + '-marketplace', allow_local_http=False)
    target = tmp_path / 'installation'
    with pytest.raises(subprocess.CalledProcessError):
        installer.install_locked(args, target, tmp_path, 'synthetic-codex')
    assert not (target / 'installation.json').exists()


def test_codex_publication_freezes_host_specific_source_and_installation_notes(monkeypatch):
    from app.services import plugin_publication_service as publication
    from app.services.plugin_host_artifact import build_artifact
    from app.services.plugin_coding_identity import adapter_hash
    from test_plugin_publication import context
    db, row, release, scenario, rows, added, locks, settings, request, _ = context(monkeypatch)
    value = codex_manifest()
    original = build_artifact(value, files=sources())
    row.proposal.update(manifest=value, files=sources(), adapter_hash=adapter_hash(value['delivery_profile']),
                        artifact_hash=hashlib.sha256(original).hexdigest())
    request = request.model_copy(update={'artifact_hash': row.proposal['artifact_hash']})
    result = publication.change_publication(db, row.id, request, settings)
    assert result.installation.host == 'codex'
    assert any('手动' in note for note in result.installation.configuration_notes)
    saved = rows[publication.AssistantMessage, result.publication_id].proposal
    assert "'plugin_installed': True" in saved['installer_source']
