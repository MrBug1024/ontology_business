"""Scoped API-key lifecycle and authentication for the external v1 API."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..external_api_models import ExternalApiKey, ExternalApiKeyAuditEvent
from ..models import Organization, OrganizationMember, User
from . import permission_service


SUPPORTED_SCOPES = frozenset({"scenarios:read", "objects:read"})
_KEY_HASH_DOMAIN = b"ontology-platform/external-api-key/v1\0"


class ExternalApiKeyError(ValueError):
    """A safe operator-facing validation error for key management."""


@dataclass(frozen=True)
class ExternalApiContext:
    """Authenticated external principal plus its request-scoped DB session."""

    db: Session
    key_id: str
    tenant_id: str
    user_id: str
    scopes: frozenset[str]
    expires_at: datetime | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    """Hash external keys in a domain separate from browser sessions."""
    return hashlib.sha256(_KEY_HASH_DOMAIN + token.encode("utf-8")).hexdigest()


def _normalize_scopes(scopes: object) -> list[str]:
    if not isinstance(scopes, (list, tuple, set, frozenset)):
        raise ExternalApiKeyError("API scope 格式无效")
    normalized = [str(scope or "").strip() for scope in scopes]
    if not normalized or any(not scope for scope in normalized):
        raise ExternalApiKeyError("至少需要选择一个 API scope")
    unknown = sorted(set(normalized) - SUPPORTED_SCOPES)
    if unknown:
        raise ExternalApiKeyError("包含不支持的 API scope")
    if len(set(normalized)) != len(normalized):
        raise ExternalApiKeyError("API scope 不能重复")
    return sorted(normalized)


def _active_member(db: Session, tenant_id: str, user_id: str) -> OrganizationMember | None:
    return db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.organization)
        .where(
            Organization.tenant_id == tenant_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
        )
        .limit(1)
    ).scalars().first()


def issue_key(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    issued_by_user_id: str,
    name: str,
    scopes: list[str],
    expires_in_days: int,
) -> tuple[ExternalApiKey, str]:
    """Create a key and return its raw value exactly once to the caller."""
    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id or user.status != "active":
        raise ExternalApiKeyError("API key 主体不是当前组织的有效用户")
    if not _active_member(db, tenant_id, user_id):
        raise ExternalApiKeyError("API key 主体没有有效组织成员身份")
    issuer = db.get(User, issued_by_user_id)
    if not issuer or issuer.tenant_id != tenant_id or issuer.status != "active":
        raise ExternalApiKeyError("API key 签发者不是当前组织的有效用户")
    if not _active_member(db, tenant_id, issued_by_user_id):
        raise ExternalApiKeyError("API key 签发者没有有效组织成员身份")
    normalized_scopes = _normalize_scopes(scopes)
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 120:
        raise ExternalApiKeyError("API key 名称无效")
    if not 1 <= int(expires_in_days) <= 365:
        raise ExternalApiKeyError("API key 有效期必须在 1 到 365 天之间")

    raw_token = f"ont_sk_{secrets.token_urlsafe(32)}"
    key = ExternalApiKey(
        tenant_id=tenant_id,
        user_id=user_id,
        issued_by_user_id=issued_by_user_id,
        name=clean_name,
        key_prefix=raw_token[:12],
        token_hint=raw_token[-4:],
        token_hash=token_hash(raw_token),
        scopes=normalized_scopes,
        status="active",
        expires_at=utc_now() + timedelta(days=int(expires_in_days)),
    )
    db.add(key)
    db.flush()
    db.add(
        ExternalApiKeyAuditEvent(
            api_key_id=key.id,
            tenant_id=tenant_id,
            subject_user_id=user_id,
            actor_user_id=issued_by_user_id,
            event_type="issued",
            details={
                "scopes": normalized_scopes,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            },
        )
    )
    return key, raw_token


def list_keys(db: Session, tenant_id: str) -> list[ExternalApiKey]:
    return db.execute(
        select(ExternalApiKey)
        .where(ExternalApiKey.tenant_id == tenant_id)
        .order_by(ExternalApiKey.created_at.desc(), ExternalApiKey.id.desc())
    ).scalars().all()


def revoke_key(
    db: Session,
    *,
    tenant_id: str,
    key_id: str,
    revoked_by_user_id: str,
) -> ExternalApiKey | None:
    key = db.execute(
        select(ExternalApiKey).where(
            ExternalApiKey.id == key_id,
            ExternalApiKey.tenant_id == tenant_id,
        )
    ).scalars().first()
    if not key:
        return None
    if key.status != "revoked":
        revoker = db.get(User, revoked_by_user_id)
        if not revoker or revoker.tenant_id != tenant_id or revoker.status != "active":
            raise ExternalApiKeyError("API key 撤销者不是当前组织的有效用户")
        if not _active_member(db, tenant_id, revoked_by_user_id):
            raise ExternalApiKeyError("API key 撤销者没有有效组织成员身份")
        key.status = "revoked"
        key.revoked_at = utc_now()
        key.revoked_by_user_id = revoked_by_user_id
        db.add(
            ExternalApiKeyAuditEvent(
                api_key_id=key.id,
                tenant_id=tenant_id,
                subject_user_id=key.user_id,
                actor_user_id=revoked_by_user_id,
                event_type="revoked",
                details={},
            )
        )
    return key


def _invalid_key() -> HTTPException:
    # Do not distinguish absent, revoked and expired credentials.  This makes
    # token enumeration and lifecycle probing less useful to an attacker.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="外部 API key 无效、已撤销或已过期",
    )


def authenticate(request: Request, db: Session) -> ExternalApiContext:
    """Authenticate strictly from ``X-API-Key``; never fall back to a cookie."""
    raw_token = request.headers.get("X-API-Key", "").strip()
    if not raw_token or len(raw_token) > 512:
        raise _invalid_key()
    now = utc_now()
    key = db.execute(
        select(ExternalApiKey).where(
            ExternalApiKey.token_hash == token_hash(raw_token),
            ExternalApiKey.status == "active",
            ExternalApiKey.expires_at.is_not(None),
            ExternalApiKey.expires_at > now,
        )
    ).scalars().first()
    if not key:
        raise _invalid_key()
    user = db.get(User, key.user_id)
    if not user or user.status != "active" or user.tenant_id != key.tenant_id:
        raise _invalid_key()

    # Bind the request to the subject user and evaluate the same live RBAC/ACL
    # as a first-party request. A role change or membership removal therefore
    # takes effect immediately without rotating every integration key.
    db.info["user_id"] = user.id
    db.info["tenant_id"] = user.tenant_id
    try:
        permission_service.require_principal(db)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            raise _invalid_key() from exc
        raise

    try:
        scopes = frozenset(_normalize_scopes(key.scopes))
    except ExternalApiKeyError as exc:
        # A malformed persisted scope list is never interpreted permissively.
        raise _invalid_key() from exc
    db.execute(
        update(ExternalApiKey)
        .where(ExternalApiKey.id == key.id, ExternalApiKey.status == "active")
        .values(last_used_at=now)
    )
    db.commit()
    return ExternalApiContext(
        db=db,
        key_id=key.id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        scopes=scopes,
        expires_at=key.expires_at,
    )


def require_scope(context: ExternalApiContext, scope: str) -> None:
    if scope not in context.scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key 未授予所需 scope")


def key_metadata(key: ExternalApiKey) -> dict:
    """Return the only safe key representation allowed in an API response."""
    try:
        scopes = _normalize_scopes(key.scopes)
    except ExternalApiKeyError:
        # Corrupt rows remain inspectable/revocable without ever being shown as
        # usable. Authentication independently rejects them above.
        scopes = []
    effective_status = "active" if key.status == "active" and scopes else "revoked"
    return {
        "id": key.id,
        "tenant_id": key.tenant_id,
        "user_id": key.user_id,
        "issued_by_user_id": key.issued_by_user_id,
        "revoked_by_user_id": key.revoked_by_user_id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "token_hint": key.token_hint,
        "scopes": scopes,
        "status": effective_status,
        "expires_at": key.expires_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
        "created_at": key.created_at,
    }
