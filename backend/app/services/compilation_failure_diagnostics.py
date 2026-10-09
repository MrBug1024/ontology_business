"""Bounded structural diagnostics for failed candidate materialization."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import traceback

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..database import SessionLocal
from ..models import AssistantAuditLog, AssistantCompilationJob
from . import permission_service


def structural_failure(error: Exception) -> dict:
    # Exception messages, source lines and locals may carry SQL, inputs or keys.
    frames = traceback.extract_tb(error.__traceback__)[-10:]
    return {'code': 'candidate_materialization_failed',
        'exception_type': type(error).__name__[:120],
        'frames': [{'file': Path(frame.filename).name[:120],
            'function': frame.name[:120], 'line': frame.lineno} for frame in frames]}


def record_failure(*, tenant_id: str, user_id: str, job_id: str,
        lease_token: str, lease_attempt: int, error: Exception) -> bool:
    """Record only the currently authorized worker's failure, without advancing it."""
    try:
        with SessionLocal() as db:
            db.info.update(tenant_id=tenant_id, user_id=user_id)
            permission_service.require_principal(db)
            job = db.scalar(select(AssistantCompilationJob).where(
                AssistantCompilationJob.id == job_id,
                AssistantCompilationJob.tenant_id == tenant_id,
                AssistantCompilationJob.created_by_user_id == user_id,
                AssistantCompilationJob.status == 'running',
                AssistantCompilationJob.lease_token == lease_token,
                AssistantCompilationJob.lease_attempt == lease_attempt,
                AssistantCompilationJob.lease_expires_at > datetime.now(timezone.utc),
            ).with_for_update())
            if job is None:
                return False
            db.add(AssistantAuditLog(tenant_id=tenant_id, user_id=user_id,
                scenario_id=job.scenario_id, thread_id=job.thread_id,
                operation='materialize_model_candidates', status='failed',
                context={'compilation_job_id': job.id, 'lease_attempt': job.lease_attempt},
                result=structural_failure(error)))
            db.commit()
            return True
    except (SQLAlchemyError, HTTPException):
        # A failed diagnostic store cannot authorize or prevent the existing
        # durable recovery path. The original failure remains a safe public code.
        return False
