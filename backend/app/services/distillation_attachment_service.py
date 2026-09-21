"""Scenario-shared temporary inputs, explicit turn binding and durable expiry cleanup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, UploadFile
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from ..distillation_attachment_models import DistillationAttachment as Attachment, DistillationTurnAttachment as Link
from ..distillation_attachment_schemas import AttachmentOut
from ..distillation_conversation_models import DistillationConversationTurn as Turn
from . import datasource_service, distillation_service, permission_service, release_service, upload_staging_service
from .distillation_attachment_parser import MAX_ATTACHMENT_BYTES, ParsedAttachment, parse_staged


ATTACHMENT_TTL = timedelta(hours=24)
MAX_READY_ATTACHMENTS = 20


def now() -> datetime:
    return datetime.now(timezone.utc)


def public_attachment(row: Attachment) -> AttachmentOut:
    values = {name: getattr(row, name) for name in AttachmentOut.model_fields}
    if row.expires_at <= now() and row.status in {"ready", "bound"}:
        values["status"] = "expired"
    return AttachmentOut.model_validate(values)


def list_pending(db: Session, project_id: str) -> list[AttachmentOut]:
    # The project ACL is the sole visibility boundary. Uploading/removing
    # inputs still requires project write access, but collaborators with read
    # access must see inputs uploaded by every member.
    project = distillation_service.project(db, project_id)
    rows = db.scalars(select(Attachment).where(Attachment.project_id == project.id,
        Attachment.tenant_id == project.tenant_id,
        Attachment.status.in_(('ready', 'bound')), Attachment.expires_at > now()).order_by(
            Attachment.created_at, Attachment.id,
        ).limit(MAX_READY_ATTACHMENTS)).all()
    return [public_attachment(row) for row in rows]


def persist_parsed(db: Session, project_id: str, *, request_id: str, filename: str, byte_size: int,
                   content_sha256: str, parsed: ParsedAttachment, scenario_id: str | None) -> Attachment:
    permission_service.refresh_request_authorization(db)
    project = distillation_service.project(db, project_id, write=True, lock=True)
    principal = permission_service.require_principal(db)
    if project.scenario_id != scenario_id:
        raise HTTPException(409, "上传期间会话场景已变化，请重新上传")
    existing = db.scalar(select(Attachment).where(Attachment.project_id == project.id,
        Attachment.created_by == principal.user_id, Attachment.request_id == request_id))
    if existing:
        if (existing.filename, existing.byte_size, existing.content_sha256) != (filename, byte_size, content_sha256):
            raise HTTPException(409, "同一上传标识不能用于不同文件")
        if existing.status in {"removed", "expired"} or existing.expires_at <= now():
            raise HTTPException(409, "该上传已移除或过期，请重新选择文件")
        return existing
    if release_service.safe_snapshot_content({"filename": filename, "content": parsed.text}) != {"filename": filename, "content": parsed.text}:
        raise HTTPException(422, "附件包含疑似凭据，请先脱敏后重新上传")
    count = db.scalar(select(func.count()).select_from(Attachment).where(
        Attachment.project_id == project.id, Attachment.tenant_id == project.tenant_id,
        Attachment.status.in_(("ready", "bound")), Attachment.expires_at > now()))
    if count >= MAX_READY_ATTACHMENTS:
        raise HTTPException(409, "本项目的临时附件已达 20 份，请先移除不再使用的附件")
    row = Attachment(tenant_id=principal.tenant_id, project_id=project.id, scenario_id=project.scenario_id,
        created_by=principal.user_id, request_id=request_id, filename=filename,
        media_type=parsed.media_type, byte_size=byte_size, content_sha256=content_sha256,
        parsed_text=parsed.text, expires_at=now() + ATTACHMENT_TTL)
    db.add(row)
    db.flush()
    return row


def upload(db: Session, project_id: str, file: UploadFile, request_id: str) -> Attachment:
    project = distillation_service.project(db, project_id, write=True)
    scenario_id = project.scenario_id
    try:
        filename = datasource_service.validate_bucket_filename(file.filename or "attachment.txt")
        if len(filename) > 255 or release_service.safe_snapshot_content({"filename": filename}) != {"filename": filename}:
            raise ValueError("Invalid filename")
    except ValueError as exc:
        raise HTTPException(422, "附件名称无效或包含疑似凭据") from exc
    db.commit()
    staged = None
    try:
        # This service runs in FastAPI's bounded synchronous endpoint pool. The
        # established streaming primitive owns spool cleanup and byte counting.
        staged = asyncio.run(upload_staging_service.stage_upload(file,
            max_bytes=MAX_ATTACHMENT_BYTES, chunk_bytes=64 * 1024))
        parsed = parse_staged(staged.path, filename, file.content_type or "application/octet-stream")
        return persist_parsed(db, project_id, request_id=request_id, filename=filename,
            byte_size=staged.byte_size, content_sha256=staged.content_sha256, parsed=parsed, scenario_id=scenario_id)
    except upload_staging_service.UploadTooLargeError as exc:
        raise HTTPException(413, "临时附件不能超过 10 MB") from exc
    except ValueError as exc:
        raise HTTPException(422, "附件为空或无法安全读取，请检查文件后重试") from exc
    finally:
        file.file.close()
        if staged is not None:
            staged.remove()


def bind(db: Session, turn: Turn, attachment_ids: list[str]) -> list[dict]:
    # Earlier submissions remain available to every collaborator in the same
    # project. Every new turn fixes that live set before execution starts.
    rows = list(db.scalars(select(Attachment).where(
        or_(
            Attachment.id.in_(attachment_ids),
            (Attachment.status == "bound") & (Attachment.expires_at > now()),
        ),
        Attachment.tenant_id == turn.tenant_id,
        Attachment.project_id == turn.project_id,
        Attachment.scenario_id == turn.context["scenario_id"],
        ).order_by(Attachment.id).with_for_update()))
    if not set(attachment_ids).issubset({row.id for row in rows}):
        raise HTTPException(404, "临时附件不存在或不属于当前会话")
    for row in rows:
        if row.scenario_id != turn.context["scenario_id"] or row.status not in {"ready", "bound"} or row.expires_at <= now():
            raise HTTPException(409, "临时附件已失效，请移除后重新上传")
        db.add(Link(turn_id=turn.id, attachment_id=row.id, tenant_id=turn.tenant_id,
            project_id=turn.project_id, user_id=turn.created_by))
        row.status = "bound"
    db.flush()
    return [public_attachment(row).model_dump(mode="json") for row in rows]


def assert_available(db: Session, turn: Turn) -> None:
    ids = turn.context.get("attachment_ids", [])
    if not ids:
        return
    count = db.scalar(select(func.count()).select_from(Link).join(Attachment, Attachment.id == Link.attachment_id).where(
        Link.turn_id == turn.id, Link.tenant_id == turn.tenant_id, Link.project_id == turn.project_id,
        Attachment.id.in_(ids), Attachment.scenario_id == turn.context["scenario_id"],
        Attachment.status.in_(("ready", "bound")), Attachment.expires_at > now()))
    if count != len(ids):
        raise HTTPException(409, "本轮临时附件已移除或过期，请重新上传后发送")


def read_attachment(db: Session, turn: Turn, attachment_id: str, offset: int, limit: int) -> dict:
    if attachment_id not in turn.context.get("attachment_ids", []):
        raise HTTPException(404, "本轮未选择该临时附件")
    assert_available(db, turn)
    result = db.execute(select(Attachment.filename, Attachment.content_sha256,
        func.char_length(Attachment.parsed_text), func.substr(Attachment.parsed_text, offset + 1, limit)).where(
        Attachment.id == attachment_id, Attachment.project_id == turn.project_id,
        Attachment.tenant_id == turn.tenant_id, Attachment.scenario_id == turn.context["scenario_id"])).one_or_none()
    if result is None:
        raise HTTPException(404, "临时附件不存在")
    return {"attachment_id": attachment_id, "filename": result[0], "content_sha256": result[1],
        "content": result[3], "offset": offset, "total_characters": result[2],
        "complete": offset == 0 and len(result[3]) >= result[2],
        "limitations": ["临时附件供当前场景协作者在本项目分析，不会进入资料库；原文件未保留，解析文本 24 小时后清理。",
            "这是有界文本节选；未实际执行数据匹配或业务操作。"]}


def _invalidate(db: Session, attachment: Attachment, status: str) -> None:
    attachment.status, attachment.parsed_text = status, ""


def cleanup_invalid_turns(db: Session, *, limit: int = 50) -> int:
    invalid = exists(select(Link.turn_id).join(Attachment, Attachment.id == Link.attachment_id).where(
        Link.turn_id == Turn.id, or_(Attachment.status.in_(("removed", "expired")), Attachment.expires_at <= now())))
    turns = list(db.scalars(select(Turn).where(invalid,
        or_(Turn.status.in_(("queued", "running")), Turn.checkpoint != [])).order_by(Turn.id)
        .with_for_update(skip_locked=True).limit(limit)))
    for turn in turns:
        turn.checkpoint = []
        if turn.status in {"queued", "running"}:
            turn.status, turn.error = "failed", "本轮临时附件已移除或过期，请重新上传后发送。"
            turn.lease_token = turn.lease_expires_at = None
            turn.completed_at = turn.updated_at = now()
            turn.steps = [{**step, "status": "failed", "summary": turn.error,
                "completed_at": turn.completed_at.isoformat()} if step["status"] == "running" else step for step in turn.steps]
    return len(turns)


def remove(db: Session, project_id: str, attachment_id: str) -> None:
    project = distillation_service.project(db, project_id, write=True, lock=True)
    row = db.scalar(select(Attachment).where(Attachment.id == attachment_id,
        Attachment.project_id == project.id, Attachment.tenant_id == project.tenant_id).with_for_update())
    if row is None:
        raise HTTPException(404, "临时附件不存在")
    _invalidate(db, row, "removed")
    db.flush()
    cleanup_invalid_turns(db)


def cleanup_expired(db: Session, *, limit: int = 50) -> int:
    rows = list(db.scalars(select(Attachment).where(Attachment.expires_at <= now(),
        Attachment.status.in_(("ready", "bound"))).order_by(Attachment.expires_at, Attachment.id)
        .with_for_update(skip_locked=True).limit(limit)))
    for row in rows:
        _invalidate(db, row, "expired")
    db.flush()
    return len(rows) + cleanup_invalid_turns(db, limit=limit)


def history_statuses(db: Session, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    principal = permission_service.require_principal(db)
    return {row.id: "expired" if row.expires_at <= now() and row.status in {"ready", "bound"} else row.status
        for row in db.execute(select(Attachment.id, Attachment.status, Attachment.expires_at).where(
            Attachment.id.in_(ids), Attachment.tenant_id == principal.tenant_id))}


def cleanup_tick(*, session_factory=None) -> int:
    from ..database import SessionLocal

    with (session_factory or SessionLocal)() as db:
        count = cleanup_expired(db)
        db.commit()
        return count
