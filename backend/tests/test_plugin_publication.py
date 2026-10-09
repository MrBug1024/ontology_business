from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
import zipfile

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.config import Settings
from app.models import AssistantMessage, AssistantThread, BusinessScenario, OntologyRelease
from app.plugin_publication_schemas import PluginPublicationChange
from app.services import plugin_installation_commands as commands, plugin_publication_service as publication
from test_plugin_artifact_catalog import reviewed_fixture


def context(monkeypatch, *, configured=True):
    row, release, scenario, original = reviewed_fixture()
    release.tenant_id = scenario.tenant_id = 'tenant'
    release.scenario_id = scenario.id
    row.proposal['manifest']['scenario']['id'] = scenario.id
    row.proposal['manifest']['deployment']['release_id'] = release.id
    thread = SimpleNamespace(id=row.thread_id, tenant_id='tenant', created_by_user_id='user')
    rows = {(AssistantMessage, row.id): row, (AssistantThread, thread.id): thread,
            (OntologyRelease, release.id): release, (BusinessScenario, scenario.id): scenario}
    added = []
    locks = []
    principal = SimpleNamespace(tenant_id='tenant', user_id='user')
    monkeypatch.setattr(publication.catalog, 'get_artifact', lambda *args: (row, release, scenario))
    monkeypatch.setattr(publication.permission_service, 'require_principal', lambda *args: principal)
    monkeypatch.setattr(publication.permission_service, 'refresh_request_authorization', lambda *args: None)
    monkeypatch.setattr(publication.permission_service, 'require_tenant_permission', lambda *args: None)
    monkeypatch.setattr(publication.permission_service, 'require_scenario_permission', lambda *args: None)
    monkeypatch.setattr(publication, 'owned_root', lambda *args, **kwargs: locks.append(kwargs.get('lock')))

    def add(value):
        rows[type(value), value.id] = value
        added.append(value)
    db = SimpleNamespace(get=lambda cls, key, **kwargs: rows.get((cls, key)), add=add,
                         flush=lambda: None, refresh=lambda *args: None)
    settings = SimpleNamespace(plugin_public_base_url='http://127.0.0.1:9999' if configured else '',
                               api_prefix='/api', plugin_allow_local_http=True)
    request = PluginPublicationChange(artifact_hash=row.proposal['artifact_hash'], expected_revision=0,
                                     action='publish', confirmed_publication=True)
    return db, row, release, scenario, rows, added, locks, settings, request, original


def test_unpublished_reviewed_plugin_is_private_and_has_no_install_command(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.get_publication(db, row.id, settings)
    assert value.status == 'unpublished' and value.revision == 0 and not value.available
    assert value.installation is None and not added
    with pytest.raises(HTTPException) as error:
        publication.public_material(db, value.publication_id, 'marketplace')
    assert error.value.status_code == 404 and error.value.detail == publication.NOT_AVAILABLE


def test_publish_pins_original_source_and_produces_compatible_credential_free_commands(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    before = deepcopy(row.proposal)
    value = publication.change_publication(db, row.id, request, settings)
    assert value.status == 'published' and value.revision == 1 and value.available
    assert locks == [True] and len(added) == 2
    assert row.proposal == before
    info = value.installation
    assert info.scope == 'local_test' and info.usage_command == '/scenario-synthetic:run-scenario'
    assert '--allow-local-http' in info.powershell_command and '--sha256' in info.bash_command
    assert info.marketplace_sha256 in info.powershell_command and info.installer_sha256 in info.bash_command
    assert 'SCENARIO_API_KEY=' not in info.powershell_command and 'SCENARIO_API_KEY=' not in info.bash_command
    name, archive, digest = publication.public_material(db, value.publication_id, 'marketplace')
    assert name.endswith('-marketplace.zip') and digest == hashlib.sha256(archive).hexdigest()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(io.BytesIO(archive)) as published:
        for path in source.namelist():
            assert published.read(f'scenario-synthetic-marketplace/plugins/{path}') == source.read(path)


def test_publish_retry_with_same_expected_revision_is_idempotent_and_stale_decisions_conflict(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    first = publication.change_publication(db, row.id, request, settings)
    repeated = publication.change_publication(db, row.id, request, settings)
    assert repeated == first and len(added) == 2
    withdrawn = publication.change_publication(db, row.id,
        request.model_copy(update={'action': 'withdraw', 'expected_revision': 1}), settings)
    assert withdrawn.revision == 2 and withdrawn.status == 'withdrawn' and withdrawn.installation is None
    with pytest.raises(HTTPException) as error:
        publication.change_publication(db, row.id, request, settings)
    assert error.value.status_code == 409


def test_withdraw_removes_anonymous_access_and_explicit_republish_restores_same_artifact(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    before = publication.public_material(db, value.publication_id, 'marketplace')[1]
    withdrawn = publication.change_publication(db, row.id,
        request.model_copy(update={'action': 'withdraw', 'expected_revision': 1}), settings)
    for kind in ('marketplace', 'installer'):
        with pytest.raises(HTTPException) as error:
            publication.public_material(db, value.publication_id, kind)
        assert error.value.status_code == 404
    after = publication.change_publication(db, row.id, request.model_copy(update={'expected_revision': 2}), settings)
    assert after.revision == 3 and after.publication_id == value.publication_id
    assert publication.public_material(db, value.publication_id, 'marketplace')[1] == before
    assert sum(item.proposal['kind'] == publication.PUBLICATION_AUDIT_KIND for item in added) == 3


@pytest.mark.parametrize('denied_permission', ['tenant', 'scenario'])
def test_publication_requires_management_permission_even_when_workspace_is_readable(monkeypatch, denied_permission):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    def denied(*args):
        raise HTTPException(403, '无权发布')
    name = 'require_tenant_permission' if denied_permission == 'tenant' else 'require_scenario_permission'
    monkeypatch.setattr(publication.permission_service, name, denied)
    with pytest.raises(HTTPException) as error:
        publication.change_publication(db, row.id, request, settings)
    assert error.value.status_code == 403 and not added


def test_publication_rejects_wrong_hash_and_missing_explicit_origin(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch, configured=False)
    for submitted in (request, request.model_copy(update={'artifact_hash': 'b' * 64})):
        with pytest.raises(HTTPException) as error:
            publication.change_publication(db, row.id, submitted, settings)
        assert error.value.status_code == 409 and not added
    value = publication.get_publication(db, row.id, settings)
    assert not value.configuration_ready and value.installation is None


@pytest.mark.parametrize('change', [{'enabled': False}, {'status': 'retired'}])
def test_release_disable_or_retirement_prevents_publication_and_retrieval(monkeypatch, change):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    for key, item in change.items():
        setattr(release, key, item)
    assert not publication.get_publication(db, row.id, settings).available
    with pytest.raises(HTTPException) as error:
        publication.public_material(db, value.publication_id, 'marketplace')
    assert error.value.status_code == 404


def test_disable_during_publication_rejects_without_a_success_record(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    db.refresh = lambda obj: setattr(release, 'enabled', False)
    with pytest.raises(HTTPException) as error:
        publication.change_publication(db, row.id, request, settings)
    assert error.value.status_code == 409 and not added


def test_withdraw_during_public_download_rejects_the_late_response(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    def withdraw(obj):
        if isinstance(obj, AssistantMessage):
            obj.proposal = {**obj.proposal, 'status': 'withdrawn', 'revision': 2}
    db.refresh = withdraw
    with pytest.raises(HTTPException) as error:
        publication.public_material(db, value.publication_id, 'marketplace')
    assert error.value.status_code == 404


def test_public_source_cannot_be_redirected_to_another_tenant_or_artifact(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    head = rows[AssistantMessage, value.publication_id]
    before = deepcopy(head.proposal)
    for patch in ({'tenant_id': 'another-tenant'}, {'artifact_id': 'other-artifact'}, {'artifact_hash': 'b' * 64}):
        head.proposal = {**before, **patch}
        with pytest.raises(HTTPException) as error:
            publication.public_material(db, value.publication_id, 'marketplace')
        assert error.value.status_code == 404 and error.value.detail == publication.NOT_AVAILABLE


def test_published_installer_is_immutable_after_trusted_installer_source_changes(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    original_installer = publication.public_material(db, value.publication_id, 'installer')[1]
    monkeypatch.setattr(publication, 'installer_bytes', lambda: b'new trusted installer')
    current = publication.get_publication(db, row.id, settings)
    assert current.installation.installer_sha256 == value.installation.installer_sha256
    assert publication.public_material(db, value.publication_id, 'installer')[1] == original_installer


def test_corrupt_or_oversized_public_material_is_rejected_without_internal_details(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    value = publication.change_publication(db, row.id, request, settings)
    head = rows[AssistantMessage, value.publication_id]
    for encoded in ('invalid-base64', 'A' * (publication.MAX_PUBLIC_ARCHIVE_BYTES * 2)):
        head.proposal = {**head.proposal, 'marketplace_archive': encoded}
        with pytest.raises(HTTPException) as error:
            publication.public_material(db, value.publication_id, 'marketplace')
        assert error.value.status_code == 404 and error.value.detail == publication.NOT_AVAILABLE


def test_origin_change_does_not_silently_rebind_published_installation_commands(monkeypatch):
    db, row, release, scenario, rows, added, locks, settings, request, original = context(monkeypatch)
    publication.change_publication(db, row.id, request, settings)
    settings.plugin_public_base_url = 'https://new-source.example.invalid'
    current = publication.get_publication(db, row.id, settings)
    assert current.status == 'published' and not current.available and current.installation is None


def test_publication_input_is_closed_deliberate_and_revision_bounded():
    for invalid in ({'confirmed_publication': False}, {'extra_secret': 'synthetic'}, {'expected_revision': -1}):
        with pytest.raises(ValidationError):
            PluginPublicationChange(**{'artifact_hash': 'a' * 64, 'expected_revision': 0,
                'action': 'publish', 'confirmed_publication': True, **invalid})


@pytest.mark.parametrize('url,allow', [
    ('http://127.0.0.1:1234', False), ('http://remote.example.invalid', True),
    ('https://user:synthetic@source.example.invalid', False), ('https://source.example.invalid/?token=synthetic', False),
    ('https://source.example.invalid/#fragment', False), ('https://source.example.invalid/../path', False),
    ('https://source.example.invalid:bad', False),
])
def test_installation_origin_rejects_unsafe_or_implicit_http_configuration(url, allow):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url='postgresql+psycopg://fixture@localhost/fixture',
                 plugin_public_base_url=url, plugin_allow_local_http=allow)


def test_installation_origin_accepts_explicit_loopback_test_and_https_base_path():
    for url, allow in [('http://127.0.0.1:1234', True), ('https://source.example.invalid:8443/platform', False)]:
        settings = Settings(_env_file=None, database_url='postgresql+psycopg://fixture@localhost/fixture',
                            plugin_public_base_url=url, plugin_allow_local_http=allow)
        assert settings.plugin_public_base_url == url


def test_shell_commands_quote_configured_path_and_never_execute_credentials():
    info = commands.installation_info(SimpleNamespace(plugin_public_base_url="https://source.example.invalid/path'quote",
        api_prefix='/api'), identity='a' * 32, package_name='scenario-synthetic', version='1.0.0',
        marketplace_hash='b' * 64, installer_hash='c' * 64)
    assert "path''quote" in info.powershell_command and "'\"'\"'" in info.bash_command
    assert '--allow-local-http' not in info.powershell_command and info.scope == 'public'
