"""Tenant-scoped request and input preparation for durable Agent turns."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentTurnRun,
    DataAsset,
    DataAssetVersion,
    DatasetVersion,
    LogicalDataset,
    ManagedUploadRun,
)
from ..schemas import ChatRequest
from . import (
    agent_turn_payload_service,
    managed_asset_lifecycle,
    managed_attachment_access,
    permission_service,
    tenant_service,
)


class AgentTurnError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AgentTurnConflict(AgentTurnError):
    def __init__(self, message: str = "Agent Turn 请求与当前状态冲突") -> None:
        super().__init__("agent_turn_conflict", message, status_code=409)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AgentTurnError(
            "invalid_agent_turn_request",
            "Agent Turn 请求必须是可规范化 JSON",
            status_code=422,
        ) from exc


def _fingerprint(
    payload: ChatRequest,
    *,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    parent_run_id: str | None = None,
) -> str:
    document = {
        "contract": "agent-turn-request/v1",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "parent_run_id": str(parent_run_id or ""),
        "request": payload.model_dump(mode="json", exclude_none=True),
    }
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def _payload_context(run: AgentTurnRun) -> dict[str, str]:
    return {
        "contract": "agent-turn-payload-context/v1",
        "run_id": str(run.id),
        "tenant_id": str(run.tenant_id),
        "requested_by_user_id": str(run.requested_by_user_id or ""),
        "agent_id": str(run.agent_id or ""),
        "conversation_id": str(run.conversation_id or ""),
        "request_fingerprint": str(run.request_fingerprint),
    }


def _open_authenticated_request(run: AgentTurnRun, message: str) -> ChatRequest:
    document = agent_turn_payload_service.open_payload(
        run.request_payload,
        context=_payload_context(run),
        summary=run.request_summary,
        digest=run.request_digest,
    )
    document["message"] = message
    payload = ChatRequest.model_validate(document)
    expected = _fingerprint(
        payload,
        tenant_id=str(run.tenant_id),
        user_id=str(run.requested_by_user_id or ""),
        agent_id=str(run.agent_id or ""),
        parent_run_id=run.parent_run_id,
    )
    if not secrets.compare_digest(expected, str(run.request_fingerprint or "")):
        raise agent_turn_payload_service.AgentTurnPayloadError(
            "agent_turn_request_changed",
            "Agent Turn 原始请求已变化，已阻止执行",
        )
    return payload


def _require_agent(db: Session, agent_id: str, *, active_runtime: bool = True) -> Agent:
    agent = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id)
        permission_service.require_scenario_permission(
            db,
            scenario,
            "read",
            message="没有该 Agent 所属业务场景的权限",
        )
        if active_runtime and scenario.status == "retired":
            raise AgentTurnError("scenario_retired", "业务场景已退役，不能创建新的 Agent 对话")
    else:
        permission_service.require_tenant_permission(db, "read")
    return agent


def _validate_attachment_scope(
    db: Session,
    payload: ChatRequest,
    *,
    user_id: str,
    agent_id: str | None = None,
) -> None:
    try:
        managed_attachment_access.validate_attachments(
            db,
            payload.attachments,
            user_id=user_id,
            agent_id=agent_id,
        )
    except managed_attachment_access.AttachmentAccessError as exc:
        if exc.code == "attachment_conflict":
            raise AgentTurnConflict(exc.message) from None
        raise AgentTurnError(exc.code, exc.message, status_code=exc.status_code) from None


def _resolve_upload_attachments(
    db: Session,
    payload: ChatRequest,
    *,
    user_id: str,
    agent_id: str | None = None,
) -> tuple[ChatRequest | None, int]:
    tenant_id = tenant_service.current_tenant_id(db)
    document = payload.model_dump(mode="json", exclude_none=True)
    resolved: list[dict[str, Any]] = []
    pending = 0
    now = _now()
    for attachment in document.get("attachments") or []:
        upload_run_id = str(attachment.get("upload_run_id") or "")
        if not upload_run_id:
            resolved.append(attachment)
            continue
        statement = select(ManagedUploadRun).where(
            ManagedUploadRun.id == upload_run_id,
            ManagedUploadRun.tenant_id == tenant_id,
            ManagedUploadRun.requested_by_user_id == user_id,
        )
        if agent_id:
            statement = statement.where(ManagedUploadRun.owner_agent_id == agent_id)
        else:
            statement = statement.where(ManagedUploadRun.owner_agent_id.is_(None))
        upload = db.scalar(statement)
        if upload is None:
            raise AgentTurnError(
                "attachment_unavailable",
                "部分附件在后台处理前已失效",
                status_code=404,
            )
        if upload.status in {"awaiting_upload", "uploading", "stored", "processing"}:
            if upload.status in {"awaiting_upload", "uploading"} and (
                _as_utc(upload.expires_at) or now
            ) <= now:
                raise AgentTurnError(
                    "attachment_upload_expired",
                    "部分附件未在有效期内完成上传",
                )
            retry_after = _as_utc(upload.available_at) or now
            if (
                upload.status == "awaiting_upload"
                and upload.error_code
                and retry_after <= now
            ):
                raise AgentTurnError(
                    "attachment_upload_failed",
                    str(upload.error_message or "部分附件内容上传失败")[:1000],
                )
            pending += 1
            continue
        if upload.status != "ready" or not upload.asset_version_id:
            raise AgentTurnError(
                "attachment_preparation_failed",
                str(upload.error_message or "部分附件后台处理失败")[:1000],
            )
        version = db.scalar(
            select(DataAssetVersion).where(
                DataAssetVersion.id == upload.asset_version_id,
                DataAssetVersion.tenant_id == tenant_id,
                DataAssetVersion.status == "ready",
            )
        )
        asset = db.get(DataAsset, version.asset_id) if version is not None else None
        if (
            version is None
            or asset is None
            or asset.tenant_id != tenant_id
            or (
                agent_id
                and asset.owner_agent_id != agent_id
            )
            or (not agent_id and asset.owner_agent_id is not None)
            or asset.lifecycle_status != "active"
            or asset.usage_plane != "invocation_input"
        ):
            raise AgentTurnError(
                "attachment_unavailable",
                "部分附件在后台处理后已失效",
                status_code=404,
            )
        try:
            managed_asset_lifecycle.require_current_asset_version(
                version.version_document
            )
        except managed_asset_lifecycle.ManagedAssetLifecycleError as exc:
            raise AgentTurnError(
                "attachment_expired"
                if exc.code == "managed_reference_expired"
                else "attachment_unavailable",
                "临时附件已过期，请重新上传"
                if exc.code == "managed_reference_expired"
                else "临时附件缺少有效生命周期",
                status_code=410 if exc.code == "managed_reference_expired" else 409,
            ) from None
        resolved.append(
            {
                "asset_version_id": version.id,
                "expected_signature": version.content_sha256,
                "filename": upload.filename,
            }
        )
    if pending:
        return None, pending
    document["attachments"] = resolved
    return ChatRequest.model_validate(document), 0


def _table_asset_version_ids(
    db: Session, payload: ChatRequest, *, agent_id: str | None = None
) -> list[str]:
    ids = [item.asset_version_id for item in payload.attachments if item.asset_version_id]
    if not ids:
        return []
    versions = {
        item.id: item
        for item in db.scalars(
            select(DataAssetVersion)
            .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
            .where(
                DataAssetVersion.id.in_(ids),
                DataAssetVersion.tenant_id == tenant_service.current_tenant_id(db),
                (DataAsset.owner_agent_id == agent_id if agent_id else DataAsset.owner_agent_id.is_(None)),
            )
        )
    }
    if any(item_id not in versions for item_id in ids):
        raise AgentTurnError(
            "attachment_unavailable",
            "部分附件在后台处理前已失效",
            status_code=404,
        )
    return [
        item_id
        for item_id in ids
        if isinstance((versions[item_id].version_document or {}).get("profile"), Mapping)
        and (versions[item_id].version_document or {}).get("profile", {}).get("category")
        == "table"
    ]


def _prepared_payload(
    payload: ChatRequest,
    *,
    table_asset_ids: set[str],
    dataset_result: Mapping[str, Any] | None,
) -> ChatRequest:
    document = payload.model_dump(mode="json", exclude_none=True)
    document["attachments"] = [
        item
        for item in document.get("attachments") or []
        if str(item.get("asset_version_id") or "") not in table_asset_ids
    ]
    if dataset_result:
        document["attachments"].insert(
            0,
            {
                "dataset_version_id": str(dataset_result["dataset_version_id"]),
                "expected_signature": str(dataset_result["content_hash"]),
                "filename": "受管数据包",
            },
        )
    return ChatRequest.model_validate(document)


__all__ = ["AgentTurnConflict", "AgentTurnError"]
