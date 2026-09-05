"""Statically trusted evaluators for legacy declarative function kinds.

The mapping in this module is code-owned and closed. Function definitions may
select one of these exact keys and provide value-only configuration, but they
cannot name Python code, import paths, commands, or external endpoints.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol


class BuiltinFunctionEvaluationError(ValueError):
    """A declarative function cannot be evaluated by its trusted implementation."""


class DeclarativeFunction(Protocol):
    input_schema: Mapping[str, Any] | None
    runtime_config: Mapping[str, Any] | None
    runtime_kind: str | None


Evaluator = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


def _required_input(schema: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    required = schema.get("required", [])
    missing = [str(field) for field in required if field not in params]
    if missing:
        raise BuiltinFunctionEvaluationError(f"缺少必填参数: {', '.join(missing)}")


def _numeric(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuiltinFunctionEvaluationError(f"参数 {field} 必须是数字")
    return value


def _weighted_score(
    config: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    weights = config.get("weights", {})
    score = float(config.get("bias", 0))
    for field, weight in weights.items():
        score += float(weight) * float(_numeric(params.get(field, 0), field=field))
    return {"score": score}


def _threshold(
    config: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    field = str(config["field"])
    threshold = config["threshold"]
    value = _numeric(params.get(field), field=field)
    operator = config.get("operator", ">=")
    matched = {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
        "!=": value != threshold,
    }[operator]
    return {"matched": matched, "value": value, "threshold": threshold}


def _point_from_geometry(geometry: Mapping[str, Any]) -> tuple[float, float]:
    value: Mapping[str, Any] = geometry
    if geometry.get("type") == "Feature":
        nested = geometry.get("geometry")
        if not isinstance(nested, Mapping):
            raise BuiltinFunctionEvaluationError("GeoJSON Feature 缺少 geometry")
        value = nested
    if value.get("type") != "Point":
        raise BuiltinFunctionEvaluationError("坐标只支持 GeoJSON Point")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise BuiltinFunctionEvaluationError("GeoJSON Point 坐标无效")
    longitude, latitude = coordinates[0], coordinates[1]
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in (longitude, latitude)
    ):
        raise BuiltinFunctionEvaluationError("GeoJSON 坐标必须是数字")
    if not -180 <= float(longitude) <= 180 or not -90 <= float(latitude) <= 90:
        raise BuiltinFunctionEvaluationError("GeoJSON 坐标超出范围")
    return float(longitude), float(latitude)


def _coordinates(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return _point_from_geometry(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _point_from_geometry({"type": "Point", "coordinates": value})
    raise BuiltinFunctionEvaluationError("坐标必须是 GeoJSON Point 或 [lon, lat]")


def _geo_distance(
    config: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    origin = _coordinates(params.get("origin"))
    target = _coordinates(params.get("target"))
    latitude_1, latitude_2 = math.radians(origin[1]), math.radians(target[1])
    delta_latitude = math.radians(target[1] - origin[1])
    delta_longitude = math.radians(target[0] - origin[0])
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    distance_km = 6371.0088 * 2 * math.asin(math.sqrt(min(1, haversine)))
    if config.get("unit", "km") == "m":
        return {"distance": distance_km * 1000, "unit": "m"}
    return {"distance": distance_km, "unit": "km"}


def _timeseries_aggregate(
    config: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    values = params.get("values", [])
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise BuiltinFunctionEvaluationError("values 必须是数字数组")
    aggregation = config.get("aggregation", "avg")
    if aggregation == "count":
        result: int | float = len(values)
    elif not values:
        result = 0
    elif aggregation == "sum":
        result = sum(values)
    elif aggregation == "min":
        result = min(values)
    elif aggregation == "max":
        result = max(values)
    else:
        result = sum(values) / len(values)
    return {"aggregation": aggregation, "value": result, "count": len(values)}


_EVALUATORS: Mapping[str, Evaluator] = MappingProxyType(
    {
        "weighted_score": _weighted_score,
        "threshold": _threshold,
        "geo_distance": _geo_distance,
        "timeseries_aggregate": _timeseries_aggregate,
    }
)


def evaluate_function(
    function: DeclarativeFunction,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one exact allowlisted function kind with value-only inputs."""

    if not isinstance(params, Mapping):
        raise BuiltinFunctionEvaluationError("函数参数必须是对象")
    schema = function.input_schema or {}
    _required_input(schema, params)
    kind = str(function.runtime_kind or "contract")
    if kind == "contract":
        raise BuiltinFunctionEvaluationError(
            "该函数仍是 contract，尚未配置受治理的内置运行类型"
        )
    evaluator = _EVALUATORS.get(kind)
    if evaluator is None:
        raise BuiltinFunctionEvaluationError("函数运行类型不受支持")
    return evaluator(function.runtime_config or {}, params)


__all__ = [
    "BuiltinFunctionEvaluationError",
    "DeclarativeFunction",
    "evaluate_function",
]
