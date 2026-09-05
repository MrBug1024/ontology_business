"""Materialize tabular modeling evidence as a reusable schema-only contract source.

The resulting LogicalDataset intentionally has no DatasetVersion: modeling files
may describe a contract, but must never become invocation data implicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..catalog_schemas import (
    DatasetFieldCreate,
    DatasetRelationCreate,
    DatasetSchemaCreate,
    LogicalDatasetCreate,
)
from ..models import BucketFile, DataSource, LogicalDataset
from . import (
    catalog_service,
    catalog_ingestion_service,
    datasource_service,
    input_contract_validator,
    object_storage_service,
    permission_service,
    tenant_service,
)


MODELING_CONTRACT_SOURCE_PURPOSE = "modeling_contract_source"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ModelingContractSourceRef:
    dataset_id: str
    schema_id: str


@dataclass(frozen=True)
class ExistingTabularProfile:
    profile: dict[str, Any]
    content_sha256: str


def _dataset_key(source_id: str, content_sha256: str) -> str:
    return f"modeling.contract.{source_id}.{content_sha256}"


def _require_source_write(db: Session, source: DataSource) -> None:
    tenant_id = tenant_service.current_tenant_id(db)
    if (
        source.tenant_id != tenant_id
        or source.resource_scope != "modeling"
        or source.type != "file_bucket"
    ):
        raise catalog_service.CatalogError("只有有权访问的建模资料文件桶可生成契约来源")
    if source.scenario_id:
        scenario = tenant_service.require_scenario(db, source.scenario_id, writable=True)
        permission_service.require_scenario_permission(
            db,
            scenario,
            "write",
            message="没有所选建模资料所属业务场景的权限",
        )
    else:
        permission_service.require_tenant_permission(db, "write")


def profile_existing_tabular_file(
    source: DataSource,
    bucket_file: BucketFile,
) -> ExistingTabularProfile | None:
    """Stream and sniff an existing managed file without trusting its filename."""

    if bucket_file.data_source_id != source.id:
        raise catalog_service.CatalogError("建模资料文件归属无效")
    try:
        bucket_name, object_key, _safe_name = datasource_service.minio_file_identity(
            bucket_file, source
        )
    except (ValueError, object_storage_service.ObjectStorageError) as exc:
        raise catalog_service.CatalogError("建模资料文件身份无效") from exc
    with TemporaryDirectory(prefix="modeling-contract-profile-") as directory:
        path = Path(directory) / "payload"
        try:
            object_storage_service.download_object_to_file(
                bucket_name,
                object_key,
                path,
                version_id=bucket_file.object_version_id,
                max_bytes=int(get_settings().catalog_max_upload_bytes),
            )
            profiled = catalog_ingestion_service.build_tabular_profile_path(
                path,
                bucket_file.filename,
                bucket_file.mime,
            )
        except (FileNotFoundError, ValueError, object_storage_service.ObjectStorageError) as exc:
            raise catalog_service.CatalogError("无法安全读取建模资料文件") from exc
        if profiled is None:
            return None
        _media_type, profile = profiled
        digest_builder = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
    recorded_digest = str(bucket_file.content_sha256 or "").strip().lower()
    if recorded_digest and recorded_digest != digest:
        raise catalog_service.CatalogError("建模资料内容完整性校验失败")
    bucket_file.content_sha256 = digest
    return ExistingTabularProfile(profile=profile, content_sha256=digest)


def _relations_from_profile(profile: Mapping[str, Any]) -> list[DatasetRelationCreate]:
    raw_tables = profile.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables or len(raw_tables) > 200:
        raise catalog_service.CatalogError("表格内容没有可用的有界结构")
    relations: list[DatasetRelationCreate] = []
    for relation_index, raw_table in enumerate(raw_tables):
        if not isinstance(raw_table, Mapping):
            raise catalog_service.CatalogError("表格关系结构无效")
        raw_columns = raw_table.get("columns")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or len(raw_columns) > 500
        ):
            raise catalog_service.CatalogError("表格字段结构无效或超过平台上限")
        fields: list[DatasetFieldCreate] = []
        for field_index, raw_field in enumerate(raw_columns):
            if not isinstance(raw_field, Mapping):
                raise catalog_service.CatalogError("表格字段结构无效")
            source_name = str(raw_field.get("name") or "").strip()
            logical_type = str(raw_field.get("logical_type") or "unknown").strip().lower()
            if not source_name or len(source_name) > 300 or logical_type not in {
                "string",
                "integer",
                "number",
                "boolean",
                "date",
                "datetime",
                "object",
                "null",
                "unknown",
            }:
                raise catalog_service.CatalogError("表格字段名称或类型无效")
            fields.append(
                DatasetFieldCreate(
                    field_key=f"field_{field_index + 1:04d}",
                    source_name=source_name,
                    logical_type=logical_type,
                    nullable=bool(raw_field.get("nullable", True)),
                    field_document={},
                )
            )
        display_name = str(raw_table.get("name") or "").strip()[:300]
        relations.append(
            DatasetRelationCreate(
                relation_key=f"relation_{relation_index + 1:04d}",
                display_name=display_name or f"表 {relation_index + 1}",
                kind="table",
                fields=fields,
            )
        )
    return relations


def materialize_tabular_contract_source(
    db: Session,
    *,
    source: DataSource,
    bucket_file: BucketFile,
    profile: Mapping[str, Any],
    content_sha256: str,
) -> ModelingContractSourceRef | None:
    """Create an idempotent schema-only modeling source in the caller transaction."""

    if str(profile.get("category") or "").strip().lower() != "table":
        return None
    _require_source_write(db, source)
    digest = str(content_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise catalog_service.CatalogError("建模资料缺少有效的内容身份")
    if bucket_file.data_source_id != source.id:
        raise catalog_service.CatalogError("建模资料文件归属无效")
    recorded_digest = str(bucket_file.content_sha256 or "").strip().lower()
    if recorded_digest and recorded_digest != digest:
        raise catalog_service.CatalogError("建模资料内容身份不一致")

    dataset_key = _dataset_key(source.id, digest)
    dataset = db.scalar(
        select(LogicalDataset).where(
            LogicalDataset.tenant_id == tenant_service.current_tenant_id(db),
            LogicalDataset.key == dataset_key,
        )
    )
    scope = "scenario" if source.scenario_id else "tenant"
    labels = {
        "catalog_purpose": MODELING_CONTRACT_SOURCE_PURPOSE,
        "modeling_source_scope": scope,
        "modeling_source_scenario_id": source.scenario_id or "",
        "modeling_source_data_source_id": source.id,
        "modeling_source_bucket_file_id": bucket_file.id,
        "source_content_sha256": digest,
    }
    if dataset is None:
        dataset = catalog_service.create_dataset(
            db,
            LogicalDatasetCreate(
                key=dataset_key,
                name=(bucket_file.filename.strip() or "表格建模资料")[:300],
                description="由建模资料中的表格内容生成，仅用于定义能力输入契约。",
                usage_plane="modeling_material",
                labels=labels,
            ),
        )
    else:
        existing_labels = dict(dataset.labels or {})
        immutable_labels = {
            key: labels[key]
            for key in (
                "catalog_purpose",
                "modeling_source_scope",
                "modeling_source_scenario_id",
                "modeling_source_data_source_id",
                "source_content_sha256",
            )
        }
        if (
            dataset.usage_plane != "modeling_material"
            or any(existing_labels.get(key) != value for key, value in immutable_labels.items())
        ):
            raise catalog_service.CatalogError("既有建模契约来源与当前资料身份冲突")
        dataset.labels = labels
        dataset.name = (bucket_file.filename.strip() or "表格建模资料")[:300]
        dataset.lifecycle_status = "active"
        dataset.retired_at = None

    schema = catalog_service.create_schema(
        db,
        dataset,
        DatasetSchemaCreate(
            compatibility="none",
            schema_document={
                "format": "modeling-tabular-contract-source/v1",
                "category": "table",
            },
            relations=_relations_from_profile(profile),
        ),
    )
    loaded_schema = catalog_service.load_schema(db, schema.id, dataset_id=dataset.id)
    try:
        input_contract_validator.build_tabular_content_contract(loaded_schema.relations)
    except input_contract_validator.InputContractError as exc:
        raise catalog_service.CatalogError(
            f"表格结构不能形成无歧义的输入契约：{exc.message}"
        ) from exc
    return ModelingContractSourceRef(dataset_id=dataset.id, schema_id=schema.id)


def retire_for_file_deletion(
    db: Session,
    source: DataSource,
    bucket_file: BucketFile,
) -> None:
    """Retire or repoint a source before deleting its current managed file."""

    digest = str(bucket_file.content_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        return
    dataset = db.scalar(
        select(LogicalDataset).where(
            LogicalDataset.tenant_id == tenant_service.current_tenant_id(db),
            LogicalDataset.key == _dataset_key(source.id, digest),
            LogicalDataset.usage_plane == "modeling_material",
        )
    )
    if dataset is None:
        return
    labels = dict(dataset.labels or {})
    if (
        labels.get("catalog_purpose") != MODELING_CONTRACT_SOURCE_PURPOSE
        or labels.get("modeling_source_bucket_file_id") != bucket_file.id
    ):
        return
    replacement = db.scalar(
        select(BucketFile)
        .where(
            BucketFile.data_source_id == source.id,
            BucketFile.content_sha256 == digest,
            BucketFile.id != bucket_file.id,
        )
        .order_by(BucketFile.created_at.desc(), BucketFile.id.desc())
        .limit(1)
    )
    if replacement is not None:
        dataset.labels = {
            **labels,
            "modeling_source_bucket_file_id": replacement.id,
        }
        dataset.name = (replacement.filename.strip() or "表格建模资料")[:300]
        return
    dataset.lifecycle_status = "retired"
    dataset.retired_at = datetime.now(timezone.utc)


def retire_for_data_source_deletion(db: Session, source: DataSource) -> None:
    """Retire every contract source whose modeling file bucket is being removed."""

    prefix = f"modeling.contract.{source.id}.%"
    datasets = list(
        db.scalars(
            select(LogicalDataset).where(
                LogicalDataset.tenant_id == tenant_service.current_tenant_id(db),
                LogicalDataset.key.like(prefix),
                LogicalDataset.usage_plane == "modeling_material",
                LogicalDataset.lifecycle_status == "active",
            )
        ).all()
    )
    retired_at = datetime.now(timezone.utc)
    for dataset in datasets:
        labels = dict(dataset.labels or {})
        if (
            labels.get("catalog_purpose") == MODELING_CONTRACT_SOURCE_PURPOSE
            and labels.get("modeling_source_data_source_id") == source.id
        ):
            dataset.lifecycle_status = "retired"
            dataset.retired_at = retired_at


def refs_for_bucket_files(
    db: Session,
    source: DataSource,
    files: list[BucketFile],
) -> dict[str, ModelingContractSourceRef]:
    """Resolve ready contract-source refs for a bounded, authorized file listing."""

    keys_by_file = {
        item.id: _dataset_key(source.id, str(item.content_sha256 or "").strip().lower())
        for item in files
        if _SHA256_RE.fullmatch(str(item.content_sha256 or "").strip().lower())
    }
    if not keys_by_file:
        return {}
    datasets = list(
        db.scalars(
            select(LogicalDataset)
            .options(selectinload(LogicalDataset.schemas))
            .where(
                LogicalDataset.tenant_id == tenant_service.current_tenant_id(db),
                LogicalDataset.key.in_(list(keys_by_file.values())),
                LogicalDataset.usage_plane == "modeling_material",
                LogicalDataset.lifecycle_status == "active",
            )
        ).all()
    )
    by_key = {item.key: item for item in datasets}
    result: dict[str, ModelingContractSourceRef] = {}
    for file_id, key in keys_by_file.items():
        dataset = by_key.get(key)
        if dataset is None:
            continue
        labels = dict(dataset.labels or {})
        digest = key.rsplit(".", 1)[-1]
        if (
            labels.get("catalog_purpose") != MODELING_CONTRACT_SOURCE_PURPOSE
            or labels.get("modeling_source_data_source_id") != source.id
            or labels.get("modeling_source_bucket_file_id") != file_id
            or labels.get("source_content_sha256") != digest
        ):
            continue
        schemas = sorted(dataset.schemas, key=lambda item: (item.schema_version, item.id))
        if schemas:
            result[file_id] = ModelingContractSourceRef(
                dataset_id=dataset.id,
                schema_id=schemas[-1].id,
            )
    return result
