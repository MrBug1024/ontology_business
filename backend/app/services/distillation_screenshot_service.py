"""Bounded step screenshots as investigation evidence in object storage.

Screenshots exist only for ``observed`` pages (never login forms), are stored
under tenant/project-scoped keys, and are referenced from the tool step. When
object storage is unavailable the step proceeds without a screenshot instead
of failing the investigation.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from . import object_storage_service

logger = logging.getLogger(__name__)
MAX_SCREENSHOT_BYTES = 400_000


def store_screenshot(tenant_id: str, project_id: str, turn_id: str, step_id: str, jpeg: bytes) -> dict | None:
    if not jpeg or len(jpeg) > MAX_SCREENSHOT_BYTES or not object_storage_service.is_configured():
        return None
    digest = hashlib.sha256(jpeg).hexdigest()
    object_key = f"distillation/investigation/{tenant_id}/{project_id}/{turn_id}/{step_id}.jpg"
    try:
        info = object_storage_service.put_object(
            object_storage_service.configuration().bucket_name,
            object_key, jpeg, content_type="image/jpeg", sha256=digest)
    except Exception:  # noqa: BLE001 - evidence enrichment must not fail a turn
        logger.warning("Investigation screenshot upload failed turn=%s step=%s", turn_id, step_id)
        return None
    return {"bucket": info.bucket_name, "object_key": info.object_key, "content_sha256": digest,
            "byte_size": info.size, "media_type": "image/jpeg"}


def decode_step_screenshot(encoded: str | None) -> bytes | None:
    """Decode a connector-supplied base64 screenshot with a hard size bound."""
    if not encoded or len(encoded) > (MAX_SCREENSHOT_BYTES * 4 // 3 + 64):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception:  # noqa: BLE001
        return None
    return data if 0 < len(data) <= MAX_SCREENSHOT_BYTES else None


def load_screenshot(bucket: str, object_key: str) -> bytes | None:
    try:
        return object_storage_service.get_object(bucket, object_key)
    except Exception:  # noqa: BLE001
        return None
