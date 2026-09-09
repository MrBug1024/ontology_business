"""Read-only hash compatibility for immutable snapshot contracts v1 and v2.

Historical labels are preserved solely to verify existing artifact digests.
They must never select data, permissions, connections or executable versions.
New captures use contract v3 and do not contain these labels.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def workflow_payload_context(
    *, run_id: str, scenario_id: str, workflow_id: str, environment: str,
    definition_hash: str,
) -> dict[str, str]:
    """Frozen v1 codec entry point retained for migration 20260829_09 only."""
    values = {
        "contract": "workflow-run-input-aad/v1", "run_id": str(run_id or "").strip(),
        "scenario_id": str(scenario_id or "").strip(), "workflow_id": str(workflow_id or "").strip(),
        "environment": str(environment or "").strip(), "definition_hash": str(definition_hash or "").strip(),
    }
    if any(not values[key] for key in ("run_id", "scenario_id", "workflow_id", "environment")):
        raise ValueError("Historical workflow input context is incomplete")
    return values


def binding_requirements(raw: Any) -> list[dict[str, str]]:
    from .connector_service import ConnectorBindingError, normalize_kind

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConnectorBindingError("连接器依赖必须是列表")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ConnectorBindingError("连接器依赖必须是对象")
        kind = normalize_kind(str(item.get("kind") or ""))
        key = item.get("binding_key")
        if not isinstance(key, str) or not 1 <= len(" ".join(key.split())) <= 180:
            raise ConnectorBindingError("连接器绑定键长度无效")
        key = " ".join(key.split())
        label = str(item.get("environment") or "dev").strip().lower()
        if label not in {"dev", "staging", "prod"}:
            raise ConnectorBindingError("历史发布标签无效")
        identity = (label, kind, key)
        if identity in seen:
            raise ConnectorBindingError("连接器依赖不能重复")
        seen.add(identity)
        result.append({
            "binding_key": key, "kind": kind, "environment": label,
            "reference_label": str(item.get("reference_label") or "").strip()[:300],
        })
    return sorted(result, key=lambda item: (item["environment"], item["kind"], item["binding_key"]))
