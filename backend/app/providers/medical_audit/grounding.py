"""Deterministic grounding and truth guards owned by the audit Provider."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...services.capability_agent_extensions import GroundingResult


PROVIDER_KEY = "trusted.medical-audit"
PROVIDER_VERSION = "1.0.0"
AUDIT_INTENT_TERMS = ("审计", "核验", "核查", "排查", "违规")
_COMPLETE_DETAIL_TERMS = ("全部", "全量", "所有", "完整", "逐条", "明细")
_STRATEGY_LABELS = {
    "charge_threshold": "单条收费数量阈值",
    "daily_overstay": "日计价超过住院天数",
    "included_service_duplicate": "包含项目重复收费",
    "limited_drug_duration": "限疗程用药",
}
_STRATEGY_ARGUMENTS = {
    "charge_threshold": ("service_name", "threshold"),
    "daily_overstay": ("service_names",),
    "included_service_duplicate": ("included_service", "duplicate_service"),
    "limited_drug_duration": ("drug_name", "max_days"),
}
_RECORD_IDENTITY_FIELDS = {
    "charge_threshold": ("charge_line_id",),
    "daily_overstay": ("encounter_id", "service_name"),
    "included_service_duplicate": ("charge_line_id",),
    "limited_drug_duration": ("encounter_id", "drug_name"),
}
_FACILITY_SUFFIXES = (
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
_FACILITY_SUFFIX_PATTERN = re.compile(
    r"[0-9A-Za-z\u4e00-\u9fff·._/-]{1,60}?(?:"
    + "|".join(re.escape(value) for value in _FACILITY_SUFFIXES)
    + r")",
    flags=re.IGNORECASE,
)
_FACILITY_SCOPE_INTRODUCERS = (
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
_GENERIC_FACILITY_PREFIXES = frozenset(
    {
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
    }
)
_FACILITY_NEGATIVE_PREFIXES = (
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


@dataclass(frozen=True, slots=True)
class GroundingPreparation:
    authoritative_facilities: tuple[str, ...] = ()
    facility_lookup_succeeded: bool | None = None


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


def _display_number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return format(float(value), ".12g")


def _medical_record_identity(
    strategy: str,
    record: Any,
) -> tuple[str, ...] | None:
    fields = _RECORD_IDENTITY_FIELDS.get(strategy)
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
    for match in _FACILITY_SUFFIX_PATTERN.finditer(str(user_message or "")):
        candidate = match.group(0)
        cut_at = 0
        for introducer in _FACILITY_SCOPE_INTRODUCERS:
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
                for value in _FACILITY_SUFFIXES
                if normalized.endswith(_normalized_business_text(value))
            ),
            "",
        )
        if not suffix:
            continue
        prefix = normalized[: -len(suffix)]
        if (
            prefix in _GENERIC_FACILITY_PREFIXES
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
        if not any(candidate != longer and candidate in longer for longer in heuristic)
    }


def _medical_request_excludes_facility(
    user_message: str,
    authoritative_facilities: Sequence[str] | None = None,
) -> bool:
    """Fail closed when an explicitly named institution is a negative scope."""

    explicit_facilities = _resolved_medical_facilities(
        user_message,
        authoritative_facilities,
    )
    if not explicit_facilities:
        return False
    normalized_message = _normalized_business_text(user_message)
    normalized_prefixes = tuple(
        _normalized_business_text(prefix) for prefix in _FACILITY_NEGATIVE_PREFIXES
    )
    facility_names: set[str] = set()
    for candidate in explicit_facilities:
        normalized = candidate
        for prefix in normalized_prefixes:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                normalized = normalized[len(prefix) :]
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
            rf"(?:不要|不|无需)(?:对)?{facility}(?:进行|开展)?(?:本次)?审计",
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
        re.search(rf"(?<![0-9a-z]){re.escape(form)}(?![0-9a-z])", raw_user)
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
    if any(term in text for term in ("大于", "高于", "超过", "超出")) and any(
        term in text for term in ("收费", "数量", "次数", "次")
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
    """Bind deterministic evidence to the user's stated task."""

    if not isinstance(arguments, Mapping) or _requested_medical_strategy(user_message) != strategy:
        return False
    parameters = evidence.get("parameters")
    if not isinstance(parameters, Mapping):
        return False
    expected_parameters: dict[str, Any] = {
        "facility_name": arguments.get("facility_name"),
    }
    for key in _STRATEGY_ARGUMENTS[strategy]:
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
        return False
    if requested_facilities and (
        len(requested_facilities) != 1
        or not expected_facility
        or requested_facilities != {expected_facility}
    ):
        return False
    for key in (
        "facility_name",
        "service_name",
        "included_service",
        "duplicate_service",
        "drug_name",
    ):
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


def _audit_status(
    tool_outcomes: Sequence[Mapping[str, Any]],
    *,
    user_message: str,
    tool_names: Sequence[str],
    authoritative_facilities: Sequence[str] | None = None,
    facility_lookup_succeeded: bool | None = None,
) -> tuple[list[str], bool, list[dict[str, Any]]]:
    """Build deterministic summaries and prove every requested detail page."""

    if facility_lookup_succeeded is False:
        return [], False, []
    normalized_authoritative_facilities = {
        normalized
        for value in authoritative_facilities or ()
        if (normalized := _normalized_business_text(value))
    }
    if _medical_request_excludes_facility(user_message, authoritative_facilities):
        return [], False, []
    requested_facilities = _resolved_medical_facilities(
        user_message,
        authoritative_facilities,
    )
    accepted_names = set(tool_names)
    groups: dict[str, list[dict[str, Any]]] = {}
    for outcome in tool_outcomes:
        if outcome.get("name") not in accepted_names or _failed_tool_result(outcome.get("result")):
            continue
        payload = _parsed_result(outcome.get("result"))
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
        if strategy not in _STRATEGY_ARGUMENTS or not _medical_request_matches_user(
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
    provenance: list[dict[str, Any]] = []
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
        label = _STRATEGY_LABELS.get(strategy, strategy or "未知策略")
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
                    payload.get("evidence"),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
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
        evidence = first.get("evidence") or {}
        lineage = first.get("lineage") or {}
        mapping_contract = (
            lineage.get("mapping_contract")
            if isinstance(lineage, Mapping)
            else {}
        )
        provenance.append(
            {
                "audit_version": str(first.get("audit_version") or ""),
                "strategy": strategy,
                "source_id": str(evidence.get("source_id") or ""),
                "mapping_fingerprint": str(
                    mapping_contract.get("fingerprint")
                    if isinstance(mapping_contract, Mapping)
                    else ""
                ),
            }
        )
    return lines, verified, provenance


def grounding_result(
    tool_outcomes: Sequence[Mapping[str, Any]],
    *,
    user_message: str,
    tool_names: Sequence[str],
    authoritative_facilities: Sequence[str] | None = None,
    facility_lookup_succeeded: bool | None = None,
    definition_hash: str = "",
) -> GroundingResult:
    lines, verified, provenance = _audit_status(
        tool_outcomes,
        user_message=user_message,
        tool_names=tool_names,
        authoritative_facilities=authoritative_facilities,
        facility_lookup_succeeded=facility_lookup_succeeded,
    )
    if any(term in user_message for term in AUDIT_INTENT_TERMS) and not verified:
        lines.append("未形成可验证审计结论，不能把当前回答作为违规审计结果。")
    return GroundingResult(
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        verified=verified,
        status_lines=tuple(lines),
        provenance={
            "definition_hash": str(definition_hash or ""),
            "evidence": provenance,
        },
    )


__all__ = [
    "AUDIT_INTENT_TERMS",
    "GroundingPreparation",
    "PROVIDER_KEY",
    "PROVIDER_VERSION",
    "grounding_result",
]
