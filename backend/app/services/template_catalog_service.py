"""Governed catalog for reusable DOCX/XLSX/Markdown business templates.

Template bytes remain ordinary :class:`BucketFile` objects.  This service adds
tenant/scenario ownership, immutable versions, inspected metadata and Action
references without introducing a second storage subsystem.
"""
from __future__ import annotations

from pathlib import Path
import re
import uuid
from typing import Any, Iterable, Mapping

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    BusinessScenario,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyProposal,
    OntologyRelease,
    OntologySnapshot,
)
from . import template_artifact_service


class TemplateCatalogError(ValueError):
    """A template catalog mutation or reference is invalid."""


_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,119}$")


def normalize_key(value: str) -> str:
    key = str(value or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise TemplateCatalogError(
            "模板标识必须以英文字母开头，且只能包含字母、数字、点、下划线或连字符"
        )
    return key


def _unique_generated_key(db: Session, tenant_id: str, digest: str) -> str:
    base = f"template_{digest[:12]}"
    key = base
    suffix = 2
    while db.scalar(select(ArtifactTemplate.id).where(
        ArtifactTemplate.tenant_id == tenant_id,
        ArtifactTemplate.key == key,
    )):
        key = f"{base}_{suffix}"
        suffix += 1
    return key


def _validate_source_scope(
    source: DataSource,
    *,
    tenant_id: str,
    scenario_id: str | None,
) -> None:
    if source.tenant_id != tenant_id:
        raise TemplateCatalogError("模板文件必须位于当前租户自有文件桶")
    if source.type != "file_bucket":
        raise TemplateCatalogError("模板文件必须位于文件桶数据源")
    if scenario_id is None:
        if source.scenario_id is not None:
            raise TemplateCatalogError("租户共享模板的文件必须位于租户级共享文件桶")
    elif source.scenario_id not in (None, scenario_id):
        raise TemplateCatalogError("模板文件桶不属于所选业务场景")


def lock_scenarios_for_template_write(
    db: Session,
    *,
    tenant_id: str,
    scenario_ids: Iterable[str | None],
) -> dict[str, BusinessScenario]:
    """Acquire the outermost S locks for a template-related write.

    PostgreSQL takes an implicit KEY SHARE lock on a referenced scenario when
    an Action, template, snapshot or release row is inserted. Acquiring the
    scenario rows explicitly and deterministically before T -> D -> F prevents
    a concurrent scenario deletion (which starts at S) from deadlocking with a
    later implicit FK lock.
    """
    ordered_ids = sorted({str(item) for item in scenario_ids if item})
    if not ordered_ids:
        return {}
    rows = db.scalars(
        select(BusinessScenario)
        .where(
            BusinessScenario.tenant_id == tenant_id,
            BusinessScenario.id.in_(ordered_ids),
        )
        .order_by(BusinessScenario.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    locked = {scenario.id: scenario for scenario in rows}
    if set(locked) != set(ordered_ids):
        raise TemplateCatalogError(
            "模板相关业务场景在写入期间已删除或不属于当前租户，请刷新后重试"
        )
    return locked


def inspect_bucket_file(
    template_file: BucketFile,
    template_source: DataSource,
) -> tuple[bytes, dict[str, Any], list[str]]:
    """Read through the bucket boundary and persist only server-derived facts."""
    try:
        content = template_artifact_service.load_bucket_template(
            template_file, template_source
        )
        metadata = template_artifact_service.inspect_template(
            template_file.filename, content
        )
        placeholders = sorted(
            template_artifact_service._placeholder_paths(  # noqa: SLF001
                template_file.filename, content
            )
        )
    except (FileNotFoundError, ValueError, template_artifact_service.TemplateArtifactError) as exc:
        raise TemplateCatalogError(str(exc)) from exc
    return content, metadata, placeholders


def _validated_uploaded_inspection(
    template_file: BucketFile,
    inspection: template_artifact_service.TemplateInspection,
) -> tuple[dict[str, Any], list[str]]:
    """Trust a staged-file inspection only when it matches the stored object row.

    The upload route validates the temporary file before streaming that same
    file to MinIO.  Avoiding a read-after-write here prevents a 400 MiB upload
    from being materialised again in an API worker.  Later catalog resolution
    still reads and verifies the object through the normal integrity boundary.
    """
    metadata = dict(inspection.metadata)
    artifact_format, mime, suffix = template_artifact_service.template_format(
        template_file.filename
    )
    expected_sha256 = str(metadata.get("sha256") or "").lower()
    expected_size = int(metadata.get("size") or -1)
    if (
        metadata.get("format") != artifact_format
        or metadata.get("mime") != mime
        or metadata.get("suffix") != suffix
        or expected_size < 1
        or expected_size != int(template_file.size or 0)
        or expected_sha256 != str(template_file.content_sha256 or "").lower()
    ):
        raise TemplateCatalogError("上传模板校验结果与存储对象不一致")
    paths = sorted({str(value) for value in inspection.placeholder_paths})
    for path in paths:
        template_artifact_service._validated_path_parts(path)  # noqa: SLF001
    if len(paths) != len(inspection.placeholder_paths):
        raise TemplateCatalogError("上传模板占位符重复或无效")
    return metadata, paths


def create_from_bucket_file(
    db: Session,
    *,
    tenant_id: str,
    template_file: BucketFile,
    template_source: DataSource,
    scenario_id: str | None,
    name: str,
    purpose: str = "",
    description: str = "",
    key: str = "",
    version_note: str = "",
    created_by_user_id: str | None = None,
    stable_template_id: str | None = None,
    stable_version_id: str | None = None,
    inspection: template_artifact_service.TemplateInspection | None = None,
) -> ArtifactTemplate:
    _validate_source_scope(
        template_source, tenant_id=tenant_id, scenario_id=scenario_id
    )
    if inspection is None:
        _content, metadata, placeholders = inspect_bucket_file(
            template_file, template_source
        )
    else:
        metadata, placeholders = _validated_uploaded_inspection(
            template_file, inspection
        )
    normalized_name = str(name or "").strip()
    if not normalized_name or len(normalized_name) > 200:
        raise TemplateCatalogError("模板名称必须是 1 到 200 个字符")
    normalized_key = normalize_key(key) if key else _unique_generated_key(
        db, tenant_id, str(metadata["sha256"])
    )
    if db.scalar(select(ArtifactTemplate.id).where(
        ArtifactTemplate.tenant_id == tenant_id,
        ArtifactTemplate.key == normalized_key,
    )):
        raise TemplateCatalogError("模板标识在当前租户内已存在")

    template = ArtifactTemplate(
        id=stable_template_id or uuid.uuid4().hex,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        key=normalized_key,
        name=normalized_name,
        purpose=str(purpose or "").strip(),
        description=str(description or "").strip(),
        status="active",
        created_by_user_id=created_by_user_id,
    )
    db.add(template)
    db.flush()
    version = ArtifactTemplateVersion(
        id=stable_version_id or uuid.uuid4().hex,
        template_id=template.id,
        version=1,
        bucket_file_id=template_file.id,
        filename=template_file.filename,
        artifact_format=str(metadata["format"]),
        mime=str(metadata["mime"]),
        size=int(metadata["size"]),
        content_sha256=str(metadata["sha256"]),
        placeholder_paths=placeholders,
        template_metadata={
            "suffix": metadata["suffix"],
            "placeholder_count": len(placeholders),
        },
        version_note=str(version_note or "").strip(),
        created_by_user_id=created_by_user_id,
    )
    db.add(version)
    db.flush()
    template.current_version_id = version.id
    template_file.content_sha256 = str(metadata["sha256"])
    db.flush()
    return template


def add_version_from_bucket_file(
    db: Session,
    template: ArtifactTemplate,
    *,
    template_file: BucketFile,
    template_source: DataSource,
    version_note: str = "",
    set_current: bool = True,
    created_by_user_id: str | None = None,
    stable_version_id: str | None = None,
    allow_deprecated: bool = False,
    inspection: template_artifact_service.TemplateInspection | None = None,
) -> ArtifactTemplateVersion:
    if template.status != "active" and not allow_deprecated:
        raise TemplateCatalogError("已停用模板不能新增版本，请先恢复模板")
    _validate_source_scope(
        template_source,
        tenant_id=template.tenant_id,
        scenario_id=template.scenario_id,
    )
    if inspection is None:
        _content, metadata, placeholders = inspect_bucket_file(
            template_file, template_source
        )
    else:
        metadata, placeholders = _validated_uploaded_inspection(
            template_file, inspection
        )
    existing = db.scalar(select(ArtifactTemplateVersion).where(
        ArtifactTemplateVersion.template_id == template.id,
        ArtifactTemplateVersion.content_sha256 == str(metadata["sha256"]),
    ))
    if existing:
        existing_file = db.get(BucketFile, existing.bucket_file_id)
        existing_source = (
            db.get(DataSource, existing_file.data_source_id) if existing_file else None
        )
        if not existing_file or not existing_source:
            raise TemplateCatalogError("相同哈希的既有模板版本文件已丢失")
        _existing_content, existing_metadata, existing_placeholders = inspect_bucket_file(
            existing_file, existing_source
        )
        if (
            str(existing_metadata["sha256"]) != existing.content_sha256
            or existing_placeholders != sorted(existing.placeholder_paths or [])
        ):
            raise TemplateCatalogError("相同哈希的既有模板版本完整性校验失败")
        if set_current:
            template.current_version_id = existing.id
        return existing
    next_version = int(db.scalar(select(func.max(ArtifactTemplateVersion.version)).where(
        ArtifactTemplateVersion.template_id == template.id
    )) or 0) + 1
    version = ArtifactTemplateVersion(
        id=stable_version_id or uuid.uuid4().hex,
        template_id=template.id,
        version=next_version,
        bucket_file_id=template_file.id,
        filename=template_file.filename,
        artifact_format=str(metadata["format"]),
        mime=str(metadata["mime"]),
        size=int(metadata["size"]),
        content_sha256=str(metadata["sha256"]),
        placeholder_paths=placeholders,
        template_metadata={
            "suffix": metadata["suffix"],
            "placeholder_count": len(placeholders),
        },
        version_note=str(version_note or "").strip(),
        created_by_user_id=created_by_user_id,
    )
    db.add(version)
    db.flush()
    if set_current:
        template.current_version_id = version.id
    template_file.content_sha256 = str(metadata["sha256"])
    db.flush()
    return version


def get_owned(
    db: Session,
    template_id: str,
    tenant_id: str,
    *,
    for_update: bool = False,
) -> ArtifactTemplate | None:
    stmt = (
        select(ArtifactTemplate)
        .options(
            selectinload(ArtifactTemplate.versions).selectinload(
                ArtifactTemplateVersion.bucket_file
            )
        )
        .where(
            ArtifactTemplate.id == template_id,
            ArtifactTemplate.tenant_id == tenant_id,
        )
    )
    if for_update:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    return db.scalar(stmt)


def get_available_for_scenario(
    db: Session,
    template_id: str,
    tenant_id: str,
    scenario_id: str,
    *,
    for_update: bool = False,
) -> ArtifactTemplate | None:
    """Resolve catalog visibility in SQL before an optional row lock."""
    stmt = (
        select(ArtifactTemplate)
        .options(
            selectinload(ArtifactTemplate.versions).selectinload(
                ArtifactTemplateVersion.bucket_file
            )
        )
        .where(
            ArtifactTemplate.id == template_id,
            ArtifactTemplate.tenant_id == tenant_id,
            or_(
                ArtifactTemplate.scenario_id.is_(None),
                ArtifactTemplate.scenario_id == scenario_id,
            ),
        )
    )
    if for_update:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    return db.scalar(stmt)


def resolve_version(
    db: Session,
    *,
    template_id: str,
    tenant_id: str,
    scenario_id: str,
    version_number: int | None = None,
    version_id: str | None = None,
    expected_sha256: str = "",
    require_active: bool = False,
    lock_template: bool = False,
) -> tuple[ArtifactTemplate, ArtifactTemplateVersion, BucketFile, DataSource]:
    """Resolve a pinned catalog revision and revalidate its immutable bytes."""
    template = get_available_for_scenario(
        db,
        template_id,
        tenant_id,
        scenario_id,
        for_update=lock_template,
    )
    if not template:
        raise TemplateCatalogError("模板不存在或不在当前租户")
    if template.scenario_id not in (None, scenario_id):
        raise TemplateCatalogError("模板不属于当前业务场景")
    if require_active and template.status != "active":
        raise TemplateCatalogError("模板已停用，不能创建新的操作绑定")
    stmt = select(ArtifactTemplateVersion).where(
        ArtifactTemplateVersion.template_id == template.id
    )
    if version_id:
        stmt = stmt.where(ArtifactTemplateVersion.id == version_id)
    elif version_number is not None:
        stmt = stmt.where(ArtifactTemplateVersion.version == version_number)
    elif template.current_version_id:
        stmt = stmt.where(ArtifactTemplateVersion.id == template.current_version_id)
    else:
        raise TemplateCatalogError("模板尚无可用版本")
    version = db.scalar(stmt)
    if not version:
        raise TemplateCatalogError("模板版本不存在")
    template_file = db.get(BucketFile, version.bucket_file_id)
    if not template_file:
        raise TemplateCatalogError("模板版本对应的文件已丢失")
    template_source = db.get(DataSource, template_file.data_source_id)
    if not template_source:
        raise TemplateCatalogError("模板版本对应的文件桶已丢失")
    _validate_source_scope(
        template_source,
        tenant_id=tenant_id,
        scenario_id=template.scenario_id,
    )
    _content, metadata, placeholders = inspect_bucket_file(template_file, template_source)
    actual_sha = str(metadata["sha256"])
    if actual_sha != version.content_sha256:
        raise TemplateCatalogError("模板版本文件内容与登记哈希不一致")
    if expected_sha256 and expected_sha256 != actual_sha:
        raise TemplateCatalogError("Action 固定的模板哈希与登记版本不一致")
    if str(metadata["format"]) != version.artifact_format or str(metadata["mime"]) != version.mime:
        raise TemplateCatalogError("模板版本文件格式与登记信息不一致")
    if placeholders != sorted(version.placeholder_paths or []):
        raise TemplateCatalogError("模板占位符与登记版本不一致")
    return template, version, template_file, template_source


def pinned_action_config(
    template: ArtifactTemplate,
    version: ArtifactTemplateVersion,
    *,
    target_data_source_id: str,
    output_filename: str,
    variable_paths: Iterable[str] = (),
) -> dict[str, Any]:
    paths = sorted(set(version.placeholder_paths or []) | {str(path) for path in variable_paths})
    return {
        "template_id": template.id,
        "template_version": version.version,
        "template_sha256": version.content_sha256,
        "target_data_source_id": target_data_source_id,
        "output_filename": output_filename,
        "template_format": version.artifact_format,
        "template_mime": version.mime,
        "template_filename": version.filename,
        "template_variable_paths": paths,
    }


def _snapshot_template_signature(action: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(action, Mapping) or str(action.get("executor_type") or "") != "template":
        return ()
    config = action.get("executor_config")
    if not isinstance(config, Mapping):
        return ()
    template_id = str(config.get("template_id") or "")
    if template_id:
        source = (
            "catalog",
            template_id,
            str(config.get("template_version") or ""),
        )
    else:
        source = (
            "legacy",
            str(config.get("template_file_id") or ""),
            str(config.get("template_data_source_id") or ""),
        )
    return (
        *source,
        str(config.get("target_data_source_id") or ""),
        str(config.get("output_filename") or ""),
    )


def validate_snapshot_template_actions(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    actions: Iterable[Mapping[str, Any]],
    previous_actions: Iterable[Mapping[str, Any]] = (),
    authorize_changes: bool = False,
) -> None:
    """Validate and serialize every template resource referenced by a snapshot.

    Action configs are JSON, so database foreign keys cannot protect catalog or
    target IDs.  Governance entry points call this in the same transaction that
    persists/applies/releases a snapshot.  Locks are always acquired as all
    catalog templates (T), then all data sources (D), then all bucket files (F),
    each in stable ID order.  That matches catalog versioning and bucket-delete
    paths and closes the scan-then-insert race with catalog/data-source deletion.
    """
    descriptors: list[dict[str, Any]] = []
    previous_by_id = {
        str(action.get("id") or ""): action
        for action in previous_actions
        if isinstance(action, Mapping) and action.get("id")
    }
    for action in actions:
        if not isinstance(action, Mapping) or str(action.get("executor_type") or "") != "template":
            continue
        label = str(action.get("name") or action.get("id") or "未命名 Action")[:200]
        if not bool(action.get("requires_confirmation", True)) or not bool(
            action.get("idempotency_required", True)
        ):
            raise TemplateCatalogError(
                f"模板 Action「{label}」必须启用人工确认和幂等保护"
            )
        config = action.get("executor_config")
        if not isinstance(config, Mapping):
            raise TemplateCatalogError(f"模板 Action「{label}」执行配置无效")
        template_id = str(config.get("template_id") or "")
        legacy_file_id = str(config.get("template_file_id") or "")
        target_id = str(config.get("target_data_source_id") or "")
        if not target_id:
            raise TemplateCatalogError(f"模板 Action「{label}」缺少附件目标文件桶")
        if template_id and legacy_file_id:
            raise TemplateCatalogError(f"模板 Action「{label}」不能混用目录与旧式文件绑定")
        previous = previous_by_id.get(str(action.get("id") or ""))
        signature = _snapshot_template_signature(action)
        previous_signature = _snapshot_template_signature(previous)
        descriptor: dict[str, Any] = {
            "action": action,
            "config": config,
            "label": label,
            "target_id": target_id,
            "signature": signature,
            "previous_signature": previous_signature,
        }
        if template_id:
            raw_version = config.get("template_version")
            raw_sha = str(config.get("template_sha256") or "")
            if isinstance(raw_version, bool):
                raise TemplateCatalogError(f"模板 Action「{label}」固定版本无效")
            try:
                version_number = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」缺少数字固定版本"
                ) from exc
            if version_number < 1:
                raise TemplateCatalogError(f"模板 Action「{label}」固定版本无效")
            if not re.fullmatch(r"[0-9a-f]{64}", raw_sha):
                raise TemplateCatalogError(f"模板 Action「{label}」缺少有效固定哈希")
            descriptor.update({
                "kind": "catalog",
                "template_id": template_id,
                "version_number": version_number,
                "sha256": raw_sha,
            })
        elif legacy_file_id:
            if authorize_changes and signature != previous_signature:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」新增或改绑必须使用模板目录的固定版本"
                )
            descriptor.update({"kind": "legacy", "file_id": legacy_file_id})
        else:
            raise TemplateCatalogError(f"模板 Action「{label}」缺少模板资源")
        descriptors.append(descriptor)
    if not descriptors:
        return

    # T: acquire every catalog row first, in a deterministic order.
    locked_templates: dict[str, ArtifactTemplate] = {}
    for template_id in sorted({
        item["template_id"] for item in descriptors if item["kind"] == "catalog"
    }):
        template = get_available_for_scenario(
            db,
            template_id,
            tenant_id,
            scenario_id,
            for_update=True,
        )
        if not template:
            raise TemplateCatalogError("模板 Action 引用的目录模板不存在或不在当前租户")
        if template.scenario_id not in (None, scenario_id):
            raise TemplateCatalogError("模板 Action 不能引用其他业务场景的目录模板")
        locked_templates[template_id] = template

    source_ids: set[str] = set()
    file_ids: set[str] = set()
    for descriptor in descriptors:
        source_ids.add(descriptor["target_id"])
        if descriptor["kind"] == "catalog":
            template = locked_templates[descriptor["template_id"]]
            version = next(
                (
                    candidate
                    for candidate in template.versions
                    if candidate.version == descriptor["version_number"]
                ),
                None,
            )
            if not version:
                raise TemplateCatalogError(
                    f"模板 Action「{descriptor['label']}」固定版本不存在"
                )
            if version.content_sha256 != descriptor["sha256"]:
                raise TemplateCatalogError(
                    f"模板 Action「{descriptor['label']}」固定哈希与登记版本不一致"
                )
            bucket_file = version.bucket_file
            if not bucket_file:
                raise TemplateCatalogError("模板 Action 固定版本的文件已丢失")
            descriptor["version"] = version
            descriptor["file_id"] = bucket_file.id
            descriptor["source_id"] = bucket_file.data_source_id
            file_ids.add(bucket_file.id)
            source_ids.add(bucket_file.data_source_id)
            if (
                authorize_changes
                and descriptor["signature"] != descriptor["previous_signature"]
                and template.status != "active"
            ):
                raise TemplateCatalogError(
                    f"模板 Action「{descriptor['label']}」不能新绑定已停用模板"
                )
        else:
            observed_row = db.execute(
                select(BucketFile, DataSource)
                .join(DataSource, DataSource.id == BucketFile.data_source_id)
                .where(
                    BucketFile.id == descriptor["file_id"],
                    DataSource.tenant_id == tenant_id,
                    or_(
                        DataSource.scenario_id.is_(None),
                        DataSource.scenario_id == scenario_id,
                    ),
                )
            ).first()
            if not observed_row:
                raise TemplateCatalogError(
                    f"模板 Action「{descriptor['label']}」引用的模板资源不存在或不在当前租户"
                )
            observed, observed_source = observed_row
            descriptor["source_id"] = observed.data_source_id
            file_ids.add(observed.id)
            source_ids.add(observed_source.id)

    # D: lock both template-source and generated-output buckets, then replace
    # every previously observed object with the refreshed locked row.
    source_rows = db.scalars(
        select(DataSource)
        .where(
            DataSource.id.in_(sorted(source_ids)),
            DataSource.tenant_id == tenant_id,
            or_(
                DataSource.scenario_id.is_(None),
                DataSource.scenario_id == scenario_id,
            ),
        )
        .order_by(DataSource.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    locked_sources = {source.id: source for source in source_rows}
    if set(locked_sources) != source_ids:
        raise TemplateCatalogError("模板 Action 引用的文件桶在校验期间已删除")

    # F: after all D locks, pin the exact source files against deletion/move.
    file_rows = db.scalars(
        select(BucketFile)
        .where(
            BucketFile.id.in_(sorted(file_ids)),
            BucketFile.data_source_id.in_(sorted(locked_sources)),
        )
        .order_by(BucketFile.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    locked_files = {bucket_file.id: bucket_file for bucket_file in file_rows}
    if set(locked_files) != file_ids:
        raise TemplateCatalogError("模板 Action 固定版本的文件在校验期间已删除")

    for descriptor in descriptors:
        label = descriptor["label"]
        target = locked_sources[descriptor["target_id"]]
        if target.tenant_id != tenant_id:
            raise TemplateCatalogError(f"模板 Action「{label}」不能引用其他租户的附件目标")
        if target.scenario_id not in (None, scenario_id):
            raise TemplateCatalogError(f"模板 Action「{label}」附件目标不属于当前业务场景")
        if target.type != "file_bucket":
            raise TemplateCatalogError(f"模板 Action「{label}」附件目标必须是文件桶")
        if (
            authorize_changes
            and target.scenario_id is None
            and descriptor["signature"] != descriptor["previous_signature"]
        ):
            # A scenario editor cannot use governance JSON as an alternate path
            # to create a new write binding into a tenant-shared bucket.
            from . import permission_service

            permission_service.require_tenant_permission(db, "write")

        template_file = locked_files[descriptor["file_id"]]
        source = locked_sources[descriptor["source_id"]]
        if template_file.data_source_id != source.id:
            raise TemplateCatalogError(f"模板 Action「{label}」模板文件已被移动")
        if source.tenant_id != tenant_id or source.type != "file_bucket":
            raise TemplateCatalogError(f"模板 Action「{label}」模板来源文件桶无效")
        if source.scenario_id not in (None, scenario_id):
            raise TemplateCatalogError(f"模板 Action「{label}」模板来源不属于当前业务场景")

        config = descriptor["config"]
        raw_output_filename = config.get("output_filename") or ""
        if not isinstance(raw_output_filename, str):
            raise TemplateCatalogError(f"模板 Action「{label}」输出文件名必须是字符串")
        output_filename = raw_output_filename
        if (
            len(output_filename) > 240
            or "/" in output_filename
            or "\\" in output_filename
        ):
            raise TemplateCatalogError(
                f"模板 Action「{label}」输出文件名不能包含目录且不能超过 240 个字符"
            )
        if output_filename:
            from . import datasource_service

            try:
                datasource_service.validate_bucket_filename(output_filename)
            except ValueError as exc:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」输出文件名无效：{exc}"
                ) from exc
        if descriptor["kind"] == "catalog":
            template, version, _file, _source = resolve_version(
                db,
                template_id=descriptor["template_id"],
                tenant_id=tenant_id,
                scenario_id=scenario_id,
                version_number=descriptor["version_number"],
                expected_sha256=descriptor["sha256"],
                require_active=False,
            )
            if template.id != locked_templates[template.id].id:
                raise TemplateCatalogError("模板 Action 固定目录在校验期间已变化")
            for field, actual in (
                ("template_format", version.artifact_format),
                ("template_mime", version.mime),
                ("template_filename", version.filename),
            ):
                configured = str(config.get(field) or "")
                if configured and configured != str(actual):
                    raise TemplateCatalogError(
                        f"模板 Action「{label}」固定元数据与登记版本不一致"
                    )
            raw_configured_paths = config.get("template_variable_paths") or []
            if not isinstance(raw_configured_paths, list) or any(
                not isinstance(path, str) for path in raw_configured_paths
            ):
                raise TemplateCatalogError(
                    f"模板 Action「{label}」占位符契约必须是字符串列表"
                )
            configured_paths = set(raw_configured_paths)
            expected_paths = set(version.placeholder_paths or [])
            expected_paths.update(
                template_artifact_service.referenced_variable_paths(output_filename)
            )
            if configured_paths != expected_paths:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」占位符契约与登记版本不一致"
                )
            requested_suffix = template_artifact_service.requested_output_suffix(
                output_filename
            )
            if requested_suffix:
                try:
                    requested_format = template_artifact_service.template_format(
                        f"output{requested_suffix}"
                    )[0]
                except template_artifact_service.TemplateArtifactError as exc:
                    raise TemplateCatalogError(
                        f"模板 Action「{label}」输出格式无效：{exc}"
                    ) from exc
                if requested_format != version.artifact_format:
                    raise TemplateCatalogError(
                        f"模板 Action「{label}」输出附件必须与源模板保持相同格式"
                    )
            input_schema = descriptor["action"].get("input_schema") or {}
            try:
                merged_schema = template_artifact_service.merge_template_input_schema(
                    input_schema,
                    expected_paths,
                )
            except template_artifact_service.TemplateArtifactError as exc:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」输入参数与占位符冲突：{exc}"
                ) from exc
            if merged_schema != input_schema:
                raise TemplateCatalogError(
                    f"模板 Action「{label}」输入参数未覆盖全部模板占位符"
                )
        else:
            configured_source_id = str(config.get("template_data_source_id") or "")
            if configured_source_id and configured_source_id != source.id:
                raise TemplateCatalogError(f"模板 Action「{label}」旧式模板来源不一致")
            pinned = template_artifact_service.pinned_template_metadata(
                template_file, source
            )
            configured_sha = str(config.get("template_sha256") or "")
            if configured_sha and configured_sha != pinned["template_sha256"]:
                raise TemplateCatalogError(f"模板 Action「{label}」旧式模板哈希已变化")


_ROLLBACKABLE_SNAPSHOT_KINDS = {
    "baseline", "merge", "rollback", "pre_merge", "pre_rollback",
}
_MERGEABLE_PROPOSAL_STATUSES = {"draft", "submitted", "approved"}


def _governed_snapshots(
    db: Session, tenant_ids: set[str]
) -> list[OntologySnapshot]:
    """Snapshots that can still restore definitions through merge/rollback."""
    proposed_ids = select(OntologyProposal.proposed_snapshot_id).where(
        OntologyProposal.tenant_id.in_(tenant_ids),
        OntologyProposal.status.in_(_MERGEABLE_PROPOSAL_STATUSES),
    )
    return db.scalars(select(OntologySnapshot).where(
        OntologySnapshot.tenant_id.in_(tenant_ids),
        or_(
            OntologySnapshot.kind.in_(_ROLLBACKABLE_SNAPSHOT_KINDS),
            OntologySnapshot.id.in_(proposed_ids),
        ),
    )).all()


def reference_index(
    db: Session, templates: Iterable[ArtifactTemplate]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Build all live/released/governance refs with one scan per backing table."""
    items = list(templates)
    template_by_id = {template.id: template for template in items}
    result = {
        template.id: {"live": [], "released": [], "governance": []}
        for template in items
    }
    if not items:
        return result
    tenant_ids = {template.tenant_id for template in items}
    template_ids = set(result)
    file_to_template_ids: dict[str, set[str]] = {}
    for template in items:
        for version in template.versions:
            file_to_template_ids.setdefault(version.bucket_file_id, set()).add(template.id)
    rows = db.execute(
        select(OntologyAction, BusinessScenario, OntologyEntity)
        .join(BusinessScenario, BusinessScenario.id == OntologyAction.scenario_id)
        .join(OntologyEntity, OntologyEntity.id == OntologyAction.entity_id)
        .where(
            BusinessScenario.tenant_id.in_(tenant_ids),
            OntologyAction.executor_type == "template",
        )
    ).all()
    for action, scenario, entity in rows:
        cfg = action.executor_config or {}
        catalog_id = str(cfg.get("template_id") or "")
        legacy_file_id = str(cfg.get("template_file_id") or "")
        matched = ({catalog_id} if catalog_id in template_ids else set()) | set(
            file_to_template_ids.get(legacy_file_id, set())
        )
        for template_id in matched:
            template = template_by_id[template_id]
            pinned_version: int | None = None
            try:
                if cfg.get("template_version") is not None:
                    pinned_version = int(cfg["template_version"])
                elif legacy_file_id:
                    pinned_version = next(
                        (
                            item.version
                            for item in template.versions
                            if item.bucket_file_id == legacy_file_id
                        ),
                        None,
                    )
            except (TypeError, ValueError):
                pinned_version = None
            result[template_id]["live"].append({
                "action_id": action.id,
                "action_name": action.name,
                "scenario_id": scenario.id,
                "scenario_name": scenario.name,
                "entity_name": entity.name,
                "uses_current": bool(catalog_id == template_id and cfg.get("template_version") is None),
                "pinned_version": pinned_version,
            })

    release_rows = db.execute(
        select(OntologyRelease, OntologySnapshot)
        .join(OntologySnapshot, OntologySnapshot.id == OntologyRelease.snapshot_id)
        .where(
            OntologyRelease.tenant_id.in_(tenant_ids),
            OntologyRelease.status == "released",
        )
    ).all()
    active_release_snapshot_ids = {snapshot.id for _release, snapshot in release_rows}
    for release, snapshot in release_rows:
        for raw_action in (snapshot.content or {}).get("actions") or []:
            if not isinstance(raw_action, dict):
                continue
            cfg = raw_action.get("executor_config") or {}
            if not isinstance(cfg, dict):
                continue
            catalog_id = str(cfg.get("template_id") or "")
            legacy_file_id = str(cfg.get("template_file_id") or "")
            matched = ({catalog_id} if catalog_id in template_ids else set()) | set(
                file_to_template_ids.get(legacy_file_id, set())
            )
            for template_id in matched:
                result[template_id]["released"].append({
                    "release_id": release.id,
                    "environment": release.environment,
                    "scenario_id": release.scenario_id,
                    "snapshot_id": snapshot.id,
                    "action_id": str(raw_action.get("id") or ""),
                    "action_name": str(raw_action.get("name") or ""),
                })
    for snapshot in _governed_snapshots(db, tenant_ids):
        # An active release is already represented above. Every other entry
        # remains a possible merge/rollback source even when its old release
        # record has become superseded or rolled_back.
        if snapshot.id in active_release_snapshot_ids:
            continue
        for raw_action in (snapshot.content or {}).get("actions") or []:
            if not isinstance(raw_action, dict):
                continue
            cfg = raw_action.get("executor_config") or {}
            if not isinstance(cfg, dict):
                continue
            catalog_id = str(cfg.get("template_id") or "")
            legacy_file_id = str(cfg.get("template_file_id") or "")
            matched = ({catalog_id} if catalog_id in template_ids else set()) | set(
                file_to_template_ids.get(legacy_file_id, set())
            )
            for template_id in matched:
                result[template_id]["governance"].append({
                    "scenario_id": snapshot.scenario_id,
                    "snapshot_id": snapshot.id,
                    "snapshot_kind": snapshot.kind,
                    "action_id": str(raw_action.get("id") or ""),
                    "action_name": str(raw_action.get("name") or ""),
                })
    return result


def action_references(
    db: Session, template: ArtifactTemplate
) -> list[dict[str, Any]]:
    return reference_index(db, [template])[template.id]["live"]


def released_snapshot_references(
    db: Session, template: ArtifactTemplate
) -> list[dict[str, Any]]:
    """Find immutable currently released definitions that pin this template."""
    return reference_index(db, [template])[template.id]["released"]


def governance_snapshot_references(
    db: Session, template: ArtifactTemplate
) -> list[dict[str, Any]]:
    """Find merge/rollback-capable historical definitions using a template."""
    return reference_index(db, [template])[template.id]["governance"]


def delete_unreferenced(db: Session, template: ArtifactTemplate) -> None:
    state = reference_index(db, [template])[template.id]
    if state["live"]:
        raise TemplateCatalogError("模板仍被 Action 引用，请先解除引用")
    if state["released"]:
        raise TemplateCatalogError("模板仍被当前发布快照引用，不能删除")
    if state["governance"]:
        raise TemplateCatalogError("模板仍被可合并或可回滚的治理快照引用，不能删除")
    # The binary remains in its user-managed bucket. Cascading removes only
    # immutable catalog metadata, after which the ordinary file API may delete it.
    template.current_version_id = None
    db.flush()
    db.delete(template)


def assert_bucket_files_not_registered(
    db: Session, bucket_file_ids: Iterable[str]
) -> None:
    ids = {str(value) for value in bucket_file_ids if value}
    if not ids:
        return
    row = db.execute(
        select(ArtifactTemplateVersion, ArtifactTemplate)
        .join(ArtifactTemplate, ArtifactTemplate.id == ArtifactTemplateVersion.template_id)
        .where(ArtifactTemplateVersion.bucket_file_id.in_(ids))
        .limit(1)
    ).first()
    if row:
        _version, template = row
        raise TemplateCatalogError(
            f"文件已登记为模板“{template.name}”，请先从模板中心解除 Action 引用并删除模板"
        )
    tenant_ids = {
        str(tenant_id)
        for tenant_id in db.scalars(
            select(DataSource.tenant_id)
            .join(BucketFile, BucketFile.data_source_id == DataSource.id)
            .where(BucketFile.id.in_(ids))
        ).all()
    }
    for config in _template_reference_configs(db, tenant_ids):
        if str(config.get("template_file_id") or "") in ids:
            raise TemplateCatalogError(
                "文件仍被旧式模板 Action 或可发布、可回滚快照引用，不能删除"
            )


def _template_reference_configs(
    db: Session, tenant_ids: set[str]
) -> list[dict[str, Any]]:
    """Return live and restorable template configs inside the tenant boundary."""
    if not tenant_ids:
        return []
    configs: list[dict[str, Any]] = []
    actions = db.scalars(
        select(OntologyAction)
        .join(BusinessScenario, BusinessScenario.id == OntologyAction.scenario_id)
        .where(
            BusinessScenario.tenant_id.in_(tenant_ids),
            OntologyAction.executor_type == "template",
        )
    ).all()
    configs.extend(
        dict(action.executor_config or {})
        for action in actions
        if isinstance(action.executor_config or {}, dict)
    )
    release_snapshots = db.scalars(
        select(OntologySnapshot)
        .join(OntologyRelease, OntologyRelease.snapshot_id == OntologySnapshot.id)
        .where(
            OntologyRelease.tenant_id.in_(tenant_ids),
            OntologyRelease.status == "released",
        )
    ).all()
    for snapshot in [*release_snapshots, *_governed_snapshots(db, tenant_ids)]:
        for raw_action in (snapshot.content or {}).get("actions") or []:
            if not isinstance(raw_action, dict):
                continue
            config = raw_action.get("executor_config") or {}
            if isinstance(config, dict):
                configs.append(config)
    return configs


def assert_data_source_not_registered(db: Session, data_source_id: str) -> None:
    registered = db.execute(
        select(ArtifactTemplateVersion, ArtifactTemplate)
        .join(BucketFile, BucketFile.id == ArtifactTemplateVersion.bucket_file_id)
        .join(ArtifactTemplate, ArtifactTemplate.id == ArtifactTemplateVersion.template_id)
        .where(BucketFile.data_source_id == data_source_id)
        .limit(1)
    ).first()
    if registered:
        _version, template = registered
        raise TemplateCatalogError(
            f"文件桶含已登记模板“{template.name}”，请先从模板中心解除 Action 引用并删除模板"
        )
    source = db.get(DataSource, data_source_id)
    if not source:
        return
    configs = _template_reference_configs(db, {str(source.tenant_id)})
    if any(
        str(config.get("target_data_source_id") or "")
        == data_source_id
        for config in configs
    ):
        raise TemplateCatalogError("文件桶仍被模板 Action 用作附件目标，不能变更或删除")
    legacy_file_ids = {
        str(config.get("template_file_id") or "")
        for config in configs
        if config.get("template_file_id")
    }
    actual_sources = dict(
        db.execute(
            select(BucketFile.id, BucketFile.data_source_id)
            .join(DataSource, DataSource.id == BucketFile.data_source_id)
            .where(
                BucketFile.id.in_(legacy_file_ids),
                DataSource.tenant_id == source.tenant_id,
            )
        ).all()
    )
    for config in configs:
        legacy_file_id = str(config.get("template_file_id") or "")
        if not legacy_file_id:
            continue
        actual_source_id = actual_sources.get(legacy_file_id)
        configured_source_id = str(config.get("template_data_source_id") or "")
        if (
            actual_source_id == data_source_id
            or (actual_source_id is None and configured_source_id == data_source_id)
        ):
            raise TemplateCatalogError(
                "文件桶仍被旧式模板 Action 或可发布、可回滚快照用作模板来源，不能变更或删除"
            )


def prepare_scenario_deletion(
    db: Session, scenario: BusinessScenario
) -> None:
    """Remove scenario-owned catalog metadata inside the scenario transaction.

    Its live Actions and rollback history are deleted by the same scenario
    cascade, so requiring users to delete the template first would create an
    impossible governance cycle. Cross-scenario references still fail closed.
    """
    templates = db.scalars(
        select(ArtifactTemplate)
        .options(selectinload(ArtifactTemplate.versions))
        .where(
            ArtifactTemplate.tenant_id == scenario.tenant_id,
            ArtifactTemplate.scenario_id == scenario.id,
        )
        .with_for_update()
    ).all()
    reference_state = reference_index(db, templates)
    for template in templates:
        external = [
            reference
            for kind in ("live", "released", "governance")
            for reference in reference_state[template.id][kind]
            if str(reference.get("scenario_id") or "") != scenario.id
        ]
        if external:
            raise TemplateCatalogError(
                "场景模板仍被其他业务场景引用，不能随当前场景删除"
            )
        template.current_version_id = None
    db.flush()
    for template in templates:
        db.delete(template)
    db.flush()


def _deterministic_id(kind: str, *parts: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, ":".join((kind, *parts))).hex


def migrate_legacy_template_actions(db: Session) -> int:
    """Idempotently catalog and pin legacy ``template_file_id`` Actions."""
    migrated = 0
    actions = db.scalars(
        select(OntologyAction)
        .where(OntologyAction.executor_type == "template")
        .order_by(OntologyAction.scenario_id, OntologyAction.id)
    ).all()
    candidates = [
        action
        for action in actions
        if not (
            (action.executor_config or {}).get("template_id")
            and (action.executor_config or {}).get("template_version") is not None
            and str((action.executor_config or {}).get("template_sha256") or "")
        )
    ]
    # Rolling application instances may execute this migration concurrently
    # with online scenario deletion or template writes. Lock every affected
    # scenario first, globally sorted, before any catalog/source/file access.
    scenario_ids = sorted({action.scenario_id for action in candidates})
    observed_scenarios = db.scalars(
        select(BusinessScenario)
        .where(BusinessScenario.id.in_(scenario_ids))
        .order_by(BusinessScenario.tenant_id, BusinessScenario.id)
    ).all()
    scenario_ids_by_tenant: dict[str, list[str]] = {}
    for scenario in observed_scenarios:
        scenario_ids_by_tenant.setdefault(str(scenario.tenant_id), []).append(
            scenario.id
        )
    locked_scenarios: dict[str, BusinessScenario] = {}
    for tenant_id in sorted(scenario_ids_by_tenant):
        locked_scenarios.update(
            lock_scenarios_for_template_write(
                db,
                tenant_id=tenant_id,
                scenario_ids=scenario_ids_by_tenant[tenant_id],
            )
        )
    candidate_ids = [action.id for action in candidates]
    if candidate_ids:
        # The first read is only for discovering which S rows to lock. An
        # online Action save may have completed while this process waited for
        # S, so lock and refresh A afterwards and re-evaluate candidacy before
        # writing any executor_config. This prevents startup from restoring a
        # stale legacy config over a newly pinned or re-bound Action.
        refreshed_actions = db.scalars(
            select(OntologyAction)
            .where(
                OntologyAction.id.in_(candidate_ids),
                OntologyAction.executor_type == "template",
            )
            .order_by(OntologyAction.scenario_id, OntologyAction.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
        candidates = [
            action
            for action in refreshed_actions
            if action.scenario_id in locked_scenarios
            and not (
                (action.executor_config or {}).get("template_id")
                and (action.executor_config or {}).get("template_version") is not None
                and str((action.executor_config or {}).get("template_sha256") or "")
            )
        ]
    for action in candidates:
        scenario = locked_scenarios.get(action.scenario_id)
        if not scenario or not scenario.tenant_id:
            continue
        cfg = dict(action.executor_config or {})
        if cfg.get("template_id"):
            try:
                template, version, _file, _source = resolve_version(
                    db,
                    template_id=str(cfg["template_id"]),
                    tenant_id=scenario.tenant_id,
                    scenario_id=scenario.id,
                    version_number=(
                        int(cfg["template_version"])
                        if cfg.get("template_version") is not None else None
                    ),
                    expected_sha256=str(cfg.get("template_sha256") or ""),
                    require_active=False,
                    lock_template=True,
                )
            except (TemplateCatalogError, TypeError, ValueError):
                continue
        else:
            file_id = str(cfg.get("template_file_id") or "")
            template_file = db.get(BucketFile, file_id)
            if not template_file:
                continue
            template_source = db.get(DataSource, template_file.data_source_id)
            if not template_source:
                continue
            version = db.scalar(
                select(ArtifactTemplateVersion)
                .join(ArtifactTemplate, ArtifactTemplate.id == ArtifactTemplateVersion.template_id)
                .where(
                    ArtifactTemplate.tenant_id == scenario.tenant_id,
                    ArtifactTemplate.scenario_id == scenario.id,
                    ArtifactTemplateVersion.bucket_file_id == template_file.id,
                )
            )
            if version:
                template = db.get(ArtifactTemplate, version.template_id)
                if not template:
                    continue
            else:
                template_id = _deterministic_id(
                    "artifact-template", scenario.tenant_id, scenario.id, template_file.id
                )
                version_id = _deterministic_id(
                    "artifact-template-version", template_id, template_file.id
                )
                try:
                    template = create_from_bucket_file(
                        db,
                        tenant_id=scenario.tenant_id,
                        template_file=template_file,
                        template_source=template_source,
                        scenario_id=scenario.id,
                        name=Path(template_file.filename).stem,
                        purpose=action.name,
                        description=f"由既有 Action“{action.name}”自动登记。",
                        key=f"legacy_{template_id[:16]}",
                        version_note="从既有模板 Action 迁移",
                        stable_template_id=template_id,
                        stable_version_id=version_id,
                    )
                    version = template.current_version
                    if version is None:
                        version = db.get(ArtifactTemplateVersion, template.current_version_id)
                except TemplateCatalogError:
                    continue
        if not version:
            continue
        output_filename = str(cfg.get("output_filename") or "")
        extra_paths = template_artifact_service.referenced_variable_paths(output_filename)
        action.executor_config = pinned_action_config(
            template,
            version,
            target_data_source_id=str(cfg.get("target_data_source_id") or ""),
            output_filename=output_filename,
            variable_paths=extra_paths,
        )
        migrated += 1
    db.flush()
    return migrated
