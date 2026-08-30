"""External REST v2 adapter for the protocol-neutral capability service."""
from __future__ import annotations

from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request as HttpRequest,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..catalog_schemas import CatalogManagedUploadMetadata, CatalogManagedUploadOut
from ..config import get_settings
from ..database import get_db
from ..external_api_schemas import (
    ExternalCapabilityEnvironment,
    ExternalCapabilityInvocationIn,
    ExternalCapabilityKind,
    ExternalCapabilityOut,
    ExternalCapabilityReceiptOut,
    ExternalCapabilityScenarioOut,
    ExternalManagedInputOptionsOut,
)
from ..models import BusinessScenario, DataAsset, DataAssetVersion
from ..services import (
    catalog_ingestion_service,
    catalog_service,
    capability_application_service,
    external_api_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
)
from ..services.capability_contracts import (
    Actor,
    CapabilityContractError,
    CapabilityRef,
    Request,
)
from ..services.capability_invoker import CapabilityInvocationError


router = APIRouter(prefix="/external/v2", tags=["external-capabilities"])

_EXTERNAL_UPLOAD_FIELDS = {
    "file",
    "name",
    "description",
    "expires_in_seconds",
}
_EXTERNAL_UPLOAD_PHYSICAL_FIELDS = {
    "access_key",
    "bucket",
    "bucket_name",
    "credential",
    "credentials",
    "endpoint",
    "file_bucket_id",
    "object_key",
    "object_path",
    "password",
    "path",
    "prefix",
    "secret_key",
    "storage_path",
    "token",
}


def _external_context(
    request: HttpRequest,
    db: Session = Depends(get_db),
) -> external_api_service.ExternalApiContext:
    return external_api_service.authenticate(request, db)


def _owned_scenario(
    context: external_api_service.ExternalApiContext,
    scenario_id: str,
) -> BusinessScenario:
    scenario = context.db.execute(
        select(BusinessScenario).where(
            BusinessScenario.id == scenario_id,
            BusinessScenario.tenant_id == context.tenant_id,
        )
    ).scalar_one_or_none()
    if scenario is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "scenario_not_found", "message": "business scenario not found"},
        )
    if not permission_service.check_scenario(context.db, scenario, "read").allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "scenario_forbidden", "message": "scenario access denied"},
        )
    return scenario


def _active_owned_scenario(
    context: external_api_service.ExternalApiContext,
    scenario_id: str,
) -> BusinessScenario:
    scenario = _owned_scenario(context, scenario_id)
    if str(scenario.status or "").strip().lower() == "retired":
        raise HTTPException(
            status_code=404,
            detail={"code": "scenario_not_found", "message": "business scenario not found"},
        )
    return scenario


def _actor(context: external_api_service.ExternalApiContext) -> Actor:
    principal = permission_service.require_principal(context.db)
    scopes = set(context.scopes)
    if "capabilities:read" in scopes:
        scopes.add("capability:read")
    if "capabilities:invoke" in scopes:
        scopes.add("capability:invoke")
    return Actor(
        actor_type="external_api",
        principal_id=context.key_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        client_id=context.key_id,
        roles=(principal.role_key,),
        scopes=tuple(scopes),
    )


def _application_error(exc: capability_application_service.CapabilityApplicationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.as_dict()) from exc


_UNPROCESSABLE_CODES = {
    "input_schema_invalid",
    "invalid_confirmation",
    "invalid_invocation_mode",
    "invalid_override_shape",
    "runtime_input_port_not_found",
    "runtime_input_override_forbidden",
    "unsupported_managed_reference",
}
_FORBIDDEN_CODES = {
    "capability_role_forbidden",
    "capability_scope_forbidden",
    "principal_scope_mismatch",
}
_NOT_FOUND_CODES = {
    "capability_not_found",
    "confirmation_not_found",
}


def _invocation_error(exc: CapabilityInvocationError) -> None:
    if exc.code in _UNPROCESSABLE_CODES:
        status_code = 422
    elif exc.code in _FORBIDDEN_CODES:
        status_code = 403
    elif exc.code in _NOT_FOUND_CODES:
        status_code = 404
    else:
        status_code = 409
    raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc


@router.post(
    "/assets/upload",
    response_model=CatalogManagedUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_invocation_attachment(
    request: HttpRequest,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    description: str = Form(""),
    expires_in_seconds: int | None = Form(None),
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> CatalogManagedUploadOut:
    """Create a temporary logical asset without exposing storage coordinates."""
    external_api_service.require_scope(context, "assets:write")
    form = await request.form()
    supplied_fields = {str(key).strip().lower() for key in form.keys()}
    disallowed = supplied_fields - _EXTERNAL_UPLOAD_FIELDS
    if disallowed:
        if disallowed.intersection(_EXTERNAL_UPLOAD_PHYSICAL_FIELDS) or any(
            any(marker in key for marker in ("password", "secret", "credential"))
            for key in disallowed
        ):
            raise HTTPException(
                status_code=400,
                detail="上传请求不得包含对象路径、存储配置或凭据字段",
            )
        raise HTTPException(status_code=422, detail="上传请求包含未知字段")
    if len(form.getlist("file")) != 1:
        raise HTTPException(status_code=422, detail="每次目录上传只能包含一个文件")
    try:
        # Resolve the hidden tenant-owned bucket before reading attacker bytes.
        source = catalog_ingestion_service.require_external_upload_bucket(context.db)
        try:
            metadata = CatalogManagedUploadMetadata(
                file_bucket_id=source.id,
                purpose="invocation_attachment",
                name=name,
                description=description,
                labels={},
                expires_in_seconds=expires_in_seconds,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        max_bytes = int(get_settings().max_upload_bytes)
        content = await file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过大小限制（{max_bytes // (1024 * 1024)} MB）",
            )
        result = catalog_ingestion_service.persist_managed_upload(
            context.db,
            source,
            content,
            file.filename or "file",
            file.content_type,
            metadata,
        )
        asset = context.db.get(DataAsset, result.asset_id)
        version = context.db.get(DataAssetVersion, result.version_id)
        if asset is None or version is None:
            raise RuntimeError("目录上传结果未能重新加载")
        return CatalogManagedUploadOut.model_validate(
            catalog_ingestion_service.managed_upload_document(
                asset,
                version,
                fallback_purpose="invocation_attachment",
                created=result.created,
            )
        )
    except HTTPException:
        context.db.rollback()
        raise
    except catalog_service.CatalogError as exc:
        context.db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        context.db.rollback()
        raise HTTPException(
            status_code=409,
            detail="目录资产在上传期间发生并发冲突，请重试",
        ) from exc
    except object_deletion_service.UploadIntentLeaseLostError as exc:
        context.db.rollback()
        raise HTTPException(status_code=503, detail="文件上传事务已失效") from exc
    except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
        context.db.rollback()
        raise HTTPException(status_code=503, detail="受管对象存储写入失败") from exc
    except ValueError as exc:
        context.db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/scenarios", response_model=list[ExternalCapabilityScenarioOut])
def list_scenarios(
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> list[ExternalCapabilityScenarioOut]:
    """List ACL-readable logical scenarios without exposing tenant internals."""
    external_api_service.require_scope(context, "capabilities:read")
    scenarios = context.db.execute(
        select(BusinessScenario)
        .where(
            BusinessScenario.tenant_id == context.tenant_id,
            BusinessScenario.status != "retired",
        )
        .order_by(BusinessScenario.name, BusinessScenario.id)
    ).scalars().all()
    return [
        ExternalCapabilityScenarioOut(
            id=scenario.id,
            name=scenario.name,
            description=scenario.description or "",
            industry=scenario.industry or "",
        )
        for scenario in scenarios
        if permission_service.check_scenario(context.db, scenario, "read").allowed
    ]


@router.get(
    "/scenarios/{scenario_id}/capabilities",
    response_model=list[ExternalCapabilityOut],
)
def list_capabilities(
    scenario_id: str,
    environment: ExternalCapabilityEnvironment = Query(default="prod"),
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> list[ExternalCapabilityOut]:
    external_api_service.require_scope(context, "capabilities:read")
    scenario = _owned_scenario(context, scenario_id)
    try:
        documents = capability_application_service.list_capabilities(
            context.db,
            scenario,
            environment=environment,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        _application_error(exc)
    return [ExternalCapabilityOut.model_validate(item) for item in documents]


@router.get(
    "/scenarios/{scenario_id}/capabilities/{kind}/{key}/ports/{port_key}/managed-input-options",
    response_model=ExternalManagedInputOptionsOut,
    response_model_exclude_none=True,
)
def list_managed_input_options(
    scenario_id: str,
    kind: ExternalCapabilityKind,
    key: str,
    port_key: str,
    environment: ExternalCapabilityEnvironment = Query(default="prod"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalManagedInputOptionsOut:
    external_api_service.require_scope(context, "capabilities:read")
    scenario = _active_owned_scenario(context, scenario_id)
    try:
        document = capability_application_service.list_managed_input_options(
            context.db,
            scenario,
            environment=environment,
            kind=kind,
            key=key,
            port_key=port_key,
            limit=limit,
            offset=offset,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        _application_error(exc)
    return ExternalManagedInputOptionsOut.model_validate(document)


@router.get(
    "/scenarios/{scenario_id}/capabilities/{kind}/{key}",
    response_model=ExternalCapabilityOut,
)
def get_capability(
    scenario_id: str,
    kind: ExternalCapabilityKind,
    key: str,
    environment: ExternalCapabilityEnvironment = Query(default="prod"),
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalCapabilityOut:
    external_api_service.require_scope(context, "capabilities:read")
    scenario = _owned_scenario(context, scenario_id)
    try:
        document = capability_application_service.get_capability(
            context.db,
            scenario,
            environment=environment,
            kind=kind,
            key=key,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        _application_error(exc)
    return ExternalCapabilityOut.model_validate(document)


@router.post(
    "/scenarios/{scenario_id}/capabilities/{kind}/{key}/invoke",
    response_model=ExternalCapabilityReceiptOut,
)
def invoke_capability(
    scenario_id: str,
    kind: ExternalCapabilityKind,
    key: str,
    payload: ExternalCapabilityInvocationIn,
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalCapabilityReceiptOut:
    external_api_service.require_scope(context, "capabilities:invoke")
    scenario = _owned_scenario(context, scenario_id)
    try:
        request = Request(
            capability=CapabilityRef(kind=kind, resource_id=key),
            inputs=payload.inputs,
            binding_overrides=tuple(
                capability_application_service.managed_binding_override(
                    item.runtime_document()
                )
                for item in payload.managed_inputs
            ),
            mode=payload.mode,
            idempotency_key=payload.idempotency_key,
            correlation_id=payload.correlation_id or f"rest:{uuid4().hex}",
            expected_definition_hash=payload.expected_definition_hash,
            expected_deployment_fingerprint=payload.expected_deployment_fingerprint,
            confirmation=(
                payload.confirmation.model_dump(mode="json", exclude_none=True)
                if payload.confirmation is not None
                else {}
            ),
            request_id=payload.request_id,
        )
        receipt = capability_application_service.invoke(
            context.db,
            scenario,
            _actor(context),
            request,
            environment=payload.environment,
            invocation_source="rest",
        )
        context.db.commit()
    except capability_application_service.CapabilityApplicationError as exc:
        context.db.rollback()
        _application_error(exc)
    except CapabilityInvocationError as exc:
        context.db.rollback()
        _invocation_error(exc)
    except CapabilityContractError as exc:
        context.db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_capability_request", "message": str(exc)},
        ) from exc
    return ExternalCapabilityReceiptOut.model_validate(
        capability_application_service.receipt_document(receipt)
    )


@router.get(
    "/invocations/{invocation_id}",
    response_model=ExternalCapabilityReceiptOut,
)
def get_invocation_receipt(
    invocation_id: str,
    context: external_api_service.ExternalApiContext = Depends(_external_context),
) -> ExternalCapabilityReceiptOut:
    external_api_service.require_scope(context, "capabilities:read")
    try:
        document = capability_application_service.get_receipt(
            context.db,
            _actor(context),
            invocation_id,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        _application_error(exc)
    return ExternalCapabilityReceiptOut.model_validate(document)


__all__ = ["router"]
