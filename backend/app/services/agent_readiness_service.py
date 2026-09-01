"""Server-owned readiness axes for validation Agents and capability runtimes."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    BusinessScenario,
    ConnectorBinding,
    DataMapping,
    DataSource,
    DatasetHead,
    DatasetVersion,
    LLMConfig,
    OntologyEntity,
    ScenarioDatasetBinding,
)
from . import (
    agent_capability_service,
    llm_service,
    permission_service,
    release_service,
    runtime_connector_service,
    runtime_definition_service,
    runtime_input_service,
    tenant_service,
)


CAPABILITY_MODES = {"shadow", "prefer_capability", "capability_only"}
_HISTORICAL_RUNTIME_MODES = {"legacy", "shadow", "prefer_capability"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _issue(code: str, label: str, target: str) -> dict[str, Any]:
    return {"code": code, "label": label, "target": target, "blocking": True}


def _axis(missing: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ready": not missing, "missing": missing}


def _normalized_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    return "invocation_input" if role == "input" else role


def legacy_chat_missing(
    db: Session,
    agent: Agent,
    *,
    runtime_context: Any | None = None,
) -> list[str]:
    """Preserve the fixed-data prerequisites of an explicitly legacy Agent."""
    if not agent.scenario_id:
        return ["业务场景", "对象类型", "数据源", "数据映射", "对话模型", "映射数据绑定"]
    has_entity = (
        bool(runtime_context.entities)
        if runtime_context is not None
        else bool(
            db.execute(
                select(OntologyEntity.id)
                .where(OntologyEntity.scenario_id == agent.scenario_id)
                .limit(1)
            ).scalar_one_or_none()
        )
    )
    has_source = (
        bool(runtime_context.data_sources)
        if runtime_context is not None
        else bool(
            db.execute(
                select(DataSource.id)
                .where(
                    tenant_service.visible_clause(DataSource, db),
                    or_(
                        DataSource.scenario_id.is_(None),
                        DataSource.scenario_id == agent.scenario_id,
                    ),
                )
                .limit(1)
            ).scalar_one_or_none()
        )
    )
    if runtime_context is not None:
        runtime_definition = getattr(runtime_context, "runtime_definition", None)
        has_mapping = bool(getattr(runtime_definition, "mappings", {}) or {})
        has_bound_mapping = bool(getattr(runtime_context, "mappings", {}) or {})
    else:
        mapped_source_ids = set(
            db.scalars(
                select(DataMapping.data_source_id).where(
                    DataMapping.scenario_id == agent.scenario_id
                )
            ).all()
        )
        has_mapping = bool(mapped_source_ids)
        has_bound_mapping = bool(mapped_source_ids.intersection(agent.data_source_ids or []))
    missing: list[str] = []
    if not has_entity:
        missing.append("对象类型")
    if not has_source:
        missing.append("数据源")
    if not has_mapping:
        missing.append("数据映射")
    if not agent.llm_config_id:
        missing.append("对话模型")
    if not has_bound_mapping:
        missing.append("映射数据绑定")
    return missing


def _dataset_binding_ready(db: Session, binding: ScenarioDatasetBinding) -> bool:
    if binding.binding_mode == "pinned":
        if not binding.dataset_version_id:
            return False
        version = db.execute(
            select(DatasetVersion.id).where(
                DatasetVersion.id == binding.dataset_version_id,
                DatasetVersion.dataset_id == binding.dataset_id,
                DatasetVersion.tenant_id == binding.tenant_id,
                DatasetVersion.status == "ready",
            )
        ).scalar_one_or_none()
        return version is not None
    if not binding.dataset_head_id:
        return False
    return (
        db.execute(
            select(DatasetHead.id)
            .join(
                DatasetVersion,
                DatasetVersion.id == DatasetHead.dataset_version_id,
            )
            .where(
                DatasetHead.id == binding.dataset_head_id,
                DatasetHead.dataset_id == binding.dataset_id,
                DatasetHead.tenant_id == binding.tenant_id,
                DatasetHead.environment == binding.environment,
                DatasetVersion.status == "ready",
            )
        ).scalar_one_or_none()
        is not None
    )


def _runtime_port_issues(
    db: Session,
    agent: Agent,
    definition: Any,
    environment: str,
) -> list[dict[str, Any]]:
    tenant_id = tenant_service.current_tenant_id(db)
    issues: list[dict[str, Any]] = []
    ports = getattr(definition, "capability_ports", {}) or {}
    for raw_key, port in sorted(ports.items()):
        if str(getattr(port, "direction", "input")) != "input" or not bool(
            getattr(port, "is_required", True)
        ):
            continue
        key = str(getattr(port, "port_key", raw_key) or raw_key).strip().lower()
        if runtime_input_service.allows_invocation_override(port):
            # A chat attachment or Agent-owned database satisfies this at turn
            # time. It is not an Agent configuration defect and must not grey
            # out the conversation composer.
            continue
        elif str(getattr(port, "binding_policy", "none") or "none") == "none":
            issues.append(
                _issue(
                    "required_port_unbindable",
                    f"必填端口未配置绑定策略：{getattr(port, 'name', key)}",
                    f"capability-port:{key}",
                )
            )
        else:
            issues.append(
                _issue(
                    "runtime_binding_missing",
                    f"当前环境缺少受管绑定：{getattr(port, 'name', key)}",
                    f"resource-binding:{key}",
                )
            )
    return issues


def compute_agent_readiness(
    db: Session,
    agent: Agent,
    *,
    environment: str | None = None,
    runtime_binding_mode: str | None = None,
) -> dict[str, Any]:
    """Compute four independent readiness axes without resolving business data.

    ``runtime_binding_mode`` is a server-internal target-state projection used
    by the one-way migration gate. It avoids mutating or autoflushing the Agent
    merely to evaluate whether ``capability_only`` would be ready.
    """
    mode = str(
        runtime_binding_mode
        if runtime_binding_mode is not None
        else (getattr(agent, "runtime_binding_mode", "legacy") or "legacy")
    )
    if mode not in CAPABILITY_MODES:
        legacy = legacy_chat_missing(db, agent)
        definition_missing = [] if agent.scenario_id else [
            _issue("scenario_required", "业务场景", "agent-config:scenario")
        ]
        validation_missing = [
            _issue("legacy_prerequisite_missing", label, "agent-config:legacy")
            for label in legacy
        ]
        runtime_missing = [
            *validation_missing,
            _issue(
                "historical_runtime_disabled",
                "历史 Agent 运行模式已停用",
                "agent-migration",
            ),
        ]
        readiness = {
            "source": "server",
            "definition": _axis(definition_missing),
            "validation": _axis(validation_missing),
            "release": _axis([
                _issue(
                    "legacy_release_unmanaged",
                    "legacy Agent 尚未迁移到能力发布链路",
                    "agent-migration",
                )
            ]),
            "runtime": _axis(runtime_missing),
        }
        return _with_flat_axes(readiness)

    # Validation is a control-plane activity and always checks the live
    # authoring definition unless a caller explicitly asks for a released
    # staging/prod deployment.  The process deployment environment must never
    # make an online authoring installation unusable.
    target_environment = environment or "dev"
    definition_issues: list[dict[str, Any]] = []
    validation_issues: list[dict[str, Any]] = []
    release_issues: list[dict[str, Any]] = []
    runtime_issues: list[dict[str, Any]] = []
    scenario: BusinessScenario | None = None
    definition = None
    target_definition = None
    if not agent.scenario_id:
        definition_issues.append(
            _issue("scenario_required", "业务场景", "agent-config:scenario")
        )
    else:
        scenario = tenant_service.get_visible(db, BusinessScenario, agent.scenario_id)
        if scenario is None:
            definition_issues.append(
                _issue("scenario_unavailable", "业务场景不可用", "agent-config:scenario")
            )
        else:
            try:
                permission_service.require_scenario_permission(db, scenario, "read")
                definition = runtime_definition_service.resolve_active(
                    db, scenario, environment="dev"
                )
                agent_capability_service.validate_scope(
                    db,
                    agent_capability_service.normalize_scope(
                        agent.capability_scope,
                        legacy_default=False,
                    ),
                    definition=definition,
                )
            except Exception as exc:  # noqa: BLE001 - readiness is a safe projection.
                definition_issues.append(
                    _issue(
                        "definition_invalid",
                        str(exc)[:300] or "能力定义不可用",
                        "scenario-definition",
                    )
                )

    if definition_issues:
        validation_issues.extend(definition_issues)
    llm = (
        tenant_service.get_visible(db, LLMConfig, agent.llm_config_id)
        if agent.llm_config_id
        else None
    )
    if llm is None or not llm_service.supports_capability(llm, "chat"):
        validation_issues.append(
            _issue("chat_model_required", "可用的对话模型", "agent-config:llm")
        )

    if scenario is None or definition_issues:
        release_issues.extend(definition_issues or [
            _issue("definition_required", "需要有效能力定义", "scenario-definition")
        ])
    elif target_environment == "dev":
        try:
            release_service.capture_snapshot_content(db, scenario)
            target_definition = definition
        except Exception as exc:  # noqa: BLE001 - return a bounded readiness reason.
            release_issues.append(
                _issue(
                    "release_contract_invalid",
                    str(exc)[:300] or "定义尚未满足发布契约",
                    "release-governance",
                )
            )
    else:
        try:
            target_definition = runtime_definition_service.resolve_active(
                db, scenario, environment=target_environment
            )
        except Exception:
            release_issues.append(
                _issue(
                    "active_release_required",
                    f"{target_environment} 环境尚无有效发布",
                    "release-governance",
                )
            )

    if release_issues:
        runtime_issues.extend(release_issues)
    elif target_definition is not None:
        runtime_issues.extend(
            _runtime_port_issues(db, agent, target_definition, target_environment)
        )
    if mode in _HISTORICAL_RUNTIME_MODES:
        runtime_issues.append(
            _issue(
                "historical_runtime_disabled",
                "历史 Agent 运行模式已停用",
                "agent-migration",
            )
        )

    readiness = {
        "source": "server",
        "definition": _axis(definition_issues),
        "validation": _axis(validation_issues),
        "release": _axis(release_issues),
        "runtime": _axis(runtime_issues),
    }
    return _with_flat_axes(readiness)


def _with_flat_axes(readiness: dict[str, Any]) -> dict[str, Any]:
    readiness["definition_valid"] = bool(readiness["definition"]["ready"])
    readiness["validation_ready"] = bool(readiness["validation"]["ready"])
    readiness["release_ready"] = bool(readiness["release"]["ready"])
    readiness["runtime_ready"] = bool(readiness["runtime"]["ready"])
    return readiness


__all__ = ["CAPABILITY_MODES", "compute_agent_readiness", "legacy_chat_missing"]
