"""LLM 配置、路由、调用指标与基础评测 API。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LLMEvaluationRecord, LLMConfig, LLMInvocationTrace
from ..schemas import (
    LLMEvaluationIn,
    LLMEvaluationOut,
    LLMEvaluationSummaryOut,
    LLMConfigIn,
    LLMConfigOut,
    LLMRouteOut,
    LLMTraceOut,
    LLMUsageSummaryOut,
    Msg,
)
from ..services import connector_service, llm_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/llm-configs", tags=["llm-configs"])

Capability = Literal["chat", "embedding", "vision", "tool"]


def _out(c: LLMConfig) -> LLMConfigOut:
    """显式构造输出，确保任何路径都不会回传 api_key。"""
    return LLMConfigOut(
        id=c.id,
        name=c.name,
        provider=c.provider,
        base_url=c.base_url,
        api_key="",
        model=c.model,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        is_default=c.is_default,
        capabilities=(
            [str(item).strip().lower() for item in c.capabilities]
            if isinstance(c.capabilities, list)
            else ["chat", "tool"]
        ),
        enabled=bool(c.enabled),
        routing_priority=c.routing_priority,
        input_cost_per_million=c.input_cost_per_million,
        output_cost_per_million=c.output_cost_per_million,
        budget_limit=c.budget_limit,
        cost_currency=c.cost_currency or "USD",
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _trace_out(trace: LLMInvocationTrace) -> LLMTraceOut:
    return LLMTraceOut(
        id=trace.id,
        llm_config_id=trace.llm_config_id,
        provider=trace.provider,
        model=trace.model,
        capability=trace.capability,
        operation=trace.operation,
        status=trace.status,
        latency_ms=trace.latency_ms,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        total_tokens=trace.total_tokens,
        estimated_cost=trace.estimated_cost,
        currency=trace.currency,
        tool_count=trace.tool_count,
        correlation_id=trace.correlation_id or "",
        agent_id=trace.agent_id,
        conversation_id=trace.conversation_id,
        scenario_id=trace.scenario_id,
        user_id=trace.user_id,
        error=trace.error,
        created_at=trace.created_at,
    )


def _evaluation_out(record: LLMEvaluationRecord) -> LLMEvaluationOut:
    return LLMEvaluationOut(
        id=record.id,
        llm_config_id=record.llm_config_id,
        name=record.name,
        capability=record.capability,
        passed=record.passed,
        score=record.score,
        latency_ms=record.latency_ms,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        estimated_cost=record.estimated_cost,
        currency=record.currency,
        notes=record.notes,
        metrics=record.metrics or {},
        created_at=record.created_at,
    )


def _validate_default(payload: LLMConfigIn) -> None:
    if payload.is_default and (not payload.enabled or "chat" not in payload.capabilities):
        raise HTTPException(400, "默认模型必须启用 chat 能力")


def _range(days: int) -> tuple[datetime, datetime]:
    until = datetime.now(timezone.utc)
    return until - timedelta(days=days), until


@router.get("", response_model=list[LLMConfigOut])
def list_llm(db: Session = Depends(get_tenant_db)):
    stmt = (
        select(LLMConfig)
        .where(tenant_service.visible_clause(LLMConfig, db))
        .order_by(LLMConfig.routing_priority.asc(), LLMConfig.is_default.desc(), LLMConfig.name.asc())
    )
    return [_out(c) for c in db.execute(stmt).scalars().all()]


@router.get("/resolve", response_model=LLMRouteOut)
def resolve_llm(
    capability: Capability = Query(default="chat"),
    db: Session = Depends(get_tenant_db),
):
    """按能力、启用状态和路由优先级选择当前租户可使用的模型。"""
    candidates = llm_service.routable_configs(db, capability)
    if not candidates:
        raise HTTPException(404, f"没有可用的 {capability} 模型")
    return LLMRouteOut(
        capability=capability,
        selected=_out(candidates[0]),
        candidates=[_out(config) for config in candidates],
    )


@router.post("", response_model=LLMConfigOut)
def create_llm(payload: LLMConfigIn, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    _validate_default(payload)
    if payload.is_default:
        for c in db.execute(
            select(LLMConfig).where(LLMConfig.tenant_id == tenant_service.current_tenant_id(db))
        ).scalars().all():
            c.is_default = False
    c = LLMConfig(tenant_id=tenant_service.current_tenant_id(db), **payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.put("/{cfg_id}", response_model=LLMConfigOut)
def update_llm(cfg_id: str, payload: LLMConfigIn, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    _validate_default(payload)
    c = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    if payload.is_default:
        for other in db.execute(
            select(LLMConfig).where(LLMConfig.tenant_id == tenant_service.current_tenant_id(db))
        ).scalars().all():
            other.is_default = False
    values = payload.model_dump()
    if not values.get("api_key"):
        values["api_key"] = c.api_key
    for key, value in values.items():
        setattr(c, key, value)
    connector_service.invalidate_connector_bindings(db, "llm", c.id)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.get("/{cfg_id}/traces", response_model=list[LLMTraceOut])
def list_traces(
    cfg_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
):
    # 公共配置也可能被其他租户调用；运行统计只允许配置所有者读取。
    tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    traces = db.execute(
        select(LLMInvocationTrace)
        .where(LLMInvocationTrace.llm_config_id == cfg_id)
        .order_by(LLMInvocationTrace.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_trace_out(trace) for trace in traces]


@router.get("/{cfg_id}/usage-summary", response_model=LLMUsageSummaryOut)
def usage_summary(
    cfg_id: str,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_tenant_db),
):
    config = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    since, until = _range(days)
    traces = db.execute(
        select(LLMInvocationTrace).where(
            LLMInvocationTrace.llm_config_id == cfg_id,
            LLMInvocationTrace.created_at >= since,
            LLMInvocationTrace.created_at <= until,
        )
    ).scalars().all()
    by_capability: dict[str, dict[str, float | int]] = {}
    for trace in traces:
        group = by_capability.setdefault(
            trace.capability or "chat",
            {"invocation_count": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0},
        )
        group["invocation_count"] = int(group["invocation_count"]) + 1
        group["input_tokens"] = int(group["input_tokens"]) + int(trace.input_tokens or 0)
        group["output_tokens"] = int(group["output_tokens"]) + int(trace.output_tokens or 0)
        group["estimated_cost"] = round(float(group["estimated_cost"]) + float(trace.estimated_cost or 0), 8)
    total_cost = round(sum(float(trace.estimated_cost or 0) for trace in traces), 8)
    successful = [trace for trace in traces if trace.status == "succeeded"]
    return LLMUsageSummaryOut(
        llm_config_id=cfg_id,
        since=since,
        until=until,
        invocation_count=len(traces),
        succeeded_count=len(successful),
        failed_count=sum(1 for trace in traces if trace.status == "failed"),
        cancelled_count=sum(1 for trace in traces if trace.status == "cancelled"),
        input_tokens=sum(int(trace.input_tokens or 0) for trace in traces),
        output_tokens=sum(int(trace.output_tokens or 0) for trace in traces),
        total_tokens=sum(int(trace.total_tokens or 0) for trace in traces),
        estimated_cost=total_cost,
        budget_limit=float(config.budget_limit or 0),
        budget_remaining=(
            round(max(0.0, float(config.budget_limit) - total_cost), 8)
            if float(config.budget_limit or 0) > 0
            else None
        ),
        currency=config.cost_currency or "USD",
        average_latency_ms=round(
            sum(int(trace.latency_ms or 0) for trace in traces) / len(traces), 2
        ) if traces else 0.0,
        by_capability=by_capability,
    )


@router.get("/{cfg_id}/evaluations", response_model=list[LLMEvaluationOut])
def list_evaluations(
    cfg_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
):
    tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    records = db.execute(
        select(LLMEvaluationRecord)
        .where(LLMEvaluationRecord.llm_config_id == cfg_id)
        .order_by(LLMEvaluationRecord.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_evaluation_out(record) for record in records]


@router.post("/{cfg_id}/evaluations", response_model=LLMEvaluationOut)
def create_evaluation(
    cfg_id: str,
    payload: LLMEvaluationIn,
    db: Session = Depends(get_tenant_db),
):
    permission_service.require_tenant_permission(db, "manage")
    config = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    record = LLMEvaluationRecord(
        tenant_id=tenant_service.current_tenant_id(db),
        llm_config_id=config.id,
        name=payload.name,
        capability=payload.capability,
        passed=payload.passed,
        score=payload.score,
        latency_ms=payload.latency_ms,
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        estimated_cost=payload.estimated_cost,
        currency=config.cost_currency or "USD",
        notes=llm_service.sanitize_trace_text(payload.notes),
        metrics=llm_service.sanitize_metrics(payload.metrics),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return _evaluation_out(record)


@router.get("/{cfg_id}/evaluation-summary", response_model=LLMEvaluationSummaryOut)
def evaluation_summary(cfg_id: str, db: Session = Depends(get_tenant_db)):
    tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    records = db.execute(
        select(LLMEvaluationRecord).where(LLMEvaluationRecord.llm_config_id == cfg_id)
    ).scalars().all()
    return LLMEvaluationSummaryOut(
        llm_config_id=cfg_id,
        total=len(records),
        passed=sum(1 for record in records if record.passed),
        failed=sum(1 for record in records if not record.passed),
        average_score=round(sum(float(record.score or 0) for record in records) / len(records), 4)
        if records else 0.0,
        average_latency_ms=round(
            sum(int(record.latency_ms or 0) for record in records) / len(records), 2
        ) if records else 0.0,
        input_tokens=sum(int(record.input_tokens or 0) for record in records),
        output_tokens=sum(int(record.output_tokens or 0) for record in records),
        estimated_cost=round(sum(float(record.estimated_cost or 0) for record in records), 8),
        latest_at=max((record.created_at for record in records), default=None),
    )


@router.delete("/{cfg_id}", response_model=Msg)
def delete_llm(cfg_id: str, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    c = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    try:
        connector_service.assert_connector_not_bound(db, "llm", c.id)
    except connector_service.ConnectorBindingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{cfg_id}/test", response_model=Msg)
def test_llm(
    cfg_id: str,
    capability: Capability | None = Query(default=None),
    db: Session = Depends(get_tenant_db),
):
    # 测试调用会产生费用和 trace，因此只允许所有者操作。
    permission_service.require_tenant_permission(db, "manage")
    c = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    if not c.enabled:
        raise HTTPException(409, "模型配置当前已停用")
    ok, msg = llm_service.test_connection(c, db=db, capability=capability)
    return Msg(ok=ok, message=msg)
