"""HTTP adapters for durable GlobalAssistant request runs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..schemas import (
    AssistantRequestCancelIn,
    AssistantRequestRetryIn,
    AssistantRequestRunOut,
)
from ..services import assistant_request_run_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/assistant", tags=["assistant"])


def _request_run_http_error(
    exc: assistant_request_run_service.AssistantRequestError,
) -> HTTPException:
    return HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})


@router.get("/request-runs/{run_id}", response_model=AssistantRequestRunOut)
def get_assistant_request_run(run_id: str, db: Session = Depends(get_tenant_db)):
    try:
        return assistant_request_run_service.get_request(db, run_id)
    except assistant_request_run_service.AssistantRequestError as exc:
        raise _request_run_http_error(exc) from exc


@router.post("/request-runs/{run_id}/retry", response_model=AssistantRequestRunOut)
def retry_assistant_request_run(
    run_id: str,
    payload: AssistantRequestRetryIn,
    db: Session = Depends(get_tenant_db),
):
    try:
        return assistant_request_run_service.retry_request(
            db,
            run_id,
            expected_revision=payload.expected_revision,
            idempotency_key=payload.idempotency_key,
        )
    except assistant_request_run_service.AssistantRequestError as exc:
        raise _request_run_http_error(exc) from exc


@router.post("/request-runs/{run_id}/cancel", response_model=AssistantRequestRunOut)
def cancel_assistant_request_run(
    run_id: str,
    payload: AssistantRequestCancelIn,
    db: Session = Depends(get_tenant_db),
):
    try:
        return assistant_request_run_service.cancel_request(
            db,
            run_id,
            expected_revision=payload.expected_revision,
        )
    except assistant_request_run_service.AssistantRequestError as exc:
        raise _request_run_http_error(exc) from exc
