"""Read-only, caller-scoped validation receipt choices for the package UI."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from ..models import CapabilityInvocation, OntologyRelease, WorkflowRun
from . import permission_service, release_service


def list_evidence(db, release_id: str) -> list[dict]:
    principal = permission_service.require_principal(db)
    release = db.scalar(select(OntologyRelease).where(
        OntologyRelease.id == release_id, OntologyRelease.tenant_id == principal.tenant_id,
        OntologyRelease.deleted_at.is_(None),
    ))
    if release is None:
        raise HTTPException(404, '发布不可用')
    release_service._scenario_for_read(db, release.scenario_id)
    rows = db.scalars(select(CapabilityInvocation).where(
        CapabilityInvocation.tenant_id == principal.tenant_id,
        CapabilityInvocation.scenario_id == release.scenario_id,
        CapabilityInvocation.release_id == release.id,
        CapabilityInvocation.requested_by_user_id == principal.user_id,
        CapabilityInvocation.status.in_(['succeeded', 'failed', 'rejected']),
    ).order_by(CapabilityInvocation.created_at.desc(), CapabilityInvocation.id.desc()).limit(100)).all()
    run_ids = {row.result_document.get('output', {}).get('workflow_run_id') for row in rows
        if row.capability_kind == 'workflow' and isinstance(row.result_document, dict)
        and isinstance(row.result_document.get('output'), dict)} - {None}
    runs = db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids),
        WorkflowRun.scenario_id == release.scenario_id, WorkflowRun.release_id == release.id)).all() if run_ids else []
    statuses = {run.id: run.status for run in runs}
    result = []
    for row in rows:
        output = (row.result_document or {}).get('output', {})
        workflow_status = statuses.get(output.get('workflow_run_id')) if isinstance(output, dict) else None
        eligible = row.capability_kind != 'workflow' or row.status != 'succeeded' or workflow_status in {'succeeded', 'failed', 'rejected'}
        result.append({'invocation_id': row.id, 'kind': row.capability_kind, 'key': row.capability_key,
            'status': row.status, 'created_at': row.created_at, 'workflow_status': workflow_status, 'eligible': eligible})
    return result
