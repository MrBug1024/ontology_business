"""First-party publication manifest for protocol adapter configuration."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..capability_access_schemas import CapabilityAccessManifestOut
from ..services import capability_access_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/developer/capability-access", tags=["capability-access"])


@router.get("/{scenario_id}/manifest", response_model=CapabilityAccessManifestOut)
def get_manifest(
    scenario_id: str,
    environment: Literal["dev", "staging", "prod"] = Query(default="prod"),
    db: Session = Depends(get_tenant_db),
) -> CapabilityAccessManifestOut:
    try:
        document = capability_access_service.build_manifest(
            db,
            scenario_id,
            environment=environment,
        )
    except capability_access_service.CapabilityAccessError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CapabilityAccessManifestOut.model_validate(document)

