"""Bounded preview and blocker evaluation for scenario purge."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    AgentTurnRun,
    Assertion,
    ArtifactTemplate,
    AssistantAttachment,
    AssistantAuditLog,
    AssistantCompilationJob,
    AssistantRouteDecision,
    AssistantThread,
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    Conversation,
    DataMappingRefreshJob,
    DataAsset,
    DataMapping,
    DataSource,
    DerivationEvidence,
    DerivationRun,
    FunctionRun,
    LLMInvocationTrace,
    ManagedUploadRun,
    Message,
    OntologyEntity,
    OntologyInstance,
    OntologyBranch,
    OntologyProposal,
    OntologyRelation,
    OntologyRelease,
    OntologyRollback,
    OntologySnapshot,
    ObjectDeletionJob,
    ReasoningTerm,
    RelationDataMapping,
    RelationInstance,
    RunInputBinding,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    ScenarioModelDraftResource,
    SemanticFieldMapping,
    SemanticMapping,
    SemanticRelationMapping,
    WorkflowRun,
)
from . import agent_deletion_service, scenario_purge_asset_service


@dataclass(frozen=True)
class ScenarioPurgePlan:
    scenario_id: str
    scenario_name: str
    status: str
    can_purge: bool
    blockers: tuple[str, ...]
    counts: dict[str, int]
    retained: dict[str, int]
    requires_audit_confirmation: bool



def _count_where(db: Session, model: Any, *conditions: Any) -> int:
    return int(
        db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
    )


def scenario_scoped_ids(model: Any, scenario: BusinessScenario) -> Any:
    """Return IDs whose scenario *and* tenant both match the purge target.

    A number of legacy tables keep ``scenario_id`` without a tenant column.
    Joining the authoritative scenario row gives those tables the same tenant
    fence as newer composite-FK tables, and also makes malformed/orphan rows
    ineligible for bulk deletion.
    """

    statement = (
        select(model.id)
        .select_from(model)
        .join(BusinessScenario, BusinessScenario.id == model.scenario_id)
        .where(
            model.scenario_id == scenario.id,
            BusinessScenario.id == scenario.id,
            BusinessScenario.tenant_id == scenario.tenant_id,
        )
    )
    tenant_column = getattr(model, "tenant_id", None)
    if tenant_column is not None:
        statement = statement.where(tenant_column == scenario.tenant_id)
    return statement


def _count_scenario(
    db: Session,
    model: Any,
    scenario: BusinessScenario,
    *conditions: Any,
) -> int:
    return _count_where(
        db,
        model,
        model.id.in_(scenario_scoped_ids(model, scenario)),
        *conditions,
    )


def scenario_tenant_mismatch_exists(
    db: Session,
    scenario: BusinessScenario,
) -> bool:
    """Detect dirty scoped rows before a purge can mutate any child state."""

    scoped_models = (
        Agent,
        ArtifactTemplate,
        Assertion,
        AssistantAuditLog,
        AssistantCompilationJob,
        AssistantRouteDecision,
        AssistantThread,
        CapabilityInvocation,
        ConnectorBinding,
        DataMappingRefreshJob,
        DataSource,
        DerivationRun,
        FunctionRun,
        LLMInvocationTrace,
        ObjectDeletionJob,
        OntologyBranch,
        OntologyProposal,
        OntologyRelease,
        OntologyRollback,
        OntologySnapshot,
        ReasoningTerm,
        RunInputBinding,
        ScenarioCapabilityPort,
        ScenarioDatasetBinding,
        ScenarioModelDraftResource,
        SemanticFieldMapping,
        SemanticMapping,
        SemanticRelationMapping,
    )
    for model in scoped_models:
        if db.scalar(
            select(model.id)
            .where(
                model.scenario_id == scenario.id,
                or_(
                    model.tenant_id.is_(None),
                    model.tenant_id != scenario.tenant_id,
                ),
            )
            .limit(1)
        ) is not None:
            return True

    # Attachments inherit scenario scope through their thread and therefore do
    # not expose scenario_id themselves.  A malformed tenant marker must not
    # be removed by the thread's cascade.
    return (
        db.scalar(
            select(AssistantAttachment.id)
            .join(AssistantThread, AssistantThread.id == AssistantAttachment.thread_id)
            .where(
                AssistantThread.scenario_id == scenario.id,
                AssistantThread.tenant_id == scenario.tenant_id,
                or_(
                    AssistantAttachment.tenant_id.is_(None),
                    AssistantAttachment.tenant_id != scenario.tenant_id,
                ),
            )
            .limit(1)
        )
        is not None
    )


@dataclass(frozen=True)
class _PurgePlanQueries:
    agent_ids: Any
    conversation_ids: Any
    turn_run_ids: Any
    assistant_thread_ids: Any
    assertion_ids: Any
    derivation_run_ids: Any
    action_log_ids: Any
    evidence_for_scenario: Any


def _plan_queries(scenario: BusinessScenario) -> _PurgePlanQueries:
    agent_ids = scenario_scoped_ids(Agent, scenario)
    conversation_ids = (
        select(Conversation.id)
        .select_from(Conversation)
        .join(Agent, Agent.id == Conversation.agent_id)
        .where(
            Conversation.agent_id.in_(agent_ids),
            Agent.scenario_id == scenario.id,
            Agent.tenant_id == scenario.tenant_id,
        )
    )
    turn_run_ids = select(AgentTurnRun.id).where(
        AgentTurnRun.agent_id.in_(agent_ids),
        AgentTurnRun.tenant_id == scenario.tenant_id,
    )
    assistant_thread_ids = scenario_scoped_ids(AssistantThread, scenario)
    assertion_ids = scenario_scoped_ids(Assertion, scenario)
    derivation_run_ids = scenario_scoped_ids(DerivationRun, scenario)
    action_log_ids = scenario_scoped_ids(ActionExecutionLog, scenario)
    evidence_for_scenario = or_(
        DerivationEvidence.derivation_run_id.in_(derivation_run_ids),
        DerivationEvidence.assertion_id.in_(assertion_ids),
        DerivationEvidence.evidence_assertion_id.in_(assertion_ids),
        DerivationEvidence.action_execution_log_id.in_(action_log_ids),
        DerivationEvidence.action_scenario_id == scenario.id,
    )
    evidence_for_scenario = (
        DerivationEvidence.tenant_id == scenario.tenant_id
    ) & evidence_for_scenario
    return _PurgePlanQueries(
        agent_ids=agent_ids,
        conversation_ids=conversation_ids,
        turn_run_ids=turn_run_ids,
        assistant_thread_ids=assistant_thread_ids,
        assertion_ids=assertion_ids,
        derivation_run_ids=derivation_run_ids,
        action_log_ids=action_log_ids,
        evidence_for_scenario=evidence_for_scenario,
    )


def _definition_counts(db: Session, scenario: BusinessScenario) -> dict[str, int]:
    return {
        "object_types": _count_scenario(db, OntologyEntity, scenario),
        "relation_types": _count_scenario(db, OntologyRelation, scenario),
        "object_instances": _count_scenario(db, OntologyInstance, scenario),
        "relation_instances": _count_scenario(db, RelationInstance, scenario),
        "mappings": _count_scenario(db, DataMapping, scenario)
        + _count_scenario(db, RelationDataMapping, scenario),
        "data_sources": _count_scenario(db, DataSource, scenario),
        "dataset_bindings": _count_scenario(
            db, ScenarioDatasetBinding, scenario
        ),
        "connector_bindings": _count_scenario(db, ConnectorBinding, scenario),
    }


def _agent_counts(
    db: Session,
    scenario: BusinessScenario,
    queries: _PurgePlanQueries,
) -> dict[str, int]:
    return {
        "agents": _count_scenario(db, Agent, scenario),
        "conversations": _count_where(
            db,
            Conversation,
            Conversation.agent_id.in_(queries.agent_ids),
        ),
        "messages": _count_where(
            db, Message, Message.conversation_id.in_(queries.conversation_ids)
        ),
        "turn_runs": _count_where(
            db, AgentTurnRun, AgentTurnRun.id.in_(queries.turn_run_ids)
        ),
        "managed_upload_runs": _count_where(
            db,
            ManagedUploadRun,
            ManagedUploadRun.owner_agent_id.in_(queries.agent_ids),
            ManagedUploadRun.tenant_id == scenario.tenant_id,
        ),
        "agent_assets": _count_where(
            db,
            DataAsset,
            DataAsset.owner_agent_id.in_(queries.agent_ids),
            DataAsset.tenant_id == scenario.tenant_id,
        ),
        "assistant_threads": _count_where(
            db,
            AssistantThread,
            AssistantThread.id.in_(queries.assistant_thread_ids),
            AssistantThread.tenant_id == scenario.tenant_id,
        ),
        "assistant_attachments": _count_where(
            db,
            AssistantAttachment,
            AssistantAttachment.thread_id.in_(queries.assistant_thread_ids),
            AssistantAttachment.tenant_id == scenario.tenant_id,
        ),
    }


def _audit_counts(
    db: Session,
    scenario: BusinessScenario,
    queries: _PurgePlanQueries,
) -> dict[str, int]:
    return {
        "assertions": _count_scenario(db, Assertion, scenario),
        "derivation_runs": _count_scenario(db, DerivationRun, scenario),
        "derivation_evidence": _count_where(
            db, DerivationEvidence, queries.evidence_for_scenario
        ),
        "capability_invocations": _count_scenario(
            db, CapabilityInvocation, scenario
        ),
        "action_logs": _count_scenario(db, ActionExecutionLog, scenario),
        "workflow_runs": _count_scenario(db, WorkflowRun, scenario),
        "releases": _count_scenario(db, OntologyRelease, scenario),
        "llm_traces": _count_where(
            db,
            LLMInvocationTrace,
            LLMInvocationTrace.id.in_(
                scenario_scoped_ids(LLMInvocationTrace, scenario)
            ),
        ),
    }


def _retained_dataset_counts(
    db: Session,
    scenario: BusinessScenario,
) -> dict[str, int]:
    dataset_ids = {
        str(value)
        for value in db.scalars(
            select(ScenarioDatasetBinding.dataset_id).where(
                ScenarioDatasetBinding.scenario_id == scenario.id,
                ScenarioDatasetBinding.tenant_id == scenario.tenant_id,
            )
        ).all()
    }
    shared_datasets = sum(
        bool(
            _count_where(
                db,
                ScenarioDatasetBinding,
                ScenarioDatasetBinding.dataset_id == dataset_id,
                ScenarioDatasetBinding.tenant_id == scenario.tenant_id,
                ScenarioDatasetBinding.scenario_id != scenario.id,
            )
        )
        for dataset_id in dataset_ids
    )
    return {
        "logical_datasets": len(dataset_ids),
        "shared_logical_datasets": shared_datasets,
    }


def _plan_blockers(
    db: Session,
    scenario: BusinessScenario,
    queries: _PurgePlanQueries,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if scenario_tenant_mismatch_exists(db, scenario):
        blockers.append("场景历史数据的租户归属不一致，已停止永久删除，请先修复数据")
    if scenario.status != "retired":
        blockers.append("请先退役场景，确认不再接受新的验证和运行请求")
    if _count_where(
        db,
        OntologyRelease,
        OntologyRelease.id.in_(scenario_scoped_ids(OntologyRelease, scenario)),
        OntologyRelease.status == "released",
    ):
        blockers.append("仍有预发布或生产 Release，请先在发布与接入中撤下")
    active_invocations = db.execute(
        select(CapabilityInvocation.status, CapabilityInvocation.result_document).where(
            CapabilityInvocation.id.in_(scenario_scoped_ids(CapabilityInvocation, scenario)),
            CapabilityInvocation.status.in_(("pending", "running", "awaiting_confirmation")),
        )
    ).all()
    now = datetime.now(timezone.utc)
    has_active_invocation = False
    for status, result_document in active_invocations:
        if status != "awaiting_confirmation":
            has_active_invocation = True
            break
        confirmation = (result_document or {}).get("confirmation", {})
        raw_expiry = confirmation.get("expires_at") if isinstance(confirmation, dict) else None
        try:
            expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # Missing/invalid expiry cannot prove that the confirmation is stale.
            has_active_invocation = True
            break
        if expiry > now:
            has_active_invocation = True
            break
    if has_active_invocation:
        blockers.append("仍有进行中的能力调用")
    if _count_where(
        db,
        WorkflowRun,
        WorkflowRun.id.in_(scenario_scoped_ids(WorkflowRun, scenario)),
        WorkflowRun.status.in_(
            ("queued", "running", "awaiting_approval", "retry_waiting")
        ),
    ):
        blockers.append("仍有进行中的工作流任务")
    if _count_where(
        db,
        AgentTurnRun,
        AgentTurnRun.id.in_(queries.turn_run_ids),
        AgentTurnRun.status.in_(agent_deletion_service.ACTIVE_TURN_STATUSES),
    ):
        blockers.append("仍有进行中的 Agent 对话任务")
    source_state = scenario_purge_asset_service.inspect_scenario_sources(db, scenario)
    blockers.extend(item for item in source_state.blockers if item not in blockers)
    return tuple(blockers)


def build_purge_plan(
    db: Session,
    scenario: BusinessScenario,
) -> ScenarioPurgePlan:
    """Build the bounded, value-free preview used by every purge entry point."""

    queries = _plan_queries(scenario)
    counts = {
        **_definition_counts(db, scenario),
        **_agent_counts(db, scenario, queries),
        **_audit_counts(db, scenario, queries),
    }
    retained = _retained_dataset_counts(db, scenario)
    blockers = _plan_blockers(db, scenario, queries)
    audit_keys = (
        "conversations",
        "messages",
        "capability_invocations",
        "action_logs",
        "workflow_runs",
        "releases",
        "llm_traces",
        "assertions",
        "derivation_runs",
        "derivation_evidence",
    )
    return ScenarioPurgePlan(
        scenario_id=str(scenario.id),
        scenario_name=scenario.name,
        status=scenario.status,
        can_purge=not blockers,
        blockers=blockers,
        counts=counts,
        retained=retained,
        requires_audit_confirmation=any(counts[key] for key in audit_keys),
    )
