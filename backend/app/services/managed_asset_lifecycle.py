"""Pure lifecycle checks shared by managed-input protocol boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


class ManagedAssetLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def require_current_asset_version(
    version_document: Mapping[str, Any] | None,
    *,
    as_of: datetime | None = None,
) -> None:
    document = version_document if isinstance(version_document, Mapping) else {}
    lifecycle = document.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or not bool(lifecycle.get("temporary")):
        return
    try:
        expires_at = datetime.fromisoformat(
            str(lifecycle.get("expires_at") or "").replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ManagedAssetLifecycleError(
            "managed_reference_not_ready",
            "temporary managed attachment has no valid expiry",
        ) from None
    now = as_of or datetime.now(timezone.utc)
    if expires_at <= now:
        raise ManagedAssetLifecycleError(
            "managed_reference_expired",
            "temporary managed attachment has expired",
        )


__all__ = [
    "ManagedAssetLifecycleError",
    "require_current_asset_version",
]
