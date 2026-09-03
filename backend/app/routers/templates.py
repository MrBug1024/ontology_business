"""Tenant-isolated management API for reusable artifact templates."""
from __future__ import annotations

from contextlib import nullcontext
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    DataSource,
)
from ..schemas import (
    ArtifactTemplateDetailOut,
    ArtifactTemplateReferenceOut,
    ArtifactTemplateRegisterIn,
    ArtifactTemplateSummaryOut,
    ArtifactTemplateUpdateIn,
    ArtifactTemplateVersionOut,
    ArtifactTemplateVersionRegisterIn,
    Msg,
)
from ..services import (
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    template_artifact_service,
    template_catalog_service,
    tenant_service,
    upload_service,
)
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/templates", tags=["templates"])


def _scenario_access(db: Session, scenario_id: str, *, writable: bool) -> None:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=writable)
    permission_service.require_scenario_permission(
        db, scenario, "write" if writable else "read"
    )


def _lock_template_scenarios(
    db: Session, scenario_ids: list[str | None]
) -> dict[str, object]:
    try:
        return template_catalog_service.lock_scenarios_for_template_write(
            db,
            tenant_id=tenant_service.current_tenant_id(db),
            scenario_ids=scenario_ids,
        )
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(409, str(exc)) from exc


def _observe_source_for_template_write(db: Session, data_source_id: str) -> DataSource:
    source = tenant_service.require_owned(
        db, DataSource, data_source_id, "文件桶不存在"
    )
    if source.scenario_id:
        _scenario_access(db, source.scenario_id, writable=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    if source.type != "file_bucket":
        raise HTTPException(400, "模板必须存放在文件桶数据源")
    return source


def _observe_file_source_for_template_write(
    db: Session, file_id: str
) -> tuple[BucketFile, DataSource]:
    row = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.id == file_id,
            DataSource.tenant_id == tenant_service.current_tenant_id(db),
        )
    ).first()
    if not row:
        raise HTTPException(404, "文件不存在")
    bucket_file, source = row
    if source.scenario_id:
        _scenario_access(db, source.scenario_id, writable=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    if source.type != "file_bucket":
        raise HTTPException(400, "模板必须存放在文件桶数据源")
    return bucket_file, source


def _template(
    db: Session,
    template_id: str,
    *,
    writable: bool = False,
    additional_scenario_ids: list[str | None] | None = None,
) -> ArtifactTemplate:
    tenant_id = tenant_service.current_tenant_id(db)
    observed = template_catalog_service.get_owned(
        db, template_id, tenant_id, for_update=False
    )
    if writable and observed:
        # Authorize before taking a row lock, then establish the global
        # S -> T -> D -> F order and reject a concurrent scope change.
        if observed.scenario_id:
            _scenario_access(db, observed.scenario_id, writable=True)
        else:
            permission_service.require_tenant_permission(db, "write")
        observed_scope = observed.scenario_id
        _lock_template_scenarios(
            db,
            [observed_scope, *(additional_scenario_ids or [])],
        )
        item = template_catalog_service.get_owned(
            db, template_id, tenant_id, for_update=True
        )
        if item and item.scenario_id != observed_scope:
            raise HTTPException(
                409, "模板场景归属在写入期间已变化，请刷新后重试"
            )
    else:
        item = observed
    if not item:
        raise HTTPException(404, "模板不存在")
    if item.scenario_id:
        _scenario_access(db, item.scenario_id, writable=writable)
    else:
        permission_service.require_tenant_permission(
            db, "write" if writable else "read"
        )
    return item


def _source(db: Session, data_source_id: str, *, writable: bool) -> DataSource:
    if writable:
        source = db.scalar(
            select(DataSource)
            .where(
                DataSource.id == data_source_id,
                DataSource.tenant_id == tenant_service.current_tenant_id(db),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if not source:
            raise HTTPException(404, "文件桶不存在")
    else:
        source = tenant_service.require_owned(
            db, DataSource, data_source_id, "文件桶不存在"
        )
    if source.scenario_id:
        _scenario_access(db, source.scenario_id, writable=writable)
    else:
        permission_service.require_tenant_permission(
            db, "write" if writable else "read"
        )
    if source.type != "file_bucket":
        raise HTTPException(400, "模板必须存放在文件桶数据源")
    return source


def _file(db: Session, file_id: str, *, writable: bool) -> tuple[BucketFile, DataSource]:
    observed = db.get(BucketFile, file_id)
    if not observed:
        raise HTTPException(404, "文件不存在")
    source = _source(db, observed.data_source_id, writable=writable)
    bucket_file = (
        db.scalar(
            select(BucketFile)
            .where(
                BucketFile.id == file_id,
                BucketFile.data_source_id == source.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if writable
        else observed
    )
    if not bucket_file:
        raise HTTPException(404, "文件不存在")
    if bucket_file.data_source_id != source.id:
        raise HTTPException(409, "文件在操作期间已移动，请刷新后重试")
    return bucket_file, source


def _catalog_error(exc: Exception, *, conflict: bool = False) -> HTTPException:
    return HTTPException(409 if conflict else 400, str(exc))


def _remove_uncommitted_upload(db: Session, bucket_file: BucketFile) -> None:
    """Delete bytes only when no committed catalog version owns the upload."""
    db.rollback()
    persisted_version = db.scalar(select(ArtifactTemplateVersion.id).where(
        ArtifactTemplateVersion.bucket_file_id == bucket_file.id
    ).limit(1))
    if not persisted_version and not datasource_service.is_managed_minio_file(
        bucket_file
    ):
        source = db.get(DataSource, bucket_file.data_source_id)
        if source is None:
            raise RuntimeError("未提交模板文件所属数据源不存在")
        datasource_service.delete_bucket_file(bucket_file, source)


def _save_template_upload(
    db: Session,
    source: DataSource,
    filename: str,
    staged: upload_service.StagedUpload,
    *,
    mime: str,
) -> BucketFile:
    file_id = uuid.uuid4().hex
    claim = None
    if datasource_service.is_managed_minio_source(source):
        claim = object_deletion_service.prepare_bucket_file_upload(
            source, file_id, filename
        )
    heartbeat = (
        object_deletion_service.heartbeat_upload_intent(claim)
        if claim is not None
        else nullcontext()
    )
    with heartbeat as active_heartbeat:
        if claim is not None:
            object_deletion_service.begin_upload_put(claim)
        saved = datasource_service.save_bucket_file_from_path(
            source,
            filename,
            staged.path,
            size=staged.size,
            content_sha256=staged.sha256,
            mime=mime,
            stable_file_id=file_id if claim is not None else None,
            upload_object_key=claim.object_key if claim is not None else None,
        )
        if claim is not None:
            object_deletion_service.assert_upload_active(
                active_heartbeat, claim, saved
            )
    db.add(saved)
    if claim is not None:
        try:
            object_deletion_service.retain_bucket_file_upload(
                db, claim, saved, source
            )
        except object_deletion_service.UploadIntentLeaseLostError:
            db.rollback()
            object_deletion_service.schedule_abandoned_upload_best_effort(
                claim,
                saved,
            )
            raise
    return saved


def _version_out(db: Session, version: ArtifactTemplateVersion) -> ArtifactTemplateVersionOut:
    bucket_file = version.bucket_file
    return ArtifactTemplateVersionOut(
        id=version.id,
        version=version.version,
        bucket_file_id=version.bucket_file_id,
        data_source_id=bucket_file.data_source_id if bucket_file else "",
        filename=version.filename,
        artifact_format=version.artifact_format,
        mime=version.mime,
        size=version.size,
        sha256=version.content_sha256,
        placeholder_paths=list(version.placeholder_paths or []),
        metadata=dict(version.template_metadata or {}),
        version_note=version.version_note,
        created_at=version.created_at,
    )


def _visible_references(
    db: Session,
    template: ArtifactTemplate,
    references: list[dict],
    scenario_visibility: dict[str, bool] | None = None,
) -> list[ArtifactTemplateReferenceOut]:
    scenario_visibility = scenario_visibility if scenario_visibility is not None else {}
    visible: list[ArtifactTemplateReferenceOut] = []
    for ref in references:
        scenario_id = str(ref["scenario_id"])
        if scenario_id not in scenario_visibility:
            try:
                _scenario_access(db, scenario_id, writable=False)
                scenario_visibility[scenario_id] = True
            except HTTPException:
                scenario_visibility[scenario_id] = False
        if not scenario_visibility[scenario_id]:
            continue
        visible.append(ArtifactTemplateReferenceOut(**ref))
    return visible


def _out(
    db: Session,
    template: ArtifactTemplate,
    *,
    detail: bool,
    reference_state: dict[str, list[dict]] | None = None,
    scenario_visibility: dict[str, bool] | None = None,
) -> ArtifactTemplateSummaryOut | ArtifactTemplateDetailOut:
    versions = sorted(template.versions, key=lambda item: item.version)
    current = next(
        (item for item in versions if item.id == template.current_version_id), None
    )
    if reference_state is None:
        reference_state = template_catalog_service.reference_index(db, [template])[template.id]
    references = list(reference_state.get("live") or [])
    release_references = list(reference_state.get("released") or [])
    governance_references = list(reference_state.get("governance") or [])
    scenario_visibility = scenario_visibility if scenario_visibility is not None else {}
    visible_references = _visible_references(
        db, template, references, scenario_visibility
    )
    visible_release_count = 0
    for reference in release_references:
        ref_scenario_id = str(reference["scenario_id"])
        if ref_scenario_id not in scenario_visibility:
            try:
                _scenario_access(db, ref_scenario_id, writable=False)
                scenario_visibility[ref_scenario_id] = True
            except HTTPException:
                scenario_visibility[ref_scenario_id] = False
        if not scenario_visibility[ref_scenario_id]:
            continue
        visible_release_count += 1
    visible_governance_count = 0
    for reference in governance_references:
        ref_scenario_id = str(reference["scenario_id"])
        if ref_scenario_id not in scenario_visibility:
            try:
                _scenario_access(db, ref_scenario_id, writable=False)
                scenario_visibility[ref_scenario_id] = True
            except HTTPException:
                scenario_visibility[ref_scenario_id] = False
        if scenario_visibility[ref_scenario_id]:
            visible_governance_count += 1
    common = dict(
        id=template.id,
        key=template.key,
        scenario_id=template.scenario_id,
        name=template.name,
        purpose=template.purpose,
        description=template.description,
        status=template.status,
        current_version_id=template.current_version_id,
        current_version=_version_out(db, current) if current else None,
        version_count=len(versions),
        # Hidden scenario activity is never leaked as a count. ``deletable``
        # still reflects every real reference so lifecycle safety is preserved.
        reference_count=(
            len(visible_references)
            + visible_release_count
            + visible_governance_count
        ),
        deletable=(
            not references and not release_references and not governance_references
        ),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )
    if not detail:
        return ArtifactTemplateSummaryOut(**common)
    return ArtifactTemplateDetailOut(
        **common,
        versions=[_version_out(db, version) for version in reversed(versions)],
        references=visible_references,
    )


def _create(
    db: Session,
    *,
    template_file: BucketFile,
    source: DataSource,
    scenario_id: str | None,
    name: str,
    purpose: str,
    description: str,
    key: str,
    version_note: str,
    inspection: template_artifact_service.TemplateInspection | None = None,
) -> ArtifactTemplateDetailOut:
    if scenario_id:
        _scenario_access(db, scenario_id, writable=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    try:
        template = template_catalog_service.create_from_bucket_file(
            db,
            tenant_id=tenant_service.current_tenant_id(db),
            template_file=template_file,
            template_source=source,
            scenario_id=scenario_id,
            name=name,
            purpose=purpose,
            description=description,
            key=key,
            version_note=version_note,
            created_by_user_id=str(db.info.get("user_id") or "") or None,
            inspection=inspection,
        )
        db.commit()
        db.refresh(template)
        return _out(db, template, detail=True)
    except template_catalog_service.TemplateCatalogError as exc:
        db.rollback()
        raise _catalog_error(exc, conflict="已存在" in str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "模板标识或版本发生并发冲突，请刷新后重试") from exc


@router.get("", response_model=list[ArtifactTemplateSummaryOut])
def list_templates(
    scenario_id: str | None = None,
    status: str | None = None,
    artifact_format: str | None = None,
    q: str = "",
    db: Session = Depends(get_tenant_db),
):
    if status not in (None, "active", "deprecated"):
        raise HTTPException(400, "模板状态筛选值无效")
    if artifact_format not in (None, "docx", "xlsx", "markdown"):
        raise HTTPException(400, "模板格式筛选值无效")
    tenant_id = tenant_service.current_tenant_id(db)
    stmt = select(ArtifactTemplate).options(
        selectinload(ArtifactTemplate.versions).selectinload(
            ArtifactTemplateVersion.bucket_file
        )
    ).where(ArtifactTemplate.tenant_id == tenant_id)
    if scenario_id:
        _scenario_access(db, scenario_id, writable=False)
        # A scenario workspace sees its own templates plus tenant-shared ones.
        stmt = stmt.where(or_(
            ArtifactTemplate.scenario_id.is_(None),
            ArtifactTemplate.scenario_id == scenario_id,
        ))
    else:
        permission_service.require_tenant_permission(db, "read")
    if status:
        stmt = stmt.where(ArtifactTemplate.status == status)
    search = q.strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(
            ArtifactTemplate.name.ilike(pattern),
            ArtifactTemplate.key.ilike(pattern),
            ArtifactTemplate.purpose.ilike(pattern),
            ArtifactTemplate.description.ilike(pattern),
        ))
    items = db.scalars(stmt.order_by(
        ArtifactTemplate.updated_at.desc(), ArtifactTemplate.id
    )).unique().all()
    reference_index = template_catalog_service.reference_index(db, items)
    output: list[ArtifactTemplateSummaryOut] = []
    # The filtered scenario was already authorized above. Reuse that result,
    # and cache any additional scenario ACL decision once for the whole list.
    scenario_visibility: dict[str, bool] = (
        {scenario_id: True} if scenario_id else {}
    )
    for item in items:
        if item.scenario_id:
            if item.scenario_id not in scenario_visibility:
                try:
                    _scenario_access(db, item.scenario_id, writable=False)
                    scenario_visibility[item.scenario_id] = True
                except HTTPException:
                    scenario_visibility[item.scenario_id] = False
            if not scenario_visibility[item.scenario_id]:
                continue
        current = next(
            (version for version in item.versions if version.id == item.current_version_id),
            None,
        )
        if artifact_format and (
            current is None or current.artifact_format != artifact_format
        ):
            continue
        output.append(
            _out(
                db,
                item,
                detail=False,
                reference_state=reference_index[item.id],
                scenario_visibility=scenario_visibility,
            )
        )
    return output


@router.post("/register", response_model=ArtifactTemplateDetailOut)
def register_template(
    payload: ArtifactTemplateRegisterIn,
    db: Session = Depends(get_tenant_db),
):
    if payload.scenario_id:
        _scenario_access(db, payload.scenario_id, writable=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    _observed_file, observed_source = _observe_file_source_for_template_write(
        db, payload.file_id
    )
    _lock_template_scenarios(
        db, [payload.scenario_id, observed_source.scenario_id]
    )
    bucket_file, source = _file(db, payload.file_id, writable=True)
    return _create(
        db,
        template_file=bucket_file,
        source=source,
        scenario_id=payload.scenario_id,
        name=payload.name,
        purpose=payload.purpose,
        description=payload.description,
        key=payload.key,
        version_note=payload.version_note,
    )


@router.post("/upload", response_model=ArtifactTemplateDetailOut)
async def upload_template(
    file: UploadFile = File(...),
    data_source_id: str = Form(...),
    name: str = Form(...),
    scenario_id: str | None = Form(None),
    purpose: str = Form(""),
    description: str = Form(""),
    key: str = Form(""),
    version_note: str = Form(""),
    db: Session = Depends(get_tenant_db),
):
    if scenario_id:
        _scenario_access(db, scenario_id, writable=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    observed_source = _observe_source_for_template_write(db, data_source_id)
    _lock_template_scenarios(db, [scenario_id, observed_source.scenario_id])
    source = _source(db, data_source_id, writable=True)
    max_bytes = get_settings().max_upload_bytes
    try:
        staged = await upload_service.stage_upload(
            file,
            max_bytes=max_bytes,
        )
    except upload_service.UploadTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    filename = file.filename or "template"
    with upload_service.cleanup_staged_upload(staged):
        try:
            inspection = template_artifact_service.inspect_template_file(
                filename,
                staged.path,
                size=staged.size,
                sha256=staged.sha256,
            )
        except (ValueError, template_artifact_service.TemplateArtifactError) as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            saved = _save_template_upload(
                db,
                source,
                filename,
                staged,
                mime=str(inspection.metadata["mime"]),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
            raise HTTPException(503, "模板对象存储写入失败") from exc
        try:
            db.flush()
            return _create(
                db,
                template_file=saved,
                source=source,
                scenario_id=scenario_id,
                name=name,
                purpose=purpose,
                description=description,
                key=key,
                version_note=version_note,
                inspection=inspection,
            )
        except Exception:
            _remove_uncommitted_upload(db, saved)
            raise


@router.get("/{template_id}", response_model=ArtifactTemplateDetailOut)
def get_template(template_id: str, db: Session = Depends(get_tenant_db)):
    return _out(db, _template(db, template_id), detail=True)


@router.put("/{template_id}", response_model=ArtifactTemplateDetailOut)
def update_template(
    template_id: str,
    payload: ArtifactTemplateUpdateIn,
    db: Session = Depends(get_tenant_db),
):
    values = payload.model_dump(exclude_unset=True)
    if "scenario_id" in values:
        if values["scenario_id"]:
            _scenario_access(db, values["scenario_id"], writable=True)
        else:
            permission_service.require_tenant_permission(db, "write")
    template = _template(
        db,
        template_id,
        writable=True,
        additional_scenario_ids=[values.get("scenario_id")]
        if "scenario_id" in values
        else [],
    )
    if "scenario_id" in values and values["scenario_id"] != template.scenario_id:
        new_scenario_id = values["scenario_id"]
        if new_scenario_id:
            _scenario_access(db, new_scenario_id, writable=True)
        else:
            permission_service.require_tenant_permission(db, "write")
        reference_state = template_catalog_service.reference_index(db, [template])[template.id]
        incompatible_references = [
            ref
            for ref in (
                list(reference_state.get("live") or [])
                + list(reference_state.get("released") or [])
                + list(reference_state.get("governance") or [])
            )
            if new_scenario_id is not None
            and str(ref.get("scenario_id") or "") != new_scenario_id
        ]
        if incompatible_references:
            raise HTTPException(
                409,
                "模板仍被其他业务场景的 Action 或发布快照引用，不能收窄或变更场景归属",
            )
        bucket_files = {
            bucket_file.id: bucket_file
            for bucket_file in db.scalars(
                select(BucketFile).where(
                    BucketFile.id.in_(
                        [version.bucket_file_id for version in template.versions]
                    )
                )
            ).all()
        }
        source_ids = {
            bucket_file.data_source_id for bucket_file in bucket_files.values()
        }
        source_rows = db.scalars(
            select(DataSource)
            .where(
                DataSource.id.in_(sorted(source_ids)),
                DataSource.tenant_id == template.tenant_id,
            )
            .order_by(DataSource.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
        locked_sources = {source.id: source for source in source_rows}
        if set(locked_sources) != source_ids:
            raise HTTPException(409, "模板版本文件或文件桶已丢失")
        for version in template.versions:
            bucket_file = bucket_files.get(version.bucket_file_id)
            source = (
                locked_sources.get(bucket_file.data_source_id)
                if bucket_file
                else None
            )
            if not source:
                raise HTTPException(409, "模板版本文件或文件桶已丢失")
            try:
                template_catalog_service._validate_source_scope(  # noqa: SLF001
                    source,
                    tenant_id=template.tenant_id,
                    scenario_id=new_scenario_id,
                )
            except template_catalog_service.TemplateCatalogError as exc:
                raise HTTPException(409, f"不能变更模板归属：{exc}") from exc
        template.scenario_id = new_scenario_id
    if "key" in values:
        try:
            normalized_key = template_catalog_service.normalize_key(values["key"])
        except template_catalog_service.TemplateCatalogError as exc:
            raise HTTPException(400, str(exc)) from exc
        duplicate = db.scalar(select(ArtifactTemplate.id).where(
            ArtifactTemplate.tenant_id == template.tenant_id,
            ArtifactTemplate.key == normalized_key,
            ArtifactTemplate.id != template.id,
        ))
        if duplicate:
            raise HTTPException(409, "模板标识在当前租户内已存在")
        template.key = normalized_key
    for field in ("name", "purpose", "description"):
        if field in values:
            normalized = str(values[field] or "").strip()
            if field == "name" and not normalized:
                raise HTTPException(400, "模板名称不能为空")
            setattr(template, field, normalized)
    if "current_version_id" in values:
        version_id = values["current_version_id"]
        version = db.get(ArtifactTemplateVersion, version_id) if version_id else None
        if not version or version.template_id != template.id:
            raise HTTPException(400, "当前版本不属于该模板")
        template.current_version_id = version.id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "模板标识发生并发冲突，请刷新后重试") from exc
    db.refresh(template)
    return _out(db, template, detail=True)


def _add_version(
    db: Session,
    template: ArtifactTemplate,
    bucket_file: BucketFile,
    source: DataSource,
    *,
    version_note: str,
    set_current: bool,
    inspection: template_artifact_service.TemplateInspection | None = None,
) -> ArtifactTemplateDetailOut:
    try:
        template_catalog_service.add_version_from_bucket_file(
            db,
            template,
            template_file=bucket_file,
            template_source=source,
            version_note=version_note,
            set_current=set_current,
            created_by_user_id=str(db.info.get("user_id") or "") or None,
            inspection=inspection,
        )
        db.commit()
        db.refresh(template)
        return _out(db, template, detail=True)
    except template_catalog_service.TemplateCatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "模板版本发生并发冲突，请刷新后重试") from exc


@router.post("/{template_id}/versions/register", response_model=ArtifactTemplateDetailOut)
def register_template_version(
    template_id: str,
    payload: ArtifactTemplateVersionRegisterIn,
    db: Session = Depends(get_tenant_db),
):
    _observed_file, observed_source = _observe_file_source_for_template_write(
        db, payload.file_id
    )
    template = _template(
        db,
        template_id,
        writable=True,
        additional_scenario_ids=[observed_source.scenario_id],
    )
    bucket_file, source = _file(db, payload.file_id, writable=True)
    return _add_version(
        db,
        template,
        bucket_file,
        source,
        version_note=payload.version_note,
        set_current=payload.set_current,
    )


@router.post("/{template_id}/versions/upload", response_model=ArtifactTemplateDetailOut)
async def upload_template_version(
    template_id: str,
    file: UploadFile = File(...),
    data_source_id: str = Form(...),
    version_note: str = Form(""),
    set_current: bool = Form(True),
    db: Session = Depends(get_tenant_db),
):
    observed_source = _observe_source_for_template_write(db, data_source_id)
    template = _template(
        db,
        template_id,
        writable=True,
        additional_scenario_ids=[observed_source.scenario_id],
    )
    if template.status != "active":
        raise HTTPException(409, "已停用模板不能新增版本，请先恢复模板")
    source = _source(db, data_source_id, writable=True)
    max_bytes = get_settings().max_upload_bytes
    try:
        staged = await upload_service.stage_upload(
            file,
            max_bytes=max_bytes,
        )
    except upload_service.UploadTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    filename = file.filename or "template"
    with upload_service.cleanup_staged_upload(staged):
        try:
            inspection = template_artifact_service.inspect_template_file(
                filename,
                staged.path,
                size=staged.size,
                sha256=staged.sha256,
            )
        except (ValueError, template_artifact_service.TemplateArtifactError) as exc:
            raise HTTPException(400, str(exc)) from exc

        existing = db.scalar(select(ArtifactTemplateVersion).where(
            ArtifactTemplateVersion.template_id == template.id,
            ArtifactTemplateVersion.content_sha256 == str(inspection.metadata["sha256"]),
        ))
        if existing:
            try:
                template_catalog_service.resolve_version(
                    db,
                    template_id=template.id,
                    tenant_id=template.tenant_id,
                    scenario_id=template.scenario_id or "",
                    version_number=existing.version,
                    expected_sha256=existing.content_sha256,
                    require_active=False,
                )
            except template_catalog_service.TemplateCatalogError as exc:
                raise HTTPException(409, f"既有同内容版本完整性校验失败：{exc}") from exc
            if set_current:
                template.current_version_id = existing.id
                db.commit()
            return _out(db, template, detail=True)

        try:
            saved = _save_template_upload(
                db,
                source,
                filename,
                staged,
                mime=str(inspection.metadata["mime"]),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
            raise HTTPException(503, "模板对象存储写入失败") from exc
        try:
            db.flush()
            return _add_version(
                db,
                template,
                saved,
                source,
                version_note=version_note,
                set_current=set_current,
                inspection=inspection,
            )
        except Exception:
            _remove_uncommitted_upload(db, saved)
            raise


@router.post("/{template_id}/deprecate", response_model=ArtifactTemplateDetailOut)
def deprecate_template(template_id: str, db: Session = Depends(get_tenant_db)):
    template = _template(db, template_id, writable=True)
    template.status = "deprecated"
    db.commit()
    db.refresh(template)
    return _out(db, template, detail=True)


@router.post("/{template_id}/activate", response_model=ArtifactTemplateDetailOut)
def activate_template(template_id: str, db: Session = Depends(get_tenant_db)):
    template = _template(db, template_id, writable=True)
    template.status = "active"
    db.commit()
    db.refresh(template)
    return _out(db, template, detail=True)


@router.delete("/{template_id}", response_model=Msg)
def delete_template(template_id: str, db: Session = Depends(get_tenant_db)):
    template = _template(db, template_id, writable=True)
    try:
        template_catalog_service.delete_unreferenced(db, template)
        db.commit()
    except template_catalog_service.TemplateCatalogError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return Msg(message="模板目录记录已删除，原文件仍保留在文件桶")
