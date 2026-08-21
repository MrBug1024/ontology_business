"""Resolve the definition that a deployment is actually allowed to execute.

``dev`` remains the authoring environment and reads the live ORM rows.  A
``staging`` or ``prod`` deployment never reads those mutable rows for a new
execution: it resolves the active ``OntologyRelease`` and builds small,
read-only resource DTOs from that release's immutable snapshot instead.

The same resolver is also used for persisted workflow runs.  In that case a
superseded release is still valid when it is explicitly pinned by the run;
changing an environment's active release must not change an in-flight approval
or retry underneath an operator.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    DataMapping,
    FunctionDefinition,
    OntologyAction,
    OntologyEvent,
    OntologyRelease,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    WorkflowRun,
)
from . import connector_service, release_service


class RuntimeDefinitionError(ValueError):
    """A deployment cannot safely resolve the requested immutable definition."""


@dataclass(frozen=True)
class RuntimeDefinition:
    """A fully resolved live or frozen definition for one scenario/environment."""

    scenario: BusinessScenario
    environment: str
    source: str
    snapshot_id: str | None
    release_id: str | None
    definition_hash: str
    actions: dict[str, Any]
    functions: dict[str, Any]
    mappings: dict[str, Any]
    rules: dict[str, Any]
    events: dict[str, Any]
    workflows: dict[str, Any]

    @property
    def is_frozen(self) -> bool:
        return self.source == "release"


def _normalize_environment(value: str | None) -> str:
    try:
        return connector_service.normalize_environment(value or "dev")
    except connector_service.ConnectorBindingError as exc:
        raise RuntimeDefinitionError(str(exc)) from exc


def _runtime_resource(raw: dict[str, Any], scenario: BusinessScenario) -> SimpleNamespace:
    """Turn verified snapshot JSON into the small ORM-shaped surface we need.

    The DTO deliberately contains no persistence session or mutable ORM state.
    Permission checks still evaluate against the current scenario and grants,
    while definition fields stay frozen at the release snapshot.
    """
    data = dict(raw)
    data["scenario_id"] = scenario.id
    data["scenario"] = scenario
    return SimpleNamespace(**data)


def _live_definition(scenario: BusinessScenario, environment: str, db: Session) -> RuntimeDefinition:
    functions = {
        item.id: item
        for item in db.execute(
            select(FunctionDefinition).where(FunctionDefinition.scenario_id == scenario.id)
        ).scalars().all()
    }
    mappings = {
        item.id: item
        for item in db.execute(
            select(DataMapping).where(DataMapping.scenario_id == scenario.id)
        ).scalars().all()
    }
    actions = {
        item.id: item
        for item in db.execute(
            select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)
        ).scalars().all()
    }
    rules = {
        item.id: item
        for item in db.execute(
            select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)
        ).scalars().all()
    }
    events = {
        item.id: item
        for item in db.execute(
            select(OntologyEvent).where(OntologyEvent.scenario_id == scenario.id)
        ).scalars().all()
    }
    workflows = {
        item.id: item
        for item in db.execute(
            select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
        ).scalars().all()
    }
    return RuntimeDefinition(
        scenario=scenario,
        environment=environment,
        source="live",
        snapshot_id=None,
        release_id=None,
        # Live/dev execution intentionally preserves compatibility with legacy
        # definitions that may be invalid halfway through authoring.  A hash
        # capture normalizes and validates the whole graph, which would turn a
        # normal runtime failure of one broken workflow into a global dev
        # scheduling outage.  Frozen releases always have a verified hash.
        definition_hash="",
        actions=actions,
        functions=functions,
        mappings=mappings,
        rules=rules,
        events=events,
        workflows=workflows,
    )


def _from_snapshot(
    scenario: BusinessScenario,
    environment: str,
    snapshot: OntologySnapshot,
    *,
    release: OntologyRelease | None,
) -> RuntimeDefinition:
    if snapshot.scenario_id != scenario.id or snapshot.tenant_id != scenario.tenant_id:
        raise RuntimeDefinitionError("发布快照不属于当前业务场景")
    try:
        content = release_service.normalize_snapshot_content(snapshot.content or {})
    except Exception as exc:  # noqa: BLE001 - unsafe historic data must fail closed.
        raise RuntimeDefinitionError("发布快照定义无效，已阻止运行") from exc
    # ``OntologySnapshot.content_hash`` covers the entire normalised artifact,
    # including logical connector requirements.  Runtime provenance must use
    # the same digest; a definition-only hash would incorrectly reject every
    # snapshot that has governed bindings.
    content_hash = release_service.snapshot_hash(content)
    if not snapshot.content_hash or snapshot.content_hash != content_hash:
        raise RuntimeDefinitionError("发布快照校验失败，已阻止运行")
    return RuntimeDefinition(
        scenario=scenario,
        environment=environment,
        source="release",
        snapshot_id=snapshot.id,
        release_id=release.id if release else None,
        definition_hash=content_hash,
        actions={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("actions", [])
        },
        functions={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("functions", [])
        },
        mappings={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("mappings", [])
        },
        rules={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("rules", [])
        },
        events={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("events", [])
        },
        workflows={
            str(item["id"]): _runtime_resource(item, scenario)
            for item in content.get("workflows", [])
        },
    )


def _active_release(
    db: Session,
    scenario: BusinessScenario,
    environment: str,
) -> OntologyRelease:
    release = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.tenant_id == scenario.tenant_id,
            OntologyRelease.environment == environment,
            OntologyRelease.status == "released",
        )
        .order_by(OntologyRelease.created_at.desc())
        .limit(1)
    ).scalars().first()
    if not release:
        raise RuntimeDefinitionError(f"{environment} 环境尚未发布该业务场景")
    return release


def resolve_active(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str | None = None,
) -> RuntimeDefinition:
    """Resolve the current definition for a request entering this deployment."""
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        return _live_definition(scenario, normalized_environment, db)
    release = _active_release(db, scenario, normalized_environment)
    snapshot = db.get(OntologySnapshot, release.snapshot_id)
    if not snapshot:
        raise RuntimeDefinitionError(f"{normalized_environment} 环境的发布快照不可用")
    return _from_snapshot(
        scenario,
        normalized_environment,
        snapshot,
        release=release,
    )


def resolve_pinned(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    snapshot_id: str | None,
    release_id: str | None,
    definition_hash: str | None,
) -> RuntimeDefinition:
    """Resolve a durable non-dev definition pin for a queued operation.

    A mapping-refresh worker uses this instead of the current active release,
    so a queued read cannot fall forward after a later deployment promotion.
    """
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        raise RuntimeDefinitionError("开发环境不应携带发布定义固定版本")
    if not snapshot_id or not release_id or not definition_hash:
        raise RuntimeDefinitionError("运行定义快照缺失，已阻止执行")
    release = db.get(OntologyRelease, release_id)
    snapshot = db.get(OntologySnapshot, snapshot_id)
    if not release or not snapshot:
        raise RuntimeDefinitionError("运行固定的发布版本已不可用")
    if (
        release.scenario_id != scenario.id
        or release.tenant_id != scenario.tenant_id
        or release.environment != normalized_environment
        or release.snapshot_id != snapshot.id
        or release.status not in {"released", "superseded", "rolled_back"}
    ):
        raise RuntimeDefinitionError("运行固定的发布版本不一致，已阻止执行")
    definition = _from_snapshot(scenario, normalized_environment, snapshot, release=release)
    if definition.definition_hash != definition_hash:
        raise RuntimeDefinitionError("运行定义快照完整性校验失败")
    return definition


def resolve_for_run(db: Session, run: WorkflowRun) -> RuntimeDefinition:
    """Resolve the version pinned when a durable run was queued.

    A non-dev run is never allowed to fall forward to whatever environment
    release happens to be active now.  That would change approval/retry meaning
    and could rebind external effects mid-flight.
    """
    scenario = db.get(BusinessScenario, run.scenario_id)
    if not scenario:
        raise RuntimeDefinitionError("工作流所属场景不存在")
    environment = _normalize_environment(run.environment)
    if environment == "dev" and not run.definition_snapshot_id and not run.release_id:
        return _live_definition(scenario, environment, db)
    if not run.definition_snapshot_id or not run.release_id:
        raise RuntimeDefinitionError("运行定义快照缺失，已阻止执行")
    release = db.get(OntologyRelease, run.release_id)
    snapshot = db.get(OntologySnapshot, run.definition_snapshot_id)
    if not release or not snapshot:
        raise RuntimeDefinitionError("运行固定的发布版本已不可用")
    if (
        release.scenario_id != scenario.id
        or release.tenant_id != scenario.tenant_id
        or release.environment != environment
        or release.snapshot_id != snapshot.id
    ):
        raise RuntimeDefinitionError("运行固定的发布版本不一致，已阻止执行")
    return _from_snapshot(scenario, environment, snapshot, release=release)


def resolve_resource(
    definition: RuntimeDefinition,
    kind: str,
    resource_id: str,
) -> Any:
    resources = {
        "action": definition.actions,
        "function": definition.functions,
        "mapping": definition.mappings,
        "rule": definition.rules,
        "event": definition.events,
        "workflow": definition.workflows,
    }.get(kind)
    if resources is None:
        raise RuntimeDefinitionError("不支持的运行时资源类型")
    resource = resources.get(str(resource_id))
    if resource is None:
        raise RuntimeDefinitionError(f"{kind} 不存在于当前运行定义")
    return resource


def active_definitions(
    db: Session,
    *,
    environment: str,
) -> Iterable[RuntimeDefinition]:
    """Yield every released scenario definition for a non-dev scheduler tick."""
    normalized_environment = _normalize_environment(environment)
    if normalized_environment == "dev":
        scenarios = db.execute(select(BusinessScenario)).scalars().all()
        return [_live_definition(scenario, normalized_environment, db) for scenario in scenarios]
    releases = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.environment == normalized_environment,
            OntologyRelease.status == "released",
        )
        .order_by(OntologyRelease.created_at.desc())
    ).scalars().all()
    definitions: list[RuntimeDefinition] = []
    for release in releases:
        scenario = db.get(BusinessScenario, release.scenario_id)
        snapshot = db.get(OntologySnapshot, release.snapshot_id)
        if not scenario or not snapshot:
            continue
        try:
            definitions.append(
                _from_snapshot(scenario, normalized_environment, snapshot, release=release)
            )
        except RuntimeDefinitionError:
            # One malformed historic release must not stop unrelated scenarios;
            # it stays unavailable rather than being silently replaced by live.
            continue
    return definitions
