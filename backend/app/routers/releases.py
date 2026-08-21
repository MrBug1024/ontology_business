"""P2 本体发布治理 API。

治理读取和写入均经由 ``release_service`` 的组织/租户校验。路由层只负责将受控
领域错误映射为稳定 HTTP 契约，并且永远使用脱敏快照输出函数，避免把历史 JSON
中的任何凭据带回客户端。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    BusinessScenario,
    OntologyBranch,
    OntologyProposal,
    OntologyRelease,
    OntologyReview,
    OntologyRollback,
    OntologySnapshot,
)
from ..schemas import (
    ReleaseBranchCreateIn,
    ReleaseBranchOut,
    ReleaseConfirmIn,
    ReleaseProposalCreateIn,
    ReleaseProposalOut,
    ReleasePublishIn,
    ReleaseRecordOut,
    ReleaseReviewCreateIn,
    ReleaseReviewOut,
    ReleaseRollbackIn,
    ReleaseRollbackOut,
    ReleaseSnapshotOut,
    ScenarioOut,
)
from ..services import permission_service, release_service
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/releases",
    tags=["releases"],
    dependencies=[Depends(get_current_user)],
)


def _release_error(exc: Exception) -> None:
    if isinstance(exc, release_service.ReleaseConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, release_service.ReleaseValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _branch_out(branch: OntologyBranch) -> ReleaseBranchOut:
    return ReleaseBranchOut(
        id=branch.id,
        tenant_id=branch.tenant_id,
        scenario_id=branch.scenario_id,
        name=branch.name,
        description=branch.description or "",
        status=branch.status,
        base_snapshot_id=branch.base_snapshot_id,
        head_snapshot_id=branch.head_snapshot_id,
        created_by_user_id=branch.created_by_user_id,
        created_at=branch.created_at,
        updated_at=branch.updated_at,
    )


def _scenario_out(scenario: BusinessScenario) -> ScenarioOut:
    """Minimal owned-scenario projection for the governance workspace.

    Generic scenario discovery may include public resources from other tenants;
    release branches and review history intentionally never do.  The count fields
    retain their schema defaults because the selector needs only safe identity and
    display metadata.
    """
    return ScenarioOut(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description or "",
        industry=scenario.industry or "",
        status=scenario.status or "draft",
        created_at=scenario.created_at,
        updated_at=scenario.updated_at,
    )


def _snapshot_out(snapshot: OntologySnapshot) -> ReleaseSnapshotOut:
    return ReleaseSnapshotOut(
        id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        scenario_id=snapshot.scenario_id,
        branch_id=snapshot.branch_id,
        parent_snapshot_id=snapshot.parent_snapshot_id,
        kind=snapshot.kind,
        content_hash=snapshot.content_hash,
        # A second, response-time redact is intentional.  Never use model_validate here.
        content=release_service.safe_snapshot_content(snapshot.content),
        created_by_user_id=snapshot.created_by_user_id,
        created_at=snapshot.created_at,
    )


def _review_out(review: OntologyReview) -> ReleaseReviewOut:
    return ReleaseReviewOut(
        id=review.id,
        proposal_id=review.proposal_id,
        reviewer_user_id=review.reviewer_user_id,
        decision=review.decision,
        comment=review.comment or "",
        created_at=review.created_at,
    )


def _proposal_out(db: Session, proposal: OntologyProposal) -> ReleaseProposalOut:
    snapshot = db.get(OntologySnapshot, proposal.proposed_snapshot_id)
    reviews = release_service.list_reviews(db, proposal.id)
    return ReleaseProposalOut(
        id=proposal.id,
        tenant_id=proposal.tenant_id,
        scenario_id=proposal.scenario_id,
        branch_id=proposal.branch_id,
        base_snapshot_id=proposal.base_snapshot_id,
        proposed_snapshot_id=proposal.proposed_snapshot_id,
        pre_merge_snapshot_id=proposal.pre_merge_snapshot_id,
        merged_snapshot_id=proposal.merged_snapshot_id,
        title=proposal.title,
        description=proposal.description or "",
        status=proposal.status,
        created_by_user_id=proposal.created_by_user_id,
        submitted_at=proposal.submitted_at,
        merged_at=proposal.merged_at,
        merged_by_user_id=proposal.merged_by_user_id,
        content=release_service.safe_snapshot_content(snapshot.content if snapshot else {}),
        reviews=[_review_out(review) for review in reviews],
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _release_out(release: OntologyRelease) -> ReleaseRecordOut:
    return ReleaseRecordOut(
        id=release.id,
        tenant_id=release.tenant_id,
        scenario_id=release.scenario_id,
        branch_id=release.branch_id,
        snapshot_id=release.snapshot_id,
        proposal_id=release.proposal_id,
        environment=release.environment,
        status=release.status,
        notes=release.notes or "",
        connector_audit=release.connector_audit or [],
        created_by_user_id=release.created_by_user_id,
        created_at=release.created_at,
    )


def _rollback_out(rollback: OntologyRollback) -> ReleaseRollbackOut:
    return ReleaseRollbackOut(
        id=rollback.id,
        tenant_id=rollback.tenant_id,
        scenario_id=rollback.scenario_id,
        branch_id=rollback.branch_id,
        from_snapshot_id=rollback.from_snapshot_id,
        target_snapshot_id=rollback.target_snapshot_id,
        result_snapshot_id=rollback.result_snapshot_id,
        environment=rollback.environment,
        reason=rollback.reason or "",
        connector_audit=rollback.connector_audit or [],
        created_by_user_id=rollback.created_by_user_id,
        created_at=rollback.created_at,
    )


@router.get("/scenarios", response_model=list[ScenarioOut])
def list_governed_scenarios(db: Session = Depends(get_db)):
    """List only current-tenant scenarios usable in release governance.

    Public scenarios from another tenant remain readable through the ordinary
    catalog but never appear as a selectable release target.
    """
    principal = permission_service.require_principal(db)
    scenarios = db.execute(
        select(BusinessScenario)
        .where(BusinessScenario.tenant_id == principal.tenant_id)
        .order_by(BusinessScenario.updated_at.desc(), BusinessScenario.name.asc())
    ).scalars().all()
    return [
        _scenario_out(scenario)
        for scenario in scenarios
        if permission_service.check_scenario(db, scenario, "read").allowed
    ]


@router.get("/scenarios/{scenario_id}/branches", response_model=list[ReleaseBranchOut])
def list_branches(scenario_id: str, db: Session = Depends(get_db)):
    try:
        return [_branch_out(branch) for branch in release_service.list_branches(db, scenario_id)]
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/scenarios/{scenario_id}/branches", response_model=ReleaseBranchOut)
def create_branch(
    scenario_id: str,
    payload: ReleaseBranchCreateIn,
    db: Session = Depends(get_db),
):
    try:
        return _branch_out(
            release_service.create_branch(
                db,
                scenario_id,
                name=payload.name,
                description=payload.description,
            )
        )
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/branches/{branch_id}", response_model=ReleaseBranchOut)
def get_branch(branch_id: str, db: Session = Depends(get_db)):
    try:
        return _branch_out(release_service.get_branch(db, branch_id))
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/snapshots/{snapshot_id}", response_model=ReleaseSnapshotOut)
def get_snapshot(snapshot_id: str, db: Session = Depends(get_db)):
    try:
        return _snapshot_out(release_service.get_snapshot(db, snapshot_id))
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/branches/{branch_id}/proposals", response_model=ReleaseProposalOut)
def create_proposal(
    branch_id: str,
    payload: ReleaseProposalCreateIn,
    db: Session = Depends(get_db),
):
    try:
        proposal = release_service.create_proposal(
            db,
            branch_id,
            title=payload.title,
            description=payload.description,
            content=payload.content,
            submit=payload.submit,
        )
        return _proposal_out(db, proposal)
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/scenarios/{scenario_id}/proposals", response_model=list[ReleaseProposalOut])
def list_proposals(
    scenario_id: str,
    branch_id: str | None = Query(default=None, min_length=1, max_length=32),
    status: str | None = Query(default=None, min_length=1, max_length=20),
    db: Session = Depends(get_db),
):
    try:
        return [
            _proposal_out(db, proposal)
            for proposal in release_service.list_proposals(
                db, scenario_id, branch_id=branch_id, status=status
            )
        ]
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/proposals/{proposal_id}", response_model=ReleaseProposalOut)
def get_proposal(proposal_id: str, db: Session = Depends(get_db)):
    try:
        return _proposal_out(db, release_service.get_proposal(db, proposal_id))
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/proposals/{proposal_id}/submit", response_model=ReleaseProposalOut)
def submit_proposal(proposal_id: str, db: Session = Depends(get_db)):
    """Move an immutable draft into the independent-review queue."""
    try:
        return _proposal_out(db, release_service.submit_proposal(db, proposal_id))
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/proposals/{proposal_id}/reviews", response_model=list[ReleaseReviewOut])
def list_reviews(proposal_id: str, db: Session = Depends(get_db)):
    try:
        return [_review_out(review) for review in release_service.list_reviews(db, proposal_id)]
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/proposals/{proposal_id}/reviews", response_model=ReleaseReviewOut)
def create_review(
    proposal_id: str,
    payload: ReleaseReviewCreateIn,
    db: Session = Depends(get_db),
):
    try:
        return _review_out(
            release_service.create_review(
                db,
                proposal_id,
                decision=payload.decision,
                comment=payload.comment,
            )
        )
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/proposals/{proposal_id}/merge", response_model=ReleaseProposalOut)
def merge_proposal(
    proposal_id: str,
    payload: ReleaseConfirmIn,
    db: Session = Depends(get_db),
):
    try:
        proposal = release_service.merge_proposal(
            db,
            proposal_id,
            confirmed=payload.confirmed,
            note=payload.note,
        )
        return _proposal_out(db, proposal)
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.get("/scenarios/{scenario_id}/publish", response_model=list[ReleaseRecordOut])
def list_release_records(
    scenario_id: str,
    environment: str | None = Query(default=None, min_length=1, max_length=20),
    db: Session = Depends(get_db),
):
    try:
        return [
            _release_out(release)
            for release in release_service.list_releases(
                db, scenario_id, environment=environment
            )
        ]
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/scenarios/{scenario_id}/publish", response_model=ReleaseRecordOut)
def publish_snapshot(
    scenario_id: str,
    payload: ReleasePublishIn,
    db: Session = Depends(get_db),
):
    try:
        return _release_out(
            release_service.publish_snapshot(
                db,
                scenario_id,
                environment=payload.environment,
                confirmed=payload.confirmed,
                branch_id=payload.branch_id,
                proposal_id=payload.proposal_id,
                snapshot_id=payload.snapshot_id,
                notes=payload.notes,
            )
        )
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)


@router.post("/scenarios/{scenario_id}/rollback", response_model=ReleaseRollbackOut)
def rollback_snapshot(
    scenario_id: str,
    payload: ReleaseRollbackIn,
    db: Session = Depends(get_db),
):
    try:
        return _rollback_out(
            release_service.rollback_snapshot(
                db,
                scenario_id,
                target_snapshot_id=payload.target_snapshot_id,
                confirmed=payload.confirmed,
                branch_id=payload.branch_id,
                environment=payload.environment,
                reason=payload.reason,
            )
        )
    except (release_service.ReleaseValidationError, release_service.ReleaseConflictError) as exc:
        _release_error(exc)
