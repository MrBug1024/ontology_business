"""Private publication governance and explicit public installation material."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import get_db
from ..plugin_publication_schemas import PluginPublicationChange, PluginPublicationOut
from ..services.auth_service import get_tenant_db
from ..services import plugin_publication_service as publication

router = APIRouter(tags=['plugin-publications'])


@router.get('/plugin-artifacts/{artifact_id}/publication', response_model=PluginPublicationOut)
def state(artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return publication.get_publication(db, artifact_id, get_settings())


@router.post('/plugin-artifacts/{artifact_id}/publication', response_model=PluginPublicationOut)
def change(payload: PluginPublicationChange, artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        value = publication.change_publication(db, artifact_id, payload, get_settings())
        db.commit()
        return value
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '插件发布状态并发冲突，请刷新后重试') from None


def material_response(db, identity: str, kind: str) -> Response:
    name, data, digest = publication.public_material(db, identity, kind)
    return Response(data, media_type='application/zip' if kind == 'marketplace' else 'text/x-python',
        headers={'Content-Disposition': f'attachment; filename="{name}"', 'ETag': f'"{digest}"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/published-plugins/{publication_id}/marketplace.zip')
def marketplace(publication_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_db)):
    return material_response(db, publication_id, 'marketplace')


@router.get('/published-plugins/{publication_id}/installer.py')
def installer(publication_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_db)):
    return material_response(db, publication_id, 'installer')
