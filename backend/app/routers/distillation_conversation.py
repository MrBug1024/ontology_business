"""Authenticated conversation adapter; durable workers own all model/tool I/O."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..distillation_conversation_schemas import ConversationPage, InvestigationToolCatalogOut, TurnCreate, TurnOut
from ..distillation_schemas import DistillationDocument, ProjectOut, RevisionRequest
from ..distillation_attachment_schemas import AttachmentOut
from ..models import AuthSession
from ..services import distillation_attachment_service as attachments
from ..services import distillation_conversation_service as conversation, distillation_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/business-distillation/{project_id}/conversation", tags=["business-distillation"])
preupload_router = APIRouter(prefix="/business-distillation/conversation", tags=["business-distillation"])


@router.get("", response_model=ConversationPage)
def list_turns(project_id: str, limit: int = Query(20, ge=1, le=50),
               before_turn_number: int | None = Query(None, ge=1), db: Session = Depends(get_tenant_db)):
    return conversation.list_turns(db, project_id, limit, before_turn_number)


@router.get("/tools", response_model=InvestigationToolCatalogOut)
def list_investigation_tools(project_id: str, db: Session = Depends(get_tenant_db)):
    return conversation.investigation_tool_catalog(db, project_id)


@router.post("/turns", response_model=TurnOut, status_code=202)
def create_turn(project_id: str, payload: TurnCreate, db: Session = Depends(get_tenant_db)):
    row = conversation.enqueue(db, project_id, payload)
    db.commit()
    return conversation.public_turn(row)


@router.get("/turns/{turn_id}", response_model=TurnOut)
def get_turn(project_id: str, turn_id: str, db: Session = Depends(get_tenant_db)):
    row = conversation.get_turn(db, project_id, turn_id)
    return conversation.public_turn(row, statuses=attachments.history_statuses(db,
        [item["id"] for item in row.context.get("attachments", [])]))


@router.get("/turns/{turn_id}/events", response_class=StreamingResponse)
def stream_turn(project_id: str, turn_id: str, request: Request, db: Session = Depends(get_tenant_db)) -> StreamingResponse:
    conversation.get_turn(db, project_id, turn_id)
    tenant_id = str(db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    auth_session_id = str(getattr(request.state, "auth_session_id", "") or "")
    # Do not pin the authenticated request session to the SSE lifetime.
    db.close()

    def generate():
        last_payload = ""
        keepalive_at = time.monotonic()
        while True:
            authorization_error = False
            payload = ""
            terminal = False
            with SessionLocal() as event_db:
                event_db.info["tenant_id"] = tenant_id
                event_db.info["user_id"] = user_id
                auth_session = event_db.get(AuthSession, auth_session_id)
                expiry = auth_session.expires_at if auth_session is not None else None
                if expiry is not None and expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if (auth_session is None or auth_session.user_id != user_id or expiry is None
                        or expiry <= datetime.now(timezone.utc)):
                    authorization_error = True
                else:
                    try:
                        row = conversation.get_turn(event_db, project_id, turn_id)
                        attachment_ids = [item["id"] for item in row.context.get("attachments", [])]
                        payload = conversation.public_turn(row, statuses=attachments.history_statuses(
                            event_db, attachment_ids)).model_dump_json()
                        terminal = row.status in {"cancelled", "failed", "succeeded", "waiting"}
                    except HTTPException:
                        authorization_error = True
            if authorization_error:
                yield 'event: error\ndata: {"code":"distillation_turn_unavailable"}\n\n'
                return
            if payload and payload != last_payload:
                last_payload = payload
                yield f"event: turn\ndata: {payload}\n\n"
                continue
            if terminal:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - keepalive_at >= 15:
                keepalive_at = time.monotonic()
                yield ": keepalive\n\n"
            time.sleep(0.35)

    response = StreamingResponse(generate(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@router.post("/turns/{turn_id}/cancel", response_model=TurnOut)
def cancel_turn(project_id: str, turn_id: str, db: Session = Depends(get_tenant_db)):
    row = conversation.cancel(db, project_id, turn_id)
    db.commit()
    return conversation.public_turn(row)


@router.post("/turns/{turn_id}/apply", response_model=ProjectOut)
def apply_turn(project_id: str, turn_id: str, payload: RevisionRequest, db: Session = Depends(get_tenant_db)):
    row = conversation.apply(db, project_id, turn_id, payload.expected_revision)
    db.commit()
    return ProjectOut(id=row.id, name=row.name, scenario_id=row.scenario_id, revision=row.revision,
        document=DistillationDocument.model_validate(row.document), created_at=row.created_at,
        updated_at=row.updated_at, can_write=distillation_service.can_write(db, row))


@router.post("/attachments", response_model=AttachmentOut, status_code=201)
def upload_attachment(project_id: str,
    request_id: Annotated[str, Form(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")], file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db)):
    row = attachments.upload(db, project_id, file, request_id)
    db.commit()
    return attachments.public_attachment(row)


@router.get("/attachments", response_model=list[AttachmentOut])
def list_attachments(project_id: str, db: Session = Depends(get_tenant_db)):
    return attachments.list_pending(db, project_id)


@router.delete("/attachments/{attachment_id}", status_code=204)
def remove_attachment(project_id: str, attachment_id: str, db: Session = Depends(get_tenant_db)):
    attachments.remove(db, project_id, attachment_id)
    db.commit()
    return Response(status_code=204)


@preupload_router.post("/attachments", response_model=AttachmentOut, status_code=201)
def upload_preproject_attachment(
    request_id: Annotated[str, Form(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    scenario_id: str | None = Form(None, max_length=32),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
):
    row = attachments.upload(db, None, file, request_id, scenario_id)
    db.commit()
    return attachments.public_attachment(row)


@preupload_router.get("/attachments", response_model=list[AttachmentOut])
def list_preproject_attachments(
    scenario_id: str | None = Query(None, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    return attachments.list_unbound(db, scenario_id)


@preupload_router.delete("/attachments/{attachment_id}", status_code=204)
def remove_preproject_attachment(
    attachment_id: str,
    scenario_id: str | None = Query(None, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    attachments.remove_unbound(db, attachment_id, scenario_id)
    db.commit()
    return Response(status_code=204)
