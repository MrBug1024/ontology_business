"""Credential writes use a separate, non-echoing validation boundary."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from ..distillation_access_schemas import SystemAccessOut, SystemAccessRequest, SystemConfigurationRequest
from ..distillation_schemas import ProjectOut, RevisionRequest
from ..services import distillation_access_service
from ..services.auth_service import get_tenant_db
from .business_distillation import _project_out


class AccessRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default errors include input values, even when the
                # Pydantic string representation hides them.
                raise HTTPException(422, "请核对访问方式、专用账号/令牌、授权期限和只读授权依据") from exc

        return handle


router = APIRouter(prefix="/business-distillation", tags=["business-distillation"], route_class=AccessRoute)


@router.put("/{project_id}/business-system", response_model=ProjectOut)
def configure_system(project_id: str, payload: SystemConfigurationRequest, db: Session = Depends(get_tenant_db)):
    row = distillation_access_service.configure(db, project_id, payload)
    db.commit()
    return _project_out(db, row)


@router.get("/{project_id}/system-access", response_model=list[SystemAccessOut])
def list_access(project_id: str, db: Session = Depends(get_tenant_db)):
    return distillation_access_service.statuses(db, project_id)


@router.post("/{project_id}/targets/{target_key}/authorize", response_model=ProjectOut)
def authorize(project_id: str, target_key: str, payload: SystemAccessRequest, db: Session = Depends(get_tenant_db)):
    row = distillation_access_service.authorize(db, project_id, target_key, payload)
    db.commit()
    return _project_out(db, row)


@router.post("/{project_id}/targets/{target_key}/revoke", response_model=ProjectOut)
def revoke(project_id: str, target_key: str, payload: RevisionRequest, db: Session = Depends(get_tenant_db)):
    row = distillation_access_service.revoke(db, project_id, target_key, payload.expected_revision)
    db.commit()
    return _project_out(db, row)
