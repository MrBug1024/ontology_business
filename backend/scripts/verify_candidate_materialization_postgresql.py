"""Reproduce pending lineage closure with the supported, non-autoflush PG session.

Uses only a newly created disposable database migrated to the actual single
head. The explicit synthetic lifecycle fixture is not business AI evidence.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import sys
import traceback
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def verify(factory):
    from sqlalchemy import select
    from app.models import Tenant, User, BusinessScenario, ScenarioModelDraftResource
    from app.services import candidate_governance_service as governance
    from app.services import scenario_model_draft_service as drafts

    tenant, user, scene = [uuid4().hex for _ in range(3)]
    before = datetime.now(timezone.utc) - timedelta(minutes=1)
    with factory() as db:
        db.info.update(tenant_id=tenant, user_id=user)
        db.add(Tenant(id=tenant, name='Isolated candidate fixture'))
        db.flush()
        db.add(User(id=user, tenant_id=tenant, email=f'{user}@example.invalid',
                    password_hash='non-login-fixture', status='active'))
        scenario = BusinessScenario(id=scene, tenant_id=tenant, name='Synthetic lineage', namespace='fixture')
        db.add(scenario)
        db.flush()
        predecessor = ScenarioModelDraftResource(id=uuid4().hex, tenant_id=tenant,
            scenario_id=scene, created_by_user_id=user, proposal_id='predecessor',
            resource_kind='workflow', resource_key='workflow.check',
            resource_identity='f' * 64, title='Check', payload={'name': 'Check'},
            source_payload={'name': 'Check'}, draft_status='resolved', revision=1,
            lineage_started_at=before, updated_at=before)
        db.add(predecessor)
        db.flush()
        proposal = {'kind': 'scenario_model', 'proposal_id': 'successor', 'payload': {
            'workflows': [{'key': 'workflow.check', 'name': 'Check', 'nodes': [], 'edges': []}]}}
        summary = drafts.materialize_draft_resources(db, scenario, proposal,
            created_by_user_id=user, consumed_draft_revisions={predecessor.id: 0})
        status = db.scalar(select(ScenarioModelDraftResource.draft_status).where(
            ScenarioModelDraftResource.proposal_id == 'successor'))
        assert status == 'superseded', 'Closed lineage decision was not flushed before returning'
        validation = governance.revalidate_materialized_candidates(db, scenario,
            tenant_id=tenant, created_by_user_id=user, proposal_id='successor')
        assert validation['revalidated_count'] == 0
        assert summary['resource_count'] == 0
        assert predecessor.draft_status == 'resolved' and predecessor.revision == 1
        current = db.scalars(select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.proposal_id == 'successor')).one()
        assert current.draft_status == 'superseded'
        assert not current.enabled and not current.publishable
        db.commit()
    return {'closed_successor_excluded_from_revalidation': True,
            'resolved_predecessor_revision_preserved': True, 'candidate_remains_inert': True}


def main():
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.config import get_settings
    from scripts.verify_alembic_roundtrip import _database_url

    settings = get_settings()
    name = f'ontology_candidate_verify_{uuid4().hex[:12]}'
    assert re.fullmatch(r'ontology_candidate_verify_[0-9a-f]{12}', name)
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
        print(json.dumps({'passed': True, 'alembic_head': head, **result}))
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
        print(json.dumps({'passed': False, 'failure_type': type(error).__name__,
            'frames': [{'function': frame.name, 'line': frame.lineno}
                       for frame in traceback.extract_tb(error.__traceback__)[-3:]]}))
        raise SystemExit(1) from None
