from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.plugin_coding_schemas import PluginArtifactDownload, PluginCodingReview
from app.services import plugin_artifact_catalog as catalog, plugin_coding_export as review
from app.services.plugin_coding_identity import adapter_hash
from app.services.plugin_coding_validation import files_hash
from app.services.scenario_package_artifact import build_artifact
from test_plugin_coding import manifest, sources


def reviewed_fixture():
    value = manifest()
    value['scenario']['name'] = 'Synthetic scenario'
    files = sources()
    package = build_artifact(value, files=files)
    snapshot = {'kind': catalog.ARTIFACT_KIND, 'manifest': value, 'files': files,
                'plugin_version': '1.0.0', 'artifact_hash': hashlib.sha256(package).hexdigest(),
                'adapter_hash': adapter_hash()}
    row = SimpleNamespace(id='artifact', thread_id='workspace', proposal=snapshot,
                          created_at=datetime.now(timezone.utc))
    release = SimpleNamespace(id='release', name='fixed version', enabled=True,
                              status='released', deleted_at=None)
    scenario = SimpleNamespace(id='scenario', name='Synthetic scenario', status='active')
    return row, release, scenario, package


def download_context(monkeypatch):
    row, release, scenario, package = reviewed_fixture()
    monkeypatch.setattr(catalog, 'get_artifact', lambda *args: (row, release, scenario))
    monkeypatch.setattr(catalog.release_service, '_scenario_for_read', lambda *args: None)
    db = SimpleNamespace(refresh=lambda *args: None)
    request = PluginArtifactDownload(artifact_hash=row.proposal['artifact_hash'])
    return db, row, release, scenario, package, request


def test_delivery_uses_reviewed_files_even_after_authoring_workspace_changes(monkeypatch):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    current_workspace = {'files': deepcopy(row.proposal['files'])}
    current_workspace['files']['README.md'] += ' changed after review'
    name, artifact = catalog.download_artifact(db, row.id, request)
    assert artifact == original
    assert name == 'scenario-synthetic-1.0.0'
    assert row.proposal['files'] != current_workspace['files']
    name, market = catalog.download_artifact(db, row.id, request.model_copy(update={'format': 'marketplace'}))
    assert name.endswith('-marketplace')
    assert market != original
    assert row.proposal['artifact_hash'] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize('change', [{'enabled': False}, {'status': 'retired'}, {'deleted_at': datetime.now(timezone.utc)}])
def test_disabled_or_retired_release_cannot_be_delivered_from_a_stale_selection(monkeypatch, change):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    for key, value in change.items():
        setattr(release, key, value)
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request)
    assert caught.value.status_code == 409


def test_release_disabled_while_restoring_artifact_is_rechecked(monkeypatch):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    db.refresh = lambda value: setattr(release, 'enabled', False)
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request)
    assert caught.value.status_code == 409


def test_changed_adapter_or_unrecoverable_bytes_never_replace_a_reviewed_version(monkeypatch):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    row.proposal['adapter_hash'] = 'old adapter'
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request)
    assert caught.value.status_code == 409
    row.proposal['adapter_hash'] = adapter_hash()
    monkeypatch.setattr(catalog, 'build_artifact', lambda *args, **kwargs: b'changed bytes')
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request)
    assert caught.value.status_code == 409


def test_selection_requires_exact_artifact_identity(monkeypatch):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request.model_copy(update={'artifact_hash': 'b' * 64}))
    assert caught.value.status_code == 409


def test_changed_validation_returns_an_actionable_conflict_and_preserves_snapshot(monkeypatch):
    db, row, release, scenario, original, request = download_context(monkeypatch)
    before = deepcopy(row.proposal)
    def rejected(*args, **kwargs):
        raise ValueError('private validation detail')
    monkeypatch.setattr(catalog, 'build_artifact', rejected)
    with pytest.raises(HTTPException) as caught:
        catalog.download_artifact(db, row.id, request)
    assert caught.value.status_code == 409
    assert 'private validation detail' not in caught.value.detail
    assert row.proposal == before


def test_artifact_read_checks_scenario_acl_before_returning_metadata(monkeypatch):
    result = reviewed_fixture()[:3]
    monkeypatch.setattr(catalog, 'catalog_query', lambda db: SimpleNamespace(where=lambda *args: None))
    def denied(*args):
        raise HTTPException(404, '不可用')
    monkeypatch.setattr(catalog.release_service, '_scenario_for_read', denied)
    db = SimpleNamespace(execute=lambda *args: SimpleNamespace(first=lambda: result))
    with pytest.raises(HTTPException) as caught:
        catalog.get_artifact(db, 'artifact')
    assert caught.value.status_code == 404


def test_review_reserves_a_snapshot_without_starting_delivery(monkeypatch):
    value = manifest()
    files = sources()
    document = {'revision': 3, 'files': files, 'manifest': value, 'coding_contract': value,
                'release_id': 'release', 'plugin_version': '1.0.0', 'adapter_hash': adapter_hash(),
                'phase': 'ready_for_review', 'events': [], 'acceptance_request': {
                    'expected_revision': 1, 'capabilities': [{'kind': 'function', 'key': 'check'}],
                    'confirmed_business_acceptance': True, 'acceptance_cases': [
                        {'kind': 'function', 'key': 'check', 'role': role, 'invocation_id': letter * 32,
                         'expected_status': 'succeeded'} for role, letter in [('success', 'a'), ('boundary', 'b'), ('failure', 'c')]]}}
    value['host'] = 'claude_code'
    root = SimpleNamespace(thread_id='workspace', proposal=document)
    monkeypatch.setattr(review, 'owned_root', lambda *args, **kwargs: root)
    monkeypatch.setattr(review, 'prepare_package', lambda *args: ('scenario-synthetic', value))
    monkeypatch.setattr(review, 'check_release_state', lambda *args: None)
    monkeypatch.setattr(review.permission_service, 'require_principal', lambda *args: SimpleNamespace(tenant_id='tenant', user_id='user'))
    saved = []
    db = SimpleNamespace(get=lambda *args: None, add=saved.append, commit=lambda: None)
    identity = review.review_workspace(db, 'workspace', PluginCodingReview(expected_revision=3,
        files_hash=files_hash(files), confirmed_code_review=True))
    assert saved[0].id == identity
    assert saved[0].proposal['kind'] == catalog.ARTIFACT_KIND
    assert root.proposal['phase'] == 'released'
    assert root.proposal['exported_count'] == 0
    assert root.proposal['revision'] == 4
    assert saved[0].proposal['files'] == files
    root.proposal['files']['README.md'] += 'later workspace mutation'
    assert saved[0].proposal['files']['README.md'] == files['README.md']
    with pytest.raises(HTTPException) as caught:
        review.review_workspace(db, 'workspace', PluginCodingReview(expected_revision=3,
            files_hash=files_hash(files), confirmed_code_review=True))
    assert caught.value.status_code == 409


def test_review_and_delivery_contracts_reject_unreviewed_or_open_inputs():
    with pytest.raises(ValidationError):
        PluginCodingReview(expected_revision=1, files_hash='a' * 64, confirmed_code_review=False)
    with pytest.raises(ValidationError):
        PluginArtifactDownload(artifact_hash='a' * 64, workspace_files={'README.md': 'tampered'})
