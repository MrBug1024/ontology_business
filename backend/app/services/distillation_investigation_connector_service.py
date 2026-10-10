"""Durable lifecycle for member-machine investigation connector sessions.

The plaintext token is returned exactly once at creation and travels only in
the generated command line; the database keeps a domain-separated hash in the
same style as the platform's other token domains. Revocation and expiry are
authoritative rows that the WebSocket gateway re-checks on every hello.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..distillation_investigation_connector_models import DistillationInvestigationConnectorSession as ConnectorSession
from ..distillation_target_schemas import TargetSystem
from .distillation_access_service import target_hash

_TOKEN_PREFIX = "diconn_"
_HASH_DOMAIN = b"ontology-business:distillation-investigation-connector:v1:"
# The command line is copied into a member terminal; keep it a single portable
# python invocation with strictly validated arguments (no shell metacharacters).
_COMMAND_ARG_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._:/?=&~#[]@$()*+,%!")


def generate_token() -> str:
    return _TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def token_hash(token: str) -> str:
    return hashlib.sha256(_HASH_DOMAIN + token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry() -> datetime:
    return _now() + timedelta(seconds=get_settings().distillation_connector_token_ttl_seconds)


def _quote_command_arg(value: str) -> str:
    if value and all(character in _COMMAND_ARG_SAFE for character in value):
        return value
    raise ValueError("连接器命令参数包含不可用字符")


def connector_command(origin: str, token: str) -> str:
    server = _quote_command_arg(origin.rstrip("/"))
    quoted = _quote_command_arg(token)
    return (
        f'python investigation_connector.py --server "{server}" --token "{quoted}"'
    )


def _public(row) -> dict:
    info = getattr(row, "connector_info", None) or {}
    return {
        "id": row.id,
        "target_key": row.target_key,
        "status": row.status,
        "token_prefix": row.token_prefix,
        "connector_platform": str(info.get("platform") or "")[:200],
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "connected_at": row.connected_at,
        "last_seen_at": row.last_seen_at,
        "revoked_reason": row.revoked_reason or "",
    }


def create_session(db: Session, project_id: str, user_id: str, target: TargetSystem,
                   *, origin: str) -> tuple[dict, str]:
    """Create (or rotate) the single active connector session for one target.

    Creating always rotates: any previous pending/connected session for the
    same project+target is revoked first, so an old member machine cannot stay
    attached after a new command is issued.
    """
    if not get_settings().distillation_connector_enabled:
        raise HTTPException(status_code=409, detail="当前部署尚未启用本机调查连接器")
    now = _now()
    for previous in db.scalars(select(ConnectorSession).where(
            ConnectorSession.project_id == project_id, ConnectorSession.target_key == target.key,
            ConnectorSession.status.in_(["pending", "connected"]))).all():
        previous.status, previous.revoked_at, previous.revoked_reason = "revoked", now, "已生成新连接命令"
    token = generate_token()
    row = ConnectorSession(
        tenant_id=db.info.get("tenant_id") or "",
        project_id=project_id, created_by=user_id, target_key=target.key,
        scope_hash=target_hash(target), token_hash=token_hash(token),
        token_prefix=token[:12], status="pending", expires_at=_expiry(),
    )
    db.add(row)
    db.flush()
    command = connector_command(origin, token)
    script_url = f"{origin.rstrip('/')}{get_settings().api_prefix}/distillation-connector/script"
    payload = _public(row) | {"command": command, "script_url": script_url}
    return payload, token


def find_active_by_token(db: Session, token: str) -> ConnectorSession | None:
    if not token.startswith(_TOKEN_PREFIX):
        return None
    row = db.scalar(select(ConnectorSession).where(
        ConnectorSession.token_hash == token_hash(token),
        ConnectorSession.status.in_(["pending", "connected"])))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= _now():
        row.status, row.revoked_reason = "expired", "令牌已过期"
        return None
    return row


def mark_connected(db: Session, row: ConnectorSession, info: dict) -> None:
    row.status = "connected"
    row.connected_at = row.last_seen_at = _now()
    row.disconnected_at = None
    bounded = {key: str(value)[:200] for key, value in (info or {}).items()
               if key in {"platform", "version", "capabilities"} and isinstance(value, (str, int, float, list))}
    if bounded:
        row.connector_info = bounded


def mark_disconnected(db: Session, session_id: str) -> None:
    row = db.get(ConnectorSession, session_id)
    if row is not None and row.status == "connected":
        row.status, row.disconnected_at = "pending", _now()


def touch(db: Session, session_id: str) -> None:
    row = db.get(ConnectorSession, session_id)
    if row is not None and row.status == "connected":
        row.last_seen_at = _now()


def revoke(db: Session, project_id: str, session_id: str, reason: str) -> dict:
    row = db.get(ConnectorSession, session_id)
    if row is None or row.project_id != project_id or row.tenant_id != (db.info.get("tenant_id") or ""):
        raise HTTPException(status_code=404, detail="连接器会话不存在")
    if row.status not in ("revoked", "expired"):
        row.status, row.revoked_at = "revoked", _now()
        row.revoked_reason = reason[:200]
    return _public(row)


def list_sessions(db: Session, project_id: str) -> list[dict]:
    rows = db.scalars(select(ConnectorSession).where(
        ConnectorSession.project_id == project_id,
        ConnectorSession.status.in_(["pending", "connected", "revoked", "expired"]),
    ).order_by(ConnectorSession.created_at.desc()).limit(8)).all()
    now = _now()
    for row in rows:
        if row.status in ("pending", "connected"):
            expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
            if expires <= now:
                row.status, row.revoked_reason = "expired", "令牌已过期"
    return [_public(row) for row in rows]


def active_connection_session(db: Session, project_id: str, target: TargetSystem) -> ConnectorSession | None:
    """The session a browser turn should use, if a member machine is attached.

    Scope must still match the current target configuration; any change to the
    target system invalidates the attached executor.
    """
    row = db.scalar(select(ConnectorSession).where(
        ConnectorSession.project_id == project_id, ConnectorSession.target_key == target.key,
        ConnectorSession.status == "connected"))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= _now() or row.scope_hash != target_hash(target):
        row.status, row.revoked_reason = "expired", "连接器范围已变化，请重新连接"
        return None
    return row


def sweep_expired(db: Session) -> None:
    """Bound stale rows without a background timer; callers invoke opportunistically."""
    now = _now()
    for row in db.scalars(select(ConnectorSession).where(
            ConnectorSession.status.in_(["pending", "connected"]))).all():
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if expires <= now:
            row.status, row.revoked_reason = "expired", "令牌已过期"
