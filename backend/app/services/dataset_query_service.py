"""Read-only query gateway for immutable Parquet dataset versions.

PostgreSQL stores the catalog and MinIO stores the immutable fragments. DuckDB
is an execution detail: every connection is in-memory and can read only the
verified, content-addressed fragment files selected by the catalog.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Any
import uuid

from sqlalchemy import select

from ..config import get_settings
from ..database import SessionLocal
from . import object_storage_service
from .policies import PolicyViolation, validate_read_only_sql


class DatasetQueryError(RuntimeError):
    """A stable, non-sensitive dataset query failure."""


@dataclass(frozen=True)
class DatasetFragmentSpec:
    id: str
    bucket_name: str
    object_key: str
    version_id: str
    content_sha256: str
    byte_size: int
    ordinal: int


@dataclass(frozen=True)
class DatasetRelationSpec:
    id: str
    relation_key: str
    ordinal: int
    row_count: int
    fragments: tuple[DatasetFragmentSpec, ...]
    kind: str = "table"
    view_sql: str = ""
    expected_columns: tuple[str, ...] = ()
    declared_columns: tuple[tuple[str, str, bool, int | None], ...] = ()


@dataclass(frozen=True)
class DatasetCatalog:
    dataset_id: str
    dataset_version_id: str
    relations: tuple[DatasetRelationSpec, ...]


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NAMED_PARAMETER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DERIVED_EXTERNAL_FUNCTION_RE = re.compile(
    r"\b(?:"
    r"read_[A-Za-z0-9_]*|glob|query|query_table|"
    r"sqlite_scan|postgres_scan|mysql_scan|iceberg_scan|delta_scan|"
    r"arrow_scan|pandas_scan|parquet_scan|pragma_[A-Za-z0-9_]*|"
    r"duckdb_[A-Za-z0-9_]*|current_setting|current_query|"
    r"current_database|current_schema|current_schemas|current_user|session_user"
    r")\s*\(",
    flags=re.IGNORECASE,
)
_DERIVED_SYSTEM_VALUE_RE = re.compile(
    r"\b(?:current_user|session_user|current_database|current_schema)\b",
    flags=re.IGNORECASE,
)
# v2 adds bounded eviction and cross-process leases. Do not reuse the legacy,
# unbounded directory because its ownership and contents cannot be trusted.
_CACHE_ROOT = Path(tempfile.gettempdir()) / "ontology-platform-dataset-cache-v2"
_verified_cache_files: dict[str, tuple[int, int, str]] = {}
_verified_cache_files_guard = threading.Lock()
_cache_process_guard = threading.RLock()
_active_cache_leases: dict[str, int] = {}
_active_cache_leases_guard = threading.Lock()
_query_semaphores: dict[int, threading.BoundedSemaphore] = {}
_query_semaphores_guard = threading.Lock()
_CACHE_LOCK_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class _CachePolicy:
    max_object_bytes: int
    max_total_bytes: int
    max_age_seconds: int


class _InterProcessLock:
    """One-byte advisory lock implemented only with the Python standard library."""

    def __init__(
        self,
        path: Path,
        *,
        blocking: bool = True,
        timeout: float = _CACHE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.path = path
        self.blocking = blocking
        self.timeout = max(0.0, float(timeout))
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = descriptor
                    return True
                except OSError as exc:
                    busy = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                    if not busy:
                        raise
                    if not self.blocking:
                        os.close(descriptor)
                        return False
                    if time.monotonic() >= deadline:
                        raise DatasetQueryError("等待数据集缓存锁超时") from exc
                    time.sleep(0.05)
        except Exception:
            if self._fd is None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def release(self) -> None:
        descriptor = self._fd
        self._fd = None
        if descriptor is None:
            return
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "_InterProcessLock":
        if not self.acquire():
            raise DatasetQueryError("数据集缓存文件正在使用")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class _CachedFragmentLease:
    """Cross-process lease that keeps an opened cache entry out of eviction."""

    def __init__(
        self,
        path: Path,
        marker: Path,
        lock: _InterProcessLock,
        content_sha256: str,
    ) -> None:
        self.path = path
        self._marker = marker
        self._lock = lock
        self._content_sha256 = content_sha256
        self._released = False
        with _active_cache_leases_guard:
            _active_cache_leases[content_sha256] = (
                _active_cache_leases.get(content_sha256, 0) + 1
            )

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._lock.release()
            try:
                self._marker.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            with _active_cache_leases_guard:
                remaining = _active_cache_leases.get(self._content_sha256, 0) - 1
                if remaining > 0:
                    _active_cache_leases[self._content_sha256] = remaining
                else:
                    _active_cache_leases.pop(self._content_sha256, None)


def _cache_policy() -> _CachePolicy:
    settings = get_settings()
    policy = _CachePolicy(
        max_object_bytes=int(
            getattr(settings, "dataset_cache_max_object_bytes", 1024 * 1024 * 1024)
        ),
        max_total_bytes=int(
            getattr(settings, "dataset_cache_max_bytes", 10 * 1024 * 1024 * 1024)
        ),
        max_age_seconds=int(
            getattr(settings, "dataset_cache_max_age_seconds", 7 * 24 * 60 * 60)
        ),
    )
    if (
        policy.max_object_bytes <= 0
        or policy.max_total_bytes <= 0
        or policy.max_age_seconds <= 0
        or policy.max_object_bytes > policy.max_total_bytes
    ):
        raise DatasetQueryError("数据集缓存容量配置无效")
    return policy


def _ensure_private_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise DatasetQueryError(f"{label}不能是符号链接")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise DatasetQueryError(f"{label}不能是符号链接")
    try:
        details = path.stat()
        if not stat.S_ISDIR(details.st_mode):
            raise DatasetQueryError(f"{label}不是目录")
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise DatasetQueryError(f"{label}不属于当前服务用户")
        if os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise DatasetQueryError(f"无法建立私有{label}") from exc
    return path.resolve(strict=True)


def _cache_layout() -> tuple[Path, Path, Path, Path]:
    root = _ensure_private_directory(_CACHE_ROOT, "数据集缓存目录")
    locks = _ensure_private_directory(root / ".locks", "数据集缓存锁目录")
    leases = _ensure_private_directory(root / ".leases", "数据集缓存租约目录")
    access = _ensure_private_directory(root / ".access", "数据集缓存访问目录")
    return root, locks, leases, access


def _model_attr(model: Any, *names: str) -> Any:
    for name in names:
        value = getattr(model, name, None)
        if value is not None:
            return value
    raise DatasetQueryError("数据集目录模型版本不兼容")


def _value(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _source_config(source: Any) -> tuple[str, str | None]:
    config = getattr(source, "config", None) or {}
    if not isinstance(config, Mapping):
        raise DatasetQueryError("数据集连接器配置无效")
    for key in config:
        normalized = str(key).casefold()
        if any(token in normalized for token in ("password", "secret", "credential", "access_key")):
            raise DatasetQueryError("数据集连接器不得保存存储凭据")
    version_id = str(config.get("dataset_version_id") or "").strip()
    if (
        not version_id
        or len(version_id) > 64
        or any(ord(character) < 32 for character in version_id)
    ):
        raise DatasetQueryError("数据集连接器缺少有效的版本标识")
    configured_dataset_id = str(config.get("dataset_id") or "").strip() or None
    return version_id, configured_dataset_id


def _validate_relation_key(value: Any) -> str:
    key = str(value or "")
    if (
        not key
        or key != key.strip()
        or key == "*"
        or len(key) > 300
        or any(ord(character) < 32 for character in key)
    ):
        raise DatasetQueryError("数据集包含无效的逻辑关系标识")
    return key


def _canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DatasetQueryError("数据集 manifest 不是稳定 JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _plain_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DatasetQueryError(f"{label}格式无效")
    try:
        normalized = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise DatasetQueryError(f"{label}不是稳定 JSON") from exc
    if not isinstance(normalized, dict):
        raise DatasetQueryError(f"{label}格式无效")
    return normalized


def _manifest_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetQueryError(f"{label}必须是非负整数")
    return value


def _catalog_field_contract(fields: Sequence[Any]) -> list[dict[str, Any]]:
    contract: list[dict[str, Any]] = []
    for expected_ordinal, field in enumerate(fields):
        ordinal = int(getattr(field, "ordinal", -1))
        source_name = str(getattr(field, "source_name", "") or "")
        physical_type = str(getattr(field, "physical_type", "") or "")
        key_ordinal = getattr(field, "key_ordinal", None)
        if (
            ordinal != expected_ordinal
            or not source_name
            or source_name != source_name.strip()
            or not physical_type
            or (key_ordinal is not None and int(key_ordinal) < 0)
        ):
            raise DatasetQueryError("数据集字段目录顺序或类型无效")
        contract.append(
            {
                "name": source_name,
                "physical_type": physical_type,
                "nullable": bool(getattr(field, "nullable", True)),
                "key_ordinal": int(key_ordinal) if key_ordinal is not None else None,
                "ordinal": ordinal,
            }
        )
    if not contract:
        raise DatasetQueryError("数据集逻辑关系没有字段目录")
    return contract


def _sql_without_literals_or_comments(statement: str) -> str:
    """Keep SQL names/operators while blanking literals and comments."""
    output: list[str] = []
    index = 0
    quote = ""
    line_comment = False
    block_comment = False
    while index < len(statement):
        character = statement[index]
        following = statement[index + 1] if index + 1 < len(statement) else ""
        if line_comment:
            output.append("\n" if character in "\r\n" else " ")
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            output.append(" ")
            if character == "*" and following == "/":
                output.append(" ")
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            if quote == "'":
                output.append(" ")
            else:
                output.append(character)
            if character == quote:
                if following == quote:
                    output.append(" " if quote == "'" else following)
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if character == "-" and following == "-":
            output.extend((" ", " "))
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            output.extend((" ", " "))
            block_comment = True
            index += 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            output.append(" " if quote == "'" else character)
            index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _validate_derived_select(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1_000_000:
        raise DatasetQueryError("派生关系缺少有效的受控 SELECT")
    try:
        statement = validate_read_only_sql(value, dialect="duckdb")
    except Exception as exc:  # PolicyViolation is intentionally hidden here.
        raise DatasetQueryError("派生关系 SQL 不是只读单语句") from exc
    scanned = _sql_without_literals_or_comments(statement)
    if not re.match(r"^\s*(?:SELECT|WITH)\b", scanned, flags=re.IGNORECASE):
        raise DatasetQueryError("派生关系只允许 SELECT 或 WITH 查询")
    if "?" in scanned or "$" in scanned or re.search(
        r"(?<!:)\:[A-Za-z_]", scanned
    ):
        raise DatasetQueryError("派生关系 SQL 不能包含运行时参数")
    if _DERIVED_EXTERNAL_FUNCTION_RE.search(
        scanned
    ) or _DERIVED_SYSTEM_VALUE_RE.search(scanned):
        raise DatasetQueryError("派生关系 SQL 不能访问外部或系统数据源")
    return statement


def _load_catalog(source: Any) -> DatasetCatalog:
    """Load and tenant-check one immutable version from the platform catalog."""
    version_id, configured_dataset_id = _source_config(source)
    try:
        from .. import models

        DatasetVersion = models.DatasetVersion
        DatasetSchema = models.DatasetSchema
        DatasetRelation = models.DatasetRelation
        DatasetField = models.DatasetField
        DatasetFragment = models.DatasetFragment
        LogicalDataset = models.LogicalDataset
        BucketFile = models.BucketFile
    except (AttributeError, ImportError) as exc:
        raise DatasetQueryError("平台尚未安装数据集目录模型") from exc

    with SessionLocal() as session:
        version = session.get(DatasetVersion, version_id)
        if version is None or str(getattr(version, "status", "") or "").lower() != "ready":
            raise DatasetQueryError("数据集版本不存在或尚未就绪")
        dataset_id = str(getattr(version, "dataset_id", "") or "").strip()
        dataset = session.get(LogicalDataset, dataset_id) if dataset_id else None
        if dataset is None:
            raise DatasetQueryError("数据集版本缺少有效的数据集归属")
        if configured_dataset_id is not None and configured_dataset_id != dataset_id:
            raise DatasetQueryError("数据集连接器配置与版本归属不一致")
        if str(getattr(dataset, "tenant_id", "") or "") != str(
            getattr(source, "tenant_id", "") or ""
        ):
            raise DatasetQueryError("数据集连接器无权访问该数据集版本")

        relation_schema_attr = _model_attr(DatasetRelation, "schema_id")
        schema_id = str(getattr(version, "schema_id", "") or "").strip()
        if not schema_id:
            raise DatasetQueryError("数据集版本缺少 Schema 版本")
        dataset_schema = session.get(DatasetSchema, schema_id)
        if dataset_schema is None or str(
            getattr(dataset_schema, "dataset_id", "") or ""
        ) != dataset_id:
            raise DatasetQueryError("数据集版本的 Schema 不属于同一逻辑数据集")
        schema_document = _plain_json_object(
            getattr(dataset_schema, "schema_document", None),
            "数据集 Schema 文档",
        )
        schema_hash = str(getattr(dataset_schema, "schema_hash", "") or "").lower()
        if (
            not _SHA256_RE.fullmatch(schema_hash)
            or _canonical_json_sha256(schema_document) != schema_hash
        ):
            raise DatasetQueryError("数据集 Schema 文档哈希校验失败")
        version_manifest = _plain_json_object(
            getattr(version, "manifest", None),
            "数据集版本 manifest",
        )
        relation_order = _model_attr(DatasetRelation, "ordinal", "ordinal_position")
        relations = list(
            session.scalars(
                select(DatasetRelation)
                .where(relation_schema_attr == schema_id)
                .order_by(relation_order, DatasetRelation.id)
            )
        )
        if not relations:
            raise DatasetQueryError("数据集版本没有可查询的逻辑关系")

        fragment_version_attr = _model_attr(DatasetFragment, "dataset_version_id")
        fragment_order = _model_attr(DatasetFragment, "ordinal", "ordinal_position")
        fragments = list(
            session.scalars(
                select(DatasetFragment)
                .where(fragment_version_attr == version_id)
                .order_by(fragment_order, DatasetFragment.id)
            )
        )
        if not fragments:
            raise DatasetQueryError("数据集版本没有可查询的 Parquet 分片")

        relation_by_id: dict[str, Any] = {}
        relation_key_seen: set[str] = set()
        relation_key_folded_seen: set[str] = set()
        relation_kind_by_id: dict[str, str] = {}
        relation_key_by_id: dict[str, str] = {}
        for relation in relations:
            relation_id = str(getattr(relation, "id", "") or "")
            relation_key = _validate_relation_key(
                _value(relation, "relation_key", "name")
            )
            folded_key = relation_key.casefold()
            if (
                not relation_id
                or relation_key in relation_key_seen
                or folded_key in relation_key_folded_seen
            ):
                raise DatasetQueryError("数据集 Schema 包含重复的逻辑关系")
            kind = str(getattr(relation, "kind", "table") or "").lower()
            if kind not in {"table", "view"}:
                raise DatasetQueryError("当前查询网关只支持表和受控派生视图")
            relation_key_seen.add(relation_key)
            relation_key_folded_seen.add(folded_key)
            relation_by_id[relation_id] = relation
            relation_kind_by_id[relation_id] = kind
            relation_key_by_id[relation_id] = relation_key

        field_relation_attr = _model_attr(DatasetField, "dataset_relation_id")
        field_order = _model_attr(DatasetField, "ordinal", "ordinal_position")
        catalog_fields: dict[str, list[Any]] = {
            relation_id: [] for relation_id in relation_by_id
        }
        for field in session.scalars(
            select(DatasetField)
            .where(field_relation_attr.in_(tuple(relation_by_id)))
            .order_by(field_relation_attr, field_order, DatasetField.id)
        ):
            relation_id = str(getattr(field, "dataset_relation_id", "") or "")
            if relation_id not in catalog_fields:
                raise DatasetQueryError("数据集字段引用了其他 Schema 的逻辑关系")
            catalog_fields[relation_id].append(field)
        field_contracts = {
            relation_id: _catalog_field_contract(fields)
            for relation_id, fields in catalog_fields.items()
        }

        schema_base = _plain_json_object(
            schema_document.get("relations"), "Schema 基础关系清单"
        )
        schema_derived = _plain_json_object(
            schema_document.get("derived_relations", {}), "Schema 派生关系清单"
        )
        manifest_base = _plain_json_object(
            version_manifest.get("relations"), "版本基础关系清单"
        )
        manifest_derived = _plain_json_object(
            version_manifest.get("derived_relations", {}), "版本派生关系清单"
        )
        base_keys = {
            relation_key_by_id[relation_id]
            for relation_id, kind in relation_kind_by_id.items()
            if kind == "table"
        }
        view_keys = {
            relation_key_by_id[relation_id]
            for relation_id, kind in relation_kind_by_id.items()
            if kind == "view"
        }
        if (
            set(schema_base) != base_keys
            or set(manifest_base) != base_keys
            or set(schema_derived) != view_keys
            or set(manifest_derived) != view_keys
            or schema_derived != manifest_derived
        ):
            raise DatasetQueryError("manifest、Schema 与关系目录不一致")

        relation_id_by_key = {
            relation_key: relation_id
            for relation_id, relation_key in relation_key_by_id.items()
        }
        derived_sql_by_key: dict[str, str] = {}
        for relation_key in sorted(base_keys):
            relation_id = relation_id_by_key[relation_key]
            expected_schema = field_contracts[relation_id]
            if schema_base.get(relation_key) != expected_schema:
                raise DatasetQueryError("基础关系字段目录与 Schema 文档不一致")
            details = manifest_base.get(relation_key)
            if not isinstance(details, Mapping) or str(
                details.get("schema_hash") or ""
            ).lower() != _canonical_json_sha256(expected_schema):
                raise DatasetQueryError("基础关系 Schema 哈希与 manifest 不一致")

        for relation_key in sorted(view_keys):
            relation_id = relation_id_by_key[relation_key]
            expected_schema = field_contracts[relation_id]
            details = manifest_derived.get(relation_key)
            if (
                not isinstance(details, Mapping)
                or details.get("kind") != "view"
                or details.get("materialized") is not False
                or details.get("schema") != expected_schema
                or str(details.get("schema_hash") or "").lower()
                != _canonical_json_sha256(expected_schema)
            ):
                raise DatasetQueryError("派生关系 manifest 与字段目录不一致")
            derived_sql_by_key[relation_key] = _validate_derived_select(
                details.get("view_sql")
            )

        grouped: dict[str, list[DatasetFragmentSpec]] = {
            relation_id: [] for relation_id in relation_by_id
        }
        relation_rows: dict[str, int] = {relation_id: 0 for relation_id in relation_by_id}
        for fragment in fragments:
            status = str(getattr(fragment, "status", "ready") or "").lower()
            if status != "ready":
                raise DatasetQueryError("就绪数据集版本包含未就绪分片")
            file_format = str(getattr(fragment, "format", "parquet") or "").lower()
            if file_format != "parquet":
                raise DatasetQueryError("当前查询网关只支持 Parquet 数据集分片")
            relation_id = str(
                _value(fragment, "dataset_relation_id", "relation_id", default="") or ""
            )
            if relation_id not in relation_by_id:
                raise DatasetQueryError("数据集分片引用了其他 Schema 的逻辑关系")
            fragment_schema_id = str(getattr(fragment, "schema_id", schema_id) or "")
            if fragment_schema_id != schema_id:
                raise DatasetQueryError("数据集分片引用了其他 Schema 版本")
            if relation_kind_by_id[relation_id] != "table":
                raise DatasetQueryError("派生视图不能绑定物理数据分片")
            bucket_file_id = str(getattr(fragment, "bucket_file_id", "") or "")
            bucket_file = session.get(BucketFile, bucket_file_id) if bucket_file_id else None
            if bucket_file is None:
                raise DatasetQueryError("数据集分片缺少 MinIO 对象记录")
            file_source = getattr(bucket_file, "data_source", None)
            if file_source is None or str(
                getattr(file_source, "tenant_id", "") or ""
            ) != str(getattr(source, "tenant_id", "") or ""):
                raise DatasetQueryError("数据集分片跨越了租户边界")
            if str(getattr(bucket_file, "storage_provider", "") or "").lower() != "minio":
                raise DatasetQueryError("数据集分片必须存储在 MinIO")

            fragment_sha = str(getattr(fragment, "content_sha256", "") or "").lower()
            file_sha = str(getattr(bucket_file, "content_sha256", "") or "").lower()
            if fragment_sha and file_sha and fragment_sha != file_sha:
                raise DatasetQueryError("数据集目录与文件记录的内容哈希不一致")
            expected_sha = fragment_sha or file_sha
            if not _SHA256_RE.fullmatch(expected_sha):
                raise DatasetQueryError("数据集分片缺少有效的 SHA-256")

            fragment_size = int(getattr(fragment, "byte_size", 0) or 0)
            file_size = int(getattr(bucket_file, "size", 0) or 0)
            if fragment_size > 0 and file_size > 0 and fragment_size != file_size:
                raise DatasetQueryError("数据集目录与文件记录的大小不一致")
            expected_size = fragment_size or file_size
            if expected_size <= 0:
                raise DatasetQueryError("数据集分片缺少有效的文件大小")

            bucket_name = str(getattr(bucket_file, "bucket_name", "") or "").strip()
            object_key = str(getattr(bucket_file, "object_key", "") or "").strip()
            if not bucket_name or not object_key:
                raise DatasetQueryError("数据集分片缺少 MinIO 对象身份")
            row_count = int(getattr(fragment, "row_count", 0) or 0)
            if row_count < 0:
                raise DatasetQueryError("数据集分片行数无效")
            relation_rows[relation_id] += row_count
            grouped[relation_id].append(
                DatasetFragmentSpec(
                    id=str(getattr(fragment, "id", "") or ""),
                    bucket_name=bucket_name,
                    object_key=object_key,
                    version_id=str(
                        getattr(bucket_file, "object_version_id", "") or ""
                    ).strip(),
                    content_sha256=expected_sha,
                    byte_size=expected_size,
                    ordinal=int(_value(fragment, "ordinal", "ordinal_position", default=0) or 0),
                )
            )

        relation_specs: list[DatasetRelationSpec] = []
        for relation_id, relation in relation_by_id.items():
            relation_fragments = tuple(
                sorted(grouped[relation_id], key=lambda item: (item.ordinal, item.id))
            )
            kind = relation_kind_by_id[relation_id]
            if kind == "table" and not relation_fragments:
                raise DatasetQueryError("数据集逻辑关系没有就绪分片")
            if kind == "view" and relation_fragments:
                raise DatasetQueryError("派生视图不能绑定物理数据分片")
            relation_key = relation_key_by_id[relation_id]
            if kind == "table":
                manifest_details = manifest_base[relation_key]
                relation_byte_size = sum(
                    fragment.byte_size for fragment in relation_fragments
                )
                if (
                    _manifest_count(
                        manifest_details.get("row_count"), "基础关系 row_count"
                    )
                    != relation_rows[relation_id]
                    or _manifest_count(
                        manifest_details.get("byte_size"), "基础关系 byte_size"
                    )
                    != relation_byte_size
                    or (
                        len(relation_fragments) == 1
                        and str(manifest_details.get("content_sha256") or "").lower()
                        != relation_fragments[0].content_sha256
                    )
                ):
                    raise DatasetQueryError("基础关系 manifest 与分片目录不一致")
            relation_specs.append(
                DatasetRelationSpec(
                    id=relation_id,
                    relation_key=relation_key,
                    ordinal=int(
                        _value(relation, "ordinal", "ordinal_position", default=0) or 0
                    ),
                    row_count=(relation_rows[relation_id] if kind == "table" else -1),
                    fragments=relation_fragments,
                    kind=kind,
                    view_sql=derived_sql_by_key.get(relation_key, ""),
                    expected_columns=tuple(
                        item["name"] for item in field_contracts[relation_id]
                    ),
                    declared_columns=tuple(
                        (
                            str(item["name"]),
                            str(item["physical_type"]),
                            bool(item["nullable"]),
                            item["key_ordinal"],
                        )
                        for item in field_contracts[relation_id]
                    ),
                )
            )
        relation_specs.sort(key=lambda item: (item.ordinal, item.relation_key, item.id))
        declared_fragment_count = int(getattr(version, "fragment_count", 0) or 0)
        if declared_fragment_count and declared_fragment_count != len(fragments):
            raise DatasetQueryError("数据集版本的分片计数与目录不一致")
        catalog_row_count = sum(
            item.row_count for item in relation_specs if item.kind == "table"
        )
        declared_row_count = int(getattr(version, "record_count", 0) or 0)
        if declared_row_count and declared_row_count != catalog_row_count:
            raise DatasetQueryError("数据集版本的行数与分片目录不一致")
        catalog_byte_size = sum(
            fragment.byte_size
            for relation in relation_specs
            for fragment in relation.fragments
        )
        declared_byte_size = int(getattr(version, "byte_size", 0) or 0)
        if declared_byte_size and declared_byte_size != catalog_byte_size:
            raise DatasetQueryError("数据集版本的字节数与分片目录不一致")
        return DatasetCatalog(
            dataset_id=dataset_id,
            dataset_version_id=version_id,
            relations=tuple(relation_specs),
        )


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _verified_file(
    path: Path,
    expected_sha256: str,
    expected_size: int,
    *,
    remember: bool = True,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    details = path.stat()
    cache_key = str(path)
    with _verified_cache_files_guard:
        cached = _verified_cache_files.get(cache_key)
    if cached == (details.st_size, details.st_mtime_ns, expected_sha256):
        return details.st_size == expected_size
    actual_sha256, actual_size = _file_sha256(path)
    valid = actual_size == expected_size and actual_sha256 == expected_sha256
    with _verified_cache_files_guard:
        if valid and remember:
            _verified_cache_files[cache_key] = (
                details.st_size,
                details.st_mtime_ns,
                expected_sha256,
            )
        elif not valid:
            _verified_cache_files.pop(cache_key, None)
    return valid


def _access_marker(access_root: Path, content_sha256: str) -> Path:
    return access_root / f"{content_sha256}.access"


def _touch_cache_access(access_root: Path, content_sha256: str) -> None:
    marker = _access_marker(access_root, content_sha256)
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    os.utime(marker, None)


def _lease_markers(leases_root: Path, content_sha256: str) -> list[Path]:
    return list(leases_root.glob(f"{content_sha256}.*.lease"))


def _entry_has_active_lease(leases_root: Path, content_sha256: str) -> bool:
    # Windows byte-range locks do not provide a portable guarantee between two
    # threads of the same process, so pair them with an in-process registry.
    with _active_cache_leases_guard:
        if _active_cache_leases.get(content_sha256, 0) > 0:
            return True
    active = False
    for marker in _lease_markers(leases_root, content_sha256):
        probe = _InterProcessLock(marker, blocking=False, timeout=0)
        try:
            acquired = probe.acquire()
        except OSError:
            active = True
            continue
        if not acquired:
            active = True
            continue
        probe.release()
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
    return active


def _acquire_cache_lease_locked(
    path: Path,
    leases_root: Path,
    access_root: Path,
    content_sha256: str,
) -> _CachedFragmentLease:
    marker = leases_root / (
        f"{content_sha256}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.lease"
    )
    lock = _InterProcessLock(marker)
    lock.acquire()
    _touch_cache_access(access_root, content_sha256)
    return _CachedFragmentLease(path, marker, lock, content_sha256)


def _cache_entries(root: Path) -> list[tuple[Path, str, int]]:
    entries: list[tuple[Path, str, int]] = []
    for path in root.glob("*.parquet"):
        content_sha256 = path.stem.lower()
        if not _SHA256_RE.fullmatch(content_sha256):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            entries.append((path, content_sha256, path.stat().st_size))
        except OSError:
            continue
    return entries


def _cache_last_access(path: Path, access_root: Path, content_sha256: str) -> float:
    marker = _access_marker(access_root, content_sha256)
    try:
        return marker.stat().st_mtime
    except OSError:
        return path.stat().st_mtime


def _remove_cache_entry_locked(
    path: Path,
    leases_root: Path,
    access_root: Path,
    content_sha256: str,
) -> bool:
    if _entry_has_active_lease(leases_root, content_sha256):
        return False
    try:
        path.unlink(missing_ok=True)
        _access_marker(access_root, content_sha256).unlink(missing_ok=True)
    except OSError:
        return False
    with _verified_cache_files_guard:
        _verified_cache_files.pop(str(path), None)
    return True


def _cleanup_stale_partials_locked(root: Path) -> None:
    for partial in root.glob(".*.partial*"):
        if partial.is_dir() and not partial.is_symlink():
            continue
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _evict_cache_locked(
    root: Path,
    leases_root: Path,
    access_root: Path,
    policy: _CachePolicy,
    *,
    required_bytes: int,
    protected: set[str],
) -> None:
    _cleanup_stale_partials_locked(root)
    now = time.time()
    entries = _cache_entries(root)
    ordered = sorted(
        entries,
        key=lambda item: (_cache_last_access(item[0], access_root, item[1]), item[1]),
    )
    for path, content_sha256, _size in ordered:
        if content_sha256 in protected:
            continue
        if now - _cache_last_access(path, access_root, content_sha256) <= policy.max_age_seconds:
            continue
        _remove_cache_entry_locked(path, leases_root, access_root, content_sha256)

    entries = _cache_entries(root)
    total = sum(item[2] for item in entries)
    ordered = sorted(
        entries,
        key=lambda item: (_cache_last_access(item[0], access_root, item[1]), item[1]),
    )
    for path, content_sha256, size in ordered:
        if total + required_bytes <= policy.max_total_bytes:
            break
        if content_sha256 in protected:
            continue
        if _remove_cache_entry_locked(path, leases_root, access_root, content_sha256):
            total -= size
    if total + required_bytes > policy.max_total_bytes:
        raise DatasetQueryError("数据集缓存容量不足，且在用分片不能淘汰")


def _cache_path(root: Path, content_sha256: str) -> Path:
    if not _SHA256_RE.fullmatch(content_sha256):
        raise DatasetQueryError("数据集分片缺少有效的 SHA-256")
    cache_path = (root / f"{content_sha256}.parquet").resolve()
    if cache_path.parent != root:
        raise DatasetQueryError("数据集缓存路径校验失败")
    return cache_path


def _materialize_fragment_locked(
    fragment: DatasetFragmentSpec,
    root: Path,
    leases_root: Path,
    access_root: Path,
    policy: _CachePolicy,
) -> Path:
    if fragment.byte_size <= 0 or fragment.byte_size > policy.max_object_bytes:
        raise DatasetQueryError("数据集分片超过单对象缓存上限")
    cache_path = _cache_path(root, fragment.content_sha256)
    _evict_cache_locked(
        root,
        leases_root,
        access_root,
        policy,
        required_bytes=0,
        protected={fragment.content_sha256},
    )
    if _verified_file(cache_path, fragment.content_sha256, fragment.byte_size):
        _touch_cache_access(access_root, fragment.content_sha256)
        return cache_path
    if cache_path.exists() or cache_path.is_symlink():
        if cache_path.is_dir() and not cache_path.is_symlink():
            raise DatasetQueryError("数据集缓存目标不是普通文件")
        cache_path.unlink(missing_ok=True)
        with _verified_cache_files_guard:
            _verified_cache_files.pop(str(cache_path), None)
    _evict_cache_locked(
        root,
        leases_root,
        access_root,
        policy,
        required_bytes=fragment.byte_size,
        protected={fragment.content_sha256},
    )
    partial = root / f".{fragment.content_sha256}.{uuid.uuid4().hex}.partial"
    try:
        info = object_storage_service.download_object_to_file(
            fragment.bucket_name,
            fragment.object_key,
            partial,
            version_id=fragment.version_id,
            max_bytes=fragment.byte_size,
        )
        if int(info.size or 0) not in {0, fragment.byte_size}:
            raise DatasetQueryError("MinIO 返回的数据集分片大小不一致")
        if not _verified_file(
            partial,
            fragment.content_sha256,
            fragment.byte_size,
            remember=False,
        ):
            raise DatasetQueryError("数据集分片完整性校验失败")
        os.replace(partial, cache_path)
        if os.name != "nt":
            os.chmod(cache_path, 0o600)
    except DatasetQueryError:
        raise
    except (FileNotFoundError, object_storage_service.ObjectStorageError) as exc:
        raise DatasetQueryError("无法读取数据集的 MinIO 分片") from exc
    finally:
        partial.unlink(missing_ok=True)
    if not _verified_file(cache_path, fragment.content_sha256, fragment.byte_size):
        raise DatasetQueryError("数据集缓存完整性校验失败")
    _touch_cache_access(access_root, fragment.content_sha256)
    return cache_path


def _with_cache_locks(fragment: DatasetFragmentSpec) -> tuple[
    Path,
    Path,
    Path,
    Any,
    _InterProcessLock,
    _InterProcessLock,
]:
    _cache_process_guard.acquire()
    global_lock: _InterProcessLock | None = None
    try:
        root, locks_root, leases_root, access_root = _cache_layout()
        global_lock = _InterProcessLock(locks_root / "cache-global.lock")
        entry_lock = _InterProcessLock(
            locks_root / f"{fragment.content_sha256}.lock"
        )
        global_lock.acquire()
        entry_lock.acquire()
    except Exception:
        if global_lock is not None:
            global_lock.release()
        _cache_process_guard.release()
        raise
    return (
        root,
        leases_root,
        access_root,
        _cache_process_guard,
        global_lock,
        entry_lock,
    )


def _materialize_fragment(fragment: DatasetFragmentSpec) -> Path:
    """Return a bounded, verified content-addressed cache file."""
    (
        root,
        leases_root,
        access_root,
        process_guard,
        global_lock,
        entry_lock,
    ) = _with_cache_locks(fragment)
    try:
        return _materialize_fragment_locked(
            fragment, root, leases_root, access_root, _cache_policy()
        )
    finally:
        entry_lock.release()
        global_lock.release()
        process_guard.release()


def _acquire_cached_fragment(fragment: DatasetFragmentSpec) -> _CachedFragmentLease:
    (
        root,
        leases_root,
        access_root,
        process_guard,
        global_lock,
        entry_lock,
    ) = _with_cache_locks(fragment)
    try:
        path = _materialize_fragment_locked(
            fragment, root, leases_root, access_root, _cache_policy()
        )
        return _acquire_cache_lease_locked(
            path, leases_root, access_root, fragment.content_sha256
        )
    finally:
        entry_lock.release()
        global_lock.release()
        process_guard.release()


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _named_to_positional(
    statement: str, parameters: Mapping[str, Any]
) -> tuple[str, tuple[Any, ...]]:
    """Translate SQLAlchemy ``:name`` binds without touching literals/comments."""
    output: list[str] = []
    values: list[Any] = []
    index = 0
    quote = ""
    line_comment = False
    block_comment = False
    while index < len(statement):
        character = statement[index]
        following = statement[index + 1] if index + 1 < len(statement) else ""
        if line_comment:
            output.append(character)
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            output.append(character)
            if character == "*" and following == "/":
                output.append(following)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            output.append(character)
            if character == quote:
                if following == quote:
                    output.append(following)
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if character == "-" and following == "-":
            output.extend((character, following))
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            output.extend((character, following))
            block_comment = True
            index += 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            output.append(character)
            index += 1
            continue
        if character == ":" and following == ":":
            output.extend((character, following))
            index += 2
            continue
        if character == ":":
            match = _NAMED_PARAMETER_RE.match(statement, index + 1)
            if match is not None:
                name = match.group(0)
                if name not in parameters:
                    raise DatasetQueryError(f"参数化查询缺少绑定参数: {name}")
                output.append("?")
                values.append(parameters[name])
                index = match.end()
                continue
        output.append(character)
        index += 1
    return "".join(output), tuple(values)


def _duckdb_module() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise DatasetQueryError("服务端缺少 Parquet 查询引擎依赖") from exc
    return duckdb


@dataclass(frozen=True)
class _DuckDBPolicy:
    timeout_seconds: float
    max_concurrent_queries: int
    memory_limit_bytes: int
    threads: int
    temp_directory: Path
    max_temp_directory_bytes: int


def _duckdb_policy() -> _DuckDBPolicy:
    settings = get_settings()
    timeout_seconds = float(
        getattr(settings, "dataset_query_timeout_seconds", 30.0)
    )
    max_concurrent_queries = int(
        getattr(settings, "dataset_query_max_concurrency", 4)
    )
    memory_limit_bytes = int(
        getattr(settings, "dataset_duckdb_memory_limit_bytes", 512 * 1024 * 1024)
    )
    threads = int(getattr(settings, "dataset_duckdb_threads", 2))
    max_temp_directory_bytes = int(
        getattr(
            settings,
            "dataset_duckdb_max_temp_directory_bytes",
            1024 * 1024 * 1024,
        )
    )
    configured_temp = str(
        getattr(settings, "dataset_duckdb_temp_directory", "") or ""
    ).strip()
    if configured_temp:
        raw_temp = Path(configured_temp).expanduser()
        if not raw_temp.is_absolute():
            raise DatasetQueryError("DuckDB 临时目录必须使用绝对路径")
    else:
        raw_temp = _CACHE_ROOT / "duckdb-temp"
    temp_directory = _ensure_private_directory(raw_temp, "DuckDB 临时目录")
    if (
        not 0.0 < timeout_seconds <= 600.0
        or not 1 <= max_concurrent_queries <= 64
        or memory_limit_bytes < 64 * 1024 * 1024
        or not 1 <= threads <= 32
        or max_temp_directory_bytes < 64 * 1024 * 1024
    ):
        raise DatasetQueryError("DuckDB 资源治理配置无效")
    return _DuckDBPolicy(
        timeout_seconds=timeout_seconds,
        max_concurrent_queries=max_concurrent_queries,
        memory_limit_bytes=memory_limit_bytes,
        threads=threads,
        temp_directory=temp_directory,
        max_temp_directory_bytes=max_temp_directory_bytes,
    )


def _query_semaphore(limit: int) -> threading.BoundedSemaphore:
    with _query_semaphores_guard:
        semaphore = _query_semaphores.get(limit)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _query_semaphores[limit] = semaphore
        return semaphore


def _derived_view_plan(
    connection: Any, catalog: DatasetCatalog
) -> dict[str, tuple[DatasetRelationSpec, set[str]]]:
    relation_by_key = {relation.relation_key: relation for relation in catalog.relations}
    pending: dict[str, tuple[DatasetRelationSpec, set[str]]] = {}
    for relation in catalog.relations:
        if relation.kind != "view":
            continue
        statement = _validate_derived_select(relation.view_sql)
        try:
            dependencies = {
                str(name) for name in connection.get_table_names(statement)
            }
        except Exception as exc:  # noqa: BLE001
            raise DatasetQueryError("无法解析派生关系依赖") from exc
        if not dependencies:
            raise DatasetQueryError("派生关系必须引用同一数据集中的逻辑关系")
        unknown = dependencies - set(relation_by_key)
        if unknown:
            raise DatasetQueryError("派生关系引用了目录之外的逻辑关系")
        pending[relation.relation_key] = (relation, dependencies)
    return pending


def _register_derived_views(
    connection: Any,
    catalog: DatasetCatalog,
    pending: dict[str, tuple[DatasetRelationSpec, set[str]]],
) -> None:
    base_keys = {
        relation.relation_key
        for relation in catalog.relations
        if relation.kind == "table"
    }

    created = set(base_keys)
    while pending:
        ready = sorted(
            (
                (key, relation, dependencies)
                for key, (relation, dependencies) in pending.items()
                if dependencies <= created
            ),
            key=lambda item: (item[1].ordinal, item[0], item[1].id),
        )
        if not ready:
            raise DatasetQueryError("派生关系依赖存在循环或未就绪引用")
        for key, relation, _dependencies in ready:
            try:
                connection.execute(
                    f"CREATE VIEW {_quote_identifier(key)} AS {relation.view_sql}"
                )
            except Exception as exc:  # noqa: BLE001
                raise DatasetQueryError("派生关系创建失败") from exc
            created.add(key)
            pending.pop(key)


def _validate_runtime_schemas(connection: Any, catalog: DatasetCatalog) -> None:
    for relation in catalog.relations:
        if not relation.expected_columns:
            continue
        try:
            rows = connection.execute(
                f"DESCRIBE SELECT * FROM {_quote_identifier(relation.relation_key)}"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DatasetQueryError("无法核验数据集运行时 Schema") from exc
        actual_columns = tuple(str(row[0]) for row in rows)
        if actual_columns != relation.expected_columns:
            raise DatasetQueryError("数据集运行时 Schema 与字段目录不一致")


class DatasetConnection:
    """One isolated DuckDB connection over a pinned catalog snapshot."""

    dialect = "dataset"

    def __init__(self, catalog: DatasetCatalog) -> None:
        self.catalog = catalog
        self._connection: Any | None = None
        self._query_lock = threading.RLock()
        self._cache_leases: list[_CachedFragmentLease] = []
        self._query_timeout_seconds = 0.0
        self._query_semaphore: threading.BoundedSemaphore | None = None
        connection: Any | None = None
        initialization_slot_acquired = False
        try:
            policy = _duckdb_policy()
            self._query_timeout_seconds = policy.timeout_seconds
            self._query_semaphore = _query_semaphore(
                policy.max_concurrent_queries
            )
            paths_by_relation: list[tuple[DatasetRelationSpec, list[Path]]] = []
            allowed_paths: list[str] = []
            leases_by_sha: dict[str, tuple[_CachedFragmentLease, int]] = {}
            for relation in catalog.relations:
                if relation.kind != "table":
                    continue
                paths: list[Path] = []
                for fragment in relation.fragments:
                    cached = leases_by_sha.get(fragment.content_sha256)
                    if cached is not None:
                        lease, declared_size = cached
                        if declared_size != fragment.byte_size:
                            raise DatasetQueryError(
                                "同一内容哈希的数据集分片大小不一致"
                            )
                    else:
                        lease = _acquire_cached_fragment(fragment)
                        self._cache_leases.append(lease)
                        leases_by_sha[fragment.content_sha256] = (
                            lease,
                            fragment.byte_size,
                        )
                    paths.append(lease.path)
                paths_by_relation.append((relation, paths))
                allowed_paths.extend(str(path) for path in paths)

            initialization_slot_acquired = self._query_semaphore.acquire(
                timeout=self._query_timeout_seconds
            )
            if not initialization_slot_acquired:
                raise DatasetQueryError("等待数据集查询执行槽超时")
            connection = _duckdb_module().connect(":memory:")
            if not callable(getattr(connection, "interrupt", None)):
                raise DatasetQueryError("DuckDB 查询引擎不支持可靠中断")
            connection.execute(
                "SET memory_limit = ?", [f"{policy.memory_limit_bytes}B"]
            )
            connection.execute("SET threads = ?", [policy.threads])
            connection.execute("SET temp_directory = ?", [str(policy.temp_directory)])
            connection.execute(
                "SET max_temp_directory_size = ?",
                [f"{policy.max_temp_directory_bytes}B"],
            )
            connection.execute("SET preserve_insertion_order = false")
            connection.execute("SET allowed_paths = ?", [allowed_paths])
            connection.execute("SET enable_external_access = false")
            connection.execute("SET allow_persistent_secrets = false")
            connection.execute("SET autoinstall_known_extensions = false")
            connection.execute("SET autoload_known_extensions = false")
            # The medical audit SQL is intentionally shared with SQLite.
            connection.execute("CREATE MACRO julianday(value) AS julian(value)")
            derived_plan = _derived_view_plan(connection, catalog)
            for relation, paths in paths_by_relation:
                connection.from_parquet(
                    [str(path) for path in paths], union_by_name=False
                ).create_view(relation.relation_key)
            _register_derived_views(connection, catalog, derived_plan)
            _validate_runtime_schemas(connection, catalog)
            connection.execute("SET lock_configuration = true")
            self._connection = connection
        except Exception as exc:  # noqa: BLE001
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - preserve initialization cause
                    pass
            self._release_cache_leases()
            raise DatasetQueryError("数据集查询引擎初始化失败") from exc
        finally:
            if initialization_slot_acquired and self._query_semaphore is not None:
                self._query_semaphore.release()

    def _release_cache_leases(self) -> None:
        leases, self._cache_leases = self._cache_leases, []
        for lease in reversed(leases):
            lease.release()

    def _run_with_timeout(self, operation: Any) -> Any:
        """Run one connection operation and cancel it after the configured limit."""
        with self._query_lock:
            connection = self._connection
            if connection is None:
                raise DatasetQueryError("数据集查询连接已关闭")
            semaphore = self._query_semaphore
            if semaphore is None:
                raise DatasetQueryError("数据集查询资源策略未初始化")
            deadline = time.monotonic() + self._query_timeout_seconds
            if not semaphore.acquire(timeout=self._query_timeout_seconds):
                raise DatasetQueryError("等待数据集查询执行槽超时")
            timed_out = threading.Event()

            def cancel() -> None:
                timed_out.set()
                try:
                    connection.interrupt()
                except Exception:  # noqa: BLE001 - caller observes timeout below
                    pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                semaphore.release()
                raise DatasetQueryError("等待数据集查询执行槽超时")
            timer = threading.Timer(remaining, cancel)
            timer.daemon = True
            timer.start()
            try:
                result = operation(connection)
                if timed_out.is_set():
                    raise DatasetQueryError("数据集查询执行超时")
                return result
            except DatasetQueryError:
                raise
            except Exception as exc:  # noqa: BLE001
                if timed_out.is_set():
                    raise DatasetQueryError("数据集查询执行超时") from exc
                raise
            finally:
                timer.cancel()
                # Do not release the query lock until the timer callback has
                # returned; a late interrupt must never cancel the next query.
                timer.join()
                semaphore.release()

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            statement = validate_read_only_sql(sql, dialect="duckdb")
        except PolicyViolation as exc:
            raise DatasetQueryError("数据集查询违反只读策略") from exc
        values: Sequence[Any] = ()
        if parameters is None:
            pass
        elif isinstance(parameters, Mapping):
            statement, values = _named_to_positional(statement, parameters)
        elif isinstance(parameters, Sequence) and not isinstance(
            parameters, (str, bytes, bytearray)
        ):
            values = tuple(parameters)
        else:
            raise DatasetQueryError("数据集查询绑定参数无效")
        try:
            def query(connection: Any) -> tuple[list[str], list[tuple[Any, ...]]]:
                cursor = connection.execute(statement, values)
                columns = [str(item[0]) for item in (cursor.description or ())]
                return columns, list(cursor.fetchall())

            return self._run_with_timeout(query)
        except DatasetQueryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DatasetQueryError("数据集只读查询执行失败") from exc

    def fetch_limited(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None,
        limit: int,
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        try:
            statement = validate_read_only_sql(sql, dialect="duckdb")
        except PolicyViolation as exc:
            raise DatasetQueryError("数据集查询违反只读策略") from exc
        values: Sequence[Any] = ()
        if parameters is None:
            pass
        elif isinstance(parameters, Mapping):
            statement, values = _named_to_positional(statement, parameters)
        elif isinstance(parameters, Sequence) and not isinstance(
            parameters, (str, bytes, bytearray)
        ):
            values = tuple(parameters)
        else:
            raise DatasetQueryError("数据集查询绑定参数无效")
        try:
            def query(connection: Any) -> tuple[list[str], list[tuple[Any, ...]]]:
                cursor = connection.execute(statement, values)
                columns = [str(item[0]) for item in (cursor.description or ())]
                return columns, list(cursor.fetchmany(limit + 1))

            columns, rows = self._run_with_timeout(query)
        except DatasetQueryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DatasetQueryError("数据集只读查询执行失败") from exc
        return columns, rows[:limit], len(rows) > limit

    def describe(self) -> list[dict[str, Any]]:
        try:
            def describe_all(connection: Any) -> list[dict[str, Any]]:
                tables: list[dict[str, Any]] = []
                for relation in self.catalog.relations:
                    rows = connection.execute(
                        "DESCRIBE SELECT * FROM "
                        f"{_quote_identifier(relation.relation_key)}"
                    ).fetchall()
                    tables.append(
                        {
                            "name": relation.relation_key,
                            "columns": [
                                {
                                    "name": str(row[0]),
                                    "type": str(row[1]),
                                    "pk": str(row[3] or "").upper() == "PRI",
                                }
                                for row in rows
                            ],
                            "row_count": relation.row_count,
                        }
                    )
                return tables

            return self._run_with_timeout(describe_all)
        except DatasetQueryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DatasetQueryError("无法读取数据集 Schema") from exc

    def close(self) -> None:
        with self._query_lock:
            connection, self._connection = self._connection, None
            try:
                if connection is not None:
                    connection.close()
            finally:
                # DuckDB must close its Parquet readers before a cache file can
                # become eligible for cross-process eviction.
                self._release_cache_leases()


def open_connection(source: Any) -> DatasetConnection:
    if str(getattr(source, "type", "") or "").lower() != "dataset":
        raise DatasetQueryError("查询连接器不是数据集类型")
    return DatasetConnection(_load_catalog(source))


def test_connection(source: Any) -> None:
    connection = open_connection(source)
    try:
        connection.execute("SELECT 1")
    finally:
        connection.close()


def _catalog_description(catalog: DatasetCatalog) -> list[dict[str, Any]]:
    return [
        {
            "name": relation.relation_key,
            "columns": [
                {"name": name, "type": physical_type, "pk": key_ordinal is not None}
                for name, physical_type, _nullable, key_ordinal in relation.declared_columns
            ],
            "row_count": relation.row_count,
        }
        for relation in catalog.relations
    ]


def list_tables(source: Any) -> list[dict[str, Any]]:
    # The catalog schema and its hash were already validated in PostgreSQL.
    # Listing metadata must not download or open every immutable fragment.
    if str(getattr(source, "type", "") or "").lower() != "dataset":
        raise DatasetQueryError("查询连接器不是数据集类型")
    return _catalog_description(_load_catalog(source))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def run_query(
    source: Any,
    sql: str,
    *,
    parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    limit: int,
) -> dict[str, Any]:
    # Validate before touching the catalog, MinIO, or DuckDB.
    statement = validate_read_only_sql(sql, dialect="duckdb")
    connection = open_connection(source)
    try:
        columns, rows, truncated = connection.fetch_limited(
            statement, parameters, max(1, int(limit))
        )
    finally:
        connection.close()
    data = [[_jsonable(value) for value in row] for row in rows]
    return {
        "columns": columns,
        "rows": data,
        "row_count": len(data),
        "truncated": truncated,
    }
