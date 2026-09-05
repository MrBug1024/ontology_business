"""HTTP adapter for durable, asynchronously profiled managed uploads."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..managed_upload_schemas import (
    ManagedUploadCancelIn,
    ManagedUploadCreateIn,
    ManagedUploadRetryIn,
    ManagedUploadRunOut,
)
from ..services import (
    managed_upload_run_service,
    object_deletion_service,
    object_storage_service,
    upload_staging_service,
)
from ..services.auth_service import get_current_user, get_tenant_db


router = APIRouter(
    prefix="/catalog/upload-runs",
    tags=["managed-uploads"],
    dependencies=[Depends(get_current_user)],
)


def _http_error(exc: managed_upload_run_service.ManagedUploadError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post("", response_model=ManagedUploadRunOut, status_code=status.HTTP_202_ACCEPTED)
def create_managed_upload_run(
    payload: ManagedUploadCreateIn,
    db: Session = Depends(get_tenant_db),
) -> ManagedUploadRunOut:
    try:
        return ManagedUploadRunOut.model_validate(
            managed_upload_run_service.create_upload_run(db, payload)
        )
    except managed_upload_run_service.ManagedUploadError as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.get("/{run_id}", response_model=ManagedUploadRunOut)
def get_managed_upload_run(
    run_id: str,
    db: Session = Depends(get_tenant_db),
) -> ManagedUploadRunOut:
    try:
        return ManagedUploadRunOut.model_validate(
            managed_upload_run_service.get_upload_run(db, run_id)
        )
    except managed_upload_run_service.ManagedUploadError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{run_id}/retry",
    response_model=ManagedUploadRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_managed_upload_run(
    run_id: str,
    payload: ManagedUploadRetryIn,
    db: Session = Depends(get_tenant_db),
) -> ManagedUploadRunOut:
    try:
        return ManagedUploadRunOut.model_validate(
            managed_upload_run_service.retry_upload_run(
                db,
                run_id,
                expected_revision=payload.expected_revision,
                idempotency_key=payload.idempotency_key,
            )
        )
    except managed_upload_run_service.ManagedUploadError as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/{run_id}/cancel",
    response_model=ManagedUploadRunOut,
)
def cancel_managed_upload_run(
    run_id: str,
    payload: ManagedUploadCancelIn,
    db: Session = Depends(get_tenant_db),
) -> ManagedUploadRunOut:
    try:
        return ManagedUploadRunOut.model_validate(
            managed_upload_run_service.cancel_upload_run(
                db,
                run_id,
                expected_revision=payload.expected_revision,
            )
        )
    except managed_upload_run_service.ManagedUploadError as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/content",
    response_model=ManagedUploadRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_managed_content(
    upload_run_id: str = Form(..., min_length=1, max_length=32),
    expected_revision: int = Form(..., ge=1),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
) -> ManagedUploadRunOut:
    staged = None
    try:
        # Authorize before consuming attacker-controlled bytes. Profiling still
        # happens later in the durable worker, outside this request.
        declared_limit = managed_upload_run_service.preflight_content_upload(
            db,
            upload_run_id,
            expected_revision=expected_revision,
        )
        db.rollback()
        settings = get_settings()
        staged = await upload_staging_service.stage_upload(
            file,
            max_bytes=min(int(settings.catalog_max_upload_bytes), declared_limit),
            chunk_bytes=int(settings.upload_stream_chunk_bytes),
        )
        lease = managed_upload_run_service.claim_content_upload(
            db,
            upload_run_id,
            expected_revision=expected_revision,
        )
        document = await asyncio.to_thread(
            managed_upload_run_service.store_uploaded_content,
            upload_run_id,
            lease,
            staged.path,
            content_sha256=staged.content_sha256,
            byte_size=staged.byte_size,
        )
        return ManagedUploadRunOut.model_validate(document)
    except upload_staging_service.UploadTooLargeError as exc:
        maximum = int(get_settings().catalog_max_upload_bytes)
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小限制（{maximum // (1024 * 1024)} MB）",
        ) from exc
    except managed_upload_run_service.ManagedUploadError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except object_deletion_service.UploadIntentLeaseLostError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="文件上传事务已失效") from exc
    except (object_storage_service.ObjectStorageError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="受管对象存储写入失败") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if staged is not None:
            staged.remove()
