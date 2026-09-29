"""Verify scenario handoff concurrency in a disposable, migrated PostgreSQL DB."""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.distillation_models import DistillationPublication, DistillationScenarioState
from app.distillation_schemas import DistillationDocument, ScenarioPublishRequest
from app.models import BusinessScenario, Organization, OrganizationMember, OrganizationRole, Tenant, User
from app.services.distillation_publication_service import publish_scenario
from app.services.distillation_service import modeling_documents
from scripts.verify_alembic_roundtrip import _database_url


def seed(engine):
    with Session(engine) as db:
        for suffix in ('a', 'b'):
            tenant_id, user_id, scenario_id = f'tenant_{suffix}', f'user_{suffix}', f'scene_{suffix}'
            db.add(Tenant(id=tenant_id, name='Isolated test')); db.flush()
            db.add(User(id=user_id, tenant_id=tenant_id, email=f'{user_id}@example.invalid',
                        password_hash='unusable-test-hash', status='active')); db.flush()
            db.add(Organization(id=f'org_{suffix}', tenant_id=tenant_id, name='Test')); db.flush()
            db.add(OrganizationRole(id=f'role_{suffix}', organization_id=f'org_{suffix}', key='owner')); db.flush()
            db.add(OrganizationMember(id=f'member_{suffix}', organization_id=f'org_{suffix}',
                                      user_id=user_id, role_id=f'role_{suffix}', status='active'))
            db.add(BusinessScenario(id=scenario_id, tenant_id=tenant_id, name='Synthetic review')); db.flush()
            document = DistillationDocument(beneficiary='Reviewer', pain='Manual review',
                desired_outcome='Evidence report', success_metric='Traceable result',
                decision='continue', decision_reason='Reviewed')
            db.add(DistillationScenarioState(id=f'state_{suffix}', tenant_id=tenant_id,
                scenario_id=scenario_id, revision=1, document=document.model_dump(),
                created_by=user_id, updated_by=user_id))
        db.commit()


def verify(engine):
    payload = ScenarioPublishRequest(expected_revision=1, decision='continue', decision_reason='Reviewed')
    locked, release, started = Event(), Event(), Event()

    def handoff(first):
        with Session(engine, expire_on_commit=False) as db:
            db.info.update(tenant_id='tenant_a', user_id='user_a')
            if not first:
                started.set()
            result = publish_scenario(db, 'scene_a', payload)
            if first:
                locked.set()
                if not release.wait(20):
                    raise RuntimeError('Test barrier timed out')
            db.commit()
            return result.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handoff, True)
        if not locked.wait(10):
            release.set()
            first.result()  # Surface the real first-worker failure.
            raise RuntimeError('First worker did not reach the publication fence')
        second = pool.submit(handoff, False)
        assert started.wait(5)
        release.set()
        assert first.result() == second.result()
    with Session(engine) as db:
        db.info.update(tenant_id='tenant_a', user_id='user_a')
        assert db.scalar(select(func.count()).select_from(DistillationPublication)) == 1
        documents = modeling_documents(db, 'scene_a')
        assert len(documents) == 1 and documents[0]['business_decision'] == 'continue'
        for scene, revision, status in [('scene_b', 1, 404), ('scene_a', 2, 409)]:
            try:
                publish_scenario(db, scene, payload.model_copy(update={'expected_revision': revision}))
            except HTTPException as error:
                assert error.status_code == status
            else:
                raise AssertionError('Unauthorized or stale handoff accepted')
            db.rollback()


def main(additional_checks=None):
    settings = get_settings()
    name = 'ontology_handoff_verify_' + uuid4().hex[:12]
    control = create_engine(_database_url(settings, 'postgres'), isolation_level='AUTOCOMMIT')
    admin_url = _database_url(settings, name)
    runtime = admin = None
    created = False
    keys = ('ALEMBIC_DATABASE_URL', 'ALEMBIC_ROLE', 'ALEMBIC_USE_ADMIN')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        with control.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
        os.environ['ALEMBIC_DATABASE_URL'] = admin_url.render_as_string(hide_password=False)
        for key in keys[1:]:
            os.environ.pop(key, None)
        command.upgrade(Config(str(BACKEND / 'alembic.ini')), 'head')
        admin = create_engine(admin_url)
        seed(admin)
        runtime = create_engine(admin_url,
            connect_args={'options': '-c lock_timeout=15000 -c statement_timeout=20000'})
        verify(runtime)
        if additional_checks:
            additional_checks(runtime)
        print('PASS: isolated-DB handoff concurrency, one publication, advisor retrieval, tenant denial, revision conflict')
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if runtime:
            runtime.dispose()
        if admin:
            admin.dispose()
        if created:
            with control.connect() as db:
                db.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname=:name AND pid<>pg_backend_pid()'), {'name': name})
                db.exec_driver_sql(f'DROP DATABASE "{name}"')
        control.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # SQL/provider exceptions can contain connection details or bound data.
        print('FAILED:', type(error).__name__, getattr(error, 'status_code', ''),
              error.detail if isinstance(error, HTTPException) else '')
        original = getattr(error, 'orig', None)
        if getattr(original, 'sqlstate', None):
            diagnostic = getattr(original, 'diag', None)
            print('PostgreSQL:', original.sqlstate, getattr(diagnostic, 'message_primary', ''))
        raise SystemExit(1)
