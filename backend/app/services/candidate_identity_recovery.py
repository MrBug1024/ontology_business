"""Bounded recovery for unedited v33/v34 sidecars during explicit revalidation.

Remove this compatibility path once no open, unedited candidates from these
two compiler versions remain. New compilations persist identities directly.
"""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AssistantCompilationJob, ScenarioModelDraftResource
from .candidate_identity_projection import project_identity


LEGACY_VERSIONS = ('scenario_model.compiler.v33', 'scenario_model.compiler.v34')


def recovered_payload(row: ScenarioModelDraftResource,
                      job: AssistantCompilationJob) -> dict[str, Any] | None:
    if (job.compiler_version not in LEGACY_VERSIONS or job.status != 'succeeded'
            or row.materialization_source != 'compiler_sidecar'
            or row.payload != row.source_payload
            or any(getattr(row, key) != getattr(job, key)
                   for key in ('tenant_id', 'scenario_id', 'created_by_user_id'))
            or row.compilation_job_id != job.id):
        return None
    result = job.result if isinstance(job.result, dict) else {}
    if result.get('proposal_id') != row.proposal_id:
        return None
    compiled = result.get('payload') or {}
    if row.resource_kind == 'property':
        parent_key = row.payload.get('entity_ref')
        parents = [item for item in compiled.get('entities', []) if item.get('key') == parent_key]
        if len(parents) != 1 or not parents[0].get('existing_id'):
            return None
        # Match within the exact parent returned by this immutable compilation.
        projected = project_identity('entities', {'properties': [row.payload]}, parents[0])
        payload = projected['properties'][0]
        payload['entity_ref'] = parents[0]['existing_id']
        return payload
    section = {'entity': 'entities', 'relation': 'relations'}.get(row.resource_kind)
    if section is None:
        return None
    peers = [item for item in compiled.get(section, []) if item.get('key') == row.resource_key]
    if len(peers) != 1 or peers[0].get('name') != row.payload.get('name'):
        return None
    return project_identity(section, row.payload, peers[0])


def recover_legacy_identities(db: Session, rows: list[ScenarioModelDraftResource]) -> None:
    """Caller owns candidate locks and increments revisions in the same transaction."""
    for scope in {(row.tenant_id, row.scenario_id, row.created_by_user_id) for row in rows}:
        scoped = [row for row in rows
                  if (row.tenant_id, row.scenario_id, row.created_by_user_id) == scope
                  and row.compilation_job_id and row.materialization_source == 'compiler_sidecar'
                  and row.payload == row.source_payload]
        if not scoped:
            continue
        jobs = db.scalars(select(AssistantCompilationJob).where(
            AssistantCompilationJob.id.in_({row.compilation_job_id for row in scoped}),
            AssistantCompilationJob.tenant_id == scope[0],
            AssistantCompilationJob.scenario_id == scope[1],
            AssistantCompilationJob.created_by_user_id == scope[2],
            AssistantCompilationJob.compiler_version.in_(LEGACY_VERSIONS),
            AssistantCompilationJob.status == 'succeeded',
        )).all()
        by_id = {job.id: job for job in jobs}
        for row in scoped:
            job = by_id.get(row.compilation_job_id)
            if job is None:
                continue
            payload = recovered_payload(row, job)
            if payload is not None and payload != row.payload:
                row.payload = copy.deepcopy(payload)
