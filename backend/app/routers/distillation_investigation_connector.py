"""Investigation connector adapters: session HTTP API, WebSocket gateway, script.

The HTTP surface is project-member scoped. The WebSocket endpoint authenticates
connectors with a one-shot bearer token (never a URL parameter), re-validates
the token after hello, and bridges loop-side sends with worker-side receives
through the process-local gateway. Single-instance boundary: the gateway lives
in the API process together with the conversation worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..distillation_investigation_connector_schemas import (
    ConnectorCommandOut, ConnectorSessionCreate, ConnectorSessionOut, ConnectorSessionPage)
from ..distillation_models import DistillationProject
from ..distillation_schemas import DistillationDocument
from ..services import distillation_investigation_connector_service as connectors
from ..services.distillation_investigation_connector_gateway import (
    CLOSE_IDLE, CLOSE_REPLACED, CLOSE_REVOKED, CLOSE_UNAUTHORIZED, CLOSE_UPGRADE_REQUIRED,
    PROTOCOL_VERSION, gateway)
from ..services.auth_service import get_tenant_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/business-distillation/{project_id}/connector-sessions", tags=["business-distillation"])
connector_ws_router = APIRouter()


def _project_for_connector(db: Session, project_id: str) -> DistillationProject:
    from ..services import distillation_service

    row = db.scalar(select(DistillationProject).where(
        DistillationProject.id == project_id,
        DistillationProject.tenant_id == (db.info.get("tenant_id") or "")))
    if row is None:
        raise HTTPException(status_code=404, detail="业务蒸馏项目不存在")
    if not distillation_service.can_write(db, row):
        raise HTTPException(status_code=403, detail="没有该项目的调查配置权限")
    return row


def _origin(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"
    base = str(request.base_url).rstrip("/")
    return base or str(request.url.origin)


@router.post("", response_model=ConnectorCommandOut, status_code=201)
def create_connector_session(project_id: str, payload: ConnectorSessionCreate,
                             request: Request, db: Session = Depends(get_tenant_db)):
    project = _project_for_connector(db, project_id)
    document = DistillationDocument.model_validate(project.document)
    target = next((item for item in document.target_systems
                   if item.key == payload.target_key), None)
    if target is None or not target.enabled or target.browser is None:
        raise HTTPException(status_code=409, detail="目标系统未配置可调查的网站访问")
    payload_out, _token = connectors.create_session(
        db, project_id, db.info.get("user_id") or "", target, origin=_origin(request))
    db.commit()
    return ConnectorCommandOut.model_validate(payload_out)


@router.get("", response_model=ConnectorSessionPage)
def list_connector_sessions(project_id: str, db: Session = Depends(get_tenant_db)):
    _project_for_connector(db, project_id)
    sessions = connectors.list_sessions(db, project_id)
    db.commit()
    return ConnectorSessionPage(sessions=[ConnectorSessionOut.model_validate(item) for item in sessions])


@router.delete("/{session_id}", response_model=ConnectorSessionOut)
def revoke_connector_session(project_id: str, session_id: str,
                             reason: Annotated[str, Query(max_length=200)] = "已手动断开",
                             db: Session = Depends(get_tenant_db)):
    _project_for_connector(db, project_id)
    payload = connectors.revoke(db, project_id, session_id, reason)
    db.commit()
    gateway().disconnect(session_id, CLOSE_REVOKED)
    return ConnectorSessionOut.model_validate(payload)


def _authenticate_connector(token: str) -> dict | None:
    with SessionLocal() as db:
        row = connectors.find_active_by_token(db, token)
        db.commit()
        if row is None:
            return None
        return {"id": row.id, "project_id": row.project_id, "target_key": row.target_key}


def _mark_connected(session_id: str, info: dict) -> None:
    with SessionLocal() as db:
        row = db.get(connectors.ConnectorSession, session_id)
        if row is not None and row.status in ("pending", "connected"):
            connectors.mark_connected(db, row, info)
        db.commit()


def _touch(session_id: str) -> None:
    with SessionLocal() as db:
        connectors.touch(db, session_id)
        db.commit()


def _mark_disconnected(session_id: str) -> None:
    with SessionLocal() as db:
        connectors.mark_disconnected(db, session_id)
        db.commit()


@connector_ws_router.websocket("/distillation-connector/ws")
async def connector_socket(websocket: WebSocket) -> None:
    settings = get_settings()
    if not settings.distillation_connector_enabled:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    authorization = websocket.headers.get("authorization") or ""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    session = await asyncio.to_thread(_authenticate_connector, token)
    if session is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    await websocket.accept()
    try:
        hello_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = json.loads(hello_raw)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await websocket.close(code=CLOSE_UPGRADE_REQUIRED)
        return
    capabilities = hello.get("capabilities")
    if hello.get("type") != "hello" or not isinstance(capabilities, list) or "browser" not in capabilities:
        await websocket.close(code=CLOSE_UPGRADE_REQUIRED)
        return
    # Re-check after hello: the token may have been rotated while connecting.
    recheck = await asyncio.to_thread(_authenticate_connector, token)
    if recheck is None or recheck["id"] != session["id"]:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    await asyncio.to_thread(_mark_connected, session["id"], hello)
    connection, replaced = gateway().register(
        session["id"], session["project_id"], session["target_key"], asyncio.get_running_loop())
    if replaced is not None:
        replaced.close_from_worker(CLOSE_REPLACED)

    async def sender() -> None:
        while True:
            item = await connection.outbound.get()
            if isinstance(item, dict) and "close" in item:
                await websocket.close(code=int(item["close"]))
                return
            await websocket.send_text(item)

    sender_task = asyncio.create_task(sender())
    try:
        await websocket.send_text(json.dumps(
            {"type": "accepted", "session_id": session["id"], "protocol_version": PROTOCOL_VERSION}))
        last_touch = 0.0
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=settings.distillation_connector_idle_timeout_seconds)
            except asyncio.TimeoutError:
                await websocket.close(code=CLOSE_IDLE)
                break
            connection.mark_seen()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "response":
                connection.inbound.put_nowait(message)
            elif message.get("type") == "ping":
                connection.outbound.put_nowait(json.dumps({"type": "pong"}))
            if time.monotonic() - last_touch >= 15:
                last_touch = time.monotonic()
                await asyncio.to_thread(_touch, session["id"])
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - gateway teardown must not raise into the loop
        logger.warning("Connector socket ended session=%s", session["id"])
    finally:
        sender_task.cancel()
        gateway().unregister(session["id"], connection)
        await asyncio.to_thread(_mark_disconnected, session["id"])


@connector_ws_router.get("/distillation-connector/script")
def connector_script() -> FileResponse:
    """Serve the generic connector CLI; it contains no secrets or tenant data."""
    script = Path(__file__).resolve().parents[2] / "connectors" / "investigation_connector.py"
    if not script.is_file():
        raise HTTPException(status_code=404, detail="连接器脚本未随部署提供")
    return FileResponse(script, media_type="text/x-python", filename="investigation_connector.py",
                        headers={"Cache-Control": "no-store"})
