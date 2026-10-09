"""Authenticated, bounded download of a reviewed scenario plugin."""
from __future__ import annotations

import hashlib
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response

from ..scenario_package_schemas import ScenarioPackageBuild, PackageEvidence
from ..services.scenario_package_evidence import list_evidence
from ..services.auth_service import get_tenant_db
from ..services.scenario_package_service import build_package
from ..services.scenario_package_acceptance import PackageValidationError


router = APIRouter(prefix='/scenario-releases', tags=['scenario-packages'])


@router.get('/{release_id}/plugin-evidence', response_model=list[PackageEvidence])
def plugin_evidence(release_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return list_evidence(db, release_id)


@router.post('/{release_id}/plugin')
def export_plugin(payload: ScenarioPackageBuild, release_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        name, artifact = build_package(db, release_id, payload)
    except PackageValidationError as exc:
        raise HTTPException(409, {'code': 'scenario_package_blocked', 'message': str(exc)}) from None
    return Response(artifact, media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{name}.zip"',
        'ETag': f'"{hashlib.sha256(artifact).hexdigest()}"',
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    })
