"""One bounded MinIO download for a previously authorized library snapshot."""
from __future__ import annotations

from pathlib import Path
import time

from minio import Minio
import urllib3

from . import object_storage_service


def download_snapshot(bucket: str, key: str, path: Path, *, version_id: str, max_bytes: int, timeout_seconds: float) -> int:
    settings = object_storage_service.require_configuration()
    deadline = time.monotonic() + min(90, timeout_seconds)
    pool = urllib3.PoolManager(num_pools=1, maxsize=1, block=True,
        timeout=urllib3.Timeout(connect=3, read=3), retries=False)
    client = Minio(settings.endpoint, access_key=settings.access_key, secret_key=settings.secret_key,
        secure=settings.secure, http_client=pool)
    response = None
    written = 0
    try:
        response = client.get_object(bucket, key, version_id=version_id or None)
        length = response.headers.get("Content-Length")
        if length and (not length.isdecimal() or int(length) > max_bytes):
            raise ValueError("资料快照超过允许大小")
        with path.open("xb") as handle:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("snapshot download deadline")
                chunk = response.read(min(65536, max_bytes - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("资料快照超过允许大小")
                handle.write(chunk)
        return written
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        pool.clear()
