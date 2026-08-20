"""P1 任务中心 API：查询异步运行、处理人工审批、恢复失败任务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BusinessScenario, WorkflowApprovalRequest, WorkflowRun
from ..schemas import ApprovalDecisionIn, WorkflowApprovalOut, WorkflowRunOut
from ..services import operations_service, permission_service, tenant_service
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/tasks",
    tags=["operations"],
    dependencies=[Depends(get_current_user)],
)


def _run_for_request(db: Session, run_id: str, *, writable: bool = False, verb: str = "read") -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    tenant_service.require_scenario(db, run.scenario_id, writable=writable)
    workflow = run.workflow
    if not workflow:
        raise HTTPException(404, "工作流不存在")
    permission_service.require_workflow_permission(db, workflow, verb)
    return run


def _run_out(db: Session, run: WorkflowRun) -> WorkflowRunOut:
    pending = db.execute(
        select(WorkflowApprovalRequest.id).where(
            WorkflowApprovalRequest.workflow_run_id == run.id,
            WorkflowApprovalRequest.status == "pending",
        ).limit(1)
    ).scalar_one_or_none()
    return WorkflowRunOut(
        id=run.id,
        scenario_id=run.scenario_id,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow.name if run.workflow else "",
        trigger_source=run.trigger_source,
        status=run.status,
        input_params=run.input_params or {},
        attempt=run.attempt,
        max_attempts=run.max_attempts,
        timeout_seconds=run.timeout_seconds,
        available_at=run.available_at,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        completed_at=run.completed_at,
        next_retry_at=run.next_retry_at,
        error=run.error or "",
        result=run.result or {},
        pending_approval=bool(pending),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _approval_out(approval: WorkflowApprovalRequest) -> WorkflowApprovalOut:
    run = approval.workflow_run
    return WorkflowApprovalOut(
        id=approval.id,
        workflow_run_id=run.id,
        scenario_id=approval.scenario_id,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow.name if run.workflow else "",
        node_id=approval.node_id,
        node_name=approval.node_name or "",
        instructions=approval.instructions or "",
        status=approval.status,
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        resolved_at=approval.resolved_at,
        comment=approval.comment or "",
    )


@router.get("", response_model=list[WorkflowRunOut])
def list_tasks(
    scenario_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=80, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = (
        select(WorkflowRun)
        .join(BusinessScenario, BusinessScenario.id == WorkflowRun.scenario_id)
        .where(tenant_service.visible_clause(BusinessScenario, db))
        .order_by(WorkflowRun.created_at.desc())
        .limit(limit)
    )
    if scenario_id:
        tenant_service.require_scenario(db, scenario_id)
        stmt = stmt.where(WorkflowRun.scenario_id == scenario_id)
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    return [
        _run_out(db, run)
        for run in db.execute(stmt).scalars().all()
        if run.workflow and permission_service.check_workflow(db, run.workflow, "read").allowed
    ]


@router.get("/approvals", response_model=list[WorkflowApprovalOut])
def list_approvals(
    scenario_id: str | None = None,
    status: str = "pending",
    limit: int = Query(default=80, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = (
        select(WorkflowApprovalRequest)
        .join(WorkflowRun, WorkflowRun.id == WorkflowApprovalRequest.workflow_run_id)
        .join(BusinessScenario, BusinessScenario.id == WorkflowRun.scenario_id)
        .where(tenant_service.visible_clause(BusinessScenario, db))
        .order_by(WorkflowApprovalRequest.requested_at.asc())
        .limit(limit)
    )
    if scenario_id:
        tenant_service.require_scenario(db, scenario_id)
        stmt = stmt.where(WorkflowApprovalRequest.scenario_id == scenario_id)
    if status:
        stmt = stmt.where(WorkflowApprovalRequest.status == status)
    return [
        _approval_out(approval)
        for approval in db.execute(stmt).scalars().all()
        if approval.workflow_run
        and approval.workflow_run.workflow
        and permission_service.check_workflow(db, approval.workflow_run.workflow, "read").allowed
    ]


@router.get("/{run_id}", response_model=WorkflowRunOut)
def get_task(run_id: str, db: Session = Depends(get_db)):
    return _run_out(db, _run_for_request(db, run_id))


@router.post("/{run_id}/approve", response_model=WorkflowRunOut)
def approve_task(run_id: str, payload: ApprovalDecisionIn, db: Session = Depends(get_db)):
    run = _run_for_request(db, run_id, writable=True, verb="approve")
    try:
        return _run_out(
            db,
            operations_service.decide_approval(
                db,
                run,
                approved=True,
                comment=payload.comment,
                user_id=str(db.info.get("user_id") or "") or None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"审批处理失败: {exc}") from exc


@router.post("/{run_id}/reject", response_model=WorkflowRunOut)
def reject_task(run_id: str, payload: ApprovalDecisionIn, db: Session = Depends(get_db)):
    run = _run_for_request(db, run_id, writable=True, verb="approve")
    try:
        return _run_out(
            db,
            operations_service.decide_approval(
                db,
                run,
                approved=False,
                comment=payload.comment,
                user_id=str(db.info.get("user_id") or "") or None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"审批处理失败: {exc}") from exc


@router.post("/{run_id}/retry", response_model=WorkflowRunOut)
def retry_task(run_id: str, db: Session = Depends(get_db)):
    run = _run_for_request(db, run_id, writable=True, verb="execute")
    try:
        return _run_out(db, operations_service.retry_run(db, run))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"任务不能重试: {exc}") from exc
