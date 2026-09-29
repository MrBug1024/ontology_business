import asyncio
import hashlib
from io import BytesIO

from fastapi import UploadFile
import pytest

from app.services import upload_staging_service as staging


@pytest.mark.parametrize("asynchronous", [False, True])
def test_upload_staging_preserves_content_hash_and_closes_input(asynchronous):
    content = b"synthetic uploaded content" * 5000
    upload = UploadFile(filename="data.csv", file=BytesIO(content))
    if asynchronous:
        result = asyncio.run(staging.stage_upload(upload, max_bytes=len(content), chunk_bytes=4096))
    else:
        result = staging.stage_upload_sync(upload, max_bytes=len(content), chunk_bytes=4096)
    try:
        assert result.path.read_bytes() == content
        assert result.content_sha256 == hashlib.sha256(content).hexdigest()
        assert result.byte_size == len(content)
        assert upload.file.closed
    finally:
        result.remove()


def test_oversize_staging_closes_stream_and_removes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(staging.tempfile, "tempdir", str(tmp_path))
    upload = UploadFile(filename="large.csv", file=BytesIO(b"x" * 100))
    with pytest.raises(staging.UploadTooLargeError):
        staging.stage_upload_sync(upload, max_bytes=10, chunk_bytes=4)
    assert upload.file.closed
    assert list(tmp_path.iterdir()) == []
