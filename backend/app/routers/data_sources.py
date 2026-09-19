"""数据源路由：数据库连接 + 文件桶上传/解析。"""
from __future__ import annotations

from contextlib import nullcontext
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    ManagedUploadRun,
)
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
    catalog_service,
    catalog_ingestion_service,
    connector_service,
    datasource_service,
    modeling_contract_source_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    rag_service,
    managed_attachment_access,
    scenario_model_draft_service,
    template_catalog_service,
    tenant_service,
    upload_staging_service,
    library_credential_service,
    library_database_service,
    library_sqlite_upload_service,
)
from ..config import get_settings
from ..services.auth_service import get_tenant_db
from ..services.library_mysql_adapter import LibraryConfigurationError

router = APIRouter(prefix="/data-sources", tags=["data-sources"])

def _public_config(config: dict) -> dict:
    """返回可给前端展示的配置，凭据字段永不回显。"""
    return library_credential_service.public_config(config or {})


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
            ds.type not in {"dataset", "distillation"}
            and
            ds.tenant_id == tenant_service.current_tenant_id(db)
            and _can_access_data_source(db, ds, writable=True)
        ),
        can_delete=(
            ds.type != "distillation"
            and
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


def _data_source(
    db: Session,
    ds_id: str,
    writable: bool = False,
    *,
    allow_runtime: bool = False,
) -> DataSource:
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
    # Runtime connectors are an Agent execution boundary, not modeling
    # material.  Only the scoped file helper below may open one; keeping this
    # default closed prevents guessed ids from turning legacy tables/query/RAG
    # endpoints into a second Agent data path.
    if ds.resource_scope == "agent_runtime" and not allow_runtime:
        raise HTTPException(404, "数据源不存在")
    _require_data_source_access(db, ds, writable=writable)
    if writable and ds.type == "distillation":
        raise HTTPException(409, "业务蒸馏交接资料不可变，请在蒸馏项目中创建新版本")
    return ds


def _file_ownership(
    db: Session,
    bucket_file: BucketFile,
) -> tuple[set[str], bool, bool]:
    """Return durable Agent owners and whether the file is a tombstone.

    The external upload bucket is intentionally shared, so its DataSource
    owner is NULL.  Ownership lives on the catalog asset and upload run; every
    file endpoint must inspect those rows instead of trusting the bucket.
    Multiple distinct owners or a deleted lifecycle are treated fail-closed.
    """

    source = db.get(DataSource, bucket_file.data_source_id)
    if source is None:
        return set(), True, False
    owner_ids: set[str] = set()
    tombstoned = False
    private_purpose = False
    versions = db.execute(
        select(DataAsset.owner_agent_id, DataAsset.lifecycle_status, DataAsset.labels)
        .join(DataAssetVersion, DataAssetVersion.asset_id == DataAsset.id)
        .where(
            DataAssetVersion.bucket_file_id == bucket_file.id,
            DataAssetVersion.bucket_data_source_id == source.id,
            DataAsset.tenant_id == source.tenant_id,
        )
    ).all()
    for owner_agent_id, lifecycle_status, labels in versions:
        if owner_agent_id:
            owner_ids.add(str(owner_agent_id))
        if (
            isinstance(labels, dict)
            and str(labels.get("catalog_purpose") or "").strip().lower()
            in managed_attachment_access.AGENT_PRIVATE_PURPOSES
        ):
            private_purpose = True
        if lifecycle_status == "retired" or (
            isinstance(labels, dict) and labels.get("lifecycle") == "agent_deleted"
        ):
            # A retired modeling asset can still be an audit reference, but an
            # Agent-deletion tombstone must never become readable again.
            if isinstance(labels, dict) and labels.get("lifecycle") == "agent_deleted":
                tombstoned = True

    runs = db.scalars(
        select(ManagedUploadRun).where(
            ManagedUploadRun.bucket_file_id == bucket_file.id,
            ManagedUploadRun.data_source_id == source.id,
            ManagedUploadRun.tenant_id == source.tenant_id,
        )
    ).all()
    for run in runs:
        if run.owner_agent_id:
            owner_ids.add(str(run.owner_agent_id))
        if (
            str(run.purpose or "").strip().lower()
            in managed_attachment_access.AGENT_PRIVATE_PURPOSES
        ):
            private_purpose = True
        if run.error_code == "agent_deleted":
            tombstoned = True
    return owner_ids, tombstoned, private_purpose


def _agent_for_file_scope(
    db: Session,
    source: DataSource,
    *,
    agent_id: str | None,
) -> Agent | None:
    """Validate an explicit Agent context without revealing cross-scope rows."""

    requested = str(agent_id or "").strip() or None
    if requested and len(requested) > 32:
        raise HTTPException(404, "文件不存在")
    if requested is None:
        return None
    tenant_id = tenant_service.current_tenant_id(db)
    agent = db.scalar(
        select(Agent).where(
            Agent.id == requested,
            Agent.tenant_id == tenant_id,
        )
    )
    if agent is None or source.tenant_id != tenant_id:
        raise HTTPException(404, "文件不存在")
    if source.scenario_id and source.scenario_id != agent.scenario_id:
        raise HTTPException(404, "文件不存在")
    # A valid Agent id is not itself an ACL grant.  The shared external upload
    # bucket has no scenario id, so this explicit check is the only place the
    # file URL can inherit the Agent's scenario authorization before the
    # per-file ownership check runs.
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id)
        permission_service.require_scenario_permission(
            db,
            scenario,
            "read",
            message="没有该 Agent 所属业务场景的权限",
        )
    else:
        permission_service.require_tenant_permission(db, "read")
    return agent


def _authorize_file_scope(
    db: Session,
    bucket_file: BucketFile,
    *,
    agent_id: str | None = None,
    writable: bool = False,
) -> DataSource:
    """Authorize a file and its transitive Agent ownership.

    Agent-owned catalog files in the shared external bucket require the exact
    Agent id.  Ownerless global files remain available to the tenant-level
    assistant without an Agent id.  Runtime sources with a direct owner always
    require that same owner, and modeling sources retain their ACL behavior.
    """

    source = db.get(DataSource, bucket_file.data_source_id)
    if source is None or source.type != "file_bucket":
        raise HTTPException(404, "文件不存在")
    requested = str(agent_id or "").strip() or None
    agent = _agent_for_file_scope(db, source, agent_id=requested)
    owner_ids, tombstoned, private_purpose = _file_ownership(db, bucket_file)
    if tombstoned:
        raise HTTPException(404, "文件不存在")
    source_owner = str(source.owner_agent_id or "").strip() or None
    if source_owner:
        owner_ids.add(source_owner)
    if private_purpose and (
        len(owner_ids) != 1 or requested not in owner_ids
    ):
        # Private upload purposes never inherit the tenant-wide ownerless
        # compatibility namespace, even when a legacy row lost its owner.
        raise HTTPException(404, "文件不存在")
    if owner_ids:
        # A file must have one unambiguous owner.  This also prevents a shared
        # or malformed row from being used as a bridge between Agents.
        if len(owner_ids) != 1 or requested not in owner_ids:
            raise HTTPException(404, "文件不存在")
    elif source.resource_scope == "agent_runtime":
        # Runtime sources are not a public modeling namespace.  The only
        # ownerless exception is the tenant-level external upload bucket used
        # by the Global Assistant (scenario_id is NULL).
        if source.scenario_id is not None or requested is not None:
            raise HTTPException(404, "文件不存在")
    elif agent is not None:
        # A scoped URL for a modeling file still needs a real Agent context;
        # the source's scenario ACL remains authoritative below.
        pass

    return _data_source(
        db,
        source.id,
        writable=writable,
        allow_runtime=True,
    )


def _authorize_source_scope(
    db: Session,
    source: DataSource,
    *,
    agent_id: str | None = None,
    writable: bool = False,
) -> DataSource:
    """Authorize a whole file bucket before listing or accepting files."""

    requested = str(agent_id or "").strip() or None
    if requested:
        _agent_for_file_scope(db, source, agent_id=requested)
    if source.resource_scope == "agent_runtime":
        source_owner = str(source.owner_agent_id or "").strip() or None
        if requested is None or (source_owner and source_owner != requested):
            raise HTTPException(404, "数据源不存在")
    return _data_source(
        db,
        source.id,
        writable=writable,
        allow_runtime=True,
    )


def _file_visible_in_scope(
    db: Session,
    bucket_file: BucketFile,
    *,
    agent_id: str | None,
) -> bool:
    try:
        _authorize_file_scope(db, bucket_file, agent_id=agent_id)
    except HTTPException:
        return False
    return True


def _file_source_for_request(
    db: Session,
    bucket_file: BucketFile,
    *,
    agent_id: str | None = None,
) -> DataSource:
    """Resolve a file's source while preserving Agent attachment isolation.

    Modeling files intentionally keep the legacy tenant/scenario ACL path, so
    old links without ``agent_id`` remain valid.  Agent runtime sources are a
    different security domain: they are private to their owning Agent and
    therefore require an explicit, existing Agent context.  The scope check is
    performed before the normal ACL lookup so a caller cannot use a scenario
    permission error as an oracle for another Agent's file.
    """
    return _authorize_file_scope(db, bucket_file, agent_id=agent_id)


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
    try:
        values["config"] = library_database_service.write_config(payload.type, payload.config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except object_storage_service.ObjectStorageError as exc:
        raise HTTPException(503, "托管存储暂不可用") from exc
    ds = DataSource(
        tenant_id=tenant_service.current_tenant_id(db),
        resource_scope="modeling",
        owner_agent_id=None,
        **values,
    )
    if ds.type in {"file_bucket", "sqlite3"}:
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
    if observed.type == "distillation":
        raise HTTPException(409, "业务蒸馏交接资料不可变，请在蒸馏项目中创建新版本")
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
    type_changed = payload.type != ds.type
    scenario_changed = payload.scenario_id != ds.scenario_id
    if type_changed:
        has_bucket_files = db.scalar(
            select(BucketFile.id)
            .where(BucketFile.data_source_id == ds.id)
            .limit(1)
        ) is not None
        if has_bucket_files:
            raise HTTPException(
                status_code=409,
                detail="已有文件的建模资料不能变更类型，请先删除文件",
            )
    if type_changed or scenario_changed:
        try:
            template_catalog_service.assert_data_source_not_registered(db, ds.id)
        except template_catalog_service.TemplateCatalogError as exc:
            raise HTTPException(
                status_code=409,
                detail="已登记模板所在文件桶不能变更类型或建模场景，请先在模板中心解除引用并删除模板",
            ) from exc
    values = payload.model_dump()
    if scenario_changed and ds.type == "sqlite3" and ds.files:
        raise HTTPException(409, "已有快照的 SQLite3 资料库不能改变场景归属，请创建新的资料库")
    try:
        values["config"] = library_database_service.write_config(payload.type, payload.config,
            old_config=ds.config if not type_changed else None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except object_storage_service.ObjectStorageError as exc:
        raise HTTPException(503, "托管存储暂不可用") from exc
    for k, v in values.items():
        setattr(ds, k, v)
    if ds.type in {"file_bucket", "sqlite3"}:
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
        modeling_contract_source_service.retire_for_data_source_deletion(db, ds)
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
    if ds.type in library_database_service.DATABASE_TYPES:
        frozen = library_database_service.snapshot(ds)
        revision = ds.connector_revision
        db.commit()
        try:
            library_database_service.database_schema(frozen)
            ok, msg = True, "连接成功，资料结构可读取"
        except LibraryConfigurationError as exc:
            ok, msg = False, str(exc)
        except Exception:  # External driver/storage boundary; never return diagnostics.
            ok, msg = False, "连接或结构读取失败，请检查配置、只读权限和部署允许名单"
        permission_service.refresh_request_authorization(db)
        ds = _data_source(db, ds_id, writable=True)
        if ds.connector_revision != revision:
            raise HTTPException(409, "测试期间资料库已变化，请重试")
        ds.status, ds.last_error = ("ok", "") if ok else ("error", msg)
        db.commit()
        return Msg(ok=ok, message=msg)
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
    if ds.type in {"file_bucket", "distillation"}:
        return []
    if ds.type in library_database_service.DATABASE_TYPES:
        frozen = library_database_service.snapshot(ds)
        revision = ds.connector_revision
        db.commit()
        try:
            tables = library_database_service.database_schema(frozen)
        except LibraryConfigurationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(422, "资料结构读取失败、超时或超过 40 表/80 列上限，请检查配置或缩小资料范围") from exc
        permission_service.refresh_request_authorization(db)
        current = _data_source(db, ds_id)
        if current.connector_revision != revision:
            raise HTTPException(409, "读取期间资料库已变化，请重试")
        return tables
    try:
        return datasource_service.list_tables(ds)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取表结构失败: {exc}")


@router.post("/{ds_id}/query", response_model=QueryResult)
def query(ds_id: str, payload: dict, db: Session = Depends(get_tenant_db)):
    ds = _data_source(db, ds_id)
    if ds.type == "distillation":
        raise HTTPException(422, "业务蒸馏资料只用于建模理解，不是运行数据库")
    if ds.type in {"mysql", "sqlite3"}:
        raise HTTPException(422, "该资料库只支持结构调查，不接受任意 SQL")
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
        # Agent runtime attachments are a separate, owner-scoped execution
        # namespace.  The generic modeling search endpoint has no Agent
        # context, so never hand runtime source ids to RAG (even when a caller
        # guesses a valid tenant-local id).
        DataSource.resource_scope == "modeling",
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
def _sqlite_upload_source(db: Session, ds_id: str) -> DataSource:
    source = _data_source(db, ds_id, writable=True)
    if source.type != "sqlite3":
        raise HTTPException(422, "请选择 SQLite3 资料库")
    frozen = library_database_service.snapshot(source)
    db.commit()
    return frozen


@router.post("/{ds_id}/sqlite-file", response_model=BucketFileOut)
async def upload_sqlite_file(ds_id: str, file: UploadFile = File(...), db: Session = Depends(get_tenant_db)):
    frozen = await run_in_threadpool(_sqlite_upload_source, db, ds_id)
    from ..services.library_sqlite_adapter import MAX_SQLITE_BYTES
    filename = file.filename or "snapshot.sqlite3"
    # Release the authorization lock before receiving/parsing/PUT. The service
    # rechecks revision, membership, ACL and ownership under a final row lock.
    try:
        staged = await upload_staging_service.stage_upload(file, max_bytes=MAX_SQLITE_BYTES,
            chunk_bytes=int(get_settings().upload_stream_chunk_bytes))
    except upload_staging_service.UploadTooLargeError as exc:
        raise HTTPException(413, "SQLite3 快照不能超过 32 MB") from exc
    except ValueError as exc:
        raise HTTPException(422, "SQLite3 快照不能为空") from exc
    try:
        return await run_in_threadpool(library_sqlite_upload_service.attach_snapshot, db, frozen, staged, filename)
    except HTTPException:
        raise
    except Exception as exc:  # Bounded parser/storage boundary.
        raise HTTPException(422, "SQLite3 快照上传失败，请检查文件格式和存储状态后重试") from exc
    finally:
        staged.remove()


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
def list_files(
    ds_id: str,
    agent_id: str | None = Query(default=None, min_length=1, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    observed = db.get(DataSource, ds_id)
    if observed is None:
        raise HTTPException(404, "数据源不存在")
    ds = _authorize_source_scope(db, observed, agent_id=agent_id)
    if ds.type != "file_bucket":
        return []
    files = [
        item
        for item in ds.files
        if _file_visible_in_scope(db, item, agent_id=agent_id)
    ]
    refs = modeling_contract_source_service.refs_for_bucket_files(db, ds, files)
    for item in files:
        ref = refs.get(item.id)
        if ref is not None:
            item.modeling_contract_dataset_id = ref.dataset_id
            item.modeling_contract_schema_id = ref.schema_id
    return files


@router.post("/{ds_id}/files", response_model=list[BucketFileOut])
async def upload_files(
    ds_id: str,
    files: list[UploadFile] = File(...),
    agent_id: str | None = Query(default=None, min_length=1, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    observed = db.get(DataSource, ds_id)
    if observed is None:
        raise HTTPException(404, "数据源不存在")
    ds = _authorize_source_scope(db, observed, agent_id=agent_id, writable=True)
    if ds.type != "file_bucket":
        raise HTTPException(400, "该数据源不是文件桶")
    if ds.resource_scope == "agent_runtime":
        # Runtime attachments must go through the catalog upload protocol so
        # the resulting DataAsset and upload run receive the exact Agent owner.
        raise HTTPException(409, "验证附件请使用 Agent 附件上传入口")
    created: list[BucketFile] = []
    settings = get_settings()
    for uf in files:
        filename = uf.filename or "file"
        content_type = uf.content_type
        try:
            staged = await upload_staging_service.stage_upload(
                uf,
                max_bytes=int(settings.catalog_max_upload_bytes),
                chunk_bytes=int(settings.upload_stream_chunk_bytes),
            )
        except upload_staging_service.UploadTooLargeError as exc:
            raise HTTPException(
                413,
                "文件超过大小限制（"
                f"{int(settings.catalog_max_upload_bytes) // (1024 * 1024)} MB）",
            ) from exc
        file_id = uuid.uuid4().hex
        staged_digest = staged.content_sha256
        table_file = False
        table_profile: dict | None = None
        table_profile_text = ""
        try:
            profiled = catalog_ingestion_service.build_tabular_profile_path(
                staged.path, filename, content_type
            )
            if profiled is not None:
                _media_type, table_profile = profiled
                table_file = True
                table_profile_text = catalog_ingestion_service.profile_summary_text(
                    table_profile, filename
                )
        except ValueError as exc:
            staged.remove()
            raise HTTPException(400, str(exc)) from exc
        upload_claim = None
        if datasource_service.is_managed_minio_source(ds):
            try:
                upload_claim = object_deletion_service.prepare_bucket_file_upload(
                    ds, file_id, filename
                )
            except Exception as exc:  # noqa: BLE001 - expose no DB/MinIO details.
                staged.remove()
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
                if staged.byte_size <= int(settings.catalog_in_memory_upload_bytes):
                    bf = datasource_service.save_bucket_file(
                        ds,
                        filename,
                        staged.path.read_bytes(),
                        mime=content_type,
                        stable_file_id=file_id if upload_claim is not None else None,
                        upload_object_key=(
                            upload_claim.object_key if upload_claim is not None else None
                        ),
                    )
                else:
                    bf = datasource_service.save_bucket_file_path(
                        ds,
                        filename,
                        staged.path,
                        mime=content_type,
                        stable_file_id=file_id if upload_claim is not None else None,
                        upload_object_key=(
                            upload_claim.object_key if upload_claim is not None else None
                        ),
                        content_sha256=staged.content_sha256,
                    )
                if upload_claim is not None:
                    object_deletion_service.assert_upload_active(
                        active_heartbeat, upload_claim, bf
                    )
        except ValueError as exc:
            staged.remove()
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
            staged.remove()
            raise HTTPException(503, str(exc)) from exc
        finally:
            staged.remove()
        db.add(bf)
        try:
            if upload_claim is not None:
                object_deletion_service.retain_bucket_file_upload(
                    db, upload_claim, bf, ds
                )
            db.flush()
            # Only schema metadata is indexed for modeling. Business rows are
            # queried from MinIO-backed datasets and never copied into PG.
            if table_file:
                bf.status = "parsed"
                bf.parsed_text = table_profile_text
                rag_service.enqueue_document_index(db, bf, parse_document=False)
                try:
                    contract_ref = modeling_contract_source_service.materialize_tabular_contract_source(
                        db,
                        source=ds,
                        bucket_file=bf,
                        profile=table_profile or {},
                        content_sha256=staged_digest,
                    )
                except catalog_service.CatalogError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"表格能力契约来源生成失败：{exc}",
                    ) from exc
                if contract_ref is None:
                    raise HTTPException(422, "表格内容未生成能力输入契约来源")
                bf.modeling_contract_dataset_id = contract_ref.dataset_id
                bf.modeling_contract_schema_id = contract_ref.schema_id
            else:
                rag_service.enqueue_document_index(db, bf, parse_document=True)
            db.commit()
        except Exception as exc:
            db.rollback()
            if upload_claim is not None:
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
def reparse_file(
    file_id: str,
    agent_id: str | None = Query(default=None, min_length=1, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    source = _authorize_file_scope(db, bf, agent_id=agent_id, writable=True)
    if source.resource_scope == "agent_runtime":
        # Runtime attachments are catalog-managed immutable inputs.  Letting
        # this legacy endpoint reparse them can materialize a modeling
        # contract (and deleting them can bypass asset/run lifecycle fences),
        # so mutations must use the Agent-scoped catalog APIs.
        raise HTTPException(status_code=409, detail="验证附件请使用附件数据源管理入口")
    existing_ref = modeling_contract_source_service.refs_for_bucket_files(
        db, source, [bf]
    ).get(bf.id)
    if existing_ref is not None:
        raise HTTPException(409, "该文件已按表格内容生成能力契约来源，无需全文重解析")
    try:
        existing_profile = modeling_contract_source_service.profile_existing_tabular_file(
            source, bf
        )
        if existing_profile is not None:
            contract_ref = modeling_contract_source_service.materialize_tabular_contract_source(
                db,
                source=source,
                bucket_file=bf,
                profile=existing_profile.profile,
                content_sha256=existing_profile.content_sha256,
            )
            if contract_ref is None:
                raise catalog_service.CatalogError("表格内容未生成能力输入契约来源")
            bf.status = "parsed"
            bf.error = ""
            bf.parsed_text = catalog_ingestion_service.profile_summary_text(
                existing_profile.profile, bf.filename
            )
            rag_service.enqueue_document_index(db, bf, parse_document=False, force=True)
            db.commit()
            db.refresh(bf)
            bf.modeling_contract_dataset_id = contract_ref.dataset_id
            bf.modeling_contract_schema_id = contract_ref.schema_id
            return bf
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise HTTPException(422, f"表格能力契约来源生成失败：{exc}") from exc
    bf.status = "pending"
    bf.error = ""
    bf.parsed_text = ""
    rag_service.enqueue_document_index(db, bf, parse_document=True, force=True)
    db.commit()
    db.refresh(bf)
    return bf


@router.get("/files/{file_id}/text")
def file_text(
    file_id: str,
    db: Session = Depends(get_tenant_db),
    agent_id: str | None = None,
):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    _file_source_for_request(db, bf, agent_id=agent_id)
    return {"filename": bf.filename, "text": bf.parsed_text}


@router.get("/files/{file_id}/download")
def file_download(
    file_id: str,
    db: Session = Depends(get_tenant_db),
    agent_id: str | None = None,
):
    """下载文件桶中的文件（附件）。"""
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    ds = _file_source_for_request(db, bf, agent_id=agent_id)
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
def delete_file(
    file_id: str,
    agent_id: str | None = Query(default=None, min_length=1, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    observed = db.get(BucketFile, file_id)
    if not observed:
        raise HTTPException(404, "文件不存在")
    source = _authorize_file_scope(
        db,
        observed,
        agent_id=agent_id,
        writable=True,
    )
    if source.resource_scope == "agent_runtime":
        raise HTTPException(status_code=409, detail="验证附件请使用附件数据源管理入口")
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
        modeling_contract_source_service.retire_for_file_deletion(db, source, bf)
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
