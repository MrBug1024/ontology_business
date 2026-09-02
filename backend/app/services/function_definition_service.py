"""Validation for governed, declarative function definitions.

Functions are typed contracts plus an optional closed-list built-in runtime.
Keeping the normalizer here lets CRUD, release snapshots, portable packages and
the execution endpoint enforce the same boundary without adding a code/runtime
escape hatch.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any


class FunctionDefinitionError(ValueError):
    """A function declaration is not a safe typed contract."""


VISIBILITIES = {"scenario", "tenant"}
_JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "oneOf",
    "anyOf",
    "allOf",
    "nullable",
    "format",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}
_FORBIDDEN_SCHEMA_KEYS = {
    "code",
    "script",
    "command",
    "handler",
    "implementation",
    "executor",
    "executor_config",
    "runtime",
    "url",
    "mcp_id",
    "skill_id",
}
_MAX_SCHEMA_BYTES = 16_000
_MAX_SCHEMA_DEPTH = 12
_MAX_PROPERTIES = 100
_MAX_TAGS = 20
RUNTIME_KINDS = {
    "contract",
    "weighted_score",
    "threshold",
    "geo_distance",
    "timeseries_aggregate",
    "provider",
}
_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROVIDER_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,79}$")
_PROVIDER_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|credential|token|authorization|cookie|dsn|"
    r"database[_-]?url|api[_-]?key|connection[_-]?string|sql|table[_-]?name|"
    r"column[_-]?name)",
    flags=re.IGNORECASE,
)
_PROVIDER_FORBIDDEN_VALUE_RE = re.compile(
    r"(?:\b(?:select|insert|update|delete|drop|alter|create|grant|revoke)\b\s+"
    r"|(?:postgres(?:ql)?|mysql|mariadb|mssql|oracle|sqlite|redis|s3|file|minio)://"
    r"|\b(?:password|passwd|secret|credential|token|authorization|api[_-]?key)\s*[:=]"
    r"|(?:^|[;\s])(?:host|server|database|user(?:name)?|port)\s*=)",
    flags=re.IGNORECASE,
)
_PROVIDER_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/[^/]+/)")


def _text(value: Any, label: str, *, maximum: int, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FunctionDefinitionError(f"{label}必须是字符串")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise FunctionDefinitionError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise FunctionDefinitionError(f"{label}长度不能超过 {maximum}")
    return normalized


def _plain_json(value: Any, *, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FunctionDefinitionError(f"{label}必须是有效 JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise FunctionDefinitionError(f"{label}不能超过 {_MAX_SCHEMA_BYTES} 字节")
    return copy.deepcopy(value)


def _validate_schema_node(value: Any, *, path: str, depth: int, require_type: bool) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        raise FunctionDefinitionError(f"{path}嵌套层级过深")
    if not isinstance(value, Mapping):
        raise FunctionDefinitionError(f"{path}必须是 JSON Schema 对象")
    for raw_key in value:
        key = str(raw_key)
        if key.startswith("$"):
            raise FunctionDefinitionError(f"{path}不允许 $ref 或动态引用")
        if key in _FORBIDDEN_SCHEMA_KEYS:
            raise FunctionDefinitionError(f"{path}不允许可执行字段 {key}")
        if key not in _SCHEMA_KEYS:
            raise FunctionDefinitionError(f"{path}包含不支持的 Schema 字段 {key}")

    schema_type = value.get("type")
    if require_type and not isinstance(schema_type, str):
        raise FunctionDefinitionError(f"{path}.type 必须声明类型")
    if schema_type is not None:
        if not isinstance(schema_type, str) or schema_type not in _JSON_TYPES:
            raise FunctionDefinitionError(f"{path}.type 不支持")

    properties = value.get("properties")
    if properties is not None:
        if schema_type != "object" or not isinstance(properties, Mapping):
            raise FunctionDefinitionError(f"{path}.properties 仅可用于 object 类型")
        if len(properties) > _MAX_PROPERTIES:
            raise FunctionDefinitionError(f"{path}.properties 过多")
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str) or not property_name:
                raise FunctionDefinitionError(f"{path}.properties 的字段名无效")
            # A business payload may legitimately contain a field called
            # ``code``.  Only the descriptor *schema keys* above are forbidden.
            _validate_schema_node(
                property_schema,
                path=f"{path}.properties.{property_name}",
                depth=depth + 1,
                require_type=False,
            )
    required = value.get("required")
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
            raise FunctionDefinitionError(f"{path}.required 必须是字段名数组")
        if len(set(required)) != len(required):
            raise FunctionDefinitionError(f"{path}.required 不能重复")
        if properties is not None and any(item not in properties for item in required):
            raise FunctionDefinitionError(f"{path}.required 引用了未声明字段")
    if "additionalProperties" in value:
        additional_properties = value["additionalProperties"]
        if isinstance(additional_properties, Mapping):
            if schema_type != "object":
                raise FunctionDefinitionError(
                    f"{path}.additionalProperties 仅可用于 object 类型"
                )
            _validate_schema_node(
                additional_properties,
                path=f"{path}.additionalProperties",
                depth=depth + 1,
                require_type=False,
            )
        elif not isinstance(additional_properties, bool):
            raise FunctionDefinitionError(
                f"{path}.additionalProperties 必须是布尔值或 JSON Schema"
            )
    if "items" in value:
        if schema_type != "array":
            raise FunctionDefinitionError(f"{path}.items 仅可用于 array 类型")
        _validate_schema_node(value["items"], path=f"{path}.items", depth=depth + 1, require_type=False)
    for composite in ("oneOf", "anyOf", "allOf"):
        if composite not in value:
            continue
        variants = value[composite]
        if not isinstance(variants, list) or not variants:
            raise FunctionDefinitionError(f"{path}.{composite} 必须是非空数组")
        for index, variant in enumerate(variants):
            _validate_schema_node(
                variant,
                path=f"{path}.{composite}[{index}]",
                depth=depth + 1,
                require_type=False,
            )
    for key in ("title", "description", "format", "pattern"):
        if key in value and not isinstance(value[key], str):
            raise FunctionDefinitionError(f"{path}.{key} 必须是字符串")
    for key in ("nullable", "uniqueItems"):
        if key in value and not isinstance(value[key], bool):
            raise FunctionDefinitionError(f"{path}.{key} 必须是布尔值")
    for key in ("enum",):
        if key in value and not isinstance(value[key], list):
            raise FunctionDefinitionError(f"{path}.{key} 必须是数组")
    for key in (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
    ):
        if key in value and (isinstance(value[key], bool) or not isinstance(value[key], (int, float))):
            raise FunctionDefinitionError(f"{path}.{key} 必须是数字")


def normalize_schema(value: Any, *, label: str) -> dict[str, Any]:
    """Validate a bounded local JSON-Schema contract without executable hooks."""
    plain = _plain_json(value, label=label)
    if not isinstance(plain, dict):
        raise FunctionDefinitionError(f"{label}必须是对象类型 JSON Schema")
    _validate_schema_node(plain, path=label, depth=0, require_type=True)
    if plain.get("type") != "object":
        raise FunctionDefinitionError(f"{label}顶层必须是 object 类型")
    return plain


def normalize_definition(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Return the sole safe persisted shape of a function declaration/runtime."""
    if not isinstance(value, Mapping):
        raise FunctionDefinitionError("函数定义必须是对象")
    allowed = {
        "name", "description", "input_schema", "output_schema", "tags", "visibility",
        "runtime_kind", "runtime_config",
    }
    extra = sorted(str(key) for key in value if str(key) not in allowed)
    if extra:
        raise FunctionDefinitionError(f"函数定义不支持字段: {', '.join(extra)}")
    raw_tags = value.get("tags", [])
    if not isinstance(raw_tags, list):
        raise FunctionDefinitionError("函数标签必须是数组")
    if len(raw_tags) > _MAX_TAGS:
        raise FunctionDefinitionError(f"函数标签不能超过 {_MAX_TAGS} 个")
    tags: list[str] = []
    for raw_tag in raw_tags:
        tag = _text(raw_tag, "函数标签", maximum=80, allow_empty=False)
        if tag not in tags:
            tags.append(tag)
    visibility = _text(value.get("visibility", "scenario"), "函数可见性", maximum=20, allow_empty=False)
    if visibility not in VISIBILITIES:
        raise FunctionDefinitionError("函数可见性必须为 scenario 或 tenant")
    runtime_kind = _text(
        value.get("runtime_kind", "contract"), "函数运行类型", maximum=40, allow_empty=False
    )
    if runtime_kind not in RUNTIME_KINDS:
        raise FunctionDefinitionError("函数运行类型不受支持")
    runtime_config = _plain_json(value.get("runtime_config", {}), label="函数运行配置")
    if not isinstance(runtime_config, dict):
        raise FunctionDefinitionError("函数运行配置必须是对象")
    if runtime_kind == "contract" and runtime_config:
        raise FunctionDefinitionError("contract 函数不能包含运行配置")
    if runtime_kind == "weighted_score":
        weights = runtime_config.get("weights")
        if not isinstance(weights, dict) or not weights or any(
            not isinstance(key, str) or not key.strip() or isinstance(value, bool)
            or not isinstance(value, (int, float)) for key, value in weights.items()
        ):
            raise FunctionDefinitionError("weighted_score 需要非空数字 weights")
        bias = runtime_config.get("bias", 0)
        if isinstance(bias, bool) or not isinstance(bias, (int, float)):
            raise FunctionDefinitionError("weighted_score.bias 必须是数字")
    elif runtime_kind == "threshold":
        if not isinstance(runtime_config.get("field"), str) or not runtime_config["field"].strip():
            raise FunctionDefinitionError("threshold.field 必须是非空字符串")
        if isinstance(runtime_config.get("threshold"), bool) or not isinstance(
            runtime_config.get("threshold"), (int, float)
        ):
            raise FunctionDefinitionError("threshold.threshold 必须是数字")
        if runtime_config.get("operator", ">=") not in {">", ">=", "<", "<=", "==", "!="}:
            raise FunctionDefinitionError("threshold.operator 不受支持")
    elif runtime_kind == "geo_distance":
        if runtime_config.get("unit", "km") not in {"km", "m"}:
            raise FunctionDefinitionError("geo_distance.unit 只能是 km 或 m")
    elif runtime_kind == "timeseries_aggregate":
        if runtime_config.get("aggregation", "avg") not in {"sum", "avg", "min", "max", "count"}:
            raise FunctionDefinitionError("timeseries_aggregate.aggregation 不受支持")
        if runtime_config.get("value_field", "value") and not isinstance(
            runtime_config.get("value_field", "value"), str
        ):
            raise FunctionDefinitionError("timeseries_aggregate.value_field 必须是字符串")
    elif runtime_kind == "provider":
        if set(runtime_config) != {
            "provider_key",
            "provider_version",
            "provider_config",
        }:
            raise FunctionDefinitionError(
                "provider 运行配置必须且只能包含 provider_key、provider_version 和 provider_config"
            )
        provider_key = str(runtime_config.get("provider_key") or "").strip().casefold()
        provider_version = str(runtime_config.get("provider_version") or "").strip()
        provider_config = runtime_config.get("provider_config")
        if not _PROVIDER_KEY_RE.fullmatch(provider_key):
            raise FunctionDefinitionError("provider_key 必须是可移植的受信 Provider 标识")
        if not _PROVIDER_VERSION_RE.fullmatch(provider_version):
            raise FunctionDefinitionError("provider_version 格式无效")
        if not isinstance(provider_config, dict):
            raise FunctionDefinitionError("provider_config 必须是对象")

        def reject_sensitive_config(document: Any) -> None:
            if isinstance(document, Mapping):
                for key, nested in document.items():
                    if _PROVIDER_SECRET_KEY_RE.search(str(key)):
                        raise FunctionDefinitionError(
                            "provider_config 不能包含凭据、SQL 或物理表列配置"
                        )
                    reject_sensitive_config(nested)
            elif isinstance(document, list):
                for nested in document:
                    reject_sensitive_config(nested)
            elif isinstance(document, str) and (
                _PROVIDER_FORBIDDEN_VALUE_RE.search(document)
                or _PROVIDER_ABSOLUTE_PATH_RE.search(document.strip())
            ):
                raise FunctionDefinitionError(
                    "provider_config 不能包含凭据、SQL、连接串或物理存储路径"
                )

        reject_sensitive_config(provider_config)
        runtime_config = {
            "provider_key": provider_key,
            "provider_version": provider_version,
            "provider_config": provider_config,
        }
    return {
        "name": _text(value.get("name"), "函数名称", maximum=200, allow_empty=False),
        "description": _text(value.get("description", ""), "函数说明", maximum=8_000),
        "input_schema": normalize_schema(value.get("input_schema"), label="输入 Schema"),
        "output_schema": normalize_schema(value.get("output_schema"), label="输出 Schema"),
        "tags": tags,
        "visibility": visibility,
        "runtime_kind": runtime_kind,
        "runtime_config": runtime_config,
    }
