"""Verify publication CAS, ACL and public reads in a disposable PostgreSQL DB.

Reviewed plugin contents are synthetic. No business execution, external hosting
or third-party credential is created by this verification.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def seed(factory):
    from app.models import (AssistantMessage, AssistantThread, AuthorizationGrant, BusinessScenario,
        OntologyRelease, OntologySnapshot, Organization, OrganizationMember, OrganizationRole, Tenant, User)
    from app.services.plugin_coding_identity import adapter_hash, snapshot_id
    from app.services.plugin_coding_workspace import WORKSPACE_KIND, root_id
    from app.services.scenario_package_artifact import build_artifact
    tenant, user, other_tenant, other_user, scenario, release, workspace, organization = [uuid4().hex for _ in range(8)]
    with factory() as db:
        for key, label in [(tenant, 'Publication fixture'), (other_tenant, 'Foreign fixture')]:
            db.add(Tenant(id=key, name=label))
        db.flush()
        for user_id, tenant_id in [(user, tenant), (other_user, other_tenant)]:
            db.add(User(id=user_id, tenant_id=tenant_id, email=f'{user_id}@example.invalid',
                password_hash='non-login-synthetic-fixture', status='active'))
            org = organization if tenant_id == tenant else uuid4().hex
            role = uuid4().hex
            db.add(Organization(id=org, tenant_id=tenant_id, name='Synthetic workspace'))
            db.flush()
            db.add(OrganizationRole(id=role, organization_id=org, key='owner', name='Synthetic owner'))
            db.flush()
            db.add(OrganizationMember(id=uuid4().hex, organization_id=org, user_id=user_id, role_id=role, status='active'))
        db.add(BusinessScenario(id=scenario, tenant_id=tenant, name='Synthetic publication scenario', status='active'))
        db.flush()
        snapshot = OntologySnapshot(id=uuid4().hex, tenant_id=tenant, scenario_id=scenario, content={}, content_hash='a' * 64)
        db.add(snapshot)
        db.flush()
        db.add(OntologyRelease(id=release, tenant_id=tenant, scenario_id=scenario, snapshot_id=snapshot.id,
                              name='Synthetic enabled release', status='released', enabled=True))
        db.flush()
        manifest = {'package_name': f'scenario-{release}', 'host': 'claude_code', 'scenario': {'id': scenario, 'name': 'Synthetic'},
                    'deployment': {'release_id': release, 'definition_hash': 'a' * 64},
                    'capabilities': [{'kind': 'function', 'key': 'check'}]}
        files = {'README.md': 'Configure SCENARIO_API_KEY externally.\n',
            'skills/run-scenario/SKILL.md': '---\nname: run-scenario\ndescription: Invoke the synthetic capability\n---\nUse invoke_scenario_capability and get_scenario_receipt.\n',
            'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\n\nasync def main():\n    result = await invoke_scenario_capability("function", "check", {})\n    print(result)\n\nif __name__ == "__main__":\n    asyncio.run(main())\n'}
        artifact = build_artifact(manifest, files=files)
        artifact_id = snapshot_id(tenant, manifest['package_name'], '1.0.0')
        db.add(AssistantThread(id=workspace, tenant_id=tenant, created_by_user_id=user, scenario_id=scenario,
                               scope_key=f'plugin-coding:{release}'))
        db.flush()
        db.add(AssistantMessage(id=root_id(workspace), thread_id=workspace, role='system', proposal={
            'kind': WORKSPACE_KIND, 'manifest': manifest, 'revision': 1}))
        db.add(AssistantMessage(id=artifact_id, thread_id=workspace, role='system', proposal={
            'kind': 'scenario-plugin-artifact.v1', 'manifest': manifest, 'files': files, 'plugin_version': '1.0.0',
            'artifact_hash': hashlib.sha256(artifact).hexdigest(), 'adapter_hash': adapter_hash()}))
        db.commit()
    return {'tenant': tenant, 'user': user, 'other_tenant': other_tenant, 'other_user': other_user,
            'scenario': scenario, 'release': release, 'artifact': artifact_id, 'organization': organization,
            'artifact_hash': hashlib.sha256(artifact).hexdigest()}


def verify(factory):
    from fastapi import HTTPException
    from sqlalchemy import event, func, select
    from app.models import AssistantMessage, AuthorizationGrant, OntologyRelease
    from app.plugin_publication_schemas import PluginPublicationChange
    from app.services import plugin_publication_service as publication
    from app.services import plugin_artifact_catalog as catalog
    from time import perf_counter
    fixture = seed(factory)
    settings = SimpleNamespace(plugin_public_base_url='http://127.0.0.1:1234', plugin_allow_local_http=True, api_prefix='/api')
    request = PluginPublicationChange(artifact_hash=fixture['artifact_hash'], expected_revision=0,
                                     action='publish', confirmed_publication=True)

    def session(*, foreign=False, anonymous=False):
        db = factory()
        if not anonymous:
            db.info.update(tenant_id=fixture['other_tenant'] if foreign else fixture['tenant'],
                           user_id=fixture['other_user'] if foreign else fixture['user'])
        return db

    with session() as db:
        initial = publication.get_publication(db, fixture['artifact'], settings)
        assert initial.status == 'unpublished' and initial.revision == 0
        original = dict(db.get(AssistantMessage, fixture['artifact']).proposal)
    with session(anonymous=True) as db:
        try:
            publication.public_material(db, initial.publication_id, 'marketplace')
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError('unpublished package was readable')
    with session(foreign=True) as db:
        try:
            publication.get_publication(db, fixture['artifact'], settings)
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError('foreign tenant publication was readable')
    deny_id = uuid4().hex
    with session() as db:
        db.add(AuthorizationGrant(id=deny_id, organization_id=fixture['organization'], user_id=fixture['user'],
            resource_type='scenario', resource_id=fixture['scenario'], verb='manage', effect='deny'))
        db.commit()
    with session() as db:
        try:
            publication.change_publication(db, fixture['artifact'], request, settings)
        except HTTPException as error:
            assert error.status_code == 403
            db.rollback()
        else:
            raise AssertionError('scenario deny was bypassed')
        db.delete(db.get(AuthorizationGrant, deny_id))
        db.commit()
    barrier = threading.Barrier(2)

    def concurrent_publish():
        with session() as db:
            barrier.wait(timeout=10)
            value = publication.change_publication(db, fixture['artifact'], request, settings)
            db.commit()
            return value
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: concurrent_publish(), range(2)))
    assert values[0].publication_id == values[1].publication_id and {value.revision for value in values} == {1}
    identity = values[0].publication_id
    with session() as db:
        assert db.scalar(select(func.count()).select_from(AssistantMessage).where(
            AssistantMessage.proposal['kind'].as_string() == publication.PUBLICATION_AUDIT_KIND)) == 1
        assert db.get(AssistantMessage, fixture['artifact']).proposal == original
    queries = []
    started = perf_counter()
    engine = factory.kw['bind']
    def count(*args):
        queries.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        with session(anonymous=True) as db:
            _, material, digest = publication.public_material(db, identity, 'marketplace')
            assert material.startswith(b'PK') and digest == hashlib.sha256(material).hexdigest()
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    elapsed = round((perf_counter() - started) * 1000, 3)
    with session() as db:
        withdrawn = publication.change_publication(db, fixture['artifact'], request.model_copy(update={
            'expected_revision': 1, 'action': 'withdraw'}), settings)
        db.commit()
        assert withdrawn.revision == 2 and withdrawn.status == 'withdrawn'
    with session(anonymous=True) as db:
        try:
            publication.public_material(db, identity, 'installer')
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError('withdrawn installer was readable')
    with session() as db:
        try:
            publication.change_publication(db, fixture['artifact'], request, settings)
        except HTTPException as error:
            assert error.status_code == 409
            db.rollback()
        else:
            raise AssertionError('stale publication revision was accepted')
        publication.change_publication(db, fixture['artifact'], request.model_copy(update={'expected_revision': 2}), settings)
        db.commit()
        db.get(OntologyRelease, fixture['release']).enabled = False
        db.commit()
    with session(anonymous=True) as db:
        try:
            publication.public_material(db, identity, 'marketplace')
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError('disabled release remained installable')
    return {'concurrent_publication_idempotent': True, 'one_durable_audit_per_revision': True,
            'foreign_tenant_rejected': True, 'scenario_manage_deny_enforced': True,
            'unpublished_anonymous_rejected': True, 'published_anonymous_exact_hash': True,
            'withdrawal_blocks_public_download': True, 'stale_revision_rejected': True,
            'explicit_republish': True, 'disabled_release_blocks_installation': True,
            'original_artifact_unchanged': True,
            'public_read': {'query_count': len(queries), 'elapsed_ms': elapsed, 'returned_bytes': len(material)},
            'reviewed_contents': 'synthetic', 'business_execution_tested': False}


def main():
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.config import get_settings
    from scripts.verify_alembic_roundtrip import _database_url
    settings = get_settings()
    name = f'ontology_publication_verify_{uuid4().hex[:12]}'
    assert re.fullmatch(r'ontology_publication_verify_[0-9a-f]{12}', name)
    control = create_engine(_database_url(settings, 'postgres'), isolation_level='AUTOCOMMIT')
    target = create_engine(_database_url(settings, name))
    keys = ('ALEMBIC_DATABASE_URL', 'ALEMBIC_ROLE', 'ALEMBIC_USE_ADMIN')
    previous = {key: os.environ.get(key) for key in keys}
    created = False
    try:
        with control.connect() as db:
            assert db.execute(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': name}).scalar() is None
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
        os.environ['ALEMBIC_DATABASE_URL'] = _database_url(settings, name).render_as_string(hide_password=False)
        os.environ.pop('ALEMBIC_ROLE', None)
        os.environ.pop('ALEMBIC_USE_ADMIN', None)
        config = Config(str(BACKEND / 'alembic.ini'))
        head = ScriptDirectory.from_config(config).get_current_head()
        assert head
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            command.upgrade(config, head)
        result = verify(sessionmaker(bind=target, expire_on_commit=False, autoflush=False))
        print(json.dumps({'passed': True, 'alembic_head': head, **result}, indent=2))
    finally:
        target.dispose()
        if created:
            with control.connect() as db:
                db.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()'), {'name': name})
                db.exec_driver_sql(f'DROP DATABASE "{name}"')
        control.dispose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Plugin publication verification failed: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1) from None
