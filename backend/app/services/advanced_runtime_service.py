"""P2 advanced ontology runtime primitives.

This module deliberately implements bounded, deterministic operators rather
than accepting Python, SQL, shell commands or remote handlers.  External data
and production connectors remain governed by the existing connector/release
layers; this runtime owns only portable asset descriptors, records and audit
evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    FunctionDefinition,
    OntologyAdvancedAsset,
    OntologyAdvancedRecord,
    OntologyAdvancedRun,
    OntologyModelFeedback,
)


class AdvancedRuntimeError(ValueError):
    """A bounded advanced asset/runtime request is invalid."""


ASSET_KINDS = {
    "geospatial", "timeseries", "media", "realtime", "ml_model", "simulation", "optimization",
}
ASSET_STATUSES = {"draft", "ready", "disabled"}
RUN_TYPES = {"predict", "simulate", "optimize", "aggregate"}
_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "apikey", "api_key", "authorization", "credential",
}
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain_json(value: Any, label: str, maximum: int = 64_000) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AdvancedRuntimeError(f"{label}必须是有效 JSON") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise AdvancedRuntimeError(f"{label}不能超过 {maximum} 字节")
    return copy.deepcopy(value)


def _reject_sensitive(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SENSITIVE_KEYS:
                raise AdvancedRuntimeError(f"{path}.{key} 不能保存凭据")
            _reject_sensitive(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive(nested, f"{path}[{index}]")


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    normalized = "" if value is None else str(value).strip()
    if required and not normalized:
        raise AdvancedRuntimeError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise AdvancedRuntimeError(f"{label}不能超过 {maximum} 个字符")
    return normalized


def normalize_asset(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AdvancedRuntimeError("高级资产定义必须是对象")
    name = _text(payload.get("name"), "资产名称", 200, required=True)
    kind = _text(payload.get("kind"), "资产类型", 30, required=True)
    if kind not in ASSET_KINDS:
        raise AdvancedRuntimeError("资产类型不受支持")
    status = _text(payload.get("status", "draft"), "资产状态", 20, required=True)
    if status not in ASSET_STATUSES:
        raise AdvancedRuntimeError("资产状态不受支持")
    schema = _plain_json(payload.get("schema", {}), "资产 Schema")
    config = _plain_json(payload.get("config", {}), "资产配置")
    if not isinstance(schema, dict) or not isinstance(config, dict):
        raise AdvancedRuntimeError("资产 Schema 和配置必须是对象")
    _reject_sensitive(config)
    if kind == "ml_model":
        _validate_model_config(config)
    elif kind == "simulation":
        _validate_simulation_config(config)
    elif kind == "optimization":
        _validate_optimization_config(config)
    return {
        "name": name,
        "kind": kind,
        "description": _text(payload.get("description", ""), "资产说明", 8_000),
        "schema": schema,
        "config": config,
        "status": status,
    }


def _validate_model_config(config: Mapping[str, Any]) -> None:
    weights = config.get("weights", {})
    if not isinstance(weights, Mapping) or len(weights) > 100:
        raise AdvancedRuntimeError("模型 weights 必须是最多 100 个字段的对象")
    for field, weight in weights.items():
        if not isinstance(field, str) or not _FIELD_RE.fullmatch(field):
            raise AdvancedRuntimeError("模型 weights 字段名无效")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise AdvancedRuntimeError("模型 weights 必须是数字")
    for key in ("bias",):
        if key in config and (isinstance(config[key], bool) or not isinstance(config[key], (int, float))):
            raise AdvancedRuntimeError(f"模型 {key} 必须是数字")


def _validate_simulation_config(config: Mapping[str, Any]) -> None:
    operations = config.get("operations", [])
    if not isinstance(operations, list) or len(operations) > 30:
        raise AdvancedRuntimeError("仿真 operations 必须是最多 30 步的数组")
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise AdvancedRuntimeError(f"仿真 operations[{index}] 必须是对象")
        if operation.get("op") not in {"add", "multiply", "set"}:
            raise AdvancedRuntimeError("仿真只支持 add、multiply、set")
        field = operation.get("field")
        if not isinstance(field, str) or not _FIELD_RE.fullmatch(field):
            raise AdvancedRuntimeError("仿真字段名无效")
        if operation.get("op") != "set" and (
            isinstance(operation.get("value"), bool) or not isinstance(operation.get("value"), (int, float))
        ):
            raise AdvancedRuntimeError("仿真 add/multiply 的 value 必须是数字")
    iterations = config.get("iterations", 1)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 100:
        raise AdvancedRuntimeError("仿真 iterations 必须在 1 到 100 之间")


def _validate_optimization_config(config: Mapping[str, Any]) -> None:
    objective = config.get("objective", "score")
    if not isinstance(objective, str) or not _FIELD_RE.fullmatch(objective):
        raise AdvancedRuntimeError("优化 objective 字段名无效")
    if config.get("direction", "max") not in {"max", "min"}:
        raise AdvancedRuntimeError("优化 direction 只能是 max 或 min")
    constraints = config.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 20:
        raise AdvancedRuntimeError("优化 constraints 必须是最多 20 条的数组")
    for constraint in constraints:
        if not isinstance(constraint, Mapping) or constraint.get("op") not in {">", ">=", "<", "<=", "==", "!="}:
            raise AdvancedRuntimeError("优化约束格式无效")
        if not isinstance(constraint.get("field"), str) or not _FIELD_RE.fullmatch(constraint["field"]):
            raise AdvancedRuntimeError("优化约束字段名无效")


def normalize_record(asset: OntologyAdvancedAsset, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AdvancedRuntimeError("资产记录必须是对象")
    event_time = payload.get("event_time")
    if isinstance(event_time, str):
        try:
            event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdvancedRuntimeError("event_time 不是有效 ISO 时间") from exc
    if event_time is not None and not isinstance(event_time, datetime):
        raise AdvancedRuntimeError("event_time 必须是 ISO 时间")
    if isinstance(event_time, datetime) and event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    geometry = _plain_json(payload.get("geometry", {}), "geometry", 16_000)
    record_payload = _plain_json(payload.get("payload", {}), "record payload", 64_000)
    if not isinstance(geometry, dict) or not isinstance(record_payload, dict):
        raise AdvancedRuntimeError("geometry 和 payload 必须是对象")
    if asset.kind == "geospatial" and geometry:
        _point_from_geometry(geometry)
    if asset.kind == "timeseries" and event_time is None:
        raise AdvancedRuntimeError("时序记录必须提供 event_time")
    if asset.kind == "realtime" and not payload.get("event_type"):
        raise AdvancedRuntimeError("实时记录必须提供 event_type")
    return {
        "event_time": event_time,
        "event_type": _text(payload.get("event_type", ""), "event_type", 120),
        "geometry": geometry,
        "payload": record_payload,
        "source_ref": _text(payload.get("source_ref", ""), "source_ref", 300),
    }


def _point_from_geometry(geometry: Mapping[str, Any]) -> tuple[float, float]:
    value: Mapping[str, Any] = geometry
    if geometry.get("type") == "Feature":
        nested = geometry.get("geometry")
        if not isinstance(nested, Mapping):
            raise AdvancedRuntimeError("GeoJSON Feature 缺少 geometry")
        value = nested
    if value.get("type") != "Point":
        raise AdvancedRuntimeError("当前地理资产只支持 GeoJSON Point")
    coords = value.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        raise AdvancedRuntimeError("GeoJSON Point 坐标无效")
    lon, lat = coords[0], coords[1]
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (lon, lat)):
        raise AdvancedRuntimeError("GeoJSON 坐标必须是数字")
    if not -180 <= float(lon) <= 180 or not -90 <= float(lat) <= 90:
        raise AdvancedRuntimeError("GeoJSON 坐标超出范围")
    return float(lon), float(lat)


def create_record(
    db: Session,
    asset: OntologyAdvancedAsset,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    scenario_id: str,
    content_type: str = "",
    storage_path: str = "",
    checksum: str = "",
) -> OntologyAdvancedRecord:
    normalized = normalize_record(asset, payload)
    last_sequence = db.execute(
        select(func.max(OntologyAdvancedRecord.sequence)).where(
            OntologyAdvancedRecord.asset_id == asset.id
        )
    ).scalar_one_or_none()
    record = OntologyAdvancedRecord(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        asset_id=asset.id,
        sequence=int(last_sequence or 0) + 1,
        content_type=_text(content_type, "content_type", 160),
        storage_path=_text(storage_path, "storage_path", 700),
        checksum=_text(checksum, "checksum", 64),
        **normalized,
    )
    db.add(record)
    return record


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def query_records(
    db: Session,
    asset: OntologyAdvancedAsset,
    *,
    after_sequence: int = 0,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    event_type: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int = 100,
) -> tuple[list[OntologyAdvancedRecord], int]:
    stmt = select(OntologyAdvancedRecord).where(
        OntologyAdvancedRecord.asset_id == asset.id,
        OntologyAdvancedRecord.sequence > max(0, after_sequence),
    )
    if from_time:
        stmt = stmt.where(OntologyAdvancedRecord.event_time >= _normalize_dt(from_time))
    if to_time:
        stmt = stmt.where(OntologyAdvancedRecord.event_time <= _normalize_dt(to_time))
    if event_type:
        stmt = stmt.where(OntologyAdvancedRecord.event_type == event_type)
    rows = db.execute(
        stmt.order_by(OntologyAdvancedRecord.sequence.asc()).limit(min(max(limit, 1), 500))
    ).scalars().all()
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        rows = [
            row for row in rows
            if row.geometry and min_lon <= _point_from_geometry(row.geometry)[0] <= max_lon
            and min_lat <= _point_from_geometry(row.geometry)[1] <= max_lat
        ]
    return rows, (rows[-1].sequence if rows else None)


def parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    try:
        values = [float(item.strip()) for item in value.split(",")]
    except ValueError as exc:
        raise AdvancedRuntimeError("bbox 必须是 minLon,minLat,maxLon,maxLat") from exc
    if len(values) != 4 or values[0] > values[2] or values[1] > values[3]:
        raise AdvancedRuntimeError("bbox 范围无效")
    if not -180 <= values[0] <= 180 or not -180 <= values[2] <= 180 or not -90 <= values[1] <= 90 or not -90 <= values[3] <= 90:
        raise AdvancedRuntimeError("bbox 坐标超出范围")
    return values[0], values[1], values[2], values[3]


def _required_input(schema: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    required = schema.get("required", []) if isinstance(schema, Mapping) else []
    missing = [str(field) for field in required if field not in params]
    if missing:
        raise AdvancedRuntimeError(f"缺少必填参数: {', '.join(missing)}")


def _coords(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return _point_from_geometry(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _point_from_geometry({"type": "Point", "coordinates": value})
    raise AdvancedRuntimeError("坐标必须是 GeoJSON Point 或 [lon, lat]")


def _execute_function(function: FunctionDefinition, params: Mapping[str, Any]) -> dict[str, Any]:
    _required_input(function.input_schema or {}, params)
    config = function.runtime_config or {}
    kind = function.runtime_kind or "contract"
    if kind == "contract":
        raise AdvancedRuntimeError("该函数仍是 contract，尚未配置受治理的内置运行类型")
    if kind == "weighted_score":
        weights = config.get("weights", {})
        score = float(config.get("bias", 0))
        for field, weight in weights.items():
            value = params.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AdvancedRuntimeError(f"参数 {field} 必须是数字")
            score += float(weight) * float(value)
        return {"score": score}
    if kind == "threshold":
        field, threshold = config["field"], config["threshold"]
        value = params.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdvancedRuntimeError(f"参数 {field} 必须是数字")
        operator = config.get("operator", ">=")
        result = {
            ">": value > threshold, ">=": value >= threshold, "<": value < threshold,
            "<=": value <= threshold, "==": value == threshold, "!=": value != threshold,
        }[operator]
        return {"matched": result, "value": value, "threshold": threshold}
    if kind == "geo_distance":
        origin, target = _coords(params.get("origin")), _coords(params.get("target"))
        lat1, lat2 = math.radians(origin[1]), math.radians(target[1])
        dlat, dlon = math.radians(target[1] - origin[1]), math.radians(target[0] - origin[0])
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance_km = 6371.0088 * 2 * math.asin(math.sqrt(min(1, haversine)))
        if config.get("unit", "km") == "m":
            return {"distance": distance_km * 1000, "unit": "m"}
        return {"distance": distance_km, "unit": "km"}
    if kind == "timeseries_aggregate":
        values = params.get("values", [])
        if not isinstance(values, list) or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise AdvancedRuntimeError("values 必须是数字数组")
        aggregation = config.get("aggregation", "avg")
        if aggregation == "count":
            result = len(values)
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
    raise AdvancedRuntimeError("函数运行类型不受支持")


def _constraint_matches(candidate: Mapping[str, Any], constraint: Mapping[str, Any]) -> bool:
    value, target, operator = candidate.get(constraint.get("field")), constraint.get("value"), constraint.get("op")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return {
        ">": value > target, ">=": value >= target, "<": value < target,
        "<=": value <= target, "==": value == target, "!=": value != target,
    }[operator]


def _execute_asset(asset: OntologyAdvancedAsset, params: Mapping[str, Any], run_type: str) -> dict[str, Any]:
    config = asset.config or {}
    expected_run_type = {
        "ml_model": "predict",
        "simulation": "simulate",
        "optimization": "optimize",
        "timeseries": "aggregate",
    }.get(asset.kind)
    if expected_run_type and run_type != expected_run_type:
        raise AdvancedRuntimeError(f"{asset.kind} 资产只能使用 {expected_run_type} 运行类型")
    if asset.kind == "ml_model":
        features = params.get("features", params)
        if not isinstance(features, Mapping):
            raise AdvancedRuntimeError("predict.features 必须是对象")
        score = float(config.get("bias", 0))
        used: list[str] = []
        for field, weight in (config.get("weights", {}) or {}).items():
            value = features.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AdvancedRuntimeError(f"模型特征 {field} 必须是数字")
            score += float(weight) * float(value)
            used.append(field)
        return {"prediction": score, "features_used": used, "model_version": asset.version}
    if asset.kind == "simulation":
        state = copy.deepcopy(params.get("state", {}))
        if not isinstance(state, dict):
            raise AdvancedRuntimeError("simulate.state 必须是对象")
        iterations = int(config.get("iterations", 1))
        for _ in range(iterations):
            for operation in config.get("operations", []):
                field, value = operation["field"], operation["value"]
                if operation["op"] == "set":
                    state[field] = copy.deepcopy(value)
                else:
                    current = state.get(field, 0)
                    if isinstance(current, bool) or not isinstance(current, (int, float)):
                        raise AdvancedRuntimeError(f"仿真字段 {field} 必须是数字")
                    state[field] = current + value if operation["op"] == "add" else current * value
        return {"state": state, "iterations": iterations, "operations": len(config.get("operations", []))}
    if asset.kind == "optimization":
        candidates = params.get("candidates", [])
        if not isinstance(candidates, list) or not candidates or len(candidates) > 1_000 or any(not isinstance(item, Mapping) for item in candidates):
            raise AdvancedRuntimeError("optimize.candidates 必须是 1 到 1000 个对象")
        valid = [item for item in candidates if all(_constraint_matches(item, c) for c in config.get("constraints", []))]
        objective, direction = config.get("objective", "score"), config.get("direction", "max")
        if not valid:
            return {"selected": None, "feasible_count": 0}
        selected = sorted(valid, key=lambda item: item.get(objective, 0), reverse=direction == "max")[0]
        return {"selected": copy.deepcopy(selected), "objective": objective, "direction": direction, "feasible_count": len(valid)}
    if asset.kind == "timeseries":
        values = params.get("values", [])
        if not isinstance(values, list) or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise AdvancedRuntimeError("aggregate.values 必须是数字数组")
        aggregation = params.get("aggregation", config.get("aggregation", "avg"))
        if aggregation not in {"sum", "avg", "min", "max", "count"}:
            raise AdvancedRuntimeError("聚合方式不受支持")
        value = len(values) if aggregation == "count" else 0 if not values else {
            "sum": sum(values), "avg": sum(values) / len(values), "min": min(values), "max": max(values)
        }[aggregation]
        return {"aggregation": aggregation, "value": value, "count": len(values)}
    raise AdvancedRuntimeError("当前资产不支持该运行类型")


def create_asset_run(
    db: Session,
    asset: OntologyAdvancedAsset,
    params: Mapping[str, Any],
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str | None,
    run_type: str,
    idempotency_key: str | None = None,
) -> OntologyAdvancedRun:
    if run_type not in RUN_TYPES:
        raise AdvancedRuntimeError("运行类型不受支持")
    if asset.status == "disabled":
        raise AdvancedRuntimeError("资产已停用")
    scope = f"asset:{asset.id}:v{asset.version}:{run_type}"
    if idempotency_key:
        existing = db.execute(
            select(OntologyAdvancedRun).where(
                OntologyAdvancedRun.tenant_id == tenant_id,
                OntologyAdvancedRun.idempotency_scope == scope,
                OntologyAdvancedRun.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing:
            return existing
    started = _now()
    run = OntologyAdvancedRun(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        asset_id=asset.id,
        run_type=run_type,
        status="running",
        input_payload=_plain_json(params, "运行参数"),
        idempotency_scope=scope,
        idempotency_key=idempotency_key,
        started_at=started,
        created_by_user_id=user_id,
    )
    db.add(run)
    try:
        run.output_payload = _execute_asset(asset, params, run_type)
        run.status = "succeeded"
    except AdvancedRuntimeError as exc:
        run.status = "failed"
        run.error = str(exc)
    run.completed_at = _now()
    return run


def create_function_run(
    db: Session,
    function: FunctionDefinition,
    params: Mapping[str, Any],
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str | None,
    idempotency_key: str | None = None,
) -> OntologyAdvancedRun:
    scope = f"function:{function.id}:{function.updated_at.isoformat()}"
    if idempotency_key:
        existing = db.execute(
            select(OntologyAdvancedRun).where(
                OntologyAdvancedRun.tenant_id == tenant_id,
                OntologyAdvancedRun.idempotency_scope == scope,
                OntologyAdvancedRun.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing:
            return existing
    run = OntologyAdvancedRun(
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
        run.output_payload = _execute_function(function, params)
        run.status = "succeeded"
    except AdvancedRuntimeError as exc:
        run.status = "failed"
        run.error = str(exc)
    run.completed_at = _now()
    return run


def create_feedback(
    db: Session,
    asset: OntologyAdvancedAsset,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str | None,
) -> OntologyModelFeedback:
    if asset.kind != "ml_model":
        raise AdvancedRuntimeError("只有 ml_model 资产支持反馈闭环")
    run_id = payload.get("run_id")
    if run_id:
        run = db.execute(
            select(OntologyAdvancedRun).where(
                OntologyAdvancedRun.id == run_id,
                OntologyAdvancedRun.asset_id == asset.id,
            )
        ).scalars().first()
        if not run:
            raise AdvancedRuntimeError("反馈引用的运行记录不存在")
    score = payload.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1):
        raise AdvancedRuntimeError("反馈 score 必须在 0 到 1 之间")
    feedback = OntologyModelFeedback(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        asset_id=asset.id,
        run_id=run_id,
        label=_text(payload.get("label", ""), "反馈标签", 160),
        expected_output=_plain_json(payload.get("expected_output", {}), "expected_output"),
        actual_output=_plain_json(payload.get("actual_output", {}), "actual_output"),
        score=float(score) if score is not None else None,
        notes=_text(payload.get("notes", ""), "反馈说明", 4_000),
        created_by_user_id=user_id,
    )
    db.add(feedback)
    return feedback


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
