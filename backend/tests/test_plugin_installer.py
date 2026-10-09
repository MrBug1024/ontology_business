import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

from app.services.plugin_coding_distribution import marketplace_artifact
from app.services.plugin_installation_commands import INSTALLER_PATH
from app.services.scenario_package_artifact import build_artifact
from test_plugin_coding import manifest, sources


spec = importlib.util.spec_from_file_location('trusted_scenario_installer', INSTALLER_PATH)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def archive_fixture():
    original = build_artifact(manifest(), files=sources())
    return marketplace_artifact(original, 'scenario-synthetic', '1.0.0')


def test_installer_checks_source_and_only_adjusts_local_python_runner(tmp_path):
    archive = tmp_path / 'marketplace.zip'
    archive.write_bytes(archive_fixture())
    root = installer.extract_archive(archive, tmp_path / 'extracted', 'scenario-synthetic-marketplace')
    plugin = installer.check_marketplace(root, 'scenario-synthetic', 'scenario-synthetic-marketplace')
    original = (plugin / '.mcp.json').read_bytes()
    interpreter = tmp_path / 'dedicated-venv' / 'python.exe'
    local = installer.prepare_local_copy(root, tmp_path / 'installation', interpreter, 'scenario-synthetic')
    assert (plugin / '.mcp.json').read_bytes() == original
    configured = json.loads((local / 'plugins/scenario-synthetic/.mcp.json').read_text(encoding='utf-8'))
    assert configured['mcpServers']['scenario']['command'] == str(interpreter.resolve())
    assert configured['mcpServers']['scenario']['env']['SCENARIO_API_KEY'] == '${SCENARIO_API_KEY}'


@pytest.mark.parametrize('unsafe_path', [
    '../outside.txt', 'scenario-synthetic-marketplace/../../outside.txt',
    'scenario-synthetic-marketplace/drive:bad', 'scenario-synthetic-marketplace/dir\\outside',
])
def test_installer_rejects_archive_path_escape_before_writing_outside_root(tmp_path, unsafe_path):
    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr(unsafe_path, 'untrusted synthetic file')
    if '\\' in unsafe_path:
        # Python's Windows ZipInfo normalizes names when writing. Preserve the
        # actual untrusted central/local filename bytes for this attack case.
        normal = unsafe_path.replace('\\', '/').encode()
        archive.write_bytes(archive.read_bytes().replace(normal, unsafe_path.encode()))
    with pytest.raises(installer.InstallationError, match='路径'):
        installer.extract_archive(archive, tmp_path / 'bounded', 'scenario-synthetic-marketplace')
    assert not (tmp_path / 'outside.txt').exists()


def test_installer_rejects_archive_symlinks_and_duplicate_entries(tmp_path):
    for kind in ('symlink', 'duplicate'):
        archive = tmp_path / f'{kind}.zip'
        info = zipfile.ZipInfo('scenario-synthetic-marketplace/file')
        if kind == 'symlink':
            info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr(info, 'synthetic')
            if kind == 'duplicate':
                with pytest.warns(UserWarning):
                    output.writestr(info, 'duplicate')
        with pytest.raises(installer.InstallationError, match='路径'):
            installer.extract_archive(archive, tmp_path / kind, 'scenario-synthetic-marketplace')


def test_installer_rejects_declared_extraction_limits(tmp_path, monkeypatch):
    archive = tmp_path / 'marketplace.zip'
    archive.write_bytes(archive_fixture())
    monkeypatch.setattr(installer, 'MAX_EXTRACTED_BYTES', 8)
    with pytest.raises(installer.InstallationError, match='大小'):
        installer.extract_archive(archive, tmp_path / 'bounded', 'scenario-synthetic-marketplace')


def test_installer_rejects_modified_plugin_and_wrong_marketplace_identity(tmp_path):
    archive = tmp_path / 'marketplace.zip'
    archive.write_bytes(archive_fixture())
    root = installer.extract_archive(archive, tmp_path / 'extracted', 'scenario-synthetic-marketplace')
    plugin = root / 'plugins/scenario-synthetic'
    with pytest.raises(installer.InstallationError, match='所选插件'):
        installer.check_marketplace(root, 'different-plugin', 'scenario-synthetic-marketplace')
    (plugin / 'README.md').write_text('tampered synthetic content', encoding='utf-8')
    with pytest.raises(installer.InstallationError, match='文件校验'):
        installer.check_marketplace(root, 'scenario-synthetic', 'scenario-synthetic-marketplace')


def test_installer_download_verifies_sha_and_enforces_actual_byte_limit(tmp_path, monkeypatch):
    data = archive_fixture()
    monkeypatch.setattr(installer, 'build_opener', lambda *args: type('Opener', (), {
        'open': lambda self, *a, **k: io.BytesIO(data)})())
    target = tmp_path / 'download.zip'
    installer.download_archive('https://source.example.invalid/marketplace.zip', hashlib.sha256(data).hexdigest(), target, False)
    assert target.read_bytes() == data
    with pytest.raises(installer.InstallationError, match='不匹配'):
        installer.download_archive('https://source.example.invalid/marketplace.zip', 'a' * 64, target, False)
    monkeypatch.setattr(installer, 'MAX_ARCHIVE_BYTES', 1)
    with pytest.raises(installer.InstallationError, match='大小'):
        installer.download_archive('https://source.example.invalid/marketplace.zip', hashlib.sha256(data).hexdigest(), target, False)


@pytest.mark.parametrize('url,allowed', [
    ('http://remote.example.invalid/install.zip', True), ('http://127.0.0.1/install.zip', False),
    ('https://user:synthetic@source.example.invalid/install.zip', False),
    ('https://source.example.invalid/install.zip?key=synthetic', False),
])
def test_installer_rejects_credential_urls_and_unapproved_http(url, allowed):
    with pytest.raises(installer.InstallationError):
        installer.checked_url(url, allowed)


def test_installer_disables_redirects_and_does_not_forward_business_credentials():
    assert installer.NoRedirects().redirect_request(None, None, 302, None, None, 'https://other.example.invalid') is None
    assert 'SCENARIO_API_KEY' not in installer.PROCESS_ENVIRONMENT
    assert 'OPENAI_API_KEY' not in installer.PROCESS_ENVIRONMENT


def test_installer_retry_rebuilds_tampered_server_and_skill_from_verified_source(tmp_path):
    archive = tmp_path / 'marketplace.zip'
    archive.write_bytes(archive_fixture())
    root = installer.extract_archive(archive, tmp_path / 'extracted', 'scenario-synthetic-marketplace')
    installer.check_marketplace(root, 'scenario-synthetic', 'scenario-synthetic-marketplace')
    target = tmp_path / 'installation'
    interpreter = tmp_path / 'venv' / 'python'
    local = installer.prepare_local_copy(root, target, interpreter, 'scenario-synthetic')
    plugin = local / 'plugins/scenario-synthetic'
    for path in ('server.py', 'skills/run-scenario/SKILL.md'):
        (plugin / path).write_text('tampered synthetic content', encoding='utf-8')
    (plugin / 'unexpected-hook.py').write_text('untrusted extra', encoding='utf-8')
    rebuilt = installer.prepare_local_copy(root, target, interpreter, 'scenario-synthetic')
    for path in ('server.py', 'skills/run-scenario/SKILL.md'):
        assert (rebuilt / 'plugins/scenario-synthetic' / path).read_bytes() == (root / 'plugins/scenario-synthetic' / path).read_bytes()
    assert not (rebuilt / 'plugins/scenario-synthetic/unexpected-hook.py').exists()


def test_installer_honors_task_specific_install_root_without_redirecting_user_home(tmp_path, monkeypatch):
    from types import SimpleNamespace
    destination = tmp_path / 'isolated-install'
    monkeypatch.setenv('SCENARIO_PLUGIN_INSTALL_ROOT', str(destination))
    monkeypatch.setattr(installer.shutil, 'which', lambda *args: 'synthetic-claude')
    monkeypatch.setattr(installer, 'install_locked', lambda args, target, base, claude: base)
    args = SimpleNamespace(package_name='scenario-synthetic', marketplace_name='scenario-synthetic-marketplace',
                           sha256='a' * 64, install_root=None)
    assert installer.install(args) == destination.resolve()
    explicit = tmp_path / 'explicit-install'
    args.install_root = str(explicit)
    assert installer.install(args) == explicit.resolve()


def test_installer_rejects_linked_install_target_before_provisioning(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(installer.shutil, 'which', lambda *args: 'synthetic-claude')
    target_name = f"scenario-synthetic-{'a' * 16}"
    monkeypatch.setattr(Path, 'is_junction', lambda path: path.name == target_name)
    args = SimpleNamespace(package_name='scenario-synthetic', marketplace_name='scenario-synthetic-marketplace',
                           sha256='a' * 64, install_root=str(tmp_path))
    with pytest.raises(installer.InstallationError, match='指定安装根或包含链接'):
        installer.install(args)


def test_installer_rejects_virtual_environment_resolving_outside_install_target(tmp_path, monkeypatch):
    from types import SimpleNamespace
    data = archive_fixture()
    monkeypatch.setattr(installer, 'download_archive', lambda url, digest, target, allow: target.write_bytes(data))
    actual_resolve = Path.resolve
    outside = tmp_path / 'external-interpreter'
    monkeypatch.setattr(Path, 'resolve', lambda path, *args, **kwargs:
        outside if path.name == 'venv' else actual_resolve(path, *args, **kwargs))
    args = SimpleNamespace(marketplace_url='https://source.example.invalid/marketplace.zip',
        sha256=hashlib.sha256(data).hexdigest(), package_name='scenario-synthetic',
        marketplace_name='scenario-synthetic-marketplace', allow_local_http=False)
    with pytest.raises(installer.InstallationError, match='虚拟环境目录超出'):
        installer.install_locked(args, tmp_path / 'installation', tmp_path, 'synthetic-claude')


def test_installer_download_total_deadline_rejects_a_slow_source(tmp_path, monkeypatch):
    data = archive_fixture()
    monkeypatch.setattr(installer, 'build_opener', lambda *args: type('Opener', (), {
        'open': lambda self, *a, **k: io.BytesIO(data)})())
    ticks = iter((0, 61))
    monkeypatch.setattr(installer, 'monotonic', lambda: next(ticks))
    with pytest.raises(installer.InstallationError, match='超时'):
        installer.download_archive('https://source.example.invalid/marketplace.zip',
            hashlib.sha256(data).hexdigest(), tmp_path / 'slow.zip', False)
