"""Explicit project authorization and trusted credentials for system observations."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_access_models import DistillationSystemAccess
from ..distillation_access_schemas import SystemAccessOut, SystemAccessRequest, SystemConfigurationRequest
from ..distillation_schemas import DistillationDocument
from ..distillation_target_schemas import TargetSystem
from . import distillation_access_crypto, distillation_service, permission_service, release_service


def target_hash(target: TargetSystem) -> str:
    canonical = json.dumps(target.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(b"distillation-target-scope/v1\x00" + canonical.encode()).hexdigest()


def _target(project, target_key: str) -> TargetSystem:
    target = next((item for item in DistillationDocument.model_validate(project.document).target_systems if item.key == target_key), None)
    if target is None:
        raise HTTPException(404, "调查目标不存在")
    return target


def latest(db: Session, project, key: str):
    return db.scalar(select(DistillationSystemAccess).where(DistillationSystemAccess.project_id == project.id,
        DistillationSystemAccess.tenant_id == project.tenant_id, DistillationSystemAccess.target_key == key)
        .order_by(DistillationSystemAccess.project_revision.desc()).limit(1))


def _status(grant, target: TargetSystem) -> SystemAccessOut:
    result = SystemAccessOut(target_key=target.key, status="missing")
    if grant is None:
        return result
    result.expires_at, result.authorization_basis = grant.expires_at, grant.authorization_basis
    result.status = "revoked" if grant.revoked_at else "expired" if grant.expires_at <= datetime.now(timezone.utc) else (
        "scope_changed" if grant.target_hash != target_hash(target) else "active")
    return result


def statuses(db: Session, project_id: str) -> list[SystemAccessOut]:
    project = distillation_service.project(db, project_id)
    return [_status(latest(db, project, target.key), target)
        for target in DistillationDocument.model_validate(project.document).target_systems]


def _advance(db: Session, project):
    project.revision += 1
    project.updated_at = datetime.now(timezone.utc)
    project.updated_by = permission_service.require_principal(db).user_id


def authorize(db: Session, project_id: str, target_key: str, payload: SystemAccessRequest):
    project = distillation_service.project(db, project_id, write=True, lock=True)
    distillation_service.assert_revision(project, payload.expected_revision)
    target = _target(project, target_key)
    if target.access_mode != "authorized_readonly" or not target.enabled:
        raise HTTPException(422, "请先保存启用的受保护只读目标范围")
    if (payload.auth_type == "browser") != (target.browser is not None):
        raise HTTPException(422, "访问凭据类型必须匹配系统的浏览器或接口访问方式")
    now = datetime.now(timezone.utc)
    if not now < payload.expires_at <= now + timedelta(days=30):
        raise HTTPException(422, "授权期限必须在未来 30 天内")
    basis = {"content": payload.authorization_basis}
    if release_service.safe_snapshot_content(basis) != basis:
        raise HTTPException(422, "授权依据不能填写密码或令牌")
    grant = DistillationSystemAccess(id=uuid.uuid4().hex, tenant_id=project.tenant_id, project_id=project.id,
        project_revision=project.revision + 1, target_key=target_key, target_hash=target_hash(target),
        auth_type=payload.auth_type, authorization_basis=payload.authorization_basis.strip(),
        expires_at=payload.expires_at, created_by=permission_service.require_principal(db).user_id)
    try:
        grant.credential_envelope = distillation_access_crypto.seal(grant, payload.username, payload.secret.get_secret_value())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _advance(db, project)
    db.add(grant)
    db.flush()
    return project


def revoke(db: Session, project_id: str, target_key: str, expected_revision: int):
    project = distillation_service.project(db, project_id, write=True, lock=True)
    distillation_service.assert_revision(project, expected_revision)
    _target(project, target_key)
    grant = latest(db, project, target_key)
    if grant is not None and not grant.revoked_at:
        grant.revoked_at = datetime.now(timezone.utc)
        _advance(db, project)
        db.flush()
    return project


def current_grant(db: Session, project_id: str, target: TargetSystem):
    project = distillation_service.project(db, project_id, write=True)
    current = _target(project, target.key)
    if target_hash(current) != target_hash(target):
        raise HTTPException(409, "系统调查范围已变化，请重新开始本轮调查")
    grant = latest(db, project, target.key)
    if _status(grant, target).status != "active":
        raise HTTPException(409, "目标系统未获有效只读授权，请在业务系统配置中授权或续期")
    return grant


def read_authorization(db: Session, project_id: str, target: TargetSystem) -> str:
    grant = current_grant(db, project_id, target)
    try:
        return distillation_access_crypto.authorization_header(grant)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def browser_credentials(db: Session, project_id: str, target: TargetSystem) -> dict[str, str]:
    grant = current_grant(db, project_id, target)
    if grant.auth_type != "browser":
        raise HTTPException(409, "请先配置网页登录账号")
    try:
        return distillation_access_crypto.credentials(grant)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def configure(db: Session, project_id: str, payload: SystemConfigurationRequest):
    from ..distillation_schemas import ProjectUpdate

    row = distillation_service.project(db, project_id, write=True, lock=True)
    distillation_service.assert_revision(row, payload.expected_revision)
    document = DistillationDocument.model_validate(row.document)
    if payload.target.access_mode == "authorized_readonly" and payload.target.enabled and payload.credentials is None:
        previous = next((target for target in document.target_systems if target.key == payload.target.key), None)
        if previous is None or target_hash(previous) != target_hash(payload.target):
            raise HTTPException(409, "新增系统或修改访问范围时，请重新填写账号密码并授权")
        current_grant(db, project_id, previous)
    targets = [target for target in document.target_systems if target.key != payload.target.key]
    document.target_systems = [*targets, payload.target]
    row = distillation_service.update_project(db, project_id, ProjectUpdate(expected_revision=row.revision,
        name=row.name, scenario_id=row.scenario_id, document=document))
    if payload.credentials:
        row = authorize(db, project_id, payload.target.key, SystemAccessRequest(
            expected_revision=row.revision, **payload.credentials.model_dump()))
    return row
