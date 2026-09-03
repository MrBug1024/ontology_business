"""Agent 引擎：基于工具调用（ReAct）循环，让 LLM 在业务场景下自主完成需求。

可用工具：
- list_ontology_model 读取对象类型、属性和关系类型
- search_ontology     检索当前 Agent 数据范围内的对象实例
- get_ontology_object 读取对象属性和关系实例
- list_data_sources   列出 Agent 绑定的数据源
- list_data_mappings  读取对象属性与源字段的映射
- query_mapped_objects 按本体属性执行确定性的参数化映射查询
- list_tables         列出某数据源的表结构
- query_business_data 按本体对象和属性执行跨表、分组和聚合业务查询
- run_medical_audit  在医保审计场景执行版本化、参数化的确定性审计策略
- search_documents    在文件桶中检索相关文档片段（RAG）
- read_document       读取某个已解析文档的全文
- list_functions      列出场景中的无副作用业务函数
- run_function        调用确定性业务函数
- list_actions        发现操作；按操作的人工确认配置执行或生成预演
- list_rules          发现规则，evaluate_rule 只做无副作用判定
- list_events         发现事件，prepare_event_publish 只准备确认清单
- list_workflows      发现工作流，execute_workflow 只准备确认清单

Agent 只会通过已配置的类型化 Action 产生副作用。启用人工确认的 Action 必须由
用户在同一对话中回复明确的“确认执行”后继续；未启用人工确认的 Action 可由 Agent
按当前定义和幂等策略直接执行。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from collections import defaultdict, deque
from types import SimpleNamespace
from typing import Any, Iterator, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Agent,
    BusinessScenario,
    BucketFile,
    DataMapping,
    DataSource,
    FunctionDefinition,
    LLMConfig,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    RelationInstance,
)
from . import (
    agent_confirmation_service,
    agent_capability_service,
    capability_readiness_service,
    business_query_service,
    datasource_service,
    function_runtime_service,
    llm_service,
    mapped_query_service,
    medical_audit_service,
    ontology_service,
    permission_service,
    rag_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
    workflow_service,
)
from .policies import (
    PolicyViolation,
    validate_action_params,
    validate_agent_sql_scope,
    validate_read_only_sql,
)


class _ToolContractError(ValueError):
    """A model-correctable error in the public Agent tool contract."""


_SAFE_TOOL_ERROR_CODES = frozenset(
    {
        "CAPABILITY_NOT_READY",
        "DIRECT_TOOL_DISABLED",
        "FORBIDDEN",
        "FUNCTION_EXECUTION_FAILED",
        "INVALID_QUERY",
        "INVALID_TOOL_ARGUMENTS",
        "RESOURCE_NOT_FOUND",
        "TOOL_EXECUTION_FAILED",
        "TOOL_RESULT_TOO_LARGE",
        "UNKNOWN_TOOL",
    }
)
_MAX_TOOL_RESULT_CHARS = 8_000
_WORKFLOW_PARAM_RE = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_AUDIT_INTENT_TERMS = ("审计", "核验", "核查", "排查", "违规")
_DELIVERY_INTENT_VERBS = ("生成", "完成", "出具", "编制", "交付", "导出", "制作", "产出", "提交")
_DELIVERY_INTENT_NOUNS = (
    "报告",
    "报表",
    "附注",
    "附件",
    "文件",
    "产出物",
    "全部工作任务",
)
_COMPLETE_DETAIL_TERMS = ("全部", "全量", "所有", "完整", "逐条", "明细")
_MEDICAL_STRATEGY_LABELS = {
    "charge_threshold": "单条收费数量阈值",
    "daily_overstay": "日计价超过住院天数",
    "included_service_duplicate": "包含项目重复收费",
    "limited_drug_duration": "限疗程用药",
}
_MEDICAL_STRATEGY_ARGUMENTS = {
    "charge_threshold": ("service_name", "threshold"),
    "daily_overstay": ("service_names",),
    "included_service_duplicate": ("included_service", "duplicate_service"),
    "limited_drug_duration": ("drug_name", "max_days"),
}
_MEDICAL_RECORD_IDENTITY_FIELDS = {
    "charge_threshold": ("charge_line_id",),
    "daily_overstay": ("encounter_id", "service_name"),
    "included_service_duplicate": ("charge_line_id",),
    "limited_drug_duration": ("encounter_id", "drug_name"),
}
_MEDICAL_FACILITY_SUFFIXES = (
    "社区卫生服务站",
    "社区卫生服务中心",
    "疾病预防控制中心",
    "妇幼保健院",
    "医疗中心",
    "急救中心",
    "卫生院",
    "门诊部",
    "医务室",
    "卫生室",
    "护理院",
    "疗养院",
    "大药房",
    "检验所",
    "中医馆",
    "诊所",
    "服务站",
    "药房",
    "药店",
    "医院",
)
_MEDICAL_FACILITY_SUFFIX_PATTERN = re.compile(
    r"[0-9A-Za-z\u4e00-\u9fff·._/-]{1,60}?(?:"
    + "|".join(re.escape(value) for value in _MEDICAL_FACILITY_SUFFIXES)
    + r")",
    flags=re.IGNORECASE,
)
_MEDICAL_FACILITY_SCOPE_INTRODUCERS = (
    "审计对象是",
    "审计对象为",
    "医疗机构是",
    "医疗机构为",
    "机构名称是",
    "机构名称为",
    "范围限定为",
    "范围为",
    "仅针对",
    "只针对",
    "项目涉及的",
    "项目中的",
    "项目内的",
    "项目下的",
    "项目的",
    "范围内的",
    "对应的",
    "所属的",
    "对应",
    "涉及的",
    "请审计",
    "完成",
    "审计",
    "核验",
    "核查",
    "排查",
    "检查",
    "查询",
    "分析",
    "针对",
    "关于",
    "对于",
    "对",
    "在",
)
_GENERIC_MEDICAL_FACILITY_PREFIXES = frozenset({
    "",
    "某",
    "某某",
    "一家",
    "这家",
    "该",
    "本",
    "当地",
    "相关",
    "上述",
    "定点",
    "医保定点",
    "医疗",
    "综合",
    "人民",
    "公立",
    "私立",
    "各",
    "所有",
    "全部",
    "任一",
})
_MEDICAL_FACILITY_NEGATIVE_PREFIXES = (
    "不要审计",
    "无需审计",
    "不审计",
    "不包括",
    "不包含",
    "不含",
    "排除",
    "剔除",
    "除",
)


def _safe_message(value: Any, fallback: str) -> str:
    """Keep typed business diagnostics useful without replaying raw internals."""
    message = " ".join(str(value or "").split())
    return message[:500] if message else fallback


def _tool_error(
    code: str,
    message: str,
    *,
    retryable: bool,
) -> str:
    """Return the only error envelope persisted in new Agent tool results."""
    if code not in _SAFE_TOOL_ERROR_CODES:
        code = "TOOL_EXECUTION_FAILED"
        message = "工具执行失败；内部异常未暴露给对话。"
        retryable = False
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": _safe_message(message, "工具执行失败"),
                "retryable": bool(retryable),
            },
        },
        ensure_ascii=False,
    )


def _is_safe_tool_error(value: Any) -> bool:
    """Recognize only the closed, data-free error envelope generated above."""
    if not isinstance(value, dict) or set(value) != {"ok", "error"} or value.get("ok") is not False:
        return False
    error = value.get("error")
    if not isinstance(error, dict) or set(error) != {"code", "message", "retryable"}:
        return False
    code = error.get("code")
    message = error.get("message")
    retryable = error.get("retryable")
    return (
        code in _SAFE_TOOL_ERROR_CODES
        and isinstance(message, str)
        and 0 < len(message) <= 500
        and "\n" not in message
        and "\r" not in message
        and isinstance(retryable, bool)
    )


def _bounded_tool_result(value: Any) -> str:
    """Keep the model-facing and persisted tool result complete and parseable.

    Cutting an arbitrary JSON string at the prompt budget boundary corrupts
    the tool contract and can hide late catalog entries or record fields.  A
    caller can retry an oversized query with narrower filters, or use the
    exact-reference mode exposed by discovery tools such as ``list_actions``.
    """
    result = value if isinstance(value, str) else _dump(value)
    if len(result) <= _MAX_TOOL_RESULT_CHARS:
        return result
    return _tool_error(
        "TOOL_RESULT_TOO_LARGE",
        "工具结果超过单轮安全传递上限；请缩小查询范围，或使用 list_* 工具的精确资源参数后重试。",
        retryable=True,
    )


def _parsed_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _failed_tool_result(value: Any) -> bool:
    parsed = _parsed_result(value)
    return isinstance(parsed, dict) and parsed.get("ok") is False


def _dry_run_action_target(outcome: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the canonical Action identity and its best user-facing label."""
    parsed = _parsed_result(outcome.get("result"))
    if not isinstance(parsed, dict) or str(parsed.get("status") or "") != "dry_run":
        return None
    result = parsed.get("result")
    plan = result.get("plan") if isinstance(result, dict) else None
    arguments = outcome.get("arguments")
    requested = (
        str(arguments.get("action_id") or "").strip()
        if isinstance(arguments, Mapping)
        else ""
    )
    canonical_id = (
        str(plan.get("action_id") or "").strip()
        if isinstance(plan, dict)
        else ""
    )
    identity = canonical_id or requested
    if not identity:
        return None
    label = (
        str(plan.get("action_name") or "").strip()
        if isinstance(plan, dict)
        else ""
    )
    return identity, label


def _executed_action_target(outcome: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return a successfully executed Action from a server-owned tool result."""
    parsed = _parsed_result(outcome.get("result"))
    if not isinstance(parsed, dict):
        return None
    status = str(parsed.get("status") or "")
    if status != "success" and not (
        status == "idempotent_replay"
        and str(parsed.get("original_status") or "") == "success"
    ):
        return None
    arguments = outcome.get("arguments")
    requested = (
        str(arguments.get("action_id") or "").strip()
        if isinstance(arguments, Mapping)
        else ""
    )
    result = parsed.get("result")
    plan = result.get("plan") if isinstance(result, dict) else None
    canonical_id = (
        str(plan.get("action_id") or parsed.get("target_id") or "").strip()
        if isinstance(plan, dict)
        else str(parsed.get("target_id") or "").strip()
    )
    identity = canonical_id or requested
    if not identity:
        return None
    label = (
        str(plan.get("action_name") or parsed.get("target_name") or "").strip()
        if isinstance(plan, dict)
        else str(parsed.get("target_name") or "").strip()
    )
    return identity, label or identity


def _automatic_action_idempotency_key(
    db: Session,
    *,
    action_id: str,
    params: Mapping[str, Any],
) -> str:
    """Derive a retry-safe key for one non-confirmed Agent action invocation."""
    trace = db.info.get("llm_trace_context")
    correlation_id = str(trace.get("correlation_id") or "").strip() if isinstance(trace, dict) else ""
    # Published MCP turns carry a durable replay hash from the service layer.
    # Prefer it over the per-attempt LLM trace so a reclaimed/resent external
    # request cannot dispatch a non-confirmed Action twice after a worker dies.
    mcp_request_hash = str(db.info.get("agent_mcp_turn_request_hash") or "").strip()
    turn_id = mcp_request_hash or correlation_id or uuid.uuid4().hex
    canonical_params = json.dumps(
        dict(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = hashlib.sha256(
        f"{action_id}\x1f{canonical_params}".encode("utf-8")
    ).hexdigest()[:32]
    return f"agent:{turn_id[:32]}:{fingerprint}"


def _assert_agent_turn_lease(db: Session) -> None:
    """Fence a published MCP turn without coupling this module to its adapter.

    Browser Agent chat does not install the guard.  The MCP service supplies a
    small object with ``assert_active`` on the request session; keeping this
    duck-typed prevents an import cycle while making side-effect boundaries
    fail closed after a cross-worker lease takeover.
    """
    guard = db.info.get("agent_mcp_turn_lease_guard")
    assert_active = getattr(guard, "assert_active", None)
    if callable(assert_active):
        assert_active()


def _display_number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return format(float(value), ".12g")


def _medical_record_identity(
    strategy: str,
    record: Any,
) -> tuple[str, ...] | None:
    fields = _MEDICAL_RECORD_IDENTITY_FIELDS.get(strategy)
    if not fields or not isinstance(record, dict):
        return None
    values: list[str] = []
    for field in fields:
        value = record.get(field)
        if value is None or isinstance(value, bool):
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        values.append(normalized)
    return tuple(values)


def _normalized_business_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _requested_medical_facilities(user_message: str) -> set[str]:
    """Extract explicit facility scope without treating business ids as names."""

    facilities: set[str] = set()
    for match in _MEDICAL_FACILITY_SUFFIX_PATTERN.finditer(str(user_message or "")):
        candidate = match.group(0)
        cut_at = 0
        for introducer in _MEDICAL_FACILITY_SCOPE_INTRODUCERS:
            index = candidate.rfind(introducer)
            if index >= 0:
                cut_at = max(cut_at, index + len(introducer))
        candidate = candidate[cut_at:]
        candidate = re.sub(
            r"^(?:(?:请)?帮我|请|麻烦|仅|只|将|把|和|与|及|以及|对|在|的)+",
            "",
            candidate,
        )
        normalized = _normalized_business_text(candidate)
        suffix = next(
            (
                _normalized_business_text(value)
                for value in _MEDICAL_FACILITY_SUFFIXES
                if normalized.endswith(_normalized_business_text(value))
            ),
            "",
        )
        if not suffix:
            continue
        prefix = normalized[: -len(suffix)]
        if (
            prefix in _GENERIC_MEDICAL_FACILITY_PREFIXES
            or re.fullmatch(
                r"(?:各|所有|全部|任一|相关|上述|当地|某|某某|一家|这家|该|本|"
                r"医保|定点|医疗|公立|私立|综合|人民)+",
                prefix,
            )
            or re.fullmatch(
                r"(?:项目)?[a-z]{1,12}\d{1,12}(?:年度|项目|业务|场景|任务|的|涉及的)*",
                prefix,
            )
        ):
            continue
        facilities.add(normalized)
    return facilities


def _resolved_medical_facilities(
    user_message: str,
    authoritative_facilities: Sequence[str] | None = None,
) -> set[str]:
    """Keep distinct governed values while removing heuristic name fragments."""

    heuristic = _requested_medical_facilities(user_message)
    authoritative = {
        normalized
        for value in authoritative_facilities or ()
        if (normalized := _normalized_business_text(value))
    }
    if authoritative:
        return authoritative | {
            candidate
            for candidate in heuristic
            if not any(candidate in governed for governed in authoritative)
        }
    return {
        candidate
        for candidate in heuristic
        if not any(
            candidate != longer and candidate in longer
            for longer in heuristic
        )
    }


def _medical_request_excludes_facility(
    user_message: str,
    authoritative_facilities: Sequence[str] | None = None,
) -> bool:
    """Fail closed when an explicitly named institution is a negative scope.

    The deterministic medical tool only supports equality or all-facility
    scope. It cannot prove a NOT-facility request, even when governed lookup
    correctly recognizes the institution being excluded.
    """

    explicit_facilities = _resolved_medical_facilities(
        user_message,
        authoritative_facilities,
    )
    if not explicit_facilities:
        return False
    normalized_message = _normalized_business_text(user_message)
    normalized_prefixes = tuple(
        _normalized_business_text(prefix)
        for prefix in _MEDICAL_FACILITY_NEGATIVE_PREFIXES
    )
    facility_names: set[str] = set()
    for candidate in explicit_facilities:
        normalized = candidate
        # Heuristic extraction can retain a leading negative verb (for
        # example, ``排除某医院``). Strip only a leading scope operator so the
        # following regexes can bind the operator back to the exact name span.
        for prefix in normalized_prefixes:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                normalized = normalized[len(prefix):]
                break
        if normalized:
            facility_names.add(normalized)

    for facility_name in facility_names:
        facility = re.escape(facility_name)
        patterns = (
            rf"除(?:了)?{facility}(?:以外|之外|外)",
            rf"除(?:了)?{facility}(?=(?:审计|核查|检查|排查|其他|其余|剩余))",
            rf"{facility}除外",
            rf"(?:不含|不包括|不包含){facility}",
            rf"(?:排除|剔除)(?:掉)?{facility}",
            rf"跳过{facility}",
            rf"(?:不要审计|不审计|无需审计){facility}",
            rf"(?:不要|不|无需)(?:对)?{facility}"
            rf"(?:进行|开展)?(?:本次)?审计",
            rf"{facility}(?:不要审计|不审计|无需审计)",
            rf"{facility}(?:暂)?不在(?:本次)?审计范围(?:之)?内",
            rf"{facility}(?:暂)?不纳入(?:本次)?审计",
            rf"(?:本次)?审计不涉及{facility}",
        )
        if any(re.search(pattern, normalized_message) for pattern in patterns):
            return True
    return False


def _chinese_integer(value: int) -> set[str]:
    digits = "零一二三四五六七八九"
    if value < 0 or value > 99:
        return set()
    if value < 10:
        result = {digits[value]}
        if value == 2:
            result.add("两")
        return result
    tens, ones = divmod(value, 10)
    prefix = "" if tens == 1 else digits[tens]
    return {prefix + "十" + (digits[ones] if ones else "")}


def _medical_number_mentioned(user_message: str, value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    raw_user = str(user_message or "").casefold()
    numeric_forms = {format(numeric, ".12g")}
    chinese_forms: set[str] = set()
    if numeric.is_integer():
        integer = int(numeric)
        numeric_forms.add(str(integer))
        chinese_forms.update(_chinese_integer(integer))
    if any(form and form in raw_user for form in chinese_forms):
        return True
    return any(
        re.search(
            rf"(?<![0-9a-z]){re.escape(form)}(?![0-9a-z])",
            raw_user,
        )
        is not None
        for form in numeric_forms
        if form
    )


def _requested_medical_strategy(user_message: str) -> str | None:
    text = _normalized_business_text(user_message)
    if any(term in text for term in ("重复收费", "重复收取", "另行收费", "包含项目", "已包含")):
        return "included_service_duplicate"
    if any(term in text for term in ("限疗程", "超疗程", "用药天数", "用药时长", "疗程天数")):
        return "limited_drug_duration"
    if any(term in text for term in ("住院天数", "日计价", "按日计费", "每日计费")):
        return "daily_overstay"
    if (
        any(term in text for term in ("大于", "高于", "超过", "超出"))
        and any(term in text for term in ("收费", "数量", "次数", "次"))
    ):
        return "charge_threshold"
    return None


def _medical_request_matches_user(
    strategy: str,
    arguments: Any,
    evidence: Mapping[str, Any],
    *,
    user_message: str,
    requested_facilities: set[str] | None = None,
    authoritative_facilities: set[str] | None = None,
) -> bool:
    """Bind deterministic medical evidence to the user's stated audit task."""

    if not isinstance(arguments, Mapping) or _requested_medical_strategy(user_message) != strategy:
        return False
    parameters = evidence.get("parameters")
    if not isinstance(parameters, Mapping):
        return False
    expected_parameters: dict[str, Any] = {
        "facility_name": arguments.get("facility_name"),
    }
    for key in _MEDICAL_STRATEGY_ARGUMENTS[strategy]:
        if key not in arguments:
            return False
        expected_parameters[key] = arguments[key]
    if dict(parameters) != expected_parameters:
        return False

    user_text = _normalized_business_text(user_message)
    requested_facilities = (
        _resolved_medical_facilities(user_message)
        if requested_facilities is None
        else requested_facilities
    )
    expected_facility = _normalized_business_text(expected_parameters.get("facility_name"))
    if (
        authoritative_facilities is not None
        and expected_parameters.get("facility_name") is not None
        and (
            not expected_facility
            or expected_facility not in authoritative_facilities
        )
    ):
        # A successful governed lookup is authoritative. Heuristic aliases or
        # unknown names cannot turn an equality query with zero rows into proof.
        return False
    if requested_facilities and (
        len(requested_facilities) != 1
        or not expected_facility
        or requested_facilities != {expected_facility}
    ):
        # A query without facility_name expands beyond an explicitly named
        # institution.  Likewise, one facility-scoped tool result cannot prove
        # a request that named a different or multiple institutions.
        return False
    for key in ("facility_name", "service_name", "included_service", "duplicate_service", "drug_name"):
        value = expected_parameters.get(key)
        if value is not None and _normalized_business_text(value) not in user_text:
            return False
    service_names = expected_parameters.get("service_names")
    if service_names is not None and (
        not isinstance(service_names, list)
        or not service_names
        or any(
            not isinstance(value, str)
            or _normalized_business_text(value) not in user_text
            for value in service_names
        )
    ):
        return False
    for key in ("threshold", "max_days"):
        if key in expected_parameters and not _medical_number_mentioned(
            user_message,
            expected_parameters[key],
        ):
            return False
    return True


def _medical_audit_status(
    tool_outcomes: list[dict[str, Any]],
    *,
    user_message: str,
    authoritative_facilities: Sequence[str] | None = None,
    facility_lookup_succeeded: bool | None = None,
) -> tuple[list[str], bool]:
    """Build deterministic summaries and prove every requested detail page."""
    if facility_lookup_succeeded is False:
        return [], False
    normalized_authoritative_facilities = {
        normalized
        for value in authoritative_facilities or ()
        if (normalized := _normalized_business_text(value))
    }
    if _medical_request_excludes_facility(
        user_message,
        authoritative_facilities,
    ):
        return [], False
    requested_facilities = _resolved_medical_facilities(
        user_message,
        authoritative_facilities,
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    for outcome in tool_outcomes:
        if outcome["name"] != "run_medical_audit" or _failed_tool_result(outcome["result"]):
            continue
        payload = _parsed_result(outcome["result"])
        evidence = payload.get("evidence") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("summary"), dict)
            or not isinstance(payload.get("records"), list)
            or not isinstance(evidence, dict)
        ):
            continue
        strategy = str(payload.get("strategy") or "")
        if strategy not in _MEDICAL_STRATEGY_ARGUMENTS or not _medical_request_matches_user(
            strategy,
            outcome.get("arguments"),
            evidence,
            user_message=user_message,
            requested_facilities=requested_facilities,
            authoritative_facilities=(
                normalized_authoritative_facilities
                if facility_lookup_succeeded is True
                else None
            ),
        ):
            continue
        signature = json.dumps(
            {
                "audit_version": payload.get("audit_version"),
                "strategy": payload.get("strategy"),
                "source_id": evidence.get("source_id"),
                "parameters": evidence.get("parameters"),
                "limit": payload.get("limit"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        groups.setdefault(signature, []).append({**outcome, "payload": payload})

    lines: list[str] = []
    verified = False
    wants_complete_details = any(term in user_message for term in _COMPLETE_DETAIL_TERMS)
    for pages in groups.values():
        first = pages[0]["payload"]
        summary = first["summary"]
        count = summary.get("violation_count")
        amount = _display_number(summary.get("violation_amount"))
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or amount is None:
            continue
        verified = True
        strategy = str(first.get("strategy") or "")
        label = _MEDICAL_STRATEGY_LABELS.get(strategy, strategy or "未知策略")
        summary_parts = [f"{label}：违规 {count} 条（组）", f"违规金额 {amount} 元"]
        for field, field_label in (
            ("violating_quantity", "涉及数量"),
            ("excess_quantity", "超计数量"),
        ):
            displayed = _display_number(summary.get(field))
            if displayed is not None:
                summary_parts.append(f"{field_label} {displayed}")
        lines.append(
            "医保确定性汇总（模型正文数字不一致时以此为准）："
            + "，".join(summary_parts)
            + "。"
        )

        expected_offset = 0
        delivered_rows = 0
        complete = False
        chain_valid = True
        seen_identities: set[tuple[str, ...]] = set()
        baseline_summary = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        baseline_evidence = json.dumps(
            first.get("evidence"), ensure_ascii=False, sort_keys=True, default=str
        )
        for page in pages:
            payload = page["payload"]
            arguments = page.get("arguments") or {}
            offset = payload.get("offset")
            row_count = payload.get("row_count")
            truncated = payload.get("truncated")
            next_offset = payload.get("next_offset")
            try:
                requested_offset = int(arguments.get("offset") or 0)
            except (TypeError, ValueError):
                chain_valid = False
                break
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 0
                or row_count != len(payload["records"])
                or not isinstance(truncated, bool)
                or offset != expected_offset
                or offset != requested_offset
                or json.dumps(payload.get("summary"), ensure_ascii=False, sort_keys=True)
                != baseline_summary
                or json.dumps(
                    payload.get("evidence"), ensure_ascii=False, sort_keys=True, default=str
                )
                != baseline_evidence
            ):
                chain_valid = False
                break
            if "limit" in arguments and payload.get("limit") != arguments.get("limit"):
                chain_valid = False
                break
            page_identities: list[tuple[str, ...]] = []
            for record in payload["records"]:
                identity = _medical_record_identity(strategy, record)
                if identity is None or identity in seen_identities:
                    chain_valid = False
                    break
                seen_identities.add(identity)
                page_identities.append(identity)
            if not chain_valid or len(page_identities) != row_count:
                chain_valid = False
                break
            delivered_rows += row_count
            if truncated:
                if row_count <= 0 or next_offset != offset + row_count:
                    chain_valid = False
                    break
                expected_offset = int(next_offset)
                continue
            if next_offset is not None:
                chain_valid = False
                break
            complete = True
            break

        if chain_valid and complete and delivered_rows == count:
            lines.append(f"审计明细分页已完整读取 {delivered_rows}/{count} 条。")
        else:
            qualifier = "本次要求全部明细；" if wants_complete_details else ""
            lines.append(
                f"{qualifier}审计统计可用，但明细仅连续读取 {delivered_rows}/{count} 条，"
                "不能视为全部明细已交付。"
            )
    return lines, verified


def _truthful_final_content(
    content: str,
    *,
    user_message: str,
    tool_outcomes: list[dict[str, Any]],
    controlled_medical_audit: bool = False,
    authoritative_medical_facilities: Sequence[str] | None = None,
    medical_facility_lookup_succeeded: bool | None = None,
) -> str:
    """Append an authoritative status derived from tools, never model claims."""
    status_lines: list[str] = []
    action_attempts = [item for item in tool_outcomes if item["name"] == "execute_action"]
    preview_targets: dict[str, str] = {}
    for item in action_attempts:
        target = _dry_run_action_target(item)
        if target is not None:
            target_id, target_label = target
            preview_targets.setdefault(target_id, target_label)
    executed_targets: dict[str, str] = {}
    for item in action_attempts:
        target = _executed_action_target(item)
        if target is not None:
            target_id, target_label = target
            executed_targets.setdefault(target_id, target_label)
    preview_count = len(preview_targets)
    executed_count = len(executed_targets)
    delivery_intent = (
        any(term in user_message for term in _DELIVERY_INTENT_VERBS)
        and any(term in user_message for term in _DELIVERY_INTENT_NOUNS)
    )
    if action_attempts or delivery_intent:
        if executed_count:
            labels = [label for label in executed_targets.values() if label]
            target_summary = f"（目标：{'、'.join(labels)}）" if labels else ""
            status_lines.append(
                f"已执行 {executed_count} 个未启用人工确认的操作{target_summary}。"
            )
        if preview_count:
            labels = [label for label in preview_targets.values() if label]
            target_summary = (
                f"（目标：{'、'.join(labels)}）"
                if labels
                else f"（唯一操作 {preview_count} 项）"
            )
            status_lines.append(
                f"已生成 {preview_count} 个可确认预演{target_summary}，"
                "尚未正式执行或生成交付物，请在当前对话回复“确认执行”。"
            )
        elif not executed_count:
            status_lines.append("未生成可确认预演，不能视为业务任务已完成。")

    medical_lines, verified_medical_audit = _medical_audit_status(
        tool_outcomes,
        user_message=user_message,
        authoritative_facilities=authoritative_medical_facilities,
        facility_lookup_succeeded=medical_facility_lookup_succeeded,
    )
    status_lines.extend(medical_lines)
    audit_intent = any(term in user_message for term in _AUDIT_INTENT_TERMS)
    # Generic object queries contain facts, not a governed audit rule/proof.
    # Only a server-owned deterministic audit contract may suppress this guard.
    successful_audit_query = verified_medical_audit
    if audit_intent and not successful_audit_query:
        status_lines.append("未形成可验证审计结论，不能把当前回答作为违规审计结果。")

    failed_counts: defaultdict[str, int] = defaultdict(int)
    for item in tool_outcomes:
        if _failed_tool_result(item["result"]):
            failed_counts[item["name"]] += 1
    if failed_counts:
        failures = "、".join(
            f"{name}（{count} 次）" for name, count in sorted(failed_counts.items())
        )
        status_lines.append(
            f"失败工具：{failures}；上述结论只能基于其余成功返回的证据。"
        )

    if not status_lines:
        return content
    base = content if content.strip() else "本轮未生成模型总结。"
    return base + "\n\n系统核验状态（以此为准）：\n\n" + "\n".join(
        f"- {line}" for line in status_lines
    )


def _resource_api_name(resource: Any) -> str:
    return str(getattr(resource, "api_name", "") or "").strip()


def _resource_by_reference(resources: list[Any], reference: Any, label: str) -> Any | None:
    """Resolve a governed resource by stable id, api_name, or display name."""
    key = str(reference or "").strip()
    if not key:
        return None
    matches = [
        resource
        for resource in resources
        if key
        in {
            str(getattr(resource, "id", "") or ""),
            _resource_api_name(resource),
            str(getattr(resource, "name", "") or ""),
        }
    ]
    if len(matches) > 1:
        raise _ToolContractError(f"{label}名称不唯一，请使用 list 工具返回的 id 或 api_name")
    return matches[0] if matches else None


def _normalized_object_schema(schema: Any) -> dict[str, Any]:
    """Present both current JSON Schema and legacy flat field maps consistently."""
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": True}
    if "properties" in schema or "required" in schema or schema.get("type") == "object":
        normalized = copy.deepcopy(schema)
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
        normalized.setdefault("required", [])
        return normalized
    properties = copy.deepcopy(schema)
    return {
        "type": "object",
        "properties": properties,
        "required": [
            str(name)
            for name, definition in properties.items()
            if isinstance(definition, dict) and definition.get("required") is True
        ],
        "additionalProperties": False,
    }


def _schema_required_fields(schema: Any) -> list[str]:
    normalized = _normalized_object_schema(schema)
    properties = normalized.get("properties", {})
    declared = normalized.get("required")
    required = {
        str(item)
        for item in (declared if isinstance(declared, list) else [])
        if isinstance(item, str) and item
    }
    required.update(
        str(name)
        for name, definition in properties.items()
        if isinstance(definition, dict) and definition.get("required") is True
    )
    return [str(name) for name in properties if str(name) in required]


def _validated_schema_params(schema: Any, value: Any) -> dict[str, Any]:
    """Translate public input-schema violations into a model-correctable error."""
    try:
        return validate_action_params(schema, value if value is not None else {})
    except PolicyViolation as exc:
        raise _ToolContractError(str(exc)) from exc


def _workflow_parameter_schema(workflow: Any, actions: list[Any]) -> dict[str, Any]:
    """Expose the best provable workflow input contract to the model.

    Workflows currently have no dedicated schema column.  Prefer an explicitly
    authored schema in trigger_config, otherwise infer parameters referenced as
    ``{{params.x}}`` and copy downstream Action field schemas when possible.
    """
    trigger = getattr(workflow, "trigger_config", {}) or {}
    if isinstance(trigger, dict):
        explicit = trigger.get("input_schema") or trigger.get("params_schema")
        if isinstance(explicit, dict):
            return _normalized_object_schema(explicit)

    properties: dict[str, Any] = {}
    required: set[str] = set()

    def remember(value: Any, *, is_required: bool = True, definition: Any = None) -> None:
        if isinstance(value, str):
            names = _WORKFLOW_PARAM_RE.findall(value)
            for name in names:
                if name not in properties:
                    properties[name] = (
                        copy.deepcopy(definition)
                        if isinstance(definition, dict)
                        else {"description": "工作流定义引用的输入参数"}
                    )
                if is_required:
                    required.add(name)
        elif isinstance(value, dict):
            for child in value.values():
                remember(child, is_required=is_required)
        elif isinstance(value, list):
            for child in value:
                remember(child, is_required=is_required)

    nodes = list(getattr(workflow, "nodes", []) or [])
    steps = list(getattr(workflow, "steps", []) or [])
    for entry in [*nodes, *steps]:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") if isinstance(entry.get("data"), dict) else entry
        if str(entry.get("type") or data.get("type") or "") != "action":
            remember(data)
            continue
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        action = _resource_by_reference(
            actions,
            data.get("action_id"),
            "工作流引用的操作",
        )
        action_schema = _normalized_object_schema(
            getattr(action, "input_schema", {}) if action is not None else {}
        )
        action_properties = action_schema.get("properties", {})
        action_required = set(_schema_required_fields(action_schema))
        for action_field, value in params.items():
            definition = action_properties.get(action_field)
            remember(
                value,
                is_required=action_field in action_required,
                definition=definition,
            )

    return {
        "type": "object",
        "properties": properties,
        "required": [name for name in properties if name in required],
        # Inferred schemas cannot prove that an unreferenced orchestration
        # parameter is invalid. Explicit trigger schemas may close this list.
        "additionalProperties": True,
    }

class AgentContext:
    """一次 Agent 会话的运行时上下文。"""

    def __init__(
        self,
        db: Session,
        agent: Agent,
        llm: LLMConfig,
        *,
        definition_mode: Literal["authoring", "execution"] = "authoring",
    ):
        """Build either an authoring chat or a governed execution context.

        A deployment environment selects infrastructure bindings only.  It
        must not turn an ordinary in-platform conversation into a 409 merely
        because a fresh scene has not been released in that environment.
        Browser chat and the current public MCP surface both use
        ``authoring`` so a working Agent can be used immediately.  Durable
        background execution still opts into ``execution`` and retains the
        immutable release requirement.
        """
        if definition_mode not in {"authoring", "execution"}:
            raise ValueError("Agent 运行定义模式无效")
        self.db = db
        self.agent = agent
        self.llm = llm
        self.definition_mode = definition_mode
        # Agent 工具始终以当前租户运行；缺少上下文时拒绝，而不是隐式获得全库访问。
        self.tenant_id = tenant_service.current_tenant_id(db)
        self.scenario = (
            tenant_service.get_visible(db, BusinessScenario, agent.scenario_id)
            if agent.scenario_id else None
        )
        if agent.scenario_id:
            if not self.scenario:
                raise PermissionError("Agent 绑定的业务场景不存在或不可见")
            permission_service.require_scenario_permission(
                db,
                self.scenario,
                "read",
                message="没有使用该 Agent 业务场景的权限",
            )
        self.data_sources: list[DataSource] = []
        # 一次回答内的引用编号必须稳定、全局唯一；不能把每次检索各自的 C1 混在一起。
        self.citations: list[dict[str, Any]] = []
        self._citations_by_key: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self._load_bindings()

    def _load_bindings(self) -> None:
        ds_ids = [str(item) for item in (self.agent.data_source_ids or []) if str(item)]
        if ds_ids:
            ds_scope = tenant_service.visible_clause(DataSource, self.db)
            self.data_sources = list(
                self.db.execute(
                    select(DataSource).where(
                        DataSource.id.in_(ds_ids),
                        ds_scope,
                        or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == self.agent.scenario_id),
                    )
                ).scalars().all()
            )
        # Every definition visible to one Agent turn comes from one resolved
        # definition.  Browser chat deliberately uses authoring/live data;
        # published Agent MCP execution opts into the frozen release path.
        self.runtime_definition: runtime_definition_service.RuntimeDefinition | None = None
        # ``NULL`` is the pre-capability-scope format.  Existing Agents were
        # allowed to use the scenario's visible business resources before the
        # allow-list was introduced, so keep that behaviour for legacy rows.
        # Newly created Agents are persisted with an explicit empty scope and
        # remain opt-in through the configuration page.
        raw_capability_scope = (
            agent_capability_service.legacy_all_scope()
            if self.agent.capability_scope is None
            else self.agent.capability_scope
        )
        self.capability_scope = agent_capability_service.normalize_scope(
            raw_capability_scope,
            legacy_default=False,
            allow_all=True,
        )
        self.entities: list[Any] = []
        self.relations: list[Any] = []
        self.functions: list[Any] = []
        self.executable_functions: list[Any] = []
        self.actions: list[Any] = []
        self.previewable_actions: list[Any] = []
        self.rules: list[Any] = []
        self.events: list[Any] = []
        self.workflows: list[Any] = []
        self.mappings: list[Any] = []
        self.relation_mappings: list[Any] = []
        self.capability_readiness: dict[str, dict[str, capability_readiness_service.CapabilityReadiness]] = {
            kind: {} for kind in ("function", "action", "rule", "event", "workflow")
        }
        self.evaluable_rules: list[Any] = []
        self.publishable_events: list[Any] = []
        self.executable_workflows: list[Any] = []
        sid = self.agent.scenario_id
        if not sid or not self.scenario:
            return

        environment = runtime_connector_service.runtime_environment()
        if self.definition_mode == "authoring":
            definition = runtime_definition_service.resolve_authoring(
                self.db,
                self.scenario,
                environment=environment,
            )
        else:
            definition = runtime_definition_service.resolve_execution(
                self.db,
                self.scenario,
                environment=environment,
            )
        self.runtime_definition = definition
        self.entities = list(definition.entities.values())
        self.relations = list(definition.relations.values())
        # ACL filtering and the Agent task contract are applied before
        # readiness, tool construction and prompt generation. Therefore even a
        # forged direct tool call can only search these scoped collections.
        visible_capabilities = agent_capability_service.visible_resources(
            self.db, definition
        )
        scoped_capabilities = agent_capability_service.filter_resources(
            visible_capabilities,
            self.capability_scope,
        )
        self.functions = scoped_capabilities["functions"]
        self.actions = scoped_capabilities["actions"]
        self.rules = scoped_capabilities["rules"]
        self.events = scoped_capabilities["events"]
        self.workflows = scoped_capabilities["workflows"]
        groups = {
            "function": self.functions,
            "action": self.actions,
            "rule": self.rules,
            "event": self.events,
            "workflow": self.workflows,
        }
        for kind, resources in groups.items():
            self.capability_readiness[kind] = {
                str(resource.id): capability_readiness_service.capability_readiness(
                    kind,
                    resource,
                    definition=definition,
                    db=self.db,
                )
                for resource in resources
            }
        self.executable_functions = [
            item for item in self.functions if self._capability_status("function", item).executable
        ]
        self.previewable_actions = [
            item for item in self.actions if self._capability_status("action", item).executable
        ]
        self.evaluable_rules = [
            item for item in self.rules if self._capability_status("rule", item).executable
        ]
        self.publishable_events = [
            item for item in self.events if self._capability_status("event", item).executable
        ]
        self.executable_workflows = [
            item for item in self.workflows if self._capability_status("workflow", item).executable
        ]

        configured_source_ids = set(ds_ids)
        if definition.is_frozen:
            # A frozen mapping selects its physical connector through the
            # environment binding. Never use the snapshot's dev data_source_id
            # as the staging/prod query target.
            resolved_sources: dict[str, DataSource] = {
                source.id: source for source in self.data_sources
                if source.type == "file_bucket"
            }
            for mapping in definition.mappings.values():
                connector, _audit = runtime_connector_service.resolve_connector(
                    self.db,
                    self.scenario,
                    kind="data_source",
                    config={
                        "data_source_id": mapping.data_source_id,
                        "data_source_binding_key": getattr(
                            mapping, "data_source_binding_key", ""
                        ),
                        "data_source_binding_ref": getattr(
                            mapping, "data_source_binding_ref", {}
                        ),
                    },
                    environment=definition.environment,
                    release_id=definition.release_id,
                )
                if (
                    str(mapping.data_source_id) not in configured_source_ids
                    and str(connector.id) not in configured_source_ids
                ):
                    continue
                runtime_mapping = SimpleNamespace(**vars(mapping))
                runtime_mapping.definition_data_source_id = mapping.data_source_id
                runtime_mapping.data_source_id = connector.id
                runtime_mapping.entity = definition.entities.get(str(mapping.entity_id))
                self.mappings.append(runtime_mapping)
                resolved_sources[connector.id] = connector
            self.data_sources = list(resolved_sources.values())
        else:
            bound_source_ids = {source.id for source in self.data_sources}
            self.mappings = [
                mapping for mapping in definition.mappings.values()
                if mapping.data_source_id in bound_source_ids
            ]
        visible_mapping_ids = {str(mapping.id) for mapping in self.mappings}
        self.relation_mappings = [
            mapping
            for mapping in definition.relation_mappings.values()
            if str(mapping.source_mapping_id) in visible_mapping_ids
            and str(mapping.target_mapping_id) in visible_mapping_ids
        ]

    def _capability_status(
        self,
        kind: str,
        resource: Any,
    ) -> capability_readiness_service.CapabilityReadiness:
        cached = self.capability_readiness.get(kind, {}).get(str(resource.id))
        if cached is not None:
            return cached
        return capability_readiness_service.capability_readiness(
            kind,
            resource,
            definition=self.runtime_definition,
            db=self.db,
        )

    def _execution_definition(self) -> runtime_definition_service.RuntimeDefinition:
        """Resolve the definition used by an effect in this Agent turn.

        Browser chat and the current MCP publication surface deliberately use
        the same ACL-scoped authoring/live definition.  That keeps an Agent
        that works in the platform usable immediately after publication,
        without making dev/prod, releases, or connector bindings feature
        gates.  Durable execution contexts still arrive with a frozen runtime
        definition and retain their existing release semantics.
        """
        if not self.scenario:
            raise runtime_definition_service.RuntimeDefinitionError("Agent 未绑定业务场景")
        if self.runtime_definition is not None:
            return self.runtime_definition
        return runtime_definition_service.resolve_execution(
            self.db,
            self.scenario,
            environment=runtime_connector_service.runtime_environment(),
        )

    def _medical_audit_access_policy(
        self,
    ) -> medical_audit_service.MedicalAuditAccessPolicy:
        """Resolve every medical field through the current ontology ACL.

        The policy starts empty and adds only declared, currently readable
        properties. Missing definitions therefore cannot become implicit
        grants for this specialized query path.
        """

        allowed: set[str] = set()
        for entity in self.entities:
            entity_api_name = str(getattr(entity, "api_name", "") or "").strip()
            if entity_api_name not in {"medical_charge_line", "medical_encounter"}:
                continue
            for prop in getattr(entity, "properties", []) or []:
                property_api_name = str(
                    getattr(prop, "api_name", "") or ""
                ).strip()
                if not property_api_name:
                    continue
                if permission_service.can_read_property(self.db, prop):
                    allowed.add(f"{entity_api_name}.{property_api_name}")
        return medical_audit_service.access_policy(sorted(allowed))

    def _medical_audit_mapping_contract(
        self,
    ) -> medical_audit_service.MedicalAuditMappingContract:
        """Resolve the specialized audit only through this turn's runtime mappings."""

        return medical_audit_service.resolve_mapping_contract(
            self.data_sources,
            self.mappings,
            definition=self.runtime_definition,
        )

    def _medical_facility_names_in_message(self, user_message: str) -> list[str]:
        """Resolve user-stated facilities through this turn's mapping and ACL."""

        return medical_audit_service.find_facility_names_in_text(
            self._medical_audit_mapping_contract(),
            user_message,
            property_access=self._medical_audit_access_policy(),
        )

    def _rule_fields_are_visible(self, rule: Any) -> bool:
        """Do not expose a rule as a side door to a hidden ontology property."""
        visible_action_ids = {str(action.id) for action in self.actions}
        if any(
            str(action_id) not in visible_action_ids
            for action_id in (getattr(rule, "trigger_action_ids", []) or [])
        ):
            return False
        entity = getattr(rule, "entity", None)
        if not entity:
            return True
        fields: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("field"):
                    fields.add(str(value["field"]))
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(getattr(rule, "condition", {}) or {})
        visible = {
            str(prop.name)
            for prop in getattr(entity, "properties", [])
            if permission_service.can_read_property(self.db, prop)
        }
        return fields.issubset(visible)

    def _writable_file_buckets(self) -> list[DataSource]:
        """公开绑定资源可读不可写；交付物只允许保存到当前租户自有桶。"""
        return [
            source for source in self.data_sources
            if source.type == "file_bucket" and source.tenant_id == self.tenant_id
        ]

    def _record_citations(self, raw_citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """登记 RAG 命中并生成本轮对话唯一的引用编号。

        ``rag_service`` 已按租户可见性过滤，这里再限制为 Agent 当前绑定的数据源，
        防止未来工具实现误把未绑定或未授权资料写入消息审计记录。
        """
        allowed_source_ids = {source.id for source in self.data_sources if source.type == "file_bucket"}
        normalized: list[dict[str, Any]] = []
        for raw in raw_citations:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("data_source_id") or "")
            file_id = str(raw.get("file_id") or "")
            chunk_id = str(raw.get("chunk_id") or "")
            if not source_id or source_id not in allowed_source_ids or not file_id:
                continue
            try:
                char_start = int(raw.get("char_start"))
                char_end = int(raw.get("char_end"))
            except (TypeError, ValueError):
                continue
            if char_start < 0 or char_end <= char_start:
                continue
            key = (file_id, chunk_id or "document", char_start, char_end)
            citation = self._citations_by_key.get(key)
            if citation is None:
                citation = {
                    "citation_id": f"C{len(self.citations) + 1}",
                    "chunk_id": chunk_id,
                    "file_id": file_id,
                    "filename": str(raw.get("filename") or "资料文件"),
                    "data_source_id": source_id,
                    "data_source_name": str(raw.get("data_source_name") or "资料库"),
                    "char_start": char_start,
                    "char_end": char_end,
                    "chunk_ordinal": int(raw.get("chunk_ordinal") or 0),
                    "content_hash": str(raw.get("content_hash") or ""),
                    "file_content_hash": str(raw.get("file_content_hash") or ""),
                    "embedding_model": str(raw.get("embedding_model") or ""),
                    "index_version": str(raw.get("index_version") or ""),
                    "score": float(raw.get("score") or 0),
                    "vector_score": float(raw.get("vector_score") or 0),
                    "keyword_score": float(raw.get("keyword_score") or 0),
                    "text": str(raw.get("text") or ""),
                }
                self.citations.append(citation)
                self._citations_by_key[key] = citation
            normalized.append(dict(citation))
        return normalized

    def citation_snapshot(self) -> list[dict[str, Any]]:
        """返回可安全发送到 SSE / 持久化消息的独立副本。"""
        return [dict(citation) for citation in self.citations]

    # ── 工具定义 ──────────────────────────────
    def build_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.scenario and self.entities:
            tools += [
                _tool(
                    "list_ontology_model",
                    "发现当前业务场景的对象类型和关系类型。无参数时返回不含完整属性的紧凑目录；"
                    "需要某个对象的字段、类型或关系详情时，必须传 entity（id、api_name 或显示名称）精确读取。",
                    {
                        "entity": {
                            "type": "string",
                            "description": "可选；对象类型 id、api_name 或完整显示名称",
                        }
                    },
                ),
                _tool(
                    "search_ontology",
                    "按对象类型和关键词检索当前业务场景中的对象实例及属性。"
                    "用于先确认业务对象，再决定是否需要查询外部数据源。",
                    {
                        "entity": {"type": "string", "description": "对象类型名称，可留空查看所有类型"},
                        "query": {"type": "string", "description": "对象名称或属性关键词，可留空"},
                    },
                ),
                _tool(
                    "get_ontology_object",
                    "读取一个对象实例的可见属性、已断言关系实例，以及由对称、传递或逆关系公理在查询期推导的只读关系。"
                    "推导关系明确标记 inferred=true 并携带物理边 path，不会写入或冒充已物化实例。"
                    "参数 object_id 来自 search_ontology。",
                    {
                        "object_id": {
                            "type": "string",
                            "description": "对象实例 id",
                            "required": True,
                        }
                    },
                ),
            ]
        if self.data_sources:
            tools += [
                _tool(
                    "list_data_sources",
                    "列出当前 Agent 绑定的所有数据源（数据库与文件桶），返回 id、名称、类型。",
                    {},
                ),
                _tool(
                    "list_tables",
                    "列出指定数据源中当前 Agent 数据映射允许读取的表及列，用于核对映射边界。",
                    {"data_source_id": {"type": "string", "description": "数据源 id", "required": True}},
                ),
                _tool(
                    "search_documents",
                    "在已绑定且有权限的文件桶中执行混合向量检索，返回可引用的资料片段。"
                    "回答引用事实时必须使用结果的 citation_id（如【C1】）。",
                    {
                        "query": {"type": "string", "description": "检索问题或关键词", "required": True},
                        "top_k": {"type": "integer", "description": "返回片段数，默认 5，最大 10"},
                    },
                ),
                _tool(
                    "read_document",
                    "读取已绑定且有权限的已解析文档。优先使用 search_documents 返回的 file_id，"
                    "避免仅按文件名读取同名资料。",
                    {
                        "file_id": {"type": "string", "description": "检索结果中的文件 id"},
                        "filename": {"type": "string", "description": "兼容旧会话的文件名，重名时会拒绝"},
                    },
                ),
            ]
        medical_audit_ready = False
        if self.scenario and self.scenario.namespace == "medical_audit":
            try:
                self._medical_audit_mapping_contract()
            except medical_audit_service.MedicalAuditError:
                medical_audit_ready = False
            else:
                medical_audit_ready = True
        if medical_audit_ready:
            tools.append(
                _tool(
                    "run_medical_audit",
                    "执行版本化、确定性的医保违规审计。只选择受控 strategy 并传业务参数；"
                    "不能传 SQL、表名、列名或数据源 id。结果包含全量命中计数和金额、证据口径及分页游标；"
                    "truncated=true 时保持相同参数并使用 next_offset 读取下一页。",
                    medical_audit_service.tool_schema(),
                )
            )
        if self.mappings:
            tools += [
                _tool(
                    "list_data_mappings",
                    "列出当前 Agent 已绑定数据源与对象类型之间的数据映射，"
                    "返回对象属性到源字段的对应关系和最近刷新状态。",
                    {},
                ),
                _tool(
                    "query_mapped_objects",
                    "优先使用本工具按本体属性查询业务对象。只传对象类型、属性、结构化过滤、排序和行数；"
                    "服务端从当前冻结/开发运行定义选择唯一映射并生成参数化只读 SQL，不能传 SQL、数据源、表或列。"
                    "结果 truncated=true 时，用 next_offset 继续读取，并保持相同的稳定排序。",
                    {
                        "entity_id": {
                            "type": "string",
                            "description": "对象类型 id；与 entity_name 至少提供一个",
                        },
                        "entity_name": {
                            "type": "string",
                            "description": "对象类型名称；重名时必须改用 entity_id",
                        },
                        "properties": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 50,
                            "description": "要返回的本体属性名",
                            "required": True,
                        },
                        "filters": {
                            "type": "array",
                            "maxItems": 20,
                            "description": "结构化过滤条件；所有条件按 AND 组合",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "property": {"type": "string"},
                                    "op": {
                                        "type": "string",
                                        "enum": sorted(mapped_query_service.FILTER_OPERATORS),
                                    },
                                    "value": {
                                        "description": "比较值；in/not_in 使用标量列表，空值判断不传"
                                    },
                                },
                                "required": ["property", "op"],
                                "additionalProperties": False,
                            },
                        },
                        "sort": {
                            "type": "array",
                            "maxItems": 5,
                            "description": "按本体属性排序",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "property": {"type": "string"},
                                    "direction": {
                                        "type": "string",
                                        "enum": ["asc", "desc"],
                                    },
                                },
                                "required": ["property"],
                                "additionalProperties": False,
                            },
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "最大返回行数，不超过平台限制",
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "从 0 开始的分页偏移；后续页传上一页返回的 next_offset",
                        },
                    },
                ),
                _tool(
                    "query_business_data",
                    "按对象类型和本体属性完成业务数据查询。支持多个相关对象、过滤、分组、次数/金额等聚合和排序；"
                    "服务端根据当前运行定义和数据映射生成参数化查询。不能传 SQL、表名、列名或数据源 id。"
                    "需要跨表明细、按业务对象统计或审计汇总时使用本工具。结果 truncated=true 时，"
                    "用 next_offset 继续读取，并保持相同的稳定排序。",
                    {
                        "base_entity": {
                            "description": "主对象类型；优先传对象引用，也兼容直接传对象显示名称字符串",
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "entity_id": {"type": "string", "minLength": 1},
                                        "entity_name": {"type": "string", "minLength": 1},
                                    },
                                    "anyOf": [
                                        {"required": ["entity_id"]},
                                        {"required": ["entity_name"]},
                                    ],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "兼容简写：对象显示名称",
                                },
                            ],
                            "required": True,
                        },
                        "base_properties": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 50,
                            "description": "主对象要返回的本体属性；纯聚合查询可省略或传空数组",
                        },
                        "base_filters": {
                            "type": "array",
                            "maxItems": 20,
                            "description": "主对象过滤条件，按 AND 组合",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "property": {"type": "string"},
                                    "op": {"type": "string", "enum": sorted(business_query_service.mapped_query_service.FILTER_OPERATORS)},
                                    "value": {},
                                },
                                "required": ["property", "op"],
                                "additionalProperties": False,
                            },
                        },
                        "related_entities": {
                            "type": "array",
                            "maxItems": 5,
                            "description": "要关联的对象；未提供 join 时，服务端尝试根据数据关系映射或唯一共同字段推断关联",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entity_id": {"type": "string", "minLength": 1},
                                    "entity_name": {"type": "string", "minLength": 1},
                                    "properties": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                                    "filters": {
                                        "type": "array",
                                        "maxItems": 20,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "property": {"type": "string"},
                                                "op": {"type": "string", "enum": sorted(business_query_service.mapped_query_service.FILTER_OPERATORS)},
                                                "value": {},
                                            },
                                            "required": ["property", "op"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "join": {
                                        "type": "object",
                                        "properties": {
                                            "base_property": {"type": "string"},
                                            "related_property": {"type": "string"},
                                        },
                                        "additionalProperties": False,
                                    },
                                },
                                "anyOf": [
                                    {"required": ["entity_id"]},
                                    {"required": ["entity_name"]},
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "group_by": {
                            "type": "array",
                            "maxItems": 20,
                            "description": "按已参与查询的对象属性分组",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entity_id": {"type": "string", "minLength": 1},
                                    "entity_name": {"type": "string", "minLength": 1},
                                    "property": {"type": "string", "minLength": 1},
                                },
                                "required": ["property"],
                                "anyOf": [
                                    {"required": ["entity_id"]},
                                    {"required": ["entity_name"]},
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "aggregations": {
                            "type": "array",
                            "maxItems": 20,
                            "description": "count、sum、avg、min、max 聚合；每项需要对象和唯一 alias。count 可省略 property 表示 COUNT(*)，其他函数必须提供 property",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "function": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                                    "entity_id": {"type": "string", "minLength": 1},
                                    "entity_name": {"type": "string", "minLength": 1},
                                    "property": {"type": "string", "minLength": 1, "description": "count 可省略；sum/avg/min/max 必填"},
                                    "alias": {"type": "string", "minLength": 1},
                                },
                                "required": ["function", "alias"],
                                "anyOf": [
                                    {"required": ["entity_id"]},
                                    {"required": ["entity_name"]},
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "having": {
                            "type": "array",
                            "maxItems": 20,
                            "description": "对聚合 alias 做阈值筛选，例如次数大于 2；只能用于分组聚合查询",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "alias": {"type": "string"},
                                    "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"]},
                                    "value": {},
                                },
                                "required": ["alias", "op", "value"],
                                "additionalProperties": False,
                            },
                        },
                        "sort": {
                            "type": "array",
                            "maxItems": 10,
                            "description": "按对象属性或聚合 alias 排序",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entity_id": {"type": "string", "minLength": 1},
                                    "entity_name": {"type": "string", "minLength": 1},
                                    "property": {"type": "string", "minLength": 1},
                                    "alias": {"type": "string", "minLength": 1},
                                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                                },
                                "required": ["direction"],
                                "anyOf": [
                                    {"required": ["alias"]},
                                    {
                                        "required": ["property"],
                                        "anyOf": [
                                            {"required": ["entity_id"]},
                                            {"required": ["entity_name"]},
                                        ],
                                    },
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "最大返回记录数，不超过平台限制",
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "从 0 开始的分页偏移；后续页传上一页返回的 next_offset",
                        },
                    },
                ),
            ]
        # 函数是闭集、确定性且无副作用的计算能力，可由 Agent 直接调用。
        if self.functions:
            tools.append(
                _tool(
                    "list_functions",
                    "列出当前业务场景中的函数契约、输入输出字段和是否可直接运行。",
                    {},
                )
            )
        if self.executable_functions:
            tools.append(
                _tool(
                    "run_function",
                    "调用无副作用的确定性业务函数。必须先用 list_functions 获取准确 input_schema/required；function_id 可传 id、api_name 或显示名称，不要向用户索取内部 id。",
                    {
                        "function_id": {"type": "string", "description": "业务函数 id、api_name 或显示名称", "required": True},
                        "params": {"type": "object", "description": "按函数输入字段填写的参数", "required": True},
                    },
                )
            )
        # 本体扩展工具：操作 / 规则 / 工作流
        if self.actions:
            tools.append(
                _tool(
                    "list_actions",
                    "发现当前业务场景中的操作。无参数时返回不含完整 input_schema 的紧凑目录；"
                    "准备调用某个操作时，必须传 action（id、api_name 或显示名称）精确读取其完整 schema。",
                    {
                        "action": {
                            "type": "string",
                            "description": "可选；操作 id、api_name 或完整显示名称",
                        }
                    },
                )
            )
        if self.previewable_actions:
            tools.append(
                _tool(
                    "execute_action",
                    "执行场景中定义的某个操作。必须先用 list_actions 的 action 参数精确读取该操作，"
                    "并严格按其 input_schema/required 填写 params；服务端会按 requires_confirmation 决定"
                    "直接执行或创建确认预演。action_id 可传 id、api_name 或显示名称，不要向用户索取内部 id。",
                    {
                        "action_id": {"type": "string", "description": "操作 id、api_name 或显示名称", "required": True},
                        "params": {"type": "object", "description": "输入参数"},
                    },
                )
            )
        if self.rules:
            tools.append(
                _tool(
                    "list_rules",
                    "列出当前业务场景中定义的所有业务规则（Rules），返回 id、名称、条件表达式、严重级别。",
                    {},
                )
            )
        if self.evaluable_rules:
            tools.append(
                _tool(
                    "evaluate_rule",
                    "用给定数据记录评估某条业务规则是否命中。rule_id 可传 id、api_name 或显示名称。",
                    {
                        "rule_id": {"type": "string", "description": "规则 id、api_name 或显示名称", "required": True},
                        "record": {"type": "object", "description": "待评估的数据记录", "required": True},
                    },
                )
            )
        if self.events:
            tools.append(
                _tool(
                    "list_events",
                    "列出当前业务场景的事件类型、载荷字段、触发来源和启用状态。",
                    {},
                )
            )
        if self.publishable_events:
            tools.append(
                _tool(
                    "prepare_event_publish",
                    "生成服务端固定的事件发布预演；本工具不会发布事件，用户需在当前对话回复“确认发布”后继续。",
                    {
                        "event_id": {"type": "string", "description": "事件 id、api_name 或显示名称", "required": True},
                        "payload": {"type": "object", "description": "事件载荷", "required": True},
                    },
                )
            )
        if self.workflows:
            tools.append(
                _tool(
                    "list_workflows",
                    "列出当前业务场景中定义的所有工作流（Workflows），返回 id、名称、触发方式、步骤数。",
                    {},
                )
            )
        if self.executable_workflows:
            tools.append(
                _tool(
                    "execute_workflow",
                    "生成服务端固定的工作流运行预演；本工具不会提交任务。必须先调用 list_workflows，并严格按其 params_schema/required 填写 params；workflow_id 可传 id、api_name 或显示名称，不要向用户索取内部 id。",
                    {
                        "workflow_id": {"type": "string", "description": "工作流 id、api_name 或显示名称", "required": True},
                        "params": {"type": "object", "description": "输入参数"},
                    },
                )
            )
        return tools

    # ── 工具执行 ──────────────────────────────
    def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        # Every published MCP tool call reaches this durable fencing point
        # before it can trigger an Action, Event, Workflow or external MCP.
        _assert_agent_turn_lease(self.db)
        if not isinstance(args, dict):
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                "工具参数必须是 JSON 对象；请根据当前工具 schema 修正后重试一次。",
                retryable=True,
            )
        try:
            if name == "list_ontology_model":
                return _dump(
                    self._ontology_model_tool(str(args.get("entity") or ""))
                )
            if name == "search_ontology":
                return _dump(
                    self._search_ontology(
                        str(args.get("entity") or ""),
                        str(args.get("query") or ""),
                    )
                )
            if name == "get_ontology_object":
                return _dump(self._ontology_object(str(args.get("object_id") or "")))
            if name == "list_data_sources":
                return _dump(
                    [
                        {
                            "id": d.id,
                            "name": d.name,
                            "type": d.type,
                            "status": d.status,
                            "connector_revision": int(d.connector_revision or 0),
                        }
                        for d in self.data_sources
                    ]
                )
            if name == "list_tables":
                ds = self._ds(args.get("data_source_id"))
                if not ds:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到所请求的数据源；请先调用 list_data_sources 刷新可用数据源。",
                        retryable=True,
                    )
                allowed: dict[str, set[str]] = {}
                for mapping in self._mapping_catalog():
                    if mapping.get("kind") != "object":
                        continue
                    if mapping["data_source_id"] == ds.id and mapping["table"]:
                        allowed.setdefault(str(mapping["table"]), set()).update(
                            str(column)
                            for column in mapping["column_map"].values()
                            if column
                        )
                tables: list[dict[str, Any]] = []
                for table in datasource_service.list_tables(ds):
                    table_name = str(table.get("name") or "")
                    if table_name not in allowed:
                        continue
                    item = dict(table)
                    item["data_source_id"] = ds.id
                    item["connector_revision"] = int(ds.connector_revision or 0)
                    item["columns"] = [
                        column
                        for column in table.get("columns", [])
                        if isinstance(column, dict)
                        and str(column.get("name") or "") in allowed[table_name]
                    ]
                    tables.append(item)
                return _dump(tables)
            if name == "run_medical_audit":
                if not self.scenario or self.scenario.namespace != "medical_audit":
                    return _tool_error(
                        "DIRECT_TOOL_DISABLED",
                        "run_medical_audit 仅供医保审计业务场景使用。",
                        retryable=False,
                    )
                try:
                    mapping_contract = self._medical_audit_mapping_contract()
                    return _dump(
                        medical_audit_service.run_medical_audit(
                            mapping_contract,
                            args,
                            property_access=self._medical_audit_access_policy(),
                        )
                    )
                except medical_audit_service.MedicalAuditError as exc:
                    return _tool_error(
                        exc.code,
                        exc.message,
                        retryable=exc.retryable,
                    )
            if name == "run_sql":
                return _tool_error(
                    "DIRECT_TOOL_DISABLED",
                    "当前 Agent 对话不直接接收 SQL；请使用 query_mapped_objects 或 query_business_data 按业务对象和属性查询。",
                    retryable=True,
                )
            if name == "search_documents":
                results = rag_service.search(
                    self.db,
                    [d.id for d in self.data_sources if d.type == "file_bucket"],
                    args.get("query", ""),
                    top_k=max(1, min(int(args.get("top_k") or 5), 10)),
                )
                citations = self._record_citations(results)
                if not citations:
                    return _dump(
                        {
                            "retrieval_mode": "hybrid-vector-keyword",
                            "citations": [],
                            "row_count": 0,
                            "empty": True,
                            "message": "未检索到可引用的文档内容。",
                        }
                    )
                return _dump(
                    {
                        "retrieval_mode": "hybrid-vector-keyword",
                        "citations": citations,
                        "instruction": "最终回答涉及资料事实时，请标注对应的 citation_id，例如【C1】；不要编造引用。",
                    }
                )
            if name == "read_document":
                return self._read_doc(args.get("file_id", ""), args.get("filename", ""))
            if name == "list_data_mappings":
                return _dump(self._mapping_catalog())
            if name == "query_mapped_objects":
                return _dump(
                    mapped_query_service.query_mapped_objects(
                        self.db,
                        definition=self.runtime_definition,
                        mappings=self.mappings,
                        data_sources=self.data_sources,
                        args=args,
                    )
                )
            if name == "query_business_data":
                return _dump(
                    business_query_service.query_business_data(
                        self.db,
                        definition=self.runtime_definition,
                        mappings=self.mappings,
                        data_sources=self.data_sources,
                        args=args,
                    )
                )
            if name == "save_deliverable" or name == "execute_skill" or name.startswith("mcp_"):
                return _tool_error(
                    "DIRECT_TOOL_DISABLED",
                    "Agent 不直接执行本地技能、外部工具或写入文件；请使用已配置的场景操作生成预演。",
                    retryable=True,
                )
            # 本体扩展工具
            if name == "list_functions":
                return _dump(
                    [
                        {
                            "id": function.id,
                            "api_name": _resource_api_name(function),
                            "name": function.name,
                            "description": function.description[:120],
                            "input_schema": _normalized_object_schema(function.input_schema),
                            "required": _schema_required_fields(function.input_schema or {}),
                            "output_schema": function.output_schema or {},
                            "runtime": function.runtime_kind,
                            "definition_hash": (
                                self.runtime_definition.definition_hash
                                if self.runtime_definition else ""
                            ),
                            **self._capability_status("function", function).as_dict(),
                        }
                        for function in self.functions
                    ]
                )
            if name == "run_function":
                key = str(args.get("function_id") or "")
                function = _resource_by_reference(self.functions, key, "函数")
                if not function:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到函数；请先调用 list_functions 刷新名称和输入字段。",
                        retryable=True,
                    )
                capability_readiness_service.require_executable(
                    "function", function, definition=self.runtime_definition, db=self.db
                )
                run = function_runtime_service.create_function_run(
                    self.db,
                    function,
                    args.get("params") or {},
                    tenant_id=self.tenant_id,
                    scenario_id=function.scenario_id,
                    user_id=str(self.db.info.get("user_id")) if self.db.info.get("user_id") else None,
                    definition_hash=(
                        self.runtime_definition.definition_hash
                        if self.runtime_definition else None
                    ),
                )
                self.db.flush()
                if run.status != "succeeded":
                    return _tool_error(
                        "FUNCTION_EXECUTION_FAILED",
                        "业务函数未成功完成；请用 list_functions 核对输入字段后重试一次。",
                        retryable=True,
                    )
                output = run.output_payload or {}
                if isinstance(output, dict) and self.runtime_definition is not None:
                    output = {
                        **output,
                        "definition_hash": self.runtime_definition.definition_hash,
                    }
                return _dump(output)
            if name == "list_actions":
                action_ref = str(args.get("action") or "").strip()
                selected_actions = self.actions
                detailed = bool(action_ref)
                if action_ref:
                    selected = _resource_by_reference(self.actions, action_ref, "操作")
                    selected_actions = [selected] if selected is not None else []
                return _dump(
                    [
                        {
                            "id": a.id,
                            "api_name": _resource_api_name(a),
                            "name": a.name,
                            "entity": a.entity.name if a.entity else "",
                            "executor_type": a.executor_type,
                            "enabled": bool(a.enabled),
                            "requires_confirmation": bool(a.requires_confirmation),
                            "required": _schema_required_fields(a.input_schema or {}),
                            **self._capability_status("action", a).as_dict(),
                            **(
                                {
                                    "description": a.description[:120],
                                    "precondition": a.precondition or "",
                                    "postcondition": a.postcondition or "",
                                    "input_schema": _normalized_object_schema(a.input_schema),
                                    "definition_hash": (
                                        self.runtime_definition.definition_hash
                                        if self.runtime_definition else ""
                                    ),
                                }
                                if detailed else {}
                            ),
                        }
                        for a in selected_actions
                    ]
                )
            if name == "execute_action":
                key = str(args.get("action_id") or "")
                a = _resource_by_reference(self.actions, key, "操作")
                if not a:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到操作；请先调用 list_actions 刷新名称、input_schema 和 required。",
                        retryable=True,
                    )
                # A stale model call can name a resource that is still
                # discoverable for authoring diagnostics but has been
                # disabled. Report that static state before asking for a
                # release, since no execution boundary is reached.
                if not bool(getattr(a, "enabled", False)):
                    return _tool_error(
                        "CAPABILITY_NOT_READY",
                        "操作已停用。",
                        retryable=False,
                    )
                definition = self._execution_definition()
                a = runtime_definition_service.resolve_resource(
                    definition,
                    "action",
                    a.id,
                )
                capability_readiness_service.require_executable(
                    "action", a, definition=definition, db=self.db
                )
                permission_service.require_action_permission(
                    self.db,
                    a,
                    "read" if a.requires_confirmation else "execute",
                )
                if not self.scenario:
                    return _tool_error(
                        "CAPABILITY_NOT_READY",
                        "操作缺少业务场景，当前不能执行。",
                        retryable=False,
                    )
                params = _validated_schema_params(
                    a.input_schema or {}, args.get("params")
                )
                if a.requires_confirmation:
                    r = workflow_service.execute_action(
                        self.db,
                        a,
                        params,
                        dry_run=True,
                        enforce_policy=True,
                        runtime_environment=definition.environment,
                        runtime_definition=definition,
                    )
                    r["message"] = (
                        "这是已固定定义版本的预演结果；请在当前对话回复“确认执行”"
                        "（多项待确认时附上操作名称）后继续。"
                    )
                else:
                    r = workflow_service.execute_action(
                        self.db,
                        a,
                        params,
                        confirm=True,
                        dry_run=False,
                        idempotency_key=_automatic_action_idempotency_key(
                            self.db,
                            action_id=str(a.id),
                            params=params,
                        ),
                        enforce_policy=True,
                        runtime_environment=definition.environment,
                        runtime_definition=definition,
                    )
                    r["message"] = "该操作未启用人工确认，已按当前定义执行。"
                r["definition_hash"] = definition.definition_hash
                return _dump(r)
            if name == "list_rules":
                scoped_action_ids = {str(action.id) for action in self.actions}
                return _dump(
                    [
                        {
                            "id": r.id,
                            "api_name": _resource_api_name(r),
                            "name": r.name,
                            "entity": r.entity.name if r.entity else "",
                            "severity": r.severity,
                            "condition": r.condition,
                            "enabled": bool(r.enabled),
                            "trigger_action_ids": [
                                action_id for action_id in (r.trigger_action_ids or [])
                                if str(action_id) in scoped_action_ids
                            ],
                            "unavailable_trigger_action_count": sum(
                                1 for action_id in (r.trigger_action_ids or [])
                                if str(action_id) not in scoped_action_ids
                            ),
                            "definition_hash": (
                                self.runtime_definition.definition_hash
                                if self.runtime_definition else ""
                            ),
                            **self._capability_status("rule", r).as_dict(),
                        }
                        for r in self.rules
                    ]
                )
            if name == "evaluate_rule":
                r = _resource_by_reference(self.rules, args.get("rule_id"), "规则")
                if not r:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到规则；请先调用 list_rules 刷新可用名称。",
                        retryable=True,
                    )
                capability_readiness_service.require_executable(
                    "rule", r, definition=self.runtime_definition, db=self.db
                )
                evaluated = workflow_service.evaluate_rule(
                    r,
                    args.get("record", {}),
                    db=self.db,
                    runtime_definition=self.runtime_definition,
                )
                scoped_action_ids = {str(action.id) for action in self.actions}
                trigger_actions = [
                    item for item in (evaluated.get("trigger_actions") or [])
                    if str(item.get("action_id") or "") in scoped_action_ids
                ]
                evaluated["unavailable_trigger_action_count"] = max(
                    0,
                    len(evaluated.get("trigger_actions") or []) - len(trigger_actions),
                )
                evaluated["trigger_actions"] = trigger_actions
                evaluated["trigger_action_ids"] = [
                    action_id for action_id in (evaluated.get("trigger_action_ids") or [])
                    if str(action_id) in scoped_action_ids
                ]
                if self.runtime_definition is not None:
                    evaluated["definition_hash"] = self.runtime_definition.definition_hash
                return _dump(evaluated)
            if name == "list_events":
                return _dump(
                    [
                        {
                            "id": event.id,
                            "api_name": _resource_api_name(event),
                            "name": event.name,
                            "description": event.description[:120],
                            "payload_schema": _normalized_object_schema(event.payload_schema),
                            "required": _schema_required_fields(event.payload_schema or {}),
                            "trigger_source": event.trigger_source,
                            "enabled": bool(event.enabled),
                            "definition_hash": (
                                self.runtime_definition.definition_hash
                                if self.runtime_definition else ""
                            ),
                            **self._capability_status("event", event).as_dict(),
                        }
                        for event in self.events
                    ]
                )
            if name == "prepare_event_publish":
                key = str(args.get("event_id") or "")
                event = _resource_by_reference(self.events, key, "事件")
                if not event:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到事件；请先调用 list_events 刷新名称和载荷字段。",
                        retryable=True,
                    )
                if not bool(getattr(event, "enabled", False)):
                    return _tool_error(
                        "CAPABILITY_NOT_READY",
                        "事件已停用。",
                        retryable=False,
                    )
                definition = self._execution_definition()
                event = runtime_definition_service.resolve_resource(
                    definition,
                    "event",
                    event.id,
                )
                capability_readiness_service.require_executable(
                    "event", event, definition=definition, db=self.db
                )
                return _dump(
                    agent_confirmation_service.preview_event_publish(
                        self.db,
                        event,
                        _validated_schema_params(
                            event.payload_schema or {},
                            args.get("payload"),
                        ),
                        runtime_definition=definition,
                    )
                )
            if name == "list_workflows":
                return _dump(
                    [
                        {
                            "id": w.id,
                            "api_name": _resource_api_name(w),
                            "name": w.name,
                            "trigger_type": w.trigger_type,
                            "steps_count": len(w.steps or []),
                            "nodes_count": len(w.nodes or []),
                            "description": w.description[:120],
                            "status": w.status,
                            "enabled": bool(w.enabled),
                            "params_schema": _workflow_parameter_schema(w, self.actions),
                            "required": _schema_required_fields(
                                _workflow_parameter_schema(w, self.actions)
                            ),
                            "definition_hash": (
                                self.runtime_definition.definition_hash
                                if self.runtime_definition else ""
                            ),
                            **self._capability_status("workflow", w).as_dict(),
                        }
                        for w in self.workflows
                    ]
                )
            if name == "execute_workflow":
                w = _resource_by_reference(
                    self.workflows,
                    args.get("workflow_id"),
                    "工作流",
                )
                if not w:
                    return _tool_error(
                        "RESOURCE_NOT_FOUND",
                        "未找到工作流；请先调用 list_workflows 刷新名称、params_schema 和 required。",
                        retryable=True,
                    )
                if not bool(getattr(w, "enabled", False)) or str(
                    getattr(w, "status", "draft") or "draft"
                ) != "active":
                    return _tool_error(
                        "CAPABILITY_NOT_READY",
                        "工作流未启用或尚未处于活动状态。",
                        retryable=False,
                    )
                definition = self._execution_definition()
                w = runtime_definition_service.resolve_resource(
                    definition,
                    "workflow",
                    w.id,
                )
                capability_readiness_service.require_executable(
                    "workflow", w, definition=definition, db=self.db
                )
                workflow_params = _validated_schema_params(
                    _workflow_parameter_schema(
                        w,
                        list(definition.actions.values()),
                    ),
                    args.get("params"),
                )
                return _dump(
                    agent_confirmation_service.preview_workflow_run(
                        self.db,
                        w,
                        workflow_params,
                        runtime_definition=definition,
                    )
                )
            return _tool_error(
                "UNKNOWN_TOOL",
                "当前运行时未提供该工具；请只使用本轮工具定义中的名称。",
                retryable=False,
            )
        except _ToolContractError as exc:
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                _safe_message(exc, "工具参数不符合契约"),
                retryable=True,
            )
        except (business_query_service.BusinessQueryError, mapped_query_service.MappedQueryError) as exc:
            return _tool_error(
                "INVALID_QUERY",
                _safe_message(exc, "业务查询参数或映射不满足查询契约"),
                retryable=True,
            )
        except function_runtime_service.FunctionRuntimeError as exc:
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                _safe_message(exc, "函数参数不符合输入契约"),
                retryable=True,
            )
        except (
            agent_confirmation_service.AgentConfirmationError,
            PolicyViolation,
            runtime_definition_service.RuntimeDefinitionError,
        ) as exc:
            return _tool_error(
                "CAPABILITY_NOT_READY",
                _safe_message(exc, "业务能力当前不可执行"),
                retryable=True,
            )
        except PermissionError:
            return _tool_error(
                "FORBIDDEN",
                "当前用户无权使用该业务能力。",
                retryable=False,
            )
        except (TypeError, ValueError):
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                "工具参数不符合当前 schema；请先调用对应的 list 工具并修正后重试一次。",
                retryable=True,
            )
        except Exception:  # noqa: BLE001 - never expose arbitrary connector/runtime exceptions.
            return _tool_error(
                "TOOL_EXECUTION_FAILED",
                "工具执行失败；内部异常未暴露给对话。",
                retryable=False,
            )

    def _ontology_model(self) -> dict[str, Any]:
        """Return the governed schema view used by this Agent, without hidden properties."""
        if not self.scenario:
            return {"entities": [], "relations": []}
        visible_entity_ids = {entity.id for entity in self.entities}
        entities: list[dict[str, Any]] = []
        for entity in self.entities:
            properties = [
                prop
                for prop in entity.properties
                if permission_service.can_read_property(self.db, prop)
            ]
            entities.append(
                {
                    "id": entity.id,
                    "name": entity.name,
                    "description": entity.description,
                    "abstract": bool(entity.is_abstract),
                    "state_property": entity.state_property or "",
                    "properties": [
                        {
                            "name": prop.name,
                            "data_type": prop.data_type,
                            "description": prop.description,
                            "key": bool(prop.is_key),
                            "title": bool(getattr(prop, "is_title", False)),
                            "required": bool(prop.is_required),
                            "enum_values": prop.enum_values or [],
                        }
                        for prop in properties
                    ],
                }
            )
        relations: list[dict[str, Any]] = []
        for relation in self.relations:
            if (
                relation.source_entity_id not in visible_entity_ids
                or relation.target_entity_id not in visible_entity_ids
            ):
                continue
            constraints = ontology_service.normalize_relation_constraints(
                getattr(relation, "constraints", {}) or {},
                relation_type=relation.relation_type,
            )
            inverse_id = str(constraints.get("inverse_relation_id") or "")
            inverse = next((item for item in self.relations if item.id == inverse_id), None)
            relations.append({
                "id": relation.id,
                "name": relation.name,
                "description": relation.description,
                "source_entity_id": relation.source_entity_id,
                "source_entity": relation.source_entity.name if relation.source_entity else "",
                "target_entity_id": relation.target_entity_id,
                "target_entity": relation.target_entity.name if relation.target_entity else "",
                "cardinality": relation.relation_type,
                "constraints": constraints,
                "query_semantics": ontology_service.relation_query_semantics(
                    constraints,
                    inverse_relation_name=getattr(inverse, "name", ""),
                ),
            })
        return {"entities": entities, "relations": relations}

    def _ontology_model_tool(self, entity_ref: str = "") -> dict[str, Any]:
        """Return a compact catalog or one exact, field-complete entity view."""
        model = self._ontology_model()
        reference = str(entity_ref or "").strip()
        if reference:
            selected = _resource_by_reference(self.entities, reference, "对象类型")
            if selected is None:
                return {"entities": [], "relations": []}
            selected_id = str(selected.id)
            return {
                "entities": [
                    item
                    for item in model["entities"]
                    if str(item.get("id") or "") == selected_id
                ],
                "relations": [
                    item
                    for item in model["relations"]
                    if selected_id
                    in {
                        str(item.get("source_entity_id") or ""),
                        str(item.get("target_entity_id") or ""),
                    }
                ],
            }
        return {
            "entities": [
                {
                    "id": item["id"],
                    "api_name": _resource_api_name(entity),
                    "name": item["name"],
                    "property_count": len(item.get("properties") or []),
                }
                for entity, item in zip(self.entities, model["entities"], strict=True)
            ],
            "relations": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "source_entity": item["source_entity"],
                    "target_entity": item["target_entity"],
                    "cardinality": item["cardinality"],
                }
                for item in model["relations"]
            ],
        }

    def _visible_instance_attributes(
        self,
        instance: OntologyInstance,
        entity: Any | None = None,
    ) -> dict[str, Any]:
        """Intersect runtime-version properties with today's field ACL."""
        entity = entity or next(
            (item for item in self.entities if item.id == instance.entity_id), None
        )
        if not entity:
            return {}
        visible_names = {
            str(prop.name)
            for prop in getattr(entity, "properties", [])
            if permission_service.can_read_property(self.db, prop)
        }
        return {
            str(name): value
            for name, value in dict(instance.attributes or {}).items()
            if str(name) in visible_names
        }

    def _search_ontology(self, entity_name: str = "", query: str = "") -> list[dict[str, Any]]:
        """Search live object data through the one resolved runtime schema."""
        if not self.scenario:
            return []
        entity_query = entity_name.strip().casefold()
        text_query = query.strip().casefold()
        entity_by_id = {
            str(entity.id): entity
            for entity in self.entities
            if not entity_query or entity_query in str(entity.name).casefold()
        }
        if not entity_by_id:
            return []
        rows = self.db.execute(
            select(OntologyInstance)
            .where(
                OntologyInstance.scenario_id == self.scenario.id,
                OntologyInstance.entity_id.in_(set(entity_by_id)),
            )
            .order_by(OntologyInstance.created_at.desc())
            .limit(200)
        ).scalars().all()
        results: list[dict[str, Any]] = []
        for instance in rows:
            if (
                not self._object_in_data_context(instance)
                or not permission_service.check_object(self.db, instance, "read").allowed
            ):
                continue
            entity = entity_by_id.get(str(instance.entity_id))
            attributes = self._visible_instance_attributes(instance, entity)
            haystack = (
                f"{instance.name} "
                f"{json.dumps(attributes, ensure_ascii=False, default=str)}"
            ).casefold()
            if text_query and text_query not in haystack:
                continue
            results.append(
                {
                    "id": instance.id,
                    "name": instance.name,
                    "entity": entity.name if entity else "",
                    "entity_id": instance.entity_id,
                    "attributes": attributes,
                    "source": instance.source,
                    "source_ref": instance.source_ref,
                }
            )
            if len(results) >= get_settings().max_query_rows:
                break
        return results

    def _object_in_data_context(self, instance: OntologyInstance | None) -> bool:
        """Manual objects inherit the scenario; imported objects inherit their source binding."""
        if (
            not instance
            or instance.scenario_id != self.agent.scenario_id
            or instance.entity_id not in {entity.id for entity in self.entities}
        ):
            return False
        if not ontology_service.instance_in_runtime_definition(
            instance, self.runtime_definition
        ):
            return False
        if instance.source != "imported":
            return True
        metadata = instance.source_metadata if isinstance(instance.source_metadata, dict) else {}
        source_id = str(metadata.get("data_source_id") or "")
        return bool(
            source_id
            and source_id in {source.id for source in self.data_sources}
        )

    def _inferred_object_relations(
        self,
        instance: OntologyInstance,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Compute bounded query-time graph entailments over readable asserted edges."""
        relation_by_id = {str(item.id): item for item in self.relations}
        if not relation_by_id:
            return [], False
        maximum = max(1, min(get_settings().max_query_rows, 200))
        rows = list(
            self.db.execute(
                select(RelationInstance)
                .where(
                    RelationInstance.scenario_id == self.scenario.id,
                    RelationInstance.relation_id.in_(set(relation_by_id)),
                )
                .order_by(RelationInstance.created_at.asc(), RelationInstance.id.asc())
                .limit(maximum + 1)
            ).scalars().all()
        )
        rows = [
            row
            for row in rows
            if ontology_service.relation_instance_in_runtime_definition(
                row, self.runtime_definition
            )
        ]
        truncated = len(rows) > maximum
        rows = rows[:maximum]
        object_ids = {
            str(value)
            for row in rows
            for value in (row.source_instance_id, row.target_instance_id)
        }
        readable: dict[str, OntologyInstance] = {}
        for object_id in object_ids:
            candidate = self.db.get(OntologyInstance, object_id)
            if (
                candidate
                and self._object_in_data_context(candidate)
                and permission_service.check_object(self.db, candidate, "read").allowed
            ):
                readable[object_id] = candidate

        # (relation, source, target) -> asserted path and the inference(s) that
        # make this direct logical edge available. Asserted always wins.
        logical: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add_edge(
            relation_id: str,
            source_id: str,
            target_id: str,
            *,
            path: list[str],
            inference: str,
        ) -> None:
            if source_id not in readable or target_id not in readable:
                return
            key = (relation_id, source_id, target_id)
            current = logical.get(key)
            candidate = {"path": path, "inferences": {inference}, "asserted": inference == "asserted"}
            if current is None:
                logical[key] = candidate
                return
            current["inferences"].add(inference)
            if candidate["asserted"] or len(path) < len(current["path"]):
                current["path"] = path
            current["asserted"] = current["asserted"] or candidate["asserted"]

        asserted_by_relation: dict[str, list[RelationInstance]] = defaultdict(list)
        for row in rows:
            relation_id = str(row.relation_id)
            asserted_by_relation[relation_id].append(row)
            add_edge(
                relation_id,
                str(row.source_instance_id),
                str(row.target_instance_id),
                path=[str(row.id)],
                inference="asserted",
            )

        constraints_by_relation: dict[str, dict[str, Any]] = {}
        for relation_id, relation in relation_by_id.items():
            constraints = ontology_service.normalize_relation_constraints(
                getattr(relation, "constraints", {}) or {},
                relation_type=relation.relation_type,
            )
            constraints_by_relation[relation_id] = constraints
            if constraints.get("symmetric"):
                for row in asserted_by_relation.get(relation_id, []):
                    add_edge(
                        relation_id,
                        str(row.target_instance_id),
                        str(row.source_instance_id),
                        path=[str(row.id)],
                        inference="symmetric",
                    )

        # An inverse declaration is bidirectional even when only one side names
        # the other. Build query edges from asserted rows on both definitions.
        for relation_id, constraints in constraints_by_relation.items():
            inverse_id = str(constraints.get("inverse_relation_id") or "")
            if inverse_id not in relation_by_id:
                continue
            for row in asserted_by_relation.get(relation_id, []):
                add_edge(
                    inverse_id,
                    str(row.target_instance_id),
                    str(row.source_instance_id),
                    path=[str(row.id)],
                    inference="inverse",
                )
            for row in asserted_by_relation.get(inverse_id, []):
                add_edge(
                    relation_id,
                    str(row.target_instance_id),
                    str(row.source_instance_id),
                    path=[str(row.id)],
                    inference="inverse",
                )

        # Transitive closure is bounded to readable logical direct edges and is
        # returned as evidence paths; no inferred edge is persisted.
        for relation_id, constraints in constraints_by_relation.items():
            if not constraints.get("transitive"):
                continue
            adjacency: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
            for (edge_relation_id, source_id, target_id), edge in list(logical.items()):
                if edge_relation_id == relation_id:
                    adjacency[source_id].append((target_id, list(edge["path"])))
            for start in list(readable):
                queue = deque((target, path) for target, path in adjacency.get(start, []))
                shortest: dict[str, list[str]] = {}
                while queue:
                    target, path = queue.popleft()
                    if target == start or target in shortest or len(path) > maximum:
                        continue
                    shortest[target] = path
                    for next_target, next_path in adjacency.get(target, []):
                        queue.append((next_target, [*path, *next_path]))
                for target, path in shortest.items():
                    if len(path) < 2:
                        continue
                    add_edge(
                        relation_id,
                        start,
                        target,
                        path=path,
                        inference="transitive",
                    )

        inferred: list[dict[str, Any]] = []
        for (relation_id, source_id, target_id), edge in logical.items():
            if edge["asserted"]:
                continue
            if instance.id == source_id:
                direction, related_id = "outgoing", target_id
            elif instance.id == target_id:
                direction, related_id = "incoming", source_id
            else:
                continue
            related = readable.get(related_id)
            relation = relation_by_id.get(relation_id)
            if not related or not relation:
                continue
            related_entity = next(
                (item for item in self.entities if item.id == related.entity_id), None
            )
            inferred.append({
                "relation_id": relation_id,
                "relation": relation.name,
                "direction": direction,
                "related_object_id": related.id,
                "related_object_name": related.name,
                "related_entity": related_entity.name if related_entity else "",
                "inferred": True,
                "materialized": False,
                "inference": sorted(edge["inferences"] - {"asserted"}),
                "path": list(edge["path"]),
            })
        inferred.sort(key=lambda item: (
            item["relation"], item["direction"], item["related_object_name"], item["related_object_id"]
        ))
        if len(inferred) > maximum:
            truncated = True
            inferred = inferred[:maximum]
        return inferred, truncated

    def _ontology_object(self, object_id: str) -> dict[str, Any]:
        """Read one object plus only relationships whose opposite object is readable."""
        if not self.scenario or not object_id:
            return {"error": "请提供对象实例 id"}
        instance = self.db.get(OntologyInstance, object_id)
        if not instance or instance.scenario_id != self.scenario.id:
            return {"error": "对象实例不存在或不属于当前业务场景"}
        if not self._object_in_data_context(instance):
            return {"error": "对象实例不在当前 Agent 绑定的数据范围内"}
        if not permission_service.check_object(self.db, instance, "read").allowed:
            return {"error": "没有读取该对象实例的权限"}
        entity = next((item for item in self.entities if item.id == instance.entity_id), None)
        relation_rows = list(
            self.db.execute(
                select(RelationInstance).where(
                    RelationInstance.scenario_id == self.scenario.id,
                    or_(
                        RelationInstance.source_instance_id == instance.id,
                        RelationInstance.target_instance_id == instance.id,
                    ),
                ).limit(max(1, min(get_settings().max_query_rows, 200)))
            ).scalars().all()
        )
        relations: list[dict[str, Any]] = []
        for relation_instance in relation_rows:
            if not ontology_service.relation_instance_in_runtime_definition(
                relation_instance, self.runtime_definition
            ):
                continue
            outgoing = relation_instance.source_instance_id == instance.id
            related_id = (
                relation_instance.target_instance_id
                if outgoing
                else relation_instance.source_instance_id
            )
            related = self.db.get(OntologyInstance, related_id)
            if (
                not related
                or related.scenario_id != self.scenario.id
                or not self._object_in_data_context(related)
                or not permission_service.check_object(self.db, related, "read").allowed
            ):
                continue
            related_entity = next(
                (item for item in self.entities if item.id == related.entity_id), None
            )
            relation = next(
                (item for item in self.relations if item.id == relation_instance.relation_id),
                None,
            )
            if not relation:
                continue
            relations.append(
                {
                    "id": relation_instance.id,
                    "relation_id": relation.id,
                    "relation": relation.name,
                    "direction": "outgoing" if outgoing else "incoming",
                    "related_object_id": related.id,
                    "related_object_name": related.name,
                    "related_entity": related_entity.name if related_entity else "",
                    "attributes": relation_instance.attributes or {},
                }
            )
        inferred_relations, inference_truncated = self._inferred_object_relations(instance)
        return {
            "id": instance.id,
            "name": instance.name,
            "entity_id": instance.entity_id,
            "entity": entity.name if entity else "",
            "attributes": self._visible_instance_attributes(instance, entity),
            "state": (
                instance.state or ""
                if entity
                and getattr(entity, "state_property", "")
                in self._visible_instance_attributes(instance, entity)
                else ""
            ),
            "source": instance.source,
            "source_ref": instance.source_ref,
            "relations": relations,
            "inferred_relations": inferred_relations,
            "inference_truncated": inference_truncated,
            "relation_semantics": "relations 仅含已断言边；inferred_relations 是受权限与行数限制的查询期推理，未写入数据库。",
        }

    def _mapping_catalog(self) -> list[dict[str, Any]]:
        """Expose semantic-to-physical mappings, never connector credentials/config."""
        source_by_id = {source.id: source for source in self.data_sources}
        result: list[dict[str, Any]] = []
        for mapping in self.mappings:
            source = source_by_id.get(mapping.data_source_id)
            entity = getattr(mapping, "entity", None) or next(
                (item for item in self.entities if item.id == mapping.entity_id), None
            )
            if not source or not entity or entity.scenario_id != self.agent.scenario_id:
                continue
            visible_property_names = {
                prop.name
                for prop in entity.properties
                if permission_service.can_read_property(self.db, prop)
            }
            column_map = {
                str(property_name): str(column_name)
                for property_name, column_name in (mapping.column_map or {}).items()
                if property_name in visible_property_names
            }
            transform_operations = {
                str(property_name): [
                    str(rule.get("op") or "")
                    for rule in rules
                    if isinstance(rule, dict) and rule.get("op")
                ]
                for property_name, rules in (mapping.transform_rules or {}).items()
                if property_name in visible_property_names and isinstance(rules, list)
            }
            result.append(
                {
                    "kind": "object",
                    "id": mapping.id,
                    "entity_id": entity.id,
                    "entity": entity.name,
                    "data_source_id": source.id,
                    "data_source": source.name,
                    "data_source_connector_revision": int(
                        source.connector_revision or 0
                    ),
                    "definition_hash": (
                        getattr(self, "runtime_definition", None).definition_hash
                        if getattr(self, "runtime_definition", None) else ""
                    ),
                    "table": mapping.table_name,
                    "column_map": column_map,
                    "transform_operations": transform_operations,
                    "status": getattr(mapping, "status", "released"),
                    "last_refreshed_at": getattr(mapping, "last_refreshed_at", None),
                    "last_imported_count": getattr(mapping, "last_imported_count", 0),
                }
            )
        relation_by_id = {str(item.id): item for item in self.relations}
        object_mapping_by_id = {str(item.id): item for item in self.mappings}
        for mapping in self.relation_mappings:
            relation = relation_by_id.get(str(mapping.relation_id))
            source_mapping = object_mapping_by_id.get(str(mapping.source_mapping_id))
            target_mapping = object_mapping_by_id.get(str(mapping.target_mapping_id))
            source = source_by_id.get(mapping.data_source_id)
            if not relation or not source_mapping or not target_mapping or not source:
                continue
            result.append(
                {
                    "kind": "relation",
                    "id": mapping.id,
                    "relation_id": relation.id,
                    "relation": relation.name,
                    "source_mapping_id": source_mapping.id,
                    "source_entity_id": source_mapping.entity_id,
                    "target_mapping_id": target_mapping.id,
                    "target_entity_id": target_mapping.entity_id,
                    "mode": mapping.mode,
                    "data_source_id": source.id,
                    "data_source_connector_revision": int(
                        source.connector_revision or 0
                    ),
                    "definition_hash": (
                        getattr(self, "runtime_definition", None).definition_hash
                        if getattr(self, "runtime_definition", None) else ""
                    ),
                    "table": mapping.table_name,
                    "foreign_key_column": getattr(mapping, "foreign_key_column", "") or "",
                    "source_key_column": getattr(mapping, "source_key_column", "") or "",
                    "target_key_column": getattr(mapping, "target_key_column", "") or "",
                    "semantics": "仅按此显式映射生成关系实例；不会按列名猜测关系。",
                }
            )
        return result

    def _ds(self, ds_id: str | None) -> DataSource | None:
        for d in self.data_sources:
            if d.id == ds_id:
                return d
        return None

    def validate_sql_query(self, data_source_id: str | None, sql: str) -> str:
        """Prove a query stays inside this Agent's current governed mapping scope."""
        if not self._ds(data_source_id):
            raise PermissionError("Agent 未绑定该数据源")
        sql_scope: dict[str, set[str]] = {}
        for mapping in self._mapping_catalog():
            if mapping.get("kind") != "object":
                continue
            if mapping["data_source_id"] != data_source_id or not mapping["table"]:
                continue
            sql_scope.setdefault(str(mapping["table"]), set()).update(
                str(column) for column in mapping["column_map"].values() if column
            )
        return validate_agent_sql_scope(sql, sql_scope)

    @staticmethod
    def _parsed_tool_result(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    def authorize_historic_tool_result(
        self,
        name: str,
        args: dict[str, Any],
        raw_result: Any,
    ) -> bool:
        """Re-authorize one persisted result before display or LLM replay.

        Tool output is a data snapshot, not an evergreen permission grant.  The
        check is intentionally conservative: an unknown/legacy shape is hidden
        rather than replayed after a binding or ACL may have changed.
        """
        result = self._parsed_tool_result(raw_result)
        if result is None:
            return False
        # Some persisted-history and migration tests construct a lightweight
        # AgentContext without the optional runtime definition. Treat that as
        # an unavailable binding and keep the authorization check fail-closed.
        runtime_definition = getattr(self, "runtime_definition", None)
        current_definition_hash = str(
            getattr(runtime_definition, "definition_hash", "") or ""
        )
        # A persisted failure can be replayed only when it is one of our small,
        # server-authored envelopes and the tool is still exposed to this Agent.
        # This lets the model see a recoverable schema error on the next turn
        # without turning arbitrary exception text into an evergreen disclosure.
        if _is_safe_tool_error(result):
            current_tools = {
                str(tool.get("function", {}).get("name") or "")
                for tool in self.build_tools()
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
            return name in current_tools
        if name == "list_ontology_model":
            if not isinstance(result, dict):
                return False
            entity_ref = str(args.get("entity") or "").strip()
            current_tool_result = self._ontology_model_tool(entity_ref)
            if result == current_tool_result:
                return True
            if entity_ref:
                return False
            # Keep pre-catalog history available when its full schema remains
            # a subset of today's governed model. New calls always use the
            # compact catalog above.
            legacy_entities = result.get("entities")
            legacy_relations = result.get("relations")
            if not isinstance(legacy_entities, list) or not legacy_entities:
                return False
            if not isinstance(legacy_relations, list):
                return False
            current = self._ontology_model()
            current_entities = {
                str(item["id"]): item
                for item in current["entities"]
            }
            for item in legacy_entities:
                if not isinstance(item, dict):
                    return False
                current_entity = current_entities.get(str(item.get("id") or ""))
                if current_entity is None or set(item) != set(current_entity):
                    return False
                if any(
                    key != "properties" and item.get(key) != current_entity.get(key)
                    for key in current_entity
                ):
                    return False
                properties = item.get("properties")
                if not isinstance(properties, list) or any(
                    not isinstance(prop, dict)
                    or prop not in current_entity["properties"]
                    for prop in properties
                ):
                    return False
            current_relations = {
                str(item["id"]): item for item in current["relations"]
            }
            return all(
                isinstance(item, dict)
                and current_relations.get(str(item.get("id") or "")) == item
                for item in legacy_relations
            )
        if name in {"search_ontology", "get_ontology_object"}:
            items = result if isinstance(result, list) else [result]
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    return False
                current = self._ontology_object(str(item["id"]))
                if current.get("error"):
                    return False
                old_attributes = item.get("attributes") or {}
                if not isinstance(old_attributes, dict) or not set(old_attributes).issubset(
                    set(current.get("attributes") or {})
                ):
                    return False
                for relation in item.get("relations", []) or []:
                    if not isinstance(relation, dict):
                        return False
                    current_relation_ids = {
                        str(current_relation.get("id") or "")
                        for current_relation in current.get("relations", [])
                    }
                    if str(relation.get("id") or "") not in current_relation_ids:
                        return False
                current_inferred = {
                    (
                        str(current_relation.get("relation_id") or ""),
                        str(current_relation.get("direction") or ""),
                        str(current_relation.get("related_object_id") or ""),
                        tuple(str(value) for value in (current_relation.get("path") or [])),
                    )
                    for current_relation in current.get("inferred_relations", []) or []
                    if isinstance(current_relation, dict)
                }
                for relation in item.get("inferred_relations", []) or []:
                    if not isinstance(relation, dict) or not relation.get("inferred"):
                        return False
                    signature = (
                        str(relation.get("relation_id") or ""),
                        str(relation.get("direction") or ""),
                        str(relation.get("related_object_id") or ""),
                        tuple(str(value) for value in (relation.get("path") or [])),
                    )
                    if signature not in current_inferred:
                        return False
            return True
        if name == "list_data_sources":
            allowed = {
                str(source.id): int(source.connector_revision or 0)
                for source in self.data_sources
            }
            return isinstance(result, list) and all(
                isinstance(item, dict)
                and str(item.get("id") or "") in allowed
                and item.get("connector_revision")
                == allowed[str(item.get("id") or "")]
                for item in result
            )
        if name == "list_tables":
            source_id = str(args.get("data_source_id") or "")
            source = self._ds(source_id)
            if source is None or not isinstance(result, list):
                return False
            for table in result:
                if not isinstance(table, dict) or not str(table.get("name") or ""):
                    return False
                if (
                    str(table.get("data_source_id") or "") != source_id
                    or table.get("connector_revision")
                    != int(source.connector_revision or 0)
                ):
                    return False
                if not isinstance(table.get("columns", []), list):
                    return False
                if not all(
                    isinstance(column, dict) and str(column.get("name") or "")
                    for column in table.get("columns", [])
                ):
                    return False
            return True
        if name == "query_mapped_objects":
            try:
                plan = mapped_query_service.prepare_query(
                    self.db,
                    definition=runtime_definition,
                    mappings=self.mappings,
                    data_sources=self.data_sources,
                    args=args,
                )
            except Exception:  # noqa: BLE001 - authorization is fail closed.
                return False
            return mapped_query_service.authorize_historic_result(plan, result)
        if name == "query_business_data":
            try:
                plan = business_query_service.prepare_query(
                    self.db,
                    definition=runtime_definition,
                    mappings=self.mappings,
                    data_sources=self.data_sources,
                    args=args,
                )
            except Exception:  # noqa: BLE001 - authorization is fail closed.
                return False
            return business_query_service.authorize_historic_result(plan, result)
        if name == "run_medical_audit":
            if not self.scenario or self.scenario.namespace != "medical_audit":
                return False
            try:
                mapping_contract = self._medical_audit_mapping_contract()
                return medical_audit_service.authorize_historic_result(
                    mapping_contract,
                    args,
                    result,
                    property_access=self._medical_audit_access_policy(),
                )
            except medical_audit_service.MedicalAuditError:
                return False
        if name == "run_sql":
            # Direct SQL is no longer an exposed Agent tool and legacy results
            # did not persist a connector revision. The UI may show the durable
            # transcript, but no such payload is trusted for model replay.
            return False
        if name == "list_data_mappings":
            current = {str(item["id"]): item for item in self._mapping_catalog()}
            if not isinstance(result, list):
                return False
            for item in result:
                if not isinstance(item, dict) or str(item.get("id") or "") not in current:
                    return False
                current_item = current[str(item["id"])]
                if (
                    not current_definition_hash
                    or str(item.get("definition_hash") or "")
                    != current_definition_hash
                    or item.get("data_source_connector_revision")
                    != current_item.get("data_source_connector_revision")
                ):
                    return False
                if str(item.get("kind") or "") != str(current_item.get("kind") or ""):
                    return False
                if item.get("kind") != "object":
                    # Relation mappings carry no object-property column map.
                    # Their definition fingerprint and connector revision above
                    # are the replay boundary; never index a missing field.
                    continue
                old_map = item.get("column_map") or {}
                if not isinstance(old_map, dict) or any(
                    str(key) not in current_item["column_map"]
                    or str(value) != str(current_item["column_map"][str(key)])
                    for key, value in old_map.items()
                ):
                    return False
            return True

        catalogs: dict[str, set[str]] = {
            "list_functions": {str(item.id) for item in self.functions},
            "list_actions": {str(item.id) for item in self.actions},
            "list_rules": {str(item.id) for item in self.rules},
            "list_events": {str(item.id) for item in self.events},
            "list_workflows": {str(item.id) for item in self.workflows},
        }
        if name in catalogs:
            return isinstance(result, list) and all(
                isinstance(item, dict)
                and str(item.get("id") or "") in catalogs[name]
                and bool(current_definition_hash)
                and str(item.get("definition_hash") or "") == current_definition_hash
                for item in result
            )
        resource_calls = {
            "run_function": ("function_id", self.functions, "函数"),
            "execute_action": ("action_id", self.actions, "操作"),
            "evaluate_rule": ("rule_id", self.rules, "规则"),
            "prepare_event_publish": ("event_id", self.events, "事件"),
            "execute_workflow": ("workflow_id", self.workflows, "工作流"),
        }
        if name in resource_calls:
            key, resources, label = resource_calls[name]
            try:
                resource = _resource_by_reference(resources, args.get(key), label)
            except _ToolContractError:
                return False
            return (
                resource is not None
                and isinstance(result, dict)
                and bool(current_definition_hash)
                and str(result.get("definition_hash") or "")
                == current_definition_hash
            )
        # Document tools require citation-level version checks in the router.
        if name in {"search_documents", "read_document"}:
            return isinstance(result, dict)
        return False

    def _read_doc(self, file_id: str = "", filename: str = "") -> str:
        """读取资料库全文时附带可持久化引用，避免全文工具绕过引用审计。"""
        bound_ids = [d.id for d in self.data_sources if d.type == "file_bucket"]
        if not bound_ids:
            return _tool_error(
                "CAPABILITY_NOT_READY",
                "当前 Agent 未绑定可检索资料库。",
                retryable=False,
            )
        stmt = select(BucketFile).where(
            BucketFile.data_source_id.in_(bound_ids),
        )
        if file_id:
            stmt = stmt.where(BucketFile.id == file_id)
        elif filename:
            stmt = stmt.where(BucketFile.filename == filename)
        else:
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                "请先调用 search_documents，并提供其返回的 file_id。",
                retryable=True,
            )
        stmt = stmt.join(DataSource, DataSource.id == BucketFile.data_source_id).where(
            tenant_service.visible_clause(DataSource, self.db)
        )
        matches = self.db.execute(stmt).scalars().all()
        if filename and len(matches) > 1:
            return _tool_error(
                "INVALID_TOOL_ARGUMENTS",
                "存在同名资料，请先使用 search_documents 返回的 file_id 精确读取。",
                retryable=True,
            )
        f = matches[0] if matches else None
        if not f:
            return _tool_error(
                "RESOURCE_NOT_FOUND",
                "未找到或无权读取所请求的资料文件；请重新检索后重试。",
                retryable=True,
            )
        content = (f.parsed_text or f"文件 {f.filename} 暂无解析内容")[:24_000]
        source = next((item for item in self.data_sources if item.id == f.data_source_id), None)
        citation = self._record_citations(
            [
                {
                    "file_id": f.id,
                    "filename": f.filename,
                    "data_source_id": f.data_source_id,
                    "data_source_name": source.name if source else "资料库",
                    "char_start": 0,
                    "char_end": len(content),
                    # 全文读取并不等于某个单独分块；保留文件版本哈希和快照，以便
                    # 历史会话仍可审计且不会被重索引后的字符偏移误导。
                    "content_hash": f.indexed_content_hash or "",
                    "file_content_hash": f.indexed_content_hash or "",
                    "index_version": f.index_version or "",
                    "text": content,
                }
            ]
        )
        if not citation:
            return _tool_error(
                "CAPABILITY_NOT_READY",
                "资料文件当前没有可审计的引用版本，请先完成解析和索引。",
                retryable=False,
            )
        return _dump(
            {
                "citation": citation[0],
                "content": content,
                "truncated": len(f.parsed_text or "") > len(content),
                "instruction": "最终回答引用该资料事实时必须标注 citation_id。",
            }
        )

    def _save_deliverable(self, filename: str, content: str) -> str:
        """Legacy direct-write hook retained as a non-executing compatibility response."""
        return _tool_error(
            "DIRECT_TOOL_DISABLED",
            "Agent 不直接写入文件桶；请通过场景操作配置文件交付，并由用户在操作页或任务中心完成确认。",
            retryable=False,
        )

    def _exec_skill(self, skill_name: str, args: list[str]) -> str:
        return "Agent 不直接执行本地技能；请通过场景操作配置技能并由用户确认。"

    def _exec_mcp(self, tool_name: str, args: dict[str, Any]) -> str:
        return "Agent 不直接调用外部工具服务；请通过场景操作配置工具并由用户确认。"


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    required = [k for k, v in properties.items() if v.get("required")]
    normalized_properties = {
        key: {field: value for field, value in definition.items() if field != "required"}
        for key, definition in properties.items()
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": normalized_properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _table_map(ctx: AgentContext) -> str:
    """Use governed DataMapping definitions as the authoritative table map."""
    lines: list[str] = []
    source_by_id = {source.id: source for source in ctx.data_sources}
    for mapping in ctx.mappings:
        if not mapping.table_name:
            continue
        entity = mapping.entity or ctx.db.get(OntologyEntity, mapping.entity_id)
        source = source_by_id.get(mapping.data_source_id)
        if not entity or not source:
            continue
        lines.append(
            f"- 数据源「{source.name}」(id={source.id})："
            f"对象类型「{entity.name}」→ 表 `{mapping.table_name}`"
        )
    if not lines:
        return ""
    return "\n".join(sorted(set(lines)))


# ──────────────────────────────────────────────
# 系统提示词构建
# ──────────────────────────────────────────────
def _capability_prompt_state(ctx: AgentContext, kind: str, resource: Any) -> str:
    readiness = ctx._capability_status(kind, resource)
    if readiness.executable:
        return "可执行"
    return "不可执行；阻塞原因：" + "；".join(readiness.blocked_reasons[:3])


def build_system_prompt(ctx: AgentContext, scenario_name: str, ontology_summary: str) -> str:
    base = ctx.agent.system_prompt or "你是一名专业的业务智能助手。"
    # Some persisted Agents predate the governed runtime and still mention
    # direct network/side-effect tools. Keep the business intent, but rewrite
    # the old database-tool wording to the semantic query capability exposed
    # by the current runtime.
    base = base.replace("run_sql", "query_business_data")
    for legacy_tool in ("save_deliverable", "search_web", "fetch_url", "execute_skill", "mcp_*"):
        base = base.replace(legacy_tool, "当前运行时不可用的旧工具")
    parts = [base, ""]
    if ctx.agent.description:
        parts.append(f"【Agent 职责】{ctx.agent.description}")
    if scenario_name:
        parts.append(f"【当前业务场景】{scenario_name}")
    if ontology_summary:
        parts.append("\n【业务本体（领域模型）】\n" + ontology_summary)
        parts.append(
            "你可以用 list_ontology_model 查看完整可见模型，用 search_ontology 查找对象，"
            "再用 get_ontology_object 查看对象之间的实际关系。"
        )
    if ctx.data_sources:
        ds_lines = [f"- {d.name}（{d.type}，id={d.id}）" for d in ctx.data_sources]
        parts.append("\n【可用数据源】\n" + "\n".join(ds_lines))
        if ctx.mappings:
            parts.append(
                "简单的单对象查询优先使用 query_mapped_objects，按对象类型和本体属性传入结构化条件；"
                "涉及跨对象关联、分组、去重或聚合审计时使用 query_business_data。"
                "所有查询参数都使用对象类型和本体属性，不要编写 SQL 或传递物理表/列名。"
            )
        if any(source.type == "file_bucket" for source in ctx.data_sources):
            parts.append(
                "用 search_documents 检索文件桶中的文档。凡引用检索到的事实，必须在答案中标出"
                "工具结果提供的【C#】；未检索到依据时明确说明。"
            )
        table_map = _table_map(ctx)
        if table_map:
            parts.append(
                "\n【实体 → 数据库表名映射血缘】（由服务端固定；调用 query_mapped_objects 时无需也不得传物理名称）\n"
                + table_map
            )
    if ctx.mappings:
        parts.append(
            "\n【数据映射】\n"
            + "\n".join(
                f"- {mapping.entity.name if mapping.entity else mapping.entity_id} ← "
                f"{next((source.name for source in ctx.data_sources if source.id == mapping.data_source_id), mapping.data_source_id)}"
                f" / {mapping.table_name or '未指定表'}（{len(mapping.column_map or {})} 个字段，"
                f"状态 {getattr(mapping, 'status', 'released')}）"
                for mapping in ctx.mappings
            )
            + "\n优先用 query_mapped_objects 或 query_business_data 按本体属性查询；list_data_mappings 仅用于查看映射血缘。"
        )
    if ctx.functions:
        parts.append(
            "\n【业务函数】\n"
            + "\n".join(
                f"- {function.name}（id={function.id}，{function.runtime_kind}，"
                f"{_capability_prompt_state(ctx, 'function', function)}）: "
                f"{function.description[:60]}"
                for function in ctx.functions
            )
            + "\n用 list_functions 查看输入输出字段；只有标记为可运行的确定性函数才能用 run_function 调用。"
        )
    if ctx.actions:
        parts.append(
            "\n【操作能力（包含未就绪定义）】\n"
            + "\n".join(
                f"- {a.name}（id={a.id}，{a.entity.name if a.entity else '?'}，{a.executor_type}，"
                f"{_capability_prompt_state(ctx, 'action', a)}）: {a.description[:60]}"
                + (f"；前置条件：{a.precondition[:100]}" if a.precondition else "")
                + (f"；完成后：{a.postcondition[:100]}" if a.postcondition else "")
                for a in ctx.actions
            )
            + "\n用 list_actions 查看就绪状态、前置/后置条件和 requires_confirmation；只有 executable=true 的操作可执行。"
            "requires_confirmation=false 的操作会直接执行；为 true 时先生成服务端固定预演，用户需在当前对话回复“确认执行”。"
        )
    if ctx.rules:
        parts.append(
            "\n【业务规则（Rules）】\n"
            + "\n".join(
                f"- {r.name}（{r.severity}，{_capability_prompt_state(ctx, 'rule', r)}）: {r.description[:60]}"
                for r in ctx.rules
            )
            + "\n用 list_rules 查看规则就绪状态；命中规则只产生待预演/待确认的操作清单，不会自动产生副作用。"
        )
    if ctx.events:
        parts.append(
            "\n【业务事件（Events）】\n"
            + "\n".join(
                f"- {event.name}（id={event.id}，{_capability_prompt_state(ctx, 'event', event)}）: "
                f"{event.description[:60]}"
                for event in ctx.events
            )
            + "\n用 list_events 查看事件载荷与就绪状态；prepare_event_publish 生成服务端固定预演，不会发布事件，用户需在当前对话回复“确认发布”。"
        )
    if ctx.workflows:
        parts.append(
            "\n【工作流（Workflows）】\n"
            + "\n".join(
                f"- {w.name}（id={w.id}，{w.trigger_type}，{w.status}，"
                f"{len(w.nodes or []) or len(w.steps or [])}节点，"
                f"{_capability_prompt_state(ctx, 'workflow', w)}）: {w.description[:60]}"
                for w in ctx.workflows
            )
            + "\n用 list_workflows 查看工作流就绪状态；execute_workflow 生成服务端固定预演，不会提交任务，用户需在当前对话回复“确认提交”。"
        )
    parts.append(
        "\n【工作方式】请根据用户问题，自主调用合适的工具获取数据，然后给出准确、结构化的回答。"
        "涉及数据时务必基于工具返回的真实数据，不要编造；无法确认时明确说明数据缺口。"
        "如果场景定义了本体、映射、函数、操作、规则、事件或工作流，优先使用这些业务抽象来完成任务。"
        "需要确认本体字段时，先无参数调用 list_ontology_model 发现对象，再用 entity 精确读取目标对象的完整属性和相关关系。"
        "用户在当前请求中明确给出的对象、范围、条件和阈值，就是本次审计的有效规则；"
        "可以用规则目录补充解释，但不能因为目录中没有同名规则而拒绝按用户条件查询。"
    )
    parts.append(
        "\n【业务查询策略】遇到‘超过次数/合计金额/重复记录/按对象统计’等复合问题时，"
        "先用 query_business_data 做只返回分组属性和聚合值的汇总查询；有明确阈值时用 having 筛掉不满足阈值的分组，"
        "再用得到的对象标识查询明细；"
        "不要把明细属性和聚合混在一个不完整的分组查询里。跨对象查询优先使用已配置的数据关系映射，"
        "没有可验证的关系或用户要求的维度未映射时，明确指出配置缺口并停止猜测。"
        "首页查询（offset 为 0）成功且 row_count=0（或 empty=true）表示该数据范围内没有命中记录："
        "必须明确回答‘在该范围内未发现违规’，不得把合法的零行结果说成缺少数据、缺少规则或执行失败；"
        "后续页 offset>0 且 row_count=0 只表示分页结束。只有结构化 error 才表示工具失败。"
    )
    parts.append(
        "\n【运行时工具与交付约束（优先级高于旧提示）】\n"
        "只使用本轮工具定义中实际提供的工具。数据库查询只能通过本轮提供的 query_mapped_objects 或"
        "query_business_data；不要调用或声称调用本轮未提供的网络爬虫或外部服务工具；如果用户要求的能力当前不可用，"
        "要明确说明阻塞点，不要反复尝试。\n"
        "工具调用阶段不要向用户输出“让我继续查询”“我现在查看”等过程性文字；完成必要查询后，"
        "只输出一份最终结果。不要重复调用同一个工具和同一组参数，达到足够证据后立即结束工具调用。\n"
        "id 是运行时内部细节。调用函数、操作、规则、事件或工作流时，先用对应的 list_* 工具按名称/API 名称"
        "发现资源和准确 schema，再把 list 返回的 id、api_name 或 name 传给执行工具；不得向用户索要内部 ID。\n"
        "若工具返回 retryable=true 的结构化 schema/参数错误，应依据 error.message 或 list_* 返回的 schema 修正参数，"
        "最多重试一次且不得原样重复；retryable=false 时停止重试并说明阻塞点。\n"
        "凡用户要求报告、附注、财务报表或其他附件交付，必须先调用 list_actions，找到相应的模板执行器操作，"
        "再按每个操作的 input_schema 实际调用 execute_action；服务端会根据人工确认配置执行或生成可确认预演，"
        "不得只写一段 Markdown、伪造下载链接或声称附件已生成。"
        "如果没有已就绪的模板操作，要明确指出缺少哪类模板操作。\n"
        "涉及审计、排查或核验时，最终结果必须包含：判断依据、数据范围、明细或明确的无结果说明、"
        "统计汇总、证据引用、数据限制和结论。若无法完成，必须明确指出缺少哪个字段、映射或能力，"
        "并停止继续猜测。"
    )
    return "\n".join(parts)


def ontology_summary_for(scenario, *, db: Session | None = None) -> str:
    """把本体（实体/属性/关系）序列化为给 LLM 的领域模型描述。"""
    if not scenario or not scenario.entities:
        return ""
    lines = []
    for e in scenario.entities:
        props = ", ".join(
            f"{p.name}:{p.data_type}"
            + ("(主键)" if p.is_key else "")
            + ("(标题)" if getattr(p, "is_title", False) else "")
            for p in e.properties
            if db is None or permission_service.can_read_property(db, p)
        )
        lines.append(f"- 实体「{e.name}」: {props or '无属性'}")
        if e.description:
            lines.append(f"  说明: {e.description}")
    for r in scenario.relations:
        src = next((e.name for e in scenario.entities if e.id == r.source_entity_id), "?")
        tgt = next((e.name for e in scenario.entities if e.id == r.target_entity_id), "?")
        constraints = ontology_service.normalize_relation_constraints(
            getattr(r, "constraints", {}) or {}, relation_type=r.relation_type
        )
        constraint_names = [
            label for key, label in (
                ("symmetric", "对称"), ("transitive", "传递"),
                ("irreflexive", "反自反"), ("asymmetric", "非对称"),
                ("antisymmetric", "反对称"), ("acyclic", "无环"),
            ) if constraints.get(key)
        ]
        suffix = f"；约束：{'、'.join(constraint_names)}" if constraint_names else ""
        if constraints.get("inverse_relation_id"):
            inverse = next(
                (item for item in scenario.relations if item.id == constraints["inverse_relation_id"]),
                None,
            )
            suffix += f"；逆关系：{getattr(inverse, 'name', constraints['inverse_relation_id'])}"
        lines.append(f"- 关系: {src} --[{r.name}]--({r.relation_type})--> {tgt}{suffix}")
        if constraints.get("symmetric") or constraints.get("transitive") or constraints.get("inverse_relation_id"):
            lines.append("  查询语义：推理边只在查询时解释，不会自动创建反向边、逆关系实例或传递闭包。")
    # 本体扩展维度
    if getattr(scenario, "function_definitions", None):
        executable = [function for function in scenario.function_definitions if function.runtime_kind != "contract"]
        if executable:
            lines.append("\n【业务函数（Functions）】")
            for function in executable:
                lines.append(f"- 函数「{function.name}」({function.runtime_kind}): {function.description[:60]}")
    if getattr(scenario, "actions", None):
        lines.append("\n【操作】")
        for a in scenario.actions:
            if db is not None and not permission_service.check_action(db, a, "read").allowed:
                continue
            ent = next((e.name for e in scenario.entities if e.id == a.entity_id), "?")
            lines.append(
                f"- 操作「{a.name}」(实体:{ent}, 执行:{a.executor_type}, "
                f"{'已启用' if a.enabled else '已停用'}): {a.description[:60]}"
            )
    if getattr(scenario, "rules", None):
        lines.append("\n【规则（Rules）】")
        for r in scenario.rules:
            lines.append(f"- 规则「{r.name}」({r.severity}): {r.description[:60]}")
    if getattr(scenario, "events", None):
        lines.append("\n【事件（Events）】")
        for e in scenario.events:
            lines.append(
                f"- 事件「{e.name}」({'已启用' if e.enabled else '已停用'}): {e.description[:60]}"
            )
    if getattr(scenario, "workflows", None):
        lines.append("\n【工作流（Workflows）】")
        for w in scenario.workflows:
            if db is not None and not permission_service.check_workflow(db, w, "read").allowed:
                continue
            lines.append(
                f"- 工作流「{w.name}」({w.trigger_type}, {w.status}, "
                f"{len(w.nodes or []) or len(w.steps or [])}节点): {w.description[:60]}"
            )
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────────
def _run_agent(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    history: list[dict[str, Any]],
    user_message: str,
    scenario_name: str,
    ontology_summary: str,
    *,
    runtime_context: AgentContext | None = None,
) -> Iterator[dict[str, Any]]:
    """执行 Agent 工具循环，逐事件 yield。

    事件类型: status / tool_call / tool_result / token / citations / done / error
    """
    # The router uses this same context to re-authorize historic tool output.
    # Rebuilding it here would introduce a TOCTOU window: an active release
    # could switch after history was approved but before that history reached
    # the model.  One turn must therefore use exactly one resolved definition.
    ctx = runtime_context or AgentContext(db, agent, llm)
    if ctx.db is not db or ctx.agent.id != agent.id:
        raise RuntimeError("Agent 运行上下文与当前对话不匹配")
    ctx.llm = llm
    controlled_medical_audit = bool(
        ctx.scenario and ctx.scenario.namespace == "medical_audit"
    )
    authoritative_medical_facilities: list[str] = []
    medical_facility_lookup_succeeded: bool | None = None
    if controlled_medical_audit and any(
        term in user_message for term in _AUDIT_INTENT_TERMS
    ):
        try:
            authoritative_medical_facilities = (
                ctx._medical_facility_names_in_message(user_message)
            )
            medical_facility_lookup_succeeded = True
        except Exception:
            # Contract, schema, connection and property ACL failures must all
            # prevent deterministic evidence from being presented as verified.
            medical_facility_lookup_succeeded = False
    # The router's summary is a presentation optimisation only. The actual
    # prompt must be rebuilt from this turn's resolved runtime definition so a
    # staging/prod Agent cannot inherit mutable live ontology text.
    runtime_model = ctx._ontology_model()
    runtime_summary_lines: list[str] = []
    for entity in runtime_model["entities"]:
        properties = ", ".join(
            f"{prop['name']}:{prop['data_type']}" + ("(主键)" if prop["key"] else "")
            for prop in entity["properties"]
        )
        runtime_summary_lines.append(
            f"- 实体「{entity['name']}」: {properties or '无属性'}"
        )
        if entity.get("description"):
            runtime_summary_lines.append(f"  说明: {entity['description']}")
    for relation in runtime_model["relations"]:
        runtime_summary_lines.append(
            f"- 关系: {relation['source_entity']} --[{relation['name']}]--"
            f"({relation['cardinality']})--> {relation['target_entity']}"
        )
        constraints = relation.get("constraints") or {}
        if constraints:
            runtime_summary_lines.append(
                "  约束: " + json.dumps(constraints, ensure_ascii=False, sort_keys=True)
            )
        for semantic in relation.get("query_semantics") or []:
            runtime_summary_lines.append(f"  查询语义: {semantic}")
    runtime_scenario_name = (
        ctx.runtime_definition.scenario_name
        if ctx.runtime_definition is not None
        else scenario_name
    )
    system_prompt = build_system_prompt(
        ctx,
        runtime_scenario_name,
        "\n".join(runtime_summary_lines),
    )
    tools = ctx.build_tools()

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    # Keep the configured multi-step budget for audits and deliverable packs,
    # while retaining a hard ceiling against pathological provider loops.
    max_rounds = max(1, min(get_settings().max_tool_rounds, 24))
    tool_call_counts: defaultdict[str, int] = defaultdict(int)
    tool_outcomes: list[dict[str, Any]] = []
    for _round in range(max_rounds):
        _assert_agent_turn_lease(db)
        # 流式获取本轮 LLM 输出
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for ev in llm_service.chat_stream(
            llm,
            messages,
            tools=tools or None,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
            db=db,
        ):
            if ev["type"] == "token":
                content_parts.append(ev["content"])
            elif ev["type"] == "tool_calls":
                tool_calls = ev["tool_calls"]

        content = "".join(content_parts)

        if not tool_calls:
            # 最终回答
            final_content = _truthful_final_content(
                content,
                user_message=user_message,
                tool_outcomes=tool_outcomes,
                controlled_medical_audit=controlled_medical_audit,
                authoritative_medical_facilities=authoritative_medical_facilities,
                medical_facility_lookup_succeeded=medical_facility_lookup_succeeded,
            )
            if ctx.citations:
                yield {"type": "citations", "data": ctx.citation_snapshot()}
            for part in content_parts:
                yield {"type": "token", "data": part}
            suffix = final_content[len(content):] if final_content.startswith(content) else final_content
            if suffix:
                yield {"type": "token", "data": suffix}
            yield {"type": "done", "data": final_content}
            return

        # 记录 assistant 的工具调用
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": json.dumps(tc["function"]["arguments"], ensure_ascii=False)},
                    }
                    for tc in tool_calls
                ],
            }
        )

        # 执行每个工具
        repeated_tool_call = False
        for tc in tool_calls:
            fname = tc["function"]["name"]
            fargs = tc["function"]["arguments"] or {}
            yield {"type": "tool_call", "data": {"id": tc["id"], "name": fname, "arguments": fargs}}
            signature = json.dumps(
                {"name": fname, "arguments": fargs},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if tool_call_counts[signature]:
                repeated_tool_call = True
                result = _tool_error(
                    "INVALID_TOOL_ARGUMENTS",
                    "本轮已经用相同参数执行过该工具；请直接基于已有结果给出最终回答。",
                    retryable=False,
                )
            else:
                result = ctx.execute_tool(fname, fargs)
                # An executor may take long enough for a crashed/partitioned
                # worker to lose its lease.  Do not expose or persist its late
                # output as part of a newer turn's transcript.
                _assert_agent_turn_lease(db)
            tool_call_counts[signature] += 1
            bounded_result = _bounded_tool_result(result)
            tool_outcomes.append(
                {
                    "name": fname,
                    "arguments": copy.deepcopy(fargs),
                    "result": bounded_result,
                }
            )
            yield {
                "type": "tool_result",
                "data": {"id": tc["id"], "name": fname, "result": bounded_result},
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fname,
                    "content": bounded_result,
                }
            )
        if repeated_tool_call:
            messages.append(
                {
                    "role": "user",
                    "content": "检测到重复工具调用。请立即停止工具调用，只基于当前已有证据输出最终结果。",
                }
            )

    if ctx.citations:
        yield {"type": "citations", "data": ctx.citation_snapshot()}
    # 工具轮次耗尽时再请求一次“只总结、不调用工具”的最终回答，避免把内部
    # 重试过程直接交给用户，也避免仅返回一句无法交付的兜底提示。
    final_messages = messages + [
        {
            "role": "user",
            "content": (
                "工具调用轮次已达到上限。请只基于已经返回的工具结果，立即生成最终可交付的回答；"
                "不要再调用工具，不要描述内部过程。明确给出结论、证据、汇总和仍存在的数据限制。"
            ),
        }
    ]
    final_parts: list[str] = []
    try:
        _assert_agent_turn_lease(db)
        for ev in llm_service.chat_stream(
            llm,
            final_messages,
            tools=None,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
            db=db,
        ):
            if ev["type"] == "token":
                final_parts.append(ev["content"])
    except Exception:
        final_parts = []

    untrusted_final_content = "".join(final_parts).strip()
    if not untrusted_final_content:
        untrusted_final_content = (
            "已完成当前可用工具查询，但在工具调用上限内未能形成完整结论。"
            "请根据已展示的工具结果补充查询条件，或检查场景的数据映射与可用能力。"
        )
    final_content = _truthful_final_content(
        untrusted_final_content,
        user_message=user_message,
        tool_outcomes=tool_outcomes,
        controlled_medical_audit=controlled_medical_audit,
        authoritative_medical_facilities=authoritative_medical_facilities,
        medical_facility_lookup_succeeded=medical_facility_lookup_succeeded,
    )
    if final_parts:
        for part in final_parts:
            yield {"type": "token", "data": part}
    else:
        # The SSE router persists token events, not the done envelope.  Emit
        # the deterministic fallback so an exhausted turn cannot save a blank
        # assistant message.
        yield {"type": "token", "data": untrusted_final_content}
    suffix = (
        final_content[len(untrusted_final_content):]
        if final_content.startswith(untrusted_final_content)
        else final_content
    )
    if suffix:
        yield {"type": "token", "data": suffix}
    yield {"type": "done", "data": final_content}


def run_agent(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    history: list[dict[str, Any]],
    user_message: str,
    scenario_name: str,
    ontology_summary: str,
    *,
    trace_context: dict[str, Any] | None = None,
    runtime_context: AgentContext | None = None,
) -> Iterator[dict[str, Any]]:
    """执行 Agent 并在每个真实模型 trace 中保留最小可审计回链。"""
    previous = db.info.get("llm_trace_context")
    previous_action_audit = db.info.get("action_audit_context")
    context = dict(trace_context or {})
    context.setdefault("agent_id", agent.id)
    context.setdefault("scenario_id", agent.scenario_id or "")
    if context:
        db.info["llm_trace_context"] = context
    # Action tools are preview-only, but their durable audit row must still
    # identify which Agent and routed model produced the recommendation.
    db.info["action_audit_context"] = {
        "agent_id": agent.id,
        "llm_config_id": llm.id,
        "model_name": llm.model or "",
    }
    try:
        yield from _run_agent(
            db,
            agent,
            llm,
            history,
            user_message,
            scenario_name,
            ontology_summary,
            runtime_context=runtime_context,
        )
    finally:
        if previous is None:
            db.info.pop("llm_trace_context", None)
        else:
            db.info["llm_trace_context"] = previous
        if previous_action_audit is None:
            db.info.pop("action_audit_context", None)
        else:
            db.info["action_audit_context"] = previous_action_audit
