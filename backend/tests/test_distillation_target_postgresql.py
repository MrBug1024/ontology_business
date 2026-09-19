"""Target configuration belongs to its authorized project, never a credential store."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session
from app.distillation_schemas import DistillationDocument, ProjectCreate
from app.distillation_target_schemas import TargetSystem
from app.services import distillation_service


def _target(**changes):
    return TargetSystem(**({"key": "portal", "name": "Target system", "base_url": "https://example.com",
        "purpose": "Review the intake process", "allowed_paths": ["/intake"]} | changes))


def test_target_scope_round_trips_in_project_and_other_tenant_cannot_read_it(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    foreign = seed_workspace(isolated_postgresql.admin_engine)
    document = DistillationDocument(target_systems=[_target()])
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        row = distillation_service.create_project(db, ProjectCreate(name="Target research",
            scenario_id=workspace["scenario_id"], document=document))
        db.commit()
        project_id = row.id
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        persisted = distillation_service.project(db, project_id)
        assert persisted.document["target_systems"] == [document.target_systems[0].model_dump()]
    with tenant_session(isolated_postgresql.runtime_engine, foreign) as db:
        with pytest.raises(HTTPException) as failure:
            distillation_service.project(db, project_id)
        assert failure.value.status_code == 404


def test_target_notes_cannot_persist_credentials(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    document = DistillationDocument(target_systems=[_target(notes="password=synthetic-test-only")])
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        with pytest.raises(HTTPException) as failure:
            distillation_service.create_project(db, ProjectCreate(name="No secrets", document=document))
        assert failure.value.status_code == 422
