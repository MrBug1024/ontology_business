"""Authorized result attachments for message adapters and thin SDK clients."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from ..services import capability_application_service, external_api_service
from .data_sources import file_download
from .external_capabilities import _actor, _application_error, _external_context


router = APIRouter(prefix="/external/v2/invocations", tags=["capability-attachments"])


@router.get("/{invocation_id}/attachments/{file_id}/download")
def download(
    invocation_id: Annotated[str, Path(min_length=1, max_length=32)],
    file_id: Annotated[str, Path(min_length=1, max_length=32)],
    context: external_api_service.ExternalApiContext = Depends(_external_context),
):
    external_api_service.require_scope(context, "capabilities:read")
    try:
        receipt = capability_application_service.get_receipt(context.db, _actor(context), invocation_id)
    except capability_application_service.CapabilityApplicationError as exc:
        _application_error(exc)
    if not any(item["id"] == file_id for item in receipt["delivery"]["attachments"]):
        raise HTTPException(404, "结果附件不存在或无权访问")
    try:
        return file_download(file_id, context.db)
    except HTTPException as exc:
        status = exc.status_code if exc.status_code in {403, 404, 409, 503} else 503
        raise HTTPException(status, "结果附件不存在、不可用或无权访问") from None
