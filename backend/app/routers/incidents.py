"""P1 场景范围的 Incident / Case 运营闭环 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BusinessScenario, IncidentCase, IncidentCaseHistory
from ..schemas import (
    IncidentCaseAcknowledgeIn,
    IncidentCaseCreateIn,
    IncidentCaseHistoryOut,
    IncidentCaseOut,
    IncidentCaseResolveIn,
    IncidentCaseUpdateIn,
)
from ..services import incident_service, permission_service, tenant_service
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/incidents",
    tags=["incidents"],
    dependencies=[Depends(get_current_user)],
)


def _scenario_for_incidents(db: Session, scenario_id: str, *, verb: str) -> BusinessScenario:
    # Cases are operational records, never public-scenario content.  Requiring
    # owned access even for reads prevents a public scenario from exposing its
    # incident history to another tenant.
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, verb)
    return scenario


def _incident_for_request(
    db: Session,
    incident_id: str,
    *,
    verb: str,
) -> tuple[IncidentCase, BusinessScenario]:
    incident = db.execute(
        select(IncidentCase).where(
            IncidentCase.id == incident_id,
            IncidentCase.tenant_id == tenant_service.current_tenant_id(db),
        )
    ).scalars().first()
    if not incident:
        # Do not disclose the existence of a Case in another tenant.
        raise HTTPException(status_code=404, detail="Case 不存在")
    return incident, _scenario_for_incidents(db, incident.scenario_id, verb=verb)


def _incident_out(incident: IncidentCase) -> IncidentCaseOut:
    return IncidentCaseOut(
        id=incident.id,
        tenant_id=incident.tenant_id,
        scenario_id=incident.scenario_id,
        title=incident.title,
        description=incident.description or "",
        severity=incident.severity,
        status=incident.status,
        source=incident.source or "manual",
        source_ref=incident.source_ref or "",
        related_object_id=incident.related_object_id,
        assignee_user_id=incident.assignee_user_id,
        context=incident.context or {},
        created_by_user_id=incident.created_by_user_id,
        acknowledged_by_user_id=incident.acknowledged_by_user_id,
        acknowledged_at=incident.acknowledged_at,
        resolved_by_user_id=incident.resolved_by_user_id,
        resolved_at=incident.resolved_at,
        resolution=incident.resolution or "",
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        history_count=len(incident.histories),
    )


def _history_out(history: IncidentCaseHistory) -> IncidentCaseHistoryOut:
    return IncidentCaseHistoryOut(
        id=history.id,
        incident_case_id=history.incident_case_id,
        tenant_id=history.tenant_id,
        scenario_id=history.scenario_id,
        action=history.action,
        actor_user_id=history.actor_user_id,
        from_status=history.from_status or "",
        to_status=history.to_status or "",
        changes=history.changes or {},
        comment=history.comment or "",
        created_at=history.created_at,
    )


def _raise_incident_error(exc: Exception) -> None:
    if isinstance(exc, incident_service.IncidentPermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, incident_service.IncidentConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, incident_service.IncidentValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/scenarios/{scenario_id}", response_model=list[IncidentCaseOut])
def list_incidents(
    scenario_id: str,
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _scenario_for_incidents(db, scenario_id, verb="read")
    if status and status not in incident_service.VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Case 状态无效")
    if severity and severity not in incident_service.VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail="Case 严重级别无效")
    stmt = (
        select(IncidentCase)
        .where(
            IncidentCase.tenant_id == tenant_service.current_tenant_id(db),
            IncidentCase.scenario_id == scenario_id,
        )
        .order_by(IncidentCase.updated_at.desc(), IncidentCase.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(IncidentCase.status == status)
    if severity:
        stmt = stmt.where(IncidentCase.severity == severity)
    return [_incident_out(incident) for incident in db.execute(stmt).scalars().all()]


@router.post("/scenarios/{scenario_id}", response_model=IncidentCaseOut, status_code=201)
def create_incident(
    scenario_id: str,
    payload: IncidentCaseCreateIn,
    db: Session = Depends(get_db),
):
    scenario = _scenario_for_incidents(db, scenario_id, verb="write")
    principal = permission_service.require_principal(db)
    try:
        incident = incident_service.create_incident(
            db,
            scenario,
            payload.model_dump(),
            actor_user_id=principal.user_id,
            tenant_id=tenant_service.current_tenant_id(db),
        )
        db.commit()
        db.refresh(incident)
        return _incident_out(incident)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_incident_error(exc)


@router.get("/{incident_id}", response_model=IncidentCaseOut)
def get_incident(incident_id: str, db: Session = Depends(get_db)):
    incident, _ = _incident_for_request(db, incident_id, verb="read")
    return _incident_out(incident)


@router.patch("/{incident_id}", response_model=IncidentCaseOut)
def update_incident(
    incident_id: str,
    payload: IncidentCaseUpdateIn,
    db: Session = Depends(get_db),
):
    incident, scenario = _incident_for_request(db, incident_id, verb="write")
    principal = permission_service.require_principal(db)
    data = payload.model_dump(exclude_unset=True)
    comment = str(data.pop("comment", ""))
    try:
        incident_service.update_incident(
            db,
            incident,
            scenario,
            data,
            actor_user_id=principal.user_id,
            comment=comment,
        )
        db.commit()
        db.refresh(incident)
        return _incident_out(incident)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_incident_error(exc)


@router.post("/{incident_id}/acknowledge", response_model=IncidentCaseOut)
def acknowledge_incident(
    incident_id: str,
    payload: IncidentCaseAcknowledgeIn,
    db: Session = Depends(get_db),
):
    incident, _ = _incident_for_request(db, incident_id, verb="write")
    principal = permission_service.require_principal(db)
    try:
        incident_service.acknowledge_incident(
            db,
            incident,
            actor_user_id=principal.user_id,
            comment=payload.comment,
        )
        db.commit()
        db.refresh(incident)
        return _incident_out(incident)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_incident_error(exc)


@router.post("/{incident_id}/resolve", response_model=IncidentCaseOut)
def resolve_incident(
    incident_id: str,
    payload: IncidentCaseResolveIn,
    db: Session = Depends(get_db),
):
    incident, _ = _incident_for_request(db, incident_id, verb="write")
    principal = permission_service.require_principal(db)
    try:
        incident_service.resolve_incident(
            db,
            incident,
            actor_user_id=principal.user_id,
            resolution=payload.resolution,
            comment=payload.comment,
        )
        db.commit()
        db.refresh(incident)
        return _incident_out(incident)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_incident_error(exc)


@router.get("/{incident_id}/history", response_model=list[IncidentCaseHistoryOut])
def incident_history(incident_id: str, db: Session = Depends(get_db)):
    incident, _ = _incident_for_request(db, incident_id, verb="read")
    return [_history_out(item) for item in incident_service.list_history(db, incident.id)]
