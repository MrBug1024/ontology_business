"""Administrative migration controls and gated per-Agent cutover APIs."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..migration_schemas import (
    AgentModeChangeIn,
    MigrationBatchIn,
    MigrationReasonIn,
)
from ..services import agent_migration_service, platform_migration_service
from ..services.auth_service import get_tenant_db


router = APIRouter(tags=["platform-migrations"])


def _call(operation: Callable[[], dict[str, Any]], db: Session) -> dict[str, Any]:
    try:
        result = operation()
        db.commit()
        return result
    except (
        agent_migration_service.AgentMigrationError,
        platform_migration_service.PlatformMigrationError,
    ) as exc:
        db.rollback()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/platform/migrations/legacy-catalog/start")
def start_legacy_catalog_migration(
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(lambda: platform_migration_service.start_catalog_backfill(db), db)


@router.get("/platform/migrations/legacy-catalog")
def legacy_catalog_migration_status(
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(lambda: platform_migration_service.catalog_backfill_status(db), db)


@router.post("/platform/migrations/legacy-catalog/run")
def run_legacy_catalog_migration(
    payload: MigrationBatchIn,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(
        lambda: platform_migration_service.run_catalog_backfill_batch(
            db, batch_size=payload.batch_size
        ),
        db,
    )


@router.post("/platform/migrations/legacy-catalog/pause")
def pause_legacy_catalog_migration(
    payload: MigrationReasonIn,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(
        lambda: platform_migration_service.pause_catalog_backfill(
            db, reason=payload.reason
        ),
        db,
    )


@router.post("/platform/migrations/legacy-catalog/resume")
def resume_legacy_catalog_migration(
    payload: MigrationReasonIn,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(
        lambda: platform_migration_service.resume_catalog_backfill(
            db, reason=payload.reason
        ),
        db,
    )


@router.post("/platform/migrations/legacy-catalog/retry")
def retry_legacy_catalog_migration(
    payload: MigrationReasonIn,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(
        lambda: platform_migration_service.retry_catalog_backfill(
            db, reason=payload.reason
        ),
        db,
    )


@router.get("/agents/{agent_id}/migration")
def get_agent_migration(
    agent_id: str,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(lambda: agent_migration_service.agent_migration_status(db, agent_id), db)


@router.post("/agents/{agent_id}/migration/mode")
def migrate_agent_mode(
    agent_id: str,
    payload: AgentModeChangeIn,
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    return _call(
        lambda: agent_migration_service.change_agent_mode(
            db,
            agent_id,
            target_mode=payload.target_mode,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        ),
        db,
    )

__all__ = ["router"]
