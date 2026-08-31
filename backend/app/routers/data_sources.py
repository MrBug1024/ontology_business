"""数据源路由：数据库连接 + 文件桶上传/解析。"""
from __future__ import annotations

from contextlib import nullcontext
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BucketFile, DataSource
from ..schemas import (
    BucketFileOut,
    DataSourceIn,
    DataSourceOut,
    DocumentReindexOut,
    DocumentSearchIn,
    DocumentSearchOut,
    Msg,
    QueryResult,
    TableInfo,
)
from ..services import (
    connector_service,
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    rag_service,
    scenario_model_draft_service,
    template_catalog_service,
    tenant_service,
)
from ..config import get_settings
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/data-sources", tags=["data-sources"])

_SECRET_KEYS = {"password", "api_key", "token", "secret", "access_token"}


def _public_config(config: dict) -> dict:
    """返回可给前端展示的配置，凭据字段永不回显。"""
    safe = dict(config or {})
    for key in _SECRET_KEYS:
        if key in safe:
            safe[key] = ""
    return safe


def _merge_config(old: dict, new: dict) -> dict:
    """编辑数据源时，空凭据表示保持原值，避免前端必须读取密钥。"""
    merged = dict(new or {})
    for key in _SECRET_KEYS:
        if not merged.get(key):
            if key in old:
                merged[key] = old[key]
    return merged


def _out(ds: DataSource, db: Session) -> DataSourceOut:
    return DataSourceOut(
        id=ds.id,
        scenario_id=ds.scenario_id,
        name=ds.name,
        type=ds.type,
        config=_public_config(ds.config or {}),
        status=ds.status,
        last_error=ds.last_error,
        created_at=ds.created_at,
        file_count=len(ds.files),
        can_write=(
            ds.type != "dataset"
            and
            ds.tenant_id == tenant_service.current_tenant_id(db)
            and _can_access_data_source(db, ds, writable=True)
        ),
        can_delete=(
            ds.tenant_id == tenant_service.current_tenant_id(db)
            and _can_access_data_source(db, ds, writable=True)
        ),
    )


def _require_data_source_access(
    db: Session,
    ds: DataSource,
    *,
    writable: bool = False,
) -> DataSource:
    """Apply the owning scenario's ACL to every data-source entry point.

    ``tenant_service`` protects tenant/public ownership, but a source bound to a
    scenario inherits that scenario's explicit allow/deny rules.  Keeping this
    check next to the generic source lookup prevents file, SQL and RAG routes
    from accidentally becoming alternate paths around the scenario workspace.
    """
    if ds.scenario_id:
        scenario = tenant_service.require_scenario(db, ds.scenario_id, writable=writable)
        permission_service.require_scenario_permission(
            db,
            scenario,
            "write" if writable else "read",
            message="没有该数据源所属业务场景的权限",
        )
    else:
        permission_service.require_tenant_permission(db, "write" if writable else "read")
    return ds


def _can_access_data_source(db: Session, ds: DataSource, *, writable: bool = False) -> bool:
    """List endpoints must hide inaccessible sources instead of leaking names."""
    try:
        _require_data_source_access(db, ds, writable=writable)
    except HTTPException:
        return False
    return True


def _data_source(db: Session, ds_id: str, writable: bool = False) -> DataSource:
    if writable:
        ds = db.scalar(
            select(DataSource)
            .where(
                DataSource.id == ds_id,
                DataSource.tenant_id == tenant_service.current_tenant_id(db),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if not ds:
            raise HTTPException(404, "数据源不存在")
    else:
        ds = tenant_service.require_visible(db, DataSource, ds_id, "数据源不存在")
    return _require_data_source_access(db, ds, writable=writable)


@router.get("", response_model=list[DataSourceOut])
def list_data_sources(scenario_id: str | None = None, db: Session = Depends(get_tenant_db)):
    if scenario_id:
        scenario = tenant_service.require_scenario(db, scenario_id)
        permission_service.require_scenario_permission(db, scenario, "read")
    stmt = select(DataSource).where(tenant_service.visible_clause(DataSource, db))
    stmt = stmt.where(DataSource.resource_scope == "modeling")
    if scenario_id:
        stmt = stmt.where(DataSource.scenario_id == scenario_id)
    return [
        _out(d, db)
        for d in db.execute(stmt).scalars().all()
        if _can_access_data_source(db, d)
    ]


@router.post("", response_model=DataSourceOut)
def create_data_source(payload: DataSourceIn, db: Session = Depends(get_tenant_db)):
    if payload.type == "dataset":
        raise HTTPException(400, "版本化数据集只能由平台摄取流程创建")
    if payload.scenario_id:
        scenario = tenant_service.require_scenario(db, payload.scenario_id, writable=True)
        permission_service.require_scenario_permission(db, scenario, "write")
    else:
        permission_service.require_tenant_permission(db, "write")
    try:
        template_catalog_service.lock_scenarios_for_template_write(
            db,
            tenant_id=tenant_service.current_tenant_id(db),
            scenario_ids=[payload.scenario_id],
        )
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(409, str(exc)) from exc
    values = payload.model_dump()
    if values.get("type") == "file_bucket":
        try:
            values["config"] = datasource_service.normalize_file_bucket_config(
                values.get("config")
            )
        except object_storage_service.ObjectStorageError as exc:
            raise HTTPException(503, str(exc)) from exc
    ds = DataSource(
        tenant_id=tenant_service.current_tenant_id(db),
        resource_scope="modeling",
        owner_agent_id=None,
        **values,
    )
    if ds.type == "file_bucket":
        try:
            datasource_service.ensure_file_bucket_storage(ds)
        except object_storage_service.ObjectStorageError as exc:
            raise HTTPException(503, str(exc)) from exc
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return _out(ds, db)


@router.put("/{ds_id}", response_model=DataSourceOut)
def update_data_source(ds_id: str, payload: DataSourceIn, db: Session = Depends(get_tenant_db)):
    observed = tenant_service.require_visible(
        db, DataSource, ds_id, "数据源不存在"
    )
    if observed.type == "dataset" or payload.type == "dataset":
        raise HTTPException(409, "版本化数据集只能通过数据资产目录发布和切换")
    _require_data_source_access(db, observed, writable=True)
    if payload.scenario_id:
        scenario = tenant_service.require_scenario(db, payload.scenario_id, writable=True)
        permission_service.require_scenario_permission(db, scenario, "write")
    else:
        permission_service.require_tenant_permission(db, "write")
    try:
        template_catalog_service.lock_scenarios_for_template_write(
            db,
            tenant_id=tenant_service.current_tenant_id(db),
            scenario_ids=[observed.scenario_id, payload.scenario_id],
        )
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(409, str(exc)) from exc
    observed_scope = observed.scenario_id
    ds = _data_source(db, ds_id, writable=True)
    if ds.scenario_id != observed_scope:
        raise HTTPException(409, "数据源场景归属在更新期间已变化，请刷新后重试")
    if payload.type != ds.type or payload.scenario_id != ds.scenario_id:
        has_bucket_files = db.scalar(
            select(BucketFile.id)
            .where(BucketFile.data_source_id == ds.id)
            .limit(1)
        ) is not None
        if has_bucket_files:
            raise HTTPException(
                status_code=409,
                detail="已有文件的数据源不能变更类型或场景归属，请先删除文件",
            )
        try:
            template_catalog_service.assert_data_source_not_registered(db, ds.id)
        except template_catalog_service.TemplateCatalogError as exc:
            raise HTTPException(
                status_code=409,
                detail="已登记模板所在文件桶不能变更类型或场景归属，请先在模板中心解除引用并删除模板",
            ) from exc
    values = payload.model_dump()
    values["config"] = _merge_config(ds.config or {}, values.get("config", {}))
    if values.get("type") == "file_bucket":
        try:
            values["config"] = datasource_service.normalize_file_bucket_config(
                values.get("config")
            )
        except object_storage_service.ObjectStorageError as exc:
            raise HTTPException(503, str(exc)) from exc
    for k, v in values.items():
        setattr(ds, k, v)
    if ds.type == "file_bucket":
        try:
            datasource_service.ensure_file_bucket_storage(ds)
        except (ValueError, object_storage_service.ObjectStorageError) as exc:
            raise HTTPException(503, str(exc)) from exc
    datasource_service.invalidate_engine(ds)
    ds.status = "unknown"
    connector_service.invalidate_connector_bindings(db, "data_source", ds.id)
    db.commit()
    db.refresh(ds)
    return _out(ds, db)


@router.delete("/{ds_id}", response_model=Msg)
def delete_data_source(ds_id: str, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id, writable=True)
    try:
        connector_service.assert_connector_not_bound(db, "data_source", ds.id)
    except connector_service.ConnectorBindingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        template_catalog_service.assert_data_source_not_registered(db, ds.id)
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    bucket_files = list(ds.files)
    try:
        deletion_job_ids = [
            object_deletion_service.enqueue_bucket_file_deletion(
                db, bucket_file, ds
            )
            for bucket_file in bucket_files
        ]
        catalog_cleanup = datasource_service.detach_platform_catalog_references_for_deletion(
            db,
            ds,
            [bucket_file.id for bucket_file in bucket_files],
        )
    except (ValueError, object_storage_service.ObjectStorageError) as exc:
        raise HTTPException(409, str(exc)) from exc
    datasource_service.invalidate_engine(ds)
    db.delete(ds)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "数据源在删除期间被业务资源引用，请刷新后重试") from exc
    object_deletion_service.drain_jobs_best_effort(db, deletion_job_ids)
    if bucket_files:
        return Msg(
            message=f"已删除建模资料，{len(bucket_files)} 个托管文件已进入 MinIO 清理队列",
            data={
                "data_source_id": ds_id,
                "files_deleted": len(bucket_files),
                "cleanup_jobs": len(deletion_job_ids),
                **catalog_cleanup,
            },
        )
    return Msg(
        message="已删除建模资料",
        data={
            "data_source_id": ds_id,
            "files_deleted": 0,
            "cleanup_jobs": 0,
        },
    )


@router.post("/{ds_id}/test", response_model=Msg)
def test_data_source(ds_id: str, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id, writable=True)
    if ds.type == "file_bucket":
        try:
            datasource_service.ensure_file_bucket_storage(ds)
        except (ValueError, object_storage_service.ObjectStorageError) as exc:
            ds.status = "error"
            ds.last_error = str(exc)
            db.commit()
            return Msg(ok=False, message=str(exc))
        ds.status = "ok"
        ds.last_error = ""
        db.flush()
        scenario_model_draft_service.auto_repair_data_source_drafts(
            db,
            ds,
            validated_source_id=ds.id,
        )
        db.commit()
        return Msg(ok=True, message="文件桶就绪")
    ok, msg = datasource_service.test_connection(ds)
    # Defense in depth: a future driver adapter or a mocked service must not
    # cause a credential-bearing error to become an API response or persisted
    # data-source status.
    msg = msg if ok else datasource_service.CONNECTION_TEST_FAILURE_MESSAGE
    ds.status = "ok" if ok else "error"
    ds.last_error = "" if ok else msg
    if ok:
        db.flush()
        scenario_model_draft_service.auto_repair_data_source_drafts(
            db,
            ds,
            validated_source_id=ds.id,
        )
    db.commit()
    return Msg(ok=ok, message=msg)


@router.get("/{ds_id}/tables", response_model=list[TableInfo])
def list_tables(ds_id: str, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id)
    if ds.type == "file_bucket":
        return []
    try:
        return datasource_service.list_tables(ds)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取表结构失败: {exc}")


@router.post("/{ds_id}/query", response_model=QueryResult)
def query(ds_id: str, payload: dict, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id)
    sql = payload.get("sql", "")
    if not sql.strip():
        raise HTTPException(400, "SQL 不能为空")
    try:
        return datasource_service.run_query(ds, sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"查询失败: {exc}")


@router.post("/search", response_model=DocumentSearchOut)
def search_documents(payload: DocumentSearchIn, db: Session = Depends(get_tenant_db)):
    """在当前租户可见的文件桶中执行混合向量检索。

    资料库可见性在此和 ``rag_service`` 中双重执行，确保 Agent、页面和
    未来外部 API 进入同一个检索边界。
    """
    if payload.scenario_id:
        scenario = tenant_service.require_scenario(db, payload.scenario_id)
        permission_service.require_scenario_permission(db, scenario, "read")
    stmt = select(DataSource).where(
        DataSource.type == "file_bucket",
        tenant_service.visible_clause(DataSource, db),
    )
    if payload.data_source_ids:
        stmt = stmt.where(DataSource.id.in_(payload.data_source_ids))
    if payload.scenario_id:
        stmt = stmt.where(
            or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == payload.scenario_id)
        )
    sources = [
        source
        for source in db.execute(stmt).scalars().all()
        if _can_access_data_source(db, source)
    ]
    source_ids = [source.id for source in sources]
    requested_ids = set(payload.data_source_ids or [])
    excluded_ids = sorted(requested_ids - set(source_ids))
    try:
        results = rag_service.search(db, source_ids, payload.query, top_k=payload.top_k)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(500, f"资料检索失败: {exc}") from exc
    return DocumentSearchOut(
        query=payload.query,
        results=results,
        searched_data_source_ids=source_ids,
        excluded_data_source_ids=excluded_ids,
        permission_message=(
            "部分指定资料库不在当前访问范围，已自动排除。" if excluded_ids else ""
        ),
    )


# ── 文件桶 ────────────────────────────────────
@router.post("/{ds_id}/reindex", response_model=DocumentReindexOut)
def reindex_files(ds_id: str, db: Session = Depends(get_tenant_db)):
    """显式排队重建资料库索引，适用于历史文件或模型版本升级后。"""
    ds = _data_source(db, ds_id, writable=True)
    try:
        result = rag_service.enqueue_data_source_reindex(db, ds, force=True)
        db.commit()
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"重建检索索引失败: {exc}") from exc


@router.get("/{ds_id}/files", response_model=list[BucketFileOut])
def list_files(ds_id: str, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id)
    if ds.type != "file_bucket":
        return []
    return list(ds.files)


@router.post("/{ds_id}/files", response_model=list[BucketFileOut])
async def upload_files(ds_id: str, files: list[UploadFile] = File(...), db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id, writable=True)
    if ds.type != "file_bucket":
        raise HTTPException(400, "该数据源不是文件桶")
    created: list[BucketFile] = []
    max_upload_bytes = get_settings().max_upload_bytes
    for uf in files:
        content = await uf.read(max_upload_bytes + 1)
        if len(content) > max_upload_bytes:
            raise HTTPException(413, f"文件超过大小限制（{max_upload_bytes // (1024 * 1024)} MB）")
        filename = uf.filename or "file"
        file_id = uuid.uuid4().hex
        upload_claim = None
        if datasource_service.is_managed_minio_source(ds):
            try:
                upload_claim = object_deletion_service.prepare_bucket_file_upload(
                    ds, file_id, filename
                )
            except Exception as exc:  # noqa: BLE001 - expose no DB/MinIO details.
                raise HTTPException(503, "无法建立文件上传事务") from exc
        try:
            heartbeat = (
                object_deletion_service.heartbeat_upload_intent(upload_claim)
                if upload_claim is not None
                else nullcontext()
            )
            with heartbeat as active_heartbeat:
                if upload_claim is not None:
                    object_deletion_service.begin_upload_put(upload_claim)
                bf = datasource_service.save_bucket_file(
                    ds,
                    filename,
                    content,
                    stable_file_id=file_id if upload_claim is not None else None,
                    upload_object_key=(
                        upload_claim.object_key
                        if upload_claim is not None
                        else None
                    ),
                )
                if upload_claim is not None:
                    object_deletion_service.assert_upload_active(
                        active_heartbeat, upload_claim, bf
                    )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
            raise HTTPException(503, str(exc)) from exc
        db.add(bf)
        try:
            if upload_claim is not None:
                object_deletion_service.retain_bucket_file_upload(
                    db, upload_claim, bf, ds
                )
            db.flush()
            # 文件已可靠写入对象存储；解析与 embedding 由可恢复的后台任务处理。
            rag_service.enqueue_document_index(db, bf, parse_document=True)
            db.commit()
        except Exception as exc:
            db.rollback()
            if (
                upload_claim is not None
                and isinstance(
                    exc,
                    object_deletion_service.UploadIntentLeaseLostError,
                )
            ):
                object_deletion_service.schedule_abandoned_upload_best_effort(
                    upload_claim,
                    bf,
                )
            elif upload_claim is None:
                try:
                    datasource_service.delete_bucket_file(bf, ds)
                except Exception:  # noqa: BLE001 - preserve the database failure.
                    pass
            raise
        db.refresh(bf)
        created.append(bf)
    return created


@router.post("/files/{file_id}/reparse", response_model=BucketFileOut)
def reparse_file(file_id: str, db: Session = Depends(get_tenant_db)):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    _data_source(db, bf.data_source_id, writable=True)
    bf.status = "pending"
    bf.error = ""
    bf.parsed_text = ""
    rag_service.enqueue_document_index(db, bf, parse_document=True, force=True)
    db.commit()
    db.refresh(bf)
    return bf


@router.get("/files/{file_id}/text")
def file_text(file_id: str, db: Session = Depends(get_tenant_db)):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    _data_source(db, bf.data_source_id)
    return {"filename": bf.filename, "text": bf.parsed_text}


@router.get("/files/{file_id}/download")
def file_download(file_id: str, db: Session = Depends(get_tenant_db)):
    """下载文件桶中的文件（附件）。"""
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    ds = _data_source(db, bf.data_source_id)
    try:
        content, actual_size, media_type = datasource_service.read_bucket_file(bf, ds)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文件已丢失") from exc
    except ValueError as exc:
        raise HTTPException(409, f"附件完整性校验失败: {exc}") from exc
    except object_storage_service.ObjectStorageError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(bf.filename, safe="")
            ),
            "Content-Length": str(actual_size),
        },
    )


@router.delete("/files/{file_id}", response_model=Msg)
def delete_file(file_id: str, db: Session = Depends(get_tenant_db)):
    observed = db.get(BucketFile, file_id)
    if not observed:
        raise HTTPException(404, "文件不存在")
    source = _data_source(db, observed.data_source_id, writable=True)
    bf = db.scalar(
        select(BucketFile)
        .where(
            BucketFile.id == file_id,
            BucketFile.data_source_id == source.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if not bf:
        raise HTTPException(404, "文件不存在")
    try:
        template_catalog_service.assert_bucket_files_not_registered(db, [bf.id])
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        deletion_job_id = object_deletion_service.enqueue_bucket_file_deletion(
            db, bf, source
        )
        catalog_cleanup = datasource_service.detach_platform_catalog_references_for_deletion(
            db, source, [bf.id]
        )
    except (ValueError, object_storage_service.ObjectStorageError) as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(bf)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "文件在删除期间被登记为模板，请刷新后重试") from exc
    object_deletion_service.drain_jobs_best_effort(db, [deletion_job_id])
    return Msg(
        message="已删除",
        data={"file_id": file_id, "cleanup_job": deletion_job_id, **catalog_cleanup},
    )
