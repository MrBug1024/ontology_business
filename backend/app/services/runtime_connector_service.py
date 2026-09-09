"""Resolve explicitly managed connectors and verify immutable release evidence."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from ..models import BusinessScenario, OntologyRelease, OntologySnapshot
from . import connector_service, release_service


class RuntimeConnectorError(connector_service.ConnectorBindingConflictError):
    """A runtime must not use the requested connector in its governed binding."""


_DIRECT_ID_FIELDS = {
    "data_source": "data_source_id",
    "mcp": "mcp_id",
    "llm": "llm_config_id",
}








def _pinned_release(
    db: Session,
    scenario: BusinessScenario,
    release_id: str,
) -> OntologyRelease:
    """Verify the tenant, scenario and immutable artifact of a durable release pin."""
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
    if release.status not in {"released", "superseded", "rolled_back", "retired"}:
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
) -> None:
    """Reject binding targets or revisions changed after publication."""
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
        raise RuntimeConnectorError("当前连接器绑定未包含在该版本的发布审计中；请重新发布")
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
    connector: Any,
    binding_key: str | None = None,
    binding_id: str | None = None,
    managed: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
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
    release_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve an explicit binding. Published definitions require managed keys."""
    normalized_kind = connector_service.normalize_kind(kind)
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
                binding_key_value=str(metadata["binding_key"]),
                kind=normalized_kind,
                reference=metadata.get("reference") or {},
            )
        except connector_service.ConnectorBindingError as exc:
            raise RuntimeConnectorError(str(exc)) from exc
        if release is not None:
            _verify_release_audit(release, metadata=metadata, connector=connector)
        return connector, _audit(
            kind=normalized_kind,
            connector=connector,
            binding_key=str(metadata["binding_key"]),
            binding_id=binding.id,
            managed=True,
        )

    if release is not None:
        raise RuntimeConnectorError(
            f"发布能力的 {normalized_kind} 必须配置运行时连接器绑定键"
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
        connector=connector,
        managed=False,
    )
