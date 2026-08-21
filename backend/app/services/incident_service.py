"""P1 运营 Case / Incident 的持久化状态机与审计服务。"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    IncidentCase,
    IncidentCaseHistory,
    OntologyInstance,
    Organization,
    OrganizationMember,
    User,
)
from . import permission_service


VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_STATUSES = {"open", "acknowledged", "resolved"}


class IncidentError(ValueError):
    """Base class for a user-correctable Case operation failure."""


class IncidentValidationError(IncidentError):
    pass


class IncidentConflictError(IncidentError):
    pass


class IncidentPermissionError(IncidentError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise IncidentValidationError(f"{label}必须是文本")
    normalized = value.strip() if required else value
    if required and not normalized:
        raise IncidentValidationError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise IncidentValidationError(f"{label}不能超过 {maximum} 个字符")
    return normalized


def _safe_context(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IncidentValidationError("Case 上下文必须是对象")
    # Copy incoming JSON so later caller-side mutations cannot silently alter
    # the SQLAlchemy JSON value or its history record.
    return copy.deepcopy(value)


def _validate_assignee(db: Session, scenario: BusinessScenario, user_id: str | None) -> str | None:
    if user_id is None:
        return None
    if not isinstance(user_id, str) or not user_id.strip():
        raise IncidentValidationError("负责人不能为空")
    assignee_id = user_id.strip()
    member = db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.organization)
        .join(OrganizationMember.user)
        .where(
            Organization.tenant_id == scenario.tenant_id,
            OrganizationMember.user_id == assignee_id,
            OrganizationMember.status == "active",
            User.status == "active",
        )
        .limit(1)
    ).scalars().first()
    if not member:
        raise IncidentValidationError("负责人必须是当前组织的有效成员")
    return assignee_id


def _validate_related_object(
    db: Session,
    scenario: BusinessScenario,
    object_id: str | None,
) -> str | None:
    if object_id is None:
        return None
    if not isinstance(object_id, str) or not object_id.strip():
        raise IncidentValidationError("关联对象不能为空")
    normalized = object_id.strip()
    instance = db.get(OntologyInstance, normalized)
    if not instance or instance.scenario_id != scenario.id:
        raise IncidentValidationError("关联对象不存在或不属于当前场景")
    if not permission_service.check_object(db, instance, "read").allowed:
        raise IncidentPermissionError("没有读取关联对象的权限")
    return normalized


def _record_history(
    db: Session,
    incident: IncidentCase,
    *,
    action: str,
    actor_user_id: str | None,
    from_status: str = "",
    to_status: str = "",
    changes: Mapping[str, Any] | None = None,
    comment: str = "",
    now: datetime | None = None,
) -> IncidentCaseHistory:
    history = IncidentCaseHistory(
        incident_case_id=incident.id,
        tenant_id=incident.tenant_id,
        scenario_id=incident.scenario_id,
        action=action,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        changes=copy.deepcopy(dict(changes or {})),
        comment=_text(comment, label="审计说明", maximum=2_000),
        created_at=now or utc_now(),
    )
    db.add(history)
    return history


def create_incident(
    db: Session,
    scenario: BusinessScenario,
    data: Mapping[str, Any],
    *,
    actor_user_id: str | None,
    tenant_id: str,
    now: datetime | None = None,
) -> IncidentCase:
    """Create an open Case and its first immutable history item."""
    severity = str(data.get("severity") or "medium")
    if severity not in VALID_SEVERITIES:
        raise IncidentValidationError("严重级别无效")
    title = _text(data.get("title"), label="Case 标题", maximum=300, required=True)
    description = _text(data.get("description"), label="Case 描述", maximum=12_000)
    source = _text(data.get("source") or "manual", label="来源", maximum=60, required=True)
    source_ref = _text(data.get("source_ref"), label="来源引用", maximum=180)
    assignee = _validate_assignee(db, scenario, data.get("assignee_user_id"))
    related_object = _validate_related_object(db, scenario, data.get("related_object_id"))
    context = _safe_context(data.get("context"))
    timestamp = now or utc_now()
    incident = IncidentCase(
        tenant_id=tenant_id,
        scenario_id=scenario.id,
        title=title,
        description=description,
        severity=severity,
        status="open",
        source=source,
        source_ref=source_ref,
        related_object_id=related_object,
        assignee_user_id=assignee,
        context=context,
        created_by_user_id=actor_user_id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(incident)
    db.flush()
    _record_history(
        db,
        incident,
        action="created",
        actor_user_id=actor_user_id,
        to_status="open",
        changes={
            "severity": severity,
            "source": source,
            "source_ref": source_ref,
            "related_object_id": related_object,
            "assignee_user_id": assignee,
        },
        comment=str(data.get("comment") or ""),
        now=timestamp,
    )
    return incident


def update_incident(
    db: Session,
    incident: IncidentCase,
    scenario: BusinessScenario,
    changes: Mapping[str, Any],
    *,
    actor_user_id: str | None,
    comment: str = "",
    now: datetime | None = None,
) -> IncidentCase:
    """Update mutable Case facts while preserving an explicit diff history."""
    if incident.status == "resolved":
        raise IncidentConflictError("已解决的 Case 不能直接修改；请新建后续 Case")
    if incident.status not in VALID_STATUSES:
        raise IncidentConflictError("Case 当前状态无效，不能修改")

    allowed = {"title", "description", "severity", "related_object_id", "assignee_user_id", "context"}
    unknown = set(changes) - allowed
    if unknown:
        raise IncidentValidationError(f"不支持修改字段: {sorted(unknown)[0]}")
    if not changes:
        raise IncidentValidationError("至少提供一个要修改的字段")

    normalized: dict[str, Any] = {}
    if "title" in changes:
        normalized["title"] = _text(changes["title"], label="Case 标题", maximum=300, required=True)
    if "description" in changes:
        normalized["description"] = _text(changes["description"], label="Case 描述", maximum=12_000)
    if "severity" in changes:
        severity = str(changes["severity"] or "")
        if severity not in VALID_SEVERITIES:
            raise IncidentValidationError("严重级别无效")
        normalized["severity"] = severity
    if "related_object_id" in changes:
        normalized["related_object_id"] = _validate_related_object(
            db, scenario, changes["related_object_id"]
        )
    if "assignee_user_id" in changes:
        normalized["assignee_user_id"] = _validate_assignee(
            db, scenario, changes["assignee_user_id"]
        )
    if "context" in changes:
        normalized["context"] = _safe_context(changes["context"])

    diff: dict[str, dict[str, Any]] = {}
    for field, value in normalized.items():
        before = copy.deepcopy(getattr(incident, field))
        if before == value:
            continue
        setattr(incident, field, value)
        diff[field] = {"from": before, "to": copy.deepcopy(value)}
    if not diff:
        raise IncidentValidationError("没有检测到可保存的变更")

    timestamp = now or utc_now()
    incident.updated_at = timestamp
    _record_history(
        db,
        incident,
        action="updated",
        actor_user_id=actor_user_id,
        from_status=incident.status,
        to_status=incident.status,
        changes=diff,
        comment=comment,
        now=timestamp,
    )
    return incident


def acknowledge_incident(
    db: Session,
    incident: IncidentCase,
    *,
    actor_user_id: str | None,
    comment: str = "",
    now: datetime | None = None,
) -> IncidentCase:
    if incident.status == "resolved":
        raise IncidentConflictError("已解决的 Case 不能确认")
    if incident.status == "acknowledged":
        return incident
    if incident.status != "open":
        raise IncidentConflictError("Case 当前状态不能确认")
    timestamp = now or utc_now()
    incident.status = "acknowledged"
    incident.acknowledged_by_user_id = actor_user_id
    incident.acknowledged_at = timestamp
    incident.updated_at = timestamp
    _record_history(
        db,
        incident,
        action="acknowledged",
        actor_user_id=actor_user_id,
        from_status="open",
        to_status="acknowledged",
        comment=comment,
        now=timestamp,
    )
    return incident


def resolve_incident(
    db: Session,
    incident: IncidentCase,
    *,
    actor_user_id: str | None,
    resolution: str,
    comment: str = "",
    now: datetime | None = None,
) -> IncidentCase:
    if incident.status == "resolved":
        return incident
    if incident.status not in {"open", "acknowledged"}:
        raise IncidentConflictError("Case 当前状态不能解决")
    normalized_resolution = _text(resolution, label="解决说明", maximum=12_000, required=True)
    timestamp = now or utc_now()
    prior = incident.status
    incident.status = "resolved"
    incident.resolution = normalized_resolution
    incident.resolved_by_user_id = actor_user_id
    incident.resolved_at = timestamp
    incident.updated_at = timestamp
    _record_history(
        db,
        incident,
        action="resolved",
        actor_user_id=actor_user_id,
        from_status=prior,
        to_status="resolved",
        changes={"resolution": normalized_resolution},
        comment=comment,
        now=timestamp,
    )
    return incident


def list_history(db: Session, incident_id: str) -> list[IncidentCaseHistory]:
    return db.execute(
        select(IncidentCaseHistory)
        .where(IncidentCaseHistory.incident_case_id == incident_id)
        .order_by(IncidentCaseHistory.created_at.asc(), IncidentCaseHistory.id.asc())
    ).scalars().all()
