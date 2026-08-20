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

import json
import re
import time
from typing import Any

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
    OntologyRule,
    OntologyWorkflow,
)
from . import datasource_service, skill_service, mcp_service, llm_service, tenant_service
from .policies import PolicyViolation, validate_action_params, validate_workflow_graph


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
def _permission_summary(action: OntologyAction, confirmed: bool) -> dict[str, Any]:
    """返回给 UI/审计日志的统一权限判定，不把权限判断留给前端。"""
    scope = action.permission_scope or "scenario"
    return {
        "allowed": scope == "scenario",
        "scope": scope,
        "requires_confirmation": bool(action.requires_confirmation),
        "confirmed": confirmed,
        "reason": "当前用户拥有业务场景的写入权限" if scope == "scenario" else "不支持的权限范围",
    }


def _action_plan(action: OntologyAction, params: dict[str, Any]) -> dict[str, Any]:
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
        plan["data_source_id"] = config.get("data_source_id", "")
        plan["sql_template"] = str(config.get("sql", ""))[:2000]
    elif action.executor_type == "http":
        plan["method"] = str(config.get("method", "GET")).upper()
        plan["url"] = str(config.get("url", ""))[:500]
    elif action.executor_type in {"skill", "mcp", "script"}:
        plan["target"] = str(
            config.get("tool_name") or config.get("skill_name") or config.get("script") or ""
        )
    return plan


def _response_from_log(log: ActionExecutionLog, status: str | None = None) -> dict[str, Any]:
    return {
        "log_id": log.id,
        "status": status or log.status,
        "result": log.result or {},
        "error": log.error or "",
        "duration_ms": log.duration_ms or 0,
        "idempotency_key": log.idempotency_key,
        "permission": {"allowed": True, "scope": "scenario", "confirmed": True},
    }


def _find_idempotent_log(db: Session, action: OntologyAction, key: str) -> ActionExecutionLog | None:
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


def preview_action(db: Session, action: OntologyAction, params: dict[str, Any]) -> dict[str, Any]:
    """校验参数并生成 Action 预演，不触发 SQL/HTTP/脚本/MCP/Skill。"""
    if not action.enabled:
        raise PolicyViolation("操作已禁用")
    normalized = validate_action_params(action.input_schema or {}, params)
    plan = _action_plan(action, normalized)
    permission = _permission_summary(action, confirmed=False)
    if not permission["allowed"]:
        raise PolicyViolation("操作权限范围不受支持")
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
        duration_ms=int((time.time() - start) * 1000),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return {
        "log_id": log.id,
        "status": "dry_run",
        "result": log.result,
        "error": "",
        "duration_ms": log.duration_ms,
        "requires_confirmation": bool(action.requires_confirmation),
        "permission": permission,
        "idempotency_key": None,
    }


def execute_action(
    db: Session,
    action: OntologyAction,
    params: dict[str, Any],
    *,
    confirm: bool = True,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    enforce_policy: bool = True,
) -> dict[str, Any]:
    """执行单个操作，统一完成参数校验、权限确认和幂等日志。"""
    if dry_run:
        return preview_action(db, action, params)
    if not action.enabled:
        raise PolicyViolation("操作已禁用")
    normalized = validate_action_params(action.input_schema or {}, params)
    permission = _permission_summary(action, confirmed=confirm)
    if not permission["allowed"]:
        raise PolicyViolation("操作权限范围不受支持")

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

    if enforce_policy and idempotency_key:
        existing = _find_idempotent_log(db, action, idempotency_key)
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
        idempotency_key=idempotency_key,
    )
    db.add(log)
    # 先提交 running 占位记录，再调用外部执行器；这样并发请求会在副作用前竞争同一幂等键。
    if enforce_policy and idempotency_key:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _find_idempotent_log(db, action, idempotency_key)
            if existing:
                return _idempotent_replay(existing, normalized, permission)
            raise
        db.refresh(log)
    else:
        db.flush()

    try:
        result = _dispatch_executor(db, action, normalized)
        log.status = "success"
        log.result = result if isinstance(result, dict) else {"output": str(result)[:2000]}
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


def _dispatch_executor(db: Session, action: OntologyAction, params: dict[str, Any]) -> Any:
    """按 executor_type 分发到具体执行器。"""
    etype = action.executor_type
    cfg = action.executor_config or {}

    if etype == "sql":
        return _exec_sql(db, {**cfg, "scenario_id": action.scenario_id}, params)
    if etype == "skill":
        return _exec_skill(db, cfg, params)
    if etype == "mcp":
        return _exec_mcp(db, cfg, params)
    if etype == "http":
        return _exec_http(cfg, params)
    if etype == "script":
        return _exec_script(cfg, params)
    raise ValueError(f"未知执行器类型: {etype}")


def _exec_sql(db: Session, cfg: dict, params: dict) -> Any:
    """SQL 执行器：在指定数据源上执行 SQL。

    cfg: {data_source_id, sql}
    params 中的值会替换 SQL 中的 {param_name} 占位符。
    """
    ds_id = cfg.get("data_source_id", "")
    sql = cfg.get("sql", "")
    if not ds_id or not sql:
        raise ValueError("SQL 执行器需要 data_source_id 和 sql 配置")
    # 参数替换
    for k, v in params.items():
        sql = sql.replace("{%s}" % k, str(v))
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise ValueError(f"数据源不存在: {ds_id}")
    if ds.scenario_id not in (None, cfg.get("scenario_id")) and cfg.get("scenario_id"):
        raise PolicyViolation("操作不能访问其他业务场景的数据源")
    return datasource_service.run_query(ds, sql, limit=get_settings().max_query_rows)


def _exec_skill(db: Session, cfg: dict, params: dict) -> Any:
    """技能执行器：调用已安装技能。

    cfg: {skill_name, skill_path, script, interpreter}
    params: 命令行参数（list 或 dict）
    """
    import os
    import subprocess

    skill_name = cfg.get("skill_name", "")
    skill_path = cfg.get("skill_path", "")
    if not skill_name or not skill_path:
        raise ValueError("技能执行器需要 skill_name 和 skill_path 配置")
    args = params.get("args", [])
    if isinstance(args, dict):
        args = [str(v) for v in args.values()]
    cmd = [
        cfg.get("interpreter", "python"),
        os.path.join(skill_path, cfg.get("script", "main.py")),
    ] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return {"stdout": r.stdout[:3000], "stderr": r.stderr[:1000], "exit_code": r.returncode}


def _exec_mcp(db: Session, cfg: dict, params: dict) -> Any:
    """MCP 执行器：调用 MCP 工具。

    cfg: {mcp_id, tool_name}
    """
    mcp_id = cfg.get("mcp_id", "")
    tool_name = cfg.get("tool_name", "")
    if not mcp_id or not tool_name:
        raise ValueError("MCP 执行器需要 mcp_id 和 tool_name 配置")
    from ..models import MCPConfig

    mcp = db.get(MCPConfig, mcp_id)
    if not mcp:
        raise ValueError(f"MCP 不存在: {mcp_id}")
    return mcp_service.call_tool(mcp, tool_name, params)


def _exec_http(cfg: dict, params: dict) -> Any:
    """HTTP 执行器：发送 HTTP 请求。

    cfg: {method, url, headers}
    params: 请求体/查询参数
    """
    import urllib.request
    import urllib.parse

    method = cfg.get("method", "GET").upper()
    url = cfg.get("url", "")
    headers = cfg.get("headers", {})
    if not url:
        raise ValueError("HTTP 执行器需要 url 配置")
    # 参数替换 URL
    for k, v in params.items():
        url = url.replace("{%s}" % k, str(v))
    data = None
    if method in ("POST", "PUT", "PATCH"):
        body = params.get("body", params)
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
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
def execute_workflow(db: Session, workflow: OntologyWorkflow, params: dict[str, Any]) -> dict[str, Any]:
    """执行工作流：优先可视化 DAG（nodes/edges），回退旧版线性 steps。"""
    start = time.time()
    log = ActionExecutionLog(
        scenario_id=workflow.scenario_id,
        target_type="workflow",
        target_id=workflow.id,
        target_name=workflow.name,
        input_params=params,
        status="running",
    )
    db.add(log)
    db.flush()

    try:
        if workflow.nodes:
            validate_workflow_graph(workflow.nodes, workflow.edges or [])
        if workflow.nodes:
            step_results = _execute_dag(db, workflow, params, execution_id=log.id)
        else:
            step_results = _execute_steps(db, workflow, params, execution_id=log.id)
        log.result = {"steps": step_results}
        failed = next((step for step in step_results if step.get("status") == "failed"), None)
        if failed:
            log.status = "failed"
            log.error = failed.get("error") or "工作流节点执行失败"
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
        "error": log.error,
        "duration_ms": log.duration_ms,
    }


# ── 旧版线性 steps（兼容）──
def _execute_steps(
    db: Session,
    workflow: OntologyWorkflow,
    params: dict[str, Any],
    *,
    execution_id: str,
) -> list[dict[str, Any]]:
    step_results: list[dict[str, Any]] = []
    context: dict[str, Any] = {"params": params}

    for i, step in enumerate(workflow.steps or []):
        step_type = step.get("type", "")
        step_num = step.get("step", i + 1)
        step_result: dict[str, Any] = {"step": step_num, "type": step_type}

        if step_type == "action":
            action_id = step.get("action_id", "")
            action = db.get(OntologyAction, action_id)
            if action and action.scenario_id != workflow.scenario_id:
                action = None
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
                )
                step_result["status"] = r["status"]
                step_result["result"] = r.get("result", {})
                context[f"step_{step_num}"] = r.get("result", {})

        elif step_type == "rule":
            rule_id = step.get("rule_id", "")
            rule = db.get(OntologyRule, rule_id)
            if rule and rule.scenario_id != workflow.scenario_id:
                rule = None
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
            event = db.get(OntologyEvent, event_id)
            if not event or event.scenario_id != workflow.scenario_id:
                step_result["status"] = "failed"
                step_result["error"] = f"事件不存在或不属于当前业务场景: {event_id}"
            else:
                step_result["status"] = "published"
                step_result["result"] = {"event_id": event_id, "payload": step.get("payload", {})}
                context[f"step_{step_num}"] = step_result["result"]

        else:
            step_result["status"] = "skipped"
            step_result["error"] = f"未知步骤类型: {step_type}"

        step_results.append(step_result)
    return step_results


# ── 可视化 DAG 执行 ──
def _execute_dag(
    db: Session,
    workflow: OntologyWorkflow,
    params: dict[str, Any],
    *,
    execution_id: str,
) -> list[dict[str, Any]]:
    """按 DAG 拓扑执行：start → 各节点 → end。

    节点类型: start / end / action / rule / llm / event / http / script
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

    def run(node_id: str) -> None:
        if node_id in visited:
            return
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
            action = db.get(OntologyAction, data.get("action_id", ""))
            if action and action.scenario_id != workflow.scenario_id:
                action = None
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
                )
                res["status"] = r["status"]
                res["result"] = _wrap_out(r.get("result", {}))
                res["error"] = r.get("error")
                ctx[node_id] = res["result"]

        elif ntype == "rule":
            rule = db.get(OntologyRule, data.get("rule_id", ""))
            if rule and rule.scenario_id != workflow.scenario_id:
                rule = None
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
                return

        elif ntype == "llm":
            llm = _resolve_llm(db, data.get("llm_config_id"))
            if not llm:
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
            event = db.get(OntologyEvent, event_id)
            if not event or event.scenario_id != workflow.scenario_id:
                res["status"] = "failed"
                res["error"] = f"事件不存在或不属于当前业务场景: {event_id}"
            else:
                payload = render_template(data.get("payload", {}) or {}, ctx)
                res["status"] = "published"
                res["result"] = {"result": payload, "event_id": event_id}
                ctx[node_id] = res["result"]

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
        # 顺序边（label 为空）
        for t in outs(node_id, ""):
            run(t)

    # 从 start 出发
    start_ids = [nid for nid, n in nodes.items() if n.get("type") == "start"]
    if not start_ids:
        raise ValueError("工作流缺少开始节点")
    for sid_ in start_ids:
        run(sid_)

    # 未连通的孤立节点也执行，便于调试
    for nid in nodes:
        if nid not in visited:
            run(nid)

    return results


def _resolve_llm(db: Session, llm_config_id: str | None) -> LLMConfig | None:
    if llm_config_id:
        if db.info.get("tenant_id"):
            return tenant_service.get_visible(db, LLMConfig, llm_config_id)
        return db.get(LLMConfig, llm_config_id)
    stmt = select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
    if db.info.get("tenant_id"):
        stmt = stmt.where(tenant_service.visible_clause(LLMConfig, db))
    return db.execute(stmt.limit(1)).scalars().first()


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
- http：HTTP 请求，data: {"name":"节点名","method":"GET","url":"..."}
- script：Python 脚本，data: {"name":"节点名","script":"result = ..."}

要求：
1. 节点 id 用 n1、n2、n3…（start 节点 id 固定为 "start"，end 节点 id 固定为 "end"）。
2. 每个 action/rule/llm/event/http/script 节点必须配置 name（中文节点名）。
3. 只能引用下面列出的操作/规则/事件 ID；若没有合适的操作，可用 llm/script/http 节点代替。
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
        llm_stmt = select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        if db.info.get("tenant_id"):
            llm_stmt = llm_stmt.where(tenant_service.visible_clause(LLMConfig, db))
        llm = db.execute(llm_stmt.limit(1)).scalars().first()
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
