"""Materialize reusable validation tables as MinIO-backed Parquet datasets."""
from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import date, datetime, timezone
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterable, Iterator, Literal, Sequence
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..catalog_schemas import ValidationDatasetBuildIn
from ..config import get_settings
from ..database import SessionLocal
from ..models import (
    Agent,
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetField,
    DatasetFragment,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    DatasetVersionAsset,
    IngestionRun,
    IngestionRunInput,
    LogicalDataset,
)
from . import (
    agent_scope_access_service,
    catalog_ingestion_service,
    catalog_service,
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    tabular_materialization_service,
    tenant_service,
)


class ValidationDatasetError(ValueError):
    pass


class NoMaterializableTableError(ValidationDatasetError):
    """The source is a valid table document but contains no data rows."""

    pass


_IDENTIFIER_MARKERS = (
    "id", "code", "number", "编号", "编码", "代码", "证件", "卡号", "单号", "序号"
)
_KEY_CLEAN_RE = re.compile(r"[\x00-\x1f\x7f]")
_BATCH_ROWS = 5_000
_PARQUET_TARGET_BYTES = 256 * 1024 * 1024
_VALIDATION_PIPELINE_KIND = "validation_dataset"
_VALIDATION_PIPELINE_VERSION = "validation-dataset/v2"
_VALIDATION_JOB_LEASE_SECONDS = 300


def _resolve_agent_scope(
    db: Session,
    agent_id: str | None,
    *,
    permission_verb: Literal["read", "write"] = "read",
    lock: bool = False,
    active_runtime: bool = False,
) -> str | None:
    """Validate the optional Agent scope before selecting source assets.

    Enqueueing is a durable ownership write.  Its caller must hold the Agent
    row lock from the authorization check through creation of the logical
    dataset/job so Agent deletion cannot pass its owner scan in between.  The
    expensive builder and read-only job endpoints leave the lock off and use
    the publication fence when they eventually write.
    """
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        permission_service.require_tenant_permission(db, permission_verb)
        return None
    statement = select(Agent).where(
        Agent.id == normalized_agent_id,
        Agent.tenant_id == tenant_service.current_tenant_id(db),
    ).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    agent = db.scalar(statement)
    if agent is None:
        raise ValidationDatasetError("Agent 不存在")
    _validate_agent_scope(
        db,
        agent,
        permission_verb=permission_verb,
        active_runtime=active_runtime,
    )
    return str(agent.id)


def _validate_agent_scope(
    db: Session,
    agent: Agent,
    *,
    permission_verb: Literal["read", "write"] = "read",
    active_runtime: bool = False,
) -> None:
    """Revalidate the Agent binding at a publication boundary."""
    try:
        scenario = agent_scope_access_service.require_agent_permission(
            db,
            agent,
            permission_verb,
            message="没有该 Agent 所属业务场景的权限",
        )
    except agent_scope_access_service.AgentScopeNotFoundError as exc:
        raise ValidationDatasetError("Agent 不存在") from exc
    if active_runtime and scenario is not None and scenario.status == "retired":
        raise ValidationDatasetError("业务场景已退役，不能生成新的验证数据包")


def _validate_input_source_lineage(
    db: Session,
    versions: Sequence[DataAssetVersion],
    *,
    tenant_id: str,
    agent_id: str | None,
) -> None:
    """Prove every selected version still belongs to its asset/source scope.

    ``bucket_file_id`` is tenant-local and therefore cannot prove ownership on
    its own.  Join the immutable version to the exact file and source row, then
    reuse the catalog source proof.  The shared external upload bucket is
    accepted only through that deterministic source identity; modeling or a
    different Agent's runtime source is never a valid Agent validation input.
    """
    version_ids = {str(item.id) for item in versions}
    if not version_ids:
        raise ValidationDatasetError("未选择验证资料")
    rows = db.execute(
        select(DataAssetVersion, DataAsset, BucketFile, DataSource)
        .join(
            DataAsset,
            (DataAsset.id == DataAssetVersion.asset_id)
            & (DataAsset.tenant_id == DataAssetVersion.tenant_id),
        )
        .join(
            BucketFile,
            (BucketFile.id == DataAssetVersion.bucket_file_id)
            & (BucketFile.data_source_id == DataAssetVersion.bucket_data_source_id),
        )
        .join(
            DataSource,
            (DataSource.id == DataAssetVersion.bucket_data_source_id)
            & (DataSource.tenant_id == DataAssetVersion.tenant_id),
        )
        .where(
            DataAssetVersion.id.in_(sorted(version_ids)),
            DataAssetVersion.tenant_id == tenant_id,
            DataAsset.tenant_id == tenant_id,
            BucketFile.data_source_id == DataAssetVersion.bucket_data_source_id,
            DataSource.tenant_id == tenant_id,
        )
    ).all()
    by_version = {str(version.id): (version, asset, bucket, source) for version, asset, bucket, source in rows}
    if set(by_version) != version_ids:
        raise ValidationDatasetError("验证资料来源记录不存在或归属无效")
    for version in versions:
        _version, asset, bucket_file, source = by_version[str(version.id)]
        if str(version.bucket_data_source_id or "") != str(source.id):
            raise ValidationDatasetError("验证资料来源记录不存在或归属无效")
        try:
            catalog_service._require_asset_file_scope(
                db,
                asset,
                bucket_file,
                source,
                allow_shared_runtime_source=True,
            )
        except catalog_service.CatalogError as exc:
            raise ValidationDatasetError("验证资料来源作用域无效") from exc
        if agent_id is not None and source.resource_scope != "agent_runtime":
            raise ValidationDatasetError("Agent 验证资料不能来自建模数据源")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _relation_key(value: str, fallback: str) -> str:
    key = _KEY_CLEAN_RE.sub("", str(value or "")).strip()
    if not key:
        key = fallback
    if len(key) > 180:
        key = f"{key[:140]}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    return key


def _unique_name(value: Any, ordinal: int, seen: set[str]) -> str:
    base = str(value).strip() if value is not None else ""
    base = _KEY_CLEAN_RE.sub("", base)[:300] or f"column_{ordinal + 1}"
    candidate = base
    suffix = 2
    while candidate.casefold() in seen:
        candidate = f"{base}_{suffix}"
        suffix += 1
    seen.add(candidate.casefold())
    return candidate


def _logical_type(profile_type: str, name: str) -> str:
    normalized = str(profile_type or "string").strip().lower()
    folded = name.casefold()
    if normalized in {"integer", "number"} and any(
        marker in folded for marker in _IDENTIFIER_MARKERS
    ):
        return "string"
    return normalized if normalized in {
        "integer", "number", "boolean", "date", "datetime", "string"
    } else "string"


def _arrow_contract(columns: Sequence[dict[str, Any]]):
    import pyarrow as pa

    arrow_fields = []
    field_contract = []
    logical_types: list[str] = []
    physical = {
        "integer": (pa.int64(), "BIGINT"),
        "number": (pa.float64(), "DOUBLE"),
        "boolean": (pa.bool_(), "BOOLEAN"),
        "date": (pa.date32(), "DATE"),
        "datetime": (pa.timestamp("us"), "TIMESTAMP"),
        "string": (pa.string(), "VARCHAR"),
    }
    seen: set[str] = set()
    for ordinal, column in enumerate(columns):
        name = _unique_name(column.get("name"), ordinal, seen)
        logical = _logical_type(str(column.get("logical_type") or ""), name)
        arrow_type, sql_type = physical[logical]
        arrow_fields.append(pa.field(name, arrow_type, nullable=True))
        logical_types.append(logical)
        field_contract.append(
            {
                "name": name,
                "physical_type": sql_type,
                "nullable": True,
                "key_ordinal": None,
                "ordinal": ordinal,
                "logical_type": logical,
            }
        )
    if not arrow_fields:
        raise ValidationDatasetError("表格没有可物化的字段")
    return pa.schema(arrow_fields), field_contract, logical_types


def _coerce(value: Any, logical: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if logical == "string":
        return str(value)
    if logical == "integer":
        try:
            parsed = int(Decimal(str(value).strip()))
            return parsed if -(2**63) <= parsed < 2**63 else None
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            return None
    if logical == "number":
        try:
            parsed = float(Decimal(str(value).strip()))
            return parsed if math.isfinite(parsed) else None
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            return None
    if logical == "boolean":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().casefold()
        if lowered in {"true", "1", "yes", "y", "是"}:
            return True
        if lowered in {"false", "0", "no", "n", "否"}:
            return False
        return None
    if logical == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            return None
    if logical == "datetime":
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        try:
            return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return str(value)


def _write_parquet_fragments(
    output_dir: Path,
    output_stem: str,
    rows: Iterable[Sequence[Any]],
    schema: Any,
    logical_types: Sequence[str],
) -> list[dict[str, Any]]:
    import pyarrow as pa
    import pyarrow.parquet as parquet

    writer = None
    destination: Path | None = None
    fragment_rows = 0
    fragment_index = 0
    fragments: list[dict[str, Any]] = []
    batch: list[Sequence[Any]] = []

    def close_fragment() -> None:
        nonlocal writer, destination, fragment_rows
        if writer is None or destination is None:
            return
        writer.close()
        digest = _file_sha256(destination)
        fragments.append(
            {
                "path": destination,
                "row_count": fragment_rows,
                "byte_size": destination.stat().st_size,
                "content_sha256": digest,
            }
        )
        writer = None
        destination = None
        fragment_rows = 0

    def flush() -> None:
        nonlocal writer, destination, fragment_rows, fragment_index
        if not batch:
            return
        if writer is None:
            fragment_index += 1
            destination = output_dir / f"{output_stem}-{fragment_index:04d}.parquet"
            writer = parquet.ParquetWriter(destination, schema, compression="zstd")
        arrays = []
        for index, field in enumerate(schema):
            arrays.append(
                pa.array(
                    [
                        _coerce(row[index] if index < len(row) else None, logical_types[index])
                        for row in batch
                    ],
                    type=field.type,
                )
            )
        writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
        fragment_rows += len(batch)
        batch.clear()
        if destination.stat().st_size >= _PARQUET_TARGET_BYTES:
            close_fragment()

    try:
        for row in rows:
            values = tuple(row)
            if not any(value is not None and str(value).strip() for value in values):
                continue
            batch.append(values)
            if len(batch) >= _BATCH_ROWS:
                flush()
        flush()
    finally:
        close_fragment()
    return fragments


def _profile(version: DataAssetVersion) -> dict[str, Any]:
    document = version.version_document or {}
    profile = document.get("profile") if isinstance(document, dict) else None
    if not isinstance(profile, dict) or profile.get("category") != "table":
        raise ValidationDatasetError("所选资料不是可查询表格")
    return profile


def _profile_extension(profile: dict[str, Any]) -> str:
    extension = str(profile.get("extension") or "").strip().lower()
    if extension not in {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}:
        raise ValidationDatasetError("表格结构 profile 缺少受支持的内容格式")
    return extension


def _csv_rows(path: Path, delimiter: str) -> Iterator[Sequence[Any]]:
    with path.open("rb") as handle:
        sample = handle.read(65536)
    encoding = "utf-8-sig"
    for candidate in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            sample.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        next(reader, None)
        yield from reader


def _close_tabular_workbook(workbook: Any) -> None:
    if workbook is None:
        return
    close = getattr(workbook, "close", None)
    release = getattr(workbook, "release_resources", None)
    if callable(close):
        close()
    elif callable(release):
        release()


def _materialize_raw_file(
    raw_path: Path,
    filename: str,
    profile: dict[str, Any],
    output_dir: Path,
    used_relation_keys: set[str],
) -> list[dict[str, Any]]:
    extension = _profile_extension(profile)
    stem = Path(filename).stem
    tables = list(profile.get("tables") or [])
    results: list[dict[str, Any]] = []
    if extension in {".csv", ".tsv"}:
        if not tables:
            raise ValidationDatasetError("表格结构 profile 缺失")
        relation_name = str(tables[0].get("relation_name") or "").strip()
        delimiter = str(tables[0].get("delimiter") or "")
        if not delimiter:
            delimiter = "\t" if extension == ".tsv" else ","
        if delimiter not in {"\t", ",", ";", "|"}:
            raise ValidationDatasetError("表格结构 profile 的分隔符无效")
        candidates = [(
            relation_name
            or catalog_ingestion_service.runtime_relation_name(filename, "data", 1),
            tables[0],
            _csv_rows(raw_path, delimiter),
        )]
        workbook = None
    elif extension in {".xlsx", ".xlsm"}:
        requests: list[tabular_materialization_service.ExcelSheetRequest] = []
        planned: list[tuple[str, list[dict[str, Any]]]] = []
        planned_relation_keys = set(used_relation_keys)
        for ordinal, table_profile in enumerate(tables):
            sheet_name = str(table_profile.get("name") or "").strip()
            if not sheet_name:
                raise ValidationDatasetError("Excel 工作表 profile 缺少名称")
            raw_relation_name = str(table_profile.get("relation_name") or "").strip()
            if not raw_relation_name:
                raw_relation_name = catalog_ingestion_service.runtime_relation_name(
                    filename, sheet_name, len(tables)
                )
            relation_name = _relation_key(raw_relation_name, f"table_{ordinal + 1}")
            if relation_name.casefold() in planned_relation_keys:
                relation_name = _relation_key(
                    f"{stem}__{relation_name}",
                    f"table_{len(planned_relation_keys) + 1}",
                )
            if relation_name.casefold() in planned_relation_keys:
                raise ValidationDatasetError(
                    f"验证数据包包含重复关系名: {relation_name}"
                )
            _schema, field_contract, logical_types = _arrow_contract(
                list(table_profile.get("columns") or [])
            )
            requests.append(
                tabular_materialization_service.ExcelSheetRequest(
                    name=sheet_name,
                    header_row_index=int(table_profile.get("header_row_index") or 0),
                    fields=tuple(
                        tabular_materialization_service.ExcelField(
                            name=str(field["name"]),
                            logical_type=logical_types[index],
                        )
                        for index, field in enumerate(field_contract)
                    ),
                    output_stem=f"{len(used_relation_keys) + ordinal + 1:04d}-0001",
                )
            )
            planned.append((relation_name, field_contract))
            planned_relation_keys.add(relation_name.casefold())
        try:
            fragments, engine = (
                tabular_materialization_service.materialize_excel_workbook(
                    raw_path, output_dir, requests
                )
            )
        except tabular_materialization_service.TabularMaterializationError as exc:
            raise ValidationDatasetError(str(exc)) from exc
        for (relation_name, field_contract), fragment in zip(
            planned, fragments, strict=True
        ):
            if fragment is None:
                continue
            used_relation_keys.add(relation_name.casefold())
            results.append(
                {
                    "relation_key": relation_name,
                    "display_name": relation_name,
                    "fields": field_contract,
                    "fragments": [fragment],
                    "row_count": int(fragment["row_count"]),
                    "byte_size": int(fragment["byte_size"]),
                    "materialization": engine,
                }
            )
        if not results:
            raise NoMaterializableTableError(f"{filename} 没有可物化的数据行")
        return results
    elif extension == ".xls":
        import xlrd

        workbook = xlrd.open_workbook(str(raw_path), on_demand=True)
        try:
            profile_by_name = {str(item.get("name") or ""): item for item in tables}
            candidates = []
            nonempty_sheets = [
                workbook.sheet_by_index(index)
                for index in range(workbook.nsheets)
                if workbook.sheet_by_index(index).nrows > 0
            ]
            for sheet in nonempty_sheets:
                table_profile = profile_by_name.get(sheet.name)
                if table_profile is None:
                    continue
                start = int(table_profile.get("header_row_index") or 0) + 1
                rows = (sheet.row_values(index) for index in range(start, sheet.nrows))
                relation_name = str(table_profile.get("relation_name") or "").strip()
                if not relation_name:
                    relation_name = catalog_ingestion_service.runtime_relation_name(
                        filename, sheet.name, len(nonempty_sheets)
                    )
                candidates.append((relation_name, table_profile, rows))
        except Exception:
            _close_tabular_workbook(workbook)
            raise
    else:
        raise ValidationDatasetError("验证数据包目前只接受 CSV、TSV、XLS 或 XLSX")
    try:
        for ordinal, (raw_relation_name, table_profile, rows) in enumerate(candidates):
            relation_name = _relation_key(raw_relation_name, f"table_{ordinal + 1}")
            if relation_name.casefold() in used_relation_keys:
                relation_name = _relation_key(
                    f"{stem}__{relation_name}", f"table_{len(used_relation_keys) + 1}"
                )
            if relation_name.casefold() in used_relation_keys:
                raise ValidationDatasetError(f"验证数据包包含重复关系名: {relation_name}")
            arrow_schema, field_contract, logical_types = _arrow_contract(
                list(table_profile.get("columns") or [])
            )
            fragments = _write_parquet_fragments(
                output_dir,
                f"{len(used_relation_keys) + 1:04d}",
                rows,
                arrow_schema,
                logical_types,
            )
            if not fragments:
                continue
            used_relation_keys.add(relation_name.casefold())
            results.append(
                {
                    "relation_key": relation_name,
                    "display_name": relation_name,
                    "fields": field_contract,
                    "fragments": fragments,
                    "row_count": sum(item["row_count"] for item in fragments),
                    "byte_size": sum(item["byte_size"] for item in fragments),
                }
            )
    finally:
        _close_tabular_workbook(workbook)
    if not results:
        raise NoMaterializableTableError(f"{filename} 没有可物化的数据行")
    return results


def _result(dataset: LogicalDataset, version: DatasetVersion, schema: DatasetSchema, *, reused: bool) -> dict[str, Any]:
    relations = list(
        version.manifest.get("relations", {}).keys()
        if isinstance(version.manifest, dict)
        else []
    )
    return {
        "dataset_id": dataset.id,
        "dataset_version_id": version.id,
        "content_hash": version.content_hash,
        "schema_hash": schema.schema_hash,
        "record_count": version.record_count,
        "byte_size": version.byte_size,
        "relation_names": relations,
        "source_asset_version_ids": [],
        "reused": reused,
    }


def _input_identity(
    versions: Sequence[DataAssetVersion], *, agent_id: str | None = None
) -> tuple[dict[str, Any], str, str]:
    identity = {
        "format": "validation-dataset-input/v2",
        "agent_id": agent_id,
        "assets": [
            {"id": item.id, "sha256": item.content_sha256}
            for item in versions
        ],
    }
    identity_hash = _canonical_hash(identity)
    return identity, identity_hash, f"validation.bundle.{identity_hash[:32]}"


def _assert_validation_job_lease_for_publication(
    db: Session,
    *,
    job_id: str,
    lease_token: str,
    expected_dataset_id: str | None = None,
) -> None:
    """Fence a worker immediately before publishing generated metadata.

    The heartbeat is intentionally allowed to run during downloads and CPU
    materialization.  This short row lock is the only lease check needed at
    the publication boundary; a cancelled or reclaimed run cannot publish a
    result with the previous token.
    """

    if not job_id or not lease_token:
        raise ValidationDatasetError("验证数据集任务租约上下文缺失")
    run = db.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == job_id,
            IngestionRun.tenant_id == tenant_service.current_tenant_id(db),
            IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
            IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    expires_at = _as_utc(run.lease_expires_at) if run is not None else None
    now = datetime.now(timezone.utc)
    if (
        run is None
        or run.status != "running"
        or run.lease_token != lease_token
        or expires_at is None
        or expires_at <= now
        or (expected_dataset_id is not None and str(run.dataset_id) != str(expected_dataset_id))
    ):
        raise ValidationDatasetError("验证数据集任务租约已失效")


def _lock_validation_publication_scope(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str | None,
    dataset_key: str,
    expected_inputs: dict[str, tuple[str, str, str]],
    job_id: str | None = None,
    lease_token: str | None = None,
) -> LogicalDataset | None:
    """Serialize Agent deletion and recheck every input before publication.

    The lock order mirrors ``agent_deletion_service``: Agent, validation
    dataset, worker lease, then owned assets/versions.  A builder that wins
    the Agent lock commits before deletion can inspect its rows; a builder
    that observes a deleted/revoked owner fails and lets the upload-intent
    cleanup remove any generated objects.
    """

    if agent_id:
        permission_service.refresh_request_authorization(db)
        locked_agent = db.scalar(
            select(Agent)
            .where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if locked_agent is None:
            raise ValidationDatasetError("Agent 已删除，验证数据集生成已取消")
        _validate_agent_scope(
            db,
            locked_agent,
            permission_verb="write",
            active_runtime=True,
        )

    dataset = db.scalar(
        select(LogicalDataset)
        .where(
            LogicalDataset.tenant_id == tenant_id,
            LogicalDataset.key == dataset_key,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if dataset is not None:
        labels = dataset.labels if isinstance(dataset.labels, dict) else {}
        owner = str(labels.get("owner_agent_id") or "") or None
        if (
            labels.get("catalog_purpose") != "validation_dataset"
            or owner != agent_id
        ):
            raise ValidationDatasetError("验证数据集归属校验失败")
        if dataset.lifecycle_status != "active":
            raise ValidationDatasetError("验证数据集已被删除，不能继续生成")
        _assert_validation_dataset_versions_reusable(db, dataset, lock=True)

    if job_id is not None or lease_token is not None:
        _assert_validation_job_lease_for_publication(
            db,
            job_id=str(job_id or ""),
            lease_token=str(lease_token or ""),
            expected_dataset_id=str(dataset.id) if dataset is not None else None,
        )

    expected_ids = set(expected_inputs)
    if not expected_ids:
        raise ValidationDatasetError("未选择验证资料")

    asset_ids = set(
        str(value)
        for value in db.scalars(
            select(DataAssetVersion.asset_id)
            .where(
                DataAssetVersion.id.in_(sorted(expected_ids)),
                DataAssetVersion.tenant_id == tenant_id,
            )
        ).all()
    )
    if not asset_ids:
        raise ValidationDatasetError("验证资料已被删除，不能继续生成")

    assets = list(
        db.scalars(
            select(DataAsset)
            .where(
                DataAsset.id.in_(sorted(asset_ids)),
                DataAsset.tenant_id == tenant_id,
                DataAsset.lifecycle_status == "active",
                DataAsset.usage_plane == "invocation_input",
                DataAsset.owner_agent_id == agent_id
                if agent_id
                else DataAsset.owner_agent_id.is_(None),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    if {str(item.id) for item in assets} != asset_ids:
        raise ValidationDatasetError("验证资料已被删除或 Agent 已删除")

    current_versions = list(
        db.scalars(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.id.in_(sorted(expected_ids)),
                DataAssetVersion.tenant_id == tenant_id,
                DataAssetVersion.status == "ready",
                DataAssetVersion.bucket_file_id.is_not(None),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    if {str(item.id) for item in current_versions} != expected_ids:
        raise ValidationDatasetError("验证资料版本已被删除或尚未就绪")
    for version in current_versions:
        expected = expected_inputs[str(version.id)]
        current = (
            str(version.asset_id),
            str(version.bucket_file_id or ""),
            str(version.content_sha256 or ""),
        )
        if current != expected:
            raise ValidationDatasetError("验证资料版本在生成期间发生变化，请重试")
    _validate_input_source_lineage(
        db,
        current_versions,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    return dataset


def _assert_validation_dataset_row(
    dataset: LogicalDataset,
    *,
    agent_id: str | None,
) -> None:
    """Reject a stale, reused, or cross-owner validation package row.

    The deterministic key is not an ownership proof: legacy rows, a retired
    Agent tombstone, or a malformed imported row may still carry the same key.
    Enqueue must fail closed before it creates/replays an ``IngestionRun``.
    """

    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    owner = str(labels.get("owner_agent_id") or "") or None
    if (
        dataset.usage_plane != "invocation_input"
        or labels.get("catalog_purpose") != "validation_dataset"
        or owner != agent_id
    ):
        raise ValidationDatasetError("验证数据集归属校验失败")
    if dataset.lifecycle_status != "active":
        raise ValidationDatasetError("验证数据集已被删除，不能重新排队")


def _assert_validation_dataset_versions_reusable(
    db: Session,
    dataset: LogicalDataset,
    *,
    lock: bool = False,
) -> None:
    """A tombstoned immutable package version must never be resurrected."""

    statement = select(DatasetVersion.id).where(
        DatasetVersion.tenant_id == dataset.tenant_id,
        DatasetVersion.dataset_id == dataset.id,
        DatasetVersion.status == "retired",
    )
    if lock:
        statement = statement.with_for_update()
    if db.scalar(statement.limit(1)) is not None:
        raise ValidationDatasetError("验证数据集版本已被删除，不能继续使用")


def _require_validation_job_dataset(
    db: Session,
    run: IngestionRun,
    *,
    agent_id: str | None,
) -> LogicalDataset:
    """Revalidate package ownership and lifecycle at every job boundary."""

    dataset = db.scalar(
        select(LogicalDataset)
        .where(
            LogicalDataset.id == run.dataset_id,
            LogicalDataset.tenant_id == run.tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if dataset is None:
        raise ValidationDatasetError("验证数据集任务不存在")
    if (
        run.pipeline_kind != _VALIDATION_PIPELINE_KIND
        or run.pipeline_version != _VALIDATION_PIPELINE_VERSION
    ):
        raise ValidationDatasetError("验证数据集任务版本已过期")
    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    owner = str(labels.get("owner_agent_id") or "") or None
    if owner != agent_id:
        raise ValidationDatasetError("验证数据集任务不属于当前 Agent")
    _assert_validation_dataset_row(dataset, agent_id=agent_id)
    _assert_validation_dataset_versions_reusable(db, dataset)
    if run.output_version_id:
        output_version = db.scalar(
            select(DatasetVersion.id).where(
                DatasetVersion.id == run.output_version_id,
                DatasetVersion.tenant_id == run.tenant_id,
                DatasetVersion.dataset_id == dataset.id,
                DatasetVersion.status == "ready",
            )
        )
        if output_version is None:
            raise ValidationDatasetError("验证数据集任务输出版本已被删除")
    elif run.status == "succeeded":
        raise ValidationDatasetError("验证数据集任务输出版本不存在")
    return dataset


def _job_document(db: Session, run: IngestionRun) -> dict[str, Any]:
    dataset = db.get(LogicalDataset, run.dataset_id)
    if dataset is None:
        raise ValidationDatasetError("验证数据集任务不存在")
    version = db.get(DatasetVersion, run.output_version_id) if run.output_version_id else None
    status = "queued" if run.status == "pending" else run.status
    result = None
    if version is not None:
        schema = db.get(DatasetSchema, version.schema_id)
        if schema is not None:
            result = _result(dataset, version, schema, reused=True)
            result["source_asset_version_ids"] = list(
                db.scalars(
                    select(IngestionRunInput.asset_version_id)
                    .where(
                        IngestionRunInput.ingestion_run_id == run.id,
                        IngestionRunInput.asset_version_id.is_not(None),
                    )
                    .order_by(IngestionRunInput.ordinal)
                ).all()
            )
    return {
        "id": run.id,
        "status": status,
        "error": str(run.error or ""),
        "created_at": run.created_at,
        "updated_at": run.finished_at or run.started_at or run.created_at,
        "result": result,
    }


def enqueue_validation_dataset_job(
    db: Session,
    payload: ValidationDatasetBuildIn,
) -> dict[str, Any]:
    tenant_id = tenant_service.current_tenant_id(db)
    # Hold the owner before reading source versions or the deterministic
    # dataset row.  Agent deletion follows the same Agent -> dataset order;
    # one of the two transactions therefore wins atomically and the loser
    # observes either a live owner or a stable "Agent 不存在" error.
    agent_id = _resolve_agent_scope(
        db,
        payload.agent_id,
        permission_verb="write",
        lock=True,
        active_runtime=True,
    )
    versions = list(
        db.scalars(
            select(DataAssetVersion)
            .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
            .where(
                DataAssetVersion.id.in_(payload.asset_version_ids),
                DataAssetVersion.tenant_id == tenant_id,
                DataAssetVersion.status == "ready",
                DataAssetVersion.bucket_file_id.is_not(None),
                DataAsset.tenant_id == tenant_id,
                DataAsset.lifecycle_status == "active",
                DataAsset.usage_plane == "invocation_input",
                DataAsset.owner_agent_id == agent_id
                if agent_id
                else DataAsset.owner_agent_id.is_(None),
            )
        ).all()
    )
    by_id = {item.id: item for item in versions}
    if len(by_id) != len(payload.asset_version_ids):
        raise ValidationDatasetError("部分验证资料不存在、已删除或尚未就绪")
    ordered = [by_id[item] for item in payload.asset_version_ids]
    _validate_input_source_lineage(
        db,
        ordered,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    _identity, identity_hash, dataset_key = _input_identity(ordered, agent_id=agent_id)
    dataset = db.scalar(
        select(LogicalDataset)
        .where(
            LogicalDataset.tenant_id == tenant_id,
            LogicalDataset.key == dataset_key,
        )
        .with_for_update()
    )
    if dataset is None:
        dataset = LogicalDataset(
            tenant_id=tenant_id,
            key=dataset_key,
            name=payload.name.strip(),
            description="验证中心按内容哈希生成的可复用数据包",
            lifecycle_status="active",
            usage_plane="invocation_input",
            labels={
                "catalog_purpose": "validation_dataset",
                "input_hash": identity_hash,
                "source_asset_version_ids": list(payload.asset_version_ids),
                "owner_agent_id": agent_id,
            },
            created_by_user_id=str(db.info.get("user_id") or "") or None,
        )
        db.add(dataset)
        db.flush()
    else:
        _assert_validation_dataset_row(dataset, agent_id=agent_id)
        _assert_validation_dataset_versions_reusable(db, dataset, lock=True)
    ready_version = db.scalar(
        select(DatasetVersion)
        .where(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.status == "ready",
        )
        .order_by(DatasetVersion.version_number.desc())
    )
    idempotency_key = f"validation-dataset:{identity_hash}"
    run = db.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.tenant_id == tenant_id,
            IngestionRun.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if run is None:
        now = datetime.now(timezone.utc)
        run = IngestionRun(
            tenant_id=tenant_id,
            dataset_id=dataset.id,
            output_version_id=ready_version.id if ready_version is not None else None,
            pipeline_kind=_VALIDATION_PIPELINE_KIND,
            pipeline_version=_VALIDATION_PIPELINE_VERSION,
            idempotency_key=idempotency_key,
            status="succeeded" if ready_version is not None else "pending",
            requested_by_user_id=str(db.info.get("user_id") or "") or None,
            checkpoint={"input_hash": identity_hash, "lease_attempt": 0},
            finished_at=now if ready_version is not None else None,
        )
        db.add(run)
        db.flush()
        for ordinal, version in enumerate(ordered):
            db.add(
                IngestionRunInput(
                    tenant_id=tenant_id,
                    ingestion_run_id=run.id,
                    ordinal=ordinal,
                    role="source",
                    asset_version_id=version.id,
                    content_hash=version.content_sha256,
                    input_document={},
                )
            )
    elif (
        str(run.dataset_id) != str(dataset.id)
        or run.pipeline_kind != _VALIDATION_PIPELINE_KIND
        or run.pipeline_version != _VALIDATION_PIPELINE_VERSION
    ):
        raise ValidationDatasetError("旧验证数据集任务不可复用")
    elif ready_version is not None:
        run.status = "succeeded"
        run.output_version_id = ready_version.id
        run.error = ""
        run.lease_token = ""
        run.lease_expires_at = None
        run.finished_at = run.finished_at or datetime.now(timezone.utc)
    elif run.status in {"failed", "cancelled"}:
        run.status = "pending"
        run.error = ""
        run.lease_token = ""
        run.lease_expires_at = None
        run.finished_at = None
    db.commit()
    db.refresh(run)
    return _job_document(db, run)


def get_validation_dataset_job(
    db: Session, job_id: str, *, agent_id: str | None = None
) -> dict[str, Any]:
    resolved_agent_id = _resolve_agent_scope(
        db,
        agent_id,
        permission_verb="read",
    )
    run = db.scalar(
        select(IngestionRun).where(
            IngestionRun.id == job_id,
            IngestionRun.tenant_id == tenant_service.current_tenant_id(db),
            IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
            IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
        )
    )
    if run is None:
        raise ValidationDatasetError("验证数据集任务不存在")
    _require_validation_job_dataset(db, run, agent_id=resolved_agent_id)
    return _job_document(db, run)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _claim_validation_dataset_job(db: Session, job_id: str) -> tuple[IngestionRun, str] | None:
    run = db.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == job_id,
            IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
            IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
        )
        .with_for_update()
    )
    if run is None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = _as_utc(run.lease_expires_at)
    if run.status != "pending" and not (
        run.status == "running" and expires_at is not None and expires_at <= now
    ):
        return None
    token = uuid.uuid4().hex
    checkpoint = dict(run.checkpoint or {})
    checkpoint["lease_attempt"] = int(checkpoint.get("lease_attempt") or 0) + 1
    run.status = "running"
    run.started_at = run.started_at or now
    run.finished_at = None
    run.error = ""
    run.lease_token = token
    run.lease_expires_at = now + timedelta(seconds=_VALIDATION_JOB_LEASE_SECONDS)
    run.checkpoint = checkpoint
    db.commit()
    return run, token


def _renew_validation_dataset_job(job_id: str, token: str) -> bool:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        result = db.execute(
            update(IngestionRun)
            .where(
                IngestionRun.id == job_id,
                IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
                IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
                IngestionRun.status == "running",
                IngestionRun.lease_token == token,
                IngestionRun.lease_expires_at.is_not(None),
                IngestionRun.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=_VALIDATION_JOB_LEASE_SECONDS)
            )
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


@contextmanager
def _validation_job_heartbeat(job_id: str, token: str) -> Iterator[None]:
    stopped = threading.Event()
    lease_lost = threading.Event()

    def heartbeat() -> None:
        interval = max(1.0, _VALIDATION_JOB_LEASE_SECONDS / 3)
        while not stopped.wait(interval):
            try:
                if not _renew_validation_dataset_job(job_id, token):
                    lease_lost.set()
                    return
            except Exception:  # noqa: BLE001 - the fenced completion remains authoritative.
                lease_lost.set()
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"validation-job-heartbeat-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
        if lease_lost.is_set():
            raise ValidationDatasetError("验证数据集任务租约已失效")
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def process_validation_dataset_job(job_id: str) -> bool:
    """Claim and execute one durable dataset job in a worker-owned session."""
    db = SessionLocal()
    token = ""
    try:
        claim = _claim_validation_dataset_job(db, job_id)
        if claim is None:
            return False
        run, token = claim
        tenant_id = str(run.tenant_id)
        user_id = str(run.requested_by_user_id or "")
        db.info["tenant_id"] = tenant_id
        db.info["user_id"] = user_id
        owner = None
        dataset = db.scalar(
            select(LogicalDataset).where(
                LogicalDataset.id == run.dataset_id,
                LogicalDataset.tenant_id == tenant_id,
            )
        )
        if dataset is not None and isinstance(dataset.labels, dict):
            owner = str(dataset.labels.get("owner_agent_id") or "") or None
        dataset = _require_validation_job_dataset(db, run, agent_id=owner)
        asset_version_ids = list(
            db.scalars(
                select(IngestionRunInput.asset_version_id)
                .where(
                    IngestionRunInput.ingestion_run_id == run.id,
                    IngestionRunInput.asset_version_id.is_not(None),
                )
                .order_by(IngestionRunInput.ordinal)
            ).all()
        )
        payload = ValidationDatasetBuildIn(
            asset_version_ids=asset_version_ids,
            name=dataset.name,
            agent_id=owner,
        )
        with _validation_job_heartbeat(job_id, token):
            result = build_validation_dataset(
                db,
                payload,
                _publication_job_id=job_id,
                _publication_lease_token=token,
            )
        completed = db.scalar(
            select(IngestionRun)
            .where(
                IngestionRun.id == job_id,
                IngestionRun.status == "running",
                IngestionRun.lease_token == token,
            )
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if (
            completed is None
            or _as_utc(completed.lease_expires_at) is None
            or _as_utc(completed.lease_expires_at) <= now
        ):
            raise ValidationDatasetError("验证数据集任务租约已失效")
        completed.status = "succeeded"
        completed.output_version_id = str(result["dataset_version_id"])
        completed.records_read = int(result["record_count"])
        completed.records_written = int(result["record_count"])
        completed.bytes_written = int(result["byte_size"])
        completed.error = ""
        completed.lease_token = ""
        completed.lease_expires_at = None
        completed.finished_at = now
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - worker exposes only a stable error.
        db.rollback()
        failed = db.scalar(
            select(IngestionRun)
            .where(
                IngestionRun.id == job_id,
                IngestionRun.status == "running",
                IngestionRun.lease_token == token,
            )
            .with_for_update()
        ) if token else None
        if failed is not None:
            failed.status = "failed"
            failed.finished_at = datetime.now(timezone.utc)
            failed.error = (
                str(exc)[:1000]
                if isinstance(exc, ValidationDatasetError)
                else "验证数据集生成失败"
            )
            failed.lease_token = ""
            failed.lease_expires_at = None
            db.commit()
        return False
    finally:
        db.close()


def process_next_validation_dataset_job() -> bool:
    db = SessionLocal()
    try:
        candidates = list(
            db.scalars(
                select(IngestionRun)
                .where(
                    IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
                    IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
                    IngestionRun.status == "pending",
                )
                .order_by(IngestionRun.created_at, IngestionRun.id)
                .limit(1)
            ).all()
        )
        job_id = next(
            (
                item.id
                for item in candidates
                if item.pipeline_kind == _VALIDATION_PIPELINE_KIND
                and item.status == "pending"
            ),
            None,
        )
    finally:
        db.close()
    return process_validation_dataset_job(job_id) if job_id else False


def recover_validation_dataset_jobs() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        now = datetime.now(timezone.utc)
        for run in db.scalars(
            select(IngestionRun).where(
                IngestionRun.pipeline_kind == _VALIDATION_PIPELINE_KIND,
                IngestionRun.pipeline_version == _VALIDATION_PIPELINE_VERSION,
                IngestionRun.status == "running",
            )
        ):
            expires_at = _as_utc(run.lease_expires_at)
            if expires_at is not None and expires_at <= now:
                run.status = "pending"
                run.error = "执行租约过期后自动恢复"
                run.lease_token = ""
                run.lease_expires_at = None
                recovered += 1
        db.commit()
        return recovered
    finally:
        db.close()


def build_validation_dataset(
    db: Session,
    payload: ValidationDatasetBuildIn,
    *,
    _publication_job_id: str | None = None,
    _publication_lease_token: str | None = None,
) -> dict[str, Any]:
    tenant_id = tenant_service.current_tenant_id(db)
    agent_id = _resolve_agent_scope(
        db,
        payload.agent_id,
        permission_verb="write",
        active_runtime=True,
    )
    versions = list(
        db.scalars(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.id.in_(payload.asset_version_ids),
                DataAssetVersion.tenant_id == tenant_id,
                DataAssetVersion.status == "ready",
                DataAssetVersion.bucket_file_id.is_not(None),
            )
        ).all()
    )
    by_id = {item.id: item for item in versions}
    if len(by_id) != len(payload.asset_version_ids):
        raise ValidationDatasetError("部分验证资料不存在、已删除或尚未就绪")
    versions = [by_id[item] for item in payload.asset_version_ids]
    expected_inputs = {
        str(item.id): (
            str(item.asset_id),
            str(item.bucket_file_id or ""),
            str(item.content_sha256 or ""),
        )
        for item in versions
    }
    assets = {
        item.id: item
        for item in db.scalars(
            select(DataAsset).where(
                DataAsset.id.in_([version.asset_id for version in versions]),
                DataAsset.tenant_id == tenant_id,
                DataAsset.lifecycle_status == "active",
                DataAsset.usage_plane == "invocation_input",
                DataAsset.owner_agent_id == agent_id
                if agent_id
                else DataAsset.owner_agent_id.is_(None),
            )
        ).all()
    }
    if len(assets) != len({item.asset_id for item in versions}):
        raise ValidationDatasetError("部分验证资料已删除")
    _validate_input_source_lineage(
        db,
        versions,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    for version in versions:
        _profile(version)

    identity, identity_hash, dataset_key = _input_identity(versions, agent_id=agent_id)
    existing_dataset = db.scalar(
        select(LogicalDataset).where(
            LogicalDataset.tenant_id == tenant_id,
            LogicalDataset.key == dataset_key,
        )
    )
    if existing_dataset is not None:
        _assert_validation_dataset_row(existing_dataset, agent_id=agent_id)
        _assert_validation_dataset_versions_reusable(db, existing_dataset)
        existing_version = db.scalar(
            select(DatasetVersion)
            .where(
                DatasetVersion.dataset_id == existing_dataset.id,
                DatasetVersion.status == "ready",
            )
            .order_by(DatasetVersion.version_number.desc())
        )
        if existing_version is not None:
            existing_schema = db.get(DatasetSchema, existing_version.schema_id)
            if existing_schema is not None:
                locked_dataset = _lock_validation_publication_scope(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    dataset_key=dataset_key,
                    expected_inputs=expected_inputs,
                    job_id=_publication_job_id,
                    lease_token=_publication_lease_token,
                )
                if locked_dataset is None:
                    raise ValidationDatasetError("验证数据集不存在，不能继续生成")
                existing_version = db.scalar(
                    select(DatasetVersion)
                    .where(
                        DatasetVersion.dataset_id == locked_dataset.id,
                        DatasetVersion.status == "ready",
                    )
                    .order_by(DatasetVersion.version_number.desc())
                )
                existing_schema = (
                    db.get(DatasetSchema, existing_version.schema_id)
                    if existing_version is not None
                    else None
                )
                if existing_version is None or existing_schema is None:
                    raise ValidationDatasetError("验证数据集版本尚未就绪，请重试")
                result = _result(locked_dataset, existing_version, existing_schema, reused=True)
                result["source_asset_version_ids"] = list(payload.asset_version_ids)
                return result

    bucket_file_ids = [str(version.bucket_file_id) for version in versions]
    bucket_files = {
        item.id: item
        for item in db.scalars(
            select(BucketFile).where(BucketFile.id.in_(bucket_file_ids))
        ).all()
    }
    if len(bucket_files) != len(set(bucket_file_ids)):
        raise ValidationDatasetError("验证资料的 MinIO 对象记录不存在")
    source = catalog_ingestion_service.require_external_upload_bucket(
        db,
        owner_agent_id=agent_id,
    )
    # The bucket resolver takes a row lock while it validates or repairs the
    # tenant-owned internal source.  Parquet materialization can take minutes,
    # so release that catalog transaction before any object download or CPU
    # work.  The immutable asset/version identities are rechecked above and
    # SessionLocal uses expire_on_commit=False.
    db.commit()
    max_bytes = int(get_settings().catalog_max_upload_bytes)
    upload_records: list[tuple[Any, BucketFile]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ontology-validation-") as raw_dir:
            work = Path(raw_dir).resolve()
            materialized: list[dict[str, Any]] = []
            skipped_sources: list[dict[str, str]] = []
            used_relation_keys: set[str] = set()
            for index, version in enumerate(versions):
                bucket_file = bucket_files[str(version.bucket_file_id)]
                profile = _profile(version)
                extension = _profile_extension(profile)
                raw_path = work / f"raw-{index:03d}{extension}"
                object_storage_service.download_object_to_file(
                    bucket_file.bucket_name,
                    bucket_file.object_key,
                    raw_path,
                    version_id=bucket_file.object_version_id,
                    max_bytes=max_bytes,
                )
                if _file_sha256(raw_path) != version.content_sha256:
                    raise ValidationDatasetError("验证资料完整性校验失败")
                try:
                    relations = _materialize_raw_file(
                        raw_path,
                        assets[version.asset_id].name,
                        profile,
                        work,
                        used_relation_keys,
                    )
                except NoMaterializableTableError as exc:
                    skipped_sources.append(
                        {
                            "asset_version_id": version.id,
                            "name": assets[version.asset_id].name,
                            "reason": str(exc),
                        }
                    )
                else:
                    materialized.extend(relations)

            if not materialized:
                raise ValidationDatasetError("所选验证资料没有可物化的表格数据")

            schema_relations = {
                item["relation_key"]: [
                    {
                        key: field[key]
                        for key in ("name", "physical_type", "nullable", "key_ordinal", "ordinal")
                    }
                    for field in item["fields"]
                ]
                for item in materialized
            }
            schema_document = {"relations": schema_relations, "derived_relations": {}}
            schema_hash = _canonical_hash(schema_document)
            manifest_relations = {
                item["relation_key"]: {
                    "schema_hash": _canonical_hash(schema_relations[item["relation_key"]]),
                    "row_count": item["row_count"],
                    "byte_size": item["byte_size"],
                    **(
                        {"content_sha256": item["fragments"][0]["content_sha256"]}
                        if len(item["fragments"]) == 1
                        else {}
                    ),
                    **(
                        {"materialization": item["materialization"]}
                        if item.get("materialization")
                        else {}
                    ),
                }
                for item in materialized
            }
            manifest = {
                "format": "validation-dataset/v1",
                "input": identity,
                "relations": manifest_relations,
                "derived_relations": {},
                "skipped_sources": skipped_sources,
            }
            version_hash = _canonical_hash(
                {"dataset_key": dataset_key, "schema_hash": schema_hash, "manifest": manifest}
            )

            # Upload generated fragments while no Agent/dataset row lock is
            # held.  BucketFile metadata is retained in its own short
            # transaction; the logical dataset graph is published only after
            # the ordered Agent -> dataset -> lease -> source locks below.
            staged_fragments: list[tuple[Any, BucketFile, dict[str, Any], int]] = []
            output_ordinal = 0
            for item in materialized:
                for fragment_ordinal, fragment in enumerate(item["fragments"]):
                    output_ordinal += 1
                    file_id = uuid.uuid4().hex
                    filename = f"fragment-{output_ordinal:04d}.parquet"
                    claim = object_deletion_service.prepare_bucket_file_upload(
                        source, file_id, filename
                    )
                    with object_deletion_service.heartbeat_upload_intent(claim) as heartbeat:
                        object_deletion_service.begin_upload_put(claim)
                        parquet_file = datasource_service.save_bucket_file_path(
                            source,
                            filename,
                            fragment["path"],
                            mime="application/vnd.apache.parquet",
                            stable_file_id=file_id,
                            upload_object_key=claim.object_key,
                            content_sha256=fragment["content_sha256"],
                        )
                        object_deletion_service.assert_upload_active(
                            heartbeat, claim, parquet_file
                        )
                    parquet_file.status = "parsed"
                    parquet_file.index_status = "not_applicable"
                    db.add(parquet_file)
                    object_deletion_service.retain_bucket_file_upload(
                        db, claim, parquet_file, source
                    )
                    db.flush()
                    db.commit()
                    upload_records.append((claim, parquet_file))
                    staged_fragments.append(
                        (claim, parquet_file, item, fragment_ordinal)
                    )

            # The final publication transaction starts with the same lock
            # order as Agent deletion.  No child FK row has been inserted
            # before this point, so PostgreSQL cannot deadlock on a dataset
            # key-share lock while waiting for the Agent row.
            publication_dataset = _lock_validation_publication_scope(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                dataset_key=dataset_key,
                expected_inputs=expected_inputs,
                job_id=_publication_job_id,
                lease_token=_publication_lease_token,
            )
            if publication_dataset is None:
                publication_dataset = LogicalDataset(
                    tenant_id=tenant_id,
                    key=dataset_key,
                    name=payload.name.strip(),
                    description="验证中心按内容哈希生成的可复用数据包",
                    lifecycle_status="active",
                    usage_plane="invocation_input",
                    labels={
                        "catalog_purpose": "validation_dataset",
                        "input_hash": identity_hash,
                        "owner_agent_id": agent_id,
                    },
                    created_by_user_id=str(db.info.get("user_id") or "") or None,
                )
                db.add(publication_dataset)
                db.flush()
            else:
                # Another builder may have published while this worker was
                # downloading/materializing. Reuse its immutable result and
                # discard only the exact fragments staged by this attempt.
                ready_after_lock = db.scalar(
                    select(DatasetVersion)
                    .where(
                        DatasetVersion.dataset_id == publication_dataset.id,
                        DatasetVersion.status == "ready",
                    )
                    .order_by(DatasetVersion.version_number.desc())
                )
                if ready_after_lock is not None:
                    ready_schema = db.get(DatasetSchema, ready_after_lock.schema_id)
                    if ready_schema is not None:
                        reused_result = _result(
                            publication_dataset,
                            ready_after_lock,
                            ready_schema,
                            reused=True,
                        )
                        reused_result["source_asset_version_ids"] = list(
                            payload.asset_version_ids
                        )
                        db.rollback()
                        for claim, bucket_file in upload_records:
                            object_deletion_service.schedule_abandoned_upload_best_effort(
                                claim, bucket_file
                            )
                        return reused_result

            schema = DatasetSchema(
                tenant_id=tenant_id,
                dataset_id=publication_dataset.id,
                schema_version=int(
                    db.scalar(
                        select(func.coalesce(func.max(DatasetSchema.schema_version), 0)).where(
                            DatasetSchema.dataset_id == publication_dataset.id
                        )
                    )
                    or 0
                ) + 1,
                schema_hash=schema_hash,
                compatibility="none",
                schema_document=schema_document,
                created_by_user_id=str(db.info.get("user_id") or "") or None,
            )
            db.add(schema)
            db.flush()
            relation_by_key: dict[str, DatasetRelation] = {}
            for ordinal, item in enumerate(materialized):
                relation = DatasetRelation(
                    tenant_id=tenant_id,
                    dataset_id=publication_dataset.id,
                    schema_id=schema.id,
                    relation_key=item["relation_key"],
                    display_name=item["display_name"],
                    kind="table",
                    ordinal=ordinal,
                    description="",
                )
                db.add(relation)
                db.flush()
                relation_by_key[item["relation_key"]] = relation
                for field in item["fields"]:
                    db.add(
                        DatasetField(
                            tenant_id=tenant_id,
                            dataset_id=publication_dataset.id,
                            schema_id=schema.id,
                            dataset_relation_id=relation.id,
                            field_key=f"field_{int(field['ordinal']) + 1}",
                            source_name=field["name"],
                            logical_type=field["logical_type"],
                            physical_type=field["physical_type"],
                            nullable=True,
                            ordinal=field["ordinal"],
                            key_ordinal=None,
                            semantic_role="",
                            field_document={},
                        )
                    )
            db.flush()

            version = DatasetVersion(
                tenant_id=tenant_id,
                dataset_id=publication_dataset.id,
                schema_id=schema.id,
                version_number=int(
                    db.scalar(
                        select(func.coalesce(func.max(DatasetVersion.version_number), 0)).where(
                            DatasetVersion.dataset_id == publication_dataset.id
                        )
                    )
                    or 0
                ) + 1,
                status="ready",
                record_count=sum(int(item["row_count"]) for item in materialized),
                fragment_count=sum(len(item["fragments"]) for item in materialized),
                byte_size=sum(int(item["byte_size"]) for item in materialized),
                content_hash=version_hash,
                manifest=manifest,
                created_by_user_id=str(db.info.get("user_id") or "") or None,
                ready_at=datetime.now(timezone.utc),
            )
            db.add(version)
            db.flush()
            for ordinal, raw_version in enumerate(versions):
                db.add(
                    DatasetVersionAsset(
                        tenant_id=tenant_id,
                        dataset_id=publication_dataset.id,
                        dataset_version_id=version.id,
                        asset_version_id=raw_version.id,
                        role="source",
                        ordinal=ordinal,
                        binding_document={},
                    )
                )
            for _claim, parquet_file, item, fragment_ordinal in staged_fragments:
                fragment = item["fragments"][fragment_ordinal]
                relation = relation_by_key[item["relation_key"]]
                db.add(
                    DatasetFragment(
                        tenant_id=tenant_id,
                        dataset_id=publication_dataset.id,
                        dataset_version_id=version.id,
                        dataset_relation_id=relation.id,
                        schema_id=schema.id,
                        bucket_file_id=parquet_file.id,
                        bucket_data_source_id=source.id,
                        ordinal=fragment_ordinal,
                        format="parquet",
                        compression="zstd",
                        status="ready",
                        row_count=fragment["row_count"],
                        byte_size=fragment["byte_size"],
                        content_sha256=fragment["content_sha256"],
                        statistics={"source_asset_count": len(versions)},
                    )
                )
            db.flush()
            db.commit()
            result = _result(publication_dataset, version, schema, reused=False)
            result["source_asset_version_ids"] = list(payload.asset_version_ids)
            return result
    except Exception:
        db.rollback()
        for claim, bucket_file in upload_records:
            object_deletion_service.schedule_abandoned_upload_best_effort(
                claim, bucket_file
            )
        raise


__all__ = [
    "ValidationDatasetError",
    "build_validation_dataset",
    "enqueue_validation_dataset_job",
    "get_validation_dataset_job",
    "process_next_validation_dataset_job",
    "process_validation_dataset_job",
    "recover_validation_dataset_jobs",
]
