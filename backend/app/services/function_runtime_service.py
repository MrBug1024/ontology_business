"""Bounded, auditable runtime for governed ``FunctionDefinition`` records.

Only the closed-list, data-only runtimes validated by
``function_definition_service`` are executable here.  The service never accepts
code, commands, URLs, connector credentials, or arbitrary handlers.
"""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FunctionDefinition, FunctionRun


class FunctionRuntimeError(ValueError):
    """A function invocation cannot be evaluated by the governed runtime."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain_json(value: Any, label: str, maximum: int = 64_000) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FunctionRuntimeError(f"{label}必须是有效 JSON") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise FunctionRuntimeError(f"{label}不能超过 {maximum} 字节")
    return copy.deepcopy(value)


def _required_input(schema: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    required = schema.get("required", []) if isinstance(schema, Mapping) else []
    missing = [str(field) for field in required if field not in params]
    if missing:
        raise FunctionRuntimeError(f"缺少必填参数: {', '.join(missing)}")


def _point_from_geometry(geometry: Mapping[str, Any]) -> tuple[float, float]:
    value: Mapping[str, Any] = geometry
    if geometry.get("type") == "Feature":
        nested = geometry.get("geometry")
        if not isinstance(nested, Mapping):
            raise FunctionRuntimeError("GeoJSON Feature 缺少 geometry")
        value = nested
    if value.get("type") != "Point":
        raise FunctionRuntimeError("坐标只支持 GeoJSON Point")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise FunctionRuntimeError("GeoJSON Point 坐标无效")
    longitude, latitude = coordinates[0], coordinates[1]
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in (longitude, latitude)
    ):
        raise FunctionRuntimeError("GeoJSON 坐标必须是数字")
    if not -180 <= float(longitude) <= 180 or not -90 <= float(latitude) <= 90:
        raise FunctionRuntimeError("GeoJSON 坐标超出范围")
    return float(longitude), float(latitude)


def _coordinates(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return _point_from_geometry(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _point_from_geometry({"type": "Point", "coordinates": value})
    raise FunctionRuntimeError("坐标必须是 GeoJSON Point 或 [lon, lat]")


def execute_function(function: FunctionDefinition, params: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one allowlisted built-in runtime without external side effects."""
    if not isinstance(params, Mapping):
        raise FunctionRuntimeError("函数参数必须是对象")
    _required_input(function.input_schema or {}, params)
    config = function.runtime_config or {}
    kind = function.runtime_kind or "contract"
    if kind == "contract":
        raise FunctionRuntimeError("该函数仍是 contract，尚未配置受治理的内置运行类型")
    if kind == "weighted_score":
        weights = config.get("weights", {})
        score = float(config.get("bias", 0))
        for field, weight in weights.items():
            value = params.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FunctionRuntimeError(f"参数 {field} 必须是数字")
            score += float(weight) * float(value)
        return {"score": score}
    if kind == "threshold":
        field, threshold = config["field"], config["threshold"]
        value = params.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FunctionRuntimeError(f"参数 {field} 必须是数字")
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
    if kind == "geo_distance":
        origin, target = _coordinates(params.get("origin")), _coordinates(params.get("target"))
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
    if kind == "timeseries_aggregate":
        values = params.get("values", [])
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in values
        ):
            raise FunctionRuntimeError("values 必须是数字数组")
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
    raise FunctionRuntimeError("函数运行类型不受支持")


def create_function_run(
    db: Session,
    function: FunctionDefinition,
    params: Mapping[str, Any],
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str | None,
    idempotency_key: str | None = None,
) -> FunctionRun:
    """Persist one invocation and replay an existing idempotent result."""
    scope = f"function:{function.id}:{function.updated_at.isoformat()}"
    if idempotency_key:
        existing = db.execute(
            select(FunctionRun).where(
                FunctionRun.tenant_id == tenant_id,
                FunctionRun.idempotency_scope == scope,
                FunctionRun.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing:
            return existing
    run = FunctionRun(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        function_id=function.id,
        run_type="function",
        status="running",
        input_payload=_plain_json(params, "运行参数"),
        idempotency_scope=scope,
        idempotency_key=idempotency_key,
        started_at=_now(),
        created_by_user_id=user_id,
    )
    db.add(run)
    try:
        run.output_payload = execute_function(function, params)
        run.status = "succeeded"
    except FunctionRuntimeError as exc:
        run.status = "failed"
        run.error = str(exc)
    run.completed_at = _now()
    return run
