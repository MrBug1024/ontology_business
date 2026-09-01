"""数据源服务：关系型数据库连接 + 文件桶管理。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import delete, MetaData, Table, create_engine, func, inspect, or_, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AssistantAttachment,
    BucketFile,
    DataAssetVersion,
    DataSource,
    DatasetFragment,
    DatasetVersion,
    DerivationEvidence,
    DerivationRun,
    DocumentChunk,
    IngestionRun,
)
from . import cache_service, dataset_query_service, object_storage_service
from .policies import validate_read_only_sql

_engine_cache: dict[str, Engine] = {}

# Database driver exceptions commonly include a full DSN (and can therefore
# disclose a password, token, hostname or database name).  Connection-test
# callers intentionally receive this stable, non-diagnostic public message.
CONNECTION_TEST_FAILURE_MESSAGE = "连接测试失败，请检查数据源配置和网络可达性"

POSTGRES_HOST_MAX_LENGTH = 253
POSTGRES_DATABASE_MAX_LENGTH = 128
POSTGRES_USER_MAX_LENGTH = 128
POSTGRES_PASSWORD_MAX_LENGTH = 4096


def _required_postgres_text(
    config: Mapping[str, Any],
    key: str,
    *,
    max_length: int,
) -> str:
    raw = config.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"PostgreSQL 数据源配置缺少有效的 {key}")
    value = raw.strip()
    if not value or len(value) > max_length:
        raise ValueError(f"PostgreSQL 数据源配置的 {key} 长度无效")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"PostgreSQL 数据源配置的 {key} 包含无效字符")
    return value


def normalize_postgres_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and canonicalize the public PostgreSQL connection contract.

    Connection identity is always explicit. This prevents a partial tenant
    configuration from silently targeting a local/default PostgreSQL server.
    Validation messages name fields only and never interpolate credentials.
    """
    if config is None or not isinstance(config, Mapping):
        raise ValueError("PostgreSQL 数据源配置必须是对象")
    normalized = dict(config)
    host = _required_postgres_text(
        config,
        "host",
        max_length=POSTGRES_HOST_MAX_LENGTH,
    )
    database = _required_postgres_text(
        config,
        "database",
        max_length=POSTGRES_DATABASE_MAX_LENGTH,
    )

    raw_user = config.get("user")
    raw_username = config.get("username")
    if raw_user is None and raw_username is None:
        raise ValueError("PostgreSQL 数据源配置缺少有效的 user")
    user_config = {"user": raw_user if raw_user is not None else raw_username}
    user = _required_postgres_text(
        user_config,
        "user",
        max_length=POSTGRES_USER_MAX_LENGTH,
    )
    if raw_user is not None and raw_username is not None:
        username = _required_postgres_text(
            {"username": raw_username},
            "username",
            max_length=POSTGRES_USER_MAX_LENGTH,
        )
        if user != username:
            raise ValueError("PostgreSQL 数据源配置的 user 与 username 不一致")

    raw_port = config.get("port")
    if isinstance(raw_port, bool):
        raise ValueError("PostgreSQL 数据源配置的 port 必须是 1 到 65535 的整数")
    if isinstance(raw_port, int):
        port = raw_port
    elif isinstance(raw_port, str) and re.fullmatch(r"[0-9]+", raw_port.strip()):
        port = int(raw_port.strip())
    else:
        raise ValueError("PostgreSQL 数据源配置的 port 必须是 1 到 65535 的整数")
    if not 1 <= port <= 65535:
        raise ValueError("PostgreSQL 数据源配置的 port 必须是 1 到 65535 的整数")

    if "password" in config:
        password = config.get("password")
        if (
            not isinstance(password, str)
            or len(password) > POSTGRES_PASSWORD_MAX_LENGTH
            or "\x00" in password
        ):
            raise ValueError("PostgreSQL 数据源配置的 password 无效")

    normalized.update(
        {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
        }
    )
    normalized.pop("username", None)
    return normalized


def _schema_cache_key(ds: DataSource) -> str:
    revision = int(getattr(ds, "connector_revision", 0) or 0)
    return f"data-source-schema:{ds.id}:revision:{revision}"


def _db_url(ds: DataSource) -> URL:
    if ds.type == "postgres":
        cfg = normalize_postgres_config(ds.config)
        return URL.create(
            "postgresql+psycopg",
            username=cfg["user"],
            password=cfg.get("password"),
            host=cfg["host"],
            port=cfg["port"],
            database=cfg["database"],
        )
    raise ValueError("数据源必须是 PostgreSQL")


def get_engine(ds: DataSource) -> Engine:
    url = _db_url(ds)
    config_digest = hashlib.sha256(
        json.dumps(ds.config or {}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    key = f"{ds.id}:{config_digest}"
    if key not in _engine_cache:
        _engine_cache[key] = create_engine(url, pool_pre_ping=True)
    return _engine_cache[key]


def invalidate_engine(ds: DataSource) -> None:
    """数据源配置变化后释放旧连接池，避免继续使用旧凭据或旧地址。"""
    prefix = f"{ds.id}:"
    for key, cached in list(_engine_cache.items()):
        if key.startswith(prefix):
            cached.dispose()
            _engine_cache.pop(key, None)
    cache_service.delete(_schema_cache_key(ds))


def test_connection(ds: DataSource) -> tuple[bool, str]:
    """测试数据库连接，返回 (ok, message)。"""
    try:
        if ds.type == "dataset":
            dataset_query_service.test_connection(ds)
            return True, "连接成功"
        engine = get_engine(ds)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "连接成功"
    except Exception:  # noqa: BLE001 - never expose driver diagnostics or DSNs.
        return False, CONNECTION_TEST_FAILURE_MESSAGE


def list_tables(ds: DataSource) -> list[dict[str, Any]]:
    """列出当前数据库的表/视图；Redis 仅保存可重建的短期结果。"""
    cache_key = _schema_cache_key(ds)
    cached = cache_service.get_json(cache_key)
    if isinstance(cached, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("columns"), list)
        for item in cached
    ):
        return cached

    if ds.type == "dataset":
        tables = dataset_query_service.list_tables(ds)
        cache_service.set_json(cache_key, tables, ttl_seconds=120)
        return tables

    engine = get_engine(ds)
    tables: list[dict[str, Any]] = []
    with engine.connect() as conn:
        inspector = inspect(conn)
        names = sorted(
            set(inspector.get_table_names()) | set(inspector.get_view_names())
        )
        for name in names:
            try:
                primary_key = set(
                    inspector.get_pk_constraint(name).get("constrained_columns") or []
                )
                cols = [
                    {
                        "name": str(column["name"]),
                        "type": str(column["type"]),
                        "pk": str(column["name"]) in primary_key,
                    }
                    for column in inspector.get_columns(name)
                ]
            except Exception:  # noqa: BLE001
                cols = []
            row_count = -1
            try:
                # The inspector already supplied this identifier. Avoid reflecting
                # it a second time: the inspector already established that it
                # is a directly queryable relation.
                reflected = Table(name, MetaData(), quote=True)
                row_count = int(
                    conn.execute(select(func.count()).select_from(reflected)).scalar_one()
                )
            except Exception:  # noqa: BLE001
                pass
            tables.append({"name": name, "columns": cols, "row_count": row_count})
    cache_service.set_json(cache_key, tables, ttl_seconds=120)
    return tables


def run_query(
    ds: DataSource,
    sql: str,
    limit: int | None = None,
    *,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """执行单条只读 SQL 查询，返回列名与行数据。

    ``max_rows`` is an internal caller ceiling.  Mapping refresh jobs have a
    separately bounded batch size (currently 500), while interactive Agent
    queries must continue to use the lower application-wide query limit.
    """
    if ds.type == "file_bucket":
        raise ValueError("文件桶数据源不支持 SQL 查询")
    sql = validate_read_only_sql(sql, dialect=ds.type)
    configured_max_rows = get_settings().max_query_rows
    caller_max_rows = configured_max_rows if max_rows is None else max(1, int(max_rows))
    limit = max(1, min(int(limit or caller_max_rows), caller_max_rows))
    if ds.type == "dataset":
        return dataset_query_service.run_query(ds, sql, limit=limit)
    engine = get_engine(ds)
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = result.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        data = []
        for row in rows:
            data.append([_jsonable(v) for v in row])
        return {"columns": columns, "rows": data, "row_count": len(data), "truncated": truncated}


def run_parameterized_query(
    ds: DataSource,
    sql: str,
    parameters: Mapping[str, Any],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Execute one server-generated read-only statement with bound values.

    This is intentionally separate from ``run_query``: callers on the semantic
    path must never interpolate model/user filter values into SQL text.  The
    statement still passes the shared read-only policy so a future compiler
    regression fails closed before reaching a connector.
    """
    if ds.type == "file_bucket":
        raise ValueError("文件桶数据源不支持 SQL 查询")
    statement = validate_read_only_sql(sql, dialect=ds.type)
    if not isinstance(parameters, Mapping) or any(
        not isinstance(key, str) or not key for key in parameters
    ):
        raise ValueError("参数化查询的绑定参数无效")
    max_rows = get_settings().max_query_rows
    resolved_limit = max(1, min(int(limit or max_rows), max_rows))
    if ds.type == "dataset":
        return dataset_query_service.run_query(
            ds,
            statement,
            parameters=parameters,
            limit=resolved_limit,
        )
    engine = get_engine(ds)
    with engine.connect() as conn:
        result = conn.execute(text(statement), dict(parameters))
        columns = list(result.keys())
        rows = result.fetchmany(resolved_limit + 1)
        truncated = len(rows) > resolved_limit
        data = [
            [_jsonable(value) for value in row]
            for row in rows[:resolved_limit]
        ]
        return {
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
        }


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ──────────────────────────────────────────────
# 文件桶
# ──────────────────────────────────────────────
def normalize_file_bucket_config(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return the only file-bucket configuration accepted by the API.

    MinIO credentials stay in process settings.  A data-source row records only
    the durable storage policy needed to locate and validate its objects.
    ``config`` is intentionally not merged because it is user-controlled and
    must not select another bucket, prefix, endpoint, or credential set.
    """
    del config
    configured = object_storage_service.require_configuration()
    return {
        "storage_backend": "minio",
        "bucket_name": configured.bucket_name,
        "prefix": configured.prefix,
    }


def _is_minio_source(ds: DataSource) -> bool:
    return str((ds.config or {}).get("storage_backend") or "").strip().lower() == "minio"


def is_managed_minio_source(ds: DataSource) -> bool:
    """Return whether a file-bucket source uses the managed MinIO backend."""
    return ds.type == "file_bucket" and _is_minio_source(ds)


def managed_minio_location(ds: DataSource) -> tuple[str, str]:
    if ds.type != "file_bucket" or not _is_minio_source(ds):
        raise ValueError("数据源未配置为 MinIO 文件桶")
    configured = object_storage_service.require_configuration()
    source_config = ds.config or {}
    bucket_name = str(source_config.get("bucket_name") or configured.bucket_name).strip()
    prefix = object_storage_service.normalize_prefix(
        str(source_config.get("prefix") or configured.prefix)
    )
    if bucket_name != configured.bucket_name or prefix != configured.prefix:
        raise ValueError("文件桶配置与服务端托管存储不一致")
    return bucket_name, prefix


def ensure_file_bucket_storage(ds: DataSource) -> None:
    """Verify that a managed file bucket exists and is writable by this service."""
    bucket_name, _prefix = managed_minio_location(ds)
    object_storage_service.ensure_bucket(bucket_name)


def _object_scope_segment(value: Any, fallback: str) -> str:
    segment = str(value or fallback).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", segment):
        raise ValueError("文件桶对象作用域标识无效")
    return segment


def _upload_generation_segment(value: str) -> str:
    segment = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", segment):
        raise ValueError("对象上传批次标识必须是 32 位十六进制字符串")
    return segment


def build_bucket_object_key(
    ds: DataSource,
    file_id: str,
    filename: str,
    *,
    upload_id: str | None = None,
) -> str:
    """Build a legacy key or a generation-scoped key for a bucket-file row."""
    _bucket_name, prefix = managed_minio_location(ds)
    safe_name = validate_bucket_filename(filename)
    parts = [
        *([prefix] if prefix else []),
        "tenants",
        _object_scope_segment(ds.tenant_id, "public"),
        "scenarios",
        _object_scope_segment(ds.scenario_id, "global"),
        "data-sources",
        _object_scope_segment(ds.id, "source"),
        "files",
        _object_scope_segment(file_id, "file"),
    ]
    if upload_id is not None:
        parts.extend(("uploads", _upload_generation_segment(upload_id)))
    parts.append(safe_name)
    return "/".join(parts)


def build_assistant_attachment_object_key(
    tenant_id: str,
    attachment_id: str,
    filename: str,
    *,
    upload_id: str | None = None,
) -> str:
    """Build a legacy or generation-scoped assistant attachment key."""
    configured = object_storage_service.require_configuration()
    safe_name = validate_bucket_filename(filename)
    parts = [
        *([configured.prefix] if configured.prefix else []),
        "tenants",
        _object_scope_segment(tenant_id, "public"),
        "scenarios",
        "global",
        "assistant-attachments",
        _object_scope_segment(attachment_id, "attachment"),
    ]
    if upload_id is not None:
        parts.extend(("uploads", _upload_generation_segment(upload_id)))
    parts.append(safe_name)
    return "/".join(parts)


def is_generation_scoped_object_key(object_key: str) -> bool:
    """Return whether a managed key carries a one-use upload generation."""
    parts = str(object_key or "").strip("/").split("/")
    return (
        len(parts) >= 3
        and parts[-3] == "uploads"
        and re.fullmatch(r"[a-f0-9]{32}", parts[-2]) is not None
    )


def _matches_generation_scoped_key(recorded_key: str, legacy_key: str) -> bool:
    base, safe_name = legacy_key.rsplit("/", 1)
    prefix = f"{base}/uploads/"
    suffix = f"/{safe_name}"
    if not recorded_key.startswith(prefix) or not recorded_key.endswith(suffix):
        return False
    generation = recorded_key[len(prefix) : -len(suffix)]
    return re.fullmatch(r"[a-f0-9]{32}", generation) is not None


def _validate_bucket_object_key(
    object_key: str,
    ds: DataSource,
    file_id: str,
    filename: str,
    *,
    require_generation: bool,
) -> str:
    recorded = str(object_key or "").strip("/")
    legacy = build_bucket_object_key(ds, file_id, filename)
    if _matches_generation_scoped_key(recorded, legacy):
        return recorded
    if not require_generation and recorded == legacy:
        return recorded
    raise ValueError("MinIO 附件对象不属于指定文件桶作用域")


def _validate_assistant_attachment_object_key(
    object_key: str,
    tenant_id: str,
    attachment_id: str,
    filename: str,
    *,
    require_generation: bool,
) -> str:
    recorded = str(object_key or "").strip("/")
    legacy = build_assistant_attachment_object_key(
        tenant_id,
        attachment_id,
        filename,
    )
    if _matches_generation_scoped_key(recorded, legacy):
        return recorded
    if not require_generation and recorded == legacy:
        return recorded
    raise ValueError("助手附件对象不属于托管存储作用域")


def save_assistant_attachment_object(
    attachment: AssistantAttachment,
    content: bytes,
    *,
    upload_object_key: str | None = None,
) -> None:
    """Persist raw temporary attachment bytes and populate its durable identity."""
    if not isinstance(content, bytes):
        raise ValueError("附件内容必须是字节数据")
    configured = object_storage_service.require_configuration()
    object_key = (
        _validate_assistant_attachment_object_key(
            upload_object_key,
            attachment.tenant_id,
            attachment.id,
            attachment.filename,
            require_generation=True,
        )
        if upload_object_key is not None
        else build_assistant_attachment_object_key(
            attachment.tenant_id,
            attachment.id,
            attachment.filename,
            upload_id=uuid.uuid4().hex,
        )
    )
    digest = hashlib.sha256(content).hexdigest()
    uploaded = object_storage_service.put_object(
        configured.bucket_name,
        object_key,
        content,
        content_type=attachment.mime or _guess_mime(attachment.filename),
        sha256=digest,
    )
    object_url = object_storage_service.stable_object_url(
        configured.bucket_name,
        object_key,
    )
    attachment.content_hash = digest
    attachment.size = len(content)
    attachment.mime = attachment.mime or _guess_mime(attachment.filename)
    attachment.storage_provider = "minio"
    attachment.bucket_name = configured.bucket_name
    attachment.object_key = object_key
    attachment.object_version_id = uploaded.version_id
    attachment.etag = uploaded.etag
    attachment.object_url = object_url
    attachment._managed_object_created = True


def save_assistant_attachment_object_path(
    attachment: AssistantAttachment,
    source_path: str | Path,
    *,
    upload_object_key: str | None = None,
    content_sha256: str = "",
) -> None:
    """Stream a staged assistant attachment to MinIO without materialising bytes."""
    configured = object_storage_service.require_configuration()
    path = Path(source_path).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ValueError("附件暂存文件必须是普通文件")
    digest = str(content_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("附件缺少有效内容哈希")
    object_key = (
        _validate_assistant_attachment_object_key(
            upload_object_key,
            attachment.tenant_id,
            attachment.id,
            attachment.filename,
            require_generation=True,
        )
        if upload_object_key is not None
        else build_assistant_attachment_object_key(
            attachment.tenant_id,
            attachment.id,
            attachment.filename,
            upload_id=uuid.uuid4().hex,
        )
    )
    uploaded = object_storage_service.put_file(
        configured.bucket_name,
        object_key,
        path,
        content_type=attachment.mime or _guess_mime(attachment.filename),
        sha256=digest,
    )
    attachment.content_hash = digest
    attachment.size = int(path.stat().st_size)
    attachment.mime = attachment.mime or _guess_mime(attachment.filename)
    attachment.storage_provider = "minio"
    attachment.bucket_name = configured.bucket_name
    attachment.object_key = object_key
    attachment.object_version_id = uploaded.version_id
    attachment.etag = uploaded.etag
    attachment.object_url = object_storage_service.stable_object_url(
        configured.bucket_name, object_key
    )
    attachment._managed_object_created = True


def assistant_attachment_object_identity(
    attachment: AssistantAttachment,
) -> tuple[str, str, str] | None:
    """Return a validated managed identity; legacy text-only rows have none."""
    provider = str(getattr(attachment, "storage_provider", "none") or "none").lower()
    object_url = str(getattr(attachment, "object_url", "") or "")
    recorded_bucket = str(getattr(attachment, "bucket_name", "") or "")
    recorded_key = str(getattr(attachment, "object_key", "") or "")
    # The column default changed from ``none`` to ``minio`` when raw uploads
    # became mandatory. Rows created before that migration can therefore read
    # as ``minio`` while still containing only parsed text. No locator means
    # there is no external object to delete; a partially populated locator
    # remains invalid and continues to fail closed below.
    if not object_url and not recorded_bucket and not recorded_key:
        return None
    if provider != "minio" and not object_url.lower().startswith("minio://"):
        raise ValueError("助手附件存储提供方无效")
    configured = object_storage_service.require_configuration()
    url_bucket = ""
    url_key = ""
    if object_url:
        url_bucket, url_key = object_storage_service.parse_object_url(object_url)
    bucket_name = recorded_bucket or url_bucket
    object_key = recorded_key or url_key
    if (url_bucket and url_bucket != bucket_name) or (url_key and url_key != object_key):
        raise ValueError("助手附件对象字段与地址不一致")
    try:
        try:
            _validate_assistant_attachment_object_key(
                object_key,
                attachment.tenant_id,
                attachment.id,
                attachment.filename,
                require_generation=False,
            )
        except ValueError:
            # Old/generated attachment rows may predate the tenant-scoped key
            # layout.  They remain deletable only by their exact persisted
            # identity inside the server-managed MinIO prefix.
            _validate_persisted_minio_object_key(
                object_key,
                configured.bucket_name,
                configured.prefix,
            )
    except ValueError as exc:
        raise ValueError("助手附件对象不属于平台托管存储作用域") from exc
    if bucket_name != configured.bucket_name:
        raise ValueError("助手附件对象不属于托管存储作用域")
    return (
        bucket_name,
        object_key,
        str(getattr(attachment, "object_version_id", "") or ""),
    )


def delete_assistant_attachment_object(attachment: AssistantAttachment) -> None:
    """Direct compensation for an attachment whose DB transaction did not commit.

    Persisted attachment deletion must go through ``object_deletion_service``.
    """
    identity = assistant_attachment_object_identity(attachment)
    if identity is None:
        return
    bucket_name, object_key, version_id = identity
    object_storage_service.delete_object(
        bucket_name,
        object_key,
        version_id=version_id,
    )


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_OFFICE_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
}


def validate_bucket_filename(filename: str) -> str:
    """Validate a user-visible filename instead of silently stripping a path.

    Silent ``Path(...).name`` normalisation hides traversal attempts and makes
    audit logs disagree with the user's request.  File-bucket APIs therefore
    accept a basename only and fail closed on platform-reserved names.
    """
    name = str(filename or "").strip()
    if not name or len(name) > 240:
        raise ValueError("文件名必须是 1 到 240 个字符")
    if name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("文件名不能包含目录或路径穿越字符")
    if any(ord(char) < 32 for char in name) or any(char in '<>:"|?*' for char in name):
        raise ValueError("文件名包含不安全字符")
    if name.endswith((" ", ".")):
        raise ValueError("文件名不能以空格或句点结尾")
    if Path(name).stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("文件名使用了系统保留名称")
    return name


def save_bucket_file(
    ds: DataSource,
    filename: str,
    content: bytes,
    *,
    mime: str | None = None,
    stable_file_id: str | None = None,
    upload_object_key: str | None = None,
) -> BucketFile:
    """Save a file as a managed MinIO object."""
    if ds.type != "file_bucket":
        raise ValueError("只有文件桶数据源可以保存文件")
    if not isinstance(content, bytes):
        raise ValueError("文件内容必须是字节数据")
    requested_name = validate_bucket_filename(filename)
    if stable_file_id is not None:
        stable_file_id = str(stable_file_id).lower()
        if not re.fullmatch(r"[a-f0-9]{32}", stable_file_id):
            raise ValueError("稳定文件标识必须是 32 位十六进制字符串")

    if is_managed_minio_source(ds):
        file_id = stable_file_id or uuid.uuid4().hex
        bucket_name, _prefix = managed_minio_location(ds)
        object_key = (
            _validate_bucket_object_key(
                upload_object_key,
                ds,
                file_id,
                requested_name,
                require_generation=True,
            )
            if upload_object_key is not None
            else build_bucket_object_key(
                ds,
                file_id,
                requested_name,
                upload_id=uuid.uuid4().hex,
            )
        )
        resolved_mime = mime or _guess_mime(requested_name)
        content_sha256 = hashlib.sha256(content).hexdigest()
        uploaded = object_storage_service.put_object(
            bucket_name,
            object_key,
            content,
            content_type=resolved_mime,
            sha256=content_sha256,
        )
        object_url = object_storage_service.stable_object_url(bucket_name, object_key)
        bucket_file = BucketFile(
            id=file_id,
            data_source_id=ds.id,
            filename=requested_name,
            stored_path=object_url,
            storage_provider="minio",
            bucket_name=bucket_name,
            object_key=object_key,
            object_version_id=uploaded.version_id,
            etag=uploaded.etag,
            object_url=object_url,
            size=len(content),
            mime=resolved_mime,
            content_sha256=content_sha256,
            status="pending",
        )
        bucket_file._managed_object_created = True
        return bucket_file

    raise ValueError("文件桶必须使用 MinIO 存储")


def save_bucket_file_path(
    ds: DataSource,
    filename: str,
    source_path: str | Path,
    *,
    mime: str | None = None,
    stable_file_id: str | None = None,
    upload_object_key: str | None = None,
    content_sha256: str = "",
) -> BucketFile:
    """Stream a local staging file to managed MinIO without loading it in RAM."""
    if ds.type != "file_bucket" or not is_managed_minio_source(ds):
        raise ValueError("文件桶必须使用 MinIO 存储")
    requested_name = validate_bucket_filename(filename)
    file_id = str(stable_file_id or uuid.uuid4().hex).lower()
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise ValueError("稳定文件标识必须是 32 位十六进制字符串")
    unresolved_path = Path(source_path).expanduser()
    if unresolved_path.is_symlink():
        raise ValueError("上传暂存文件必须是普通文件")
    path = unresolved_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("上传暂存文件必须是普通文件")
    size = int(path.stat().st_size)
    if size <= 0:
        raise ValueError("上传文件不能为空")
    digest = str(content_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    bucket_name, _prefix = managed_minio_location(ds)
    object_key = (
        _validate_bucket_object_key(
            upload_object_key,
            ds,
            file_id,
            requested_name,
            require_generation=True,
        )
        if upload_object_key is not None
        else build_bucket_object_key(
            ds,
            file_id,
            requested_name,
            upload_id=uuid.uuid4().hex,
        )
    )
    resolved_mime = mime or _guess_mime(requested_name)
    uploaded = object_storage_service.put_file(
        bucket_name,
        object_key,
        path,
        content_type=resolved_mime,
        sha256=digest,
    )
    object_url = object_storage_service.stable_object_url(bucket_name, object_key)
    bucket_file = BucketFile(
        id=file_id,
        data_source_id=ds.id,
        filename=requested_name,
        stored_path=object_url,
        storage_provider="minio",
        bucket_name=bucket_name,
        object_key=object_key,
        object_version_id=uploaded.version_id,
        etag=uploaded.etag,
        object_url=object_url,
        size=size,
        mime=resolved_mime,
        content_sha256=digest,
        status="pending",
    )
    bucket_file._managed_object_created = True
    return bucket_file


def _validated_bucket_mime(filename: str, recorded_mime: str) -> str:
    canonical_mime = _OFFICE_MIMES.get(Path(filename).suffix.lower())
    recorded_base = str(recorded_mime or "").split(";", 1)[0].strip().lower()
    canonical_base = str(canonical_mime or "").split(";", 1)[0].strip().lower()
    # Older records may omit a MIME or use octet-stream for typed files. Treat
    # those values as compatible while rejecting a contradictory MIME.
    compatible_legacy = recorded_base in {"", "application/octet-stream"}
    if canonical_mime and not compatible_legacy and recorded_base != canonical_base:
        raise ValueError("附件 MIME 与文件格式不一致")
    return canonical_mime or recorded_mime or _guess_mime(filename)


def minio_file_identity(bf: BucketFile, ds: DataSource) -> tuple[str, str, str]:
    if bf.data_source_id != ds.id or ds.type != "file_bucket":
        raise ValueError("附件不属于指定文件桶")
    safe_name = validate_bucket_filename(bf.filename)
    bucket_name, _prefix = managed_minio_location(ds)
    object_url = str(getattr(bf, "object_url", "") or bf.stored_path or "")
    url_bucket = ""
    url_key = ""
    if object_url:
        url_bucket, url_key = object_storage_service.parse_object_url(object_url)
    recorded_bucket = str(getattr(bf, "bucket_name", "") or url_bucket).strip()
    recorded_key = str(getattr(bf, "object_key", "") or url_key).strip("/")
    if not recorded_bucket or not recorded_key:
        raise ValueError("MinIO 附件缺少对象身份记录")
    if (url_bucket and url_bucket != recorded_bucket) or (url_key and url_key != recorded_key):
        raise ValueError("MinIO 附件对象字段与地址不一致")
    try:
        _validate_bucket_object_key(
            recorded_key,
            ds,
            bf.id,
            safe_name,
            require_generation=False,
        )
    except ValueError as exc:
        raise ValueError("MinIO 附件对象不属于指定文件桶作用域") from exc
    if recorded_bucket != bucket_name:
        raise ValueError("MinIO 附件对象不属于指定文件桶作用域")
    return recorded_bucket, recorded_key, safe_name


def _validate_persisted_minio_object_key(
    object_key: str,
    bucket_name: str,
    prefix: str,
) -> str:
    """Validate an object identity already persisted for a platform file.

    New uploads use the tenant/scenario/data-source/file layout validated by
    ``_validate_bucket_object_key``.  Older migration/import rows can have a
    different layout, but they are still deletable when the exact identity is
    already recorded in our database and remains below the one server-managed
    MinIO prefix.  The key is never accepted from the delete request itself.
    """
    recorded = str(object_key or "").strip("/")
    if not recorded:
        raise ValueError("MinIO 附件缺少对象身份记录")
    try:
        # stable_object_url delegates to the storage layer's canonical object
        # key validation (no empty/dot/traversal/control-character segments).
        _bucket, normalized = object_storage_service.parse_object_url(
            object_storage_service.stable_object_url(bucket_name, recorded)
        )
    except object_storage_service.ObjectStorageError as exc:
        raise ValueError("MinIO 附件对象身份记录无效") from exc
    if prefix and not normalized.startswith(f"{prefix}/"):
        raise ValueError("MinIO 附件对象不属于平台托管文件桶")
    return normalized


def _minio_file_deletion_identity(
    bf: BucketFile,
    ds: DataSource,
) -> tuple[str, str, str]:
    """Resolve a persisted MinIO file identity for a destructive operation.

    Deletion intentionally accepts both the current scoped key format and
    historical platform-managed keys.  Ownership is established by the
    tenant-owned ``DataSource``/``BucketFile`` relationship plus the exact
    bucket/key recorded in the control-plane database; the configured bucket
    and prefix remain an independent server-side boundary.
    """
    if bf.data_source_id != ds.id or ds.type != "file_bucket":
        raise ValueError("附件不属于指定文件桶")
    safe_name = validate_bucket_filename(bf.filename)
    bucket_name, prefix = managed_minio_location(ds)
    object_url = str(getattr(bf, "object_url", "") or bf.stored_path or "")
    url_bucket = ""
    url_key = ""
    if object_url:
        try:
            url_bucket, url_key = object_storage_service.parse_object_url(object_url)
        except object_storage_service.ObjectStorageError as exc:
            raise ValueError("MinIO 附件对象地址无效") from exc
    recorded_bucket = str(getattr(bf, "bucket_name", "") or url_bucket).strip()
    recorded_key = str(getattr(bf, "object_key", "") or url_key).strip("/")
    if not recorded_bucket or not recorded_key:
        raise ValueError("MinIO 附件缺少对象身份记录")
    if (url_bucket and url_bucket != recorded_bucket) or (
        url_key and url_key != recorded_key
    ):
        raise ValueError("MinIO 附件对象字段与地址不一致")
    if recorded_bucket != bucket_name:
        raise ValueError("MinIO 附件对象不属于平台托管文件桶")
    try:
        # Prefer the current strict identity check.  If this is an older
        # migration/generated key, fall back only to the exact persisted key
        # under the server-managed prefix.
        try:
            validated_key = _validate_bucket_object_key(
                recorded_key,
                ds,
                bf.id,
                safe_name,
                require_generation=False,
            )
        except ValueError:
            validated_key = _validate_persisted_minio_object_key(
                recorded_key,
                bucket_name,
                prefix,
            )
    except ValueError as exc:
        raise ValueError("MinIO 附件对象不属于平台托管文件桶") from exc
    return recorded_bucket, validated_key, safe_name


def detach_platform_catalog_references_for_deletion(
    db: Session,
    data_source: DataSource,
    bucket_file_ids: list[str] | tuple[str, ...],
) -> dict[str, int]:
    """Detach local catalog pointers before deleting owned source files.

    A BucketFile is the physical payload for several immutable catalog rows.
    Removing the platform-owned payload must not be blocked by those rows, but
    those rows must also stop advertising a now-deleted MinIO object.  The
    logical catalog identities are retained as retired audit metadata; only
    their physical-file pointers and directly materialized fragments are
    detached.  Remote database contents are not involved in this operation.
    """
    normalized_ids = sorted({str(value) for value in bucket_file_ids if str(value)})
    if not normalized_ids:
        return {
            "asset_versions_detached": 0,
            "dataset_fragments_deleted": 0,
            "manifest_versions_detached": 0,
        }
    if data_source.type != "file_bucket":
        return {
            "asset_versions_detached": 0,
            "dataset_fragments_deleted": 0,
            "manifest_versions_detached": 0,
        }

    if db.get_bind().dialect.name == "postgresql":
        try:
            result = db.execute(
                text(
                    "SELECT asset_versions_detached, "
                    "dataset_fragments_deleted, manifest_versions_detached "
                    "FROM public.detach_data_source_file_references("
                    ":source_id, :tenant_id, CAST(:file_ids AS varchar[]))"
                ),
                {
                    "source_id": data_source.id,
                    "tenant_id": data_source.tenant_id,
                    "file_ids": normalized_ids,
                },
            ).one()
        except ProgrammingError as exc:
            raise ValueError(
                "数据库尚未完成本地 MinIO 文件删除迁移，请先升级数据库并重启后端"
            ) from exc
        return {
            "asset_versions_detached": int(result.asset_versions_detached or 0),
            "dataset_fragments_deleted": int(result.dataset_fragments_deleted or 0),
            "manifest_versions_detached": int(result.manifest_versions_detached or 0),
        }

    # SQLite is used by local tests and development.  Keep its lifecycle
    # behavior equivalent to the governed PostgreSQL function above.
    ingestion_runs = list(
        db.scalars(
            select(IngestionRun)
            .where(
                IngestionRun.trace_bucket_file_id.in_(normalized_ids),
                IngestionRun.trace_data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    derivation_runs = list(
        db.scalars(
            select(DerivationRun)
            .where(
                DerivationRun.trace_bucket_file_id.in_(normalized_ids),
                DerivationRun.trace_data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    for run in [*ingestion_runs, *derivation_runs]:
        run.trace_bucket_file_id = None
        run.trace_data_source_id = None

    asset_versions = list(
        db.scalars(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.bucket_file_id.in_(normalized_ids),
                DataAssetVersion.bucket_data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    for version in asset_versions:
        version.status = "retired"
        version.bucket_file_id = None
        version.bucket_data_source_id = None
        version.source_locator = {}

    fragments = list(
        db.scalars(
            select(DatasetFragment)
            .where(
                DatasetFragment.bucket_file_id.in_(normalized_ids),
                DatasetFragment.bucket_data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    document_chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.bucket_file_id.in_(normalized_ids),
                DocumentChunk.data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    evidence_conditions = []
    if fragments:
        evidence_conditions.append(
            DerivationEvidence.dataset_fragment_id.in_(
                [fragment.id for fragment in fragments]
            )
        )
    if document_chunks:
        evidence_conditions.append(
            DerivationEvidence.document_chunk_id.in_(
                [chunk.id for chunk in document_chunks]
            )
        )
    if evidence_conditions:
        dependent_evidence = list(
            db.scalars(
                select(DerivationEvidence)
                .where(or_(*evidence_conditions))
                .with_for_update()
            ).all()
        )
        for evidence in dependent_evidence:
            db.delete(evidence)

    affected_dataset_version_ids = {
        str(fragment.dataset_version_id) for fragment in fragments
    }
    manifest_versions = list(
        db.scalars(
            select(DatasetVersion)
            .where(
                DatasetVersion.manifest_bucket_file_id.in_(normalized_ids),
                DatasetVersion.manifest_data_source_id == data_source.id,
            )
            .with_for_update()
        ).all()
    )
    affected_dataset_version_ids.update(
        str(version.id) for version in manifest_versions
    )
    for fragment in fragments:
        db.delete(fragment)
    affected_versions = (
        list(
            db.scalars(
                select(DatasetVersion)
                .where(DatasetVersion.id.in_(sorted(affected_dataset_version_ids)))
                .with_for_update()
            ).all()
        )
        if affected_dataset_version_ids
        else []
    )
    for version in affected_versions:
        version.status = "retired"
        if version.manifest_bucket_file_id in normalized_ids:
            version.manifest_bucket_file_id = None
            version.manifest_data_source_id = None

    return {
        "asset_versions_detached": len(asset_versions),
        "dataset_fragments_deleted": len(fragments),
        "manifest_versions_detached": len(manifest_versions),
    }


def read_bucket_file(bf: BucketFile, ds: DataSource) -> tuple[bytes, int, str]:
    """Read and integrity-check a managed MinIO object."""
    bucket_name, object_key, safe_name = minio_file_identity(bf, ds)
    version_id = str(getattr(bf, "object_version_id", "") or "")
    current = object_storage_service.stat_object(
        bucket_name,
        object_key,
        version_id=version_id,
    )
    recorded_etag = str(getattr(bf, "etag", "") or "").strip('"')
    if recorded_etag and current.etag and current.etag != recorded_etag:
        raise ValueError("附件 ETag 与文件记录不一致")
    if bf.size > 0 and current.size != bf.size:
        raise ValueError("附件大小与文件记录不一致")
    content = object_storage_service.get_object(
        bucket_name,
        object_key,
        version_id=version_id,
    )
    if current.size != len(content) or (bf.size > 0 and len(content) != bf.size):
        raise ValueError("附件大小与对象内容不一致")
    if bf.content_sha256 and hashlib.sha256(content).hexdigest() != bf.content_sha256:
        raise ValueError("附件内容哈希与文件记录不一致")
    return content, len(content), _validated_bucket_mime(safe_name, bf.mime)


def bucket_file_deletion_identity(
    bf: BucketFile,
    ds: DataSource,
) -> tuple[str, str, str, str]:
    """Validate and return the MinIO bucket, key and version for deletion."""
    bucket_name, object_key, _safe_name = _minio_file_deletion_identity(bf, ds)
    return (
        "minio",
        bucket_name,
        object_key,
        str(getattr(bf, "object_version_id", "") or ""),
    )


def is_managed_minio_file(bucket_file: BucketFile) -> bool:
    provider = str(
        getattr(bucket_file, "storage_provider", "") or ""
    ).strip().lower()
    durable_url = str(
        getattr(bucket_file, "object_url", "") or bucket_file.stored_path or ""
    )
    return provider == "minio" or durable_url.lower().startswith("minio://")


def managed_object_was_created(
    record: BucketFile | AssistantAttachment,
) -> bool:
    return bool(getattr(record, "_managed_object_created", False))


def delete_bucket_file(bf: BucketFile, ds: DataSource) -> None:
    """Direct compensation for an object whose DB transaction did not commit.

    Persisted file deletion must go through ``object_deletion_service``.
    """
    provider, bucket_or_path, object_key, version_id = (
        bucket_file_deletion_identity(bf, ds)
    )
    if provider != "minio":
        raise ValueError("文件删除任务必须使用 MinIO")
    object_storage_service.delete_object(
        bucket_or_path,
        object_key,
        version_id=version_id,
    )


def _guess_mime(name: str) -> str:
    import mimetypes

    return _OFFICE_MIMES.get(Path(name).suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"
