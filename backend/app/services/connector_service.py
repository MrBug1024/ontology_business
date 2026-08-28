"""Credential-free connector catalog and scenario/environment bindings.

The platform already has three mature configuration resources (data sources,
MCP servers and LLM deployments).  This module deliberately treats those as
the physical targets instead of copying their credentials into another table.
``ConnectorBinding`` is the small governance layer that records which target a
portable package reference may use in a particular scenario/environment.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, ConnectorBinding, DataSource, LLMConfig, MCPConfig
from . import datasource_service, llm_service, mcp_service


ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
CONNECTOR_KINDS = frozenset({"data_source", "mcp", "llm"})
HEALTH_STATUSES = frozenset({"unknown", "healthy", "unhealthy"})
RUNTIME_BINDING_FIELDS = {
    "data_source": ("data_source_binding_key", "data_source_binding_ref"),
    "mcp": ("mcp_binding_key", "mcp_binding_ref"),
    "llm": ("llm_binding_key", "llm_binding_ref"),
}


class ConnectorBindingError(ValueError):
    """A binding cannot be safely created, resolved or used."""


class ConnectorBindingConflictError(ConnectorBindingError):
    """The external target is stale, unavailable or no longer compatible."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _key_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff_.:-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-._:") or "unnamed"


def _text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConnectorBindingError(f"{label}必须是字符串")
    result = " ".join(value.split())
    if not result or len(result) > maximum:
        raise ConnectorBindingError(f"{label}长度无效")
    return result


def normalize_environment(environment: str) -> str:
    value = str(environment or "").strip().lower()
    if value not in ENVIRONMENTS:
        raise ConnectorBindingError("环境必须为 dev、staging 或 prod")
    return value


def normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in CONNECTOR_KINDS:
        raise ConnectorBindingError("连接器类型必须为 data_source、mcp 或 llm")
    return value


def runtime_binding_fields(kind: str) -> tuple[str, str]:
    """Return the public, credential-free config fields for one connector kind."""
    return RUNTIME_BINDING_FIELDS[normalize_kind(kind)]


def _runtime_reference(raw: Any) -> dict[str, Any]:
    """Keep only the compatibility facts that are safe to persist at runtime.

    A runtime binding key alone proves neither an adapter nor the capabilities
    expected by the caller.  The portable package compiler writes this compact
    descriptor alongside the key so a healthy but incompatible re-binding is
    still rejected.  Names, endpoints and credential-shaped fields are omitted.
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, Mapping):
        raise ConnectorBindingError("运行时连接器兼容性描述必须是对象")
    adapter_raw = raw.get("adapter") or raw.get("type") or ""
    if adapter_raw and (not isinstance(adapter_raw, str) or len(adapter_raw.strip()) > 80):
        raise ConnectorBindingError("运行时连接器适配器无效")
    adapter = adapter_raw.strip() if isinstance(adapter_raw, str) else ""
    capabilities_raw = raw.get("required_capabilities", [])
    if capabilities_raw is None:
        capabilities_raw = []
    if not isinstance(capabilities_raw, list) or len(capabilities_raw) > 40:
        raise ConnectorBindingError("运行时连接器能力描述无效")
    capabilities: list[str] = []
    for item in capabilities_raw:
        if not isinstance(item, str):
            raise ConnectorBindingError("运行时连接器能力必须是字符串")
        capability = item.strip().lower()
        if not capability or len(capability) > 80:
            raise ConnectorBindingError("运行时连接器能力无效")
        if capability not in capabilities:
            capabilities.append(capability)
    result: dict[str, Any] = {}
    if adapter:
        result["adapter"] = adapter
    if capabilities:
        result["required_capabilities"] = capabilities
    return result


def with_required_capabilities(reference: Any, *required: str) -> dict[str, Any]:
    """Return a normalized safe reference augmented with caller requirements.

    A generic data-source binding can be healthy while still being unsuitable
    for a SQL mapping (for example a file bucket).  Callers add the capability
    they actually execute so publish-time and runtime compatibility checks use
    the same contract.
    """
    normalized = _runtime_reference(reference)
    capabilities = list(normalized.get("required_capabilities") or [])
    for item in required:
        capability = str(item or "").strip().lower()
        if capability and capability not in capabilities:
            capabilities.append(capability)
    if capabilities:
        normalized["required_capabilities"] = capabilities
    return normalized


def runtime_binding_metadata(kind: str, reference: Any, path: str = "") -> dict[str, Any]:
    """Build safe logical binding metadata for a materialized package value."""
    normalized_kind = normalize_kind(kind)
    return {
        "binding_key": binding_key(normalized_kind, reference, path),
        "reference": _runtime_reference(reference),
    }


def runtime_binding_from_config(config: Any, kind: str) -> dict[str, Any] | None:
    """Read and validate an optional logical runtime binding from JSON config."""
    if not isinstance(config, Mapping):
        return None
    normalized_kind = normalize_kind(kind)
    key_field, ref_field = runtime_binding_fields(normalized_kind)
    raw_key = config.get(key_field)
    raw_ref = config.get(ref_field)
    if raw_key in (None, ""):
        if raw_ref not in (None, "", {}):
            raise ConnectorBindingError(f"{ref_field} 不能脱离 {key_field} 单独使用")
        return None
    return {
        "kind": normalized_kind,
        "binding_key": _text(raw_key, "运行时连接器绑定键", maximum=180),
        "reference": _runtime_reference(raw_ref),
    }


def binding_key(kind: str, reference: Any, path: str = "") -> str:
    """Make a deterministic, credential-free key for a portable reference.

    Data-source name/type and named MCP/LLM references survive an export/import.
    Legacy anonymous MCP/LLM placeholders fall back to a stable package path so
    they can still be bound without exposing their old runtime id.
    """
    normalized_kind = normalize_kind(kind)
    ref = reference if isinstance(reference, Mapping) else {}
    name = _key_token(ref.get("name"))
    if normalized_kind == "data_source":
        adapter = _key_token(ref.get("type") or ref.get("adapter"))
        if name != "unnamed" or adapter != "unnamed":
            return f"data_source:{name}:{adapter}"
    elif name != "unnamed":
        return f"{normalized_kind}:{name}"
    digest = hashlib.sha256(f"{normalized_kind}|{path}".encode("utf-8")).hexdigest()[:20]
    return f"{normalized_kind}:path:{digest}"


def binding_label(kind: str, reference: Any, path: str = "") -> str:
    normalized_kind = normalize_kind(kind)
    ref = reference if isinstance(reference, Mapping) else {}
    name = str(ref.get("name") or "").strip()
    if normalized_kind == "data_source":
        adapter = str(ref.get("type") or ref.get("adapter") or "").strip()
        if name or adapter:
            return f"数据源：{name or '未命名'}{f'（{adapter}）' if adapter else ''}"[:300]
    elif name:
        label = "MCP" if normalized_kind == "mcp" else "LLM"
        return f"{label}：{name}"[:300]
    labels = {"mcp": "MCP", "llm": "LLM", "data_source": "数据源"}
    return f"{labels[normalized_kind]} 外部引用（{path or '未命名位置'}）"[:300]


def normalize_snapshot_binding_requirements(raw: Any) -> list[dict[str, str]]:
    """Validate safe connector requirements stored in a release snapshot."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConnectorBindingError("连接器依赖必须是列表")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ConnectorBindingError("连接器依赖必须是对象")
        kind = normalize_kind(str(item.get("kind") or ""))
        key = _text(item.get("binding_key"), "连接器绑定键", maximum=180)
        environment = normalize_environment(str(item.get("environment") or "dev"))
        label = str(item.get("reference_label") or "").strip()[:300]
        identity = (environment, kind, key)
        if identity in seen:
            raise ConnectorBindingError("连接器依赖不能重复")
        seen.add(identity)
        normalized.append(
            {
                "binding_key": key,
                "kind": kind,
                "environment": environment,
                "reference_label": label,
            }
        )
    return sorted(normalized, key=lambda item: (item["environment"], item["kind"], item["binding_key"]))


_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|credential)\s*([=:])\s*[^\s,;]+")
_SECRET_URL = re.compile(r"(://[^\s/@:]+:)[^\s/@]+(@)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def sanitize_message(value: Any, *, maximum: int = 600) -> str:
    """Persist only a compact, redacted health diagnostic."""
    text = " ".join(str(value or "").split())
    text = _SECRET_URL.sub(r"\1[REDACTED]\2", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    text = _SECRET_NAME.sub(r"\1\2[REDACTED]", text)
    return text[:maximum]


def _model_for_kind(kind: str):
    return {"data_source": DataSource, "mcp": MCPConfig, "llm": LLMConfig}[kind]


def _connector_enabled(kind: str, connector: Any) -> bool:
    return True if kind == "data_source" else bool(getattr(connector, "enabled", False))


def _secret_state(kind: str, connector: Any) -> str:
    if kind == "data_source":
        if str(getattr(connector, "type", "")) in {"file_bucket", "dataset"}:
            return "not_required"
        return "configured" if bool(getattr(connector, "config", {}) or {}) else "missing"
    if kind == "mcp":
        env = getattr(connector, "env", {}) or {}
        headers = getattr(connector, "headers", {}) or {}
        return "configured" if env or headers else "not_required"
    return "configured" if bool(getattr(connector, "api_key", "")) else "not_required"


def _adapter(kind: str, connector: Any) -> str:
    if kind == "data_source":
        return str(getattr(connector, "type", ""))
    if kind == "mcp":
        return str(getattr(connector, "transport", ""))
    return str(getattr(connector, "provider", ""))


def _capabilities(kind: str, connector: Any) -> list[str]:
    if kind == "data_source":
        return ["document_search"] if getattr(connector, "type", "") == "file_bucket" else ["sql_read", "schema"]
    if kind == "mcp":
        return ["tool"]
    values = getattr(connector, "capabilities", []) or []
    return sorted({str(item).strip().lower() for item in values if str(item).strip()})


def connector_revision(connector: Any) -> int:
    """Return the durable, opaque revision for a physical connector target.

    Revisions are persisted on all three connector models and are deliberately
    separate from the public-shape signature: configuration values and secrets
    must never be copied into a release audit merely to pin a target.
    """
    raw = getattr(connector, "connector_revision", None)
    if isinstance(raw, bool):
        raise ConnectorBindingError("连接器修订版本无效，请重新保存连接器配置")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorBindingError("连接器修订版本无效，请重新保存连接器配置") from exc
    if value < 1:
        raise ConnectorBindingError("连接器修订版本无效，请重新保存连接器配置")
    return value


def _signature_payload(kind: str, connector: Any) -> dict[str, Any]:
    """Use public shape fields plus an opaque persisted configuration pin."""
    common = {
        "id": str(getattr(connector, "id", "")),
        "name": str(getattr(connector, "name", "")),
        "adapter": _adapter(kind, connector),
        "tenant_id": str(getattr(connector, "tenant_id", "") or ""),
        "enabled": _connector_enabled(kind, connector),
        "connector_revision": connector_revision(connector),
    }
    if kind == "data_source":
        common.update(
            {
                "scenario_id": str(getattr(connector, "scenario_id", "") or ""),
                "config_keys": sorted(str(key) for key in (getattr(connector, "config", {}) or {})),
                "status": str(getattr(connector, "status", "unknown") or "unknown"),
            }
        )
    elif kind == "mcp":
        common.update(
            {
                "command": str(getattr(connector, "command", "") or ""),
                "args": list(getattr(connector, "args", []) or []),
                "url_set": bool(getattr(connector, "url", "")),
                "env_keys": sorted(str(key) for key in (getattr(connector, "env", {}) or {})),
                "header_keys": sorted(str(key) for key in (getattr(connector, "headers", {}) or {})),
            }
        )
    else:
        common.update(
            {
                "base_url_set": bool(getattr(connector, "base_url", "")),
                "model": str(getattr(connector, "model", "") or ""),
                "capabilities": _capabilities(kind, connector),
            }
        )
    return common


def connector_signature(kind: str, connector: Any) -> str:
    return hashlib.sha256(_canonical(_signature_payload(kind, connector)).encode("utf-8")).hexdigest()


def _resolve_connector(
    db: Session,
    kind: str,
    connector_id: str,
    scenario: BusinessScenario,
) -> Any:
    normalized_kind = normalize_kind(kind)
    value = str(connector_id or "").strip()
    if not value or len(value) > 32:
        raise ConnectorBindingError("连接器目标无效")
    connector = db.get(_model_for_kind(normalized_kind), value)
    if connector is None:
        raise ConnectorBindingConflictError("绑定的连接器已不存在")
    if str(getattr(connector, "tenant_id", "") or "") != str(scenario.tenant_id or ""):
        raise ConnectorBindingConflictError("连接器必须属于当前租户")
    if normalized_kind == "data_source" and getattr(connector, "scenario_id", None) not in {None, scenario.id}:
        raise ConnectorBindingConflictError("数据源只能绑定到当前场景或租户级范围")
    if not _connector_enabled(normalized_kind, connector):
        raise ConnectorBindingConflictError("连接器已停用")
    return connector


def require_connector_target(
    db: Session,
    scenario: BusinessScenario,
    *,
    kind: str,
    connector_id: str,
) -> Any:
    """Resolve a legacy physical target with the same tenant/scope rules.

    This is intentionally public so runtime callers never reimplement the
    polymorphic connector lookup and accidentally skip scenario or tenant checks.
    """
    return _resolve_connector(db, kind, connector_id, scenario)


def _binding_query(scenario: BusinessScenario, environment: str, key: str):
    return select(ConnectorBinding).where(
        ConnectorBinding.scenario_id == scenario.id,
        ConnectorBinding.tenant_id == scenario.tenant_id,
        ConnectorBinding.environment == environment,
        ConnectorBinding.binding_key == key,
    )


def _binding_state(
    db: Session,
    binding: ConnectorBinding,
    scenario: BusinessScenario,
) -> tuple[bool, str, Any | None]:
    try:
        connector = _resolve_connector(db, binding.connector_kind, binding.connector_id, scenario)
    except ConnectorBindingError as exc:
        return False, str(exc), None
    if binding.health_status != "healthy":
        message = binding.health_message or "连接器尚未通过健康检查"
        return False, message, connector
    if binding.connector_signature != connector_signature(binding.connector_kind, connector):
        return False, "连接器配置已变更，请重新执行健康检查", connector
    if binding.connector_kind == "data_source" and getattr(connector, "status", "unknown") == "error":
        return False, "数据源当前健康检查失败，请修复后重新验证绑定", connector
    return True, "", connector


def connector_summary(
    connector: Any,
    kind: str,
    *,
    binding: ConnectorBinding | None = None,
) -> dict[str, Any]:
    normalized_kind = normalize_kind(kind)
    if binding is not None:
        health = binding.health_status if binding.health_status in HEALTH_STATUSES else "unknown"
        checked_at = binding.checked_at
        message = binding.health_message or ""
    elif normalized_kind == "data_source":
        raw = str(getattr(connector, "status", "unknown") or "unknown")
        health = {"ok": "healthy", "error": "unhealthy"}.get(raw, "unknown")
        checked_at = None
        message = sanitize_message(getattr(connector, "last_error", "")) if raw == "error" else ""
    else:
        health, checked_at, message = "unknown", None, ""
    return {
        "id": str(getattr(connector, "id", "")),
        "name": str(getattr(connector, "name", "")),
        "kind": normalized_kind,
        "adapter_type": _adapter(normalized_kind, connector),
        "scenario_id": getattr(connector, "scenario_id", None) if normalized_kind == "data_source" else None,
        "enabled": _connector_enabled(normalized_kind, connector),
        "secret_state": _secret_state(normalized_kind, connector),
        "health": health,
        "checked_at": checked_at,
        "message": message,
        "capabilities": _capabilities(normalized_kind, connector),
    }


def list_catalog(db: Session, scenario: BusinessScenario) -> list[dict[str, Any]]:
    """Return only same-tenant targets legal for the chosen scenario."""
    tenant_id = scenario.tenant_id
    sources = db.execute(
        select(DataSource).where(
            DataSource.tenant_id == tenant_id,
            or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == scenario.id),
        )
    ).scalars().all()
    mcps = db.execute(select(MCPConfig).where(MCPConfig.tenant_id == tenant_id)).scalars().all()
    llms = db.execute(select(LLMConfig).where(LLMConfig.tenant_id == tenant_id)).scalars().all()
    result = [connector_summary(item, "data_source") for item in sources]
    result.extend(connector_summary(item, "mcp") for item in mcps)
    result.extend(connector_summary(item, "llm") for item in llms)
    return sorted(result, key=lambda item: (item["kind"], item["name"].lower(), item["id"]))


def get_binding(db: Session, binding_id: str, scenario: BusinessScenario | None = None) -> ConnectorBinding:
    binding = db.get(ConnectorBinding, str(binding_id or "").strip())
    if not binding:
        raise ConnectorBindingError("环境绑定不存在")
    if scenario and (binding.scenario_id != scenario.id or binding.tenant_id != scenario.tenant_id):
        raise ConnectorBindingError("环境绑定不存在")
    return binding


def binding_summary(db: Session, binding: ConnectorBinding, scenario: BusinessScenario) -> dict[str, Any]:
    ready, reason, connector = _binding_state(db, binding, scenario)
    item = connector_summary(connector, binding.connector_kind, binding=binding) if connector else {
        "id": binding.connector_id,
        "name": "已删除连接器",
        "kind": binding.connector_kind,
        "adapter_type": "",
        "scenario_id": None,
        "enabled": False,
        "secret_state": "missing",
        "health": "unhealthy",
        "checked_at": binding.checked_at,
        "message": sanitize_message(reason),
        "capabilities": [],
    }
    item.update(
        {
            "binding_id": binding.id,
            "binding_key": binding.binding_key,
            "reference_label": binding.reference_label or "",
            "environment": binding.environment,
            "ready": ready,
            "blocking_reason": sanitize_message(reason),
            "created_at": binding.created_at,
            "updated_at": binding.updated_at,
        }
    )
    return item


def list_bindings(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(ConnectorBinding).where(
        ConnectorBinding.scenario_id == scenario.id,
        ConnectorBinding.tenant_id == scenario.tenant_id,
    )
    if environment:
        stmt = stmt.where(ConnectorBinding.environment == normalize_environment(environment))
    bindings = db.execute(stmt.order_by(ConnectorBinding.environment, ConnectorBinding.binding_key)).scalars().all()
    return [binding_summary(db, binding, scenario) for binding in bindings]


def _check_compatibility(
    *,
    kind: str,
    connector: Any,
    reference: Mapping[str, Any] | None = None,
) -> None:
    reference = reference or {}
    expected_adapter = str(reference.get("type") or reference.get("adapter") or "").strip()
    if expected_adapter and _adapter(kind, connector) != expected_adapter:
        raise ConnectorBindingConflictError("连接器适配器与资源包外部引用不匹配")
    wanted = reference.get("required_capabilities")
    if isinstance(wanted, list):
        needed = {str(value).strip().lower() for value in wanted if str(value).strip()}
        available = set(_capabilities(kind, connector))
        if not needed.issubset(available):
            raise ConnectorBindingConflictError("连接器能力不满足资源包要求")


def upsert_binding(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    binding_key_value: str,
    kind: str,
    connector_id: str,
    reference_label: str = "",
    check: bool = False,
    created_by_user_id: str | None = None,
) -> ConnectorBinding:
    resolved_environment = normalize_environment(environment)
    normalized_kind = normalize_kind(kind)
    key = _text(binding_key_value, "连接器绑定键", maximum=180)
    connector = _resolve_connector(db, normalized_kind, connector_id, scenario)
    label = str(reference_label or "").strip()[:300]
    binding = db.execute(_binding_query(scenario, resolved_environment, key)).scalars().first()
    if binding and binding.connector_kind != normalized_kind:
        raise ConnectorBindingConflictError("同一环境绑定键不能指向不同连接器类型")
    if binding is None:
        binding = ConnectorBinding(
            tenant_id=str(scenario.tenant_id),
            scenario_id=scenario.id,
            environment=resolved_environment,
            binding_key=key,
            reference_label=label,
            connector_kind=normalized_kind,
            connector_id=str(connector.id),
            created_by_user_id=created_by_user_id,
        )
        db.add(binding)
        db.flush()
    else:
        target_changed = binding.connector_id != str(connector.id)
        binding.reference_label = label or binding.reference_label
        binding.connector_id = str(connector.id)
        if target_changed:
            binding.health_status = "unknown"
            binding.health_message = "连接器目标已变更，请重新执行健康检查"
            binding.connector_signature = ""
            binding.checked_at = None
    if check:
        check_binding(db, binding, scenario)
    return binding


def check_binding(db: Session, binding: ConnectorBinding, scenario: BusinessScenario) -> ConnectorBinding:
    """Run an explicit health check and persist only a sanitized result."""
    connector = _resolve_connector(db, binding.connector_kind, binding.connector_id, scenario)
    try:
        if binding.connector_kind == "data_source":
            if connector.type == "file_bucket":
                ok, message = True, "文件桶就绪"
            else:
                ok, message = datasource_service.test_connection(connector)
            connector.status = "ok" if ok else "error"
            connector.last_error = "" if ok else sanitize_message(message)
        elif binding.connector_kind == "mcp":
            ok, message = mcp_service.test_connection(connector)
        else:
            ok, message = llm_service.test_connection(connector, db=db)
    except Exception as exc:  # noqa: BLE001 - errors are intentionally converted to a safe status.
        ok, message = False, sanitize_message(exc)
    binding.health_status = "healthy" if ok else "unhealthy"
    binding.health_message = "" if ok else sanitize_message(message)
    binding.checked_at = _now()
    binding.connector_signature = connector_signature(binding.connector_kind, connector)
    return binding


def require_ready_binding(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    binding_key_value: str,
    kind: str,
    reference: Mapping[str, Any] | None = None,
) -> tuple[ConnectorBinding, Any]:
    resolved_environment = normalize_environment(environment)
    normalized_kind = normalize_kind(kind)
    key = _text(binding_key_value, "连接器绑定键", maximum=180)
    binding = db.execute(_binding_query(scenario, resolved_environment, key)).scalars().first()
    if binding is None:
        raise ConnectorBindingConflictError("目标环境尚未配置该连接器绑定")
    if binding.connector_kind != normalized_kind:
        raise ConnectorBindingConflictError("目标环境连接器绑定类型不匹配")
    ready, reason, connector = _binding_state(db, binding, scenario)
    if not ready or connector is None:
        raise ConnectorBindingConflictError(reason or "目标环境连接器不可用")
    _check_compatibility(kind=normalized_kind, connector=connector, reference=reference)
    return binding, connector


def requirement_resolution(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    kind: str,
    reference: Mapping[str, Any] | None,
    path: str,
    binding_key_value: str | None = None,
) -> dict[str, Any]:
    """Resolve a package reference or return a safe, user-actionable blocker."""
    normalized_kind = normalize_kind(kind)
    reference = reference or {}
    key = (
        _text(binding_key_value, "连接器绑定键", maximum=180)
        if binding_key_value not in (None, "")
        else binding_key(normalized_kind, reference, path)
    )
    label = binding_label(normalized_kind, reference, path)
    configured_binding = db.execute(
        _binding_query(scenario, normalize_environment(environment), key)
    ).scalars().first()
    try:
        binding, connector = require_ready_binding(
            db,
            scenario,
            environment=environment,
            binding_key_value=key,
            kind=normalized_kind,
            reference=reference,
        )
    except ConnectorBindingConflictError as exc:
        return {
            "resolved": False,
            "binding_key": key,
            "kind": normalized_kind,
            "path": path,
            "reference_label": label,
            "reason": sanitize_message(exc),
            "configured": configured_binding is not None,
        }
    return {
        "resolved": True,
        "binding_key": key,
        "kind": normalized_kind,
        "path": path,
        "reference_label": label,
        "binding_id": binding.id,
        "connector_id": str(connector.id),
        "connector_name": str(connector.name),
        "adapter_type": _adapter(normalized_kind, connector),
        "configured": True,
    }


def validate_snapshot_bindings(
    db: Session,
    scenario: BusinessScenario,
    content: Mapping[str, Any],
    *,
    environment: str,
) -> list[dict[str, Any]]:
    """Recheck persisted requirements without triggering external I/O."""
    resolved_environment = normalize_environment(environment)
    requirements = normalize_snapshot_binding_requirements(content.get("connector_bindings"))
    audit: list[dict[str, Any]] = []
    for requirement in requirements:
        binding, connector = require_ready_binding(
            db,
            scenario,
            environment=resolved_environment,
            binding_key_value=requirement["binding_key"],
            kind=requirement["kind"],
        )
        audit.append(
            {
                "binding_id": binding.id,
                "binding_key": requirement["binding_key"],
                "kind": requirement["kind"],
                "connector_id": str(connector.id),
                "connector_name": str(connector.name),
                "adapter_type": _adapter(requirement["kind"], connector),
                "connector_signature": connector_signature(requirement["kind"], connector),
                "connector_revision": connector_revision(connector),
                "environment": resolved_environment,
                "health_status": "healthy",
                "checked_at": binding.checked_at.isoformat() if binding.checked_at else None,
            }
        )
    return audit


def readiness(
    db: Session,
    scenario: BusinessScenario,
    content: Mapping[str, Any],
    *,
    environment: str,
) -> dict[str, Any]:
    """Return a UI-safe publish gate decision; no connection test is performed."""
    try:
        audit = validate_snapshot_bindings(db, scenario, content, environment=environment)
        return {"ready": True, "environment": normalize_environment(environment), "reasons": [], "audit": audit}
    except ConnectorBindingError as exc:
        return {
            "ready": False,
            "environment": normalize_environment(environment),
            "reasons": [sanitize_message(exc)],
            "audit": [],
        }


def invalidate_connector_bindings(
    db: Session,
    kind: str,
    connector_id: str,
    *,
    message: str = "连接器配置已变更，请重新执行健康检查",
) -> None:
    normalized_kind = normalize_kind(kind)
    bindings = db.execute(
        select(ConnectorBinding).where(
            ConnectorBinding.connector_kind == normalized_kind,
            ConnectorBinding.connector_id == str(connector_id),
        )
    ).scalars().all()
    for binding in bindings:
        binding.health_status = "unknown"
        binding.health_message = sanitize_message(message)
        binding.connector_signature = ""
        binding.checked_at = None


def assert_connector_not_bound(db: Session, kind: str, connector_id: str) -> None:
    normalized_kind = normalize_kind(kind)
    binding = db.execute(
        select(ConnectorBinding).where(
            ConnectorBinding.connector_kind == normalized_kind,
            ConnectorBinding.connector_id == str(connector_id),
        ).limit(1)
    ).scalars().first()
    if binding:
        raise ConnectorBindingConflictError("连接器仍被场景环境绑定引用；请先迁移或删除绑定")
