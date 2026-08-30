"""Governed catalog writes without coupling assets to business scenarios."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..catalog_schemas import (
    DataAssetCreate,
    DataAssetVersionRegister,
    DatasetSchemaCreate,
    DatasetVersionCreate,
    LogicalDatasetCreate,
    ScenarioCapabilityPortCreate,
    ScenarioDatasetBindingCreate,
    SemanticMappingCreate,
)
from ..models import (
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetField,
    DatasetHead,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    DatasetVersionAsset,
    FunctionDefinition,
    LogicalDataset,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologyRelease,
    OntologySnapshot,
    OntologyWorkflow,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    SemanticFieldMapping,
    SemanticMapping,
)
from . import datasource_service, permission_service, tenant_service


class CatalogError(ValueError):
    """A catalog request violates a stable, non-sensitive domain rule."""


_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,179}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_PARTS = {
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "connection_string",
    "database_url",
}
_PORT_PHYSICAL_REFERENCE_KEYS = {
    "asset_id",
    "asset_version_id",
    "bucket_file_id",
    "connector_binding_id",
    "connection_string",
    "connection_url",
    "data_source_id",
    "dataset_head_id",
    "dataset_id",
    "dataset_version_id",
    "dsn",
    "physical_table",
    "sql",
    "table_name",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(db: Session) -> str | None:
    value = str(db.info.get("user_id") or "").strip()
    return value or None


def _tenant(db: Session) -> str:
    return tenant_service.current_tenant_id(db)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_document(value: Any, *, label: str, maximum: int = 128_000) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{label}必须是有效 JSON") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise CatalogError(f"{label}不能超过 {maximum} 字节")

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for raw_key, nested in node.items():
                key = str(raw_key).strip().lower()
                if key in _SECRET_PARTS or any(
                    part in key for part in ("password", "secret", "credential")
                ):
                    raise CatalogError(f"{label}不得包含凭据字段 {path}{raw_key}")
                visit(nested, f"{path}{raw_key}.")
        elif isinstance(node, list):
            for index, nested in enumerate(node):
                visit(nested, f"{path}{index}.")

    visit(value, "")
    return json.loads(encoded)


def safe_catalog_document(
    value: Any, *, label: str, maximum: int = 128_000
) -> Any:
    """Public guard for catalog-adjacent services writing JSON documents."""
    return _safe_document(value, label=label, maximum=maximum)


def _key(value: str, label: str = "key") -> str:
    normalized = str(value or "").strip()
    if _KEY_RE.fullmatch(normalized) is None:
        raise CatalogError(f"{label} 必须以字母或数字开头，且只能包含 . _ : -")
    return normalized


def validate_catalog_key(value: str, label: str = "key") -> str:
    """Apply the catalog's stable identifier policy outside CRUD handlers."""
    return _key(value, label)


def _port_config(value: dict[str, Any]) -> dict[str, Any]:
    document = _safe_document(value, label="能力端口配置", maximum=64_000)

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for raw_key, child in node.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in _PORT_PHYSICAL_REFERENCE_KEYS:
                    raise CatalogError(
                        f"能力端口配置不得包含物理资源或运行数据字段 {path}{raw_key}"
                    )
                visit(child, f"{path}{raw_key}.")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}{index}.")

    visit(document, "")
    return document


def require_asset(db: Session, asset_id: str) -> DataAsset:
    asset = db.execute(
        select(DataAsset).where(
            DataAsset.id == asset_id,
            DataAsset.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if asset is None:
        raise CatalogError("数据资产不存在")
    return asset


def require_asset_version(db: Session, version_id: str) -> DataAssetVersion:
    version = db.execute(
        select(DataAssetVersion).where(
            DataAssetVersion.id == version_id,
            DataAssetVersion.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if version is None:
        raise CatalogError("数据资产版本不存在")
    return version


def require_dataset(db: Session, dataset_id: str) -> LogicalDataset:
    dataset = db.execute(
        select(LogicalDataset).where(
            LogicalDataset.id == dataset_id,
            LogicalDataset.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if dataset is None:
        raise CatalogError("逻辑数据集不存在")
    return dataset


def require_schema(db: Session, schema_id: str, *, dataset_id: str | None = None) -> DatasetSchema:
    statement = select(DatasetSchema).where(
        DatasetSchema.id == schema_id,
        DatasetSchema.tenant_id == _tenant(db),
    )
    if dataset_id:
        statement = statement.where(DatasetSchema.dataset_id == dataset_id)
    schema = db.execute(statement).scalar_one_or_none()
    if schema is None:
        raise CatalogError("数据集 Schema 不存在")
    return schema


def require_dataset_version(
    db: Session,
    version_id: str,
    *,
    dataset_id: str | None = None,
    ready: bool = False,
) -> DatasetVersion:
    statement = select(DatasetVersion).where(
        DatasetVersion.id == version_id,
        DatasetVersion.tenant_id == _tenant(db),
    )
    if dataset_id:
        statement = statement.where(DatasetVersion.dataset_id == dataset_id)
    version = db.execute(statement).scalar_one_or_none()
    if version is None:
        raise CatalogError("数据集版本不存在")
    if ready and version.status != "ready":
        raise CatalogError("数据集版本尚未就绪")
    return version


def list_assets(db: Session) -> list[DataAsset]:
    permission_service.require_tenant_permission(db, "read")
    return list(
        db.scalars(
            select(DataAsset)
            .options(selectinload(DataAsset.versions))
            .where(DataAsset.tenant_id == _tenant(db))
            .order_by(DataAsset.created_at.desc(), DataAsset.id.desc())
        ).all()
    )


def create_asset(db: Session, payload: DataAssetCreate) -> DataAsset:
    permission_service.require_tenant_permission(db, "write")
    asset = DataAsset(
        tenant_id=_tenant(db),
        key=_key(payload.key, "资产 key"),
        name=payload.name.strip(),
        description=payload.description,
        kind=payload.kind,
        media_type=payload.media_type,
        labels=_safe_document(payload.labels, label="资产标签", maximum=32_000),
        lifecycle_status="active",
        created_by_user_id=_actor(db),
    )
    db.add(asset)
    db.flush()
    return asset


def register_asset_version(
    db: Session,
    asset: DataAsset,
    payload: DataAssetVersionRegister,
    *,
    allow_duplicate_content: bool = False,
) -> DataAssetVersion:
    permission_service.require_tenant_permission(db, "write")
    if asset.tenant_id != _tenant(db) or asset.lifecycle_status != "active":
        raise CatalogError("数据资产不可写")
    asset = db.execute(
        select(DataAsset)
        .where(
            DataAsset.id == asset.id,
            DataAsset.tenant_id == _tenant(db),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).scalar_one()
    row = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.id == payload.bucket_file_id,
            DataSource.tenant_id == _tenant(db),
        )
    ).first()
    if row is None:
        raise CatalogError("登记文件不存在")
    bucket_file, source = row
    if not datasource_service.is_managed_minio_file(bucket_file):
        raise CatalogError("只有受管 MinIO 文件可以登记为不可变资产版本")
    digest = str(bucket_file.content_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        try:
            content, actual_size, _media = datasource_service.read_bucket_file(bucket_file, source)
        except Exception as exc:  # noqa: BLE001 - keep storage details private.
            raise CatalogError("无法验证登记文件的完整性") from exc
        digest = hashlib.sha256(content).hexdigest()
        if actual_size != len(content):
            raise CatalogError("登记文件大小校验失败")
        bucket_file.content_sha256 = digest
    if not allow_duplicate_content:
        duplicate = db.execute(
            select(DataAssetVersion).where(
                DataAssetVersion.asset_id == asset.id,
                DataAssetVersion.content_sha256 == digest,
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            return duplicate
    next_number = int(
        db.scalar(
            select(func.coalesce(func.max(DataAssetVersion.version_number), 0)).where(
                DataAssetVersion.asset_id == asset.id
            )
        )
        or 0
    ) + 1
    locator = {
        "storage_provider": "minio",
        "bucket_name": bucket_file.bucket_name,
        "object_key": bucket_file.object_key,
        "object_version_id": bucket_file.object_version_id,
    }
    version = DataAssetVersion(
        tenant_id=asset.tenant_id,
        asset_id=asset.id,
        version_number=next_number,
        bucket_file_id=bucket_file.id,
        bucket_data_source_id=bucket_file.data_source_id,
        provenance_kind=payload.provenance_kind,
        status="ready",
        content_sha256=digest,
        byte_size=max(0, int(bucket_file.size or 0)),
        source_locator=locator,
        version_document=_safe_document(
            payload.version_document,
            label="资产版本说明",
        ),
        created_by_user_id=_actor(db),
    )
    db.add(version)
    db.flush()
    return version


def list_datasets(db: Session) -> list[LogicalDataset]:
    permission_service.require_tenant_permission(db, "read")
    return list(
        db.scalars(
            select(LogicalDataset)
            .options(
                selectinload(LogicalDataset.schemas),
                selectinload(LogicalDataset.versions),
                selectinload(LogicalDataset.heads),
            )
            .where(LogicalDataset.tenant_id == _tenant(db))
            .order_by(LogicalDataset.created_at.desc(), LogicalDataset.id.desc())
        ).all()
    )


def create_dataset(db: Session, payload: LogicalDatasetCreate) -> LogicalDataset:
    permission_service.require_tenant_permission(db, "write")
    dataset = LogicalDataset(
        tenant_id=_tenant(db),
        key=_key(payload.key, "数据集 key"),
        name=payload.name.strip(),
        description=payload.description,
        lifecycle_status="active",
        labels=_safe_document(payload.labels, label="数据集标签", maximum=32_000),
        created_by_user_id=_actor(db),
    )
    db.add(dataset)
    db.flush()
    return dataset


def create_schema(
    db: Session,
    dataset: LogicalDataset,
    payload: DatasetSchemaCreate,
) -> DatasetSchema:
    permission_service.require_tenant_permission(db, "write")
    if dataset.tenant_id != _tenant(db) or dataset.lifecycle_status != "active":
        raise CatalogError("逻辑数据集不可写")
    contract = {
        "schema_document": _safe_document(payload.schema_document, label="Schema 文档"),
        "relations": [item.model_dump(mode="json") for item in payload.relations],
    }
    schema_hash = _canonical_hash(contract)
    existing = db.execute(
        select(DatasetSchema).where(
            DatasetSchema.dataset_id == dataset.id,
            DatasetSchema.schema_hash == schema_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    next_number = int(
        db.scalar(
            select(func.coalesce(func.max(DatasetSchema.schema_version), 0)).where(
                DatasetSchema.dataset_id == dataset.id
            )
        )
        or 0
    ) + 1
    schema = DatasetSchema(
        tenant_id=dataset.tenant_id,
        dataset_id=dataset.id,
        schema_version=next_number,
        schema_hash=schema_hash,
        compatibility=payload.compatibility,
        schema_document=contract["schema_document"],
        created_by_user_id=_actor(db),
    )
    db.add(schema)
    db.flush()
    for relation_ordinal, relation_payload in enumerate(payload.relations):
        relation = DatasetRelation(
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            relation_key=_key(relation_payload.relation_key, "关系 key"),
            display_name=relation_payload.display_name.strip(),
            kind=relation_payload.kind,
            ordinal=relation_ordinal,
            description=relation_payload.description,
        )
        db.add(relation)
        db.flush()
        for field_ordinal, field_payload in enumerate(relation_payload.fields):
            db.add(
                DatasetField(
                    tenant_id=dataset.tenant_id,
                    dataset_id=dataset.id,
                    schema_id=schema.id,
                    dataset_relation_id=relation.id,
                    field_key=_key(field_payload.field_key, "字段 key"),
                    source_name=field_payload.source_name.strip(),
                    logical_type=field_payload.logical_type.strip(),
                    physical_type=field_payload.physical_type,
                    nullable=field_payload.nullable,
                    ordinal=field_ordinal,
                    key_ordinal=field_payload.key_ordinal,
                    semantic_role=field_payload.semantic_role,
                    field_document=_safe_document(
                        field_payload.field_document,
                        label="字段说明",
                        maximum=32_000,
                    ),
                )
            )
    db.flush()
    return schema


def load_schema(db: Session, schema_id: str, *, dataset_id: str | None = None) -> DatasetSchema:
    statement = (
        select(DatasetSchema)
        .options(
            selectinload(DatasetSchema.relations).selectinload(DatasetRelation.fields)
        )
        .where(
            DatasetSchema.id == schema_id,
            DatasetSchema.tenant_id == _tenant(db),
        )
    )
    if dataset_id:
        statement = statement.where(DatasetSchema.dataset_id == dataset_id)
    schema = db.execute(statement).scalar_one_or_none()
    if schema is None:
        raise CatalogError("数据集 Schema 不存在")
    return schema


def create_dataset_version(
    db: Session,
    dataset: LogicalDataset,
    payload: DatasetVersionCreate,
) -> DatasetVersion:
    permission_service.require_tenant_permission(db, "write")
    schema = require_schema(db, payload.schema_id, dataset_id=dataset.id)
    parent = None
    if payload.parent_version_id:
        parent = require_dataset_version(
            db,
            payload.parent_version_id,
            dataset_id=dataset.id,
            ready=True,
        )
    asset_ids = list(dict.fromkeys(payload.asset_version_ids))
    assets = list(
        db.scalars(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.id.in_(asset_ids),
                DataAssetVersion.tenant_id == _tenant(db),
                DataAssetVersion.status == "ready",
            )
            .order_by(DataAssetVersion.id)
        ).all()
    ) if asset_ids else []
    if len(assets) != len(asset_ids):
        raise CatalogError("输入资产版本不存在、未就绪或不属于当前租户")
    manifest = _safe_document(payload.manifest, label="数据集 manifest")
    identity = {
        "format": "catalog-dataset-version/v1",
        "dataset_id": dataset.id,
        "schema_hash": schema.schema_hash,
        "parent_content_hash": parent.content_hash if parent else None,
        "assets": [
            {"id": item.id, "content_sha256": item.content_sha256}
            for item in assets
        ],
        "manifest": manifest,
    }
    content_hash = _canonical_hash(identity)
    existing = db.execute(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.content_hash == content_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    next_number = int(
        db.scalar(
            select(func.coalesce(func.max(DatasetVersion.version_number), 0)).where(
                DatasetVersion.dataset_id == dataset.id
            )
        )
        or 0
    ) + 1
    record_count = manifest.get("record_count", 0)
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise CatalogError("manifest.record_count 必须是非负整数")
    version = DatasetVersion(
        tenant_id=dataset.tenant_id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=next_number,
        parent_version_id=parent.id if parent else None,
        status="ready",
        record_count=record_count,
        fragment_count=0,
        byte_size=sum(int(item.byte_size or 0) for item in assets),
        content_hash=content_hash,
        manifest={**manifest, "identity": identity},
        created_by_user_id=_actor(db),
        ready_at=_now(),
    )
    db.add(version)
    db.flush()
    for ordinal, asset_version in enumerate(assets):
        db.add(
            DatasetVersionAsset(
                tenant_id=dataset.tenant_id,
                dataset_id=dataset.id,
                dataset_version_id=version.id,
                asset_version_id=asset_version.id,
                role="source",
                ordinal=ordinal,
                binding_document={},
            )
        )
    db.flush()
    return version


def set_head(
    db: Session,
    dataset: LogicalDataset,
    environment: str,
    version_id: str,
    *,
    expected_version_id: str | None = None,
) -> DatasetHead:
    permission_service.require_tenant_permission(db, "write")
    if environment not in {"dev", "staging", "prod"}:
        raise CatalogError("不支持的数据集环境")
    version = require_dataset_version(
        db,
        version_id,
        dataset_id=dataset.id,
        ready=True,
    )
    head = db.execute(
        select(DatasetHead)
        .where(
            DatasetHead.dataset_id == dataset.id,
            DatasetHead.environment == environment,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if expected_version_id is not None:
        current_version_id = head.dataset_version_id if head is not None else None
        if current_version_id != expected_version_id:
            raise CatalogError("数据集 Head 已由其他操作更新，请刷新后重试")
    if head is None:
        head = DatasetHead(
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            environment=environment,
            dataset_version_id=version.id,
            updated_by_user_id=_actor(db),
        )
        db.add(head)
    else:
        head.dataset_version_id = version.id
        head.updated_by_user_id = _actor(db)
        head.updated_at = _now()
    db.flush()
    return head


def list_scenario_bindings(db: Session, scenario_id: str) -> list[ScenarioDatasetBinding]:
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    return list(
        db.scalars(
            select(ScenarioDatasetBinding)
            .where(
                ScenarioDatasetBinding.scenario_id == scenario.id,
                ScenarioDatasetBinding.tenant_id == _tenant(db),
            )
            .order_by(
                ScenarioDatasetBinding.environment,
                ScenarioDatasetBinding.binding_key,
            )
        ).all()
    )


def create_scenario_binding(
    db: Session,
    scenario_id: str,
    payload: ScenarioDatasetBindingCreate,
) -> ScenarioDatasetBinding:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "write")
    dataset = require_dataset(db, payload.dataset_id)
    role = "invocation_input" if payload.role == "input" else payload.role
    config = _safe_document(payload.config, label="场景数据绑定配置", maximum=64_000)
    if payload.binding_mode == "head":
        head = db.execute(
            select(DatasetHead).where(
                DatasetHead.id == payload.dataset_head_id,
                DatasetHead.dataset_id == dataset.id,
                DatasetHead.tenant_id == _tenant(db),
            )
        ).scalar_one_or_none()
        if head is None:
            raise CatalogError("数据集 Head 不存在")
        if head.environment != payload.environment:
            raise CatalogError("数据集 Head 与绑定环境不一致")
    else:
        require_dataset_version(
            db,
            str(payload.dataset_version_id or ""),
            dataset_id=dataset.id,
            ready=True,
        )
    binding = ScenarioDatasetBinding(
        tenant_id=_tenant(db),
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key=_key(payload.binding_key, "绑定 key"),
        environment=payload.environment,
        role=role,
        binding_mode=payload.binding_mode,
        dataset_head_id=payload.dataset_head_id,
        dataset_version_id=payload.dataset_version_id,
        is_required=payload.is_required,
        status=payload.status,
        config=config,
    )
    db.add(binding)
    db.flush()
    return binding


def resolved_binding_version(binding: ScenarioDatasetBinding) -> str | None:
    if binding.binding_mode == "pinned":
        return binding.dataset_version_id
    head = binding.dataset_head
    return head.dataset_version_id if head is not None else None


def list_capability_ports(db: Session, scenario_id: str) -> list[ScenarioCapabilityPort]:
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    return list(
        db.scalars(
            select(ScenarioCapabilityPort)
            .options(selectinload(ScenarioCapabilityPort.dataset_schema))
            .where(
                ScenarioCapabilityPort.scenario_id == scenario.id,
                ScenarioCapabilityPort.tenant_id == _tenant(db),
            )
            .order_by(
                ScenarioCapabilityPort.capability_kind,
                ScenarioCapabilityPort.capability_key,
                ScenarioCapabilityPort.direction,
                ScenarioCapabilityPort.port_key,
            )
        ).all()
    )


def require_capability_port(
    db: Session,
    scenario_id: str,
    port_id: str,
    *,
    writable: bool = False,
) -> ScenarioCapabilityPort:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=writable)
    permission_service.require_scenario_permission(
        db, scenario, "write" if writable else "read"
    )
    port = db.execute(
        select(ScenarioCapabilityPort)
        .options(selectinload(ScenarioCapabilityPort.dataset_schema))
        .where(
            ScenarioCapabilityPort.id == port_id,
            ScenarioCapabilityPort.scenario_id == scenario.id,
            ScenarioCapabilityPort.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if port is None:
        raise CatalogError("能力端口不存在")
    return port


_CAPABILITY_TARGET_MODELS = {
    "function": FunctionDefinition,
    "action": OntologyAction,
    "workflow": OntologyWorkflow,
}


def _require_capability_target(
    db: Session,
    scenario_id: str,
    *,
    capability_kind: str,
    capability_key: str,
) -> None:
    kind = str(capability_kind or "").strip().lower()
    key = str(capability_key or "").strip()
    model = _CAPABILITY_TARGET_MODELS.get(kind)
    if model is None:
        raise CatalogError("能力端口所属能力类型不受支持")
    target = db.get(model, key)
    if target is None or str(target.scenario_id or "") != scenario_id:
        raise CatalogError("能力端口所属能力不存在或不属于当前场景")


def _apply_capability_port(
    db: Session,
    port: ScenarioCapabilityPort,
    payload: ScenarioCapabilityPortCreate,
) -> ScenarioCapabilityPort:
    _require_capability_target(
        db,
        port.scenario_id,
        capability_kind=payload.capability_kind,
        capability_key=payload.capability_key,
    )
    schema = None
    if payload.dataset_id and payload.dataset_schema_id:
        schema = require_schema(
            db, payload.dataset_schema_id, dataset_id=payload.dataset_id
        )
    port.capability_kind = payload.capability_kind
    port.capability_key = payload.capability_key.strip()
    port.port_key = _key(payload.port_key, "能力端口 key")
    port.name = payload.name.strip()
    port.description = payload.description
    port.direction = payload.direction
    port.role = payload.role
    port.media_kind = payload.media_kind
    port.dataset_id = schema.dataset_id if schema else None
    port.dataset_schema_id = schema.id if schema else None
    port.schema_document = _safe_document(
        payload.schema_document,
        label="能力端口 JSON Schema",
    )
    port.is_required = payload.is_required
    port.cardinality = payload.cardinality
    port.binding_policy = payload.binding_policy
    port.status = payload.status
    port.config = _port_config(payload.config)
    port.updated_at = _now()
    return port


def create_capability_port(
    db: Session,
    scenario_id: str,
    payload: ScenarioCapabilityPortCreate,
) -> ScenarioCapabilityPort:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "write")
    port = ScenarioCapabilityPort(
        tenant_id=_tenant(db),
        scenario_id=scenario.id,
        capability_kind=payload.capability_kind,
        capability_key=payload.capability_key.strip(),
        port_key="pending",
        name=payload.name.strip(),
        direction=payload.direction,
        role=payload.role,
        created_by_user_id=_actor(db),
    )
    _apply_capability_port(db, port, payload)
    db.add(port)
    db.flush()
    return port


def update_capability_port(
    db: Session,
    scenario_id: str,
    port_id: str,
    payload: ScenarioCapabilityPortCreate,
) -> ScenarioCapabilityPort:
    port = require_capability_port(db, scenario_id, port_id, writable=True)
    _apply_capability_port(db, port, payload)
    db.flush()
    return port


def delete_capability_port(db: Session, scenario_id: str, port_id: str) -> None:
    port = require_capability_port(db, scenario_id, port_id, writable=True)
    if port.status == "active":
        raise CatalogError("激活端口必须先显式退役后才能删除")
    released_contents = db.execute(
        select(OntologySnapshot.content)
        .join(OntologyRelease, OntologyRelease.snapshot_id == OntologySnapshot.id)
        .where(
            OntologyRelease.tenant_id == _tenant(db),
            OntologyRelease.scenario_id == scenario_id,
        )
    ).scalars()
    if any(
        isinstance(item, dict) and str(item.get("id") or "") == port.id
        for content in released_contents
        if isinstance(content, dict)
        for item in content.get("capability_ports", [])
        if isinstance(item, dict)
    ):
        raise CatalogError("能力端口仍是历史发布的审计锚点，只能保持退役状态")
    db.delete(port)
    db.flush()


def create_semantic_mapping(
    db: Session,
    scenario_id: str,
    payload: SemanticMappingCreate,
) -> SemanticMapping:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "write")
    binding = db.execute(
        select(ScenarioDatasetBinding).where(
            ScenarioDatasetBinding.id == payload.scenario_dataset_binding_id,
            ScenarioDatasetBinding.scenario_id == scenario.id,
            ScenarioDatasetBinding.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if binding is None:
        raise CatalogError("场景数据绑定不存在")
    entity = db.execute(
        select(OntologyEntity).where(
            OntologyEntity.id == payload.entity_id,
            OntologyEntity.scenario_id == scenario.id,
        )
    ).scalar_one_or_none()
    if entity is None:
        raise CatalogError("对象类型不属于当前场景")
    schema = require_schema(db, payload.dataset_schema_id, dataset_id=binding.dataset_id)
    relation = db.execute(
        select(DatasetRelation).where(
            DatasetRelation.id == payload.dataset_relation_id,
            DatasetRelation.schema_id == schema.id,
            DatasetRelation.dataset_id == binding.dataset_id,
            DatasetRelation.tenant_id == _tenant(db),
        )
    ).scalar_one_or_none()
    if relation is None:
        raise CatalogError("数据关系不属于绑定数据集的 Schema")
    property_ids = [item.ontology_property_id for item in payload.fields]
    field_ids = [item.dataset_field_id for item in payload.fields]
    properties = {
        item.id: item
        for item in db.scalars(
            select(OntologyProperty).where(
                OntologyProperty.id.in_(property_ids),
                OntologyProperty.entity_id == entity.id,
            )
        ).all()
    } if property_ids else {}
    fields = {
        item.id: item
        for item in db.scalars(
            select(DatasetField).where(
                DatasetField.id.in_(field_ids),
                DatasetField.dataset_relation_id == relation.id,
                DatasetField.schema_id == schema.id,
                DatasetField.dataset_id == binding.dataset_id,
                DatasetField.tenant_id == _tenant(db),
            )
        ).all()
    } if field_ids else {}
    if len(properties) != len(set(property_ids)):
        raise CatalogError("字段映射包含不属于对象类型的属性")
    if len(fields) != len(set(field_ids)):
        raise CatalogError("字段映射包含不属于数据关系的字段")
    if len(property_ids) != len(set(property_ids)):
        raise CatalogError("同一对象属性不能重复映射")
    mapping = SemanticMapping(
        tenant_id=_tenant(db),
        dataset_id=binding.dataset_id,
        scenario_id=scenario.id,
        entity_id=entity.id,
        scenario_dataset_binding_id=binding.id,
        dataset_schema_id=schema.id,
        dataset_relation_id=relation.id,
        mapping_key=_key(payload.mapping_key, "语义映射 key"),
        status=payload.status,
        identifier_strategy=_safe_document(
            payload.identifier_strategy,
            label="标识策略",
            maximum=32_000,
        ),
        filter_expression=_safe_document(
            payload.filter_expression,
            label="过滤表达式",
            maximum=32_000,
        ),
    )
    db.add(mapping)
    db.flush()
    for ordinal, item in enumerate(payload.fields):
        db.add(
            SemanticFieldMapping(
                tenant_id=_tenant(db),
                scenario_id=scenario.id,
                dataset_id=binding.dataset_id,
                dataset_schema_id=schema.id,
                dataset_relation_id=relation.id,
                ontology_entity_id=entity.id,
                semantic_mapping_id=mapping.id,
                ontology_property_id=item.ontology_property_id,
                dataset_field_id=item.dataset_field_id,
                ordinal=ordinal,
                direction=item.direction,
                is_required=item.is_required,
                transform=_safe_document(
                    item.transform,
                    label="字段转换",
                    maximum=32_000,
                ),
            )
        )
    db.flush()
    return mapping


def list_semantic_mappings(db: Session, scenario_id: str) -> list[SemanticMapping]:
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    return list(
        db.scalars(
            select(SemanticMapping)
            .options(selectinload(SemanticMapping.field_mappings))
            .where(
                SemanticMapping.scenario_id == scenario.id,
                SemanticMapping.tenant_id == _tenant(db),
            )
            .order_by(SemanticMapping.created_at, SemanticMapping.id)
        ).all()
    )
