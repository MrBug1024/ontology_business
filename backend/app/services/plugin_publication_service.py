"""Deliberate publication of reviewed immutable plugin installation material."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import AssistantMessage, AssistantThread, BusinessScenario, OntologyRelease
from ..plugin_publication_schemas import PluginPublicationChange, PluginPublicationOut
from . import permission_service, plugin_artifact_catalog as catalog
from .capability_contracts import canonical_hash
from .plugin_host_distribution import marketplace_artifact
from .plugin_coding_workspace import owned_root
from .plugin_host_installation import MAX_INSTALLER_BYTES, installation_info, installer_bytes

PUBLICATION_KIND = 'scenario-plugin-publication.v1'
PUBLICATION_AUDIT_KIND = 'scenario-plugin-publication-audit.v1'
MAX_PUBLIC_ARCHIVE_BYTES = 4 * 1024 * 1024
NOT_AVAILABLE = '插件安装源不可用'


def publication_id(tenant_id: str, artifact_id: str) -> str:
    return canonical_hash({'tenant': tenant_id, 'artifact': artifact_id}, domain=PUBLICATION_KIND)[:32]


def publication_row(db: Session, tenant_id: str, row: AssistantMessage) -> AssistantMessage | None:
    value = db.get(AssistantMessage, publication_id(tenant_id, row.id), populate_existing=True)
    if value is not None and (value.thread_id != row.thread_id or (value.proposal or {}).get('kind') != PUBLICATION_KIND):
        raise HTTPException(409, '插件发布记录身份不匹配')
    return value


def publication_out(row, release, scenario, publication, settings) -> PluginPublicationOut:
    value = catalog.artifact_out(row, release, scenario)
    document = publication.proposal if publication else {}
    configured = bool(settings.plugin_public_base_url)
    status = document.get('status', 'unpublished')
    reason = value.unavailable_reason
    if not reason and not configured:
        reason = '请由部署者配置 PLUGIN_PUBLIC_BASE_URL 后提供安装源；不会生成虚构的公网地址'
    if not reason and status != 'published':
        reason = '尚未发布' if status == 'unpublished' else '此插件安装源已撤回'
    if not reason and document.get('public_base_url') != settings.plugin_public_base_url:
        reason = '安装服务地址已变化，请撤回后重新发布以生成新的安装命令'
    info = None
    if not reason:
        info = installation_info(settings, identity=publication.id, package_name=value.package_name,
            version=value.plugin_version, marketplace_hash=document['marketplace_sha256'],
            installer_hash=document['installer_sha256'], host=value.host)
    return PluginPublicationOut(publication_id=publication.id if publication else publication_id(scenario.tenant_id, row.id),
        artifact_id=row.id, revision=document.get('revision', 0), status=status,
        published_at=document.get('published_at'), configuration_ready=configured,
        available=not reason, unavailable_reason=reason, installation=info)


def get_publication(db: Session, artifact_id: str, settings) -> PluginPublicationOut:
    row, release, scenario = catalog.get_artifact(db, artifact_id)
    principal = permission_service.require_principal(db)
    return publication_out(row, release, scenario, publication_row(db, principal.tenant_id, row), settings)


def assert_revision(document: dict, request: PluginPublicationChange) -> bool:
    revision = document.get('revision', 0)
    if revision == request.expected_revision:
        return False
    if (revision == request.expected_revision + 1 and document.get('last_expected_revision') == request.expected_revision
            and document.get('last_action') == request.action and document.get('artifact_hash') == request.artifact_hash):
        return True
    raise HTTPException(409, '插件发布状态已变化，请刷新后重试')


def published_material(document: dict) -> dict:
    original = catalog.restore_artifact(document)
    host = document['manifest'].get('host', 'claude_code')
    archive = marketplace_artifact(original, document['manifest']['package_name'], document['plugin_version'], host=host)
    if len(archive) > MAX_PUBLIC_ARCHIVE_BYTES:
        raise HTTPException(409, '插件发布材料超过大小上限')
    installer = installer_bytes(host)
    return {'marketplace_archive': base64.b64encode(archive).decode('ascii'),
            'marketplace_sha256': hashlib.sha256(archive).hexdigest(),
            'installer_source': installer.decode('utf-8'), 'installer_sha256': hashlib.sha256(installer).hexdigest()}


def refresh_management_authority(db: Session, scenario):
    permission_service.refresh_request_authorization(db)
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, 'manage')
    permission_service.require_scenario_permission(db, scenario, 'manage')
    return principal


def change_publication(db: Session, artifact_id: str, request: PluginPublicationChange, settings) -> PluginPublicationOut:
    row, release, scenario = catalog.get_artifact(db, artifact_id)
    principal = permission_service.require_principal(db)
    # Publication is broader than authoring. A scenario manage deny remains
    # effective even for a tenant administrator who can read the workspace.
    permission_service.require_tenant_permission(db, 'manage')
    permission_service.require_scenario_permission(db, scenario, 'manage')
    owned_root(db, row.thread_id, lock=True)
    principal = refresh_management_authority(db, scenario)
    publication = publication_row(db, principal.tenant_id, row)
    document = deepcopy(publication.proposal) if publication else {}
    if request.artifact_hash != row.proposal['artifact_hash']:
        raise HTTPException(409, '所选插件版本身份不匹配，请重新选择')
    replay = assert_revision(document, request)
    desired = 'published' if request.action == 'publish' else 'withdrawn'
    if replay or (document.get('status') == desired and (request.action == 'withdraw'
                   or document.get('public_base_url') == settings.plugin_public_base_url)):
        return publication_out(row, release, scenario, publication, settings)
    if request.action == 'publish':
        if not settings.plugin_public_base_url:
            raise HTTPException(409, '请由部署者配置 HTTPS 插件安装源；本机试装须显式配置 loopback 地址及 HTTP 开关')
        value = catalog.artifact_out(row, release, scenario)
        if not value.available:
            raise HTTPException(409, value.unavailable_reason)
        document.update(published_material(row.proposal))
        db.refresh(release)
        db.refresh(scenario)
        permission_service.require_scenario_permission(db, scenario, 'manage')
        if not catalog.artifact_out(row, release, scenario).available:
            raise HTTPException(409, '场景版本在发布期间发生变化，请刷新后重试')
    elif publication is None:
        raise HTTPException(409, '此插件尚未发布')
    principal = refresh_management_authority(db, scenario)
    revision = document.get('revision', 0) + 1
    identity = publication_id(principal.tenant_id, row.id)
    now = datetime.now(timezone.utc).isoformat()
    document.update(kind=PUBLICATION_KIND, tenant_id=principal.tenant_id, artifact_id=row.id,
        artifact_hash=row.proposal['artifact_hash'], revision=revision, status=desired,
        last_action=request.action, last_expected_revision=request.expected_revision, updated_at=now,
        changed_by_user_id=principal.user_id)
    if request.action == 'publish':
        document.update(published_at=now, public_base_url=settings.plugin_public_base_url)
    if publication is None:
        publication = AssistantMessage(id=identity, thread_id=row.thread_id, role='system',
            content='人工发布的场景插件安装源', proposal=document)
        db.add(publication)
    else:
        publication.proposal = document
    audit_id = canonical_hash({'publication': identity, 'revision': revision}, domain=PUBLICATION_AUDIT_KIND)[:32]
    db.add(AssistantMessage(id=audit_id, thread_id=row.thread_id, role='system', content='场景插件发布状态变更',
        proposal={'kind': PUBLICATION_AUDIT_KIND, 'publication_id': identity, 'artifact_id': row.id,
            'artifact_hash': request.artifact_hash, 'revision': revision, 'action': request.action,
            'principal_id': principal.user_id, 'at': now}))
    db.flush()
    return publication_out(row, release, scenario, publication, settings)


def public_context(db: Session, identity: str):
    publication = db.get(AssistantMessage, identity, populate_existing=True)
    document = publication.proposal if publication else {}
    if publication is None or document.get('kind') != PUBLICATION_KIND or document.get('status') != 'published':
        raise HTTPException(404, NOT_AVAILABLE)
    artifact = db.get(AssistantMessage, document.get('artifact_id'))
    thread = db.get(AssistantThread, publication.thread_id)
    if (artifact is None or thread is None or artifact.thread_id != publication.thread_id
            or (artifact.proposal or {}).get('kind') != catalog.ARTIFACT_KIND
            or thread.tenant_id != document.get('tenant_id')
            or publication_id(thread.tenant_id, artifact.id) != identity
            or artifact.proposal.get('artifact_hash') != document.get('artifact_hash')):
        raise HTTPException(404, NOT_AVAILABLE)
    # Deleted snapshots stop serving public material even if a stale publication row remains.
    if artifact.proposal.get('deleted_at') or artifact.proposal.get('retired_at'):
        raise HTTPException(404, NOT_AVAILABLE)
    manifest = artifact.proposal.get('manifest', {})
    release = db.get(OntologyRelease, manifest.get('deployment', {}).get('release_id'))
    scenario = db.get(BusinessScenario, manifest.get('scenario', {}).get('id'))
    if (release is None or scenario is None or release.scenario_id != scenario.id
            or release.tenant_id != thread.tenant_id or scenario.tenant_id != thread.tenant_id):
        raise HTTPException(404, NOT_AVAILABLE)
    if not catalog.artifact_out(artifact, release, scenario).available:
        raise HTTPException(404, NOT_AVAILABLE)
    return publication, artifact, release, scenario


def public_material(db: Session, identity: str, kind: str) -> tuple[str, bytes, str]:
    publication, artifact, release, scenario = public_context(db, identity)
    document = publication.proposal
    revision = document['revision']
    try:
        if kind == 'marketplace':
            encoded = document['marketplace_archive']
            if not isinstance(encoded, str) or len(encoded) > (MAX_PUBLIC_ARCHIVE_BYTES + 2) * 4 // 3:
                raise ValueError('Archive limit')
            data = base64.b64decode(encoded, validate=True)
            digest = document['marketplace_sha256']
            name = f"{artifact.proposal['manifest']['package_name']}-{artifact.proposal['plugin_version']}-marketplace.zip"
        else:
            data = document['installer_source'].encode('utf-8')
            digest = document['installer_sha256']
            name = 'scenario-plugin-installer.py'
        limit = MAX_PUBLIC_ARCHIVE_BYTES if kind == 'marketplace' else MAX_INSTALLER_BYTES
        if len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Content identity')
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(404, NOT_AVAILABLE) from None
    # A withdrawal or release disable while material was restored rejects the
    # late response. No authenticated principal is impersonated for public reads.
    db.refresh(publication)
    db.refresh(release)
    db.refresh(scenario)
    if (publication.proposal.get('status') != 'published' or publication.proposal.get('revision') != revision
            or not catalog.artifact_out(artifact, release, scenario).available):
        raise HTTPException(404, NOT_AVAILABLE)
    return name, data, digest
