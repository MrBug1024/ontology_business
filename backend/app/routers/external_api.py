"""P2 versioned external API and integration-key management endpoints.

The v1 surface is deliberately read-only.  It is a stable integration boundary
for scenario discovery and authorized object reads, not a shortcut around the
governed Action/workflow APIs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..database import get_db
from ..external_api_models import ExternalApiKey
from ..external_api_schemas import (
    ExternalApiIdentityOut,
    ExternalApiKeyCreatedOut,
    ExternalApiKeyCreateIn,
    ExternalApiKeyOut,
    ExternalEntityOut,
    ExternalObjectOut,
    ExternalObjectPageOut,
    ExternalPropertyOut,
    ExternalScenarioOut,
)
from ..models import BusinessScenario, OntologyEntity, OntologyInstance
from ..services import external_api_service, permission_service
from ..services.auth_service import get_tenant_db


management_router = APIRouter(prefix="/developer/api-keys", tags=["external-api"])
router = APIRouter(prefix="/external/v1", tags=["external-api"])

# A credentialed list call must not turn a small ``limit`` into an unbounded
# object/ACL walk.  We inspect at most this many SQL-sorted candidates (plus one
# sentinel row to report that more source rows exist), then apply the existing
# v1 ``offset``/``limit`` semantics to the authorized representation.
MAX_EXTERNAL_OBJECT_CANDIDATES = 1_000
MAX_EXTERNAL_OBJECT_OFFSET = 900


def _management_principal(db: Session):
    permission_service.require_tenant_permission(db, "manage")
    return permission_service.require_principal(db)


def _key_out(key: ExternalApiKey) -> ExternalApiKeyOut:
    return ExternalApiKeyOut(**external_api_service.key_metadata(key))


@management_router.get("", response_model=list[ExternalApiKeyOut])
def list_api_keys(db: Session = Depends(get_tenant_db)) -> list[ExternalApiKeyOut]:
    """List credential metadata for the current tenant; never return a secret."""
    principal = _management_principal(db)
    return [_key_out(key) for key in external_api_service.list_keys(db, principal.tenant_id)]


@management_router.post("", response_model=ExternalApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: ExternalApiKeyCreateIn,
    db: Session = Depends(get_tenant_db),
) -> ExternalApiKeyCreatedOut:
    """Issue a bounded integration credential and expose it exactly once."""
    principal = _management_principal(db)
    subject_user_id = payload.user_id or principal.user_id
    # An admin is allowed to manage integration credentials, but must not mint
    # one in another member's (especially an owner's) identity.  That would be
    # a privilege-escalation path around the normal role hierarchy.
    if subject_user_id != principal.user_id and principal.role_key != "owner":
        raise HTTPException(status_code=403, detail="只有所有者可以为其他成员签发 API key")
    try:
        key, raw_token = external_api_service.issue_key(
            db,
            tenant_id=principal.tenant_id,
            user_id=subject_user_id,
            issued_by_user_id=principal.user_id,
            name=payload.name,
            scopes=list(payload.scopes),
            expires_in_days=payload.expires_in_days,
        )
        db.commit()
        db.refresh(key)
    except external_api_service.ExternalApiKeyError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ExternalApiKeyCreatedOut(**external_api_service.key_metadata(key), token=raw_token)


@management_router.delete("/{key_id}", response_model=ExternalApiKeyOut)
def revoke_api_key(key_id: str, db: Session = Depends(get_tenant_db)) -> ExternalApiKeyOut:
    """Revoke immediately; historical metadata remains available for audit."""
    principal = _management_principal(db)
    try:
        key = external_api_service.revoke_key(
            db,
            tenant_id=principal.tenant_id,
            key_id=key_id,
            revoked_by_user_id=principal.user_id,
        )
        if not key:
            raise HTTPException(status_code=404, detail="API key 不存在")
        db.commit()
        db.refresh(key)
    except external_api_service.ExternalApiKeyError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _key_out(key)


def _external_context(
    request: Request,
    db: Session = Depends(get_db),
) -> external_api_service.ExternalApiContext:
    return external_api_service.authenticate(request, db)


def _owned_scenario(
    context: external_api_service.ExternalApiContext,
    scenario_id: str,
) -> BusinessScenario:
    """External keys never inherit cross-tenant ``is_public`` discovery."""
    scenario = context.db.execute(
        select(BusinessScenario).where(
            BusinessScenario.id == scenario_id,
            BusinessScenario.tenant_id == context.tenant_id,
        )
    ).scalars().first()
    if not scenario:
        raise HTTPException(status_code=404, detail="业务场景不存在")
    permission_service.require_scenario_permission(context.db, scenario, "read")
    return scenario


def _scenario_out(scenario: BusinessScenario) -> ExternalScenarioOut:
    return ExternalScenarioOut(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description or "",
        industry=scenario.industry or "",
        status=scenario.status or "draft",
        created_at=scenario.created_at,
        updated_at=scenario.updated_at,
    )


def _entity_out(db: Session, entity: OntologyEntity) -> ExternalEntityOut:
    # Properties are filtered independently so an API key scoped to an object
    # can never use schema discovery as a sensitive-field side channel.
    return ExternalEntityOut(
        id=entity.id,
        scenario_id=entity.scenario_id,
        name=entity.name,
        description=entity.description or "",
        properties=[
            ExternalPropertyOut(
                name=prop.name,
                data_type=prop.data_type,
                description=prop.description or "",
                is_key=bool(prop.is_key),
                is_required=bool(prop.is_required),
                is_enum=bool(prop.is_enum),
                enum_values=list(prop.enum_values or []),
            )
            for prop in entity.properties
            if permission_service.can_read_property(db, prop)
        ],
    )


def _object_out(db: Session, instance: OntologyInstance) -> ExternalObjectOut:
    entity = instance.entity
    return ExternalObjectOut(
        id=instance.id,
        scenario_id=instance.scenario_id,
        entity_id=instance.entity_id,
        entity_name=entity.name if entity else "",
        name=instance.name,
        attributes=permission_service.filter_instance_attributes(db, instance),
        created_at=instance.created_at,
    )


@router.get("/identity", response_model=ExternalApiIdentityOut)
def identity(
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalApiIdentityOut:
    return ExternalApiIdentityOut(
        key_id=context.key_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        scopes=sorted(context.scopes),
        expires_at=context.expires_at,
    )


@router.get("/scenarios", response_model=list[ExternalScenarioOut])
def list_scenarios(
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> list[ExternalScenarioOut]:
    external_api_service.require_scope(context, "scenarios:read")
    scenarios = context.db.execute(
        select(BusinessScenario)
        .where(BusinessScenario.tenant_id == context.tenant_id)
        .order_by(BusinessScenario.created_at.desc(), BusinessScenario.id.desc())
    ).scalars().all()
    return [
        _scenario_out(scenario)
        for scenario in scenarios
        if permission_service.check_scenario(context.db, scenario, "read").allowed
    ]


@router.get("/scenarios/{scenario_id}/entities", response_model=list[ExternalEntityOut])
def list_entities(
    scenario_id: str,
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> list[ExternalEntityOut]:
    external_api_service.require_scope(context, "scenarios:read")
    _owned_scenario(context, scenario_id)
    entities = context.db.execute(
        select(OntologyEntity)
        .options(joinedload(OntologyEntity.properties))
        .where(OntologyEntity.scenario_id == scenario_id)
        .order_by(OntologyEntity.created_at.asc(), OntologyEntity.id.asc())
    ).unique().scalars().all()
    return [_entity_out(context.db, entity) for entity in entities]


@router.get("/scenarios/{scenario_id}/objects", response_model=ExternalObjectPageOut)
def list_objects(
    scenario_id: str,
    q: str = Query(default="", max_length=200),
    entity_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=MAX_EXTERNAL_OBJECT_OFFSET),
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalObjectPageOut:
    external_api_service.require_scope(context, "objects:read")
    _owned_scenario(context, scenario_id)
    filters = [OntologyInstance.scenario_id == scenario_id]
    if entity_id:
        entity = context.db.execute(
            select(OntologyEntity).where(
                OntologyEntity.id == entity_id,
                OntologyEntity.scenario_id == scenario_id,
            )
        ).scalars().first()
        if not entity:
            raise HTTPException(status_code=400, detail="实体不属于当前业务场景")
        filters.append(OntologyInstance.entity_id == entity_id)
    query = q.strip().lower()
    statement = (
        select(OntologyInstance)
        .options(
            joinedload(OntologyInstance.entity).selectinload(OntologyEntity.properties),
            joinedload(OntologyInstance.scenario),
        )
        .where(*filters)
        .order_by(OntologyInstance.created_at.desc(), OntologyInstance.id.desc())
    )
    if query:
        # Push the inexpensive, portable candidate search into SQL.  The JSON
        # term is only an inclusive prefilter: final matching below always uses
        # the already ACL/sensitivity-filtered representation, so a hidden
        # value can neither surface an object nor affect its visible total.
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        statement = statement.where(
            or_(
                func.lower(OntologyInstance.name).like(pattern, escape="\\"),
                func.lower(cast(OntologyInstance.attributes, String)).like(pattern, escape="\\"),
            )
        )
    # A sentinel row detects continuation without loading an entire tenant.
    # ACL filtering intentionally happens before slicing so legacy callers keep
    # their visible-record offset semantics even when a source row is denied.
    candidates = context.db.execute(
        statement.limit(MAX_EXTERNAL_OBJECT_CANDIDATES + 1)
    ).unique().scalars().all()
    has_uninspected_candidates = len(candidates) > MAX_EXTERNAL_OBJECT_CANDIDATES
    candidates = candidates[:MAX_EXTERNAL_OBJECT_CANDIDATES]
    visible: list[tuple[OntologyInstance, dict]] = []
    for instance in candidates:
        if not permission_service.check_object(context.db, instance, "read").allowed:
            continue
        attributes = permission_service.filter_instance_attributes(context.db, instance)
        # The SQL filter may have matched a sensitive field.  Re-check against
        # public object data before deciding either membership or ``total``.
        if query and query not in instance.name.lower() and query not in str(attributes).lower():
            continue
        visible.append((instance, attributes))
    total = len(visible)
    page = visible[offset : offset + limit]
    has_more = has_uninspected_candidates or len(visible) > offset + len(page)
    return ExternalObjectPageOut(
        items=[
            ExternalObjectOut(
                id=instance.id,
                scenario_id=instance.scenario_id,
                entity_id=instance.entity_id,
                entity_name=instance.entity.name if instance.entity else "",
                name=instance.name,
                attributes=attributes,
                created_at=instance.created_at,
            )
            for instance, attributes in page
        ],
        total=total,
        limit=limit,
        offset=offset,
        query=query,
        entity_id=entity_id,
        total_is_exact=not has_uninspected_candidates,
        has_more=has_more,
    )


@router.get("/scenarios/{scenario_id}/objects/{object_id}", response_model=ExternalObjectOut)
def get_object(
    scenario_id: str,
    object_id: str,
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalObjectOut:
    external_api_service.require_scope(context, "objects:read")
    _owned_scenario(context, scenario_id)
    instance = context.db.execute(
        select(OntologyInstance)
        .options(joinedload(OntologyInstance.entity))
        .where(
            OntologyInstance.id == object_id,
            OntologyInstance.scenario_id == scenario_id,
        )
    ).scalars().first()
    if not instance:
        raise HTTPException(status_code=404, detail="对象不存在")
    permission_service.require_object_permission(context.db, instance, "read")
    return _object_out(context.db, instance)
