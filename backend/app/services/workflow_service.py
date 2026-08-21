"""工作流服务：操作执行 / 规则评估 / 可视化工作流编排（DAG）。

设计原则（元模型驱动）：
- 平台只提供"执行框架"，不预设业务语义
- 操作（Action）通过 executor_type 绑定到具体执行器（sql/skill/mcp/http/script）
- 规则（Rule）用 JSON 条件表达式描述，由通用规则引擎解析
- 工作流（Workflow）支持两种形态：
  1. 旧版线性 steps（兼容保留）
  2. 可视化 DAG（nodes + edges，VueFlow 格式），支持分支/并行/LLM 节点
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import hashlib
import json
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    ActionExecutionLog,
    BusinessScenario,
    DataSource,
    LLMConfig,
    OntologyAction,
    OntologyEvent,
    OntologyRule,
    OntologyWorkflow,
    Skill,
)
from . import (
    datasource_service,
    skill_service,
    mcp_service,
    llm_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)
from .policies import PolicyViolation, validate_action_params, validate_workflow_graph


class WorkflowDeadlineExceeded(PolicyViolation):
    """Raised before a workflow starts another node after its run deadline."""


def _check_deadline(deadline_at: datetime | None) -> None:
    if deadline_at is None:
        return
    deadline = deadline_at if deadline_at.tzinfo else deadline_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= deadline:
        raise WorkflowDeadlineExceeded("任务执行超过配置的超时限制，已停止后续节点")


def _workflow_action_status(response: dict[str, Any]) -> tuple[str, str]:
    """Map idempotent records into safe workflow execution semantics.

    A completed Action is safely replayed as data.  A failed or indeterminate
    Action is *not* invoked again automatically with a fresh key: doing so could
    repeat an external side effect whose outcome was never confirmed.
    """
    status = str(response.get("status") or "failed")
    if status != "idempotent_replay":
        return status, str(response.get("error") or "")
    original = str(response.get("original_status") or "")
    if original == "success":
        return status, ""
    return "failed", str(response.get("error") or "此前同一幂等操作未成功完成，已阻止自动重放")


def _runtime_provenance(
    runtime_definition: runtime_definition_service.RuntimeDefinition | None,
    runtime_environment: str | None,
) -> dict[str, str | None]:
    """Return auditable, execution-local definition provenance.

    A frozen definition is not optional metadata: its environment must match
    the deployment assertion and every child action receives the same release
    pin.  Legacy callers without a definition remain supported for dev-only
    internal use, but no non-dev route may rely on that compatibility path.
    """
    environment = runtime_connector_service.runtime_environment(runtime_environment)
    if runtime_definition is None:
        return {
            "environment": environment,
            "definition_snapshot_id": None,
            "release_id": None,
            "definition_hash": "",
            "definition_source": "live",
        }
    if runtime_definition.environment != environment:
        raise PolicyViolation("运行定义环境与当前部署环境不一致，已阻止执行")
    return {
        "environment": environment,
        "definition_snapshot_id": runtime_definition.snapshot_id,
        "release_id": runtime_definition.release_id,
        "definition_hash": runtime_definition.definition_hash,
        "definition_source": runtime_definition.source,
    }


def _scoped_idempotency_key(key: str | None, environment: str) -> str | None:
    """Make external idempotency keys environment-local without widening SQL keys."""
    if not key:
        return None
    scoped = f"{environment}:{key}"
    if len(scoped) <= 120:
        return scoped
    return f"{environment}:sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _definition_resource(
    db: Session,
    workflow: Any,
    *,
    kind: str,
    resource_id: str,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None,
) -> Any | None:
    """Resolve child resources from the pinned map, never fall back to live."""
    if runtime_definition is not None:
        try:
            return runtime_definition_service.resolve_resource(
                runtime_definition, kind, resource_id
            )
        except runtime_definition_service.RuntimeDefinitionError:
            return None
    model = {
        "action": OntologyAction,
        "rule": OntologyRule,
        "event": OntologyEvent,
    }.get(kind)
    if model is None:
        return None
    resource = db.get(model, resource_id)
    if resource and resource.scenario_id != workflow.scenario_id:
        return None
    return resource


def validate_workflow_definition(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """后端统一校验工作流 DAG；前端校验只是交互提示，不能作为安全边界。"""
    validate_workflow_graph(nodes, edges)
    unsafe_types = sorted(
        {
            str(node.get("type") or "")
            for node in nodes
            if str(node.get("type") or "") in {"http", "script"}
        }
    )
    if unsafe_types and not get_settings().allow_unsafe_workflow_nodes:
        labels = "、".join("原生 HTTP" if node_type == "http" else "Python 脚本" for node_type in unsafe_types)
        raise PolicyViolation(
            f"{labels} 节点默认停用；请改用经过权限、确认、幂等和审计约束的 Action"
        )


_HTTP_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_HTTP_BLOCKED_HEADERS = {"host", "connection", "proxy-authorization", "proxy-connection"}
_UNMANAGED_SKILL_CONFIG_FIELDS = {"skill_name", "skill_path", "script", "interpreter"}


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def validate_http_action_config(config: dict[str, Any]) -> None:
    """Validate a declarative HTTP Action without performing network I/O."""
    url = str((config or {}).get("url") or "").strip()
    if not url:
        raise PolicyViolation("HTTP Action 需要 url 配置")
    parsed = urlparse(url)
    settings = get_settings()
    if parsed.scheme not in ({"https", "http"} if settings.allow_insecure_http_actions else {"https"}):
        raise PolicyViolation("HTTP Action 仅允许 HTTPS 目标；受控开发环境需显式开启不安全 HTTP")
    if not parsed.hostname or parsed.username or parsed.password:
        raise PolicyViolation("HTTP Action URL 必须包含合法主机，且不能包含用户凭据")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise PolicyViolation("HTTP Action 不允许访问本机或内网主机")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise PolicyViolation("HTTP Action 不允许访问本机、私网或保留地址")
    method = str((config or {}).get("method") or "GET").upper()
    if method not in _HTTP_ALLOWED_METHODS:
        raise PolicyViolation("HTTP Action 请求方法不受支持")
    headers = (config or {}).get("headers") or {}
    if not isinstance(headers, dict):
        raise PolicyViolation("HTTP Action headers 必须是对象")
    blocked = sorted(str(key) for key in headers if str(key).lower() in _HTTP_BLOCKED_HEADERS)
    if blocked:
        raise PolicyViolation(f"HTTP Action 不允许设置受控请求头: {', '.join(blocked)}")


def validate_skill_action_config(db: Session, config: dict[str, Any]) -> Skill:
    """Resolve a Skill Action through the governed catalog, never a caller path.

    Action definitions are durable and may be imported or created outside the
    normal editor. Keeping the lookup here makes execution fail closed for
    legacy JSON that still contains an arbitrary interpreter, path, or script.
    """
    config = config or {}
    unmanaged = sorted(
        key for key in _UNMANAGED_SKILL_CONFIG_FIELDS if config.get(key) not in (None, "")
    )
    if unmanaged:
        raise PolicyViolation("Skill Action 只能引用受管理 skill_id，不能包含本地路径或脚本配置")
    skill_id = str(config.get("skill_id") or "").strip()
    if not skill_id:
        raise PolicyViolation("Skill Action 需要已登记的 skill_id")
    skill = tenant_service.require_visible(db, Skill, skill_id, "操作引用的 Skill 不存在")
    if not skill.enabled:
        raise PolicyViolation("操作引用的 Skill 当前已停用")
    return skill


def _assert_public_http_target(url: str) -> None:
    """Resolve once before connecting and reject private DNS answers/redirect bypasses."""
    parsed = urlparse(url)
    validate_http_action_config({"url": url, "method": "GET"})
    hostname = parsed.hostname or ""
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise PolicyViolation("HTTP Action 目标主机无法安全解析") from exc
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise PolicyViolation("HTTP Action 目标解析到本机、私网或保留地址")


# ──────────────────────────────────────────────
# 规则引擎：JSON 条件表达式解析
# ──────────────────────────────────────────────
# 条件表达式格式：
# {"op": "and"|"or"|"not", "conditions": [...]}
# {"field": "数量", "op": ">", "value": 2}
# 支持运算符: > >= < <= == != in not_in contains not_contains is_null is_not_null

_OPS = {
    ">": lambda a, b: _safe(a, 0) > _safe(b, 0),
    ">=": lambda a, b: _safe(a, 0) >= _safe(b, 0),
    "<": lambda a, b: _safe(a, 0) < _safe(b, 0),
    "<=": lambda a, b: _safe(a, 0) <= _safe(b, 0),
    "==": lambda a, b: _norm(a) == _norm(b),
    "!=": lambda a, b: _norm(a) != _norm(b),
    "in": lambda a, b: _norm(a) in (b if isinstance(b, list) else [b]),
    "not_in": lambda a, b: _norm(a) not in (b if isinstance(b, list) else [b]),
    "contains": lambda a, b: str(b) in str(a),
    "not_contains": lambda a, b: str(b) not in str(a),
    "is_null": lambda a, b: a is None or a == "",
    "is_not_null": lambda a, b: a is not None and a != "",
}


def _safe(v: Any, default: Any = 0) -> Any:
    """尝试转 float，失败返回 default。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm(v: Any) -> Any:
    """规范化比较值：去空格、转小写（字符串）。"""
    if isinstance(v, str):
        return v.strip()
    return v


def evaluate_condition(condition: dict[str, Any], record: dict[str, Any]) -> bool:
    """递归评估 JSON 条件表达式。"""
    if not condition:
        return False

    op = condition.get("op", "")

    # 逻辑组合
    if op in ("and", "or", "not"):
        conds = condition.get("conditions", [])
        if op == "not":
            return not (evaluate_condition(conds[0], record) if conds else False)
        results = [evaluate_condition(c, record) for c in conds]
        return all(results) if op == "and" else any(results)

    # 叶子条件：field + op + value
    field = condition.get("field", "")
    value = condition.get("value")
    actual = record.get(field)
    func = _OPS.get(op)
    if not func:
        return False
    try:
        return func(actual, value)
    except Exception:  # noqa: BLE001
        return False


def evaluate_rule(rule: OntologyRule, record: dict[str, Any]) -> dict[str, Any]:
    """评估单条规则对给定记录是否命中。"""
    matched = evaluate_condition(rule.condition or {}, record)
    return {
        "rule_id": rule.id,
        "rule_name": rule.name,
        "matched": matched,
        "severity": rule.severity,
        "action_on_match": rule.action_on_match if matched else "",
        "trigger_action_ids": rule.trigger_action_ids if matched else [],
    }


# ──────────────────────────────────────────────
# 操作执行器
# ──────────────────────────────────────────────
def _permission_summary(
    db: Session,
    action: OntologyAction,
    confirmed: bool,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """返回经集中 ACL 判定的 Action 权限摘要，而不是相信前端字段。"""
    decision = permission_service.check_action(db, action, "read" if dry_run else "execute")
    return {
        "allowed": decision.allowed,
        "scope": "action",
        "configured_scope": action.permission_scope or "scenario",
        "requires_confirmation": bool(action.requires_confirmation),
        "confirmed": confirmed,
        "reason": decision.reason,
        "role": decision.role_key,
    }


def _action_runtime_connector(
    db: Session,
    action: Any,
    *,
    kind: str,
    config: dict[str, Any],
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> tuple[Any, dict[str, Any]]:
    scenario = db.get(BusinessScenario, action.scenario_id)
    if not scenario:
        raise PolicyViolation("操作所属业务场景不存在")
    try:
        return runtime_connector_service.resolve_connector(
            db,
            scenario,
            kind=kind,
            config=config,
            environment=runtime_environment,
            release_id=(runtime_definition.release_id if runtime_definition else None),
        )
    except runtime_connector_service.RuntimeConnectorError as exc:
        raise PolicyViolation(str(exc)) from exc


def _action_plan(
    db: Session,
    action: Any,
    params: dict[str, Any],
    *,
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> dict[str, Any]:
    """生成预演计划；只返回执行元数据和参数，不调用任何执行器。"""
    config = action.executor_config or {}
    plan = {
        "action_id": action.id,
        "action_name": action.name,
        "executor_type": action.executor_type,
        "parameter_count": len(params),
        "parameters": params,
        "side_effects_skipped": True,
    }
    if action.executor_type == "sql":
        if any(key in config for key in ("data_source_id", "data_source_binding_key", "data_source_binding_ref")):
            connector, audit = _action_runtime_connector(
                db,
                action,
                kind="data_source",
                config=config,
                runtime_environment=runtime_environment,
                runtime_definition=runtime_definition,
            )
            plan["data_source_id"] = str(connector.id)
            plan["connector_audit"] = [audit]
        else:
            plan["data_source_id"] = ""
        plan["sql_template"] = str(config.get("sql", ""))[:2000]
    elif action.executor_type == "mcp":
        if any(key in config for key in ("mcp_id", "mcp_binding_key", "mcp_binding_ref")):
            connector, audit = _action_runtime_connector(
                db,
                action,
                kind="mcp",
                config=config,
                runtime_environment=runtime_environment,
                runtime_definition=runtime_definition,
            )
            plan["mcp_id"] = str(connector.id)
            plan["connector_audit"] = [audit]
        else:
            plan["mcp_id"] = ""
        plan["target"] = str(config.get("tool_name") or "")
    elif action.executor_type == "http":
        plan["method"] = str(config.get("method", "GET")).upper()
        plan["url"] = str(config.get("url", ""))[:500]
    elif action.executor_type == "skill":
        plan["skill_id"] = str(config.get("skill_id") or "")
    elif action.executor_type == "script":
        plan["target"] = "受控脚本"
    return plan


def _response_from_log(log: ActionExecutionLog, status: str | None = None) -> dict[str, Any]:
    return {
        "log_id": log.id,
        "status": status or log.status,
        "result": log.result or {},
        "connector_audit": log.connector_audit or [],
        "error": log.error or "",
        "duration_ms": log.duration_ms or 0,
        "idempotency_key": log.idempotency_key,
        "permission": {"allowed": True, "scope": "scenario", "confirmed": True},
        # A direct Action response is the user's first audit surface.  Keep
        # the immutable execution pin alongside the result (including replay
        # and confirmation responses) rather than making callers query logs.
        "environment": log.environment or "dev",
        "definition_snapshot_id": log.definition_snapshot_id,
        "release_id": log.release_id,
        "definition_hash": log.definition_hash or "",
        "definition_source": log.definition_source or "live",
    }


def _find_idempotent_log(db: Session, action: Any, key: str) -> ActionExecutionLog | None:
    return db.execute(
        select(ActionExecutionLog)
        .where(
            ActionExecutionLog.scenario_id == action.scenario_id,
            ActionExecutionLog.target_type == "action",
            ActionExecutionLog.target_id == action.id,
            ActionExecutionLog.idempotency_key == key,
            ActionExecutionLog.mode == "execute",
        )
        .order_by(ActionExecutionLog.created_at.desc())
    ).scalars().first()


def _idempotent_replay(
    existing: ActionExecutionLog,
    normalized: dict[str, Any],
    permission: dict[str, Any],
) -> dict[str, Any]:
    if (existing.input_params or {}) != normalized:
        raise PolicyViolation("同一个 idempotency_key 不能复用不同的参数")
    replay = _response_from_log(existing, status="idempotent_replay")
    replay["original_status"] = existing.status
    replay["permission"] = permission
    return replay


def preview_action(
    db: Session,
    action: Any,
    params: dict[str, Any],
    *,
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> dict[str, Any]:
    """校验参数并生成 Action 预演，不触发 SQL/HTTP/脚本/MCP/Skill。"""
    if not action.enabled:
        raise PolicyViolation("操作已禁用")
    normalized = validate_action_params(action.input_schema or {}, params)
    provenance = _runtime_provenance(runtime_definition, runtime_environment)
    plan = _action_plan(
        db,
        action,
        normalized,
        runtime_environment=runtime_environment,
        runtime_definition=runtime_definition,
    )
    permission = _permission_summary(db, action, confirmed=False, dry_run=True)
    if not permission["allowed"]:
        raise PolicyViolation("没有预演该操作的权限")
    start = time.time()
    log = ActionExecutionLog(
        scenario_id=action.scenario_id,
        target_type="action",
        target_id=action.id,
        target_name=action.name,
        input_params=normalized,
        status="dry_run",
        mode="dry_run",
        result={"plan": plan, "permission": permission},
        connector_audit=plan.get("connector_audit", []),
        **provenance,
        duration_ms=int((time.time() - start) * 1000),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    response = _response_from_log(log, status="dry_run")
    response.update(
        {
            "requires_confirmation": bool(action.requires_confirmation),
            "permission": permission,
            "idempotency_key": None,
        }
    )
    return response


def execute_action(
    db: Session,
    action: Any,
    params: dict[str, Any],
    *,
    confirm: bool = True,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    enforce_policy: bool = True,
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> dict[str, Any]:
    """执行单个操作，统一完成参数校验、权限确认和幂等日志。"""
    if dry_run:
        return preview_action(
            db,
            action,
            params,
            runtime_environment=runtime_environment,
            runtime_definition=runtime_definition,
        )
    provenance = _runtime_provenance(runtime_definition, runtime_environment)
    scoped_idempotency_key = _scoped_idempotency_key(
        idempotency_key, str(provenance["environment"])
    )
    if not action.enabled:
        raise PolicyViolation("操作已禁用")
    normalized = validate_action_params(action.input_schema or {}, params)
    permission = _permission_summary(db, action, confirmed=confirm)
    if not permission["allowed"]:
        raise PolicyViolation("没有执行该操作的权限")

    if enforce_policy and action.requires_confirmation and not confirm:
        log = ActionExecutionLog(
            scenario_id=action.scenario_id,
            target_type="action",
            target_id=action.id,
            target_name=action.name,
            input_params=normalized,
            status="confirmation_required",
            mode="confirmation",
            # 确认提醒不占用幂等键；真正的 execute 记录才会保留并竞争该键。
            idempotency_key=None,
            result={"permission": permission},
            **provenance,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        response = _response_from_log(log)
        response.update({
            "status": "confirmation_required",
            "requires_confirmation": True,
            "permission": permission,
            "idempotency_key": idempotency_key,
        })
        return response

    if enforce_policy and action.idempotency_required and not idempotency_key:
        raise PolicyViolation("执行操作必须提供 idempotency_key")

    if enforce_policy and scoped_idempotency_key:
        existing = _find_idempotent_log(db, action, scoped_idempotency_key)
        if existing:
            return _idempotent_replay(existing, normalized, permission)

    start = time.time()
    log = ActionExecutionLog(
        scenario_id=action.scenario_id,
        target_type="action",
        target_id=action.id,
        target_name=action.name,
        input_params=normalized,
        status="running",
        mode="execute",
        idempotency_key=scoped_idempotency_key,
        **provenance,
    )
    db.add(log)
    # 先提交 running 占位记录，再调用外部执行器；这样并发请求会在副作用前竞争同一幂等键。
    if enforce_policy and scoped_idempotency_key:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _find_idempotent_log(db, action, scoped_idempotency_key)
            if existing:
                return _idempotent_replay(existing, normalized, permission)
            raise
        db.refresh(log)
    else:
        db.flush()

    try:
        result, connector_audit = _dispatch_executor(
            db,
            action,
            normalized,
            runtime_environment=runtime_environment,
            runtime_definition=runtime_definition,
        )
        log.status = "success"
        log.result = result if isinstance(result, dict) else {"output": str(result)[:2000]}
        log.connector_audit = connector_audit
    except Exception as exc:  # noqa: BLE001
        log.status = "failed"
        log.error = str(exc)
        log.result = {"error": str(exc)}

    log.duration_ms = int((time.time() - start) * 1000)
    db.commit()
    db.refresh(log)
    response = _response_from_log(log)
    response.update({"requires_confirmation": bool(action.requires_confirmation), "permission": permission})
    return response


def _dispatch_executor(
    db: Session,
    action: Any,
    params: dict[str, Any],
    *,
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """按 executor_type 分发到具体执行器。"""
    etype = action.executor_type
    cfg = action.executor_config or {}

    # A release snapshot intentionally redacts arbitrary HTTP headers and
    # Skill/Script execution may depend on mutable host state.  Those executor
    # types are therefore not portable, frozen deployment semantics yet.  Keep
    # the runtime guard even though publish-time validation rejects new ones.
    if runtime_definition and runtime_definition.is_frozen and etype in {"http", "skill", "script"}:
        raise PolicyViolation(f"{etype} Action 不能在已冻结的发布环境执行")

    if etype == "sql":
        source, audit = _action_runtime_connector(
            db,
            action,
            kind="data_source",
            config=cfg,
            runtime_environment=runtime_environment,
            runtime_definition=runtime_definition,
        )
        return _exec_sql(
            db,
            {**cfg, "scenario_id": action.scenario_id},
            params,
            data_source=source,
        ), [audit]
    if etype == "skill":
        return _exec_skill(db, cfg, params), []
    if etype == "mcp":
        mcp, audit = _action_runtime_connector(
            db,
            action,
            kind="mcp",
            config=cfg,
            runtime_environment=runtime_environment,
            runtime_definition=runtime_definition,
        )
        return _exec_mcp(db, cfg, params, mcp=mcp), [audit]
    if etype == "http":
        return _exec_http(cfg, params), []
    if etype == "script":
        return _exec_script(cfg, params), []
    raise ValueError(f"未知执行器类型: {etype}")


def _exec_sql(
    db: Session,
    cfg: dict,
    params: dict,
    *,
    data_source: DataSource | None = None,
) -> Any:
    """SQL 执行器：在指定数据源上执行 SQL。

    cfg: {data_source_id, sql}
    params 中的值会替换 SQL 中的 {param_name} 占位符。
    """
    ds_id = cfg.get("data_source_id", "")
    sql = cfg.get("sql", "")
    if not sql or (not ds_id and data_source is None):
        raise ValueError("SQL 执行器需要 data_source_id 和 sql 配置")
    # 参数替换
    for k, v in params.items():
        sql = sql.replace("{%s}" % k, str(v))
    ds = data_source or db.get(DataSource, ds_id)
    if not ds:
        raise ValueError(f"数据源不存在: {ds_id}")
    if ds.scenario_id not in (None, cfg.get("scenario_id")) and cfg.get("scenario_id"):
        raise PolicyViolation("操作不能访问其他业务场景的数据源")
    return datasource_service.run_query(ds, sql, limit=get_settings().max_query_rows)


def _exec_skill(db: Session, cfg: dict, params: dict) -> Any:
    """Execute a catalogued enabled Skill with a bounded argument shape."""
    skill = validate_skill_action_config(db, cfg)
    args = params.get("args", [])
    if isinstance(args, dict):
        args = [str(v) for v in args.values()]
    if not isinstance(args, list):
        raise ValueError("Skill Action 的 args 必须是数组或对象")
    result = skill_service.execute_skill(skill, [str(arg) for arg in args], timeout=60)
    return {
        "status": str(result.get("status") or "error"),
        "stdout": str(result.get("stdout") or "")[:3000],
        "stderr": str(result.get("stderr") or "")[:1000],
        "exit_code": int(result.get("exit_code") or 0),
    }


def _exec_mcp(db: Session, cfg: dict, params: dict, *, mcp: Any = None) -> Any:
    """MCP 执行器：调用 MCP 工具。

    cfg: {mcp_id, tool_name}
    """
    mcp_id = cfg.get("mcp_id", "")
    tool_name = cfg.get("tool_name", "")
    if not tool_name or (not mcp_id and mcp is None):
        raise ValueError("MCP 执行器需要 mcp_id 和 tool_name 配置")
    from ..models import MCPConfig

    mcp = mcp or db.get(MCPConfig, mcp_id)
    if not mcp:
        raise ValueError(f"MCP 不存在: {mcp_id}")
    return mcp_service.call_tool(mcp, tool_name, params)


def _exec_http(cfg: dict, params: dict) -> Any:
    """HTTP 执行器：发送 HTTP 请求。

    cfg: {method, url, headers}
    params: 请求体/查询参数
    """
    import urllib.error
    import urllib.request

    method = str(cfg.get("method", "GET")).upper()
    url = str(cfg.get("url", ""))
    headers = dict(cfg.get("headers") or {})
    validate_http_action_config({**cfg, "method": method, "url": url, "headers": headers})
    # 参数替换 URL
    for k, v in params.items():
        url = url.replace("{%s}" % k, str(v))
    _assert_public_http_target(url)
    data = None
    if method in ("POST", "PUT", "PATCH"):
        body = params.get("body", params)
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers={str(k): str(v) for k, v in headers.items()}, method=method)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise PolicyViolation("HTTP Action 不跟随重定向，请为目标配置经过审核的最终 HTTPS 地址") from exc
        raise
    with response as resp:
        body = resp.read(1_048_577)
        if len(body) > 1_048_576:
            raise PolicyViolation("HTTP Action 响应超过 1MB 限制")
        body = body.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"status": resp.status, "body": body[:3000]}


def _exec_script(cfg: dict, params: dict) -> Any:
    """脚本执行器：执行 Python 脚本片段。

    cfg: {script}  # Python 代码，可用 params 变量
    """
    script = cfg.get("script", "")
    if not script:
        raise ValueError("脚本执行器需要 script 配置")
    if not get_settings().allow_unsafe_workflow_nodes:
        raise PolicyViolation("脚本节点默认被禁用，请在受控环境中显式开启")
    namespace: dict[str, Any] = {"params": params, "json": json}
    exec(script, namespace)  # noqa: S102
    return namespace.get("result", namespace.get("output", ""))


# ──────────────────────────────────────────────
# 模板变量：{{params.x}} / {{n1.result}} / {{n1.output}}
# ──────────────────────────────────────────────
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class _NodeOut:
    """节点输出包装：同时支持属性访问（n1.result）与下标访问（n1["result"]）。"""

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, k: str) -> Any:
        d = object.__getattribute__(self, "_d")
        if k in d:
            return d[k]
        raise AttributeError(k)

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def __contains__(self, k: str) -> bool:
        return k in self._d

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)

    def to_dict(self) -> dict[str, Any]:
        return self._d

    def __repr__(self) -> str:  # pragma: no cover
        return repr(self._d)


def _wrap_out(out: Any) -> dict[str, Any]:
    """统一节点输出为 {"result": ...}，保证 {{n1.result}} / n1.result 一致可用。"""
    return {"result": out}


def _lookup_path(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def render_template(value: Any, ctx: dict[str, Any]) -> Any:
    """递归渲染模板：字符串中的 {{path}} 替换为上下文值。

    - 整个字符串恰好是一个变量 → 返回原始类型（数字/对象等）
    - 否则做字符串拼接（None → 空串）
    - dict/list 递归处理
    """
    if isinstance(value, str):
        m = _VAR_RE.fullmatch(value.strip())
        if m:
            return _lookup_path(ctx, m.group(1))
        if _VAR_RE.search(value):
            def _sub(mm: re.Match) -> str:
                v = _lookup_path(ctx, mm.group(1))
                if v is None:
                    return ""
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False)
                return str(v)
            return _VAR_RE.sub(_sub, value)
        return value
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    return value


# ──────────────────────────────────────────────
# 工作流执行
# ──────────────────────────────────────────────
def execute_workflow(
    db: Session,
    workflow: Any,
    params: dict[str, Any],
    *,
    execution_id: str | None = None,
    approved_node_ids: set[str] | None = None,
    attempt: int = 1,
    source_run_id: str | None = None,
    runtime_environment: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
    deadline_at: datetime | None = None,
) -> dict[str, Any]:
    """执行工作流：优先可视化 DAG（nodes/edges），回退旧版线性 steps。

    ``execution_id`` 由 P1 任务队列持久化：审批恢复和自动重试均复用同一
    执行谱系，已成功的 Action 只回放其审计结果而不再次产生副作用。显式
    人工重试会由队列生成新的 ``execution_id``。
    """
    status = workflow.status or ("active" if workflow.enabled else "disabled")
    if status != "active" or not workflow.enabled:
        raise PolicyViolation("工作流当前未启用")
    workflow_permission = permission_service.check_workflow(db, workflow, "execute")
    if not workflow_permission.allowed:
        raise PolicyViolation("没有执行该工作流的权限")
    start = time.time()
    provenance = _runtime_provenance(runtime_definition, runtime_environment)
    log = ActionExecutionLog(
        scenario_id=workflow.scenario_id,
        target_type="workflow",
        target_id=workflow.id,
        target_name=workflow.name,
        input_params=params,
        status="running",
        **provenance,
    )
    db.add(log)
    db.flush()
    execution_key = execution_id or log.id
    approved_nodes = approved_node_ids or set()

    try:
        _check_deadline(deadline_at)
        if workflow.nodes:
            validate_workflow_definition(workflow.nodes, workflow.edges or [])
        if workflow.nodes:
            step_results = _execute_dag(
                db,
                workflow,
                params,
                execution_id=execution_key,
                approved_node_ids=approved_nodes,
                attempt=attempt,
                source_run_id=source_run_id,
                runtime_environment=runtime_environment,
                runtime_definition=runtime_definition,
                deadline_at=deadline_at,
            )
        else:
            step_results = _execute_steps(
                db,
                workflow,
                params,
                execution_id=execution_key,
                approved_node_ids=approved_nodes,
                attempt=attempt,
                source_run_id=source_run_id,
                runtime_environment=runtime_environment,
                runtime_definition=runtime_definition,
                deadline_at=deadline_at,
            )
        log.result = {"steps": step_results}
        log.connector_audit = [
            audit
            for step in step_results
            for audit in (step.get("connector_audit") or [])
            if isinstance(audit, dict)
        ]
        failed = next((step for step in step_results if step.get("status") == "failed"), None)
        waiting = next((step for step in step_results if step.get("status") == "awaiting_approval"), None)
        if failed:
            log.status = "failed"
            log.error = failed.get("error") or "工作流节点执行失败"
        elif waiting:
            log.status = "awaiting_approval"
        else:
            log.status = "success"
    except Exception as exc:  # noqa: BLE001
        log.status = "failed"
        log.error = str(exc)
        log.result = {"steps": [], "error": str(exc)}

    log.duration_ms = int((time.time() - start) * 1000)
    db.commit()
    db.refresh(log)
    return {
        "log_id": log.id,
        "status": log.status,
        "steps": log.result.get("steps", []),
        "connector_audit": log.connector_audit or [],
        "error": log.error,
        "duration_ms": log.duration_ms,
    }


# ── 旧版线性 steps（兼容）──
def _execute_steps(
    db: Session,
    workflow: Any,
    params: dict[str, Any],
    *,
    execution_id: str,
    approved_node_ids: set[str],
    attempt: int,
    source_run_id: str | None,
    runtime_environment: str | None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None,
    deadline_at: datetime | None,
) -> list[dict[str, Any]]:
    step_results: list[dict[str, Any]] = []
    context: dict[str, Any] = {"params": params}

    for i, step in enumerate(workflow.steps or []):
        _check_deadline(deadline_at)
        step_type = step.get("type", "")
        step_num = step.get("step", i + 1)
        step_result: dict[str, Any] = {"step": step_num, "type": step_type}

        if step_type == "action":
            action_id = step.get("action_id", "")
            action = _definition_resource(
                db,
                workflow,
                kind="action",
                resource_id=str(action_id),
                runtime_definition=runtime_definition,
            )
            if not action:
                step_result["status"] = "skipped"
                step_result["error"] = f"操作不存在: {action_id}"
            else:
                step_params = {**params, **step.get("params", {})}
                r = execute_action(
                    db,
                    action,
                    step_params,
                    confirm=True,
                    idempotency_key=f"workflow:{execution_id}:step:{step_num}",
                    enforce_policy=True,
                    runtime_environment=runtime_environment,
                    runtime_definition=runtime_definition,
                )
                step_result["status"], step_error = _workflow_action_status(r)
                step_result["log_id"] = r.get("log_id")
                step_result["result"] = r.get("result", {})
                step_result["connector_audit"] = r.get("connector_audit", [])
                if step_error:
                    step_result["error"] = step_error
                context[f"step_{step_num}"] = r.get("result", {})

        elif step_type == "rule":
            rule_id = step.get("rule_id", "")
            rule = _definition_resource(
                db,
                workflow,
                kind="rule",
                resource_id=str(rule_id),
                runtime_definition=runtime_definition,
            )
            if not rule:
                step_result["status"] = "skipped"
                step_result["error"] = f"规则不存在: {rule_id}"
            else:
                record = step.get("record", context.get("record", {}))
                r = evaluate_rule(rule, record)
                step_result["status"] = "matched" if r["matched"] else "not_matched"
                step_result["result"] = r
                context[f"step_{step_num}"] = r

        elif step_type == "event":
            event_id = step.get("event_id", "")
            event = _definition_resource(
                db,
                workflow,
                kind="event",
                resource_id=str(event_id),
                runtime_definition=runtime_definition,
            )
            if not event:
                step_result["status"] = "failed"
                step_result["error"] = f"事件不存在或不属于当前业务场景: {event_id}"
            else:
                from .operations_service import publish_event

                payload = render_template(step.get("payload", {}) or {}, context)
                envelope, queued_runs = publish_event(
                    db,
                    event,
                    payload if isinstance(payload, dict) else {"value": payload},
                    source="workflow",
                    source_run_id=source_run_id,
                    dedupe_key=f"workflow:{execution_id}:step:{step_num}",
                    created_by_user_id=str(db.info.get("user_id") or "") or None,
                    runtime_definition=runtime_definition,
                )
                step_result["status"] = "published"
                step_result["result"] = {
                    "event_id": event_id,
                    "payload": payload,
                    "envelope_id": envelope.id,
                    "queued_workflow_run_ids": [run.id for run in queued_runs],
                }
                context[f"step_{step_num}"] = step_result["result"]

        elif step_type == "approval":
            node_id = str(step.get("id") or f"step-{step_num}")
            if node_id in approved_node_ids:
                step_result["status"] = "approved"
                step_result["result"] = {"node_id": node_id}
            else:
                step_result["status"] = "awaiting_approval"
                step_result["result"] = {
                    "node_id": node_id,
                    "instructions": step.get("instructions", "请核对影响范围后决定是否批准。"),
                    "timeout_seconds": step.get("timeout_seconds"),
                    "on_timeout": step.get("on_timeout", "reject"),
                }
                step_results.append(step_result)
                break

        else:
            step_result["status"] = "skipped"
            step_result["error"] = f"未知步骤类型: {step_type}"

        step_results.append(step_result)
        # 失败后不应继续产生后续副作用；审批节点在上方已显式暂停。
        if step_result.get("status") == "failed":
            break
    return step_results


# ── 可视化 DAG 执行 ──
def _execute_dag(
    db: Session,
    workflow: Any,
    params: dict[str, Any],
    *,
    execution_id: str,
    approved_node_ids: set[str],
    attempt: int,
    source_run_id: str | None,
    runtime_environment: str | None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None,
    deadline_at: datetime | None,
) -> list[dict[str, Any]]:
    """按 DAG 拓扑执行：start → 各节点 → end。

    节点类型: start / end / action / rule / llm / event / approval。
    原生 HTTP/Python 节点仅能在受控部署显式启用，默认要求通过类型化 Action。
    边 label: true / false（规则分支），空 = 顺序
    上下文: ctx["params"] = 入参；ctx[node_id] = 节点输出（result/output/matched）
    """
    nodes: dict[str, dict] = {n["id"]: n for n in workflow.nodes if n.get("id")}
    edges: list[dict] = workflow.edges or []
    out_map: dict[tuple[str, str], list[dict]] = {}
    for e in edges:
        out_map.setdefault((e.get("source", ""), e.get("label", "")), []).append(e)

    def outs(node_id: str, label: str = "") -> list[str]:
        return [e.get("target", "") for e in out_map.get((node_id, label), []) if e.get("target") in nodes]

    ctx: dict[str, Any] = {"params": params}
    results: list[dict[str, Any]] = []
    visited: set[str] = set()
    halted = False

    def run(node_id: str) -> None:
        nonlocal halted
        if halted or node_id in visited:
            return
        _check_deadline(deadline_at)
        visited.add(node_id)
        node = nodes[node_id]
        ntype = node.get("type", "")
        data = node.get("data", {}) or {}
        res: dict[str, Any] = {"node": node_id, "name": data.get("name", ""), "type": ntype}

        if ntype == "start":
            res["status"] = "success"
            res["result"] = params
            ctx[node_id] = params

        elif ntype == "end":
            res["status"] = "success"
            res["result"] = {"summary": render_template(data.get("summary", ""), ctx)}

        elif ntype == "action":
            action = _definition_resource(
                db,
                workflow,
                kind="action",
                resource_id=str(data.get("action_id", "")),
                runtime_definition=runtime_definition,
            )
            if not action:
                res["status"] = "failed"
                res["error"] = f"操作不存在: {data.get('action_id', '')}"
            else:
                step_params = render_template(data.get("params", {}) or {}, ctx)
                r = execute_action(
                    db,
                    action,
                    step_params,
                    confirm=True,
                    idempotency_key=f"workflow:{execution_id}:node:{node_id}",
                    enforce_policy=True,
                    runtime_environment=runtime_environment,
                    runtime_definition=runtime_definition,
                )
                res["status"], action_error = _workflow_action_status(r)
                res["log_id"] = r.get("log_id")
                res["result"] = _wrap_out(r.get("result", {}))
                res["error"] = action_error or r.get("error")
                res["connector_audit"] = r.get("connector_audit", [])
                ctx[node_id] = res["result"]

        elif ntype == "rule":
            rule = _definition_resource(
                db,
                workflow,
                kind="rule",
                resource_id=str(data.get("rule_id", "")),
                runtime_definition=runtime_definition,
            )
            if not rule:
                res["status"] = "failed"
                res["error"] = f"规则不存在或不属于当前业务场景: {data.get('rule_id', '')}"
            else:
                record = render_template(data.get("record", {}) or {}, ctx)
                if not isinstance(record, dict):
                    record = {"value": record}
                r = evaluate_rule(rule, record)
                res["status"] = "matched" if r["matched"] else "not_matched"
                res["result"] = r
                ctx[node_id] = _wrap_out(r)
                results.append(res)
                # 分支：命中走 true 边，未命中走 false 边
                branch = "true" if r["matched"] else "false"
                for t in outs(node_id, branch):
                    run(t)
                    if halted:
                        break
                return

        elif ntype == "llm":
            llm = None
            try:
                resolved_environment = runtime_connector_service.runtime_environment(runtime_environment)
                has_runtime_config = any(
                    key in data
                    for key in ("llm_config_id", "llm_binding_key", "llm_binding_ref")
                )
                if has_runtime_config or resolved_environment != "dev":
                    scenario = db.get(BusinessScenario, workflow.scenario_id)
                    if not scenario:
                        raise PolicyViolation("工作流所属业务场景不存在")
                    llm, audit = runtime_connector_service.resolve_connector(
                        db,
                        scenario,
                        kind="llm",
                        config=data,
                        environment=resolved_environment,
                        release_id=(runtime_definition.release_id if runtime_definition else None),
                    )
                    res["connector_audit"] = [audit]
                else:
                    llm = _resolve_llm(db, data.get("llm_config_id"))
            except runtime_connector_service.RuntimeConnectorError as exc:
                res["status"] = "failed"
                res["error"] = str(exc)
            if not llm:
                if res.get("status") != "failed":
                    res["status"] = "failed"
                    res["error"] = "未找到可用 LLM 配置（请先在 LLM 配置中设置默认模型）"
            else:
                prompt = render_template(data.get("prompt", ""), ctx)
                system = data.get("system", "你是一个严谨的业务助手。")
                try:
                    resp = llm_service.chat(
                        llm,
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": str(prompt)},
                        ],
                        temperature=0.3,
                        db=db,
                    )
                    content = resp.get("content", "")
                    res["status"] = "success"
                    res["result"] = {"result": content, "parsed": _try_parse_json(content)}
                    ctx[node_id] = res["result"]
                except Exception as exc:  # noqa: BLE001
                    res["status"] = "failed"
                    res["error"] = str(exc)

        elif ntype == "event":
            event_id = data.get("event_id", "")
            event = _definition_resource(
                db,
                workflow,
                kind="event",
                resource_id=str(event_id),
                runtime_definition=runtime_definition,
            )
            if not event:
                res["status"] = "failed"
                res["error"] = f"事件不存在或不属于当前业务场景: {event_id}"
            else:
                payload = render_template(data.get("payload", {}) or {}, ctx)
                from .operations_service import publish_event

                envelope, queued_runs = publish_event(
                    db,
                    event,
                    payload if isinstance(payload, dict) else {"value": payload},
                    source="workflow",
                    source_run_id=source_run_id,
                    dedupe_key=f"workflow:{execution_id}:node:{node_id}",
                    created_by_user_id=str(db.info.get("user_id") or "") or None,
                    runtime_definition=runtime_definition,
                )
                res["status"] = "published"
                res["result"] = {
                    "result": payload,
                    "event_id": event_id,
                    "envelope_id": envelope.id,
                    "queued_workflow_run_ids": [run.id for run in queued_runs],
                }
                ctx[node_id] = res["result"]

        elif ntype == "approval":
            if node_id in approved_node_ids:
                res["status"] = "approved"
                res["result"] = {"node_id": node_id}
                ctx[node_id] = res["result"]
            else:
                res["status"] = "awaiting_approval"
                res["result"] = {
                    "node_id": node_id,
                    "instructions": data.get("instructions", "请核对影响范围后决定是否批准。"),
                    "timeout_seconds": data.get("timeout_seconds"),
                    "on_timeout": data.get("on_timeout", "reject"),
                }
                results.append(res)
                # 审批是流程暂停点；不能让父节点或后续分支继续运行。
                halted = True
                return

        elif ntype == "http":
            cfg = {
                "method": data.get("method", "GET"),
                "url": render_template(data.get("url", ""), ctx),
                "headers": data.get("headers", {}) or {},
            }
            try:
                out = _exec_http(cfg, render_template(data.get("body", {}) or {}, ctx))
                res["status"] = "success"
                res["result"] = _wrap_out(out)
                ctx[node_id] = res["result"]
            except Exception as exc:  # noqa: BLE001
                res["status"] = "failed"
                res["error"] = str(exc)

        elif ntype == "script":
            script = render_template(data.get("script", ""), ctx)
            try:
                if not get_settings().allow_unsafe_workflow_nodes:
                    raise PolicyViolation("脚本节点默认被禁用，请在受控环境中显式开启")
                namespace: dict[str, Any] = {
                    "params": params,
                    "ctx": ctx,
                    "json": json,
                }
                # 将各节点输出暴露为变量：n1 / n2 ...（支持 n1.result 属性访问）
                for nid_, nout in ctx.items():
                    if nid_ == "params" or nid_ == node_id:
                        continue
                    namespace[nid_] = _NodeOut(nout if isinstance(nout, dict) else {"result": nout})
                exec(script, namespace)  # noqa: S102
                out = namespace.get("result", namespace.get("output", ""))
                res["status"] = "success"
                res["result"] = _wrap_out(out)
                ctx[node_id] = res["result"]
            except Exception as exc:  # noqa: BLE001
                res["status"] = "failed"
                res["error"] = str(exc)

        else:
            res["status"] = "skipped"
            res["error"] = f"未知节点类型: {ntype}"

        results.append(res)
        # 对失败和审批都采用 fail/stop-fast 语义，避免错误路径继续执行副作用节点。
        if res.get("status") in {"failed", "awaiting_approval"}:
            halted = True
            return
        # 顺序边（label 为空）
        for t in outs(node_id, ""):
            run(t)
            if halted:
                break

    # 从 start 出发
    start_ids = [nid for nid, n in nodes.items() if n.get("type") == "start"]
    if not start_ids:
        raise ValueError("工作流缺少开始节点")
    for sid_ in start_ids:
        run(sid_)

    return results


def _resolve_llm(db: Session, llm_config_id: str | None) -> LLMConfig | None:
    if llm_config_id:
        if db.info.get("tenant_id"):
            return tenant_service.get_visible(db, LLMConfig, llm_config_id)
        return db.get(LLMConfig, llm_config_id)
    if db.info.get("tenant_id"):
        candidates = llm_service.routable_configs(db, "chat")
        return candidates[0] if candidates else None
    return db.execute(
        select(LLMConfig).where(LLMConfig.is_default == True, LLMConfig.enabled == True)  # noqa: E712
    ).scalars().first()


def _try_parse_json(text: str) -> Any:
    """LLM 输出若为 JSON 则解析，否则返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.S)
    if m:
        text = m.group(1)
    elif text[0] not in "{[":
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        else:
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", text))
        except json.JSONDecodeError:
            return None


# ──────────────────────────────────────────────
# AI 生成可视化工作流（DAG 草稿，不落库）
# ──────────────────────────────────────────────
_WF_GEN_PROMPT = """你是资深业务流程架构师，擅长把业务描述编排成可视化工作流（DAG）。
请根据下面的业务描述，设计一个简洁、可执行的工作流。

可用节点类型（type）：
- start：开始节点，必须有且仅有 1 个，data 可为空 {}
- end：结束节点，data 可含 summary（结束摘要，支持 {{n1.result}} 变量）
- action：执行已定义的操作，data: {"name":"节点名","action_id":"<操作ID>","params":{}}
- rule：规则判断（分支节点），data: {"name":"节点名","rule_id":"<规则ID>","record":{}}
  命中走 label="true" 的边，未命中走 label="false" 的边
- llm：调用大模型，data: {"name":"节点名","prompt":"提示词，可用 {{params.x}} / {{n1.result}} 变量","system":"系统提示(可选)"}
- event：发布事件，data: {"name":"节点名","event_id":"<事件ID>","payload":{}}

要求：
1. 节点 id 用 n1、n2、n3…（start 节点 id 固定为 "start"，end 节点 id 固定为 "end"）。
2. 每个 action/rule/llm/event/approval 节点必须配置 name（中文节点名）。
3. 只能引用下面列出的操作/规则/事件 ID；涉及外部副作用时必须使用类型化 Action。
4. 连线 edges: [{"id":"e1","source":"start","target":"n1","label":""}]，分支节点必须同时给出 true 和 false 两条出边。
5. 流程必须从 start 出发并最终到达 end。
6. 只输出 JSON，不要输出任何解释文字。

输出格式（严格 JSON）：
{
  "name": "工作流名称",
  "description": "一句话描述",
  "nodes": [
    {"id":"start","type":"start","name":"开始","data":{}},
    {"id":"n1","type":"action","name":"查询违规数据","data":{"action_id":"...","params":{}}},
    {"id":"n2","type":"rule","name":"是否命中规则","data":{"rule_id":"...","record":{"数量":"{{n1.result.rows.0.数量}}"}}},
    {"id":"end","type":"end","name":"结束","data":{"summary":"流程完成"}}
  ],
  "edges": [
    {"id":"e1","source":"start","target":"n1","label":""},
    {"id":"e2","source":"n1","target":"n2","label":""},
    {"id":"e3","source":"n2","target":"n3","label":"true"},
    {"id":"e4","source":"n2","target":"end","label":"false"}
  ]
}

可用操作（Actions）：
{actions}

可用规则（Rules）：
{rules}

可用事件（Events）：
{events}

业务描述：
{description}
"""


def generate_workflow(db: Session, scenario: BusinessScenario, description: str) -> dict[str, Any]:
    """调用 LLM 生成可视化工作流草稿（DAG 节点+连线，不落库）。"""
    from ..models import OntologyEvent

    llm = tenant_service.get_visible(db, LLMConfig, scenario.llm_config_id) if getattr(scenario, "llm_config_id", None) and db.info.get("tenant_id") else None
    if not llm:
        if db.info.get("tenant_id"):
            candidates = llm_service.routable_configs(db, "chat")
            llm = candidates[0] if candidates else None
        else:
            llm = db.execute(
                select(LLMConfig).where(LLMConfig.is_default == True, LLMConfig.enabled == True)  # noqa: E712
            ).scalars().first()
    if not llm:
        raise ValueError("请先在「LLM 配置」中配置并启用一个默认模型")

    actions = db.execute(select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)).scalars().all()
    rules = db.execute(select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)).scalars().all()
    events = db.execute(select(OntologyEvent).where(OntologyEvent.scenario_id == scenario.id)).scalars().all()

    def _fmt(items: list) -> str:
        if not items:
            return "（暂无）"
        return "\n".join(f"- {x.id}: {x.name}" for x in items[:30])

    prompt = (
        _WF_GEN_PROMPT.replace("{actions}", _fmt(actions))
        .replace("{rules}", _fmt(rules))
        .replace("{events}", _fmt(events))
        .replace("{description}", (description or scenario.description or "")[:3000])
    )

    from .ontology_service import _extract_json

    last_err: Exception | None = None
    data: dict[str, Any] = {}
    for _ in range(3):
        resp = llm_service.chat(
            llm,
            [
                {"role": "system", "content": "你只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
            db=db,
        )
        try:
            data = _extract_json(resp.get("content", ""))
            if data.get("nodes"):
                break
            last_err = ValueError("AI 未返回有效节点")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    else:
        raise ValueError(f"AI 多次生成均失败: {last_err}")

    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not nodes:
        raise ValueError("AI 未返回有效节点，请补充业务描述后重试")

    # 规范化：补 id / position / data
    node_ids: set[str] = set()
    for i, n in enumerate(nodes):
        nid = str(n.get("id") or f"n{i + 1}")
        ntype = str(n.get("type") or "action")
        if ntype == "start":
            nid = "start"
        elif ntype == "end":
            nid = "end"
        n["id"] = nid
        n["type"] = ntype
        n["name"] = str(n.get("name") or ntype)
        n["data"] = n.get("data") or {}
        n["position"] = n.get("position") or {"x": 0, "y": 0}
        node_ids.add(nid)

    # 过滤悬空边
    edges = [e for e in edges if e.get("source") in node_ids and e.get("target") in node_ids]
    for i, e in enumerate(edges):
        e["id"] = str(e.get("id") or f"e{i + 1}")
        e.setdefault("label", "")

    return {
        "name": str(data.get("name") or "AI 生成工作流"),
        "description": str(data.get("description") or ""),
        "nodes": nodes,
        "edges": edges,
    }
