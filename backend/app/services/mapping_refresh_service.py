"""持久化的数据映射刷新队列。

HTTP 请求只校验权限和记录刷新意图；外部数据源读取及对象/关系写入统一在
worker 中完成。任务携带环境快照与映射定义指纹，避免一个环境的 worker 使用另一
环境的数据源，或在映射已编辑后继续执行旧定义。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BusinessScenario, DataMapping, DataMappingRefreshJob
from . import (
    connector_service,
    ontology_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)
from .policies import PolicyViolation


ACTIVE_STATUSES = {"queued", "running", "retry_waiting"}
DISPATCHABLE_STATUSES = {"queued", "retry_waiting"}
TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}
MAPPING_REFRESH_MAX_ATTEMPTS = 3
MAPPING_REFRESH_TIMEOUT_SECONDS = 300
MAPPING_REFRESH_RETRY_SECONDS = 5
MAPPING_REFRESH_LIMIT_MIN = 1
MAPPING_REFRESH_LIMIT_MAX = 500


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


def bounded_limit(value: Any, *, default: int = 50) -> int:
    """Parse the bounded per-job batch size without allowing accidental full scans."""
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("刷新行数上限必须是整数") from exc
    return max(MAPPING_REFRESH_LIMIT_MIN, min(parsed, MAPPING_REFRESH_LIMIT_MAX))


def _mapping_snapshot_value(mapping: Any, field: str, default: Any = "") -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(field, default)
    return getattr(mapping, field, default)


def mapping_snapshot(mapping: Any) -> dict[str, Any]:
    """Capture the credential-free mapping contract a job is allowed to run."""
    raw_binding_ref = _mapping_snapshot_value(mapping, "data_source_binding_ref", {})
    if raw_binding_ref is None:
        raw_binding_ref = {}
    raw_column_map = _mapping_snapshot_value(mapping, "column_map", {})
    if raw_column_map is None:
        raw_column_map = {}
    if not isinstance(raw_column_map, Mapping):
        raise PolicyViolation("映射字段配置无效，无法创建刷新快照")
    raw_transform_rules = _mapping_snapshot_value(mapping, "transform_rules", {})
    if raw_transform_rules is None:
        raw_transform_rules = {}
    if not isinstance(raw_transform_rules, Mapping):
        raise PolicyViolation("映射转换规则无效，无法创建刷新快照")
    config = {
        "data_source_id": str(_mapping_snapshot_value(mapping, "data_source_id", "") or ""),
        "data_source_binding_key": _mapping_snapshot_value(
            mapping, "data_source_binding_key", ""
        ) or "",
        "data_source_binding_ref": raw_binding_ref,
    }
    try:
        binding = connector_service.runtime_binding_from_config(config, "data_source")
    except connector_service.ConnectorBindingError as exc:
        raise PolicyViolation(f"映射运行时绑定配置无效: {exc}") from exc
    if binding is None:
        config["data_source_binding_key"] = ""
        config["data_source_binding_ref"] = {}
    else:
        config["data_source_binding_key"] = binding["binding_key"]
        config["data_source_binding_ref"] = connector_service.with_required_capabilities(
            binding["reference"], "sql_read"
        )
    mapping_id = str(_mapping_snapshot_value(mapping, "id", "") or "").strip()
    scenario_id = str(_mapping_snapshot_value(mapping, "scenario_id", "") or "").strip()
    entity_id = str(_mapping_snapshot_value(mapping, "entity_id", "") or "").strip()
    if not mapping_id or not scenario_id or not entity_id or not config["data_source_id"]:
        raise PolicyViolation("映射定义不完整，无法创建刷新快照")
    return {
        "id": mapping_id,
        "scenario_id": scenario_id,
        "entity_id": entity_id,
        "data_source_id": config["data_source_id"],
        "data_source_binding_key": config["data_source_binding_key"],
        "data_source_binding_ref": copy.deepcopy(config["data_source_binding_ref"]),
        "table_name": str(_mapping_snapshot_value(mapping, "table_name", "") or ""),
        "column_map": copy.deepcopy(dict(raw_column_map)),
        "transform_rules": copy.deepcopy(dict(raw_transform_rules)),
    }


def _mapping_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def frozen_mapping(snapshot: Any) -> SimpleNamespace:
    """Build a small detached mapping DTO from a persisted job/release snapshot."""
    if not isinstance(snapshot, Mapping):
        raise PolicyViolation("映射刷新定义快照无效")
    normalized = mapping_snapshot(snapshot)
    return SimpleNamespace(**normalized)


def mapping_runtime_config(mapping: Any) -> dict[str, Any]:
    """Expose only the connector fields accepted by the runtime resolver."""
    binding_ref = getattr(mapping, "data_source_binding_ref", None)
    if binding_ref is None:
        binding_ref = {}
    config = {
        "data_source_id": mapping.data_source_id,
        "data_source_binding_key": getattr(mapping, "data_source_binding_key", "") or "",
        # Keep malformed legacy JSON visible to the resolver so it fails closed
        # with a binding-config error instead of silently treating it as empty.
        "data_source_binding_ref": binding_ref,
    }
    try:
        metadata = connector_service.runtime_binding_from_config(config, "data_source")
    except connector_service.ConnectorBindingError:
        # Preserve malformed values so the runtime resolver can return its
        # normal fail-closed diagnostic rather than silently treating them as
        # an unbound legacy mapping.
        return config
    if metadata is not None:
        config["data_source_binding_key"] = metadata["binding_key"]
        config["data_source_binding_ref"] = connector_service.with_required_capabilities(
            metadata["reference"], "sql_read"
        )
    return config


def mapping_fingerprint(mapping: Any) -> str:
    """Fingerprint the normalized snapshot a queued job is allowed to execute."""
    return _mapping_snapshot_fingerprint(mapping_snapshot(mapping))


def relation_mapping_fingerprint(
    definition: runtime_definition_service.RuntimeDefinition,
    mapping_id: str,
) -> str:
    """Pin every relation binding and endpoint definition touched by a job."""
    related = [
        item
        for item in definition.relation_mappings.values()
        if str(mapping_id) in {
            str(getattr(item, "source_mapping_id", "") or ""),
            str(getattr(item, "target_mapping_id", "") or ""),
        }
    ]
    relation_fields = (
        "id", "relation_id", "source_mapping_id", "target_mapping_id", "mode",
        "data_source_id", "data_source_binding_key", "data_source_binding_ref",
        "table_name", "foreign_key_column", "source_key_column", "target_key_column",
    )
    payload: list[dict[str, Any]] = []
    for item in sorted(related, key=lambda value: str(value.id)):
        endpoint_ids = {
            str(item.source_mapping_id), str(item.target_mapping_id)
        }
        endpoints = [
            mapping_snapshot(definition.mappings[endpoint_id])
            for endpoint_id in sorted(endpoint_ids)
            if endpoint_id in definition.mappings
        ]
        relation = definition.relations.get(str(item.relation_id))
        payload.append(
            {
                "mapping": {
                    field: copy.deepcopy(getattr(item, field, None))
                    for field in relation_fields
                },
                "endpoints": endpoints,
                "relation": {
                    field: copy.deepcopy(getattr(relation, field, None))
                    for field in (
                        "id", "source_entity_id", "target_entity_id",
                        "relation_type", "constraints",
                    )
                } if relation is not None else None,
            }
        )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_mapping_runtime_definition(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
    *,
    environment: str | None = None,
) -> tuple[Any, runtime_definition_service.RuntimeDefinition]:
    """Resolve the mapping contract authorised for this deployment.

    In staging/prod the live ``DataMapping`` is used only to authorise the
    route and locate its scenario.  The returned mapping is a detached DTO from
    the active release snapshot, so API requests and queued work cannot read a
    newer dev definition by accident.
    """
    resolved_environment = runtime_connector_service.runtime_environment(environment)
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=resolved_environment,
        )
        if not definition.is_frozen:
            return mapping, definition
        released_mapping = runtime_definition_service.resolve_resource(
            definition,
            "mapping",
            mapping.id,
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation(str(exc)) from exc
    try:
        frozen = frozen_mapping(mapping_snapshot(released_mapping))
        frozen.entity = getattr(released_mapping, "entity", None)
        return frozen, definition
    except PolicyViolation:
        raise
    except Exception as exc:  # noqa: BLE001 - snapshot JSON is untrusted at this boundary.
        raise PolicyViolation("发布映射定义无效，已阻止运行") from exc


def _job_mapping_snapshot(job: DataMappingRefreshJob) -> SimpleNamespace:
    frozen = frozen_mapping(job.mapping_snapshot)
    if frozen.id != job.mapping_id:
        raise PolicyViolation("映射刷新快照与任务映射不一致")
    if frozen.scenario_id != job.scenario_id:
        raise PolicyViolation("映射刷新快照不属于任务场景")
    if not job.mapping_fingerprint or _mapping_snapshot_fingerprint(vars(frozen)) != job.mapping_fingerprint:
        raise PolicyViolation("映射刷新定义快照完整性校验失败")
    return frozen


def _job_runtime_mapping(
    db: Session,
    job: DataMappingRefreshJob,
    scenario: BusinessScenario,
) -> SimpleNamespace:
    """Validate a job's provenance and return only its frozen mapping DTO."""
    frozen = _job_mapping_snapshot(job)
    if frozen.scenario_id != scenario.id:
        raise PolicyViolation("映射刷新快照不属于当前业务场景")
    if job.environment == "dev":
        if (
            job.definition_source != "live"
            or job.definition_snapshot_id is not None
            or job.release_id is not None
        ):
            raise PolicyViolation("开发环境映射刷新任务的定义来源无效")
        return frozen
    if job.definition_source != "release":
        raise PolicyViolation("非开发环境映射刷新缺少发布定义来源")
    try:
        definition = runtime_definition_service.resolve_pinned(
            db,
            scenario,
            environment=job.environment,
            snapshot_id=job.definition_snapshot_id,
            release_id=job.release_id,
            definition_hash=job.definition_hash,
        )
        released_mapping = runtime_definition_service.resolve_resource(
            definition,
            "mapping",
            job.mapping_id,
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation(str(exc)) from exc
    released_snapshot = mapping_snapshot(released_mapping)
    if _mapping_snapshot_fingerprint(released_snapshot) != job.mapping_fingerprint:
        raise PolicyViolation("映射刷新快照与固定发布定义不一致")
    return frozen


def _job_runtime_definition(
    db: Session,
    job: DataMappingRefreshJob,
    scenario: BusinessScenario,
) -> runtime_definition_service.RuntimeDefinition:
    """Resolve the complete definition pinned by a refresh job."""
    try:
        if job.environment == "dev":
            definition = runtime_definition_service.resolve_active(
                db, scenario, environment="dev"
            )
            if definition.is_frozen or job.definition_source != "live":
                raise PolicyViolation("开发环境映射刷新任务的定义来源无效")
            if (
                not job.definition_hash
                or definition.definition_hash != job.definition_hash
                or
                not job.relation_mapping_fingerprint
                or relation_mapping_fingerprint(definition, job.mapping_id)
                != job.relation_mapping_fingerprint
            ):
                raise PolicyViolation("关系映射或其端点定义已变化，请重新提交刷新")
            return definition
        if job.definition_source != "release":
            raise PolicyViolation("非开发环境映射刷新缺少发布定义来源")
        definition = runtime_definition_service.resolve_pinned(
            db,
            scenario,
            environment=job.environment,
            snapshot_id=job.definition_snapshot_id,
            release_id=job.release_id,
            definition_hash=job.definition_hash,
        )
        if (
            not job.relation_mapping_fingerprint
            or relation_mapping_fingerprint(definition, job.mapping_id)
            != job.relation_mapping_fingerprint
        ):
            raise PolicyViolation("固定发布中的关系映射定义与刷新任务不一致")
        return definition
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation(str(exc)) from exc


def _live_mapping_matches_job(mapping: DataMapping | None, job: DataMappingRefreshJob) -> bool:
    if mapping is None or mapping.id != job.mapping_id or mapping.scenario_id != job.scenario_id:
        return False
    try:
        return mapping_fingerprint(mapping) == job.mapping_fingerprint
    except PolicyViolation:
        return False


def _clear_stale_mapping_runtime_state(
    mapping: DataMapping | None,
    job: DataMappingRefreshJob,
) -> None:
    """Do not leave a newer live definition visually owned by an old job."""
    if (
        mapping is not None
        and mapping.id == job.mapping_id
        and mapping.scenario_id == job.scenario_id
        and not _live_mapping_matches_job(mapping, job)
    ):
        set_mapping_runtime_state(
            mapping,
            environment=job.environment,
            status="unknown",
        )


def mapping_matches_snapshot(mapping: DataMapping, snapshot: Any) -> bool:
    """Whether a live mapping can safely receive state from a frozen view."""
    try:
        return mapping_fingerprint(mapping) == _mapping_snapshot_fingerprint(mapping_snapshot(snapshot))
    except PolicyViolation:
        return False


def mapping_runtime_state(
    mapping: DataMapping,
    *,
    environment: str | None = None,
) -> dict[str, Any]:
    """Return the refresh state visible to this fixed deployment environment.

    The platform can use one metadata database for multiple deployments.  Old
    top-level mapping status fields remain the dev compatibility view; new
    records use ``environment_status`` so a staging/prod worker cannot make a
    dev page look refreshed (or failed).
    """
    resolved_environment = runtime_connector_service.runtime_environment(environment)
    raw_states = getattr(mapping, "environment_status", None) or {}
    state = raw_states.get(resolved_environment) if isinstance(raw_states, dict) else None
    if isinstance(state, dict):
        return {
            "status": str(state.get("status") or "unknown"),
            "last_error": str(state.get("last_error") or ""),
            "last_checked_at": state.get("last_checked_at"),
            "last_refreshed_at": state.get("last_refreshed_at"),
            "last_row_count": int(state.get("last_row_count") or 0),
            "last_imported_count": int(state.get("last_imported_count") or 0),
        }
    if resolved_environment != "dev":
        return {
            "status": "unknown",
            "last_error": "",
            "last_checked_at": None,
            "last_refreshed_at": None,
            "last_row_count": 0,
            "last_imported_count": 0,
        }
    return {
        "status": str(getattr(mapping, "status", "unknown") or "unknown"),
        "last_error": str(getattr(mapping, "last_error", "") or ""),
        "last_checked_at": getattr(mapping, "last_checked_at", None),
        "last_refreshed_at": getattr(mapping, "last_refreshed_at", None),
        "last_row_count": int(getattr(mapping, "last_row_count", 0) or 0),
        "last_imported_count": int(getattr(mapping, "last_imported_count", 0) or 0),
    }


def set_mapping_runtime_state(
    mapping: DataMapping,
    *,
    environment: str | None = None,
    status: str,
    error: str = "",
    checked_at: datetime | None = None,
    refreshed_at: datetime | None = None,
    rows_scanned: int | None = None,
    instances_created: int | None = None,
) -> None:
    """Persist a status transition without allowing environment cross-talk."""
    resolved_environment = runtime_connector_service.runtime_environment(environment)
    existing = getattr(mapping, "environment_status", None) or {}
    states = dict(existing) if isinstance(existing, dict) else {}
    prior = states.get(resolved_environment)
    state = dict(prior) if isinstance(prior, dict) else {}
    state["status"] = str(status or "unknown")
    state["last_error"] = str(error or "")
    if checked_at is not None:
        state["last_checked_at"] = checked_at.isoformat()
    if refreshed_at is not None:
        state["last_refreshed_at"] = refreshed_at.isoformat()
    if rows_scanned is not None:
        state["last_row_count"] = max(0, int(rows_scanned))
    if instances_created is not None:
        state["last_imported_count"] = max(0, int(instances_created))
    states[resolved_environment] = state
    mapping.environment_status = states

    # Keep existing API/database consumers working in dev while preventing
    # non-dev refreshes from overwriting the compatibility fields.
    if resolved_environment == "dev":
        mapping.status = state["status"]
        mapping.last_error = state["last_error"]
        if checked_at is not None:
            mapping.last_checked_at = checked_at
        if refreshed_at is not None:
            mapping.last_refreshed_at = refreshed_at
        if rows_scanned is not None:
            mapping.last_row_count = state["last_row_count"]
        if instances_created is not None:
            mapping.last_imported_count = state["last_imported_count"]


def invalidate_mapping_runtime_state(mapping: DataMapping) -> None:
    """Clear freshness facts after a mapping definition is edited.

    A mapping definition is shared by all deployment environments.  Keeping a
    prior ``ok`` state after its source/table/column contract changes would make
    a different environment look safely refreshed even though it has never
    executed the new definition.  The imported objects remain available for
    audit and are updated on the next successful refresh; only the freshness
    claim is invalidated here.
    """
    mapping.environment_status = {}
    mapping.status = "unknown"
    mapping.last_error = ""
    mapping.last_checked_at = None
    mapping.last_refreshed_at = None
    mapping.last_row_count = 0
    mapping.last_imported_count = 0


def _safe_error(value: Any) -> str:
    """Persist a compact diagnostic without leaking connection credentials."""
    return connector_service.sanitize_message(value, maximum=600)


def _mapping_status_for_job(status: str) -> str:
    return {
        "queued": "queued",
        "running": "refreshing",
        "retry_waiting": "retry_waiting",
        "succeeded": "ok",
        "failed": "error",
        "timed_out": "error",
        "cancelled": "error",
    }.get(status, "unknown")


def _active_key(mapping_id: str, environment: str) -> str:
    return f"{mapping_id}:{environment}"


def cancel_active_mapping_refresh_jobs(
    db: Session,
    mapping_id: str,
    *,
    reason: str = "映射已删除或被新定义替换",
    now: datetime | None = None,
) -> int:
    """Cancel queued/running work before a mapping definition is removed.

    This intentionally does not commit: callers cancel and replace/delete the
    definition in one transaction, while the worker rechecks job ownership just
    before its final commit.
    """
    finished_at = now or utc_now()
    return int(
        db.execute(
            update(DataMappingRefreshJob)
            .where(
                DataMappingRefreshJob.mapping_id == mapping_id,
                DataMappingRefreshJob.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status="cancelled",
                error=_safe_error(reason),
                active_key=None,
                next_retry_at=None,
                completed_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        or 0
    )


def enqueue_mapping_refresh(
    db: Session,
    mapping: DataMapping,
    *,
    limit: int | None = None,
    requested_by_user_id: str | None = None,
) -> tuple[DataMappingRefreshJob, bool]:
    """Create or return the one active refresh job for this mapping/environment."""
    tenant_id = tenant_service.current_tenant_id(db)
    scenario = db.get(BusinessScenario, mapping.scenario_id)
    if not scenario or scenario.tenant_id != tenant_id:
        raise PolicyViolation("映射所属业务场景不可刷新")
    if requested_by_user_id:
        principal = permission_service.require_principal(db)
        if principal.user_id != requested_by_user_id:
            raise PolicyViolation("刷新请求主体与当前登录主体不一致")
    else:
        requested_by_user_id = permission_service.require_principal(db).user_id

    # The deployment selects its own definition; a request cannot make a
    # staging/prod worker refresh mutable dev authoring rows.
    environment = runtime_connector_service.runtime_environment()
    runtime_mapping, definition = resolve_mapping_runtime_definition(
        db,
        scenario,
        mapping,
        environment=environment,
    )
    frozen_snapshot = mapping_snapshot(runtime_mapping)
    frozen_fingerprint = _mapping_snapshot_fingerprint(frozen_snapshot)
    frozen_relation_fingerprint = relation_mapping_fingerprint(
        definition, mapping.id
    )
    active = db.execute(
        select(DataMappingRefreshJob)
        .where(
            DataMappingRefreshJob.tenant_id == tenant_id,
            DataMappingRefreshJob.mapping_id == mapping.id,
            DataMappingRefreshJob.environment == environment,
            DataMappingRefreshJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(DataMappingRefreshJob.created_at.desc())
        .limit(1)
    ).scalars().first()
    if active:
        if _live_mapping_matches_job(mapping, active):
            set_mapping_runtime_state(
                mapping,
                environment=environment,
                status=_mapping_status_for_job(active.status),
                error=active.error or "",
            )
        else:
            _clear_stale_mapping_runtime_state(mapping, active)
        return active, False

    job = DataMappingRefreshJob(
        tenant_id=tenant_id,
        scenario_id=scenario.id,
        mapping_id=mapping.id,
        requested_by_user_id=requested_by_user_id,
        environment=environment,
        active_key=_active_key(mapping.id, environment),
        mapping_snapshot=frozen_snapshot,
        mapping_fingerprint=frozen_fingerprint,
        relation_mapping_fingerprint=frozen_relation_fingerprint,
        definition_snapshot_id=definition.snapshot_id if definition.is_frozen else None,
        release_id=definition.release_id if definition.is_frozen else None,
        # Dev jobs are immutable too: endpoint key/title/type changes alter how
        # source values become object identities and links. Pin the complete
        # live definition hash so a queued job cannot pick up those edits.
        definition_hash=definition.definition_hash or "",
        definition_source=definition.source,
        limit=bounded_limit(limit),
        status="queued",
        max_attempts=MAPPING_REFRESH_MAX_ATTEMPTS,
        timeout_seconds=MAPPING_REFRESH_TIMEOUT_SECONDS,
        available_at=utc_now(),
    )
    try:
        # The unique active key is the final concurrency guard for simultaneous
        # HTTP requests.  A savepoint keeps the caller's session usable when the
        # competing request has already inserted its job.
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        active = db.execute(
            select(DataMappingRefreshJob)
            .where(
                DataMappingRefreshJob.tenant_id == tenant_id,
                DataMappingRefreshJob.active_key == _active_key(mapping.id, environment),
            )
            .order_by(DataMappingRefreshJob.created_at.desc())
            .limit(1)
        ).scalars().first()
        if active is None:
            raise
        if _live_mapping_matches_job(mapping, active):
            set_mapping_runtime_state(
                mapping,
                environment=environment,
                status=_mapping_status_for_job(active.status),
                error=active.error or "",
            )
        else:
            _clear_stale_mapping_runtime_state(mapping, active)
        return active, False

    if _live_mapping_matches_job(mapping, job):
        set_mapping_runtime_state(mapping, environment=environment, status="queued")
    return job, True


def _retry_or_finish(
    db: Session,
    job: DataMappingRefreshJob,
    mapping: DataMapping | None,
    *,
    final_status: str,
    error: str,
    now: datetime,
) -> None:
    safe_error = _safe_error(error)
    job.error = safe_error
    if job.attempt < job.max_attempts:
        delay = min(MAPPING_REFRESH_RETRY_SECONDS * (2 ** max(0, job.attempt - 1)), 300)
        job.status = "retry_waiting"
        job.available_at = now + timedelta(seconds=delay)
        job.next_retry_at = job.available_at
        job.completed_at = None
        if _live_mapping_matches_job(mapping, job):
            set_mapping_runtime_state(
                mapping,
                environment=job.environment,
                status="retry_waiting",
                error=safe_error,
            )
        else:
            _clear_stale_mapping_runtime_state(mapping, job)
    else:
        job.status = final_status
        job.active_key = None
        job.next_retry_at = None
        job.completed_at = now
        if _live_mapping_matches_job(mapping, job):
            set_mapping_runtime_state(
                mapping,
                environment=job.environment,
                status=_mapping_status_for_job(final_status),
                error=safe_error,
                checked_at=now,
            )
        else:
            _clear_stale_mapping_runtime_state(mapping, job)
    db.commit()


def _cancel_job(
    db: Session,
    job: DataMappingRefreshJob,
    mapping: DataMapping | None,
    *,
    reason: str,
    now: datetime,
) -> None:
    job.status = "cancelled"
    job.error = _safe_error(reason)
    job.active_key = None
    job.next_retry_at = None
    job.completed_at = now
    # Do not overwrite a changed definition's fresh status.  Its new version
    # must be explicitly refreshed instead of inheriting an old job's error.
    if _live_mapping_matches_job(mapping, job):
        set_mapping_runtime_state(
            mapping,
            environment=job.environment,
            status="error",
            error=job.error,
            checked_at=now,
        )
    elif mapping is not None and mapping.id == job.mapping_id:
        set_mapping_runtime_state(
            mapping,
            environment=job.environment,
            status="unknown",
        )
    db.commit()


def _job_context(
    db: Session,
    job: DataMappingRefreshJob,
) -> tuple[BusinessScenario | None, DataMapping | None, str | None]:
    scenario = db.get(BusinessScenario, job.scenario_id)
    if scenario is None or scenario.tenant_id != job.tenant_id:
        return scenario, None, "业务场景已删除或不再属于任务租户"
    mapping = db.get(DataMapping, job.mapping_id)
    try:
        _job_runtime_mapping(db, job, scenario)
        _job_runtime_definition(db, job, scenario)
    except PolicyViolation as exc:
        return scenario, mapping, str(exc)
    if mapping is None or mapping.scenario_id != scenario.id:
        return scenario, mapping, "映射已删除或不再属于目标业务场景"
    return scenario, mapping, None


def expire_stale_mapping_refresh_jobs(db: Session, *, now: datetime | None = None) -> None:
    """Reclaim stalled jobs using each job's persisted environment."""
    now = now or utc_now()
    jobs = db.execute(
        select(DataMappingRefreshJob).where(
            DataMappingRefreshJob.status == "running",
        )
    ).scalars().all()
    for job in jobs:
        started_at = _aware(job.started_at)
        if started_at and now > started_at + timedelta(seconds=job.timeout_seconds):
            _scenario, mapping, cancellation_reason = _job_context(db, job)
            if cancellation_reason:
                _cancel_job(db, job, mapping, reason=cancellation_reason, now=now)
                continue
            _retry_or_finish(
                db,
                job,
                mapping,
                final_status="timed_out",
                error="映射刷新超过配置的超时限制",
                now=now,
            )


def resolve_mapping_data_source(
    db: Session,
    scenario: BusinessScenario,
    mapping: Any,
    *,
    environment: str | None = None,
    release_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve a mapping's data source through the fixed runtime environment."""
    return runtime_connector_service.resolve_connector(
        db,
        scenario,
        kind="data_source",
        config=mapping_runtime_config(mapping),
        environment=environment,
        release_id=release_id,
    )


def _claimed_job_context(
    db: Session,
    job_id: str,
) -> tuple[DataMappingRefreshJob | None, BusinessScenario | None, DataMapping | None, str | None]:
    """Reload ownership after external I/O before committing imported objects.

    A mapping can be deleted/replaced or its queued job cancelled while a worker
    is reading a connector.  Re-reading with ``populate_existing`` prevents an
    identity-map cache from turning that cancellation into an old-definition
    commit.  Callers roll back any tentative import before acting on a reason.
    """
    db.expire_all()
    job = db.get(DataMappingRefreshJob, job_id, populate_existing=True)
    if job is None:
        return None, None, None, "映射刷新任务已删除"
    if job.status != "running":
        return job, None, None, "映射刷新任务已取消或不再可提交"
    scenario, mapping, reason = _job_context(db, job)
    return job, scenario, mapping, reason


def process_mapping_refresh_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 4,
) -> list[DataMappingRefreshJob]:
    """Atomically claim and process a bounded number of mapping refresh jobs."""
    dispatch_now = now or utc_now()
    worker_environment = runtime_connector_service.runtime_environment()
    job_ids = db.execute(
        select(DataMappingRefreshJob.id)
        .where(
            DataMappingRefreshJob.status.in_(DISPATCHABLE_STATUSES),
            DataMappingRefreshJob.available_at <= dispatch_now,
            DataMappingRefreshJob.environment == worker_environment,
        )
        .order_by(DataMappingRefreshJob.available_at.asc(), DataMappingRefreshJob.created_at.asc())
        .limit(max(1, min(limit, 16)))
    ).scalars().all()
    processed: list[DataMappingRefreshJob] = []

    for job_id in job_ids:
        # Use the real claim time for each job.  A long first refresh must not
        # make later jobs look as if they had already consumed its timeout.
        claim_now = now or utc_now()
        claimed = db.execute(
            update(DataMappingRefreshJob)
            .where(
                DataMappingRefreshJob.id == job_id,
                DataMappingRefreshJob.status.in_(DISPATCHABLE_STATUSES),
                DataMappingRefreshJob.available_at <= claim_now,
                DataMappingRefreshJob.environment == worker_environment,
            )
            .values(
                status="running",
                attempt=DataMappingRefreshJob.attempt + 1,
                started_at=claim_now,
                next_retry_at=None,
                error="",
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            db.rollback()
            continue
        db.commit()
        db.expire_all()
        job = db.get(DataMappingRefreshJob, job_id)
        if job is None:
            continue

        scenario, mapping, cancellation_reason = _job_context(db, job)
        if cancellation_reason:
            _cancel_job(db, job, mapping, reason=cancellation_reason, now=claim_now)
            processed.append(job)
            continue
        assert scenario is not None and mapping is not None

        try:
            runtime_mapping = _job_runtime_mapping(db, job, scenario)
            definition = _job_runtime_definition(db, job, scenario)
            definition_mapping = definition.mappings.get(job.mapping_id)
            if (
                definition_mapping is None
                or _mapping_snapshot_fingerprint(mapping_snapshot(definition_mapping))
                != job.mapping_fingerprint
            ):
                raise PolicyViolation("映射刷新快照与当前固定运行定义不一致")
            runtime_mapping = definition_mapping
            with permission_service.execution_principal(
                db,
                scenario,
                requested_user_id=job.requested_by_user_id,
            ):
                permission_service.require_scenario_permission(db, scenario, "write")
                # The job executes only its captured DTO.  A later live edit
                # may clear the current mapping's freshness state, but must not
                # silently rewrite this queued operation's external read.
                if _live_mapping_matches_job(mapping, job):
                    set_mapping_runtime_state(
                        mapping,
                        environment=job.environment,
                        status="refreshing",
                    )
                source, connector_audit = resolve_mapping_data_source(
                    db,
                    scenario,
                    runtime_mapping,
                    environment=job.environment,
                    release_id=job.release_id if job.definition_source == "release" else None,
                )
                relation_mappings = [
                    relation_mapping
                    for relation_mapping in definition.relation_mappings.values()
                    if job.mapping_id in {
                        str(getattr(relation_mapping, "source_mapping_id", "") or ""),
                        str(getattr(relation_mapping, "target_mapping_id", "") or ""),
                    }
                ]
                required_mapping_ids = {job.mapping_id}
                for relation_mapping in relation_mappings:
                    required_mapping_ids.update(
                        {
                            str(relation_mapping.source_mapping_id),
                            str(relation_mapping.target_mapping_id),
                        }
                    )
                mapping_data_sources: dict[str, Any] = {job.mapping_id: source}
                mapping_connector_audits: dict[str, dict[str, Any]] = {
                    job.mapping_id: connector_audit
                }
                connector_audits: list[dict[str, Any]] = [connector_audit]
                for mapping_id in sorted(required_mapping_ids - {job.mapping_id}):
                    endpoint_mapping = definition.mappings.get(mapping_id)
                    if endpoint_mapping is None:
                        raise PolicyViolation("关系映射端点不属于当前运行定义")
                    endpoint_source, endpoint_audit = resolve_mapping_data_source(
                        db,
                        scenario,
                        endpoint_mapping,
                        environment=job.environment,
                        release_id=job.release_id if job.definition_source == "release" else None,
                    )
                    mapping_data_sources[mapping_id] = endpoint_source
                    mapping_connector_audits[mapping_id] = endpoint_audit
                    connector_audits.append(endpoint_audit)
                relation_data_sources: dict[str, Any] = {}
                relation_connector_audits: dict[str, dict[str, Any]] = {}
                for relation_mapping in relation_mappings:
                    if str(getattr(relation_mapping, "mode", "")) != "join_table":
                        continue
                    join_source, join_audit = resolve_mapping_data_source(
                        db,
                        scenario,
                        relation_mapping,
                        environment=job.environment,
                        release_id=job.release_id if job.definition_source == "release" else None,
                    )
                    relation_data_sources[str(relation_mapping.id)] = join_source
                    relation_connector_audits[str(relation_mapping.id)] = join_audit
                    connector_audits.append(join_audit)
                result = ontology_service.import_instances_from_mapping(
                    db,
                    scenario,
                    runtime_mapping,
                    limit=job.limit,
                    data_source=source,
                    commit=False,
                    environment=job.environment,
                    relation_mappings=relation_mappings,
                    relation_data_sources=relation_data_sources,
                    mapping_data_sources=mapping_data_sources,
                    runtime_mappings=definition.mappings,
                    runtime_relations=definition.relations,
                    mapping_connector_audits=mapping_connector_audits,
                    relation_connector_audits=relation_connector_audits,
                    definition_provenance={
                        "snapshot_id": definition.snapshot_id,
                        "release_id": definition.release_id,
                        "definition_hash": definition.definition_hash,
                        "source": definition.source,
                    },
                )

            finished_at = utc_now()
            if finished_at > claim_now + timedelta(seconds=job.timeout_seconds):
                # The import service flushes in order to build relation rows.
                # A timeout is not a partial success: discard that transaction
                # before recording the retry/terminal state in a fresh one.
                db.rollback()
                current, _scenario, current_mapping, current_reason = _claimed_job_context(
                    db,
                    job_id,
                )
                if current is not None and current.status in ACTIVE_STATUSES:
                    if current_reason:
                        _cancel_job(
                            db,
                            current,
                            current_mapping,
                            reason=current_reason,
                            now=finished_at,
                        )
                    else:
                        _retry_or_finish(
                            db,
                            current,
                            current_mapping,
                            final_status="timed_out",
                            error="映射刷新超过配置的超时限制",
                            now=finished_at,
                        )
            else:
                current, _current_scenario, current_mapping, current_reason = _claimed_job_context(
                    db,
                    job_id,
                )
                if current_reason:
                    raise PolicyViolation(current_reason)
                assert current is not None and current_mapping is not None
                job = current
                mapping = current_mapping
                job.status = "succeeded"
                job.active_key = None
                job.error = ""
                job.connector_audit = connector_audits
                job.rows_scanned = int(result.get("rows_scanned", 0))
                job.instances_created = int(result.get("instances_created", 0))
                job.instances_updated = int(result.get("instances_updated", 0))
                job.relations_created = int(result.get("relations_created", 0))
                job.completed_at = finished_at
                job.next_retry_at = None
                if _live_mapping_matches_job(mapping, job):
                    set_mapping_runtime_state(
                        mapping,
                        environment=job.environment,
                        status="ok",
                        checked_at=finished_at,
                        refreshed_at=finished_at,
                        rows_scanned=job.rows_scanned,
                        instances_created=job.instances_created,
                    )
                else:
                    _clear_stale_mapping_runtime_state(mapping, job)
                db.commit()
        except (PolicyViolation, HTTPException) as exc:
            db.rollback()
            current = db.get(DataMappingRefreshJob, job_id)
            if current is not None and current.status in ACTIVE_STATUSES:
                current_mapping = db.get(DataMapping, current.mapping_id)
                _cancel_job(db, current, current_mapping, reason=str(exc), now=utc_now())
        except Exception as exc:  # noqa: BLE001 - errors become bounded, redacted retries.
            # import_instances_from_mapping may have flushed partial writes.  Roll
            # them back before recording the retry state so an attempt is atomic.
            db.rollback()
            current = db.get(DataMappingRefreshJob, job_id)
            if current is not None and current.status in ACTIVE_STATUSES:
                current_mapping = db.get(DataMapping, current.mapping_id)
                _retry_or_finish(
                    db,
                    current,
                    current_mapping,
                    final_status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    now=utc_now(),
                )
        finally:
            db.expire_all()
        current = db.get(DataMappingRefreshJob, job_id)
        if current is not None:
            processed.append(current)
    return processed
