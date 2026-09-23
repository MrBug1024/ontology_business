"""Application service for permanently purging a retired scenario.

The service owns locking and ordered database cleanup. It does not commit or
roll back; the protocol boundary owns that transaction and drains durable
storage deletion jobs only after commit succeeds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    Assertion,
    AssistantAttachment,
    AssistantAuditLog,
    AssistantCompilationJob,
    AssistantRequestRun,
    AssistantRouteDecision,
    AssistantThread,
    BusinessScenario,
    CapabilityInvocation,
    DataSource,
    DerivationEvidence,
    DerivationRun,
    DerivationRunInput,
    LLMInvocationTrace,
    OntologyProposal,
    OntologyRelease,
    OntologyReview,
    OntologyRollback,
    ReasoningTerm,
    RunInputBinding,
    ScenarioDatasetBinding,
    SemanticFieldMapping,
    SemanticMapping,
    SemanticRelationMapping,
)
from ..distillation_access_models import DistillationSystemAccess
from ..distillation_attachment_models import DistillationAttachment, DistillationTurnAttachment
from ..distillation_conversation_models import DistillationConversationTurn
from ..distillation_models import (
    DistillationProject,
    DistillationPublication,
    DistillationScenarioState,
)
from ..external_api_models import ExternalScenarioAsset
from . import (
    agent_deletion_service,
    object_deletion_service,
    object_storage_service,
    release_service,
    distillation_service,
    scenario_purge_asset_service,
    scenario_purge_plan_service,
    template_catalog_service,
)


class ScenarioPurgeConflict(ValueError):
    """The requested purge is unsafe or no longer matches current state."""


class ScenarioPurgeMigrationRequired(RuntimeError):
    """The database is missing the privileged scenario-audit purge function."""


@dataclass(frozen=True)
class PreparedScenarioPurge:
    scenario_id: str
    deletion_job_ids: tuple[str, ...]
    retained: dict[str, int]


def _lock_scenario(
    db: Session,
    scenario_id: str,
    tenant_id: str,
) -> BusinessScenario:
    scenario = db.scalar(
        select(BusinessScenario)
        .where(
            BusinessScenario.id == scenario_id,
            BusinessScenario.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if scenario is None:
        raise ScenarioPurgeConflict(
            "业务场景在删除期间已变化，请刷新后重试"
        )
    return scenario


def _assert_scenario_tenant_integrity(
    db: Session,
    scenario: BusinessScenario,
) -> None:
    """Refuse a purge when a scoped history row is tenant-inconsistent.

    Most current tables close this invariant with composite foreign keys, but
    older audit tables and offline imports can still contain a mismatched
    tenant marker.  A purge must never turn such a row into a cross-tenant
    delete; leave the scenario intact until the data is repaired explicitly.
    """

    if scenario_purge_plan_service.scenario_tenant_mismatch_exists(db, scenario):
        raise ScenarioPurgeConflict(
            "场景历史数据的租户归属不一致，已停止永久删除，请先修复数据"
        )


def _validate_request(
    scenario: BusinessScenario,
    plan: scenario_purge_plan_service.ScenarioPurgePlan,
    *,
    expected_name: str,
    confirmed: bool,
    delete_audit_history: bool,
) -> None:
    if not confirmed:
        raise ScenarioPurgeConflict("永久删除尚未确认")
    if expected_name != scenario.name:
        raise ScenarioPurgeConflict("输入的场景名称与当前场景不一致")
    if plan.blockers:
        raise ScenarioPurgeConflict("；".join(plan.blockers))
    if plan.requires_audit_confirmation and not delete_audit_history:
        raise ScenarioPurgeConflict(
            "该场景包含验证或运行审计，请明确确认同时删除审计历史"
        )


def _prepare_templates(db: Session, scenario: BusinessScenario) -> None:
    try:
        release_service.assert_scenario_deletion_allowed(db, scenario)
        template_catalog_service.prepare_scenario_deletion(db, scenario)
    except (
        release_service.ReleaseValidationError,
        template_catalog_service.TemplateCatalogError,
    ) as exc:
        raise ScenarioPurgeConflict(str(exc)) from exc
    except IntegrityError as exc:
        raise ScenarioPurgeConflict(
            "场景模板或共享资源仍被保护，永久删除已取消"
        ) from exc


def _cleanup_agents(db: Session, scenario: BusinessScenario) -> list[str]:
    scenario_agents = list(
        db.scalars(
            select(Agent)
            .where(
                Agent.scenario_id == scenario.id,
                Agent.tenant_id == scenario.tenant_id,
            )
            .with_for_update()
        ).all()
    )
    scenario_agent_ids = {str(agent.id) for agent in scenario_agents}
    owned_runtime_source_ids = (
        {
            str(value)
            for value in db.scalars(
                select(DataSource.id).where(
                    DataSource.tenant_id == scenario.tenant_id,
                    DataSource.scenario_id == scenario.id,
                    DataSource.resource_scope == "agent_runtime",
                    DataSource.owner_agent_id.in_(sorted(scenario_agent_ids)),
                )
            ).all()
        }
        if scenario_agent_ids
        else set()
    )
    deletion_job_ids: list[str] = []
    try:
        for scenario_agent in scenario_agents:
            cleanup = agent_deletion_service.cleanup_agent_owned_records(
                db,
                scenario_agent,
            )
            deletion_job_ids.extend(cleanup.deletion_job_ids)
    except agent_deletion_service.AgentDeletionConflict as exc:
        raise ScenarioPurgeConflict(str(exc)) from exc
    except IntegrityError as exc:
        raise ScenarioPurgeConflict(
            "场景下的 Agent 仍被受保护资源引用，永久删除已取消"
        ) from exc
    scenario_purge_asset_service.detach_cleaned_runtime_sources(
        db,
        scenario,
        owned_runtime_source_ids,
    )
    return deletion_job_ids


def _load_assistant_attachments(
    db: Session,
    scenario: BusinessScenario,
) -> list[AssistantAttachment]:
    thread_ids = list(
        db.scalars(
            select(AssistantThread.id).where(
                AssistantThread.scenario_id == scenario.id,
                AssistantThread.tenant_id == scenario.tenant_id,
            )
        ).all()
    )
    if not thread_ids:
        return []
    return list(
        db.scalars(
            select(AssistantAttachment)
            .where(
                AssistantAttachment.thread_id.in_(thread_ids),
                AssistantAttachment.tenant_id == scenario.tenant_id,
            )
            .order_by(AssistantAttachment.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def _delete_governance_history(
    db: Session,
    scenario: BusinessScenario,
) -> None:
    proposal_ids = select(OntologyProposal.id).where(
        OntologyProposal.scenario_id == scenario.id,
        OntologyProposal.tenant_id == scenario.tenant_id,
    )
    db.execute(
        delete(OntologyReview).where(OntologyReview.proposal_id.in_(proposal_ids))
    )
    db.execute(
        delete(OntologyRelease).where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(OntologyRollback).where(
            OntologyRollback.scenario_id == scenario.id,
            OntologyRollback.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(OntologyProposal).where(
            OntologyProposal.scenario_id == scenario.id,
            OntologyProposal.tenant_id == scenario.tenant_id,
        )
    )
    db.flush()
    db.expire(
        scenario,
        (
            "ontology_releases",
            "ontology_rollbacks",
            "ontology_proposals",
            "ontology_snapshots",
            "ontology_branches",
        ),
    )


def _delete_invocation_and_reasoning_records(
    db: Session,
    scenario: BusinessScenario,
) -> None:
    invocation_ids = select(CapabilityInvocation.id).where(
        CapabilityInvocation.scenario_id == scenario.id,
        CapabilityInvocation.tenant_id == scenario.tenant_id,
    )
    run_ids = select(DerivationRun.id).where(
        DerivationRun.scenario_id == scenario.id,
        DerivationRun.tenant_id == scenario.tenant_id,
    )
    assertion_ids = select(Assertion.id).where(
        Assertion.scenario_id == scenario.id,
        Assertion.tenant_id == scenario.tenant_id,
    )
    action_log_ids = scenario_purge_plan_service.scenario_scoped_ids(
        ActionExecutionLog,
        scenario,
    )

    is_postgresql = db.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        # Runtime roles cannot delete immutable audit rows directly; the
        # migration-owned function enforces the same tenant/scenario fence.
        db.execute(
            text("SELECT public.purge_retired_scenario_audit(:scenario_id, :tenant_id)"),
            {"scenario_id": scenario.id, "tenant_id": scenario.tenant_id},
        )
    else:
        db.execute(
            delete(DerivationEvidence).where(
                or_(
                    DerivationEvidence.derivation_run_id.in_(run_ids),
                    DerivationEvidence.assertion_id.in_(assertion_ids),
                    DerivationEvidence.evidence_assertion_id.in_(assertion_ids),
                    DerivationEvidence.action_execution_log_id.in_(action_log_ids),
                    DerivationEvidence.action_scenario_id == scenario.id,
                ),
                DerivationEvidence.tenant_id == scenario.tenant_id,
            )
        )
    db.execute(
        delete(RunInputBinding).where(
            RunInputBinding.invocation_id.in_(invocation_ids),
            RunInputBinding.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(CapabilityInvocation).where(
            CapabilityInvocation.scenario_id == scenario.id,
            CapabilityInvocation.tenant_id == scenario.tenant_id,
        )
    )
    if not is_postgresql:
        # Internal supersedes edges use RESTRICT, so clear them before bulk
        # deleting this scenario's assertions.
        db.execute(
            update(Assertion)
            .where(
                Assertion.scenario_id == scenario.id,
                Assertion.tenant_id == scenario.tenant_id,
                Assertion.supersedes_assertion_id.is_not(None),
            )
            .values(supersedes_assertion_id=None)
        )
        db.execute(
            delete(Assertion).where(
                Assertion.scenario_id == scenario.id,
                Assertion.tenant_id == scenario.tenant_id,
            )
        )
        db.execute(
            delete(DerivationRunInput).where(
                DerivationRunInput.derivation_run_id.in_(run_ids),
                DerivationRunInput.tenant_id == scenario.tenant_id,
            )
        )
    db.execute(
        delete(DerivationRun).where(
            DerivationRun.scenario_id == scenario.id,
            DerivationRun.tenant_id == scenario.tenant_id,
        )
    )
    if not is_postgresql:
        db.execute(
            delete(ReasoningTerm).where(
                ReasoningTerm.scenario_id == scenario.id,
                ReasoningTerm.tenant_id == scenario.tenant_id,
            )
        )


def _detach_distillation_sources(db: Session, scenario: BusinessScenario) -> None:
    publications = list(db.scalars(
        select(DistillationPublication)
        .where(
            DistillationPublication.scenario_id == scenario.id,
            DistillationPublication.tenant_id == scenario.tenant_id,
        )
    ))
    for publication in publications:
        distillation_service.detach_publication_data_source(db, publication)


def _delete_distillation_history(db: Session, scenario: BusinessScenario) -> None:
    project_ids = select(DistillationProject.id).where(
        DistillationProject.scenario_id == scenario.id,
        DistillationProject.tenant_id == scenario.tenant_id,
    )
    now = datetime.now(timezone.utc)
    db.execute(
        update(DistillationConversationTurn)
        .where(
            DistillationConversationTurn.project_id.in_(project_ids),
            DistillationConversationTurn.tenant_id == scenario.tenant_id,
            DistillationConversationTurn.status.in_(("queued", "running", "waiting")),
        )
        .values(
            status="cancelled",
            lease_token=None,
            lease_expires_at=None,
            lease_generation=DistillationConversationTurn.lease_generation + 1,
            completed_at=now,
            updated_at=now,
        )
    )
    db.execute(
        delete(DistillationTurnAttachment).where(
            DistillationTurnAttachment.project_id.in_(project_ids),
            DistillationTurnAttachment.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(DistillationConversationTurn).where(
            DistillationConversationTurn.project_id.in_(project_ids),
            DistillationConversationTurn.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(DistillationAttachment).where(
            DistillationAttachment.project_id.in_(project_ids),
            DistillationAttachment.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(DistillationSystemAccess).where(
            DistillationSystemAccess.project_id.in_(project_ids),
            DistillationSystemAccess.tenant_id == scenario.tenant_id,
        )
    )
    publications = list(db.scalars(
        select(DistillationPublication)
        .where(
            DistillationPublication.scenario_id == scenario.id,
            DistillationPublication.tenant_id == scenario.tenant_id,
        )
    ))
    for publication in publications:
        distillation_service.delete_publication_record(db, publication)
    db.execute(
        delete(DistillationProject).where(
            DistillationProject.scenario_id == scenario.id,
            DistillationProject.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(DistillationScenarioState).where(
            DistillationScenarioState.scenario_id == scenario.id,
            DistillationScenarioState.tenant_id == scenario.tenant_id,
        )
    )


def _delete_semantic_bindings(db: Session, scenario: BusinessScenario) -> None:
    # Logical datasets are shared catalog objects; only scenario bindings and
    # scenario-owned semantic mappings belong to this purge.
    semantic_mapping_ids = select(SemanticMapping.id).where(
        SemanticMapping.scenario_id == scenario.id,
        SemanticMapping.tenant_id == scenario.tenant_id,
    )
    db.execute(
        delete(SemanticRelationMapping).where(
            SemanticRelationMapping.scenario_id == scenario.id,
            SemanticRelationMapping.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(SemanticFieldMapping).where(
            SemanticFieldMapping.semantic_mapping_id.in_(semantic_mapping_ids),
            SemanticFieldMapping.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(SemanticMapping).where(
            SemanticMapping.scenario_id == scenario.id,
            SemanticMapping.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(ScenarioDatasetBinding).where(
            ScenarioDatasetBinding.scenario_id == scenario.id,
            ScenarioDatasetBinding.tenant_id == scenario.tenant_id,
        )
    )


def _delete_external_assets(db: Session, scenario: BusinessScenario) -> None:
    db.execute(
        delete(ExternalScenarioAsset).where(
            ExternalScenarioAsset.scenario_id == scenario.id,
            ExternalScenarioAsset.tenant_id == scenario.tenant_id,
        )
    )


def _delete_assistant_history(db: Session, scenario: BusinessScenario) -> None:
    thread_ids = select(AssistantThread.id).where(
        AssistantThread.scenario_id == scenario.id,
        AssistantThread.tenant_id == scenario.tenant_id,
    )
    db.execute(
        delete(LLMInvocationTrace).where(
            LLMInvocationTrace.scenario_id == scenario.id,
            LLMInvocationTrace.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(AssistantAuditLog).where(
            AssistantAuditLog.scenario_id == scenario.id,
            AssistantAuditLog.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(AssistantRouteDecision).where(
            AssistantRouteDecision.scenario_id == scenario.id,
            AssistantRouteDecision.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(AssistantCompilationJob).where(
            AssistantCompilationJob.scenario_id == scenario.id,
            AssistantCompilationJob.tenant_id == scenario.tenant_id,
        )
    )
    # Request runs use a composite principal FK with RESTRICT semantics so a
    # thread cannot be removed until its durable upload/LLM run lineage is
    # deleted.  Delete the complete thread-owned run set first; parent retry
    # rows cascade within that set.
    db.execute(
        delete(AssistantRequestRun).where(
            AssistantRequestRun.thread_id.in_(thread_ids),
            AssistantRequestRun.tenant_id == scenario.tenant_id,
        )
    )
    db.execute(
        delete(AssistantThread).where(
            AssistantThread.scenario_id == scenario.id,
            AssistantThread.tenant_id == scenario.tenant_id,
        )
    )


def _delete_scenario_records(db: Session, scenario: BusinessScenario) -> None:
    _delete_distillation_history(db, scenario)
    _delete_external_assets(db, scenario)
    _delete_invocation_and_reasoning_records(db, scenario)
    _delete_semantic_bindings(db, scenario)
    _delete_assistant_history(db, scenario)
    db.execute(
        delete(Agent).where(
            Agent.scenario_id == scenario.id,
            Agent.tenant_id == scenario.tenant_id,
        )
    )
    _delete_governance_history(db, scenario)
    db.delete(scenario)


def prepare_scenario_purge(
    db: Session,
    *,
    scenario_id: str,
    tenant_id: str,
    expected_name: str,
    confirmed: bool,
    delete_audit_history: bool,
) -> PreparedScenarioPurge:
    """Prepare one atomic purge and return post-commit storage work."""

    scenario = _lock_scenario(db, scenario_id, tenant_id)
    _assert_scenario_tenant_integrity(db, scenario)
    plan = scenario_purge_plan_service.build_purge_plan(db, scenario)
    _validate_request(
        scenario,
        plan,
        expected_name=expected_name,
        confirmed=confirmed,
        delete_audit_history=delete_audit_history,
    )
    _prepare_templates(db, scenario)
    _detach_distillation_sources(db, scenario)
    _delete_external_assets(db, scenario)
    deletion_job_ids = _cleanup_agents(db, scenario)

    source_state = scenario_purge_asset_service.inspect_scenario_sources(
        db,
        scenario,
        lock=True,
        check_template_refs=True,
    )
    if source_state.blockers:
        raise ScenarioPurgeConflict("；".join(source_state.blockers))
    attachments = _load_assistant_attachments(db, scenario)
    try:
        deletion_job_ids.extend(
            scenario_purge_asset_service.enqueue_source_file_deletions(
                db,
                source_state,
            )
        )
        deletion_job_ids.extend(
            job_id
            for attachment in attachments
            if (
                job_id := object_deletion_service.enqueue_assistant_attachment_deletion(
                    db,
                    attachment,
                )
            )
        )
        scenario_purge_asset_service.delete_scenario_sources(
            db,
            scenario,
            source_state,
        )
    except (ValueError, object_storage_service.ObjectStorageError) as exc:
        raise ScenarioPurgeConflict(str(exc)) from exc

    try:
        _delete_scenario_records(db, scenario)
        db.flush()
    except IntegrityError as exc:
        raise ScenarioPurgeConflict(
            "场景仍被受保护的审计或共享资源引用，永久删除已取消"
        ) from exc
    except ProgrammingError as exc:
        if "purge_retired_scenario_audit" in str(exc):
            raise ScenarioPurgeMigrationRequired(
                "数据库尚未完成永久删除迁移，请先升级数据库并重启后端"
            ) from exc
        raise
    return PreparedScenarioPurge(
        scenario_id=scenario_id,
        deletion_job_ids=tuple(deletion_job_ids),
        retained=plan.retained,
    )


def drain_deletion_jobs_best_effort(
    db: Session,
    deletion_job_ids: tuple[str, ...],
) -> int:
    """Attempt object cleanup only after the authoritative transaction commits."""

    return object_deletion_service.drain_jobs_best_effort(
        db,
        list(deletion_job_ids),
    )
