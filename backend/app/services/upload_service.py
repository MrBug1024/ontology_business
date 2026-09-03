"""Bounded, disk-backed multipart upload staging.

``UploadFile.read()`` without a size argument can materialise an attacker
controlled body in an API worker.  This helper consumes multipart files in
fixed-size chunks, enforces the exact per-file limit, and gives storage callers
a normal temporary file suitable for MinIO's streaming file upload API.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile
from typing import AsyncIterator, Iterator

from fastapi import UploadFile


DEFAULT_UPLOAD_CHUNK_BYTES = 1024 * 1024


class UploadTooLargeError(ValueError):
    """Raised when a single uploaded file exceeds the configured ceiling."""


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    size: int
    sha256: str


@contextmanager
def cleanup_staged_upload(upload: StagedUpload) -> Iterator[StagedUpload]:
    """Remove the application-owned temporary file after its request finishes."""
    try:
        yield upload
    finally:
        try:
            upload.path.unlink(missing_ok=True)
        except OSError:
            # The request outcome is more important than best-effort cleanup;
            # the operating system's temporary-directory policy remains the
            # final fallback for an interrupted process.
            pass


async def stage_upload(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES,
) -> StagedUpload:
    """Copy one multipart body to disk while enforcing an exact byte limit."""
    if max_bytes < 1:
        raise ValueError("上传大小限制必须大于 0")
    if chunk_bytes < 1:
        raise ValueError("上传分块大小必须大于 0")

    descriptor, raw_path = tempfile.mkstemp(prefix="ontology-upload-", suffix=".part")
    path = Path(raw_path)
    total = 0
    digest = hashlib.sha256()
    try:
        with open(descriptor, "wb", closefd=True) as destination:
            while True:
                chunk = await upload.read(chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"文件超过大小限制（{max_bytes // (1024 * 1024)} MB）"
                    )
                destination.write(chunk)
                digest.update(chunk)
        return StagedUpload(path=path, size=total, sha256=digest.hexdigest())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

