"""Regression coverage for bounded upload transport before router storage work."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import main
from app.config import Settings
from app.services import upload_service


UPLOAD_LIMIT_BYTES = 400 * 1024 * 1024


class _ChunkedUpload:
    """Small UploadFile-shaped source that records every requested read size."""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _VirtualChunk:
    """Represent a huge chunk without allocating 400 MiB in the test suite."""

    def __init__(self, size: int) -> None:
        self.size = size

    def __len__(self) -> int:
        return self.size

    def __bool__(self) -> bool:
        return True


class _VirtualUpload:
    def __init__(self, chunk: _VirtualChunk) -> None:
        self._chunks: list[object] = [chunk, b""]
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> object:
        self.read_sizes.append(size)
        return self._chunks.pop(0)


class _DiscardingDestination:
    """Closes the mkstemp descriptor while discarding a virtual large chunk."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.written = 0

    def __enter__(self) -> "_DiscardingDestination":
        return self

    def __exit__(self, *_args) -> None:
        os.close(self.descriptor)

    def write(self, chunk: object) -> int:
        self.written += len(chunk)  # type: ignore[arg-type]
        return len(chunk)  # type: ignore[arg-type]


class _VirtualDigest:
    def __init__(self) -> None:
        self.total = 0

    def update(self, chunk: object) -> None:
        self.total += len(chunk)  # type: ignore[arg-type]

    def hexdigest(self) -> str:
        return "virtual-digest"


def test_default_upload_limit_is_400_mib() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:password@localhost/ontology",
        mail_starttls=False,
        mail_ssl_tls=False,
    )

    assert settings.max_upload_bytes == UPLOAD_LIMIT_BYTES


def test_stage_upload_streams_fixed_chunks_to_a_temporary_file() -> None:
    content = b"streamed-content"
    upload = _ChunkedUpload(content)

    staged = asyncio.run(
        upload_service.stage_upload(upload, max_bytes=UPLOAD_LIMIT_BYTES, chunk_bytes=3)
    )
    try:
        assert staged.size == len(content)
        assert staged.sha256 == hashlib.sha256(content).hexdigest()
        assert staged.path.read_bytes() == content
        assert upload.read_sizes == [3] * ((len(content) + 2) // 3 + 1)
    finally:
        with upload_service.cleanup_staged_upload(staged):
            assert staged.path.exists()
    assert not staged.path.exists()


def test_stage_upload_accepts_the_exact_400_mib_limit_without_buffering_it() -> None:
    upload = _VirtualUpload(_VirtualChunk(UPLOAD_LIMIT_BYTES))

    def open_discarding_destination(descriptor: int, *_args, **_kwargs):
        return _DiscardingDestination(descriptor)

    with (
        patch("builtins.open", side_effect=open_discarding_destination),
        patch.object(upload_service.hashlib, "sha256", return_value=_VirtualDigest()),
    ):
        staged = asyncio.run(
            upload_service.stage_upload(upload, max_bytes=UPLOAD_LIMIT_BYTES)
        )
    try:
        assert staged.size == UPLOAD_LIMIT_BYTES
        assert staged.sha256 == "virtual-digest"
        assert upload.read_sizes == [
            upload_service.DEFAULT_UPLOAD_CHUNK_BYTES,
            upload_service.DEFAULT_UPLOAD_CHUNK_BYTES,
        ]
    finally:
        staged.path.unlink(missing_ok=True)


def test_stage_upload_rejects_the_first_byte_over_400_mib() -> None:
    upload = _VirtualUpload(_VirtualChunk(UPLOAD_LIMIT_BYTES + 1))

    def open_discarding_destination(descriptor: int, *_args, **_kwargs):
        return _DiscardingDestination(descriptor)

    with (
        patch("builtins.open", side_effect=open_discarding_destination),
        patch.object(upload_service.hashlib, "sha256", return_value=_VirtualDigest()),
    ):
        try:
            asyncio.run(upload_service.stage_upload(upload, max_bytes=UPLOAD_LIMIT_BYTES))
        except upload_service.UploadTooLargeError as exc:
            assert "400 MB" in str(exc)
        else:  # pragma: no cover - guards against a future silent truncation.
            raise AssertionError("expected UploadTooLargeError")


def test_request_body_over_the_transport_allowance_returns_413() -> None:
    request = SimpleNamespace(
        headers={"content-length": str(UPLOAD_LIMIT_BYTES + 1024 * 1024 + 1)}
    )

    async def call_next(_request):  # pragma: no cover - a 413 must short-circuit.
        raise AssertionError("oversized body reached the route")

    with patch.object(main, "settings", SimpleNamespace(max_upload_bytes=UPLOAD_LIMIT_BYTES)):
        response = asyncio.run(main.reject_oversized_request_body(request, call_next))

    assert response.status_code == 413
