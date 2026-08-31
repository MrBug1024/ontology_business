"""Bounded, constant-memory staging for multipart uploads.

Starlette may spool an ``UploadFile`` itself, but application code must not
call ``read()`` without a streaming boundary.  This module gives catalog,
modeling and external APIs one audited size/hash contract before MinIO PUT.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import AsyncIterator

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedUpload:
    path: Path
    byte_size: int
    content_sha256: str

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


async def stage_upload(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_bytes: int,
) -> StagedUpload:
    """Copy an untrusted upload into a private file while hashing and limiting it."""
    limit = int(max_bytes)
    chunk_size = int(chunk_bytes)
    if limit <= 0 or chunk_size <= 0:
        raise ValueError("上传流配置无效")
    suffix = Path(str(upload.filename or "")).suffix.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".part"
    # Some parsers validate a path's extension before inspecting its signature.
    descriptor, raw_path = tempfile.mkstemp(prefix="ontology-upload-", suffix=suffix)
    path = Path(raw_path).resolve()
    hasher = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                chunk = await upload.read(min(chunk_size, limit - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise UploadTooLargeError("文件超过允许的上传大小")
                hasher.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if written <= 0:
            raise ValueError("上传文件不能为空")
        return StagedUpload(
            path=path,
            byte_size=written,
            content_sha256=hasher.hexdigest(),
        )
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        await upload.close()
