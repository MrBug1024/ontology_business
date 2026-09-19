"""One metadata entry point for explicitly selected research libraries."""
from __future__ import annotations

from threading import BoundedSemaphore

from pydantic import ValidationError

from ..library_schemas import RemoteLibraryConfig
from ..models import BucketFile, DataSource
from . import distillation_connector_service, library_credential_service


DATABASE_TYPES = frozenset({"postgres", "mysql", "sqlite3"})
MAX_CONCURRENT_READS = 4
_READ_SLOTS = BoundedSemaphore(MAX_CONCURRENT_READS)


def write_config(kind: str, config: dict, *, old_config: dict | None = None) -> dict:
    if kind in {"postgres", "mysql"}:
        return library_credential_service.seal_config(remote_config(config, old_config=old_config))
    if kind in {"file_bucket", "sqlite3"} and config:
        raise ValueError("文件资料库只接受受管上传，不接受路径、地址或对象配置")
    if kind in {"file_bucket", "sqlite3"}:
        from .datasource_service import normalize_file_bucket_config
        values = normalize_file_bucket_config()
        if kind == "sqlite3" and old_config:
            for key in ("snapshot_file_id", "snapshot_sha256"):
                if key in old_config:
                    values[key] = old_config[key]
        return values
    raise ValueError("不支持的资料库类型")


def remote_config(config: dict, *, old_config: dict | None = None) -> dict:
    values = dict(config)
    # Older API clients use username. Keep that single documented alias while
    # rejecting arbitrary connection options, URLs, socket paths and envelopes.
    if "username" in values:
        username = values.pop("username")
        if "user" in values and values["user"] != username:
            raise ValueError("用户名字段不一致")
        values["user"] = username
    values.pop("password_configured", None)
    if old_config and not values.get("password"):
        values["password"] = library_credential_service.open_config(old_config).get("password", "")
    try:
        return RemoteLibraryConfig.model_validate(values).model_dump()
    except ValidationError as exc:
        raise ValueError("请填写有效的主机、端口、数据库和用户名；连接配置含不支持字段") from exc


def snapshot(source: DataSource) -> DataSource:
    copy = DataSource(id=source.id, tenant_id=source.tenant_id, scenario_id=source.scenario_id,
        type=source.type, name=source.name, config=dict(source.config or {}),
        connector_revision=source.connector_revision, resource_scope=source.resource_scope)
    if source.type == "sqlite3":
        copy.files = [BucketFile(**{key: getattr(file, key) for key in (
            "id", "data_source_id", "filename", "size", "mime", "content_sha256", "stored_path",
            "storage_provider", "bucket_name", "object_key", "object_version_id", "object_url", "etag")})
            for file in source.files]
    return copy


def database_schema(source: DataSource, *, timeout_seconds: float = 20, sample=None) -> list[dict] | dict:
    if not _READ_SLOTS.acquire(blocking=False):
        raise ValueError("资料库结构读取繁忙，请稍后重试")
    try:
        return _database_schema(source, timeout_seconds=timeout_seconds, sample=sample)
    finally:
        _READ_SLOTS.release()


def _database_schema(source: DataSource, *, timeout_seconds: float, sample=None) -> list[dict] | dict:
    if source.type == "postgres":
        return distillation_connector_service.postgres_schema(source, timeout_seconds=timeout_seconds, **({"sample": sample} if sample is not None else {}))
    if source.type == "mysql":
        from .library_mysql_adapter import mysql_schema
        config = remote_config(library_credential_service.open_config(source.config or {}))
        return mysql_schema(config, timeout_seconds=timeout_seconds, **({"sample": sample} if sample is not None else {}))
    if source.type == "sqlite3":
        from .library_sqlite_adapter import sqlite_schema
        return sqlite_schema(source, timeout_seconds=timeout_seconds, **({"sample": sample} if sample is not None else {}))
    raise ValueError("该资料库不提供数据库结构")
