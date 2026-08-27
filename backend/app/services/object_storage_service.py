"""Managed MinIO object storage with stable, non-presigned identities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..config import get_settings


class ObjectStorageError(RuntimeError):
    """A non-sensitive managed storage failure."""


class ObjectStorageConfigurationError(ObjectStorageError):
    """The server-side MinIO configuration is missing or unsafe."""


@dataclass(frozen=True)
class MinioConfiguration:
    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    prefix: str
    secure: bool = True

    @property
    def configured(self) -> bool:
        return bool(
            self.endpoint
            and self.access_key
            and self.secret_key
            and self.bucket_name
        )


@dataclass(frozen=True)
class ObjectInfo:
    bucket_name: str
    object_key: str
    size: int = 0
    etag: str = ""
    version_id: str = ""
    content_type: str = ""
    last_modified: datetime | None = None
    is_delete_marker: bool = False


_client: Any | None = None
_client_signature: tuple[str, str, str, bool] | None = None
_client_lock = threading.Lock()


def normalize_prefix(value: str) -> str:
    prefix = str(value or "").strip().replace("\\", "/").strip("/")
    if not prefix:
        return ""
    parts = prefix.split("/")
    if any(
        not part
        or part in {".", ".."}
        or any(ord(character) < 32 for character in part)
        for part in parts
    ) or len(prefix) > 1024:
        raise ObjectStorageConfigurationError("MinIO 对象前缀格式无效")
    return "/".join(parts)


def _normalize_endpoint(value: str) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        return "", True
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme.lower() != "https":
        raise ObjectStorageConfigurationError("MinIO 端点必须使用 HTTPS")
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ObjectStorageConfigurationError("MinIO 端点格式无效")
    return parsed.netloc, True


def configuration() -> MinioConfiguration:
    settings = get_settings()
    endpoint, secure = _normalize_endpoint(
        getattr(settings, "minio_aliyun_endpoint", "")
    )
    return MinioConfiguration(
        endpoint=endpoint,
        access_key=str(
            getattr(settings, "minio_aliyun_access_key_id", "") or ""
        ).strip(),
        secret_key=str(
            getattr(settings, "minio_aliyun_access_key_secret", "") or ""
        ).strip(),
        bucket_name=str(getattr(settings, "minio_bucketname", "") or "").strip(),
        prefix=normalize_prefix(
            str(getattr(settings, "minio_aliyun_file_path", "") or "")
        ),
        secure=secure,
    )


def is_configured() -> bool:
    return configuration().configured


def require_configuration() -> MinioConfiguration:
    configured = configuration()
    if not configured.configured:
        raise ObjectStorageConfigurationError("服务端 MinIO 配置不完整")
    return configured


def _create_client(configured: MinioConfiguration) -> Any:
    try:
        from minio import Minio
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise ObjectStorageConfigurationError("服务端缺少 MinIO 客户端依赖") from exc
    return Minio(
        configured.endpoint,
        access_key=configured.access_key,
        secret_key=configured.secret_key,
        secure=configured.secure,
    )


def get_client() -> Any:
    """Create the MinIO client lazily and refresh it after configuration changes."""
    global _client, _client_signature
    configured = require_configuration()
    signature = (
        configured.endpoint,
        configured.access_key,
        configured.secret_key,
        configured.secure,
    )
    if _client is not None and _client_signature == signature:
        return _client
    with _client_lock:
        if _client is None or _client_signature != signature:
            _client = _create_client(configured)
            _client_signature = signature
    return _client


def reset_client_cache() -> None:
    """Drop only the local client handle; useful after settings/test changes."""
    global _client, _client_signature
    with _client_lock:
        _client = None
        _client_signature = None


def _validate_bucket_name(bucket_name: str) -> str:
    value = str(bucket_name or "").strip()
    if (
        not 3 <= len(value) <= 63
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", value)
        or ".." in value
        or ".-" in value
        or "-." in value
    ):
        raise ObjectStorageConfigurationError("MinIO bucket 名称格式无效")
    return value


def _validate_object_key(object_key: str) -> str:
    value = str(object_key or "").strip("/")
    parts = value.split("/") if value else []
    if (
        not parts
        or len(value) > 2048
        or any(
            not part
            or part in {".", ".."}
            or "\\" in part
            or any(ord(character) < 32 for character in part)
            for part in parts
        )
    ):
        raise ObjectStorageError("MinIO 对象键格式无效")
    return "/".join(parts)


def stable_object_url(bucket_name: str, object_key: str) -> str:
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    url = f"minio://{bucket}/{quote(key, safe='/-_.~')}"
    if len(url) > 4096:
        raise ObjectStorageError("MinIO 对象地址超过持久化长度限制")
    return url


def parse_object_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(str(value or ""))
    if (
        parsed.scheme.lower() != "minio"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ObjectStorageError("MinIO 对象地址格式无效")
    return _validate_bucket_name(parsed.netloc), _validate_object_key(
        unquote(parsed.path).lstrip("/")
    )


def _not_found(exc: BaseException) -> bool:
    return str(getattr(exc, "code", "") or "") in {
        "NoSuchBucket",
        "NoSuchKey",
        "NoSuchObject",
        "NoSuchVersion",
        "NotFound",
    }


def _versioning_not_supported(exc: BaseException) -> bool:
    """Recognize S3-compatible gateways that do not implement versioning."""
    code = str(getattr(exc, "code", "") or "").strip().lower()
    status = getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status", None)
    return code in {
        "notimplemented",
        "notimplementedexception",
        "unsupported",
        "unsupportedoperation",
        "xnotimplemented",
    } or status == 501


def ensure_bucket(bucket_name: str) -> None:
    bucket = _validate_bucket_name(bucket_name)
    client = get_client()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception as exc:  # noqa: BLE001
        # Another instance may have won the create race.
        try:
            exists_after_race = bool(client.bucket_exists(bucket))
        except Exception:  # noqa: BLE001
            exists_after_race = False
        if not exists_after_race:
            raise ObjectStorageError("MinIO bucket 初始化失败") from exc
    try:
        from minio.versioningconfig import ENABLED, VersioningConfig

        versioning = client.get_bucket_versioning(bucket)
        if str(getattr(versioning, "status", "") or "") != ENABLED:
            client.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
            versioning = client.get_bucket_versioning(bucket)
        if str(getattr(versioning, "status", "") or "") != ENABLED:
            raise ObjectStorageError("MinIO bucket 版本控制未启用")
    except Exception as exc:  # noqa: BLE001
        if _versioning_not_supported(exc):
            return
        if isinstance(exc, ObjectStorageError):
            raise
        raise ObjectStorageError("MinIO bucket 版本控制初始化失败") from exc


def healthcheck() -> bool:
    """Return MinIO readiness without exposing SDK or endpoint diagnostics."""
    try:
        configured = require_configuration()
        client = get_client()
        return bool(client.bucket_exists(configured.bucket_name))
    except Exception:  # noqa: BLE001 - health responses must remain non-sensitive.
        return False


def _object_info(
    value: Any,
    bucket_name: str,
    object_key: str,
    *,
    size: int = 0,
    content_type: str = "",
) -> ObjectInfo:
    version_id = str(getattr(value, "version_id", "") or "").strip()
    return ObjectInfo(
        bucket_name=bucket_name,
        object_key=object_key,
        size=int(getattr(value, "size", size) or size),
        etag=str(getattr(value, "etag", "") or "").strip('"'),
        version_id=version_id,
        content_type=str(getattr(value, "content_type", content_type) or content_type),
        last_modified=getattr(value, "last_modified", None),
        is_delete_marker=bool(getattr(value, "is_delete_marker", False)),
    )


def put_object(
    bucket_name: str,
    object_key: str,
    content: bytes,
    *,
    content_type: str = "application/octet-stream",
    sha256: str = "",
) -> ObjectInfo:
    if not isinstance(content, bytes):
        raise ValueError("对象内容必须是字节数据")
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    ensure_bucket(bucket)
    metadata = {"sha256": sha256} if sha256 else None
    try:
        result = get_client().put_object(
            bucket,
            key,
            BytesIO(content),
            len(content),
            content_type=content_type or "application/octet-stream",
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        raise ObjectStorageError("MinIO 对象上传失败") from exc
    return _object_info(
        result,
        bucket,
        key,
        size=len(content),
        content_type=content_type,
    )


def put_file(
    bucket_name: str,
    object_key: str,
    source_path: str | Path,
    *,
    content_type: str = "application/octet-stream",
    sha256: str = "",
) -> ObjectInfo:
    """Upload a local file without first loading it into application memory."""
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    unresolved_path = Path(source_path).expanduser()
    if unresolved_path.is_symlink():
        raise ValueError("上传源必须是普通文件")
    path = unresolved_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("上传源必须是普通文件")
    ensure_bucket(bucket)
    size = path.stat().st_size
    metadata = {"sha256": sha256} if sha256 else None
    try:
        result = get_client().fput_object(
            bucket,
            key,
            str(path),
            content_type=content_type or "application/octet-stream",
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        raise ObjectStorageError("MinIO 文件上传失败") from exc
    return _object_info(
        result,
        bucket,
        key,
        size=size,
        content_type=content_type,
    )


def stat_object(
    bucket_name: str,
    object_key: str,
    *,
    version_id: str = "",
) -> ObjectInfo:
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    try:
        result = get_client().stat_object(
            bucket, key, version_id=version_id or None
        )
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            raise FileNotFoundError("MinIO 对象不存在") from exc
        raise ObjectStorageError("MinIO 对象状态读取失败") from exc
    return _object_info(result, bucket, key)


def get_object(
    bucket_name: str,
    object_key: str,
    *,
    version_id: str = "",
) -> bytes:
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    response: Any | None = None
    try:
        response = get_client().get_object(
            bucket, key, version_id=version_id or None
        )
        return bytes(response.read())
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            raise FileNotFoundError("MinIO 对象不存在") from exc
        raise ObjectStorageError("MinIO 对象读取失败") from exc
    finally:
        if response is not None:
            try:
                response.close()
            finally:
                release = getattr(response, "release_conn", None)
                if callable(release):
                    release()


def download_object_to_file(
    bucket_name: str,
    object_key: str,
    destination_path: str | Path,
    *,
    version_id: str = "",
    max_bytes: int | None = None,
) -> ObjectInfo:
    """Stream one managed object to an explicit local cache file.

    The caller chooses the cache path independently of the object key. This
    prevents an object name from becoming a local filesystem path.
    """
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    destination = Path(destination_path).expanduser()
    if not destination.is_absolute():
        raise ValueError("下载目标必须是绝对路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination.is_dir() or destination.is_symlink()):
        raise ValueError("下载目标必须是普通文件路径")
    if max_bytes is not None:
        limit = int(max_bytes)
        if limit <= 0:
            raise ValueError("下载大小上限必须是正整数")
        if destination.exists() or destination.is_symlink():
            raise ValueError("受限下载目标必须是新文件路径")
        current = stat_object(bucket, key, version_id=version_id)
        if current.size > limit:
            raise ObjectStorageError("MinIO 对象超过允许的下载大小")
        response: Any | None = None
        written = 0
        try:
            response = get_client().get_object(
                bucket, key, version_id=version_id or None
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    chunk = response.read(min(1024 * 1024, limit - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise ObjectStorageError("MinIO 对象超过允许的下载大小")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if current.size and written != current.size:
                raise ObjectStorageError("MinIO 对象大小在下载期间发生变化")
            return ObjectInfo(
                bucket_name=bucket,
                object_key=key,
                size=written,
                etag=current.etag,
                version_id=current.version_id,
                content_type=current.content_type,
                last_modified=current.last_modified,
                is_delete_marker=current.is_delete_marker,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            if _not_found(exc):
                raise FileNotFoundError("MinIO 对象不存在") from exc
            if isinstance(exc, ObjectStorageError):
                raise
            raise ObjectStorageError("MinIO 对象下载失败") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                finally:
                    release = getattr(response, "release_conn", None)
                    if callable(release):
                        release()
    try:
        result = get_client().fget_object(
            bucket,
            key,
            str(destination),
            version_id=version_id or None,
        )
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            raise FileNotFoundError("MinIO 对象不存在") from exc
        raise ObjectStorageError("MinIO 对象下载失败") from exc
    size = destination.stat().st_size
    return _object_info(result, bucket, key, size=size)


def delete_object(
    bucket_name: str,
    object_key: str,
    *,
    version_id: str = "",
) -> None:
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    try:
        get_client().remove_object(bucket, key, version_id=version_id or None)
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            return
        raise ObjectStorageError("MinIO 对象删除失败") from exc


def delete_object_url(value: str) -> None:
    bucket, key = parse_object_url(value)
    delete_object(bucket, key)


def list_objects(bucket_name: str, prefix: str = "") -> list[ObjectInfo]:
    bucket = _validate_bucket_name(bucket_name)
    normalized_prefix = normalize_prefix(prefix)
    try:
        objects = get_client().list_objects(
            bucket,
            prefix=(f"{normalized_prefix}/" if normalized_prefix else ""),
            recursive=True,
        )
        return [
            _object_info(item, bucket, str(item.object_name))
            for item in objects
            if not bool(getattr(item, "is_dir", False))
        ]
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            return []
        raise ObjectStorageError("MinIO 对象列表读取失败") from exc


def list_object_versions(bucket_name: str, prefix: str = "") -> list[ObjectInfo]:
    """List every object version below a managed prefix, including markers."""
    bucket = _validate_bucket_name(bucket_name)
    normalized_prefix = normalize_prefix(prefix)
    try:
        objects = get_client().list_objects(
            bucket,
            prefix=(f"{normalized_prefix}/" if normalized_prefix else ""),
            recursive=True,
            include_version=True,
        )
        return [
            _object_info(item, bucket, str(item.object_name))
            for item in objects
            if not bool(getattr(item, "is_dir", False))
        ]
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            return []
        raise ObjectStorageError("MinIO 对象版本列表读取失败") from exc


def _delete_current_object_if_present(bucket_name: str, object_key: str) -> None:
    try:
        current = stat_object(bucket_name, object_key)
    except FileNotFoundError:
        return
    delete_object(
        bucket_name,
        object_key,
        version_id=current.version_id,
    )


def delete_all_object_versions(bucket_name: str, object_key: str) -> None:
    """Delete every data version and marker for one exact managed key."""
    bucket = _validate_bucket_name(bucket_name)
    key = _validate_object_key(object_key)
    try:
        listed = [
            _object_info(item, bucket, str(item.object_name))
            for item in get_client().list_objects(
                bucket,
                prefix=key,
                recursive=True,
                include_version=True,
            )
            if not bool(getattr(item, "is_dir", False))
            and str(getattr(item, "object_name", "")) == key
        ]
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            return
        if _versioning_not_supported(exc):
            _delete_current_object_if_present(bucket, key)
            return
        raise ObjectStorageError("MinIO 对象版本列表读取失败") from exc
    if not listed:
        _delete_current_object_if_present(bucket, key)
        return
    listed.sort(key=lambda item: bool(item.is_delete_marker))
    deleted_unversioned = False
    for item in listed:
        if not item.version_id:
            if deleted_unversioned:
                continue
            delete_object(bucket, key)
            deleted_unversioned = True
            continue
        delete_object(bucket, key, version_id=item.version_id)
