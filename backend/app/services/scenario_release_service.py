"""Human commands for immutable scenario releases and audited lifecycle changes."""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, OntologyRelease
from ..release_models import ReleaseLifecycleEvent
from . import release_service


def _audit(db: Session, release: OntologyRelease, actor_id: str, action: str) -> None:
    db.add(ReleaseLifecycleEvent(
        release_id=release.id, tenant_id=release.tenant_id, scenario_id=release.scenario_id,
        snapshot_id=release.snapshot_id, actor_id=actor_id, revision=release.revision,
        action=action,
    ))


def _capture_current(db: Session, scenario: BusinessScenario) -> dict:
    bind = db.get_bind()
    # All graph reads belong to one committed point in time, including concurrent authoring edits.
    with Session(bind=bind, autoflush=False) as reader:
        reader.info.update(db.info)
        reader.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        current, _ = release_service._scenario_for_read(reader, scenario.id)
        return release_service.capture_snapshot_content(reader, current)


def create_release(
    db: Session, scenario_id: str, *, confirmed: bool, name: str = "", notes: str = "",
    branch_id: str | None = None, proposal_id: str | None = None,
    snapshot_id: str | None = None,
) -> OntologyRelease:
    if confirmed is not True:
        raise release_service.ReleaseValidationError("发布必须显式确认")
    scenario, principal = release_service._scenario_for_manage(db, scenario_id)
    scenario = release_service._lock_template_governance_scenario(db, scenario)
    try:
        branch = proposal = None
        if branch_id or proposal_id or snapshot_id:
            branch, snapshot, proposal = release_service._resolve_publish_snapshot(
                db, scenario, branch_id=branch_id, proposal_id=proposal_id, snapshot_id=snapshot_id,
            )
        else:
            snapshot = release_service._create_snapshot(
                db, scenario, branch_id=None, parent_snapshot_id=None, kind="release",
                content=_capture_current(db, scenario),
                created_by_user_id=principal.user_id,
            )
        content = snapshot.content or {}
        release_service._require_snapshot_modeling_provenance(content)
        release_service._validate_snapshot_template_actions(db, scenario, content)
        release_service._require_snapshot_managed_dependencies(db, scenario, content)
        connector_audit = release_service._require_snapshot_connectors(
            db, scenario, content, for_publication=True,
        )
        release = OntologyRelease(
            tenant_id=principal.tenant_id, scenario_id=scenario.id,
            branch_id=branch.id if branch else None, snapshot_id=snapshot.id,
            proposal_id=proposal.id if proposal else None,
            name=release_service._string(name or scenario.name, "发布名称", maximum=160),
            notes=release_service._string(notes, "发布说明", maximum=8_000),
            status="released", enabled=False, revision=1, connector_audit=connector_audit,
            created_by_user_id=principal.user_id,
        )
        db.add(release)
        db.flush()
        _audit(db, release, principal.user_id, "created")
        db.commit()
        db.refresh(release)
        return release
    except Exception:
        db.rollback()
        raise


def change_release(
    db: Session, scenario_id: str, release_id: str, *, expected_revision: int,
    action: Literal["enable", "disable", "retire", "delete"],
) -> OntologyRelease:
    scenario, principal = release_service._scenario_for_manage(db, scenario_id)
    try:
        release_service._lock_template_governance_scenario(db, scenario)
        release = db.scalar(select(OntologyRelease).where(
            OntologyRelease.id == release_id, OntologyRelease.tenant_id == principal.tenant_id,
            OntologyRelease.scenario_id == scenario.id, OntologyRelease.deleted_at.is_(None),
        ).execution_options(populate_existing=True).with_for_update())
        if release is None:
            raise HTTPException(status_code=404, detail="发布不存在")
        if release.revision != expected_revision:
            raise release_service.ReleaseConflictError("发布状态已变更，请刷新后重试")
        if action == "enable":
            if release.status != "released":
                raise release_service.ReleaseConflictError("已退役发布不能重新启用，请创建新发布")
            active = db.scalar(select(OntologyRelease.id).where(
                OntologyRelease.scenario_id == scenario.id, OntologyRelease.enabled.is_(True),
                OntologyRelease.id != release.id,
            ).limit(1))
            if active is not None:
                raise release_service.ReleaseConflictError("该场景已有启用的发布，请先停用后再启用此发布")
            release.enabled = True
        elif action == "disable":
            if release.status != "released":
                raise release_service.ReleaseConflictError("当前发布已退役")
            release.enabled = False
        elif action == "retire":
            if release.status == "retired":
                raise release_service.ReleaseConflictError("当前发布已退役")
            release.enabled = False
            release.status = "retired"
            release.retired_at = release_service._now()
        elif action == "delete":
            if release.status != "retired":
                raise release_service.ReleaseConflictError("请先退役发布，再删除")
            release.deleted_at = release_service._now()
        else:
            raise release_service.ReleaseValidationError("发布操作无效")
        release.revision += 1
        _audit(db, release, principal.user_id, action)
        db.commit()
        db.refresh(release)
        return release
    except Exception:
        db.rollback()
        raise
