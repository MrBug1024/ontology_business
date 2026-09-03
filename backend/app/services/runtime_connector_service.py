"""Environment-aware runtime resolution for governed connector bindings.

Connector credentials remain on their physical DataSource/MCP/LLM records.  A
runtime caller supplies only its scenario and declarative config; this service
selects the physical target for the server's fixed deployment environment and
returns credential-free audit facts suitable for execution logs.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BusinessScenario, OntologyRelease, OntologySnapshot
from . import connector_service, release_service


class RuntimeConnectorError(connector_service.ConnectorBindingConflictError):
    """A runtime must not use the requested connector in its environment."""


_DIRECT_ID_FIELDS = {
    "data_source": "data_source_id",
    "mcp": "mcp_id",
    "llm": "llm_config_id",
}


def _configured_runtime_environment() -> str:
    """Return the one environment this server process is allowed to run."""
    try:
        return connector_service.normalize_environment(get_settings().runtime_environment)
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeConnectorError(str(exc)) from exc


def runtime_environment(environment: str | None = None) -> str:
    """Return the fixed deployment environment and reject cross-env overrides.

    ``environment`` is intentionally accepted only for durable workflow runs
    which carry the environment they were queued in.  It is an assertion, not
    a selector: a dev worker must never execute a staging/prod run (or vice
    versa) merely because a persisted value says so.
    """
    configured = _configured_runtime_environment()
    if environment in (None, ""):
        return configured
    try:
        requested = connector_service.normalize_environment(environment)
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeConnectorError(str(exc)) from exc
    if requested != configured:
        raise RuntimeConnectorError(
            f"运行环境 {requested} 与当前部署环境 {configured} 不一致，已安全阻断"
        )
    return configured


def _pinned_release(
    db: Session,
    scenario: BusinessScenario,
    release_id: str,
) -> OntologyRelease:
    """Return one explicitly pinned release without consulting live definitions.

    A durable workflow run will eventually carry a release id chosen at enqueue
    time.  Such a run must continue to resolve the connector evidence from that
    exact release even after a newer definition is merged or published.  This
    A pin is an assertion, never a way to select another scenario.  The
    deployment environment selects a local connector binding, not an ontology
    version.  Validate every ownership edge and the immutable snapshot digest
    before accepting it, then let ``_verify_release_audit`` below check the
    logical binding on every execution.
    """
    normalized_id = str(release_id or "").strip()
    if not normalized_id:
        raise RuntimeConnectorError("固定发布记录标识不能为空")
    release = db.get(OntologyRelease, normalized_id)
    if release is None:
        raise RuntimeConnectorError("固定发布记录不存在")
    if release.scenario_id != scenario.id or release.tenant_id != scenario.tenant_id:
        raise RuntimeConnectorError("固定发布记录不属于当前业务场景")
    # ``superseded`` / ``rolled_back`` records can still be referenced by a
    # previously queued run.  Their connector audit remains immutable and is
    # safer than silently switching that run to a newer release.  Unknown or
    # incomplete legacy states cannot become a runtime authority.
    if release.status not in {"released", "superseded", "rolled_back"}:
        raise RuntimeConnectorError("固定发布记录状态不可用于运行时解析")

    snapshot = db.get(OntologySnapshot, release.snapshot_id)
    if (
        snapshot is None
        or snapshot.id != release.snapshot_id
        or snapshot.scenario_id != scenario.id
        or snapshot.tenant_id != scenario.tenant_id
    ):
        raise RuntimeConnectorError("固定发布记录的本体快照不可用或不属于当前场景")
    try:
        normalized_content = release_service.normalize_snapshot_content(snapshot.content or {})
        expected_hash = release_service.snapshot_hash(normalized_content)
    except Exception as exc:  # noqa: BLE001 - untrusted legacy JSON must fail closed.
        raise RuntimeConnectorError("固定发布记录的本体快照校验失败") from exc
    if not snapshot.content_hash or snapshot.content_hash != expected_hash:
        raise RuntimeConnectorError("固定发布记录的本体快照完整性校验失败")
    return release


def _verify_release_audit(
    release: OntologyRelease,
    *,
    metadata: Mapping[str, Any],
    connector: Any,
    environment: str,
) -> None:
    """Verify a released logical binding against this deployment.

    Connector IDs and signatures are deployment-local.  A release created
    while checking one deployment can execute in another as long as both have
    a healthy connector for the same declared binding key/reference.  Preserve
    the strict physical connector check when the execution deployment matches
    the audit deployment, where it detects a post-release connector change.
    """
    kind = str(metadata["kind"])
    key = str(metadata["binding_key"])
    audit = next(
        (
            item
            for item in (release.connector_audit or [])
            if isinstance(item, Mapping)
            and item.get("kind") == kind
            and item.get("binding_key") == key
        ),
        None,
    )
    if audit is None:
        raise RuntimeConnectorError("当前连接器绑定未包含在发布审计中；请重新发布")
    audit_environment = str(audit.get("environment") or "").strip()
    if not audit_environment:
        raise RuntimeConnectorError("发布审计缺少连接器部署环境；请重新发布")
    if audit_environment != environment:
        # ``require_ready_binding`` has already proved this deployment owns a
        # healthy target for the immutable logical binding.  Comparing it to
        # the publisher's physical connector would make deployment labels
        # change product behavior and prevent valid multi-environment setup.
        return
    if str(audit.get("connector_id") or "") != str(getattr(connector, "id", "") or ""):
        raise RuntimeConnectorError("连接器目标在发布后已变更；请重新发布")
    expected_signature = str(audit.get("connector_signature") or "")
    if not expected_signature:
        raise RuntimeConnectorError("发布审计缺少连接器签名；请重新发布")
    expected_revision = audit.get("connector_revision")
    # Release audit is generated from a JSON integer.  Do not coerce strings
    # or booleans here: accepting malformed legacy/user-written JSON would
    # create a downgrade path around the immutable target pin.
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise RuntimeConnectorError("发布审计缺少连接器修订版本；请重新发布")
    try:
        current_revision = connector_service.connector_revision(connector)
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeConnectorError(str(exc)) from exc
    if expected_revision != current_revision:
        raise RuntimeConnectorError("连接器配置在发布后已变更；请重新检查并发布")
    current_signature = connector_service.connector_signature(kind, connector)
    if expected_signature != current_signature:
        raise RuntimeConnectorError("连接器配置在发布后已变更；请重新检查并发布")


def _audit(
    *,
    kind: str,
    environment: str,
    connector: Any,
    binding_key: str | None = None,
    binding_id: str | None = None,
    managed: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "environment": environment,
        "managed": managed,
        "binding_key": binding_key,
        "binding_id": binding_id,
        "connector_id": str(getattr(connector, "id", "") or ""),
        "connector_name": str(getattr(connector, "name", "") or ""),
        "adapter_type": (
            str(getattr(connector, "type", "") or "")
            if kind == "data_source"
            else str(
                getattr(connector, "transport", "")
                if kind == "mcp"
                else getattr(connector, "provider", "")
            )
        ),
    }


def resolve_connector(
    db: Session,
    scenario: BusinessScenario,
    *,
    kind: str,
    config: Mapping[str, Any] | None,
    environment: str | None = None,
    release_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve an Action/DAG config to this deployment's physical target.

    New packages contain an explicit logical key and compatibility descriptor.
    Mutable authoring/debug flows can use legacy direct IDs in any deployment
    while definitions are migrated.  A caller that supplies a frozen release
    pin must use a logical binding key, keeping published execution portable
    and auditable without turning dev/prod into a feature switch.

    ``release_id`` is reserved for a durable caller that has already pinned a
    governed release.  It validates immutable scenario ownership and release
    audit evidence, but never selects the deployment environment; that is
    determined solely by the running process's infrastructure configuration.
    """
    normalized_kind = connector_service.normalize_kind(kind)
    resolved_environment = runtime_environment(environment)
    runtime_config: Mapping[str, Any] = config or {}
    try:
        metadata = connector_service.runtime_binding_from_config(runtime_config, normalized_kind)
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeConnectorError(str(exc)) from exc

    release = None
    if release_id not in (None, ""):
        release = _pinned_release(
            db,
            scenario,
            str(release_id),
        )

    if metadata is not None:
        try:
            binding, connector = connector_service.require_ready_binding(
                db,
                scenario,
                environment=resolved_environment,
                binding_key_value=str(metadata["binding_key"]),
                kind=normalized_kind,
                reference=metadata.get("reference") or {},
            )
        except connector_service.ConnectorBindingError as exc:
            raise RuntimeConnectorError(str(exc)) from exc
        if release is not None:
            _verify_release_audit(
                release,
                metadata=metadata,
                connector=connector,
                environment=resolved_environment,
            )
        return connector, _audit(
            kind=normalized_kind,
            environment=resolved_environment,
            connector=connector,
            binding_key=str(metadata["binding_key"]),
            binding_id=binding.id,
            managed=True,
        )

    if release is not None:
        raise RuntimeConnectorError(
            f"已发布执行的 {normalized_kind} 必须配置运行时连接器绑定键"
        )
    direct_id = str(runtime_config.get(_DIRECT_ID_FIELDS[normalized_kind]) or "").strip()
    if not direct_id:
        raise RuntimeConnectorError(f"{normalized_kind} 执行器缺少连接器配置")
    try:
        connector = connector_service.require_connector_target(
            db,
            scenario,
            kind=normalized_kind,
            connector_id=direct_id,
        )
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeConnectorError(str(exc)) from exc
    return connector, _audit(
        kind=normalized_kind,
        environment=resolved_environment,
        connector=connector,
        managed=False,
    )
