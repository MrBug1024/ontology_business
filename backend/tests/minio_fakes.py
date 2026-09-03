"""In-memory MinIO double shared by storage-dependent unit tests."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace


class FakeObjectResponse(BytesIO):
    def release_conn(self) -> None:
        return None


class FakeMinioNotFoundError(Exception):
    code = "NoSuchKey"


class FakeMinioNotImplementedError(Exception):
    code = "NotImplemented"
    status = 501


class FakeMinio:
    """Minimal version-aware MinIO client for deterministic unit tests."""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.versioning_status = ""
        self.versioning_not_implemented = False

    def bucket_exists(self, bucket_name: str) -> bool:
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.buckets.add(bucket_name)

    def get_bucket_versioning(self, _bucket_name: str):
        return SimpleNamespace(status=self.versioning_status)

    def set_bucket_versioning(self, _bucket_name: str, config) -> None:
        if self.versioning_not_implemented:
            raise FakeMinioNotImplementedError()
        self.versioning_status = str(config.status or "")

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data,
        length: int,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ):
        content = data.read(length)
        versions = self.objects.setdefault((bucket_name, object_name), [])
        version_id = (
            f"v{len(versions) + 1}"
            if self.versioning_status == "Enabled"
            else ""
        )
        etag = hashlib.sha256(content).hexdigest()
        record: dict[str, object] = {
            "content": content,
            "content_type": content_type,
            "metadata": dict(metadata or {}),
            "version_id": version_id,
            "etag": etag,
        }
        if version_id:
            versions.append(record)
        else:
            self.objects[(bucket_name, object_name)] = [record]
        return SimpleNamespace(
            size=len(content),
            etag=etag,
            version_id=version_id,
            content_type=content_type,
        )

    def fput_object(
        self,
        bucket_name: str,
        object_name: str,
        file_path: str,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ):
        content = Path(file_path).read_bytes()
        return self.put_object(
            bucket_name,
            object_name,
            BytesIO(content),
            len(content),
            content_type=content_type,
            metadata=metadata,
        )

    def _record(
        self,
        bucket_name: str,
        object_name: str,
        version_id: str | None,
    ) -> dict[str, object]:
        versions = self.objects.get((bucket_name, object_name), [])
        if version_id:
            for record in versions:
                if record["version_id"] == version_id:
                    return record
        elif versions:
            return versions[-1]
        raise FakeMinioNotFoundError()

    def overwrite_object(
        self,
        bucket_name: str,
        object_name: str,
        content: bytes,
        *,
        version_id: str = "",
        preserve_etag: bool = False,
    ) -> None:
        """Corrupt an existing object version without changing its identity."""
        record = self._record(bucket_name, object_name, version_id or None)
        record["content"] = content
        if not preserve_etag:
            record["etag"] = hashlib.sha256(content).hexdigest()

    def stat_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ):
        record = self._record(bucket_name, object_name, version_id)
        return SimpleNamespace(
            size=len(record["content"]),
            etag=record["etag"],
            version_id=record["version_id"],
            content_type=record["content_type"],
        )

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ):
        record = self._record(bucket_name, object_name, version_id)
        return FakeObjectResponse(record["content"])

    def remove_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ) -> None:
        key = (bucket_name, object_name)
        versions = self.objects.get(key, [])
        if version_id:
            self.objects[key] = [
                record for record in versions if record["version_id"] != version_id
            ]
            if not self.objects[key]:
                self.objects.pop(key, None)
        else:
            self.objects.pop(key, None)

    def list_objects(
        self,
        bucket_name: str,
        *,
        prefix: str,
        recursive: bool,
        include_version: bool = False,
    ):
        del recursive
        if include_version and self.versioning_not_implemented:
            raise FakeMinioNotImplementedError()
        result = []
        for (bucket, key), versions in self.objects.items():
            if bucket != bucket_name or not key.startswith(prefix):
                continue
            selected = versions if include_version else versions[-1:]
            result.extend(
                SimpleNamespace(
                    object_name=key,
                    size=len(record["content"]),
                    etag=record["etag"],
                    version_id=record["version_id"],
                    content_type=record["content_type"],
                    is_dir=False,
                    is_delete_marker=False,
                )
                for record in selected
            )
        return result
