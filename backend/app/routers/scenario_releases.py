"""Authenticated HTTP commands for manually managed scenario releases."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, OntologyRelease, User
from ..scenario_release_schemas import ScenarioReleaseChange, ScenarioReleaseCreate, ScenarioReleaseOut, ScenarioReleasePage
from ..services import permission_service, release_service, scenario_release_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/scenario-releases", tags=["scenario-releases"])


def _out(db: Session, release: OntologyRelease, scenario: BusinessScenario) -> ScenarioReleaseOut:
    creator = db.get(User, release.created_by_user_id) if release.created_by_user_id else None
    return ScenarioReleaseOut(
        id=release.id, scenario_id=scenario.id, scenario_name=scenario.name,
        name=release.name, notes=release.notes or "", enabled=release.enabled,
        status=release.status, revision=release.revision, created_at=release.created_at,
        created_by_user_id=release.created_by_user_id,
        created_by_name=str((creator.display_name or creator.email) if creator else "历史记录"),
        retired_at=release.retired_at, deleted_at=release.deleted_at,
        can_manage=(scenario.status != "retired" and permission_service.check_tenant_permission(db, "manage").allowed),
    )


@router.get("", response_model=ScenarioReleasePage)
def list_releases(
    scenario_id: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000),
    db: Session = Depends(get_tenant_db),
) -> ScenarioReleasePage:
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "read")
    statement = select(OntologyRelease, BusinessScenario).join(
        BusinessScenario, BusinessScenario.id == OntologyRelease.scenario_id,
    ).where(
        OntologyRelease.tenant_id == principal.tenant_id,
        BusinessScenario.tenant_id == principal.tenant_id,
        OntologyRelease.deleted_at.is_(None),
    )
    if scenario_id:
        release_service._scenario_for_read(db, scenario_id)
        statement = statement.where(OntologyRelease.scenario_id == scenario_id)
    rows = db.execute(statement.order_by(OntologyRelease.created_at.desc(), OntologyRelease.id.desc()).offset(offset).limit(limit + 1)).all()
    return ScenarioReleasePage(
        items=[_out(db, release, scenario) for release, scenario in rows[:limit]
               if permission_service.check_scenario(db, scenario, "read").allowed],
        limit=limit, offset=offset, has_more=len(rows) > limit,
    )


@router.post("", response_model=ScenarioReleaseOut, status_code=status.HTTP_201_CREATED)
def create_release(payload: ScenarioReleaseCreate, db: Session = Depends(get_tenant_db)) -> ScenarioReleaseOut:
    try:
        release = scenario_release_service.create_release(db, **payload.model_dump())
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scenario, _ = release_service._scenario_for_read(db, release.scenario_id)
    return _out(db, release, scenario)


@router.patch("/{release_id}", response_model=ScenarioReleaseOut)
def change_release(
    release_id: str, payload: ScenarioReleaseChange, db: Session = Depends(get_tenant_db),
) -> ScenarioReleaseOut:
    principal = permission_service.require_principal(db)
    release = db.scalar(select(OntologyRelease).where(
        OntologyRelease.id == release_id, OntologyRelease.tenant_id == principal.tenant_id,
        OntologyRelease.deleted_at.is_(None),
    ))
    if release is None:
        raise HTTPException(status_code=404, detail="发布不存在")
    try:
        release = scenario_release_service.change_release(db, release.scenario_id, release.id, **payload.model_dump())
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scenario, _ = release_service._scenario_for_read(db, release.scenario_id)
    return _out(db, release, scenario)
