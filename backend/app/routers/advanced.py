"""P2 advanced data/model assets and their governed runtime endpoints."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import (
    BusinessScenario,
    FunctionDefinition,
    OntologyAdvancedAsset,
    OntologyAdvancedRecord,
    OntologyAdvancedRun,
    OntologyModelFeedback,
    OntologyRelease,
)
from ..schemas import (
    AdvancedAssetCreateIn,
    AdvancedAssetOut,
    AdvancedAssetSummaryOut,
    AdvancedAssetUpdateIn,
    AdvancedFeedbackIn,
    AdvancedFeedbackOut,
    AdvancedRecordIn,
    AdvancedRecordOut,
    AdvancedRecordPageOut,
    AdvancedRunIn,
    AdvancedRunOut,
)
from ..services import advanced_runtime_service, permission_service, release_service, tenant_service
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/advanced",
    tags=["advanced-runtime"],
    dependencies=[Depends(get_current_user)],
)


def _scenario(db: Session, scenario_id: str, *, write: bool = False) -> BusinessScenario:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "write" if write else "read")
    return scenario


def _user_id(db: Session) -> str | None:
    value = db.info.get("user_id")
    return str(value) if value else None


def _asset(db: Session, asset_id: str, *, write: bool = False) -> OntologyAdvancedAsset:
    tenant_id = tenant_service.current_tenant_id(db)
    asset = db.execute(
        select(OntologyAdvancedAsset).where(
            OntologyAdvancedAsset.id == asset_id,
            OntologyAdvancedAsset.tenant_id == tenant_id,
        )
    ).scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="高级资产不存在")
    _scenario(db, asset.scenario_id, write=write)
    if write:
        return asset
    return _published_asset_view(db, asset)


def _published_asset_view(db: Session, asset: OntologyAdvancedAsset):
    """Resolve staging/prod reads and runs from the immutable release snapshot."""
    runtime_environment = str(get_settings().runtime_environment)
    if runtime_environment == "dev":
        return asset
    release = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.scenario_id == asset.scenario_id,
            OntologyRelease.environment == runtime_environment,
            OntologyRelease.status == "released",
        )
        .order_by(OntologyRelease.created_at.desc())
        .limit(1)
    ).scalars().first()
    if not release:
        raise HTTPException(status_code=409, detail=f"当前 {runtime_environment} 环境尚未发布高级资产")
    snapshot = release_service.get_snapshot(db, release.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=409, detail="当前环境发布快照不可用")
    try:
        content = release_service.normalize_snapshot_content(snapshot.content or {})
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(status_code=409, detail="当前环境发布快照无效") from exc
    item = next(
        (candidate for candidate in content.get("advanced_assets", []) if candidate.get("id") == asset.id),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="高级资产尚未发布到当前环境")
    return SimpleNamespace(
        id=asset.id,
        tenant_id=asset.tenant_id,
        scenario_id=asset.scenario_id,
        name=item["name"],
        kind=item["kind"],
        description=item.get("description", ""),
        schema=item.get("schema", {}),
        config=item.get("config", {}),
        status=item.get("status", "draft"),
        version=asset.version,
        created_by_user_id=asset.created_by_user_id,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _require_definition_dev() -> None:
    runtime_environment = str(get_settings().runtime_environment)
    if runtime_environment != "dev":
        raise HTTPException(status_code=409, detail="高级资产定义只能在 dev 环境修改，请通过分支提案发布")


def _record_out(record: OntologyAdvancedRecord) -> AdvancedRecordOut:
    return AdvancedRecordOut(
        id=record.id,
        tenant_id=record.tenant_id,
        scenario_id=record.scenario_id,
        asset_id=record.asset_id,
        sequence=record.sequence,
        event_time=record.event_time,
        event_type=record.event_type or "",
        geometry=record.geometry or {},
        payload=record.payload or {},
        source_ref=record.source_ref or "",
        content_type=record.content_type or "",
        checksum=record.checksum or "",
        created_at=record.created_at,
    )


def _run_out(run: OntologyAdvancedRun) -> AdvancedRunOut:
    return AdvancedRunOut(
        id=run.id,
        tenant_id=run.tenant_id,
        scenario_id=run.scenario_id,
        asset_id=run.asset_id,
        function_id=run.function_id,
        run_type=run.run_type,
        status=run.status,
        input_payload=run.input_payload or {},
        output_payload=run.output_payload or {},
        error=run.error or "",
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_by_user_id=run.created_by_user_id,
        created_at=run.created_at,
    )


def _runtime_error(exc: Exception) -> None:
    if isinstance(exc, advanced_runtime_service.AdvancedRuntimeError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/scenarios/{scenario_id}/assets", response_model=list[AdvancedAssetOut])
def list_assets(
    scenario_id: str,
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _scenario(db, scenario_id)
    stmt = select(OntologyAdvancedAsset).where(
        OntologyAdvancedAsset.scenario_id == scenario_id,
        OntologyAdvancedAsset.tenant_id == tenant_service.current_tenant_id(db),
    )
    if kind:
        if kind not in advanced_runtime_service.ASSET_KINDS:
            raise HTTPException(status_code=400, detail="资产类型不受支持")
        stmt = stmt.where(OntologyAdvancedAsset.kind == kind)
    rows = db.execute(stmt.order_by(OntologyAdvancedAsset.updated_at.desc())).scalars().all()
    if str(get_settings().runtime_environment) == "dev":
        return rows
    return [_published_asset_view(db, row) for row in rows]


@router.post("/scenarios/{scenario_id}/assets", response_model=AdvancedAssetOut, status_code=201)
def create_asset(
    scenario_id: str,
    payload: AdvancedAssetCreateIn,
    db: Session = Depends(get_db),
):
    scenario = _scenario(db, scenario_id, write=True)
    _require_definition_dev()
    try:
        normalized = advanced_runtime_service.normalize_asset(payload.model_dump(by_alias=True))
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    asset = OntologyAdvancedAsset(
        tenant_id=tenant_service.current_tenant_id(db),
        scenario_id=scenario.id,
        created_by_user_id=_user_id(db),
        **normalized,
    )
    db.add(asset)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if "uq_advanced_asset_scenario_name" in str(exc):
            raise HTTPException(status_code=409, detail="同一场景下资产名称不能重复") from exc
        raise
    db.refresh(asset)
    return asset


@router.get("/assets/{asset_id}", response_model=AdvancedAssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    return _asset(db, asset_id)


@router.patch("/assets/{asset_id}", response_model=AdvancedAssetOut)
def update_asset(asset_id: str, payload: AdvancedAssetUpdateIn, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id, write=True)
    _require_definition_dev()
    try:
        normalized = advanced_runtime_service.normalize_asset(payload.model_dump(by_alias=True))
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    for key, value in normalized.items():
        setattr(asset, key, value)
    asset.version = int(asset.version or 1) + 1
    db.commit()
    db.refresh(asset)
    return asset


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id, write=True)
    _require_definition_dev()
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            db.get(BusinessScenario, asset.scenario_id),
            kind="advanced_asset",
            resource_id=asset.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    settings = get_settings()
    storage_root = (settings.data_dir if hasattr(settings, "data_dir") else Path(settings.database_url.replace("sqlite:///", "")).parent) / "advanced-media"
    for record in db.execute(select(OntologyAdvancedRecord).where(OntologyAdvancedRecord.asset_id == asset.id)).scalars().all():
        if record.storage_path:
            target = Path(record.storage_path).resolve()
            try:
                target.relative_to(storage_root.resolve())
            except ValueError:
                continue
            target.unlink(missing_ok=True)
    db.delete(asset)
    db.commit()
    return {"ok": True, "message": "高级资产已删除"}


@router.get("/assets/{asset_id}/summary", response_model=AdvancedAssetSummaryOut)
def asset_summary(asset_id: str, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id)
    record_count = db.execute(
        select(func.count(OntologyAdvancedRecord.id)).where(OntologyAdvancedRecord.asset_id == asset.id)
    ).scalar_one()
    run_count = db.execute(
        select(func.count(OntologyAdvancedRun.id)).where(OntologyAdvancedRun.asset_id == asset.id)
    ).scalar_one()
    feedback_count = db.execute(
        select(func.count(OntologyModelFeedback.id)).where(OntologyModelFeedback.asset_id == asset.id)
    ).scalar_one()
    last = db.execute(
        select(OntologyAdvancedRecord)
        .where(OntologyAdvancedRecord.asset_id == asset.id)
        .order_by(OntologyAdvancedRecord.sequence.desc())
        .limit(1)
    ).scalars().first()
    return AdvancedAssetSummaryOut(
        asset_id=asset.id,
        kind=asset.kind,
        record_count=int(record_count or 0),
        run_count=int(run_count or 0),
        feedback_count=int(feedback_count or 0),
        last_event_time=last.event_time if last else None,
        last_sequence=last.sequence if last else 0,
    )


@router.get("/assets/{asset_id}/records", response_model=AdvancedRecordPageOut)
def list_records(
    asset_id: str,
    after_sequence: int = Query(default=0, ge=0),
    from_time: datetime | None = Query(default=None),
    to_time: datetime | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=120),
    bbox: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    asset = _asset(db, asset_id)
    try:
        bbox_value = advanced_runtime_service.parse_bbox(bbox)
        rows, next_sequence = advanced_runtime_service.query_records(
            db,
            asset,
            after_sequence=after_sequence,
            from_time=from_time,
            to_time=to_time,
            event_type=event_type,
            bbox=bbox_value,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    return AdvancedRecordPageOut(
        items=[_record_out(row) for row in rows],
        next_sequence=next_sequence,
        total=len(rows),
    )


@router.post("/assets/{asset_id}/records", response_model=AdvancedRecordOut, status_code=201)
def create_record(asset_id: str, payload: AdvancedRecordIn, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id, write=True)
    try:
        record = advanced_runtime_service.create_record(
            db,
            asset,
            payload.model_dump(),
            tenant_id=asset.tenant_id,
            scenario_id=asset.scenario_id,
        )
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    db.commit()
    db.refresh(record)
    return _record_out(record)


@router.post("/assets/{asset_id}/media", response_model=AdvancedRecordOut, status_code=201)
async def upload_media(
    asset_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    asset = _asset(db, asset_id, write=True)
    if asset.kind != "media":
        raise HTTPException(status_code=409, detail="只有 media 资产支持文件上传")
    settings = get_settings()
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过平台上传大小限制")
    safe_name = Path(file.filename or "upload.bin").name
    suffix = Path(safe_name).suffix.lower()[:20]
    storage_root = Path(settings.database_url.replace("sqlite:///", "")).parent / "advanced-media"
    target_dir = storage_root / asset.tenant_id / asset.id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / f"{uuid4().hex}{suffix}").resolve()
    target.write_bytes(data)
    try:
        record = advanced_runtime_service.create_record(
            db,
            asset,
            {
                "event_type": "media.upload",
                "payload": {"filename": safe_name, "size": len(data)},
                "source_ref": safe_name,
            },
            tenant_id=asset.tenant_id,
            scenario_id=asset.scenario_id,
            content_type=file.content_type or "application/octet-stream",
            storage_path=str(target),
            checksum=advanced_runtime_service.checksum_bytes(data),
        )
        db.commit()
        db.refresh(record)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return _record_out(record)


@router.get("/assets/{asset_id}/media/{record_id}")
def download_media(asset_id: str, record_id: str, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id)
    if asset.kind != "media":
        raise HTTPException(status_code=409, detail="只有 media 资产支持文件下载")
    record = db.execute(
        select(OntologyAdvancedRecord).where(
            OntologyAdvancedRecord.id == record_id,
            OntologyAdvancedRecord.asset_id == asset.id,
        )
    ).scalars().first()
    if not record or not record.storage_path:
        raise HTTPException(status_code=404, detail="媒体记录不存在")
    target = Path(record.storage_path).resolve()
    root = (Path(get_settings().database_url.replace("sqlite:///", "")).parent / "advanced-media").resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="媒体文件不可用") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    return FileResponse(target, media_type=record.content_type or "application/octet-stream")


@router.post("/assets/{asset_id}/runs", response_model=AdvancedRunOut, status_code=201)
def run_asset(
    asset_id: str,
    payload: AdvancedRunIn,
    run_type: str = Query(default="predict"),
    db: Session = Depends(get_db),
):
    asset = _asset(db, asset_id, write=True)
    try:
        run = advanced_runtime_service.create_asset_run(
            db,
            asset,
            payload.params,
            tenant_id=asset.tenant_id,
            scenario_id=asset.scenario_id,
            user_id=_user_id(db),
            run_type=run_type,
            idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    db.commit()
    db.refresh(run)
    return _run_out(run)


@router.get("/assets/{asset_id}/runs", response_model=list[AdvancedRunOut])
def list_runs(asset_id: str, limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    asset = _asset(db, asset_id)
    rows = db.execute(
        select(OntologyAdvancedRun)
        .where(OntologyAdvancedRun.asset_id == asset.id)
        .order_by(OntologyAdvancedRun.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_run_out(row) for row in rows]


@router.post("/assets/{asset_id}/feedback", response_model=AdvancedFeedbackOut, status_code=201)
def create_feedback(asset_id: str, payload: AdvancedFeedbackIn, db: Session = Depends(get_db)):
    asset = _asset(db, asset_id, write=True)
    try:
        feedback = advanced_runtime_service.create_feedback(
            db,
            asset,
            payload.model_dump(),
            tenant_id=asset.tenant_id,
            scenario_id=asset.scenario_id,
            user_id=_user_id(db),
        )
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    db.commit()
    db.refresh(feedback)
    return feedback


@router.get("/assets/{asset_id}/feedback", response_model=list[AdvancedFeedbackOut])
def list_feedback(asset_id: str, limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    asset = _asset(db, asset_id)
    return db.execute(
        select(OntologyModelFeedback)
        .where(OntologyModelFeedback.asset_id == asset.id)
        .order_by(OntologyModelFeedback.created_at.desc())
        .limit(limit)
    ).scalars().all()


@router.post("/functions/{function_id}/run", response_model=AdvancedRunOut, status_code=201)
def run_function(function_id: str, payload: AdvancedRunIn, db: Session = Depends(get_db)):
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(status_code=404, detail="函数定义不存在")
    _scenario(db, function.scenario_id, write=True)
    try:
        run = advanced_runtime_service.create_function_run(
            db,
            function,
            payload.params,
            tenant_id=tenant_service.current_tenant_id(db),
            scenario_id=function.scenario_id,
            user_id=_user_id(db),
            idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        _runtime_error(exc)
    db.commit()
    db.refresh(run)
    return _run_out(run)


@router.get("/functions/{function_id}/runs", response_model=list[AdvancedRunOut])
def list_function_runs(function_id: str, limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(status_code=404, detail="函数定义不存在")
    _scenario(db, function.scenario_id)
    rows = db.execute(
        select(OntologyAdvancedRun)
        .where(OntologyAdvancedRun.function_id == function.id)
        .order_by(OntologyAdvancedRun.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_run_out(row) for row in rows]
