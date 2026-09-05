"""Definition-time validation and UI manifests for trusted Providers.

The registry remains the sole source of executable Provider identities.  This
module exposes only bounded, data-only authoring metadata and invokes an exact
Provider-owned validator before a function definition can be persisted or
released.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from .capability_contracts import canonical_json
from .capability_registry import (
    CapabilityProviderRegistry,
    CapabilityRegistryError,
    default_provider_registry,
)
from .function_definition_service import normalize_schema


class ProviderDefinitionError(ValueError):
    """A Provider identity, authoring manifest, or definition is invalid."""


_MANIFEST_FIELDS = {
    "capability_kind",
    "display_name",
    "description",
    "config_schema",
    "default_config",
    "input_schema",
    "output_schema",
    "input_schema_mode",
    "output_schema_mode",
    "ui_schema",
    "deprecated",
    "migration_message",
}
_UI_CONTROLS = {
    "checkbox",
    "number",
    "select",
    "semantic_mapping_multiselect",
    "text",
}
_UI_FIELD_KEYS = {"control", "label", "help", "placeholder"}
_MAX_MANIFEST_BYTES = 64_000


def _plain(value: Any, *, label: str) -> Any:
    try:
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > _MAX_MANIFEST_BYTES:
            raise ProviderDefinitionError(f"{label}过大")
        return json.loads(encoded)
    except ProviderDefinitionError:
        raise
    except Exception as exc:  # noqa: BLE001 - Provider internals stay hidden.
        raise ProviderDefinitionError(f"{label}必须是稳定 JSON") from exc


def _text(value: Any, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProviderDefinitionError(f"{label}必须是字符串")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ProviderDefinitionError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise ProviderDefinitionError(f"{label}长度不能超过 {maximum}")
    return normalized


def _normalize_ui_schema(value: Any, config_schema: Mapping[str, Any]) -> dict[str, Any]:
    plain = _plain(value, label="Provider UI Schema")
    if not isinstance(plain, dict):
        raise ProviderDefinitionError("Provider UI Schema 必须是对象")
    properties = config_schema.get("properties")
    declared = set(properties) if isinstance(properties, Mapping) else set()
    if set(plain) - declared:
        raise ProviderDefinitionError("Provider UI Schema 引用了未声明的配置字段")
    normalized: dict[str, Any] = {}
    for field_name, raw_descriptor in plain.items():
        if not isinstance(raw_descriptor, dict) or set(raw_descriptor) - _UI_FIELD_KEYS:
            raise ProviderDefinitionError("Provider UI 字段描述包含不支持的字段")
        control = _text(
            raw_descriptor.get("control", "text"),
            label="Provider UI control",
            maximum=60,
        )
        if control not in _UI_CONTROLS:
            raise ProviderDefinitionError("Provider UI control 不受支持")
        descriptor = {"control": control}
        for key in ("label", "help", "placeholder"):
            if key in raw_descriptor:
                descriptor[key] = _text(
                    raw_descriptor[key],
                    label=f"Provider UI {key}",
                    maximum=500,
                    allow_empty=True,
                )
        normalized[str(field_name)] = descriptor
    return normalized


def _manifest(provider: Any) -> dict[str, Any] | None:
    factory = getattr(provider, "definition_manifest", None)
    if not callable(factory):
        return None
    try:
        raw = factory()
    except Exception as exc:  # noqa: BLE001 - do not expose Provider internals.
        raise ProviderDefinitionError("Provider 定义清单生成失败") from exc
    plain = _plain(raw, label="Provider 定义清单")
    if not isinstance(plain, dict) or set(plain) - _MANIFEST_FIELDS:
        raise ProviderDefinitionError("Provider 定义清单包含不支持的字段")
    if plain.get("capability_kind") != "function":
        raise ProviderDefinitionError("Provider 定义清单仅支持 function 能力")
    config_schema = normalize_schema(
        plain.get("config_schema"),
        label="Provider 配置 Schema",
    )
    if config_schema.get("additionalProperties") is not False:
        raise ProviderDefinitionError("Provider 配置 Schema 必须关闭额外字段")
    input_schema = normalize_schema(plain.get("input_schema"), label="Provider 输入 Schema")
    output_schema = normalize_schema(
        plain.get("output_schema"), label="Provider 输出 Schema"
    )
    default_config = _plain(plain.get("default_config", {}), label="Provider 默认配置")
    if not isinstance(default_config, dict):
        raise ProviderDefinitionError("Provider 默认配置必须是对象")
    input_mode = str(plain.get("input_schema_mode") or "fixed").strip()
    output_mode = str(plain.get("output_schema_mode") or "fixed").strip()
    if input_mode not in {"fixed", "editable"} or output_mode not in {
        "fixed",
        "editable",
    }:
        raise ProviderDefinitionError("Provider Schema 编辑模式无效")
    return {
        "provider_key": str(provider.provider_key),
        "provider_version": str(provider.provider_version),
        "capability_kind": "function",
        "display_name": _text(
            plain.get("display_name"), label="Provider 显示名称", maximum=160
        ),
        "description": _text(
            plain.get("description", ""),
            label="Provider 说明",
            maximum=2_000,
            allow_empty=True,
        ),
        "config_schema": config_schema,
        "default_config": default_config,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "input_schema_mode": input_mode,
        "output_schema_mode": output_mode,
        "ui_schema": _normalize_ui_schema(plain.get("ui_schema", {}), config_schema),
        "deprecated": bool(plain.get("deprecated", False)),
        "migration_message": _text(
            plain.get("migration_message", ""),
            label="Provider 迁移说明",
            maximum=1_000,
            allow_empty=True,
        ),
    }


def list_function_provider_manifests(
    *,
    registry: CapabilityProviderRegistry | None = None,
) -> list[dict[str, Any]]:
    """Return bounded manifests for statically registered function Providers."""

    trusted_registry = registry or default_provider_registry
    manifests: list[dict[str, Any]] = []
    for provider_key, provider_version in trusted_registry.identities():
        try:
            provider = trusted_registry.resolve(provider_key, provider_version)
            manifest = _manifest(provider)
        except CapabilityRegistryError as exc:
            raise ProviderDefinitionError("Provider 定义清单解析失败") from exc
        if manifest is not None:
            manifests.append(manifest)
    return sorted(
        manifests,
        key=lambda item: (
            bool(item["deprecated"]),
            str(item["display_name"]).casefold(),
            str(item["provider_key"]),
            str(item["provider_version"]),
        ),
    )


def validate_function_definition(
    declaration: Mapping[str, Any],
    *,
    registry: CapabilityProviderRegistry | None = None,
    compatibility_mode: bool = False,
) -> dict[str, Any]:
    """Resolve and validate one already-normalized function Provider binding."""

    normalized = copy.deepcopy(dict(declaration))
    if str(normalized.get("runtime_kind") or "") != "provider":
        return normalized
    runtime_config = normalized.get("runtime_config")
    if not isinstance(runtime_config, Mapping):
        raise ProviderDefinitionError("Provider 运行配置无效")
    provider_key = str(runtime_config.get("provider_key") or "").strip().casefold()
    provider_version = str(runtime_config.get("provider_version") or "").strip()
    if not provider_key or not provider_version:
        raise ProviderDefinitionError("Provider identity 必须包含精确 key 和 version")
    trusted_registry = registry or default_provider_registry
    try:
        provider = trusted_registry.resolve(provider_key, provider_version)
    except CapabilityRegistryError as exc:
        raise ProviderDefinitionError(
            f"Provider 未注册或版本不匹配: {provider_key}@{provider_version}"
        ) from exc
    validator = getattr(provider, "validate_definition", None)
    if not callable(validator):
        raise ProviderDefinitionError(
            f"Provider 未声明定义校验器: {provider_key}@{provider_version}"
        )
    try:
        provider_config = validator(
            input_schema=copy.deepcopy(normalized.get("input_schema") or {}),
            output_schema=copy.deepcopy(normalized.get("output_schema") or {}),
            provider_config=copy.deepcopy(runtime_config.get("provider_config")),
            compatibility_mode=bool(compatibility_mode),
        )
    except Exception as exc:  # noqa: BLE001 - translate trusted Provider failures.
        message = str(exc).strip()[:500] or "Provider 配置无效"
        raise ProviderDefinitionError(message) from exc
    plain_config = _plain(provider_config, label="Provider 配置")
    if not isinstance(plain_config, dict):
        raise ProviderDefinitionError("Provider 校验器必须返回对象配置")
    normalized["runtime_config"] = {
        "provider_key": provider_key,
        "provider_version": provider_version,
        "provider_config": plain_config,
    }
    return normalized


__all__ = [
    "ProviderDefinitionError",
    "list_function_provider_manifests",
    "validate_function_definition",
]
