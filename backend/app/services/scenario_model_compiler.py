"""Compile one business document into one governed, atomic scenario model.

The compiler deliberately separates model interpretation from persistence:
LLM output is normalized into a closed schema, every source paragraph is
accounted for, every cross-resource reference is resolved, and the whole
change set is preflighted before the first row is added to the session.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    RelationDataMapping,
)
from ..schemas import EntityIn, PropertyIn, RelationDataMappingIn
from . import (
    connector_service,
    datasource_service,
    function_definition_service,
    llm_service,
    mapping_refresh_service,
    ontology_service,
    operations_service,
    tenant_service,
    workflow_service,
)
from .policies import PolicyViolation


SCHEMA_VERSION = "scenario_model.v1"
# This version participates in the persistent assistant execution fingerprint.
# Bump it whenever extraction/prompt semantics change in a way that should
# permit recompiling otherwise identical inputs.
COMPILER_VERSION = "scenario_model.compiler.v5"
MAX_SOURCE_CHARS = 100_000
MAX_OUTPUT_TOKENS = 20_000
FALLBACK_SOURCE_CHARS = 12_000
# Chunked extraction uses the same provider-supported response budget as the
# whole-document attempt.  Keeping this as an alias avoids a lower fallback
# ceiling silently forcing otherwise healthy chunks into recursive bisection.
FALLBACK_MAX_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
DIRECT_CHUNK_SOURCE_CHARS = FALLBACK_SOURCE_CHARS * 3
_RESOURCE_SECTIONS = (
    "entities",
    "relations",
    "functions",
    "actions",
    "rules",
    "events",
    "workflows",
    "mappings",
    "relation_mappings",
)
_RESOURCE_KEY_PREFIXES = {
    "entities": "entity",
    "relations": "relation",
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
    "mappings": "mapping",
    "relation_mappings": "relation_mapping",
}
_RESOURCE_PREFIX_ALIASES = {
    section: {prefix, section}
    for section, prefix in _RESOURCE_KEY_PREFIXES.items()
}
_RESOURCE_PREFIX_CANONICAL = {
    alias: _RESOURCE_KEY_PREFIXES[section]
    for section, aliases in _RESOURCE_PREFIX_ALIASES.items()
    for alias in aliases
}

# Model-reported issues are untrusted input.  A model may suggest severity, but
# only this closed platform policy can let an issue remain non-blocking.  Keep
# the physical-mapping set separate because those codes are safe only when the
# platform can prove that no data source or physical mapping is in play.
_RAW_NONBLOCKING_AUDIT_CODES = frozenset({
    "USER_CORRECTION_APPLIED",
    "DOCUMENT_ADVISORY",
})
_RAW_NONBLOCKING_PHYSICAL_MAPPING_CODES = frozenset({
    # MISSING_DATA_SOURCE is retained as a compatibility alias for proposals
    # compiled before the canonical mapping-deferred code was introduced.
    "MISSING_DATA_SOURCE",
    "MAPPING_DEFERRED_NO_DATA_SOURCE",
    "DATA_SOURCE_NOT_CONFIGURED",
    "DATA_SOURCE_UNAVAILABLE",
})
# These issues are produced deterministically by platform validation rather
# than copied from model output.  Adding a new non-blocking compiler issue must
# therefore be an explicit policy change instead of an accidental call-site
# choice.
_PLATFORM_NONBLOCKING_ISSUE_CODES = frozenset({
    "title_fallback_to_primary_key",
})


class CompilationCallBudgetExceeded(ValueError):
    """Raised before a provider call would exceed the whole-job ceiling."""


class LLMCallBudget:
    """Shared provider-call counter across direct, fallback and split paths."""

    def __init__(self, total: int, on_consume=None) -> None:
        if int(total) < 1:
            raise ValueError("完整业务模型的 LLM 总调用预算必须至少为 1")
        self.total = int(total)
        self.used = 0
        self._on_consume = on_consume

    def consume(self, phase: str) -> None:
        if self.used >= self.total:
            raise CompilationCallBudgetExceeded(
                f"完整业务模型编译已达到 LLM 总调用预算 {self.total}；"
                "任务已失败且不会写入任何正式模型，请缩小或拆分文档后重试"
            )
        self.used += 1
        if self._on_consume is not None:
            self._on_consume(self.used, self.total, phase)
_RULE_LEAF_OPS = {
    ">", ">=", "<", "<=", "==", "!=", "in", "not_in", "contains",
    "not_contains", "is_null", "is_not_null",
}
_SCHEMA_TYPE_ALIASES: dict[str, tuple[str, str | None]] = {
    # JSON Schema's primitive vocabulary is deliberately smaller than the
    # business/model vocabularies commonly emitted by an LLM.  These aliases
    # are lossless at the contract boundary; unknown domain types still fail
    # the closed validator below.
    "object": ("object", None),
    "array": ("array", None),
    "string": ("string", None),
    "str": ("string", None),
    "text": ("string", None),
    "number": ("number", None),
    "numeric": ("number", None),
    "decimal": ("number", None),
    "float": ("number", None),
    "double": ("number", None),
    "integer": ("integer", None),
    "int": ("integer", None),
    "long": ("integer", None),
    "boolean": ("boolean", None),
    "bool": ("boolean", None),
    "null": ("null", None),
    "date": ("string", "date"),
    "datetime": ("string", "date-time"),
    "date_time": ("string", "date-time"),
    "date-time": ("string", "date-time"),
}
_RULE_OPERATOR_ALIASES = {
    "=": "==",
    "eq": "==",
    "equals": "==",
    "equal": "==",
    "<>": "!=",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "gte": ">=",
    "lt": "<",
    "le": "<=",
    "lte": "<=",
    "not_in": "not_in",
    "not_contains": "not_contains",
    "does_not_contain": "not_contains",
    "isnull": "is_null",
    "null": "is_null",
    "not_null": "is_not_null",
    "notnull": "is_not_null",
    "等于": "==",
    "不等于": "!=",
    "大于": ">",
    "大于等于": ">=",
    "不小于": ">=",
    "小于": "<",
    "小于等于": "<=",
    "不大于": "<=",
    "属于": "in",
    "不属于": "not_in",
    "包含": "contains",
    "不包含": "not_contains",
    "为空": "is_null",
    "是空值": "is_null",
    "非空": "is_not_null",
    "不为空": "is_not_null",
    "不是空值": "is_not_null",
    "且": "and",
    "并且": "and",
    "或": "or",
    "非": "not",
}
_RULE_SEVERITY_ALIASES = {
    "info": "info",
    "informational": "info",
    "notice": "info",
    "low": "info",
    "minor": "info",
    "信息": "info",
    "提示": "info",
    "低": "info",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "moderate": "warning",
    "警告": "warning",
    "中": "warning",
    "critical": "critical",
    "high": "critical",
    "severe": "critical",
    "error": "critical",
    "fatal": "critical",
    "blocker": "critical",
    "严重": "critical",
    "高": "critical",
    "紧急": "critical",
}


class _CompilerOutputTruncated(RuntimeError):
    """Provider stopped a bounded compiler response at its output-token limit."""


class CompilerProviderUnavailable(RuntimeError):
    """A bounded set of transient provider attempts was exhausted."""


def _malformed_output_is_probably_truncated(
    content: str,
    error: Exception,
) -> bool:
    """Detect an undeclared token-limit cut without attempting lossy JSON repair.

    Some OpenAI-compatible providers return ``finish_reason=stop`` even though
    a long JSON object ends mid-token.  Retrying the same oversized prompt is
    both wasteful and unreliable; a parse error at the very end of a large
    response is a deterministic signal to bisect that source branch instead.
    Short or mid-document syntax errors still use the ordinary bounded retry.
    """
    if not isinstance(error, json.JSONDecodeError):
        return False
    text = str(content or "").strip()
    if len(text) < 8_000:
        return False
    position = max(0, int(getattr(error, "pos", 0) or 0))
    near_tail = position >= len(text) - 512 or position >= int(len(text) * 0.97)
    if not near_tail:
        return False
    message = str(getattr(error, "msg", error) or "").lower()
    return any(
        marker in message
        for marker in (
            "unterminated string",
            "expecting ',' delimiter",
            "expecting ':' delimiter",
            "expecting value",
            "expecting property name",
        )
    )


def _text(value: Any, *, maximum: int = 8_000) -> str:
    return str(value or "").strip()[:maximum]


def _canonicalize_resource_schema(value: Any, *, path: str = "资源 Schema") -> Any:
    """Translate only lossless model-type aliases into strict JSON Schema.

    This adapter is intentionally compiler-local: persisted function, action
    and event definitions still pass the platform's closed JSON-Schema
    validator and never acquire a second schema dialect.
    """
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    raw_type = result.get("type")
    expected_format: str | None = None
    if isinstance(raw_type, str):
        token = raw_type.strip().lower()
        alias = _SCHEMA_TYPE_ALIASES.get(token)
        if alias:
            result["type"], expected_format = alias
        else:
            # Normalizing case/whitespace is deterministic, while leaving an
            # unknown token in place ensures the closed validator blocks it.
            result["type"] = token
    raw_format = result.get("format")
    if isinstance(raw_format, str):
        format_token = raw_format.strip().lower()
        if format_token in {"datetime", "date_time"}:
            format_token = "date-time"
        result["format"] = format_token
    if expected_format:
        existing_format = result.get("format")
        if existing_format not in (None, "", expected_format):
            raise ValueError(
                f"{path} 的类型 {raw_type} 与 format={existing_format} 冲突"
            )
        result["format"] = expected_format

    properties = result.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            name: _canonicalize_resource_schema(
                schema,
                path=f"{path}.properties.{name}",
            )
            for name, schema in properties.items()
        }
    if "items" in result:
        result["items"] = _canonicalize_resource_schema(
            result["items"], path=f"{path}.items"
        )
    for keyword in ("oneOf", "anyOf", "allOf"):
        variants = result.get(keyword)
        if isinstance(variants, list):
            result[keyword] = [
                _canonicalize_resource_schema(
                    variant,
                    path=f"{path}.{keyword}[{index}]",
                )
                for index, variant in enumerate(variants)
            ]
    return result


def _object_schema(value: Any) -> dict[str, Any]:
    schema = value if isinstance(value, dict) and value else {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return function_definition_service.normalize_schema(
        _canonicalize_resource_schema(schema),
        label="资源 Schema",
    )


_CONSTRAINT_KEY_ALIASES = {
    "const": "const",
    "fixed": "const",
    "fixed_value": "const",
    "fixedvalue": "const",
    "min": "minimum",
    "minimum": "minimum",
    "max": "maximum",
    "maximum": "maximum",
    "exclusive_min": "exclusive_minimum",
    "exclusive_minimum": "exclusive_minimum",
    "exclusive_max": "exclusive_maximum",
    "exclusive_maximum": "exclusive_maximum",
    "min_length": "min_length",
    "minlength": "min_length",
    "max_length": "max_length",
    "maxlength": "max_length",
    "pattern": "pattern",
    "format": "format",
}

_ENTITY_PROPERTY_TYPE_ALIASES = {
    "str": "string",
    "numeric": "number",
    "decimal": "number",
    "double": "number",
    "int": "integer",
    "long": "integer",
    "bool": "boolean",
    "object": "json",
    "array": "json",
    "date_time": "datetime",
    "date-time": "datetime",
}

_RELATION_BOOLEAN_TEXT = {
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "1": True,
    "0": False,
    "是": True,
    "否": False,
    "真": True,
    "假": False,
}
_RELATION_UNBOUNDED_TEXT = {
    "*",
    "n",
    "many",
    "unbounded",
    "unlimited",
    "infinite",
    "infinity",
    "∞",
    "多",
    "多个",
    "不限",
    "无上限",
    "任意",
    "任意多个",
}
_RELATION_BOOLEAN_FIELDS = {
    "symmetric", "transitive", "irreflexive", "asymmetric",
    "antisymmetric", "acyclic",
}
_RELATION_CARDINALITY_FIELDS = {
    "source_min_cardinality", "source_max_cardinality",
    "target_min_cardinality", "target_max_cardinality",
}
_RELATION_TYPE_ALIASES = {
    "1:1": "1:1",
    "1:N": "1:N",
    "N:1": "N:1",
    "N:M": "N:M",
    # The platform's N:M form is the unconstrained binary-relation shape:
    # without explicit maximum constraints it does not assert that either
    # side actually has multiple values. It is therefore the lossless
    # representation of a generic OWL/object-property association.
    "ASSOCIATION": "N:M",
    # Common multiplicity spellings retain exactly the same endpoint limits.
    "M:N": "N:M",
    "M:M": "N:M",
    "N:N": "N:M",
    "*:*": "N:M",
    "ONE_TO_ONE": "1:1",
    "ONE_TO_MANY": "1:N",
    "MANY_TO_ONE": "N:1",
    "MANY_TO_MANY": "N:M",
}


def _normalize_entity_property_type(value: Any) -> str:
    token = _text(value or "string", maximum=40).lower()
    return _ENTITY_PROPERTY_TYPE_ALIASES.get(token, token)


def _normalize_compiler_relation_type(value: Any) -> str:
    """Return a closed platform multiplicity without truncating the token."""
    token = _text(value or "1:N", maximum=80).upper()
    normalized_token = re.sub(r"[\s-]+", "_", token)
    return _RELATION_TYPE_ALIASES.get(
        token,
        _RELATION_TYPE_ALIASES.get(normalized_token, ""),
    )


def _constraint_scalar(value: str) -> Any:
    text = str(value or "").strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text.strip("\"'")


def _normalize_compiler_constraints(data_type: str, value: Any) -> dict[str, Any]:
    """Normalize common LLM constraint spellings without hiding ambiguity."""
    if value is None or value == "":
        return {}
    candidate: Any = value
    if isinstance(candidate, str):
        stripped = candidate.strip()
        if not stripped or stripped.lower() in {"null", "none"} or stripped in {"无", "空"}:
            return {}
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            candidate = decoded
        else:
            parsed: dict[str, Any] = {}
            fragments = [
                item.strip()
                for item in re.split(r"[,，;；]", stripped)
                if item.strip()
            ]
            if not fragments:
                raise ValueError("属性约束必须是对象")
            for fragment in fragments:
                match = re.fullmatch(r"([A-Za-z_]+)\s*[:=]\s*(.+)", fragment)
                if match is None:
                    raise ValueError("属性约束简写必须使用 key:value 格式")
                source_key, raw_value = match.groups()
                key = _CONSTRAINT_KEY_ALIASES.get(source_key.strip().lower())
                if key is None:
                    raise ValueError(f"属性约束包含不支持的字段：{source_key}")
                parsed[key] = _constraint_scalar(raw_value)
            candidate = parsed
    if not isinstance(candidate, dict):
        raise ValueError("属性约束必须是对象")
    normalized: dict[str, Any] = {}
    for source_key, raw_value in candidate.items():
        key = _CONSTRAINT_KEY_ALIASES.get(str(source_key).strip().lower())
        if key is None:
            raise ValueError(f"属性约束包含不支持的字段：{source_key}")
        normalized[key] = raw_value
    return ontology_service.normalize_property_constraints(data_type, normalized)


def _normalize_compiler_relation_constraints(
    value: Any,
    *,
    relation_type: str,
) -> tuple[dict[str, Any], str]:
    """Normalize the closed graph-axiom shape without accepting raw ids."""
    if value in (None, ""):
        candidate: Any = {}
    elif isinstance(value, str):
        try:
            candidate = json.loads(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("关系约束必须是 JSON 对象") from exc
    else:
        candidate = copy.deepcopy(value)
    if not isinstance(candidate, dict):
        raise ValueError("关系约束必须是对象")
    nested_inverse_ref = _text(candidate.pop("inverse_relation_ref", ""), maximum=300)
    if candidate.get("inverse_relation_id"):
        raise ValueError("AI 编译结果不能直接写逆关系 ID，必须使用 inverse_relation_ref")
    candidate.pop("inverse_relation_id", None)

    # LLMs and ontology documents commonly spell unbounded cardinality as
    # ``*``/``N``/``many`` and may serialize booleans or integers as strings.
    # These are closed, lossless syntax conversions rather than semantic
    # guesses.  Runtime storage remains strict: an unbounded maximum is
    # represented by an omitted/None maximum, and every persisted boolean or
    # bound still passes ontology_service.normalize_relation_constraints.
    for key in _RELATION_BOOLEAN_FIELDS & candidate.keys():
        raw_value = candidate[key]
        if raw_value in (None, ""):
            candidate.pop(key, None)
            continue
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, int) and raw_value in {0, 1}:
            candidate[key] = bool(raw_value)
            continue
        if isinstance(raw_value, str):
            token = raw_value.strip().lower()
            if token in _RELATION_BOOLEAN_TEXT:
                candidate[key] = _RELATION_BOOLEAN_TEXT[token]

    for key in _RELATION_CARDINALITY_FIELDS & candidate.keys():
        raw_value = candidate[key]
        is_maximum = "_max_" in key
        if isinstance(raw_value, bool) or raw_value in (None, ""):
            continue
        if isinstance(raw_value, int):
            if is_maximum and raw_value == -1:
                candidate[key] = None
            continue
        if isinstance(raw_value, float) and raw_value.is_integer():
            integer_value = int(raw_value)
            candidate[key] = None if is_maximum and integer_value == -1 else integer_value
            continue
        if not isinstance(raw_value, str):
            continue
        token = raw_value.strip()
        lowered = token.lower()
        if is_maximum and lowered in _RELATION_UNBOUNDED_TEXT:
            candidate[key] = None
            continue
        if re.fullmatch(r"\+?\d+", token):
            candidate[key] = int(token)
            continue
        # Standard OWL/UML-style interval shorthand occasionally lands in a
        # scalar cardinality field.  Expand it only when both endpoints are
        # explicit and the companion field does not contradict it.
        interval = re.fullmatch(
            r"(\d+)\s*\.\.\s*(\d+|\*|n|many|unbounded|∞)",
            lowered,
        )
        if interval is None:
            continue
        side = "source" if key.startswith("source_") else "target"
        minimum_token, maximum_token = interval.groups()
        minimum_key = f"{side}_min_cardinality"
        maximum_key = f"{side}_max_cardinality"
        minimum = int(minimum_token)
        maximum = (
            None
            if maximum_token in _RELATION_UNBOUNDED_TEXT
            else int(maximum_token)
        )
        if minimum_key not in candidate or candidate.get(minimum_key) in (None, "", token):
            candidate[minimum_key] = minimum
        candidate[maximum_key] = maximum
    return (
        ontology_service.normalize_relation_constraints(
            candidate, relation_type=relation_type
        ),
        nested_inverse_ref,
    )


def _looks_like_relation_axiom_rule(value: dict[str, Any]) -> bool:
    """Recognize graph axioms only to issue a precise blocker, never to guess."""
    serialized = json.dumps(value, ensure_ascii=False, default=str).lower()
    return any(
        token in serialized
        for token in (
            "symmetric", "transitive", "irreflexive", "asymmetric",
            "antisymmetric", "acyclic", "inverseof", "inverse_relation",
            "functionalproperty", "inversefunctional", "cardinality",
            "mincardinality", "maxcardinality", "exactcardinality",
            "对称", "传递", "反自反", "非对称", "反对称", "无环", "逆关系",
            "函数性关系", "功能性关系", "基数约束", "最小基数", "最大基数",
        )
    )


def _looks_like_class_axiom_rule(value: dict[str, Any]) -> bool:
    """Identify class-level OWL-style axioms that P0 must not fake as rules."""
    serialized = json.dumps(value, ensure_ascii=False, default=str).lower()
    return any(
        token in serialized
        for token in (
            "equivalentclass", "equivalent_class", "disjointwith", "disjoint_class",
            "subclassof", "subclass_of", "superclass", "inheritance",
            "类等价", "等价类", "类互斥", "互斥类", "不相交类", "子类", "父类", "继承",
        )
    )


def _paragraphs(text: str) -> list[str]:
    """Create bounded deterministic paragraph units without dropping content."""
    raw_blocks = re.split(r"\n\s*\n", str(text or "").replace("\r\n", "\n"))
    result: list[str] = []
    for block in raw_blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}".strip()
            if current and len(candidate) > 1_200:
                result.append(current)
                current = line
            else:
                current = candidate
        if current:
            while len(current) > 1_200:
                split_at = max(
                    current.rfind(mark, 0, 1_200)
                    for mark in ("。", "；", ";", ".", "，", ",", " ")
                )
                if split_at < 400:
                    split_at = 1_200
                else:
                    split_at += 1
                result.append(current[:split_at].strip())
                current = current[split_at:].strip()
            if current:
                result.append(current)
    return result


_REQUEST_SECTION_PATTERN = re.compile(
    r"(?im)(?:^|[\r\n。；;])[ \t]*(?P<label>业务描述|补充描述)[ \t]*[:：][ \t]*"
)
_PURE_COMPILATION_CONTROL_PATTERN = re.compile(
    r"^(?:编译(?:关系模型|关系映射|业务模型|本体模型)|请(?:"
    r"(?:只)?(?:逐段)?(?:编译|解析)(?:我)?(?:本次)?(?:上传的)?(?:完整)?(?:业务)?(?:附件|文档)"
    r"|(?:只)?(?:严格)?(?:根据|按照|以)(?:我)?(?:本次)?(?:上传的)?(?:这个)?(?:完整)?(?:业务)?(?:附件|文档)(?:内容)?(?:编译|解析|建模)"
    r"))"
    r"(?:，?(?:生成|输出)完整业务模型)?"
    r"(?:，?并列出(?:所有)?未识别、歧义和冲突项)?[。.!！]?$"
)


def _empty_structured_request_value(value: str) -> bool:
    """Return true only for explicit empty/control placeholders.

    A statement such as ``无特殊限制`` is business semantics and must not be
    mistaken for the empty value ``无``. The longer patterns below are limited
    to generic UI placeholders; anything else is retained as evidence.
    """
    normalized = re.sub(r"\s+", "", str(value or "")).strip("。.!！")
    if normalized in {"", "暂无", "无", "没有", "未提供", "未填写", "待补充", "不适用"}:
        return True
    if re.fullmatch(
        r"(?:暂无|未提供|未填写)[，,;；:]?请结合(?:我)?(?:上传的)?(?:附件|文档)"
        r"(?:提取|解析)(?:业务)?目标(?:和|与)边界",
        normalized,
    ):
        return True
    return bool(re.fullmatch(
        r"请(?:先)?根据场景名称提出需要补充的关键信息",
        normalized,
    ))


def _structured_request_source(message: str) -> tuple[bool, str]:
    """Extract UI-declared business sections without copying control preambles."""
    body = str(message or "")
    matches = list(_REQUEST_SECTION_PATTERN.finditer(body))
    if not matches:
        return False, ""
    sections: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end():end].strip()
        if _empty_structured_request_value(value):
            continue
        label = str(match.group("label") or "").strip()
        sections.append(f"{label}：{value}")
    return True, "\n\n".join(sections)


def _is_pure_compilation_control(message: str) -> bool:
    """Conservatively recognize only a closed, semantics-free quick command.

    Unrecognized free text is deliberately treated as business evidence. This
    prevents a request such as ``请根据附件编译，并将 A 修正为 B`` from losing
    the correction merely because it also contains a compiler verb.
    """
    normalized = re.sub(r"\s+", "", str(message or "")).strip()
    return bool(_PURE_COMPILATION_CONTROL_PATTERN.fullmatch(normalized))


def _request_business_source(message: str, *, has_documents: bool) -> str:
    """Select the auditable business portion of one assistant request."""
    raw = str(message or "").strip()
    if not raw:
        return ""
    has_structured_sections, structured = _structured_request_source(raw)
    if has_structured_sections:
        return structured
    if has_documents and _is_pure_compilation_control(raw):
        return ""
    # With no attachment, even instruction-like text is the only possible
    # source. With attachments, unknown/mixed wording is retained by design.
    return raw


def build_source_bundle(
    message: str,
    documents: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build the immutable manifest and paragraph ids used for provenance."""
    document_list = list(documents)
    sources: list[dict[str, Any]] = []
    paragraphs: list[dict[str, str]] = []
    seen_source_ids: set[str] = set()
    total = 0
    for index, document in enumerate(document_list, 1):
        status = _text(document.get("status"), maximum=30)
        filename = _text(
            document.get("filename") or document.get("id") or f"document-{index}",
            maximum=300,
        )
        if status and status != "parsed":
            detail = _text(document.get("error"), maximum=500)
            raise ValueError(
                f"附件“{filename}”尚未成功解析，不能编译完整业务模型"
                + (f"：{detail}" if detail else "")
            )
        body = str(document.get("text") or "")
        if not body.strip():
            raise ValueError(f"附件“{filename}”没有可编译的正文，不能回退为仅解析对话指令")
        total += len(body)
        source_id = _text(document.get("id") or f"document-{index}", maximum=80)
        if not source_id or source_id in seen_source_ids:
            raise ValueError("业务文档来源 ID 为空或重复，不能建立唯一来源段落")
        if source_id == "request":
            raise ValueError("业务文档来源 ID request 为用户补充描述保留字，请更换附件 ID")
        seen_source_ids.add(source_id)
        units = _paragraphs(body)
        for paragraph_index, paragraph in enumerate(units, 1):
            paragraphs.append({
                "ref": f"{source_id}:p{paragraph_index:04d}",
                "source_id": source_id,
                "source_kind": "attachment",
                "text": paragraph,
            })
        sources.append({
            "source_id": source_id,
            "filename": filename,
            "source_kind": "attachment",
            "semantic_role": "baseline_document",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "characters": len(body),
            "paragraph_count": len(units),
        })
    # Attachments and user-authored descriptions/corrections are independent
    # semantic sources. Only a positively identified control-only request is
    # omitted; unknown mixed text is kept so a correction cannot be lost.
    # A selected-but-failed/empty attachment still fails closed above.
    request_body = _request_business_source(
        message,
        has_documents=bool(document_list),
    )
    if request_body:
        body = request_body
        total += len(body)
        units = _paragraphs(body)
        for paragraph_index, paragraph in enumerate(units, 1):
            paragraphs.append({
                "ref": f"request:p{paragraph_index:04d}",
                "source_id": "request",
                "source_kind": "user_request",
                "text": paragraph,
            })
        sources.append({
            "source_id": "request",
            "filename": "用户补充描述与修正建议",
            "source_kind": "user_request",
            "semantic_role": "supplement_or_correction",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "characters": len(body),
            "paragraph_count": len(units),
        })
    if not paragraphs:
        raise ValueError("没有可编译的业务文档内容")
    if total > MAX_SOURCE_CHARS:
        raise ValueError(
            f"待编译文档共 {total} 个字符，超过单次 {MAX_SOURCE_CHARS} 个字符的明确边界；"
            "系统不会静默截断，请拆分后分别编译"
        )
    return {"documents": sources, "paragraphs": paragraphs, "total_characters": total}


def _mapping_catalog(
    db: Session,
    scenario: BusinessScenario,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], set[str]]]:
    """Read only credential-free physical schemas; broken sources are excluded."""
    candidate_sources = list(
        db.execute(
            select(DataSource).where(
                tenant_service.visible_clause(DataSource, db),
                or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == scenario.id),
                DataSource.type != "file_bucket",
            ).order_by(DataSource.created_at, DataSource.id).limit(50)
        ).scalars().all()
    )
    referenced_source_ids = {
        str(item.data_source_id) for item in scenario.data_mappings
    } | {
        str(item.data_source_id)
        for item in getattr(scenario, "relation_data_mappings", [])
    }
    sources = sorted(
        candidate_sources,
        key=lambda item: (
            str(item.id) not in referenced_source_ids,
            str(item.created_at or ""),
            str(item.id),
        ),
    )[:10]
    catalog: list[dict[str, Any]] = []
    columns: dict[tuple[str, str], set[str]] = {}
    for source in sources:
        try:
            tables = datasource_service.list_tables(source)[:40]
        except Exception:  # noqa: BLE001 - unavailable connectors are not candidates.
            continue
        safe_tables: list[dict[str, Any]] = []
        for table in tables:
            table_name = _text(table.get("name"), maximum=300)
            if not table_name:
                continue
            safe_columns = [
                {
                    "name": _text(column.get("name"), maximum=300),
                    "type": _text(column.get("type"), maximum=100),
                    "pk": bool(column.get("pk")),
                }
                for column in (table.get("columns") or [])[:120]
                if _text(column.get("name"), maximum=300)
            ]
            columns[(source.id, table_name)] = {item["name"] for item in safe_columns}
            safe_tables.append({"name": table_name, "columns": safe_columns})
        if safe_tables:
            catalog.append({
                "data_source_id": source.id,
                "data_source_name": source.name,
                "type": source.type,
                "tables": safe_tables,
            })
    return catalog, columns


def prepare_compilation_context(
    db: Session,
    scenario: BusinessScenario,
) -> dict[str, Any]:
    """Freeze the exact credential-free physical schema used by one job."""
    mapping_catalog, columns = _mapping_catalog(db, scenario)
    canonical = json.dumps(
        mapping_catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "mapping_catalog": mapping_catalog,
        "columns_by_table": columns,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _existing_catalog(scenario: BusinessScenario) -> dict[str, Any]:
    return {
        "entities": [
            {
                "id": entity.id,
                "name": entity.name,
                "is_abstract": bool(entity.is_abstract),
                "state_property": entity.state_property,
                "properties": [
                    {
                        "name": prop.name,
                        "data_type": prop.data_type,
                        "is_key": bool(prop.is_key),
                        "is_title": bool(prop.is_title),
                        "is_required": bool(prop.is_required),
                        "is_enum": bool(prop.is_enum),
                        "enum_values": prop.enum_values or [],
                    }
                    for prop in entity.properties
                ],
            }
            for entity in scenario.entities
        ],
        "relations": [
            {
                "id": item.id,
                "name": item.name,
                "source_entity_id": item.source_entity_id,
                "target_entity_id": item.target_entity_id,
                "relation_type": item.relation_type,
                "constraints": item.constraints or {},
            }
            for item in scenario.relations
        ],
        "functions": [{"id": item.id, "name": item.name} for item in scenario.function_definitions],
        "actions": [{"id": item.id, "name": item.name, "entity_id": item.entity_id} for item in scenario.actions],
        "rules": [{"id": item.id, "name": item.name, "entity_id": item.entity_id} for item in scenario.rules],
        "events": [{"id": item.id, "name": item.name} for item in scenario.events],
        "workflows": [{"id": item.id, "name": item.name} for item in scenario.workflows],
        "mappings": [
            {
                "id": item.id,
                "entity_id": item.entity_id,
                "entity_name": item.entity.name if item.entity else "",
                "data_source_id": item.data_source_id,
                "table_name": item.table_name,
                "column_map": item.column_map or {},
            }
            for item in scenario.data_mappings
        ],
        "relation_mappings": [
            {
                "id": item.id,
                "relation_id": item.relation_id,
                "source_mapping_id": item.source_mapping_id,
                "target_mapping_id": item.target_mapping_id,
                "mode": item.mode,
                "data_source_id": item.data_source_id,
                "table_name": item.table_name,
                "foreign_key_column": item.foreign_key_column or "",
                "source_key_column": item.source_key_column or "",
                "target_key_column": item.target_key_column or "",
            }
            for item in getattr(scenario, "relation_data_mappings", [])
        ],
    }


_PROMPT = """你是业务本体文档编译器。只输出一个 JSON 对象，不输出 Markdown。
目标：把附件与用户补充描述/修正建议共同编译为同一业务场景中的对象类型、属性、关系、函数契约、操作、规则、事件、工作流、对象数据映射和关系数据映射。

来源与冲突策略：
- 待逐段编译的来源记录中，source_kind=attachment 是附件基线，source_kind=user_request 是用户本次业务描述、补充或修正；两者都是业务语义来源，只有给定 ref 可以作为证据。
- user_request 不会仅因时间更新就自动覆盖附件。只有用户明确表达“修正、改为、替换、删除、以此为准”等变更意图时，才以该明确修正为准；生成后的资源必须同时引用修正段落和被修正的附件段落，并在 unresolved 中增加 blocking=false、code=USER_CORRECTION_APPLIED 的审计说明，写清旧定义与新定义。
- 附件与用户描述不一致但覆盖意图不明确时，不得自行选边：写入 blocking=true、code=SOURCE_CONFLICT，source_refs 同时引用冲突两侧，并把相关 coverage 标为 ambiguous。
- 明确修正也不能绕过缺失引用、非法约束或其他校验；任何 blocking=true 项都会使整份变更保持零写入。所有引用与约束校验通过后，整份变更才可由用户确认并原子应用。

强制要求：
1. 不得臆造文档没有说明的业务语义；不确定、冲突、关系属性、缺失引用必须写入 unresolved。
2. 每个资源必须有稳定 key、evidence_refs（引用给定段落 ref）和 0~1 confidence。
3. coverage 必须逐条覆盖所有段落，status 只能是 modeled/context/irrelevant/ambiguous，并给 reason；ambiguous 会阻止整份应用。
4. 对象类型使用行业通用名称；每个非抽象对象必须有且仅有一个 is_key=true 的主键属性和一个 is_title=true 的标题属性，二者可以是同一属性。关系基数只能 1:1、1:N、N:1、N:M；文档仅说明普通关联而没有任何基数限制时，用不施加隐式上限的 N:M。关系的 symmetric（对称）、transitive（传递）、irreflexive（反自反）、asymmetric（非对称）、antisymmetric（反对称）、acyclic（无环）和源/目标最小最大基数必须写入 relation.constraints，绝不能建模为 record rule。布尔约束只有在来源明确支持为真时才输出 JSON true；未明确时省略，不能根据关系名称猜测，也不要填充无意义的 false 默认值。基数必须输出大于等于 0 的 JSON 整数，无上限最大基数必须省略或写 null，不能写 N、many、* 或字符串数字。普通关系本来就能从源端和目标端双向遍历，不要为“反向查看”臆造逆关系；只有文档明确给出两个不同命名谓词或 inverseOf 时，才用 inverse_relation_ref 引用另一关系。查询型对称/传递/逆关系不会物化边。
5. 函数只定义输入/输出 JSON Schema，不生成代码、URL、SQL 或运行配置。所有 Schema 的 type 只能是 object/array/string/number/integer/boolean/null；日期用 type=string+format=date，日期时间用 type=string+format=date-time，decimal/float 使用 type=number。
6. 操作只描述输入、前置条件、后置效果，不生成执行器配置；平台会将其保存为停用的“待绑定”操作。
7. 规则 condition 只允许 and/or/not 与比较操作 > >= < <= == != in not_in contains not_contains is_null is_not_null；每个叶子必须明确 op。字面量比较只能是 {"field":"字段名","op":">=","value":1}；字段对字段比较必须显式使用 {"field":"结束日期","op":">=","value_field":"开始日期"}，value 与 value_field 必须且只能出现一个；判空格式只能是 {"field":"字段名","op":"is_null"}；逻辑组合格式只能是 {"op":"and","conditions":[...]}。field 和 value_field 都必须是 entity_ref 所指对象类型上已定义的直接属性；“合同金额*0.1”“日期-15天”等表达式必须先建模为函数或计算结果，关联对象上的字段必须通过关系查询或函数取得，绝不能把表达式或跨对象路径伪装成属性名。不得把另一个字段名塞进字符串 value，不得用 type/expression/自然语言代替 op；无法形成该结构时写入 unresolved，不得输出残缺规则。类等价、类互斥、继承属于当前 P0 尚未承载的类公理，必须写入 unresolved；关系基数及关系特性必须进入 relation.constraints；两类都绝不能输出为对象记录规则。severity 只能是 info/warning/critical，未说明时省略并由平台默认为 info。
8. 工作流只允许 start/end/action/rule/event/approval 节点；action/rule/event 节点用 resource_ref 引用同次生成 key、已有 ID 或唯一名称，且引用的资源必须真实存在于本次输出或已有资源目录。不能完整定义资源时不要生成悬空节点，应写入 unresolved。必须是一个开始、一个结束、无环且所有路径可达结束；每个规则节点必须明确给出 label=true 和 label=false 两条分支，缺少分支目标时写入 unresolved，不得猜测。scheduled 目前只支持 trigger_config.interval_seconds（不支持 cron）；event 必须用 trigger_config.event_ref 引用事件。事件触发已经由 trigger_config 表示，不得再用 event 节点表示“收到触发事件”；event 节点只表示发布新的下游事件，禁止发布与本工作流触发事件相同的事件。approval 节点可配置 timeout_seconds 和 on_timeout(reject/timeout)。
9. mappings 只能选择“可用数据源表结构”中的真实 data_source_id、表和列；没有候选时不要生成映射，把需求写入 code=MAPPING_DEFERRED_NO_DATA_SOURCE、blocking=false 的 unresolved。数据源尚未配置只表示物理映射延期，不阻止对象、关系、函数、操作、规则、事件和工作流的概念模型应用。
10. relation_mappings 只能引用本次 mappings 的 key 或已有映射 ID，以及本次 relations 的 key 或已有关系 ID。mode 只能是 source_fk、target_fk、join_table。source_fk/target_fk 只填写 foreign_key_column，列必须来自对应承载侧对象映射的真实表；join_table 只填写 join_data_source_ref、join_table_name、source_key_column、target_key_column，且表列必须来自“可用数据源表结构”。不得输出 SQL、查询表达式或自由 JSON 配置；证据不足时写 unresolved。

JSON 顶层字段固定为：
schema_version, entities, relations, functions, actions, rules, events, workflows, mappings, relation_mappings, unresolved, coverage。
entities: [{key,name,description,is_abstract,state_property,properties:[{name,data_type,description,is_key,is_title,is_required,is_enum,enum_values,default_value,constraints,is_sensitive}],evidence_refs,confidence}]
relations: [{key,name,source_ref,target_ref,relation_type,constraints:{symmetric,transitive,irreflexive,asymmetric,antisymmetric,acyclic,source_min_cardinality,source_max_cardinality,target_min_cardinality,target_max_cardinality},inverse_relation_ref,description,evidence_refs,confidence}]
functions: [{key,name,description,input_schema,output_schema,tags,evidence_refs,confidence}]
actions: [{key,name,entity_ref,description,input_schema,precondition,postcondition,evidence_refs,confidence}]
rules: [{key,name,entity_ref,description,condition,action_on_match,trigger_action_refs,severity,evidence_refs,confidence}]
events: [{key,name,description,payload_schema,trigger_source,evidence_refs,confidence}]
workflows: [{key,name,description,trigger_type,trigger_config,nodes,edges,evidence_refs,confidence}]
mappings: [{key,entity_ref,data_source_ref,table_name,column_map,evidence_refs,confidence}]
relation_mappings: [{key,relation_ref,source_mapping_ref,target_mapping_ref,mode,foreign_key_column,join_data_source_ref,join_table_name,source_key_column,target_key_column,evidence_refs,confidence}]
unresolved: [{code,message,source_refs,blocking}]
coverage: [{source_ref,status,reason,change_keys}]
"""

_FALLBACK_CHUNK_PROMPT = """
这是整份文档的超时降级分块，不是独立文档。
1. 只提取有本分块 evidence_refs 支持的资源，并且逐条输出本分块全部段落的 coverage。
2. key 必须由“资源类型+业务名称”稳定生成，不得包含分块序号。
3. 可以用稳定 key 或唯一业务名称引用可能定义在其他分块的资源；引用当前场景目录中的资源时必须逐字使用目录给出的 ID 或唯一名称，引用同次生成资源时必须逐字使用该资源输出中的 key 或唯一名称。不得翻译、缩写或另造英文 key 来引用已有/已生成的中文对象。不要仅因本分块看不到定义而写 unresolved。
4. 不得引用本分块之外的段落 ref。后续会在全局视图中合并同名/同 key 资源、校验跨块引用并统一规范化。
"""


def _compiler_prompt(
    scenario: BusinessScenario,
    *,
    message: str,
    paragraphs: list[dict[str, str]],
    mapping_catalog: list[dict[str, Any]],
    chunk_index: int | str | None = None,
    chunk_count: int | None = None,
) -> str:
    chunk_instruction = ""
    if chunk_index is not None and chunk_count is not None:
        chunk_instruction = (
            _FALLBACK_CHUNK_PROMPT
            + f"\n本分块为 {chunk_index}/{chunk_count}，只处理下方给出的段落。\n"
        )
    raw_message = str(message or "").strip()
    has_structured_sections, structured = _structured_request_source(raw_message)
    if structured or (
        raw_message
        and not has_structured_sections
        and not _is_pure_compilation_control(raw_message)
    ):
        task_instruction = (
            "用户输入中的业务描述或修正已在完整来源清单中登记为 "
            "source_kind=user_request 的 request:* 来源；本分块只能引用下方实际"
            "出现的 ref，不要从本控制说明复制或推断业务语义。"
        )
    elif has_structured_sections:
        task_instruction = (
            "用户本次显式业务描述为空；只编译下方附件来源，不得把控制模板或"
            "空值占位语句当作业务证据。"
        )
    else:
        task_instruction = _text(raw_message, maximum=12_000)
    return (
        _PROMPT
        + chunk_instruction
        + "\n当前场景：\n"
        + json.dumps(_existing_catalog(scenario), ensure_ascii=False)
        + "\n可用数据源表结构：\n"
        + json.dumps(mapping_catalog, ensure_ascii=False)
        + "\n编译控制说明（不可作为业务证据）：\n"
        + task_instruction
        + "\n待逐段编译的业务语义来源：\n"
        + json.dumps(paragraphs, ensure_ascii=False)
    )


def _paragraph_record_size(paragraph: dict[str, str]) -> int:
    return len(json.dumps(paragraph, ensure_ascii=False, separators=(",", ":")))


def _source_chunks(
    paragraphs: Iterable[dict[str, str]],
    *,
    maximum: int | None = None,
) -> list[list[dict[str, str]]]:
    """Partition whole paragraph records in source order without overlap or loss."""
    maximum = FALLBACK_SOURCE_CHARS if maximum is None else maximum
    if maximum <= 0:
        raise ValueError("文档降级分块上限必须为正整数")
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_size = 0
    for paragraph in paragraphs:
        item_size = _paragraph_record_size(paragraph)
        if current and current_size + item_size > maximum:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(paragraph)
        current_size += item_size
    if current:
        chunks.append(current)
    return chunks


def _bisect_source_chunk(
    paragraphs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
    """Split once near half the serialized character weight, preserving order."""
    if len(paragraphs) < 2:
        return None
    sizes = [_paragraph_record_size(item) for item in paragraphs]
    total = sum(sizes)
    prefix = 0
    candidates: list[tuple[int, int]] = []
    for index, size in enumerate(sizes[:-1], 1):
        prefix += size
        candidates.append((abs(total - (2 * prefix)), index))
    _, split_index = min(candidates)
    return paragraphs[:split_index], paragraphs[split_index:]


def _validate_raw_contract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("复合业务模型必须是 JSON 对象")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("复合业务模型缺少受支持的 schema_version")
    for section in (*_RESOURCE_SECTIONS, "unresolved", "coverage"):
        if not isinstance(raw.get(section), list):
            raise ValueError(f"复合业务模型字段 {section} 必须是数组")
        if any(not isinstance(item, dict) for item in raw[section]):
            raise ValueError(f"复合业务模型字段 {section} 包含非对象条目")
    return raw


def _canonicalize_chunk_schema_version(raw: Any) -> Any:
    """Treat a chunk's version as platform-owned transport metadata."""
    if not isinstance(raw, dict):
        return raw
    return {**raw, "schema_version": SCHEMA_VERSION}


def _validate_chunk_source_scope(
    raw: dict[str, Any],
    *,
    allowed_refs: set[str],
) -> None:
    """Fail a chunk response that leaks provenance across chunk boundaries."""
    cited_refs = {
        str(ref)
        for section in _RESOURCE_SECTIONS
        for item in raw.get(section) or []
        for ref in (item.get("evidence_refs") or [])
    }
    cited_refs.update(
        str(ref)
        for item in raw.get("unresolved") or []
        for ref in (item.get("source_refs") or [])
    )
    foreign_refs = sorted(cited_refs - allowed_refs)
    if foreign_refs:
        raise ValueError(
            "分块结果引用了当前分块之外的来源段落："
            + "、".join(foreign_refs)
        )
    coverage_refs = [str(item.get("source_ref") or "") for item in raw.get("coverage") or []]
    if len(coverage_refs) != len(set(coverage_refs)):
        raise ValueError("分块结果包含重复的 coverage source_ref")
    if set(coverage_refs) != allowed_refs:
        missing = sorted(allowed_refs - set(coverage_refs))
        extra = sorted(set(coverage_refs) - allowed_refs)
        detail = []
        if missing:
            detail.append("缺少 " + "、".join(missing))
        if extra:
            detail.append("越界 " + "、".join(extra))
        raise ValueError("分块 coverage 必须逐条且仅覆盖当前段落：" + "；".join(detail))


def _is_provider_timeout(error: BaseException) -> bool:
    """Recognize timeout wrappers propagated by OpenAI/httpx without string matching."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    timeout_names = {
        "APITimeoutError", "ConnectTimeout", "PoolTimeout", "ReadTimeout",
        "TimeoutError", "TimeoutException", "WriteTimeout",
    }
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, TimeoutError) or type(current).__name__ in timeout_names:
            return True
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _is_transient_provider_error(error: BaseException) -> bool:
    """Retry only transport, timeout, rate-limit and provider 5xx failures."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    transient_names = {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
    }
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if _is_provider_timeout(current) or type(current).__name__ in transient_names:
            return True
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and (
            status_code in {408, 409, 425, 429} or status_code >= 500
        ):
            return True
        message = str(current or "").strip().lower()
        if any(token in message for token in (
            "connection error",
            "connection reset",
            "temporarily unavailable",
            "service unavailable",
            "rate limit",
            "timed out",
            "timeout",
        )):
            return True
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _response_finish_reason(response: Any) -> str:
    """Read OpenAI-compatible choice.finish_reason from object or dict payloads."""
    if not isinstance(response, dict):
        return ""
    raw = response.get("raw")

    def field(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    choices = field(raw, "choices") or []
    if not isinstance(choices, (list, tuple)) or not choices:
        return ""
    choice = choices[0]
    reason = field(choice, "finish_reason")
    if not reason:
        reason = field(field(choice, "message"), "finish_reason")
    return str(reason or "").strip().lower()


def _chat_raw_model(
    db: Session,
    llm: Any,
    prompt: str,
    *,
    max_tokens: int,
    allowed_refs: set[str] | None = None,
    attempts: int = 3,
    call_budget: LLMCallBudget | None = None,
) -> dict[str, Any]:
    """Call the provider and retry only malformed compiler JSON."""
    last_error: Exception | None = None
    for attempt_index in range(attempts):
        try:
            response = llm_service.chat(
                llm,
                [
                    {"role": "system", "content": "你只输出符合给定闭合契约的合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                request_timeout=get_settings().scenario_model_llm_timeout,
                max_retries=0,
                db=db,
                before_provider_call=(
                    (lambda: call_budget.consume("chunk_provider_call"))
                    if call_budget is not None else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - only known transient failures retry.
            # A provider timeout may already have consumed the full request
            # deadline; retrying it synchronously can multiply latency by
            # tens of minutes. Preserve the existing fail-fast timeout policy.
            if _is_provider_timeout(exc):
                raise
            if not _is_transient_provider_error(exc):
                raise
            last_error = exc
            if attempt_index + 1 < attempts:
                continue
            raise CompilerProviderUnavailable(
                f"模型服务连接连续 {attempts} 次失败；任务已保持零写入，请稍后显式重试"
            ) from exc
        if _response_finish_reason(response) == "length":
            raise _CompilerOutputTruncated("分块编译输出达到 token 上限")
        try:
            extracted = ontology_service._extract_json(response.get("content", ""))
            if allowed_refs is not None:
                extracted = _canonicalize_chunk_schema_version(extracted)
            raw = _validate_raw_contract(extracted)
            if allowed_refs is not None:
                _validate_chunk_source_scope(raw, allowed_refs=allowed_refs)
            return raw
        except Exception as exc:  # noqa: BLE001 - retry malformed model output.
            # A malformed response for several source paragraphs is usually a
            # complexity/size failure. Repeating the identical prompt spends
            # budget without improving auditability; deterministic bisection
            # preserves every source ref and gives each branch a smaller
            # closed contract. A single paragraph still receives the bounded
            # retry below because it cannot be split further.
            if allowed_refs is not None and len(allowed_refs) > 1:
                raise _CompilerOutputTruncated(
                    "多段来源块未通过闭合 JSON 契约，需要按来源二分"
                ) from exc
            if _malformed_output_is_probably_truncated(
                response.get("content", ""), exc
            ):
                raise _CompilerOutputTruncated(
                    "分块编译返回了在末尾截断的超长 JSON"
                ) from exc
            last_error = exc
    raise ValueError(f"复合业务模型连续 {attempts} 次输出无效：{last_error}")


def _missing_fragment(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_json_fragment(
    current: Any,
    incoming: Any,
    *,
    path: str,
    conflicts: set[str],
) -> Any:
    """Merge complementary repeated fragments, retaining first value on conflict."""
    if current == incoming or _missing_fragment(incoming):
        return current
    if _missing_fragment(current):
        return copy.deepcopy(incoming)
    if (
        isinstance(current, str)
        and isinstance(incoming, str)
        and (path == "description" or path.endswith(".description"))
    ):
        return "；".join(dict.fromkeys((current, incoming)))
    if isinstance(current, dict) and isinstance(incoming, dict):
        for key, value in incoming.items():
            if key not in current:
                current[key] = copy.deepcopy(value)
                continue
            current[key] = _merge_json_fragment(
                current[key], value, path=f"{path}.{key}", conflicts=conflicts
            )
        return current
    if isinstance(current, list) and isinstance(incoming, list):
        if all(not isinstance(item, (dict, list)) for item in [*current, *incoming]):
            for value in incoming:
                if value not in current:
                    current.append(copy.deepcopy(value))
            return current
        if all(isinstance(item, dict) for item in [*current, *incoming]):
            identity_field = next(
                (
                    field
                    for field in ("id", "name", "key")
                    if all(str(item.get(field) or "") for item in [*current, *incoming])
                ),
                None,
            )
            if identity_field:
                by_identity = {str(item[identity_field]): item for item in current}
                for value in incoming:
                    identity = str(value[identity_field])
                    existing = by_identity.get(identity)
                    if existing is None:
                        copied = copy.deepcopy(value)
                        current.append(copied)
                        by_identity[identity] = copied
                    else:
                        _merge_json_fragment(
                            existing,
                            value,
                            path=f"{path}[{identity_field}={identity}]",
                            conflicts=conflicts,
                        )
                return current
        conflicts.add(path)
        return current
    conflicts.add(path)
    return current


def _canonicalize_merged_entity_enum_flags(
    target: dict[str, Any],
    incoming: dict[str, Any],
) -> None:
    """Resolve the redundant ``is_enum`` flag before chunk conflict checks.

    A non-empty ``enum_values`` list is the actual enum declaration in the
    compiler contract.  When another chunk retained the model's default
    ``is_enum=false``, treating that redundant flag as a semantic conflict
    creates a false blocker even though the merged property is unambiguous.
    """
    target_properties = target.get("properties")
    incoming_properties = incoming.get("properties")
    if not isinstance(target_properties, list) or not isinstance(incoming_properties, list):
        return
    target_by_name = {
        str(prop.get("name") or ""): prop
        for prop in target_properties
        if isinstance(prop, dict) and str(prop.get("name") or "")
    }
    for incoming_property in incoming_properties:
        if not isinstance(incoming_property, dict):
            continue
        target_property = target_by_name.get(str(incoming_property.get("name") or ""))
        if target_property is None:
            continue
        if target_property.get("enum_values") or incoming_property.get("enum_values"):
            target_property["is_enum"] = True
            incoming_property["is_enum"] = True


def _canonicalize_merged_entity_property_types(
    target: dict[str, Any],
    incoming: dict[str, Any],
) -> None:
    """Collapse only runtime-equivalent property type spellings before merge."""
    target_properties = target.get("properties")
    incoming_properties = incoming.get("properties")
    if not isinstance(target_properties, list) or not isinstance(incoming_properties, list):
        return
    target_by_name = {
        str(prop.get("name") or ""): prop
        for prop in target_properties
        if isinstance(prop, dict) and str(prop.get("name") or "")
    }
    equivalent_families = (
        {"string", "text"},
        {"float", "number", "numeric", "decimal", "double"},
    )
    for incoming_property in incoming_properties:
        if not isinstance(incoming_property, dict):
            continue
        target_property = target_by_name.get(str(incoming_property.get("name") or ""))
        if target_property is None:
            continue
        target_type = str(target_property.get("data_type") or "string").strip().lower()
        incoming_type = str(incoming_property.get("data_type") or "string").strip().lower()
        if target_type == incoming_type:
            continue
        if any({target_type, incoming_type} <= family for family in equivalent_families):
            incoming_property["data_type"] = target_property.get("data_type") or target_type


def _canonicalize_merged_relation_fragment(fragment: dict[str, Any]) -> None:
    """Remove syntax/default noise before comparing relation fragments.

    Relation constraints persist only positive boolean axioms and concrete
    cardinality bounds. Consequently ``false``/``null`` are absence rather
    than contrary assertions. Canonicalizing each chunk independently lets a
    later positive assertion enrich the merged resource while genuine true
    values, endpoint, name and multiplicity disagreements remain conflicts.
    """
    raw_relation_type = fragment.get("relation_type")
    relation_type = _normalize_compiler_relation_type(raw_relation_type)
    if relation_type:
        fragment["relation_type"] = relation_type

    if "constraints" not in fragment:
        return
    try:
        normalized, inverse_ref = _normalize_compiler_relation_constraints(
            fragment.get("constraints"),
            relation_type=relation_type or "N:M",
        )
    except (TypeError, ValueError):
        # Leave malformed input untouched so global normalization emits the
        # usual closed-schema blocker instead of concealing the defect.
        return
    if inverse_ref:
        normalized["inverse_relation_ref"] = inverse_ref
    fragment["constraints"] = normalized


def _merge_resource_fragment(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    section: str,
    unresolved: list[dict[str, Any]],
    conflict_index: dict[
        tuple[str, str], tuple[dict[str, Any], set[str]]
    ] | None = None,
) -> None:
    if section == "entities":
        _canonicalize_merged_entity_enum_flags(target, incoming)
        _canonicalize_merged_entity_property_types(target, incoming)
    elif section == "relations":
        _canonicalize_merged_relation_fragment(target)
        _canonicalize_merged_relation_fragment(incoming)
    target["evidence_refs"] = list(dict.fromkeys([
        *(str(ref) for ref in (target.get("evidence_refs") or [])),
        *(str(ref) for ref in (incoming.get("evidence_refs") or [])),
    ]))
    numeric_confidences = [
        float(value)
        for value in (target.get("confidence", 0), incoming.get("confidence", 0))
        if not isinstance(value, bool) and isinstance(value, (int, float))
    ]
    target["confidence"] = max(numeric_confidences, default=0.0)
    conflicts: set[str] = set()
    if (
        target.get("name") and incoming.get("name")
        and str(target["name"]).strip() != str(incoming["name"]).strip()
    ):
        conflicts.add("name")
    for field, value in incoming.items():
        if field in {"key", "name", "evidence_refs", "confidence"}:
            continue
        if field not in target:
            target[field] = copy.deepcopy(value)
            continue
        target[field] = _merge_json_fragment(
            target[field], value, path=field, conflicts=conflicts
        )
    if conflicts:
        key = str(target.get("key") or target.get("name") or "未命名资源")
        if conflict_index is not None:
            identity = (section, key)
            indexed = conflict_index.get(identity)
            if indexed is None:
                issue = {
                    "code": "chunk_resource_conflict",
                    "message": "",
                    "source_refs": [],
                    "blocking": True,
                }
                fields = set(conflicts)
                conflict_index[identity] = (issue, fields)
                unresolved.append(issue)
            else:
                issue, fields = indexed
                fields.update(conflicts)
            issue["source_refs"] = list(dict.fromkeys([
                *(str(ref) for ref in (issue.get("source_refs") or [])),
                *(str(ref) for ref in (target.get("evidence_refs") or [])),
            ]))
            issue["message"] = (
                f"分块对 {section} 资源 {key} 的定义不一致："
                + "、".join(sorted(fields)[:12])
            )
            return
        _issue(
            unresolved,
            "chunk_resource_conflict",
            f"分块对 {section} 资源 {key} 的定义不一致："
            + "、".join(sorted(conflicts)[:12]),
            source_refs=target.get("evidence_refs") or [],
        )


def _canonical_generated_key(section: str, value: Any) -> str:
    """Give generated resources a stable, globally unique typed key.

    Chunk extraction can emit ``entity_building``, ``entity:building`` or an
    unqualified ``building`` for the same object.  This function only repairs
    that syntactic variation; it deliberately does not guess semantic
    equivalence between different business words.
    """
    token = str(value or "").strip()
    if not token:
        return token
    prefix = _RESOURCE_KEY_PREFIXES[section]
    all_prefixes = {
        alias
        for aliases in _RESOURCE_PREFIX_ALIASES.values()
        for alias in aliases
    }
    expected_prefixes = sorted(
        _RESOURCE_PREFIX_ALIASES[section], key=len, reverse=True
    )
    while token:
        match = re.match(r"^([A-Za-z]+)[._:-]+(.+)$", token)
        if match and match.group(1).lower() in all_prefixes:
            token = match.group(2).strip()
            continue
        lowered = token.lower()
        compact_match = next(
            (
                alias
                for alias in expected_prefixes
                if lowered.startswith(alias)
                and len(token) > len(alias)
                and (
                    not token[len(alias)].isascii()
                    or token[len(alias)].isupper()
                )
            ),
            None,
        )
        if compact_match is None:
            break
        token = token[len(compact_match):].strip()
    return f"{prefix}.{token}" if token else ""


def _generated_key_aliases(section: str, key: str) -> set[str]:
    """Return deterministic spelling aliases for one canonical typed key."""
    canonical = _canonical_generated_key(section, key)
    if not canonical:
        return set()
    prefix, suffix = canonical.split(".", 1)
    aliases = {canonical}
    for type_prefix in _RESOURCE_PREFIX_ALIASES[section]:
        aliases.update({
            f"{type_prefix}.{suffix}",
            f"{type_prefix}_{suffix}",
            f"{type_prefix}:{suffix}",
            f"{type_prefix}-{suffix}",
        })
    return aliases


def _register_generated_alias(
    aliases: dict[str, set[str]],
    alias: Any,
    canonical_key: str,
) -> None:
    token = str(alias or "").strip()
    if token and canonical_key:
        aliases.setdefault(token, set()).add(canonical_key)


def _alias_token(value: Any, aliases: dict[str, set[str]]) -> Any:
    if not isinstance(value, str):
        return value
    matches = aliases.get(value.strip(), set())
    # A deterministic repair is safe only when the token identifies exactly
    # one resource of the expected type.  Keeping an ambiguous token unchanged
    # lets the normalizer emit the existing ambiguous_reference blocker.
    return next(iter(matches)) if len(matches) == 1 else value


def _rewrite_resource_item_aliases(
    section: str,
    item: dict[str, Any],
    aliases: dict[str, dict[str, set[str]]],
) -> None:
    """Rewrite one resource's references using only their expected type."""
    entity_aliases = aliases["entities"]
    if section == "relations":
        for field in ("source_ref", "target_ref", "source", "target"):
            if field in item:
                item[field] = _alias_token(item[field], entity_aliases)
        if "inverse_relation_ref" in item:
            item["inverse_relation_ref"] = _alias_token(
                item["inverse_relation_ref"], aliases["relations"]
            )
        constraints = item.get("constraints")
        if isinstance(constraints, dict) and "inverse_relation_ref" in constraints:
            constraints["inverse_relation_ref"] = _alias_token(
                constraints["inverse_relation_ref"], aliases["relations"]
            )
        return
    if section == "actions":
        if "entity_ref" in item:
            item["entity_ref"] = _alias_token(item["entity_ref"], entity_aliases)
        return
    if section == "rules":
        if "entity_ref" in item:
            item["entity_ref"] = _alias_token(item["entity_ref"], entity_aliases)
        item["trigger_action_refs"] = list(dict.fromkeys(
            _alias_token(str(value), aliases["actions"])
            for value in (item.get("trigger_action_refs") or [])
        ))
        return
    if section == "workflows":
        trigger_config = item.get("trigger_config")
        if isinstance(trigger_config, dict):
            for field in ("event_ref", "event_id", "event_name"):
                if field in trigger_config:
                    trigger_config[field] = _alias_token(
                        trigger_config[field], aliases["events"]
                    )
        for node in item.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_section = {
                "action": "actions",
                "rule": "rules",
                "event": "events",
            }.get(str(node.get("type") or "").strip().lower())
            if node_section is None:
                continue
            node_aliases = aliases[node_section]
            if "resource_ref" in node:
                node["resource_ref"] = _alias_token(
                    node["resource_ref"], node_aliases
                )
            data = node.get("data")
            if isinstance(data, dict):
                for field in ("resource_ref", "action_id", "rule_id", "event_id"):
                    if field in data:
                        data[field] = _alias_token(data[field], node_aliases)
        return
    if section == "mappings" and "entity_ref" in item:
        item["entity_ref"] = _alias_token(item["entity_ref"], entity_aliases)
        return
    if section == "relation_mappings":
        if "relation_ref" in item:
            item["relation_ref"] = _alias_token(
                item["relation_ref"], aliases["relations"]
            )
        for field in ("source_mapping_ref", "target_mapping_ref"):
            if field in item:
                item[field] = _alias_token(item[field], aliases["mappings"])


def _rewrite_merged_aliases(
    raw: dict[str, Any],
    aliases: dict[str, dict[str, set[str]]],
) -> None:
    for section in _RESOURCE_SECTIONS:
        for item in raw.get(section) or []:
            if isinstance(item, dict):
                _rewrite_resource_item_aliases(section, item, aliases)
    global_aliases: dict[str, set[str]] = defaultdict(set)
    for section_aliases in aliases.values():
        for token, matches in section_aliases.items():
            global_aliases[token].update(matches)
    for item in raw.get("coverage") or []:
        item["change_keys"] = list(dict.fromkeys(
            _alias_token(str(value), global_aliases)
            for value in (item.get("change_keys") or [])
        ))


def _reconcile_generated_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize keys and typed references without merging resources.

    The full-document path does not pass through ``_merge_chunk_models``.  It
    still needs the same deterministic spelling repair, while duplicate or
    semantically ambiguous resources must remain visible to normal validation.
    """
    reconciled = copy.deepcopy(raw)
    aliases: dict[str, dict[str, set[str]]] = {
        section: {} for section in _RESOURCE_SECTIONS
    }
    for section in _RESOURCE_SECTIONS:
        for item in reconciled.get(section) or []:
            if not isinstance(item, dict):
                continue
            raw_key = str(item.get("key") or "").strip()
            canonical_key = _canonical_generated_key(section, raw_key)
            if canonical_key:
                item["key"] = canonical_key
            for alias in {
                raw_key,
                str(item.get("name") or "").strip(),
                *_generated_key_aliases(section, raw_key),
                *_generated_key_aliases(section, canonical_key),
            }:
                _register_generated_alias(
                    aliases[section], alias, canonical_key
                )
    _rewrite_merged_aliases(reconciled, aliases)
    return reconciled


def _merge_chunk_models(models: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically merge chunk extracts before global normalization."""
    model_list = list(models)
    merged: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        **{section: [] for section in _RESOURCE_SECTIONS},
        "unresolved": [],
        "coverage": [],
    }
    aliases: dict[str, dict[str, set[str]]] = {
        section: {} for section in _RESOURCE_SECTIONS
    }
    conflict_index: dict[
        tuple[str, str], tuple[dict[str, Any], set[str]]
    ] = {}
    for model in model_list:
        for item in model.get("unresolved") or []:
            if isinstance(item, dict):
                merged["unresolved"].append(copy.deepcopy(item))

    for section in _RESOURCE_SECTIONS:
        by_key: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for model in model_list:
            for raw_item in model.get(section) or []:
                item = copy.deepcopy(raw_item)
                raw_key = str(item.get("key") or "").strip()
                key = _canonical_generated_key(section, raw_key)
                if key:
                    item["key"] = key
                # Referenced sections precede their consumers in
                # _RESOURCE_SECTIONS.  Canonicalizing each incoming fragment
                # now prevents spelling-only differences (key vs name or
                # entity_x vs entity.x) from being reported as chunk resource
                # conflicts before the final global rewrite runs.
                _rewrite_resource_item_aliases(section, item, aliases)
                name = str(item.get("name") or "").strip()
                key_match = by_key.get(key) if key else None
                has_semantic_name = section not in {"mappings", "relation_mappings"}
                name_match = by_name.get(name) if name and has_semantic_name else None
                if key_match is not None and name_match is not None and key_match is not name_match:
                    _issue(
                        merged["unresolved"],
                        "chunk_resource_identity_conflict",
                        f"分块中 {section} 的 key {key} 与名称 {name} 指向不同资源",
                        source_refs=item.get("evidence_refs") or [],
                    )
                target = key_match or name_match
                if target is None:
                    merged[section].append(item)
                    if key:
                        by_key[key] = item
                    if name and has_semantic_name:
                        by_name[name] = item
                    for alias in {raw_key, name, *_generated_key_aliases(section, key)}:
                        _register_generated_alias(aliases[section], alias, key)
                    continue
                if not target.get("key") and key:
                    target["key"] = key
                    by_key[key] = target
                if not target.get("name") and name:
                    target["name"] = name
                    if has_semantic_name:
                        by_name[name] = target
                canonical_key = str(target.get("key") or "").strip()
                if key and canonical_key and key != canonical_key:
                    by_key[key] = target
                for alias in {
                    raw_key,
                    key,
                    name,
                    *_generated_key_aliases(section, raw_key),
                    *_generated_key_aliases(section, key),
                }:
                    _register_generated_alias(
                        aliases[section], alias, canonical_key
                    )
                _merge_resource_fragment(
                    target,
                    item,
                    section=section,
                    unresolved=merged["unresolved"],
                    conflict_index=conflict_index,
                )

        # Include final key/name spellings after every fragment has merged.
        for item in merged[section]:
            canonical_key = str(item.get("key") or "").strip()
            for alias in {
                canonical_key,
                str(item.get("name") or "").strip(),
                *_generated_key_aliases(section, canonical_key),
            }:
                _register_generated_alias(
                    aliases[section], alias, canonical_key
                )

    coverage_by_ref: dict[str, dict[str, Any]] = {}
    for model in model_list:
        for raw_item in model.get("coverage") or []:
            item = copy.deepcopy(raw_item)
            source_ref = str(item.get("source_ref") or "")
            existing = coverage_by_ref.get(source_ref)
            if existing is None:
                coverage_by_ref[source_ref] = item
                merged["coverage"].append(item)
                continue
            existing["change_keys"] = list(dict.fromkeys([
                *(str(value) for value in (existing.get("change_keys") or [])),
                *(str(value) for value in (item.get("change_keys") or [])),
            ]))
            existing_reason = str(existing.get("reason") or "").strip()
            incoming_reason = str(item.get("reason") or "").strip()
            if incoming_reason and incoming_reason != existing_reason:
                existing["reason"] = "；".join(
                    value for value in (existing_reason, incoming_reason) if value
                )
            if item.get("status") != existing.get("status"):
                existing["status"] = "ambiguous"
                _issue(
                    merged["unresolved"],
                    "chunk_coverage_conflict",
                    f"分块对来源段落 {source_ref} 的 coverage 状态不一致",
                    source_refs=[source_ref],
                )
    _rewrite_merged_aliases(merged, aliases)
    return merged


def _extract_chunk_models_recursively(
    db: Session,
    scenario: BusinessScenario,
    *,
    message: str,
    llm: Any,
    mapping_catalog: list[dict[str, Any]],
    paragraphs: list[dict[str, str]],
    chunk_label: str,
    chunk_count: int,
    call_budget: LLMCallBudget | None = None,
) -> list[dict[str, Any]]:
    """Retry only truncated branches by stable character-weighted bisection."""
    chunk_prompt = _compiler_prompt(
        scenario,
        message=message,
        paragraphs=paragraphs,
        mapping_catalog=mapping_catalog,
        chunk_index=chunk_label,
        chunk_count=chunk_count,
    )
    try:
        return [_chat_raw_model(
            db,
            llm,
            chunk_prompt,
            max_tokens=FALLBACK_MAX_OUTPUT_TOKENS,
            allowed_refs={item["ref"] for item in paragraphs},
            call_budget=call_budget,
        )]
    except _CompilerOutputTruncated as exc:
        halves = _bisect_source_chunk(paragraphs)
        if halves is None:
            source_ref = str(paragraphs[0].get("ref") or "未知段落")
            raise ValueError(
                f"来源段落 {source_ref} 单独编译仍达到 "
                f"{FALLBACK_MAX_OUTPUT_TOKENS} token 输出上限；"
                "系统不会丢弃该段落，请简化或拆分其内容后重试"
            ) from exc
        results: list[dict[str, Any]] = []
        for child_index, child in enumerate(halves, 1):
            results.extend(_extract_chunk_models_recursively(
                db,
                scenario,
                message=message,
                llm=llm,
                mapping_catalog=mapping_catalog,
                paragraphs=child,
                chunk_label=f"{chunk_label}.{child_index}",
                chunk_count=chunk_count,
                call_budget=call_budget,
            ))
        return results


def _compile_scenario_model_in_chunks(
    db: Session,
    scenario: BusinessScenario,
    *,
    message: str,
    llm: Any,
    source_bundle: dict[str, Any],
    mapping_catalog: list[dict[str, Any]],
    columns: dict[tuple[str, str], set[str]],
    call_budget: LLMCallBudget | None = None,
) -> dict[str, Any]:
    """Extract bounded chunks, then normalize once with the complete source view."""
    chunks = _source_chunks(source_bundle["paragraphs"])
    chunk_models: list[dict[str, Any]] = []
    for chunk_index, paragraphs in enumerate(chunks, 1):
        chunk_models.extend(_extract_chunk_models_recursively(
            db,
            scenario,
            message=message,
            llm=llm,
            mapping_catalog=mapping_catalog,
            paragraphs=paragraphs,
            chunk_label=str(chunk_index),
            chunk_count=len(chunks),
            call_budget=call_budget,
        ))
    raw = _merge_chunk_models(chunk_models)
    normalized = normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=source_bundle,
        mapping_catalog=mapping_catalog,
        columns_by_table=columns,
    )
    if not any(
        item.get("blocking", True)
        for item in (normalized.get("unresolved") or [])
    ):
        preflight_scenario_model(
            db,
            scenario,
            normalized,
            inspect_mappings=False,
        )
    return normalized


def compile_scenario_model(
    db: Session,
    scenario: BusinessScenario,
    *,
    message: str,
    documents: Iterable[dict[str, Any]],
    llm: Any,
    call_budget: LLMCallBudget | None = None,
    prepared_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile and normalize one full document without writing scenario definitions."""
    if llm is None:
        raise ValueError("请先配置并启用一个默认 LLM")
    source_bundle = build_source_bundle(message, documents)
    if prepared_context is None:
        prepared_context = prepare_compilation_context(db, scenario)
    mapping_catalog = copy.deepcopy(prepared_context.get("mapping_catalog") or [])
    columns = {
        tuple(key): set(value)
        for key, value in (prepared_context.get("columns_by_table") or {}).items()
    }
    expected_context_hash = hashlib.sha256(json.dumps(
        mapping_catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if str(prepared_context.get("fingerprint") or "") != expected_context_hash:
        raise ValueError("编译数据映射上下文指纹不一致，拒绝使用非冻结表结构")
    if source_bundle["total_characters"] > DIRECT_CHUNK_SOURCE_CHARS:
        return _compile_scenario_model_in_chunks(
            db,
            scenario,
            message=message,
            llm=llm,
            source_bundle=source_bundle,
            mapping_catalog=mapping_catalog,
            columns=columns,
            call_budget=call_budget,
        )
    prompt = _compiler_prompt(
        scenario,
        message=message,
        paragraphs=source_bundle["paragraphs"],
        mapping_catalog=mapping_catalog,
    )
    last_error: Exception | None = None
    for attempt_index in range(3):
        try:
            response = llm_service.chat(
                llm,
                [
                    {"role": "system", "content": "你只输出符合给定闭合契约的合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=MAX_OUTPUT_TOKENS,
                request_timeout=get_settings().scenario_model_llm_timeout,
                max_retries=0,
                db=db,
                before_provider_call=(
                    (lambda: call_budget.consume("direct_provider_call"))
                    if call_budget is not None else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - bounded transport recovery only.
            if _is_provider_timeout(exc):
                return _compile_scenario_model_in_chunks(
                    db,
                    scenario,
                    message=message,
                    llm=llm,
                    source_bundle=source_bundle,
                    mapping_catalog=mapping_catalog,
                    columns=columns,
                    call_budget=call_budget,
                )
            if not _is_transient_provider_error(exc):
                raise
            last_error = exc
            if attempt_index < 2:
                continue
            raise CompilerProviderUnavailable(
                "模型服务连接连续 3 次失败；任务已保持零写入，请稍后显式重试"
            ) from exc
        if _response_finish_reason(response) == "length":
            return _compile_scenario_model_in_chunks(
                db,
                scenario,
                message=message,
                llm=llm,
                source_bundle=source_bundle,
                mapping_catalog=mapping_catalog,
                columns=columns,
                call_budget=call_budget,
            )
        try:
            raw = ontology_service._extract_json(response.get("content", ""))
            normalized = normalize_scenario_model(
                db,
                scenario,
                raw,
                source_bundle=source_bundle,
                mapping_catalog=mapping_catalog,
                columns_by_table=columns,
            )
            if not any(
                item.get("blocking", True)
                for item in (normalized.get("unresolved") or [])
            ):
                preflight_scenario_model(
                    db,
                    scenario,
                    normalized,
                    inspect_mappings=False,
                )
            return normalized
        except Exception as exc:  # noqa: BLE001 - retry malformed model output.
            last_error = exc
    raise ValueError(f"复合业务模型连续三次编译失败：{last_error}")


def _meta(
    raw: dict[str, Any],
    *,
    key: str,
    valid_sources: set[str],
    unresolved: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = [
        str(value) for value in (raw.get("evidence_refs") or [])
        if str(value) in valid_sources
    ]
    invalid = sorted({
        str(value) for value in (raw.get("evidence_refs") or [])
        if str(value) not in valid_sources
    })
    if invalid:
        unresolved.append({
            "code": "invalid_evidence_reference",
            "message": f"{key} 引用了不存在的来源段落：{'、'.join(invalid)}",
            "source_refs": [],
            "blocking": True,
        })
    if not evidence:
        unresolved.append({
            "code": "missing_evidence",
            "message": f"{key} 没有可核验的来源段落",
            "source_refs": [],
            "blocking": True,
        })
    confidence = raw.get("confidence", 0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0
    return {
        "key": key,
        "evidence_refs": list(dict.fromkeys(evidence)),
        "confidence": max(0.0, min(1.0, float(confidence))),
    }


def _issue(
    unresolved: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    source_refs: Iterable[str] = (),
    blocking: bool = True,
    reported_code: str | None = None,
) -> None:
    candidate = {
        "code": code,
        "message": message,
        "source_refs": list(dict.fromkeys(str(value) for value in source_refs if str(value))),
        "blocking": blocking,
    }
    if reported_code is not None:
        candidate["reported_code"] = reported_code
    signature = (candidate["code"], candidate["message"], tuple(candidate["source_refs"]))
    existing = {
        (item.get("code"), item.get("message"), tuple(item.get("source_refs") or []))
        for item in unresolved
    }
    if signature not in existing:
        unresolved.append(candidate)


def _normalize_reported_issue_code(value: Any) -> str:
    """Canonicalize an untrusted model issue code without widening policy."""
    return _text(value or "DOCUMENT_AMBIGUITY", maximum=100).upper()


def _raw_issue_can_be_nonblocking(
    code: str,
    *,
    mapping_is_cleanly_deferred: bool,
) -> bool:
    if code in _RAW_NONBLOCKING_AUDIT_CODES:
        return True
    return (
        mapping_is_cleanly_deferred
        and code in _RAW_NONBLOCKING_PHYSICAL_MAPPING_CODES
    )


def _assert_unresolved_severity_policy(
    unresolved: list[dict[str, Any]],
    *,
    mapping_is_cleanly_deferred: bool,
) -> None:
    """Fail closed if any final non-blocking issue escaped the closed policy.

    Raw model issues remain grouped as ``document_reported_issue`` so they can
    never impersonate a platform validator code.  Their normalized source code
    is carried separately and checked again at the final proposal boundary.
    """
    unsafe: list[str] = []
    for item in unresolved:
        if item.get("blocking") is not False:
            # Canonical booleans avoid truthy/falsey values crossing the
            # proposal boundary even for platform-created issues.
            item["blocking"] = True
            continue
        code = str(item.get("code") or "")
        if code == "document_reported_issue":
            reported_code = _normalize_reported_issue_code(
                item.get("reported_code")
            )
            item["reported_code"] = reported_code
            allowed = _raw_issue_can_be_nonblocking(
                reported_code,
                mapping_is_cleanly_deferred=mapping_is_cleanly_deferred,
            )
        else:
            allowed = code in _PLATFORM_NONBLOCKING_ISSUE_CODES
        if not allowed:
            unsafe.append(code or "<missing>")
    if unsafe:
        raise AssertionError(
            "编译器生成了未纳入严重性闭集的非阻塞项："
            + "、".join(sorted(set(unsafe)))
        )


def _resource_index(items: Iterable[Any]) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    by_id: dict[str, Any] = {}
    by_name: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        by_id[str(item.id)] = item
        name = str(getattr(item, "name", "") or "").strip()
        if name:
            by_name[name].append(item)
    return by_id, by_name


def _reference_token_variants(value: Any) -> set[str]:
    """Return exact syntactic variants without translating business words."""
    token = _text(value, maximum=300)
    variants = {token}
    all_prefixes = sorted(
        {
            alias
            for aliases in _RESOURCE_PREFIX_ALIASES.values()
            for alias in aliases
        },
        key=len,
        reverse=True,
    )
    current = token
    while current:
        match = re.match(r"^([A-Za-z]+)[._:-]+(.+)$", current)
        if match and match.group(1).lower() in all_prefixes:
            prefix = match.group(1).lower()
            current = match.group(2).strip()
            variants.update({
                current,
                f"{_RESOURCE_PREFIX_CANONICAL[prefix]}.{current}",
            })
            continue
        lowered = current.lower()
        compact_match = next(
            (
                alias
                for alias in all_prefixes
                if lowered.startswith(alias)
                and len(current) > len(alias)
                and (
                    not current[len(alias)].isascii()
                    or current[len(alias)].isupper()
                )
            ),
            None,
        )
        if compact_match is None:
            break
        current = current[len(compact_match):].strip()
        variants.update({
            current,
            f"{_RESOURCE_PREFIX_CANONICAL[compact_match]}.{current}",
        })
    return {item for item in variants if item}


def _resolve_ref(
    ref: Any,
    *,
    generated: list[dict[str, Any]],
    existing: Iterable[Any],
    resource_label: str,
    unresolved: list[dict[str, Any]],
    source_refs: Iterable[str],
) -> dict[str, str] | None:
    token = _text(ref, maximum=300)
    token_variants = _reference_token_variants(token)
    generated_matches = [
        item
        for item in generated
        if token_variants & {
            str(item.get("key") or "").strip(),
            str(item.get("name") or "").strip(),
        }
    ]
    by_id, by_name = _resource_index(existing)
    existing_matches = [
        item
        for variant in token_variants
        for item in (
            ([by_id[variant]] if variant in by_id else [])
            + by_name.get(variant, [])
        )
    ]
    # Generated entity entries that extend an existing object resolve directly
    # to that existing id and must not count as an ambiguous second match.
    if len(generated_matches) == 1 and generated_matches[0].get("existing_id"):
        return {"kind": "existing", "id": str(generated_matches[0]["existing_id"])}
    candidates = [*(('generated', item) for item in generated_matches), *(('existing', item) for item in existing_matches)]
    dedup: dict[tuple[str, str], tuple[str, Any]] = {}
    for kind, item in candidates:
        identity = str(item.get("key")) if isinstance(item, dict) else str(item.id)
        dedup[(kind, identity)] = (kind, item)
    candidates = list(dedup.values())
    if len(candidates) != 1:
        code = "missing_reference" if not candidates else "ambiguous_reference"
        _issue(
            unresolved,
            code,
            f"{resource_label}引用“{token or '空值'}”{'不存在' if not candidates else '不唯一'}",
            source_refs=source_refs,
        )
        return None
    kind, item = candidates[0]
    if kind == "generated":
        return {"kind": "generated", "key": str(item["key"])}
    return {"kind": "existing", "id": str(item.id)}


def _canonical_rule_operator(value: Any) -> str:
    token = _text(value, maximum=30).lower()
    if not token:
        return ""
    normalized = re.sub(r"[\s-]+", "_", token)
    return _RULE_OPERATOR_ALIASES.get(normalized, normalized)


def _normalize_rule_condition(value: Any, *, depth: int = 0) -> dict[str, Any]:
    if depth > 8 or not isinstance(value, dict) or not value:
        raise ValueError("规则条件必须是深度不超过 8 的非空对象")
    supplied_ops = [
        _canonical_rule_operator(value.get(field))
        for field in ("op", "operator")
        if _text(value.get(field), maximum=30)
    ]
    if len(set(supplied_ops)) > 1:
        raise ValueError("规则条件同时提供了互相冲突的 op 与 operator")
    op = supplied_ops[0] if supplied_ops else ""
    if op in {"and", "or", "not"}:
        conditions = value.get("conditions")
        if not isinstance(conditions, list) or not conditions or len(conditions) > 50:
            raise ValueError("逻辑规则必须包含 1 到 50 个子条件")
        if op == "not" and len(conditions) != 1:
            raise ValueError("not 规则必须且只能包含一个子条件")
        return {
            "op": op,
            "conditions": [
                _normalize_rule_condition(item, depth=depth + 1) for item in conditions
            ],
        }
    if op not in _RULE_LEAF_OPS:
        raise ValueError(f"规则包含不支持的运算符：{op or '空值'}")
    field = _text(value.get("field"), maximum=200)
    if not field:
        raise ValueError("规则叶子条件缺少字段名")
    result = {"field": field, "op": op}
    if op in {"is_null", "is_not_null"}:
        if "value_field" in value:
            raise ValueError("判空条件不能包含 value_field")
        return result
    has_value = "value" in value
    has_value_field = "value_field" in value
    if has_value == has_value_field:
        raise ValueError("比较条件必须且只能包含 value 或 value_field 之一")
    if has_value_field:
        value_field = _text(value.get("value_field"), maximum=200)
        if not value_field:
            raise ValueError("规则字段比较缺少 value_field 字段名")
        result["value_field"] = value_field
    else:
        result["value"] = value.get("value")
    return result


def _normalize_rule_severity(value: Any) -> str:
    token = _text(value, maximum=30).lower()
    if not token:
        return "info"
    normalized = re.sub(r"[\s-]+", "_", token)
    severity = _RULE_SEVERITY_ALIASES.get(normalized)
    if not severity:
        raise ValueError(f"不支持的严重级别：{token}")
    return severity


def _condition_fields(value: dict[str, Any]) -> set[str]:
    if not isinstance(value, dict):
        return set()
    if value.get("op") in {"and", "or", "not"}:
        return {
            field
            for item in (value.get("conditions") or [])
            for field in _condition_fields(item)
        }
    fields = {
        _text(value.get("field"), maximum=200),
        _text(value.get("value_field"), maximum=200),
    }
    return {field for field in fields if field}


def _ambiguous_rule_literal_fields(
    value: dict[str, Any],
    available_fields: set[str],
) -> set[str]:
    """Find string literals that are indistinguishable from field references."""
    if not isinstance(value, dict):
        return set()
    if value.get("op") in {"and", "or", "not"}:
        return {
            field
            for item in (value.get("conditions") or [])
            for field in _ambiguous_rule_literal_fields(item, available_fields)
        }
    literal = value.get("value")
    field = _text(value.get("field"), maximum=200)
    if (
        isinstance(literal, str)
        and literal in available_fields
        and literal != field
    ):
        return {literal}
    return set()


def _rewrite_self_qualified_rule_fields(
    value: dict[str, Any],
    *,
    qualifiers: set[str],
    available_fields: set[str],
) -> dict[str, Any]:
    """Remove an explicit current-entity qualifier from flat record fields.

    Rule evaluation receives one flat object record.  A model may nonetheless
    spell a field as ``entity_order.amount``.  The rewrite is safe only when
    the prefix uniquely denotes the rule's own entity and the suffix already
    exists verbatim on that entity; cross-object paths and translations remain
    visible blockers.
    """
    result = copy.deepcopy(value)
    if result.get("op") in {"and", "or", "not"}:
        result["conditions"] = [
            _rewrite_self_qualified_rule_fields(
                item,
                qualifiers=qualifiers,
                available_fields=available_fields,
            )
            for item in (result.get("conditions") or [])
        ]
        return result
    for field_key in ("field", "value_field"):
        field = _text(result.get(field_key), maximum=200)
        matches = {
            field[len(qualifier) + 1:]
            for qualifier in qualifiers
            if qualifier and field.startswith(f"{qualifier}.")
            and field[len(qualifier) + 1:] in available_fields
        }
        if len(matches) == 1:
            result[field_key] = next(iter(matches))
    return result


def _property_definition(prop: Any) -> dict[str, Any]:
    return {
        "name": prop.name,
        "api_name": getattr(prop, "api_name", "") or ontology_service.normalize_api_name(
            display_name=prop.name,
            prefix="property",
            stable_key=getattr(prop, "id", "") or prop.name,
        ),
        "data_type": prop.data_type,
        "description": prop.description or "",
        "is_key": bool(prop.is_key),
        "is_title": bool(prop.is_title),
        "is_required": bool(prop.is_required),
        "is_enum": bool(prop.is_enum),
        "enum_values": prop.enum_values or [],
        "default_value": prop.default_value or "",
        "constraints": prop.constraints or {},
        "is_sensitive": bool(prop.is_sensitive),
    }


def _property_storage_signature(
    data_type: Any,
    constraints: Any,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Represent only losslessly equivalent ontology property storage types."""
    kind = str(data_type or "string").strip().lower()
    normalized_constraints = copy.deepcopy(constraints) if isinstance(constraints, dict) else {}
    if kind in {"string", "text"}:
        kind = "string"
    elif kind in {"float", "number"}:
        kind = "number"
    elif kind == "date":
        if normalized_constraints.get("format") not in (None, "date"):
            return kind, tuple(sorted(
                (str(key), json.dumps(value, ensure_ascii=False, sort_keys=True))
                for key, value in normalized_constraints.items()
            ))
        kind = "string"
        normalized_constraints["format"] = "date"
    elif kind == "datetime":
        if normalized_constraints.get("format") not in (None, "date-time"):
            return kind, tuple(sorted(
                (str(key), json.dumps(value, ensure_ascii=False, sort_keys=True))
                for key, value in normalized_constraints.items()
            ))
        kind = "string"
        normalized_constraints["format"] = "date-time"
    return kind, tuple(sorted(
        (str(key), json.dumps(value, ensure_ascii=False, sort_keys=True))
        for key, value in normalized_constraints.items()
    ))


def _retain_compatible_existing_property_storage(
    proposed: dict[str, Any],
    existing: dict[str, Any],
) -> None:
    """Keep persisted spelling when proposed type/constraints are equivalent."""
    if _property_storage_signature(
        proposed.get("data_type"), proposed.get("constraints")
    ) != _property_storage_signature(
        existing.get("data_type"), existing.get("constraints")
    ):
        return
    proposed["data_type"] = existing["data_type"]
    proposed["constraints"] = copy.deepcopy(existing.get("constraints") or {})


def _is_safe_empty_entity_enum_upgrade(
    db: Session,
    current: OntologyProperty,
    proposed: dict[str, Any],
) -> bool:
    """Allow one monotonic schema enrichment before an entity has bound data.

    Turning an unconstrained string into an enum can invalidate existing or
    externally mapped values.  It is therefore deterministic only while the
    entity has neither ontology instances nor a data mapping, and when enum
    metadata is the *only* structural difference.  Every other schema change
    remains a blocking conflict that needs an explicit migration.
    """
    if bool(current.is_enum) or not bool(proposed.get("is_enum")):
        return False
    enum_values = proposed.get("enum_values") or []
    if not enum_values or any(not isinstance(value, str) for value in enum_values):
        return False
    if len(enum_values) != len(set(enum_values)):
        return False
    current_definition = _property_definition(current)
    unchanged_fields = (
        "data_type",
        "is_key",
        "is_title",
        "is_required",
        "constraints",
        "is_sensitive",
    )
    if any(
        current_definition[field] != proposed.get(field)
        for field in unchanged_fields
    ):
        return False
    default_value = proposed.get("default_value")
    if default_value not in (None, "") and str(default_value) not in set(enum_values):
        return False
    entity_id = str(current.entity_id or "")
    if not entity_id:
        return False
    if db.scalar(
        select(OntologyInstance.id)
        .where(OntologyInstance.entity_id == entity_id)
        .limit(1)
    ) is not None:
        return False
    if db.scalar(
        select(DataMapping.id)
        .where(DataMapping.entity_id == entity_id)
        .limit(1)
    ) is not None:
        return False
    return True


def _is_safe_title_fallback(
    entity: OntologyEntity,
    current: OntologyProperty,
    proposed: dict[str, Any],
) -> bool:
    """Allow the compiler's explicit PK-as-title repair for legacy objects."""
    if bool(entity.is_abstract) or not bool(current.is_key):
        return False
    if bool(current.is_title) or not bool(proposed.get("is_title")):
        return False
    if any(bool(prop.is_title) for prop in entity.properties):
        return False
    current_definition = _property_definition(current)
    return all(
        current_definition[field] == proposed.get(field)
        for field in current_definition
        if field not in {"is_title", "description", "default_value"}
    )


def _entity_property_names_for_ref(
    scenario: BusinessScenario,
    generated: list[dict[str, Any]],
    ref: dict[str, str] | None,
) -> set[str]:
    if not ref:
        return set()
    if ref.get("kind") == "generated":
        item = next(
            (entry for entry in generated if entry.get("key") == ref.get("key")),
            None,
        )
        return {prop["name"] for prop in (item or {}).get("properties") or []}
    entity_id = str(ref.get("id") or "")
    existing = next((entry for entry in scenario.entities if entry.id == entity_id), None)
    names = {prop.name for prop in (existing.properties if existing else [])}
    extension = next(
        (entry for entry in generated if entry.get("existing_id") == entity_id),
        None,
    )
    names.update(prop["name"] for prop in (extension or {}).get("properties") or [])
    return names


def _entity_field_qualifiers_for_ref(
    scenario: BusinessScenario,
    generated: list[dict[str, Any]],
    ref: dict[str, str] | None,
) -> set[str]:
    if not ref:
        return set()
    if ref.get("kind") == "generated":
        item = next(
            (entry for entry in generated if entry.get("key") == ref.get("key")),
            None,
        )
        if not item:
            return set()
        key = str(item.get("key") or "")
        return {
            key,
            str(item.get("name") or ""),
            *_generated_key_aliases("entities", key),
        } - {""}
    entity_id = str(ref.get("id") or "")
    existing = next((entry for entry in scenario.entities if entry.id == entity_id), None)
    if not existing:
        return set()
    name = str(existing.name or "")
    return {
        entity_id,
        name,
        f"entity.{name}",
        f"entity_{name}",
        f"entity:{name}",
        f"entity-{name}",
    } - {""}


def _entity_invalid_property_names_for_ref(
    generated: list[dict[str, Any]],
    ref: dict[str, str] | None,
) -> set[str]:
    if not ref:
        return set()
    item = None
    if ref.get("kind") == "generated":
        item = next(
            (
                entry for entry in generated
                if entry.get("key") == ref.get("key")
            ),
            None,
        )
    else:
        item = next(
            (
                entry for entry in generated
                if entry.get("existing_id") == ref.get("id")
            ),
            None,
        )
    return {
        str(value)
        for value in (item or {}).get("_invalid_property_names") or []
        if str(value)
    }


def _entity_key_property_for_ref(
    scenario: BusinessScenario,
    generated: list[dict[str, Any]],
    ref: dict[str, str] | None,
) -> str:
    if not ref:
        return ""
    if ref.get("kind") == "generated":
        item = next(
            (entry for entry in generated if entry.get("key") == ref.get("key")),
            None,
        )
        return next(
            (prop["name"] for prop in (item or {}).get("properties") or [] if prop.get("is_key")),
            "",
        )
    entity_id = str(ref.get("id") or "")
    existing = next((entry for entry in scenario.entities if entry.id == entity_id), None)
    current = next((prop.name for prop in (existing.properties if existing else []) if prop.is_key), "")
    if current:
        return current
    extension = next(
        (entry for entry in generated if entry.get("existing_id") == entity_id),
        None,
    )
    return next(
        (prop["name"] for prop in (extension or {}).get("properties") or [] if prop.get("is_key")),
        "",
    )


_WORKFLOW_NODE_TYPE_ALIASES = {
    "start": "start",
    "startnode": "start",
    "begin": "start",
    "beginnode": "start",
    "initial": "start",
    "initialnode": "start",
    "开始": "start",
    "开始节点": "start",
    "起始": "start",
    "起始节点": "start",
    "end": "end",
    "endnode": "end",
    "finish": "end",
    "finishnode": "end",
    "terminal": "end",
    "terminalnode": "end",
    "结束": "end",
    "结束节点": "end",
    "终止": "end",
    "终止节点": "end",
    "action": "action",
    "actionnode": "action",
    "operation": "action",
    "operationnode": "action",
    "操作": "action",
    "操作节点": "action",
    "动作": "action",
    "动作节点": "action",
    "rule": "rule",
    "rulenode": "rule",
    "decision": "rule",
    "decisionnode": "rule",
    "规则": "rule",
    "规则节点": "rule",
    "决策": "rule",
    "决策节点": "rule",
    "event": "event",
    "eventnode": "event",
    "事件": "event",
    "事件节点": "event",
    "approval": "approval",
    "approvalnode": "approval",
    "humanapproval": "approval",
    "humanapprovalnode": "approval",
    "审批": "approval",
    "审批节点": "approval",
    "人工审批": "approval",
    "人工审批节点": "approval",
}
_WORKFLOW_TRUE_LABELS = {"true", "yes", "1", "是", "真", "通过", "命中", "满足"}
_WORKFLOW_FALSE_LABELS = {"false", "no", "0", "否", "假", "不通过", "未通过", "未命中", "不满足"}


def _workflow_alias_key(value: Any) -> str:
    """Return the conservative key used only for documented workflow aliases."""
    return re.sub(r"[\s_-]+", "", _text(value, maximum=100).casefold())


def _normalize_workflow_node_type(value: Any) -> str:
    raw = _text(value, maximum=30)
    return _WORKFLOW_NODE_TYPE_ALIASES.get(_workflow_alias_key(raw), raw.casefold())


def _workflow_container_values(raw_node: dict[str, Any]) -> list[dict[str, Any]]:
    values = [raw_node]
    for key in ("data", "config"):
        value = raw_node.get(key)
        if isinstance(value, dict):
            values.append(value)
    return values


def _workflow_reference_token(raw_node: dict[str, Any], node_type: str) -> str:
    """Read an explicit reference alias without guessing from descriptive text."""
    field_names = (
        "resource_ref", "resourceRef", "resource_key", "resourceKey",
        f"{node_type}_ref", f"{node_type}Ref",
        f"{node_type}_id", f"{node_type}Id",
    )
    for container in _workflow_container_values(raw_node):
        for field in field_names:
            token = container.get(field)
            if token not in (None, ""):
                return _text(token, maximum=300)
        resource = container.get("resource")
        if isinstance(resource, str) and resource.strip():
            return _text(resource, maximum=300)
        if isinstance(resource, dict):
            for field in ("key", "id", "name", "ref"):
                if resource.get(field) not in (None, ""):
                    return _text(resource[field], maximum=300)
    return ""


def _workflow_reference_match_count(
    token: str,
    *,
    generated: list[dict[str, Any]],
    existing: Iterable[Any],
) -> int:
    if not token:
        return 0
    token_variants = _reference_token_variants(token)
    identities: set[tuple[str, str]] = set()
    for item in generated:
        if token_variants & {
            _text(item.get("key"), maximum=300),
            _text(item.get("name"), maximum=300),
        }:
            identities.add(("generated", _text(item.get("key"), maximum=300)))
    for item in existing:
        if token_variants & {
            _text(item.id, maximum=300),
            _text(item.name, maximum=300),
        }:
            identities.add(("existing", _text(item.id, maximum=300)))
    return len(identities)


def _infer_workflow_resource_token(
    raw_node: dict[str, Any],
    *,
    generated: list[dict[str, Any]],
    existing: Iterable[Any],
) -> str:
    """Infer a missing reference only from an exact, unique resource name."""
    data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else {}
    candidates = (
        raw_node.get("name"),
        data.get("label"),
        data.get("name"),
    )
    for value in candidates:
        token = _text(value, maximum=300)
        if _workflow_reference_match_count(
            token, generated=generated, existing=existing
        ) == 1:
            return token
    return ""


def _infer_workflow_node_type(
    raw_node: dict[str, Any],
    resources: dict[str, tuple[list[dict[str, Any]], Iterable[Any], str]],
) -> str:
    node_type = _normalize_workflow_node_type(raw_node.get("type"))
    if node_type:
        # A model can label a node as ``action`` while its explicit reference
        # uniquely names a Rule or Event (a common cross-chunk error).  Repair
        # only that closed, exact mismatch.  Ambiguous or unknown references
        # retain the declared type and become ordinary blocking issues later.
        if node_type in resources:
            token = _workflow_reference_token(raw_node, node_type)
            if token:
                matching_types = {
                    kind
                    for kind, (generated, existing, _id_key) in resources.items()
                    if _workflow_reference_match_count(
                        token, generated=generated, existing=existing
                    ) == 1
                }
                if node_type not in matching_types and len(matching_types) == 1:
                    return next(iter(matching_types))
        return node_type

    data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else {}
    structural_tokens = {
        _workflow_alias_key(raw_node.get("id")),
        _workflow_alias_key(raw_node.get("name")),
        _workflow_alias_key(data.get("label")),
    }
    structural_types = {
        alias
        for token in structural_tokens
        if token
        for alias in [_WORKFLOW_NODE_TYPE_ALIASES.get(token)]
        if alias in {"start", "end"}
    }

    resource_types: set[str] = set()
    for kind, (generated, existing, _id_key) in resources.items():
        explicit = _workflow_reference_token(raw_node, kind)
        if explicit and _workflow_reference_match_count(
            explicit, generated=generated, existing=existing
        ) == 1:
            resource_types.add(kind)
            continue
        inferred = _infer_workflow_resource_token(
            raw_node, generated=generated, existing=existing
        )
        if inferred:
            resource_types.add(kind)

    candidates = structural_types | resource_types
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _workflow_edge_value(edge: dict[str, Any], *, source: bool) -> Any:
    fields = (
        ("source", "source_id", "sourceId", "from", "from_id", "fromNodeId")
        if source else
        ("target", "target_id", "targetId", "to", "to_id", "toNodeId")
    )
    for field in fields:
        if edge.get(field) not in (None, ""):
            value = edge[field]
            if isinstance(value, dict):
                for nested in ("id", "node_id", "nodeId", "key", "name"):
                    if value.get(nested) not in (None, ""):
                        return value[nested]
                return ""
            return value
    return ""


def _workflow_node_aliases(
    raw_nodes: list[dict[str, Any]],
    node_ids: list[str],
) -> dict[str, str]:
    candidates: dict[str, set[str]] = defaultdict(set)
    for raw_node, node_id in zip(raw_nodes, node_ids, strict=True):
        data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else {}
        for value in (
            raw_node.get("id"), raw_node.get("node_id"), raw_node.get("nodeId"),
            raw_node.get("key"), raw_node.get("name"), data.get("label"), data.get("name"),
        ):
            token = _text(value, maximum=300)
            if token:
                candidates[token].add(node_id)
    return {
        token: next(iter(matches))
        for token, matches in candidates.items()
        if len(matches) == 1
    }


def _normalize_workflow_edge_label(value: Any) -> str:
    token = _text(value, maximum=30).casefold()
    if token in _WORKFLOW_TRUE_LABELS:
        return "true"
    if token in _WORKFLOW_FALSE_LABELS:
        return "false"
    return token


def _complete_unambiguous_rule_branch_labels(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Fill one missing complementary branch; two blank branches remain blocking."""
    rule_ids = {node["id"] for node in nodes if node.get("type") == "rule"}
    for node_id in rule_ids:
        outgoing = [edge for edge in edges if edge.get("source") == node_id]
        if len(outgoing) != 2:
            continue
        labels = [edge.get("label") or "" for edge in outgoing]
        if labels.count("") != 1:
            continue
        known = next((label for label in labels if label), "")
        if known not in {"true", "false"}:
            continue
        outgoing[labels.index("")]["label"] = "false" if known == "true" else "true"


def _workflow_rule_branch_gaps(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[tuple[str, list[str]]]:
    """List every unresolved rule branch so one workflow reports all gaps."""
    labels_by_source: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        labels_by_source[str(edge.get("source") or "")].add(
            str(edge.get("label") or "")
        )
    gaps: list[tuple[str, list[str]]] = []
    for node in nodes:
        if node.get("type") != "rule":
            continue
        node_id = str(node.get("id") or "")
        missing = sorted({"true", "false"} - labels_by_source.get(node_id, set()))
        if missing:
            gaps.append((node_id, missing))
    return gaps


def _next_workflow_id(prefix: str, used: set[str]) -> str:
    if prefix not in used:
        used.add(prefix)
        return prefix
    index = 2
    while f"{prefix}_{index}" in used:
        index += 1
    value = f"{prefix}_{index}"
    used.add(value)
    return value


def _add_unambiguous_workflow_boundaries(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Add control boundaries only when the existing graph has one clear root/sink."""
    node_ids = {str(node.get("id") or "") for node in nodes if node.get("id")}
    if not node_ids or any(
        edge.get("source") not in node_ids or edge.get("target") not in node_ids
        for edge in edges
    ):
        return
    used_edge_ids = {str(edge.get("id") or "") for edge in edges if edge.get("id")}

    if not any(node.get("type") == "start" for node in nodes):
        incoming = {str(edge["target"]) for edge in edges}
        roots = sorted(node_ids - incoming)
        if len(roots) == 1:
            start_id = _next_workflow_id("start", node_ids)
            nodes.insert(0, {
                "id": start_id,
                "type": "start",
                "name": "开始",
                "position": {"x": 0, "y": 100},
                "data": {"label": "开始"},
            })
            edge_id = _next_workflow_id("edge_start", used_edge_ids)
            edges.insert(0, {
                "id": edge_id, "source": start_id, "target": roots[0], "label": "",
            })

    if not any(node.get("type") == "end" for node in nodes):
        outgoing = {str(edge["source"]) for edge in edges}
        sinks = sorted(node_ids - outgoing)
        if len(sinks) == 1:
            end_id = _next_workflow_id("end", node_ids)
            nodes.append({
                "id": end_id,
                "type": "end",
                "name": "结束",
                "position": {"x": len(nodes) * 180, "y": 100},
                "data": {"label": "结束"},
            })
            edge_id = _next_workflow_id("edge_end", used_edge_ids)
            edges.append({
                "id": edge_id, "source": sinks[0], "target": end_id, "label": "",
            })


def _mapping_identity(
    *,
    data_source_id: str,
    table_name: str,
    column_map: dict[str, Any],
    key_property: str,
) -> tuple[str, str, str]:
    # Keep this contract aligned with the Assistant mapping apply path: a
    # source/table/key-column change receives a new mapping id, while display
    # column edits retain the import identity.
    return (
        str(data_source_id or ""),
        str(table_name or ""),
        str((column_map or {}).get(key_property) or ""),
    )


def _mapping_plan(
    scenario: BusinessScenario,
    generated_entities: list[dict[str, Any]],
    item: dict[str, Any],
) -> dict[str, Any]:
    entity_ref = item.get("entity") or {}
    if entity_ref.get("kind") != "existing":
        return {"mode": "add", "canonical_id": "", "delete_ids": []}
    entity_id = str(entity_ref.get("id") or "")
    existing = sorted(
        [mapping for mapping in scenario.data_mappings if mapping.entity_id == entity_id],
        key=lambda mapping: (str(mapping.created_at or ""), mapping.id),
    )
    if not existing:
        return {"mode": "add", "canonical_id": "", "delete_ids": []}
    key_property = _entity_key_property_for_ref(scenario, generated_entities, entity_ref)
    current = existing[0]
    current_identity = _mapping_identity(
        data_source_id=current.data_source_id,
        table_name=current.table_name,
        column_map=current.column_map or {},
        key_property=key_property,
    )
    incoming_identity = _mapping_identity(
        data_source_id=str((item.get("data_source") or {}).get("id") or ""),
        table_name=item.get("table_name") or "",
        column_map=item.get("column_map") or {},
        key_property=key_property,
    )
    if current_identity == incoming_identity:
        return {
            "mode": "update" if (current.column_map or {}) != (item.get("column_map") or {}) else "skip",
            "canonical_id": current.id,
            "delete_ids": [mapping.id for mapping in existing[1:]],
        }
    return {
        "mode": "replace",
        "canonical_id": "",
        "delete_ids": [mapping.id for mapping in existing],
    }


def _generated_for_ref(
    items: Iterable[dict[str, Any]],
    ref: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not ref or ref.get("kind") != "generated":
        return None
    key = str(ref.get("key") or "")
    return next((item for item in items if str(item.get("key") or "") == key), None)


def _relation_endpoints_for_ref(
    scenario: BusinessScenario,
    generated_relations: list[dict[str, Any]],
    ref: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    generated = _generated_for_ref(generated_relations, ref)
    if generated is not None:
        return generated.get("source"), generated.get("target")
    relation_id = str((ref or {}).get("id") or "")
    relation = next(
        (item for item in scenario.relations if str(item.id) == relation_id),
        None,
    )
    if relation is None:
        return None, None
    return (
        {"kind": "existing", "id": str(relation.source_entity_id)},
        {"kind": "existing", "id": str(relation.target_entity_id)},
    )


def _mapping_entity_for_ref(
    scenario: BusinessScenario,
    generated_mappings: list[dict[str, Any]],
    ref: dict[str, Any] | None,
) -> dict[str, str] | None:
    generated = _generated_for_ref(generated_mappings, ref)
    if generated is not None:
        return generated.get("entity")
    mapping_id = str((ref or {}).get("id") or "")
    mapping = next(
        (item for item in scenario.data_mappings if str(item.id) == mapping_id),
        None,
    )
    if mapping is None:
        return None
    return {"kind": "existing", "id": str(mapping.entity_id)}


def _mapping_physical_definition(
    scenario: BusinessScenario,
    generated_mappings: list[dict[str, Any]],
    ref: dict[str, Any] | None,
) -> dict[str, Any] | None:
    generated = _generated_for_ref(generated_mappings, ref)
    if generated is not None:
        return {
            "data_source_id": str((generated.get("data_source") or {}).get("id") or ""),
            "table_name": str(generated.get("table_name") or ""),
            "column_map": dict(generated.get("column_map") or {}),
            "entity": generated.get("entity"),
        }
    mapping_id = str((ref or {}).get("id") or "")
    mapping = next(
        (item for item in scenario.data_mappings if str(item.id) == mapping_id),
        None,
    )
    if mapping is None:
        return None
    return {
        "data_source_id": str(mapping.data_source_id),
        "table_name": str(mapping.table_name or ""),
        "column_map": dict(mapping.column_map or {}),
        "entity": {"kind": "existing", "id": str(mapping.entity_id)},
    }


def _planned_mapping_id(
    generated_mappings: list[dict[str, Any]],
    ref: dict[str, Any] | None,
) -> str:
    if not ref:
        return ""
    if ref.get("kind") == "existing":
        return str(ref.get("id") or "")
    generated = _generated_for_ref(generated_mappings, ref)
    plan = (generated or {}).get("apply_plan") or {}
    if plan.get("mode") in {"update", "skip"}:
        return str(plan.get("canonical_id") or "")
    return ""


_RELATION_MAPPING_FINGERPRINT_FIELDS = (
    "relation_id",
    "source_mapping_id",
    "target_mapping_id",
    "mode",
    "data_source_id",
    "table_name",
    "foreign_key_column",
    "source_key_column",
    "target_key_column",
)


def _relation_mapping_fingerprint(value: Any) -> str:
    body = {
        field: str(getattr(value, field, "") or "")
        for field in _RELATION_MAPPING_FINGERPRINT_FIELDS
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _relation_mapping_plan(
    scenario: BusinessScenario,
    generated_mappings: list[dict[str, Any]],
    item: dict[str, Any],
) -> dict[str, Any]:
    relation_ref = item.get("relation") or {}
    if relation_ref.get("kind") != "existing":
        return {"mode": "add", "existing_id": "", "fingerprint": ""}
    relation_id = str(relation_ref.get("id") or "")
    matches = [
        mapping
        for mapping in getattr(scenario, "relation_data_mappings", [])
        if str(mapping.relation_id) == relation_id
    ]
    if not matches:
        return {"mode": "add", "existing_id": "", "fingerprint": ""}
    current = matches[0]
    source_mapping_id = _planned_mapping_id(
        generated_mappings, item.get("source_mapping")
    )
    target_mapping_id = _planned_mapping_id(
        generated_mappings, item.get("target_mapping")
    )
    join_source_id = str((item.get("join_data_source") or {}).get("id") or "")
    comparable = bool(
        source_mapping_id
        and target_mapping_id
        and str(current.data_source_binding_key or "")
    )
    if comparable:
        proposed = {
            "relation_id": relation_id,
            "source_mapping_id": source_mapping_id,
            "target_mapping_id": target_mapping_id,
            "mode": item.get("mode") or "",
            "data_source_id": (
                join_source_id
                if item.get("mode") == "join_table"
                else str((item.get("carrier") or {}).get("data_source_id") or "")
            ),
            "table_name": (
                item.get("join_table_name")
                if item.get("mode") == "join_table"
                else str((item.get("carrier") or {}).get("table_name") or "")
            ),
            "foreign_key_column": item.get("foreign_key_column") or "",
            "source_key_column": item.get("source_key_column") or "",
            "target_key_column": item.get("target_key_column") or "",
        }
        current_body = {
            field: str(getattr(current, field, "") or "")
            for field in _RELATION_MAPPING_FINGERPRINT_FIELDS
        }
        comparable = all(
            current_body[field] == str(proposed[field] or "")
            for field in _RELATION_MAPPING_FINGERPRINT_FIELDS
        )
    return {
        "mode": "skip" if comparable else "update",
        "existing_id": str(current.id),
        "fingerprint": _relation_mapping_fingerprint(current),
    }


def _function_definition(item: dict[str, Any]) -> dict[str, Any]:
    """Strip compiler provenance before the closed function validator."""
    return function_definition_service.normalize_definition({
        field: item.get(field)
        for field in (
            "name", "description", "input_schema", "output_schema", "tags",
            "visibility", "runtime_kind", "runtime_config",
        )
    })


def normalize_scenario_model(
    db: Session,
    scenario: BusinessScenario,
    raw: dict[str, Any],
    *,
    source_bundle: dict[str, Any],
    mapping_catalog: list[dict[str, Any]] | None = None,
    columns_by_table: dict[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    """Turn untrusted model JSON into the only persistable compound shape."""
    if not isinstance(raw, dict):
        raise ValueError("复合业务模型必须是 JSON 对象")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("复合业务模型缺少受支持的 schema_version")
    # New LLM responses pass through _validate_raw_contract and must include
    # the closed relation_mappings section. Keep direct normalization of
    # already-stored v1 proposals backward-compatible by treating the one
    # newly introduced section as an explicit empty list.
    if "relation_mappings" not in raw:
        raw = {**raw, "relation_mappings": []}
    for section in (*_RESOURCE_SECTIONS, "unresolved", "coverage"):
        if not isinstance(raw.get(section), list):
            raise ValueError(f"复合业务模型字段 {section} 必须是数组")
        if any(not isinstance(item, dict) for item in raw[section]):
            raise ValueError(f"复合业务模型字段 {section} 包含非对象条目")
    raw = _reconcile_generated_references(raw)
    valid_sources = {item["ref"] for item in source_bundle["paragraphs"]}
    unresolved: list[dict[str, Any]] = []
    mapping_is_cleanly_deferred = (
        not (mapping_catalog or [])
        and not (raw.get("mappings") or [])
        and not (raw.get("relation_mappings") or [])
    )
    for item in raw.get("unresolved") or []:
        if not isinstance(item, dict):
            continue
        model_code = _normalize_reported_issue_code(item.get("code"))
        requested_nonblocking = item.get("blocking") is False
        _issue(
            unresolved,
            "document_reported_issue",
            f"[{model_code}] "
            + _text(item.get("message") or "文档存在待确认项", maximum=2_000),
            source_refs=[ref for ref in (item.get("source_refs") or []) if ref in valid_sources],
            blocking=not (
                requested_nonblocking
                and _raw_issue_can_be_nonblocking(
                    model_code,
                    mapping_is_cleanly_deferred=mapping_is_cleanly_deferred,
                )
            ),
            reported_code=model_code,
        )

    existing_entities_by_name: dict[str, list[OntologyEntity]] = defaultdict(list)
    for entity in scenario.entities:
        existing_entities_by_name[entity.name].append(entity)
    used_entity_api_names = {
        ontology_service.normalize_api_name(
            getattr(entity, "api_name", ""),
            display_name=entity.name,
            prefix="entity",
            stable_key=entity.id,
        )
        for entity in scenario.entities
    }
    entities: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    for index, value in enumerate(raw.get("entities") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"entity:{index}", maximum=200)
        name = _text(value.get("name"), maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        if not name:
            _issue(unresolved, "missing_name", f"{key} 缺少对象类型名称", source_refs=meta["evidence_refs"])
            continue
        if key in seen_keys or name in seen_names:
            _issue(unresolved, "duplicate_generated_resource", f"对象类型 key 或名称重复：{key}/{name}", source_refs=meta["evidence_refs"])
            continue
        seen_keys.add(key)
        seen_names.add(name)
        matches = existing_entities_by_name.get(name, [])
        if len(matches) > 1:
            _issue(unresolved, "ambiguous_existing_resource", f"已有对象类型名称不唯一：{name}", source_refs=meta["evidence_refs"])
        existing = matches[0] if len(matches) == 1 else None
        existing_id = existing.id if existing else ""
        if existing is not None:
            entity_api_name = ontology_service.normalize_api_name(
                getattr(existing, "api_name", ""),
                display_name=existing.name,
                prefix="entity",
                stable_key=existing.id,
            )
            requested_api_name = str(value.get("api_name") or "").strip()
            if requested_api_name and ontology_service.normalize_api_name(
                requested_api_name,
                display_name=name,
                prefix="entity",
                stable_key=existing.id,
            ) != entity_api_name:
                _issue(
                    unresolved,
                    "immutable_api_name",
                    f"对象类型“{name}”的 api_name 创建后不能修改",
                    source_refs=meta["evidence_refs"],
                )
        else:
            try:
                entity_api_name = ontology_service.reserve_api_name(
                    used_entity_api_names,
                    value.get("api_name"),
                    display_name=name,
                    prefix="entity",
                    stable_key=key,
                )
            except ValueError as exc:
                _issue(
                    unresolved,
                    "duplicate_api_name",
                    f"对象类型“{name}”的 api_name 无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
                entity_api_name = ontology_service.normalize_api_name(
                    display_name=name, prefix="entity", stable_key=key
                )
        description = _text(
            value.get("description") if "description" in value else getattr(existing, "description", "")
        )
        is_abstract = bool(
            value.get("is_abstract") if "is_abstract" in value else getattr(existing, "is_abstract", False)
        )
        state_property = _text(
            value.get("state_property")
            if "state_property" in value
            else getattr(existing, "state_property", ""),
            maximum=200,
        )
        existing_props = {
            prop.name: prop for prop in (existing.properties if existing else [])
        }
        used_property_api_names = {
            ontology_service.normalize_api_name(
                getattr(prop, "api_name", ""),
                display_name=prop.name,
                prefix="property",
                stable_key=prop.id,
            )
            for prop in existing_props.values()
        }
        normalized_properties: list[dict[str, Any]] = []
        invalid_property_names: set[str] = set()
        for property_index, raw_property in enumerate(value.get("properties") or [], 1):
            if not isinstance(raw_property, dict):
                invalid_property_names.add(f"property:{property_index}")
                _issue(
                    unresolved,
                    "invalid_property",
                    f"对象类型“{name}”的第 {property_index} 个属性不是对象",
                    source_refs=meta["evidence_refs"],
                )
                continue
            normalized_property = copy.deepcopy(raw_property)
            property_name = _text(
                normalized_property.get("name") or f"property:{property_index}",
                maximum=200,
            )
            data_type = _normalize_entity_property_type(
                normalized_property.get("data_type")
            )
            normalized_property["data_type"] = data_type
            current_property = existing_props.get(property_name)
            if current_property is not None:
                property_api_name = ontology_service.normalize_api_name(
                    getattr(current_property, "api_name", ""),
                    display_name=current_property.name,
                    prefix="property",
                    stable_key=current_property.id,
                )
                requested_property_api_name = str(
                    normalized_property.get("api_name") or ""
                ).strip()
                if requested_property_api_name and ontology_service.normalize_api_name(
                    requested_property_api_name,
                    display_name=property_name,
                    prefix="property",
                    stable_key=current_property.id,
                ) != property_api_name:
                    _issue(
                        unresolved,
                        "immutable_api_name",
                        f"对象类型“{name}”的属性“{property_name}”不能修改 api_name",
                        source_refs=meta["evidence_refs"],
                    )
            else:
                try:
                    property_api_name = ontology_service.reserve_api_name(
                        used_property_api_names,
                        normalized_property.get("api_name"),
                        display_name=property_name,
                        prefix="property",
                        stable_key=f"{key}.{property_name}",
                    )
                except ValueError as exc:
                    _issue(
                        unresolved,
                        "duplicate_api_name",
                        f"对象类型“{name}”的属性“{property_name}”api_name 无效：{exc}",
                        source_refs=meta["evidence_refs"],
                    )
                    property_api_name = ontology_service.normalize_api_name(
                        display_name=property_name,
                        prefix="property",
                        stable_key=f"{key}.{property_name}",
                    )
            normalized_property["api_name"] = property_api_name
            # A non-empty enum value set is an unambiguous declaration even
            # when an LLM forgets the redundant boolean flag.
            if normalized_property.get("enum_values"):
                normalized_property["is_enum"] = True
            try:
                normalized_property["constraints"] = _normalize_compiler_constraints(
                    data_type,
                    normalized_property.get("constraints"),
                )
            except ValueError as exc:
                _issue(
                    unresolved,
                    "invalid_property_constraints",
                    f"对象类型“{name}”的属性“{property_name}”约束无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
                # Keep the rest of the entity inspectable while the explicit
                # blocker prevents an unsafe apply.
                normalized_property["constraints"] = {}
            try:
                normalized_properties.append(
                    PropertyIn.model_validate(normalized_property).model_dump()
                )
            except Exception as exc:  # noqa: BLE001
                invalid_property_names.add(property_name)
                _issue(
                    unresolved,
                    "invalid_property",
                    f"对象类型“{name}”的属性“{property_name}”无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
        entity_validation_error = ""
        try:
            entity_input = EntityIn.model_validate({
                "name": name,
                "api_name": entity_api_name,
                "description": description,
                "is_abstract": is_abstract,
                # Validate the combined property set below.  An extension may
                # legitimately retain an existing state property that the
                # document does not repeat in its property list.
                "state_property": "",
                "properties": normalized_properties,
            })
        except Exception as exc:  # noqa: BLE001
            _issue(unresolved, "invalid_entity", f"对象类型“{name}”无效：{exc}", source_refs=meta["evidence_refs"])
            definition = {
                "name": name,
                "api_name": entity_api_name,
                "description": description,
                "is_abstract": is_abstract,
                "state_property": state_property,
                "properties": [],
            }
        else:
            # Keep every successfully parsed property in the inspectable
            # proposal even when a cross-property ontology rule fails (for
            # example, two candidate primary keys).  The validation error is
            # still blocking, so this does not make the entity writable; it
            # prevents the empty fallback from creating misleading secondary
            # errors such as “missing primary key”.
            try:
                ontology_service.validate_entity_definition(
                    entity_input, scenario_namespace=scenario.namespace or "default"
                )
            except Exception as exc:  # noqa: BLE001
                entity_validation_error = str(exc)
                _issue(
                    unresolved,
                    "invalid_entity",
                    f"对象类型“{name}”无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
            definition = entity_input.model_dump()
            definition["state_property"] = state_property
        raw_props = {
            _text(prop.get("name"), maximum=200): prop
            for prop in (value.get("properties") or [])
            if isinstance(prop, dict) and _text(prop.get("name"), maximum=200)
        }
        if existing:
            for prop in definition["properties"]:
                current = existing_props.get(prop["name"])
                if current is None:
                    prop["_operation"] = "add"
                    continue
                current_definition = _property_definition(current)
                raw_prop = raw_props.get(prop["name"], {})
                # Omitted fields mean “retain the current definition”, not
                # “replace it with a Pydantic default”.
                for field, current_value in current_definition.items():
                    if field != "name" and field not in raw_prop:
                        prop[field] = current_value
                _retain_compatible_existing_property_storage(
                    prop, current_definition
                )
                comparable = {
                    "data_type": current.data_type,
                    "is_key": bool(current.is_key),
                    "is_title": bool(current.is_title),
                    "is_required": bool(current.is_required),
                    "is_enum": bool(current.is_enum),
                    "enum_values": current.enum_values or [],
                    "constraints": current.constraints or {},
                    "is_sensitive": bool(current.is_sensitive),
                }
                structural_conflict = any(
                    comparable[field] != prop.get(field)
                    for field in comparable
                )
                safe_enum_upgrade = bool(
                    structural_conflict
                    and _is_safe_empty_entity_enum_upgrade(db, current, prop)
                )
                safe_title_upgrade = bool(
                    structural_conflict
                    and existing is not None
                    and _is_safe_title_fallback(existing, current, prop)
                )
                if structural_conflict and not (safe_enum_upgrade or safe_title_upgrade):
                    _issue(
                        unresolved,
                        "existing_property_conflict",
                        f"对象类型“{name}”的属性“{prop['name']}”与已有定义冲突",
                        source_refs=meta["evidence_refs"],
                    )
                metadata_changed = any(
                    current_definition[field] != prop.get(field)
                    for field in ("description", "default_value")
                )
                if safe_enum_upgrade:
                    prop["_structural_update"] = "enum_upgrade"
                if safe_title_upgrade:
                    prop["_structural_update"] = "title_fallback"
                prop["_operation"] = (
                    "update"
                    if metadata_changed or safe_enum_upgrade or safe_title_upgrade
                    else "skip"
                )
        else:
            for prop in definition["properties"]:
                prop["_operation"] = "add"

        combined_properties = {
            prop.name: _property_definition(prop) for prop in (existing.properties if existing else [])
        }
        combined_properties.update({
            prop["name"]: {field: value for field, value in prop.items() if not field.startswith("_")}
            for prop in definition["properties"]
        })
        key_count = sum(bool(prop.get("is_key")) for prop in combined_properties.values())
        if key_count > 1:
            _issue(
                unresolved,
                "multiple_primary_keys",
                f"对象类型“{name}”合并已有定义后包含 {key_count} 个主键属性",
                source_refs=meta["evidence_refs"],
            )
        if (
            not definition["is_abstract"]
            and key_count == 0
            and not invalid_property_names
        ):
            _issue(unresolved, "missing_primary_key", f"对象类型“{name}”必须明确一个主键属性", source_refs=meta["evidence_refs"])
        title_count = sum(
            bool(prop.get("is_title")) for prop in combined_properties.values()
        )
        if title_count > 1:
            _issue(
                unresolved,
                "multiple_title_properties",
                f"对象类型“{name}”合并已有定义后包含 {title_count} 个标题属性",
                source_refs=meta["evidence_refs"],
            )
        if not definition["is_abstract"] and title_count == 0 and key_count == 1:
            key_name = next(
                prop_name
                for prop_name, prop in combined_properties.items()
                if bool(prop.get("is_key"))
            )
            combined_properties[key_name]["is_title"] = True
            proposed_key = next(
                (
                    prop for prop in definition["properties"]
                    if prop.get("name") == key_name
                ),
                None,
            )
            if proposed_key is None:
                current_key = existing_props.get(key_name)
                if current_key is not None:
                    proposed_key = _property_definition(current_key)
                    proposed_key.update({
                        "is_title": True,
                        "_operation": "update",
                        "_structural_update": "title_fallback",
                    })
                    definition["properties"].append(proposed_key)
            else:
                proposed_key["is_title"] = True
                if existing_props.get(key_name) is not None:
                    proposed_key["_operation"] = "update"
                    proposed_key["_structural_update"] = "title_fallback"
            _issue(
                unresolved,
                "title_fallback_to_primary_key",
                f"对象类型“{name}”未提供标题属性，已确定性使用唯一主键“{key_name}”作为标题",
                source_refs=meta["evidence_refs"],
                blocking=False,
            )
            title_count = 1
        if (
            not definition["is_abstract"]
            and title_count == 0
            and not invalid_property_names
        ):
            _issue(
                unresolved,
                "missing_title_property",
                f"对象类型“{name}”必须明确一个标题属性",
                source_refs=meta["evidence_refs"],
            )
        try:
            combined_input = EntityIn.model_validate({
                "name": name,
                "description": definition["description"],
                "is_abstract": definition["is_abstract"],
                "state_property": definition["state_property"],
                "properties": list(combined_properties.values()),
            })
            ontology_service.validate_entity_definition(
                combined_input, scenario_namespace=scenario.namespace or "default"
            )
        except Exception as exc:  # noqa: BLE001
            # Primary-key multiplicity already has a precise blocker above.
            # Repeating it as an invalid combined entity makes one defect look
            # like two independent decisions.  The same applies to an
            # otherwise identical validation error on a brand-new entity.
            duplicate_validation = (
                (key_count > 1 and str(exc) == "一个实体最多只能有一个主键属性")
                or (
                    title_count > 1
                    and str(exc) == "一个对象类型最多只能有一个标题属性"
                )
                or (
                    not existing
                    and bool(entity_validation_error)
                    and str(exc) == entity_validation_error
                )
            )
            if not duplicate_validation:
                _issue(
                    unresolved,
                    "invalid_combined_entity",
                    f"对象类型“{name}”与已有定义合并后无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
        entity_fields_changed = bool(existing) and any(
            getattr(existing, field) != definition[field]
            for field in ("description", "is_abstract", "state_property")
        )
        property_changes = any(
            prop.get("_operation") in {"add", "update"}
            for prop in definition["properties"]
        )
        operation = "add" if not existing else "update" if entity_fields_changed or property_changes else "skip"
        entities.append({
            **meta,
            **definition,
            "existing_id": existing_id,
            "operation": operation,
            "_invalid_property_names": sorted(invalid_property_names),
        })

    relations: list[dict[str, Any]] = []
    used_relation_api_names = {
        ontology_service.normalize_api_name(
            getattr(relation, "api_name", ""),
            display_name=relation.name,
            prefix="relation",
            stable_key=relation.id,
        )
        for relation in scenario.relations
    }
    for index, value in enumerate(raw.get("relations") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"relation:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        raw_relation_type = _text(
            value.get("relation_type") or "1:N", maximum=80
        )
        relation_type = _normalize_compiler_relation_type(raw_relation_type)
        if not relation_type:
            _issue(
                unresolved,
                "invalid_relation_cardinality",
                f"关系 {key} 的基数不受支持：{raw_relation_type}",
                source_refs=meta["evidence_refs"],
            )
            relation_type = "N:M"
        source = _resolve_ref(
            value.get("source_ref") or value.get("source"), generated=entities,
            existing=scenario.entities, resource_label=f"关系 {key} 的来源对象", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        target = _resolve_ref(
            value.get("target_ref") or value.get("target"), generated=entities,
            existing=scenario.entities, resource_label=f"关系 {key} 的目标对象", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        if value.get("properties") or value.get("attributes"):
            _issue(
                unresolved,
                "relationship_metadata_requires_linking_object",
                f"关系 {key} 自身包含业务属性，应先建模为关联对象类型",
                source_refs=meta["evidence_refs"],
            )
        try:
            constraints, nested_inverse_ref = _normalize_compiler_relation_constraints(
                value.get("constraints"), relation_type=relation_type
            )
        except Exception as exc:  # noqa: BLE001
            _issue(
                unresolved,
                "invalid_relation_constraints",
                f"关系 {key} 的本体公理无效：{exc}",
                source_refs=meta["evidence_refs"],
            )
            constraints, nested_inverse_ref = {}, ""
        top_inverse_ref = _text(value.get("inverse_relation_ref"), maximum=300)
        if top_inverse_ref and nested_inverse_ref and top_inverse_ref != nested_inverse_ref:
            _issue(
                unresolved,
                "conflicting_inverse_relation_reference",
                f"关系 {key} 同时提供了两个不同的逆关系引用",
                source_refs=meta["evidence_refs"],
            )
        inverse_relation_ref = top_inverse_ref or nested_inverse_ref
        if source and target:
            try:
                ontology_service.validate_relation_constraint_endpoints(
                    constraints,
                    source_entity_id=_reference_token(source),
                    target_entity_id=_reference_token(target),
                )
            except Exception as exc:  # noqa: BLE001
                _issue(
                    unresolved,
                    "invalid_relation_constraint_endpoints",
                    f"关系 {key} 的本体公理无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
        relation_name = _text(value.get("name") or key, maximum=200)
        relation_matches = [item for item in scenario.relations if item.name == relation_name]
        existing_id = ""
        existing = None
        if len(relation_matches) > 1:
            _issue(unresolved, "ambiguous_existing_resource", f"已有关系名称不唯一：{value.get('name') or key}", source_refs=meta["evidence_refs"])
        elif len(relation_matches) == 1:
            existing = relation_matches[0]
            source_id = (source or {}).get("id") if (source or {}).get("kind") == "existing" else ""
            target_id = (target or {}).get("id") if (target or {}).get("kind") == "existing" else ""
            if (
                source_id == existing.source_entity_id
                and target_id == existing.target_entity_id
                and relation_type == existing.relation_type
            ):
                existing_id = existing.id
            else:
                _issue(unresolved, "existing_relation_conflict", f"关系“{existing.name}”与已有定义冲突", source_refs=meta["evidence_refs"])
        if existing is not None:
            relation_api_name = ontology_service.normalize_api_name(
                getattr(existing, "api_name", ""),
                display_name=existing.name,
                prefix="relation",
                stable_key=existing.id,
            )
            requested_api_name = str(value.get("api_name") or "").strip()
            if requested_api_name and ontology_service.normalize_api_name(
                requested_api_name,
                display_name=relation_name,
                prefix="relation",
                stable_key=existing.id,
            ) != relation_api_name:
                _issue(
                    unresolved,
                    "immutable_api_name",
                    f"关系“{relation_name}”的 api_name 创建后不能修改",
                    source_refs=meta["evidence_refs"],
                )
        else:
            try:
                relation_api_name = ontology_service.reserve_api_name(
                    used_relation_api_names,
                    value.get("api_name"),
                    display_name=relation_name,
                    prefix="relation",
                    stable_key=key,
                )
            except ValueError as exc:
                _issue(
                    unresolved,
                    "duplicate_api_name",
                    f"关系“{relation_name}”的 api_name 无效：{exc}",
                    source_refs=meta["evidence_refs"],
                )
                relation_api_name = ontology_service.normalize_api_name(
                    display_name=relation_name,
                    prefix="relation",
                    stable_key=key,
                )
        try:
            navigation = ontology_service.normalize_relation_navigation(
                relation_name=relation_name,
                relation_api_name=relation_api_name,
                source_display_name=value.get("source_display_name"),
                source_api_name=value.get("source_api_name"),
                target_display_name=value.get("target_display_name"),
                target_api_name=value.get("target_api_name"),
                current=existing,
            )
        except ValueError as exc:
            _issue(
                unresolved,
                "immutable_api_name",
                f"关系“{relation_name}”的双侧导航定义无效：{exc}",
                source_refs=meta["evidence_refs"],
            )
            navigation = ontology_service.normalize_relation_navigation(
                relation_name=relation_name,
                relation_api_name=relation_api_name,
                current=existing,
            )
        try:
            storage_kind = ontology_service.normalize_relation_storage_kind(
                value.get("storage_kind"),
                current=getattr(existing, "storage_kind", "") if existing else "",
            )
        except ValueError as exc:
            _issue(
                unresolved,
                "invalid_relation_storage_kind",
                f"关系“{relation_name}”的存储策略无效：{exc}",
                source_refs=meta["evidence_refs"],
            )
            storage_kind = ontology_service.normalize_relation_storage_kind(
                None,
                current=getattr(existing, "storage_kind", "") if existing else "none",
            )
        relations.append({
            **meta,
            "name": relation_name,
            "api_name": relation_api_name,
            "description": _text(value.get("description")),
            "relation_type": relation_type,
            "source": source,
            "target": target,
            **navigation,
            "storage_kind": storage_kind,
            "constraints": constraints,
            "_inverse_relation_ref": inverse_relation_ref,
            "existing_id": existing_id,
        })

    existing_relations_by_id = {str(item.id): item for item in scenario.relations}
    for item in relations:
        inverse_ref = item.pop("_inverse_relation_ref", "")
        item["inverse_relation"] = (
            _resolve_ref(
                inverse_ref,
                generated=relations,
                existing=scenario.relations,
                resource_label=f"关系 {item['key']} 的逆关系",
                unresolved=unresolved,
                source_refs=item["evidence_refs"],
            )
            if inverse_ref
            else None
        )
        existing = existing_relations_by_id.get(str(item.get("existing_id") or ""))
        if existing is None:
            item["operation"] = "add"
            continue
        try:
            current_constraints = ontology_service.normalize_relation_constraints(
                existing.constraints or {}, relation_type=existing.relation_type
            )
        except Exception as exc:  # noqa: BLE001
            _issue(
                unresolved,
                "invalid_existing_relation_constraints",
                f"已有关系“{existing.name}”的本体公理无效：{exc}",
                source_refs=item["evidence_refs"],
            )
            current_constraints = {}
        current_inverse_id = str(current_constraints.pop("inverse_relation_id", "") or "")
        inverse = item.get("inverse_relation") or {}
        proposed_inverse = (
            str(inverse.get("id") or "")
            if inverse.get("kind") == "existing"
            else f"generated:{inverse.get('key')}" if inverse else ""
        )
        item["operation"] = (
            "update"
            if (
                (existing.description or "") != item.get("description", "")
                or (existing.source_display_name or "")
                != item.get("source_display_name", "")
                or (existing.target_display_name or "")
                != item.get("target_display_name", "")
                or (existing.storage_kind or "none")
                != item.get("storage_kind", "none")
                or current_constraints != item.get("constraints", {})
                or current_inverse_id != proposed_inverse
            )
            else "skip"
        )

    functions: list[dict[str, Any]] = []
    for index, value in enumerate(raw.get("functions") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"function:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        try:
            definition = function_definition_service.normalize_definition({
                "name": _text(value.get("name"), maximum=200),
                "description": _text(value.get("description")),
                "input_schema": _object_schema(value.get("input_schema")),
                "output_schema": _object_schema(value.get("output_schema")),
                "tags": value.get("tags") or [],
                "visibility": "scenario",
                "runtime_kind": "contract",
                "runtime_config": {},
            })
        except Exception as exc:  # noqa: BLE001
            _issue(unresolved, "invalid_function", f"函数契约 {key} 无效：{exc}", source_refs=meta["evidence_refs"])
            continue
        functions.append({**meta, **definition})

    actions: list[dict[str, Any]] = []
    for index, value in enumerate(raw.get("actions") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"action:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        entity_ref = _resolve_ref(
            value.get("entity_ref"), generated=entities, existing=scenario.entities,
            resource_label=f"操作 {key} 的对象类型", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        try:
            input_schema = _object_schema(value.get("input_schema"))
        except Exception as exc:  # noqa: BLE001
            _issue(unresolved, "invalid_action_schema", f"操作 {key} 的输入契约无效：{exc}", source_refs=meta["evidence_refs"])
            input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        actions.append({
            **meta,
            "name": _text(value.get("name") or key, maximum=200),
            "description": _text(value.get("description")),
            "entity": entity_ref,
            "input_schema": input_schema,
            "precondition": _text(value.get("precondition")),
            "postcondition": _text(value.get("postcondition")),
            "executor_type": "unbound",
            "executor_config": {},
            "enabled": False,
        })

    rules: list[dict[str, Any]] = []
    for index, value in enumerate(raw.get("rules") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"rule:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        entity_ref = _resolve_ref(
            value.get("entity_ref"), generated=entities, existing=scenario.entities,
            resource_label=f"规则 {key} 的对象类型", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        try:
            condition = _normalize_rule_condition(value.get("condition"))
        except Exception as exc:  # noqa: BLE001
            if _looks_like_class_axiom_rule(value):
                _issue(
                    unresolved,
                    "unsupported_class_axiom",
                    f"规则 {key} 描述的是类等价、互斥或继承等本体结构；当前 P0 不能把它伪装成对象记录规则，请保留为待建模本体公理：{exc}",
                    source_refs=meta["evidence_refs"],
                )
            elif _looks_like_relation_axiom_rule(value):
                _issue(
                    unresolved,
                    "relation_axiom_requires_relation_constraint",
                    f"规则 {key} 描述的是图公理；请将其建模到关系 constraints 或 inverse_relation_ref，不能放入对象记录规则：{exc}",
                    source_refs=meta["evidence_refs"],
                )
            else:
                _issue(unresolved, "invalid_rule_condition", f"规则 {key} 无效：{exc}", source_refs=meta["evidence_refs"])
            condition = {}
        if entity_ref and condition:
            available_fields = _entity_property_names_for_ref(scenario, entities, entity_ref)
            condition = _rewrite_self_qualified_rule_fields(
                condition,
                qualifiers=_entity_field_qualifiers_for_ref(
                    scenario, entities, entity_ref
                ),
                available_fields=available_fields,
            )
            ambiguous_literals = sorted(
                _ambiguous_rule_literal_fields(condition, available_fields)
            )
            if ambiguous_literals:
                _issue(
                    unresolved,
                    "ambiguous_rule_literal_field",
                    f"规则 {key} 的字符串 value 与同对象字段同名："
                    f"{'、'.join(ambiguous_literals)}；字段比较必须显式使用 value_field",
                    source_refs=meta["evidence_refs"],
                )
            invalid_fields = _entity_invalid_property_names_for_ref(
                entities, entity_ref
            )
            unknown_fields = sorted(
                _condition_fields(condition) - available_fields - invalid_fields
            )
            if unknown_fields:
                _issue(
                    unresolved,
                    "unknown_rule_field",
                    f"规则 {key} 引用了对象类型中不存在的字段：{'、'.join(unknown_fields)}",
                    source_refs=meta["evidence_refs"],
                )
        action_refs = [
            _resolve_ref(
                ref, generated=actions, existing=scenario.actions,
                resource_label=f"规则 {key} 的触发操作", unresolved=unresolved,
                source_refs=meta["evidence_refs"],
            )
            for ref in (value.get("trigger_action_refs") or [])
        ]
        try:
            severity = _normalize_rule_severity(value.get("severity"))
        except Exception as exc:  # noqa: BLE001
            _issue(
                unresolved,
                "invalid_rule_severity",
                f"规则 {key} 的严重级别无效：{exc}",
                source_refs=meta["evidence_refs"],
            )
            severity = "info"
        rules.append({
            **meta,
            "name": _text(value.get("name") or key, maximum=200),
            "description": _text(value.get("description")),
            "entity": entity_ref,
            "condition": condition,
            "action_on_match": _text(value.get("action_on_match")),
            "trigger_actions": [item for item in action_refs if item],
            "severity": severity,
            "enabled": False,
        })

    events: list[dict[str, Any]] = []
    for index, value in enumerate(raw.get("events") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"event:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        try:
            payload_schema = _object_schema(value.get("payload_schema"))
        except Exception as exc:  # noqa: BLE001
            _issue(unresolved, "invalid_event_schema", f"事件 {key} 的载荷契约无效：{exc}", source_refs=meta["evidence_refs"])
            payload_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        events.append({
            **meta,
            "name": _text(value.get("name") or key, maximum=200),
            "description": _text(value.get("description")),
            "payload_schema": payload_schema,
            "trigger_source": _text(value.get("trigger_source")),
            "enabled": False,
        })

    workflows: list[dict[str, Any]] = []
    allowed_nodes = {"start", "end", "action", "rule", "event", "approval"}
    resources = {
        "action": (actions, scenario.actions, "action_id"),
        "rule": (rules, scenario.rules, "rule_id"),
        "event": (events, scenario.events, "event_id"),
    }
    for index, value in enumerate(raw.get("workflows") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"workflow:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        raw_nodes = [
            item for item in (value.get("nodes") or []) if isinstance(item, dict)
        ]
        nodes: list[dict[str, Any]] = []
        unsupported_nodes: list[tuple[str, str]] = []
        missing_resource_nodes: list[tuple[str, str]] = []
        for node_index, raw_node in enumerate(raw_nodes, 1):
            node_type = _infer_workflow_node_type(raw_node, resources)
            node_id = _text(raw_node.get("id") or f"n{node_index}", maximum=100)
            if node_type not in allowed_nodes:
                unsupported_nodes.append((node_id, node_type))
            node_data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else {}
            safe_data = {"label": _text(node_data.get("label") or raw_node.get("name") or node_id, maximum=300)}
            if node_type in resources:
                generated, existing, id_key = resources[node_type]
                token = _workflow_reference_token(raw_node, node_type)
                if not token:
                    token = _infer_workflow_resource_token(
                        raw_node, generated=generated, existing=existing
                    )
                if token:
                    resolved = _resolve_ref(
                        token,
                        generated=generated, existing=existing,
                        resource_label=f"工作流 {key} 的 {node_type} 节点", unresolved=unresolved,
                        source_refs=meta["evidence_refs"],
                    )
                else:
                    resolved = None
                    missing_resource_nodes.append((node_id, node_type))
                safe_data["resource"] = resolved
            elif node_type == "approval":
                if node_data.get("timeout_seconds") not in (None, ""):
                    safe_data["timeout_seconds"] = node_data.get("timeout_seconds")
                if node_data.get("on_timeout") not in (None, ""):
                    safe_data["on_timeout"] = _text(node_data.get("on_timeout"), maximum=20)
            nodes.append({
                "id": node_id,
                "type": node_type,
                "name": safe_data["label"],
                "position": raw_node.get("position") if isinstance(raw_node.get("position"), dict) else {"x": node_index * 180, "y": 100},
                "data": safe_data,
            })
        if unsupported_nodes:
            details = "、".join(
                f"{node_id}（{node_type or '空类型'}）"
                for node_id, node_type in unsupported_nodes[:12]
            )
            suffix = f"等 {len(unsupported_nodes)} 个节点" if len(unsupported_nodes) > 12 else ""
            _issue(
                unresolved,
                "unsupported_workflow_node",
                f"工作流 {key} 包含不受支持或无法确定类型的节点：{details}{suffix}",
                source_refs=meta["evidence_refs"],
            )
        if missing_resource_nodes:
            labels = {"action": "操作", "rule": "规则", "event": "事件"}
            details = "、".join(
                f"{node_id}（{labels[node_type]}）"
                for node_id, node_type in missing_resource_nodes[:12]
            )
            suffix = f"等 {len(missing_resource_nodes)} 个节点" if len(missing_resource_nodes) > 12 else ""
            _issue(
                unresolved,
                "missing_workflow_resource_refs",
                f"工作流 {key} 的节点缺少可唯一解析的资源引用：{details}{suffix}",
                source_refs=meta["evidence_refs"],
            )

        aliases = _workflow_node_aliases(
            raw_nodes, [str(node["id"]) for node in nodes]
        )
        edges: list[dict[str, Any]] = []
        for edge_index, edge in enumerate(value.get("edges") or [], 1):
            if not isinstance(edge, dict):
                continue
            source_token = _text(
                _workflow_edge_value(edge, source=True), maximum=300
            )
            target_token = _text(
                _workflow_edge_value(edge, source=False), maximum=300
            )
            raw_label = next(
                (
                    edge.get(field)
                    for field in ("label", "branch", "condition", "sourceHandle")
                    if edge.get(field) not in (None, "")
                ),
                "",
            )
            edges.append({
                "id": _text(edge.get("id") or f"e{edge_index}", maximum=100),
                "source": aliases.get(source_token, source_token),
                "target": aliases.get(target_token, target_token),
                "label": _normalize_workflow_edge_label(raw_label),
            })

        _complete_unambiguous_rule_branch_labels(nodes, edges)
        _add_unambiguous_workflow_boundaries(nodes, edges)

        node_ids = {str(node.get("id") or "") for node in nodes if node.get("id")}
        dangling_edges = [
            edge for edge in edges
            if edge.get("source") not in node_ids or edge.get("target") not in node_ids
        ]
        if dangling_edges:
            examples = "、".join(
                f"{edge.get('id')}({edge.get('source') or '空'}→{edge.get('target') or '空'})"
                for edge in dangling_edges[:8]
            )
            suffix = f"等 {len(dangling_edges)} 条" if len(dangling_edges) > 8 else ""
            _issue(
                unresolved,
                "workflow_graph_reference_mismatch",
                f"工作流 {key} 的连线无法对应到现有节点：{examples}{suffix}",
                source_refs=meta["evidence_refs"],
            )
        elif not unsupported_nodes:
            try:
                workflow_service.validate_workflow_definition(nodes, edges)
            except Exception as exc:  # noqa: BLE001
                branch_gaps = _workflow_rule_branch_gaps(nodes, edges)
                if branch_gaps and "规则节点" in str(exc):
                    details = "、".join(
                        f"{node_id}（缺少 {'/'.join(missing)}）"
                        for node_id, missing in branch_gaps[:20]
                    )
                    suffix = f"等 {len(branch_gaps)} 个规则节点" if len(branch_gaps) > 20 else ""
                    message = f"工作流 {key} 的规则分支不完整：{details}{suffix}"
                else:
                    message = f"工作流 {key} 无效：{exc}"
                _issue(
                    unresolved,
                    "invalid_workflow",
                    message,
                    source_refs=meta["evidence_refs"],
                )
        try:
            operations_service.validate_approval_nodes(nodes, [])
            for node in nodes:
                if node.get("type") == "approval":
                    node_data = node.get("data") or {}
                    if node_data.get("timeout_seconds") not in (None, ""):
                        node_data["timeout_seconds"] = int(node_data["timeout_seconds"])
        except Exception as exc:  # noqa: BLE001
            _issue(unresolved, "invalid_workflow", f"工作流 {key} 无效：{exc}", source_refs=meta["evidence_refs"])
        trigger_type = _text(value.get("trigger_type") or "manual", maximum=30)
        if trigger_type not in {"manual", "scheduled", "event"}:
            _issue(
                unresolved,
                "invalid_workflow_trigger",
                f"工作流 {key} 的触发类型不受支持：{trigger_type or '空值'}",
                source_refs=meta["evidence_refs"],
            )
            trigger_type = "manual"
        raw_trigger_config = value.get("trigger_config") if isinstance(value.get("trigger_config"), dict) else {}
        trigger_event = None
        trigger_config: dict[str, Any] = {}
        try:
            if raw_trigger_config.get("cron") or raw_trigger_config.get("timezone"):
                raise PolicyViolation("当前运行时不支持 cron/timezone，请改用 interval_seconds")
            allowed_raw_fields = {
                "max_attempts", "timeout_seconds", "retry_backoff_seconds",
            }
            if trigger_type == "scheduled":
                allowed_raw_fields.add("interval_seconds")
            elif trigger_type == "event":
                allowed_raw_fields.update({"event_ref", "event_id", "event_name"})
            unsupported_fields = sorted(set(raw_trigger_config) - allowed_raw_fields)
            if unsupported_fields:
                raise PolicyViolation(
                    f"包含未治理字段：{'、'.join(unsupported_fields)}"
                )
            policy = operations_service.runtime_policy(raw_trigger_config)
            trigger_config.update(policy)
            if trigger_type == "scheduled":
                trigger_config["interval_seconds"] = raw_trigger_config.get("interval_seconds")
            elif trigger_type == "event":
                event_token = (
                    raw_trigger_config.get("event_ref")
                    or raw_trigger_config.get("event_id")
                    or raw_trigger_config.get("event_name")
                )
                trigger_event = _resolve_ref(
                    event_token,
                    generated=events,
                    existing=scenario.events,
                    resource_label=f"工作流 {key} 的触发事件",
                    unresolved=unresolved,
                    source_refs=meta["evidence_refs"],
                )
            validation_config = dict(trigger_config)
            if trigger_type == "event" and trigger_event:
                validation_config["event_id"] = (
                    trigger_event.get("id") or f"generated:{trigger_event.get('key')}"
                )
            operations_service.validate_trigger_config(trigger_type, validation_config)
            if trigger_type == "scheduled":
                trigger_config["interval_seconds"] = int(trigger_config["interval_seconds"])
        except Exception as exc:  # noqa: BLE001
            _issue(
                unresolved,
                "invalid_workflow_trigger",
                f"工作流 {key} 的触发配置无效：{exc}",
                source_refs=meta["evidence_refs"],
            )
        workflows.append({
            **meta,
            "name": _text(value.get("name") or key, maximum=200),
            "description": _text(value.get("description")),
            "trigger_type": trigger_type,
            "trigger_config": trigger_config,
            "trigger_event": trigger_event,
            "nodes": nodes,
            "edges": edges,
            "status": "draft",
            "enabled": False,
        })

    mapping_catalog = mapping_catalog or []
    columns_by_table = columns_by_table or {}
    data_sources = [
        type("CatalogSource", (), {"id": item["data_source_id"], "name": item["data_source_name"]})()
        for item in mapping_catalog
    ]
    mappings: list[dict[str, Any]] = []
    for index, value in enumerate(raw.get("mappings") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"mapping:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        entity_ref = _resolve_ref(
            value.get("entity_ref"), generated=entities, existing=scenario.entities,
            resource_label=f"映射 {key} 的对象类型", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        source_ref = _resolve_ref(
            value.get("data_source_ref"), generated=[], existing=data_sources,
            resource_label=f"映射 {key} 的数据源", unresolved=unresolved,
            source_refs=meta["evidence_refs"],
        )
        table_name = _text(value.get("table_name"), maximum=300)
        source_id = (source_ref or {}).get("id", "")
        available = columns_by_table.get((source_id, table_name))
        raw_map = value.get("column_map")
        column_map = {
            _text(prop, maximum=200): _text(column, maximum=300)
            for prop, column in (raw_map.items() if isinstance(raw_map, dict) else [])
            if _text(prop, maximum=200) and _text(column, maximum=300)
        }
        if available is None:
            _issue(unresolved, "missing_mapping_table", f"映射 {key} 引用的真实源表不存在", source_refs=meta["evidence_refs"])
        elif any(column not in available for column in column_map.values()):
            _issue(unresolved, "missing_mapping_column", f"映射 {key} 引用了源表中不存在的字段", source_refs=meta["evidence_refs"])
        if not column_map:
            _issue(unresolved, "empty_mapping", f"映射 {key} 没有字段映射", source_refs=meta["evidence_refs"])
        mappings.append({
            **meta,
            "entity": entity_ref,
            "data_source": source_ref,
            "table_name": table_name,
            "column_map": column_map,
        })

    mapping_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in mappings:
        entity_ref = item.get("entity") or {}
        identity = (
            f"existing:{entity_ref.get('id')}"
            if entity_ref.get("kind") == "existing"
            else f"generated:{entity_ref.get('key')}"
        )
        mapping_by_entity[identity].append(item)
        item["apply_plan"] = _mapping_plan(scenario, entities, item)
    for identity, items in mapping_by_entity.items():
        if len(items) > 1:
            _issue(
                unresolved,
                "duplicate_entity_mapping",
                f"同一对象类型 {identity} 在一次变更中只能有一条数据映射",
                source_refs=[ref for item in items for ref in item.get("evidence_refs") or []],
            )

    relation_mappings: list[dict[str, Any]] = []
    relation_mapping_allowed_fields = {
        "key", "relation_ref", "source_mapping_ref", "target_mapping_ref",
        "mode", "foreign_key_column", "join_data_source_ref",
        "join_table_name", "source_key_column", "target_key_column",
        "evidence_refs", "confidence",
    }
    for index, value in enumerate(raw.get("relation_mappings") or [], 1):
        if not isinstance(value, dict):
            continue
        key = _text(value.get("key") or f"relation_mapping:{index}", maximum=200)
        meta = _meta(value, key=key, valid_sources=valid_sources, unresolved=unresolved)
        unsupported_fields = sorted(set(value) - relation_mapping_allowed_fields)
        if unsupported_fields:
            _issue(
                unresolved,
                "unsupported_relation_mapping_fields",
                f"关系映射 {key} 包含未治理字段：{'、'.join(unsupported_fields)}",
                source_refs=meta["evidence_refs"],
            )
        relation_ref = _resolve_ref(
            value.get("relation_ref"), generated=relations,
            existing=scenario.relations,
            resource_label=f"关系映射 {key} 的关系",
            unresolved=unresolved, source_refs=meta["evidence_refs"],
        )
        source_mapping_ref = _resolve_ref(
            value.get("source_mapping_ref"), generated=mappings,
            existing=scenario.data_mappings,
            resource_label=f"关系映射 {key} 的源对象映射",
            unresolved=unresolved, source_refs=meta["evidence_refs"],
        )
        target_mapping_ref = _resolve_ref(
            value.get("target_mapping_ref"), generated=mappings,
            existing=scenario.data_mappings,
            resource_label=f"关系映射 {key} 的目标对象映射",
            unresolved=unresolved, source_refs=meta["evidence_refs"],
        )
        expected_source, expected_target = _relation_endpoints_for_ref(
            scenario, relations, relation_ref
        )
        mapped_source = _mapping_entity_for_ref(
            scenario, mappings, source_mapping_ref
        )
        mapped_target = _mapping_entity_for_ref(
            scenario, mappings, target_mapping_ref
        )
        if expected_source and mapped_source and (
            _reference_token(expected_source) != _reference_token(mapped_source)
        ):
            _issue(
                unresolved,
                "relation_mapping_endpoint_mismatch",
                f"关系映射 {key} 的源对象映射不属于关系的源对象类型",
                source_refs=meta["evidence_refs"],
            )
        if expected_target and mapped_target and (
            _reference_token(expected_target) != _reference_token(mapped_target)
        ):
            _issue(
                unresolved,
                "relation_mapping_endpoint_mismatch",
                f"关系映射 {key} 的目标对象映射不属于关系的目标对象类型",
                source_refs=meta["evidence_refs"],
            )

        mode = _text(value.get("mode"), maximum=30).lower()
        if mode not in {"source_fk", "target_fk", "join_table"}:
            _issue(
                unresolved,
                "invalid_relation_mapping_mode",
                f"关系映射 {key} 的模式不受支持：{mode or '空值'}",
                source_refs=meta["evidence_refs"],
            )
        foreign_key_column = _text(value.get("foreign_key_column"), maximum=300)
        join_table_name = _text(value.get("join_table_name"), maximum=300)
        source_key_column = _text(value.get("source_key_column"), maximum=300)
        target_key_column = _text(value.get("target_key_column"), maximum=300)
        join_source_ref: dict[str, str] | None = None
        carrier: dict[str, Any] = {}
        if mode in {"source_fk", "target_fk"}:
            irrelevant = [
                field
                for field in (
                    "join_data_source_ref", "join_table_name",
                    "source_key_column", "target_key_column",
                )
                if value.get(field) not in (None, "")
            ]
            if irrelevant:
                _issue(
                    unresolved,
                    "conflicting_relation_mapping_fields",
                    f"关系映射 {key} 的外键模式不能填写：{'、'.join(irrelevant)}",
                    source_refs=meta["evidence_refs"],
                )
            if not foreign_key_column:
                _issue(
                    unresolved,
                    "missing_relation_mapping_foreign_key",
                    f"关系映射 {key} 未选择承载侧外键列",
                    source_refs=meta["evidence_refs"],
                )
            carrier_ref = (
                source_mapping_ref if mode == "source_fk" else target_mapping_ref
            )
            physical = _mapping_physical_definition(
                scenario, mappings, carrier_ref
            )
            if physical:
                carrier = {
                    "data_source_id": physical["data_source_id"],
                    "table_name": physical["table_name"],
                }
                available = columns_by_table.get((
                    physical["data_source_id"], physical["table_name"]
                ))
                if available is None:
                    _issue(
                        unresolved,
                        "uninspected_relation_mapping_table",
                        f"关系映射 {key} 的承载侧真实表未被成功检查",
                        source_refs=meta["evidence_refs"],
                    )
                elif foreign_key_column and foreign_key_column not in available:
                    _issue(
                        unresolved,
                        "missing_relation_mapping_column",
                        f"关系映射 {key} 的外键列“{foreign_key_column}”不存在",
                        source_refs=meta["evidence_refs"],
                    )
        elif mode == "join_table":
            if foreign_key_column:
                _issue(
                    unresolved,
                    "conflicting_relation_mapping_fields",
                    f"关系映射 {key} 的中间表模式不能填写 foreign_key_column",
                    source_refs=meta["evidence_refs"],
                )
            join_source_ref = _resolve_ref(
                value.get("join_data_source_ref"), generated=[], existing=data_sources,
                resource_label=f"关系映射 {key} 的中间表数据源",
                unresolved=unresolved, source_refs=meta["evidence_refs"],
            )
            if not join_table_name or not source_key_column or not target_key_column:
                _issue(
                    unresolved,
                    "incomplete_join_table_relation_mapping",
                    f"关系映射 {key} 必须选择中间表及源、目标键列",
                    source_refs=meta["evidence_refs"],
                )
            join_source_id = str((join_source_ref or {}).get("id") or "")
            available = columns_by_table.get((join_source_id, join_table_name))
            if join_source_ref and available is None:
                _issue(
                    unresolved,
                    "missing_relation_mapping_table",
                    f"关系映射 {key} 引用的真实中间表不存在",
                    source_refs=meta["evidence_refs"],
                )
            elif available is not None:
                missing_columns = sorted({
                    column for column in (source_key_column, target_key_column)
                    if column and column not in available
                })
                if missing_columns:
                    _issue(
                        unresolved,
                        "missing_relation_mapping_column",
                        f"关系映射 {key} 引用了中间表中不存在的列：{'、'.join(missing_columns)}",
                        source_refs=meta["evidence_refs"],
                    )
        item = {
            **meta,
            "relation": relation_ref,
            "source_mapping": source_mapping_ref,
            "target_mapping": target_mapping_ref,
            "mode": mode,
            "foreign_key_column": foreign_key_column,
            "join_data_source": join_source_ref,
            "join_table_name": join_table_name,
            "source_key_column": source_key_column,
            "target_key_column": target_key_column,
            "carrier": carrier,
        }
        item["apply_plan"] = _relation_mapping_plan(scenario, mappings, item)
        relation_mappings.append(item)

    relation_mapping_by_relation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in relation_mappings:
        if item.get("relation"):
            relation_mapping_by_relation[_reference_token(item["relation"])].append(item)
    for identity, items in relation_mapping_by_relation.items():
        if len(items) > 1:
            _issue(
                unresolved,
                "duplicate_relation_mapping",
                f"同一关系 {identity} 在一次变更中只能有一条关系映射",
                source_refs=[ref for item in items for ref in item.get("evidence_refs") or []],
            )

    deleted_mapping_ids = {
        str(mapping_id)
        for item in mappings
        for mapping_id in ((item.get("apply_plan") or {}).get("delete_ids") or [])
    }
    updated_relation_mapping_ids = {
        str((item.get("apply_plan") or {}).get("existing_id") or "")
        for item in relation_mappings
    }
    for existing_relation_mapping in getattr(scenario, "relation_data_mappings", []):
        if not (
            deleted_mapping_ids
            & {
                str(existing_relation_mapping.source_mapping_id),
                str(existing_relation_mapping.target_mapping_id),
            }
        ):
            continue
        if str(existing_relation_mapping.id) not in updated_relation_mapping_ids:
            _issue(
                unresolved,
                "stale_relation_mapping_dependency",
                "对象映射身份将被替换，必须在同一复合变更中重新配置受影响的关系映射",
            )

    sections = {
        "entities": entities,
        "relations": relations,
        "functions": functions,
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": workflows,
        "mappings": mappings,
        "relation_mappings": relation_mappings,
    }
    for section, items in sections.items():
        keys = [str(item.get("key")) for item in items]
        names = [str(item.get("name")) for item in items if item.get("name")]
        if len(keys) != len(set(keys)):
            _issue(unresolved, "duplicate_change_key", f"{section} 中存在重复 key")
        if section not in {"mappings", "relation_mappings"} and len(names) != len(set(names)):
            _issue(unresolved, "duplicate_generated_name", f"{section} 中存在重复名称")
    global_keys = [str(item.get("key")) for items in sections.values() for item in items]
    if len(global_keys) != len(set(global_keys)):
        duplicates = sorted({key for key in global_keys if global_keys.count(key) > 1})
        _issue(
            unresolved,
            "duplicate_change_key",
            f"跨资源变更 key 必须全局唯一：{'、'.join(duplicates)}",
        )

    # Existing non-entity resources are never overwritten by an AI document.
    for section, existing in (
        ("functions", scenario.function_definitions), ("actions", scenario.actions),
        ("rules", scenario.rules), ("events", scenario.events), ("workflows", scenario.workflows),
    ):
        existing_names = {item.name for item in existing}
        for item in sections[section]:
            if item.get("name") in existing_names:
                _issue(unresolved, "existing_resource_conflict", f"{section} 中的“{item['name']}”已存在，AI 不会覆盖", source_refs=item["evidence_refs"])

    try:
        _validate_event_feedback_graph(scenario, workflows)
    except PolicyViolation as exc:
        _issue(
            unresolved,
            "event_feedback_loop",
            str(exc),
            source_refs=[ref for item in workflows for ref in item.get("evidence_refs") or []],
        )

    change_keys = {
        item["key"] for items in sections.values() for item in items
    }
    failed_evidence_by_source: dict[str, set[str]] = defaultdict(set)
    for section in _RESOURCE_SECTIONS:
        for raw_item in raw.get(section) or []:
            if not isinstance(raw_item, dict):
                continue
            raw_key = str(raw_item.get("key") or "").strip()
            if raw_key and raw_key in change_keys:
                continue
            for source_ref in raw_item.get("evidence_refs") or []:
                if str(source_ref) in valid_sources:
                    failed_evidence_by_source[str(source_ref)].add(
                        raw_key or f"{section}:invalid"
                    )
    referenced_by: dict[str, list[str]] = defaultdict(list)
    for items in sections.values():
        for item in items:
            for source_ref in item.get("evidence_refs") or []:
                referenced_by[source_ref].append(item["key"])
    explicit_coverage: dict[str, dict[str, Any]] = {}
    for item in raw.get("coverage") or []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        if source_ref not in valid_sources:
            continue
        if source_ref in explicit_coverage:
            _issue(
                unresolved,
                "duplicate_source_coverage",
                f"来源段落 {source_ref} 在完整编译结果中被重复声明 coverage",
                source_refs=[source_ref],
            )
            continue
        status = _text(item.get("status"), maximum=30)
        if status not in {"modeled", "context", "irrelevant", "ambiguous"}:
            status = "ambiguous"
        declared_keys = [
            str(key) for key in (item.get("change_keys") or [])
            if str(key) in change_keys
        ]
        actual_keys = list(dict.fromkeys(referenced_by.get(source_ref) or []))
        linked_keys = [key for key in declared_keys if key in actual_keys]
        if status == "modeled" and actual_keys:
            # evidence_refs are already constrained to this exact source
            # bundle.  They are therefore a stronger, verifiable link than a
            # chunk's stale pre-merge change_keys spelling.  Rebuild the links
            # after key/name alias reconciliation instead of creating a false
            # blocker for an otherwise fully evidenced resource.
            linked_keys = actual_keys
        elif status == "modeled" and not failed_evidence_by_source.get(source_ref):
            _issue(
                unresolved,
                "invalid_modeled_coverage",
                f"来源段落 {source_ref} 声称已建模，但没有关联真实变更及其 evidence",
                source_refs=[source_ref],
            )
        if status != "modeled" and actual_keys:
            _issue(
                unresolved,
                "inconsistent_source_coverage",
                f"来源段落 {source_ref} 已被资源 evidence 引用，不能标记为 {status}",
                source_refs=[source_ref],
            )
        explicit_coverage[source_ref] = {
            "source_ref": source_ref,
            "status": status,
            "reason": _text(item.get("reason"), maximum=1_000),
            "change_keys": linked_keys if status == "modeled" else [],
        }
    coverage: list[dict[str, Any]] = []
    for source_ref in sorted(valid_sources):
        item = explicit_coverage.get(source_ref)
        if item is None and referenced_by.get(source_ref):
            item = {
                "source_ref": source_ref,
                "status": "modeled",
                "reason": "被一个或多个模型变更引用",
                "change_keys": list(dict.fromkeys(referenced_by[source_ref])),
            }
        if item is None:
            _issue(unresolved, "missing_source_coverage", f"来源段落 {source_ref} 未被解释或建模", source_refs=[source_ref])
            item = {"source_ref": source_ref, "status": "ambiguous", "reason": "模型未覆盖", "change_keys": []}
        elif item["status"] == "ambiguous":
            _issue(unresolved, "ambiguous_source", f"来源段落 {source_ref} 尚有歧义", source_refs=[source_ref])
        if not item["reason"]:
            _issue(unresolved, "missing_coverage_reason", f"来源段落 {source_ref} 缺少覆盖说明", source_refs=[source_ref])
        coverage.append(item)

    changes: list[dict[str, Any]] = []
    labels = {
        "entities": "entity", "relations": "relation", "functions": "function",
        "actions": "action", "rules": "rule", "events": "event",
        "workflows": "workflow", "mappings": "mapping",
        "relation_mappings": "relation_mapping",
    }
    for section, items in sections.items():
        for item in items:
            if section == "entities":
                operation = item.get("operation") or ("update" if item.get("existing_id") else "add")
            elif section == "relations":
                operation = item.get("operation") or ("skip" if item.get("existing_id") else "add")
            elif section == "mappings":
                plan_mode = (item.get("apply_plan") or {}).get("mode")
                operation = (
                    "update" if plan_mode == "update"
                    else "skip" if plan_mode == "skip"
                    else "add"
                )
            elif section == "relation_mappings":
                plan_mode = (item.get("apply_plan") or {}).get("mode")
                operation = plan_mode if plan_mode in {"add", "update", "skip"} else "add"
            else:
                operation = "add"
            name = item.get("name") or (
                f"{item.get('table_name') or '数据表'} → 本体对象" if section == "mappings" else item["key"]
            )
            if section == "relation_mappings":
                name = f"{item['key']}（{item.get('mode') or '未指定模式'}）"
            summary = {
                "add": "新增草稿定义",
                "update": "更新已存在的定义",
                "skip": "定义已存在且没有差异，应用时跳过",
            }.get(operation, "变更草稿定义")
            if section == "mappings" and (item.get("apply_plan") or {}).get("mode") == "replace":
                summary = "新增映射身份，并移除同一对象类型的旧映射定义"
            changes.append({
                "change_id": item["key"],
                "operation": operation,
                "resource": labels[section],
                "name": name,
                "summary": summary,
                "depends_on": (
                    [
                        str(ref.get("key"))
                        for ref in (
                            item.get("relation"), item.get("source_mapping"),
                            item.get("target_mapping"), item.get("join_data_source"),
                        )
                        if isinstance(ref, dict) and ref.get("kind") == "generated"
                    ]
                    if section == "relation_mappings"
                    else []
                ),
                "evidence_refs": item.get("evidence_refs") or [],
                "confidence": item.get("confidence", 0),
            })
            if section == "entities":
                for prop in item.get("properties") or []:
                    property_operation = prop.get("_operation") or "add"
                    changes.append({
                        "change_id": f"{item['key']}:property:{prop['name']}",
                        "operation": property_operation,
                        "resource": "property",
                        "name": f"{item['name']}.{prop['name']}",
                        "summary": {
                            "add": "新增对象属性",
                            "update": (
                                "为尚未绑定数据的对象属性补充枚举定义"
                                if prop.get("_structural_update") == "enum_upgrade"
                                else "将唯一主键明确标记为对象标题"
                                if prop.get("_structural_update") == "title_fallback"
                                else "更新对象属性的说明或默认值"
                            ),
                            "skip": "对象属性已存在且定义一致",
                        }.get(property_operation, "变更对象属性"),
                        "depends_on": [item["key"]],
                        "evidence_refs": item.get("evidence_refs") or [],
                        "confidence": item.get("confidence", 0),
                    })
            if section == "mappings":
                for mapping_id in (item.get("apply_plan") or {}).get("delete_ids") or []:
                    changes.append({
                        "change_id": f"{item['key']}:delete:{mapping_id}",
                        "operation": "delete",
                        "resource": "mapping",
                        "name": f"旧数据映射 {mapping_id}",
                        "summary": "映射身份被替换或清理同实体历史重复定义",
                        "depends_on": [item["key"]],
                        "evidence_refs": item.get("evidence_refs") or [],
                        "confidence": item.get("confidence", 0),
                    })

    if (
        not any(item.get("operation") in {"add", "update", "delete"} for item in changes)
        and not any(item.get("blocking", True) for item in unresolved)
    ):
        _issue(
            unresolved,
            "no_applicable_changes",
            "复合业务模型没有可应用的新增、更新或删除变更",
        )

    _assert_unresolved_severity_policy(
        unresolved,
        mapping_is_cleanly_deferred=mapping_is_cleanly_deferred,
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_manifest": source_bundle["documents"],
        "source_refs": sorted(valid_sources),
        "source_paragraph_count": len(valid_sources),
        **sections,
        "changes": changes,
        "unresolved": unresolved,
        "coverage": coverage,
        "coverage_summary": {
            "total": len(coverage),
            "modeled": sum(item["status"] == "modeled" for item in coverage),
            "context": sum(item["status"] == "context" for item in coverage),
            "irrelevant": sum(item["status"] == "irrelevant" for item in coverage),
            "ambiguous": sum(item["status"] == "ambiguous" for item in coverage),
        },
    }
    return payload


def _resolved_id(ref: dict[str, str] | None, created: dict[str, str]) -> str:
    if not ref:
        raise PolicyViolation("复合模型存在未解析引用")
    if ref.get("kind") == "existing":
        resolved = str(ref.get("id") or "")
    else:
        resolved = str(created.get(str(ref.get("key") or "")) or "")
    if not resolved:
        raise PolicyViolation("复合模型存在未解析引用")
    return resolved


def _entity_properties(
    scenario: BusinessScenario,
    payload: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    catalog: dict[str, dict[str, dict[str, Any]]] = {
        entity.id: {
            prop.name: {
                "name": prop.name,
                "api_name": getattr(prop, "api_name", "")
                or ontology_service.normalize_api_name(
                    display_name=prop.name,
                    prefix="property",
                    stable_key=getattr(prop, "id", "") or prop.name,
                ),
                "data_type": prop.data_type,
                "description": prop.description or "",
                "is_key": bool(prop.is_key),
                "is_title": bool(prop.is_title),
                "is_required": bool(prop.is_required),
                "is_enum": bool(prop.is_enum),
                "enum_values": prop.enum_values or [],
                "default_value": prop.default_value or "",
                "constraints": prop.constraints or {},
                "is_sensitive": bool(prop.is_sensitive),
            }
            for prop in entity.properties
        }
        for entity in scenario.entities
    }
    for item in payload.get("entities") or []:
        identity = item.get("existing_id") or f"generated:{item['key']}"
        target = catalog.setdefault(identity, {})
        for prop in item.get("properties") or []:
            existing = target.get(prop["name"])
            if existing and any(
                existing.get(field) != prop.get(field)
                for field in ("data_type", "is_key", "is_required")
            ):
                raise PolicyViolation(
                    f"对象类型“{item['name']}”的属性“{prop['name']}”与已有定义冲突"
                )
            target[prop["name"]] = prop
    return catalog


def _reference_token(ref: dict[str, Any]) -> str:
    if ref.get("kind") == "existing":
        return f"existing:{str(ref.get('id') or '')}"
    return f"generated:{str(ref.get('key') or '')}"


def _assert_reference(
    ref: Any,
    *,
    generated_keys: set[str],
    existing_ids: set[str],
    label: str,
) -> None:
    if not isinstance(ref, dict):
        raise PolicyViolation(f"{label}缺少已解析引用")
    kind = ref.get("kind")
    if kind == "generated" and str(ref.get("key") or "") in generated_keys:
        return
    if kind == "existing" and str(ref.get("id") or "") in existing_ids:
        return
    raise PolicyViolation(f"{label}引用不存在或不属于当前业务场景")


def _validate_coverage(payload: dict[str, Any]) -> None:
    source_refs = [str(value) for value in (payload.get("source_refs") or [])]
    expected_count = int(payload.get("source_paragraph_count") or 0)
    if not source_refs or len(source_refs) != expected_count or len(source_refs) != len(set(source_refs)):
        raise PolicyViolation("文档来源段落清单不完整，不能应用")
    coverage = payload.get("coverage") or []
    coverage_refs = [str(item.get("source_ref") or "") for item in coverage if isinstance(item, dict)]
    if len(coverage) != expected_count or set(coverage_refs) != set(source_refs) or len(coverage_refs) != len(set(coverage_refs)):
        raise PolicyViolation("文档来源覆盖不完整或包含重复段落，不能应用")
    resources = {
        str(item.get("key")): item
        for section in _RESOURCE_SECTIONS
        for item in (payload.get(section) or [])
        if isinstance(item, dict) and item.get("key")
    }
    evidence_by_source: dict[str, set[str]] = defaultdict(set)
    for resource in resources.values():
        evidence = [str(value) for value in (resource.get("evidence_refs") or [])]
        if not evidence or any(value not in source_refs for value in evidence):
            raise PolicyViolation("复合业务模型存在缺失或越界的来源 evidence")
        for source_ref in evidence:
            evidence_by_source[source_ref].add(str(resource.get("key")))
    for item in coverage:
        status = item.get("status")
        if status not in {"modeled", "context", "irrelevant"}:
            raise PolicyViolation("文档仍有歧义或无效覆盖状态，不能应用")
        if not str(item.get("reason") or "").strip():
            raise PolicyViolation("文档来源覆盖缺少说明，不能应用")
        change_keys = [str(value) for value in (item.get("change_keys") or [])]
        if status != "modeled":
            if change_keys:
                raise PolicyViolation("上下文或无关段落不能声明模型变更")
            if evidence_by_source.get(str(item.get("source_ref") or "")):
                raise PolicyViolation("被资源 evidence 引用的段落必须标记为已建模")
            continue
        source_ref = str(item.get("source_ref") or "")
        if not change_keys:
            raise PolicyViolation("已建模段落没有关联真实变更")
        for key in change_keys:
            resource = resources.get(key)
            if not resource or source_ref not in (resource.get("evidence_refs") or []):
                raise PolicyViolation("已建模段落的变更引用与资源 evidence 不一致")


def _validate_compiled_references(
    scenario: BusinessScenario,
    payload: dict[str, Any],
    properties: dict[str, dict[str, dict[str, Any]]],
) -> None:
    sections = {
        section: [item for item in (payload.get(section) or []) if isinstance(item, dict)]
        for section in _RESOURCE_SECTIONS
    }
    all_keys = [str(item.get("key") or "") for items in sections.values() for item in items]
    if any(not key for key in all_keys) or len(all_keys) != len(set(all_keys)):
        raise PolicyViolation("复合模型的资源 key 必须非空且全局唯一")
    generated = {section: {str(item["key"]) for item in items} for section, items in sections.items()}
    existing = {
        "entities": {str(item.id) for item in scenario.entities},
        "relations": {str(item.id) for item in scenario.relations},
        "actions": {str(item.id) for item in scenario.actions},
        "rules": {str(item.id) for item in scenario.rules},
        "events": {str(item.id) for item in scenario.events},
        "mappings": {str(item.id) for item in scenario.data_mappings},
    }
    for item in sections["entities"]:
        existing_id = str(item.get("existing_id") or "")
        if existing_id and existing_id not in existing["entities"]:
            raise PolicyViolation("复合模型扩展的对象类型已不存在")
        identity = existing_id or f"generated:{item['key']}"
        entity_props = properties.get(identity, {})
        key_count = sum(bool(prop.get("is_key")) for prop in entity_props.values())
        if key_count > 1:
            raise PolicyViolation(f"对象类型“{item.get('name')}”包含多个主键")
        if not item.get("is_abstract") and key_count != 1:
            raise PolicyViolation(f"对象类型“{item.get('name')}”必须且只能有一个主键")
        title_count = sum(
            bool(prop.get("is_title")) for prop in entity_props.values()
        )
        if title_count > 1:
            raise PolicyViolation(f"对象类型“{item.get('name')}”包含多个标题属性")
        if not item.get("is_abstract") and title_count != 1:
            raise PolicyViolation(f"对象类型“{item.get('name')}”必须且只能有一个标题属性")
        state_property = str(item.get("state_property") or "")
        if state_property and (
            state_property not in entity_props or not entity_props[state_property].get("is_enum")
        ):
            raise PolicyViolation(f"对象类型“{item.get('name')}”的状态属性无效")
    for item in sections["relations"]:
        _assert_reference(
            item.get("source"), generated_keys=generated["entities"],
            existing_ids=existing["entities"], label=f"关系“{item.get('name')}”的来源对象",
        )
        _assert_reference(
            item.get("target"), generated_keys=generated["entities"],
            existing_ids=existing["entities"], label=f"关系“{item.get('name')}”的目标对象",
        )
        relation_type = str(item.get("relation_type") or "").upper()
        if relation_type not in {"1:1", "1:N", "N:1", "N:M"}:
            raise PolicyViolation(f"关系“{item.get('name')}”的基数无效")
        if (item.get("constraints") or {}).get("inverse_relation_id"):
            raise PolicyViolation("复合模型不能绕过引用解析直接写逆关系 ID")
        constraints = ontology_service.normalize_relation_constraints(
            item.get("constraints") or {}, relation_type=relation_type
        )
        ontology_service.validate_relation_constraint_endpoints(
            constraints,
            source_entity_id=_reference_token(item.get("source") or {}),
            target_entity_id=_reference_token(item.get("target") or {}),
        )
        inverse = item.get("inverse_relation")
        if inverse:
            _assert_reference(
                inverse,
                generated_keys=generated["relations"],
                existing_ids=existing["relations"],
                label=f"关系“{item.get('name')}”的逆关系",
            )
            if inverse.get("kind") == "generated":
                inverse_item = next(
                    entry for entry in sections["relations"]
                    if str(entry.get("key") or "") == str(inverse.get("key") or "")
                )
                inverse_source = _reference_token(inverse_item.get("source") or {})
                inverse_target = _reference_token(inverse_item.get("target") or {})
            else:
                inverse_model = next(
                    relation for relation in scenario.relations
                    if str(relation.id) == str(inverse.get("id") or "")
                )
                inverse_source = f"existing:{inverse_model.source_entity_id}"
                inverse_target = f"existing:{inverse_model.target_entity_id}"
            if (
                _reference_token(item.get("source") or {}) != inverse_target
                or _reference_token(item.get("target") or {}) != inverse_source
            ):
                raise PolicyViolation(
                    f"关系“{item.get('name')}”的逆关系源/目标对象类型没有反向对应"
                )
    for item in sections["actions"]:
        _assert_reference(
            item.get("entity"), generated_keys=generated["entities"],
            existing_ids=existing["entities"], label=f"操作“{item.get('name')}”的对象类型",
        )
    for item in sections["rules"]:
        _assert_reference(
            item.get("entity"), generated_keys=generated["entities"],
            existing_ids=existing["entities"], label=f"规则“{item.get('name')}”的对象类型",
        )
        entity_ref = item.get("entity") or {}
        identity = (
            str(entity_ref.get("id") or "")
            if entity_ref.get("kind") == "existing"
            else f"generated:{entity_ref.get('key')}"
        )
        available_fields = set(properties.get(identity, {}))
        unknown = _condition_fields(item.get("condition") or {}) - available_fields
        if unknown:
            raise PolicyViolation(f"规则“{item.get('name')}”引用了不存在的字段：{'、'.join(sorted(unknown))}")
        ambiguous_literals = _ambiguous_rule_literal_fields(
            item.get("condition") or {}, available_fields
        )
        if ambiguous_literals:
            raise PolicyViolation(
                f"规则“{item.get('name')}”的 value 与字段同名，必须显式使用 value_field"
            )
        for ref in item.get("trigger_actions") or []:
            _assert_reference(
                ref, generated_keys=generated["actions"], existing_ids=existing["actions"],
                label=f"规则“{item.get('name')}”的触发操作",
            )
    for item in sections["workflows"]:
        for node in item.get("nodes") or []:
            kind = str(node.get("type") or "")
            if kind not in {"action", "rule", "event"}:
                continue
            _assert_reference(
                (node.get("data") or {}).get("resource"),
                generated_keys=generated[f"{kind}s"], existing_ids=existing[f"{kind}s"],
                label=f"工作流“{item.get('name')}”的 {kind} 节点",
            )
        if item.get("trigger_type") == "event":
            _assert_reference(
                item.get("trigger_event"), generated_keys=generated["events"],
                existing_ids=existing["events"], label=f"工作流“{item.get('name')}”的触发事件",
            )
    mapping_entities: set[str] = set()
    for item in sections["mappings"]:
        _assert_reference(
            item.get("entity"), generated_keys=generated["entities"],
            existing_ids=existing["entities"], label=f"映射 {item.get('key')} 的对象类型",
        )
        identity = _reference_token(item.get("entity") or {})
        if identity in mapping_entities:
            raise PolicyViolation("同一对象类型在一次复合变更中只能有一条数据映射")
        mapping_entities.add(identity)
        if item.get("apply_plan") != _mapping_plan(scenario, sections["entities"], item):
            raise PolicyViolation("数据映射的应用计划已过期，请重新编译")
    relation_mapping_relations: set[str] = set()
    for item in sections["relation_mappings"]:
        _assert_reference(
            item.get("relation"), generated_keys=generated["relations"],
            existing_ids=existing["relations"],
            label=f"关系映射 {item.get('key')} 的关系",
        )
        _assert_reference(
            item.get("source_mapping"), generated_keys=generated["mappings"],
            existing_ids=existing["mappings"],
            label=f"关系映射 {item.get('key')} 的源对象映射",
        )
        _assert_reference(
            item.get("target_mapping"), generated_keys=generated["mappings"],
            existing_ids=existing["mappings"],
            label=f"关系映射 {item.get('key')} 的目标对象映射",
        )
        relation_identity = _reference_token(item.get("relation") or {})
        if relation_identity in relation_mapping_relations:
            raise PolicyViolation("同一关系在一次复合变更中只能有一条关系映射")
        relation_mapping_relations.add(relation_identity)
        expected_source, expected_target = _relation_endpoints_for_ref(
            scenario, sections["relations"], item.get("relation")
        )
        mapped_source = _mapping_entity_for_ref(
            scenario, sections["mappings"], item.get("source_mapping")
        )
        mapped_target = _mapping_entity_for_ref(
            scenario, sections["mappings"], item.get("target_mapping")
        )
        if (
            not expected_source or not expected_target
            or not mapped_source or not mapped_target
            or _reference_token(expected_source) != _reference_token(mapped_source)
            or _reference_token(expected_target) != _reference_token(mapped_target)
        ):
            raise PolicyViolation("关系映射的对象映射与关系端点不一致")
        mode = str(item.get("mode") or "")
        if mode not in {"source_fk", "target_fk", "join_table"}:
            raise PolicyViolation("关系映射模式不受支持")
        if mode in {"source_fk", "target_fk"}:
            if not str(item.get("foreign_key_column") or ""):
                raise PolicyViolation("关系映射缺少承载侧外键列")
            if any(
                item.get(field) not in (None, "", {})
                for field in (
                    "join_data_source", "join_table_name",
                    "source_key_column", "target_key_column",
                )
            ):
                raise PolicyViolation("外键关系映射包含中间表字段")
        else:
            join_source = item.get("join_data_source") or {}
            if (
                join_source.get("kind") != "existing"
                or not str(join_source.get("id") or "")
            ):
                raise PolicyViolation("关系映射的中间表数据源缺少已解析引用")
            if (
                item.get("foreign_key_column")
                or not item.get("join_table_name")
                or not item.get("source_key_column")
                or not item.get("target_key_column")
            ):
                raise PolicyViolation("中间表关系映射字段不完整或互相冲突")
        if item.get("apply_plan") != _relation_mapping_plan(
            scenario, sections["mappings"], item
        ):
            raise PolicyViolation("关系映射的应用计划已过期，请重新编译")


def _validate_event_feedback_graph(
    scenario: BusinessScenario,
    workflows: list[dict[str, Any]],
) -> None:
    event_ids = {str(event.id) for event in scenario.events}
    events_by_name: dict[str, list[str]] = defaultdict(list)
    for event in scenario.events:
        events_by_name[str(event.name)].append(str(event.id))

    def existing_trigger(config: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        event_id = str((config or {}).get("event_id") or "")
        if event_id:
            if event_id not in event_ids:
                raise PolicyViolation("已有事件工作流引用了不存在的触发事件")
            result.add(f"existing:{event_id}")
        event_name = str((config or {}).get("event_name") or "")
        if event_name:
            matches = events_by_name.get(event_name, [])
            if len(matches) != 1:
                raise PolicyViolation("已有事件工作流的触发事件名称不存在或不唯一")
            result.add(f"existing:{matches[0]}")
        return result

    graph: dict[str, set[str]] = defaultdict(set)
    for workflow in scenario.workflows:
        if workflow.trigger_type != "event":
            continue
        inputs = existing_trigger(workflow.trigger_config or {})
        outputs: set[str] = set()
        for node in workflow.nodes or []:
            if node.get("type") != "event":
                continue
            event_id = str((node.get("data") or {}).get("event_id") or "")
            if event_id not in event_ids:
                raise PolicyViolation("已有工作流发布了不存在的事件")
            outputs.add(f"existing:{event_id}")
        for source in inputs:
            graph[source].update(outputs)
    for workflow in workflows:
        if workflow.get("trigger_type") != "event":
            continue
        trigger_ref = workflow.get("trigger_event") or {}
        source = _reference_token(trigger_ref)
        for node in workflow.get("nodes") or []:
            if node.get("type") == "event":
                target = _reference_token(
                    (node.get("data") or {}).get("resource") or {}
                )
                if source and target == source:
                    workflow_label = str(
                        workflow.get("key") or workflow.get("name") or "未命名工作流"
                    )
                    node_id = str(node.get("id") or "未命名事件节点")
                    raise PolicyViolation(
                        f"工作流 {workflow_label} 的事件节点 {node_id} 又发布了本工作流的触发事件；"
                        "触发事件已由 trigger_config 表示，不能在流程中重复发布"
                    )
                graph[source].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(target) for target in graph.get(node, set())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    if any(visit(node) for node in list(graph)):
        raise PolicyViolation("事件触发工作流形成反馈环")


def _validate_existing_property_updates(
    db: Session,
    scenario: BusinessScenario,
    payload: dict[str, Any],
) -> None:
    """Recheck structural property updates against current persisted state."""
    by_id = {entity.id: entity for entity in scenario.entities}
    by_name = {entity.name: entity for entity in scenario.entities}
    structural_fields = (
        "data_type",
        "is_key",
        "is_title",
        "is_required",
        "is_enum",
        "enum_values",
        "constraints",
        "is_sensitive",
    )
    for item in payload.get("entities") or []:
        entity = by_id.get(item.get("existing_id")) or by_name.get(item.get("name"))
        marker_properties = [
            prop for prop in (item.get("properties") or [])
            if prop.get("_structural_update")
        ]
        if entity is None:
            if marker_properties:
                raise PolicyViolation("对象属性结构更新引用的已有对象类型已不存在，请重新编译")
            continue
        current_properties = {prop.name: prop for prop in entity.properties}
        for proposed in item.get("properties") or []:
            current = current_properties.get(proposed.get("name"))
            marker = proposed.get("_structural_update")
            if current is None:
                if marker:
                    raise PolicyViolation("对象属性结构更新引用的已有属性已不存在，请重新编译")
                continue
            current_definition = _property_definition(current)
            structural_changed = any(
                current_definition[field] != proposed.get(field)
                for field in structural_fields
            )
            if not structural_changed:
                if marker:
                    raise PolicyViolation("对象属性结构已变化，请重新编译")
                continue
            allowed_enum_upgrade = (
                marker == "enum_upgrade"
                and proposed.get("_operation") == "update"
                and _is_safe_empty_entity_enum_upgrade(db, current, proposed)
            )
            allowed_title_fallback = (
                marker == "title_fallback"
                and proposed.get("_operation") == "update"
                and _is_safe_title_fallback(entity, current, proposed)
            )
            if not (allowed_enum_upgrade or allowed_title_fallback):
                raise PolicyViolation("对象属性包含未经治理的结构变更，请重新编译")


def preflight_scenario_model(
    db: Session,
    scenario: BusinessScenario,
    payload: dict[str, Any],
    *,
    inspect_mappings: bool = True,
) -> None:
    """Validate the complete graph before any mutation is added to the session."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PolicyViolation("复合业务模型版本不受支持，请重新编译")
    blocking = [item for item in payload.get("unresolved") or [] if item.get("blocking", True)]
    if blocking:
        raise PolicyViolation(f"复合业务模型仍有 {len(blocking)} 个阻塞项，必须先消除后重新编译")
    _validate_coverage(payload)
    current_entities = list(db.execute(
        select(OntologyEntity)
        .where(OntologyEntity.scenario_id == scenario.id)
        .execution_options(populate_existing=True)
    ).scalars().all())
    for current_entity in current_entities:
        db.expire(current_entity, ["properties"])
    _validate_existing_property_updates(db, scenario, payload)
    properties = _entity_properties(scenario, payload)
    _validate_compiled_references(scenario, payload, properties)
    current_entity_names = {
        str(item.name): str(item.id) for item in current_entities
    }
    for item in payload.get("entities") or []:
        current_id = current_entity_names.get(str(item.get("name") or ""), "")
        expected_id = str(item.get("existing_id") or "")
        if expected_id:
            if current_id != expected_id:
                raise PolicyViolation("对象类型定义已变化，请重新编译")
        elif current_id:
            raise PolicyViolation("同名对象类型已在编译后新增，请重新编译")
    for section, model in (
        ("functions", FunctionDefinition),
        ("actions", OntologyAction),
        ("rules", OntologyRule),
        ("events", OntologyEvent),
        ("workflows", OntologyWorkflow),
    ):
        current_items = db.execute(
            select(model)
            .where(model.scenario_id == scenario.id)
            .execution_options(populate_existing=True)
        ).scalars().all()
        current_names = {str(item.name) for item in current_items}
        if any(str(item.get("name") or "") in current_names for item in payload.get(section) or []):
            raise PolicyViolation(f"{section} 中的同名资源已在编译后新增，请重新编译")
    if not any(
        item.get("operation") in {"add", "update", "delete"}
        for item in (payload.get("changes") or [])
    ):
        raise PolicyViolation("复合业务模型没有可应用的变更")
    for item in payload.get("functions") or []:
        _function_definition(item)
    for item in payload.get("actions") or []:
        if item.get("executor_type") != "unbound" or item.get("enabled") is not False:
            raise PolicyViolation("AI 生成的操作必须保持待绑定且停用")
        _object_schema(item.get("input_schema"))
    for item in payload.get("rules") or []:
        _normalize_rule_condition(item.get("condition"))
    for item in payload.get("events") or []:
        _object_schema(item.get("payload_schema"))
    for item in payload.get("workflows") or []:
        if item.get("status") != "draft" or item.get("enabled") is not False:
            raise PolicyViolation("AI 生成的工作流必须保持草稿且停用")
        workflow_service.validate_workflow_definition(item.get("nodes") or [], item.get("edges") or [])
        operations_service.validate_approval_nodes(item.get("nodes") or [], [])
        trigger_type = item.get("trigger_type")
        if trigger_type not in {"manual", "scheduled", "event"}:
            raise PolicyViolation("复合模型包含不受支持的工作流触发类型")
        allowed_config = {"max_attempts", "timeout_seconds", "retry_backoff_seconds"}
        if trigger_type == "scheduled":
            allowed_config.add("interval_seconds")
        if set((item.get("trigger_config") or {}).keys()) - allowed_config:
            raise PolicyViolation("复合模型包含未治理的工作流触发配置")
        trigger_config = dict(item.get("trigger_config") or {})
        if trigger_type == "event":
            trigger_config["event_id"] = _reference_token(item.get("trigger_event") or {})
        operations_service.validate_trigger_config(trigger_type, trigger_config)
    _validate_event_feedback_graph(scenario, payload.get("workflows") or [])

    existing_entities = {entity.id: entity for entity in scenario.entities}
    current_relations = list(db.execute(
        select(OntologyRelation)
        .where(OntologyRelation.scenario_id == scenario.id)
        .execution_options(populate_existing=True)
    ).scalars().all())
    existing_relations = {relation.name: relation for relation in current_relations}
    existing_relations_by_id = {
        relation.id: relation for relation in current_relations
    }
    for item in payload.get("relations") or []:
        operation = item.get("operation") or ("skip" if item.get("existing_id") else "add")
        current = (
            existing_relations_by_id.get(str(item.get("existing_id") or ""))
            or existing_relations.get(item.get("name"))
        )
        if not current:
            if operation != "add":
                raise PolicyViolation("复合模型引用的已有关系已不存在，请重新编译")
            continue
        if operation == "add" or str(item.get("existing_id") or "") != str(current.id):
            raise PolicyViolation(f"关系“{item.get('name')}”与已有定义冲突")
        source_ref = item.get("source") or {}
        target_ref = item.get("target") or {}
        source_id = str(source_ref.get("id") or "") if source_ref.get("kind") == "existing" else ""
        target_id = str(target_ref.get("id") or "") if target_ref.get("kind") == "existing" else ""
        # A relation to newly generated objects cannot be an identical replay
        # of an existing relation. Existing-name collisions therefore block.
        if (
            not source_id
            or not target_id
            or current.source_entity_id != source_id
            or current.target_entity_id != target_id
            or current.relation_type != item.get("relation_type")
        ):
            raise PolicyViolation(f"关系“{item.get('name')}”与已有定义冲突")
        proposed_constraints = ontology_service.normalize_relation_constraints(
            item.get("constraints") or {}, relation_type=item.get("relation_type")
        )
        ontology_service.validate_existing_relation_graph(
            db,
            current,
            constraints=proposed_constraints,
            relation_type=item.get("relation_type"),
        )
        current_constraints = ontology_service.normalize_relation_constraints(
            current.constraints or {}, relation_type=current.relation_type
        )
        current_inverse = str(current_constraints.pop("inverse_relation_id", "") or "")
        inverse = item.get("inverse_relation") or {}
        proposed_inverse = (
            str(inverse.get("id") or "")
            if inverse.get("kind") == "existing"
            else f"generated:{inverse.get('key')}" if inverse else ""
        )
        changed = (
            (current.description or "") != item.get("description", "")
            or (current.source_display_name or "")
            != item.get("source_display_name", "")
            or (current.target_display_name or "")
            != item.get("target_display_name", "")
            or (current.storage_kind or "none")
            != item.get("storage_kind", "none")
            or current_constraints != proposed_constraints
            or current_inverse != proposed_inverse
        )
        if operation == "skip" and changed:
            raise PolicyViolation(f"关系“{item.get('name')}”已变化，请重新编译")
        if operation == "update" and not changed:
            raise PolicyViolation(f"关系“{item.get('name')}”已无需更新，请重新编译")

    if not inspect_mappings:
        return
    for item in payload.get("mappings") or []:
        source_id = _resolved_id(item.get("data_source"), {})
        source = tenant_service.get_visible(db, DataSource, source_id)
        if not source or source.scenario_id not in (None, scenario.id) or source.type == "file_bucket":
            raise PolicyViolation("复合模型的数据映射引用了不可用的数据源")
        try:
            tables = datasource_service.list_tables(source)
        except Exception as exc:  # noqa: BLE001
            raise PolicyViolation("应用前无法重新读取数据源表结构") from exc
        table = next((entry for entry in tables if str(entry.get("name")) == item.get("table_name")), None)
        if not table:
            raise PolicyViolation("应用前数据映射引用的源表已不存在")
        available = {str(column.get("name")) for column in (table.get("columns") or [])}
        if any(column not in available for column in (item.get("column_map") or {}).values()):
            raise PolicyViolation("应用前数据映射引用的源字段已不存在")
        entity_ref = item.get("entity") or {}
        identity = entity_ref.get("id") if entity_ref.get("kind") == "existing" else f"generated:{entity_ref.get('key')}"
        entity_props = properties.get(str(identity), {})
        mapped = set((item.get("column_map") or {}).keys())
        unknown = mapped - set(entity_props)
        missing = {
            name for name, prop in entity_props.items()
            if (prop.get("is_key") or prop.get("is_required")) and name not in mapped
        }
        if unknown:
            raise PolicyViolation(f"数据映射引用了不存在的本体属性：{'、'.join(sorted(unknown))}")
        if missing:
            raise PolicyViolation(f"数据映射未覆盖主键或必填属性：{'、'.join(sorted(missing))}")

    def inspect_object_mapping(
        ref: dict[str, Any] | None,
        *,
        label: str,
    ) -> tuple[dict[str, Any], DataSource, set[str]]:
        physical = _mapping_physical_definition(
            scenario, payload.get("mappings") or [], ref
        )
        if not physical:
            raise PolicyViolation(f"{label}已不存在，请重新编译")
        source = tenant_service.get_visible(
            db, DataSource, str(physical.get("data_source_id") or "")
        )
        if (
            not source
            or source.scenario_id not in (None, scenario.id)
            or source.type == "file_bucket"
        ):
            raise PolicyViolation(f"{label}引用了不可用的数据源")
        try:
            tables = datasource_service.list_tables(source)
        except Exception as exc:  # noqa: BLE001
            raise PolicyViolation(f"应用前无法重新检查{label}的真实表结构") from exc
        table = next(
            (
                entry for entry in tables
                if str(entry.get("name") or "") == str(physical.get("table_name") or "")
            ),
            None,
        )
        if not table:
            raise PolicyViolation(f"{label}引用的真实表已不存在")
        available = {
            str(column.get("name") or "")
            for column in (table.get("columns") or [])
        }
        entity_ref = physical.get("entity") or {}
        identity = (
            str(entity_ref.get("id") or "")
            if entity_ref.get("kind") == "existing"
            else f"generated:{entity_ref.get('key')}"
        )
        entity_props = properties.get(identity, {})
        key_names = [
            name for name, prop in entity_props.items() if prop.get("is_key")
        ]
        if len(key_names) != 1:
            raise PolicyViolation(f"{label}的对象类型必须且只能有一个主键")
        key_column = str(
            (physical.get("column_map") or {}).get(key_names[0]) or ""
        )
        if not key_column or key_column not in available:
            raise PolicyViolation(f"{label}的主键属性未映射到真实源列")
        return physical, source, available

    for item in payload.get("relation_mappings") or []:
        source_physical, _source, source_columns = inspect_object_mapping(
            item.get("source_mapping"), label="关系映射的源对象映射"
        )
        target_physical, _target, target_columns = inspect_object_mapping(
            item.get("target_mapping"), label="关系映射的目标对象映射"
        )
        mode = str(item.get("mode") or "")
        if mode == "source_fk":
            if str(item.get("foreign_key_column") or "") not in source_columns:
                raise PolicyViolation("应用前关系映射的源对象外键列已不存在")
        elif mode == "target_fk":
            if str(item.get("foreign_key_column") or "") not in target_columns:
                raise PolicyViolation("应用前关系映射的目标对象外键列已不存在")
        else:
            join_source_id = str(
                (item.get("join_data_source") or {}).get("id") or ""
            )
            join_source = tenant_service.get_visible(db, DataSource, join_source_id)
            if (
                not join_source
                or join_source.scenario_id not in (None, scenario.id)
                or join_source.type == "file_bucket"
            ):
                raise PolicyViolation("应用前关系映射的中间表数据源不可用")
            try:
                join_tables = datasource_service.list_tables(join_source)
            except Exception as exc:  # noqa: BLE001
                raise PolicyViolation("应用前无法重新检查关系映射中间表") from exc
            join_table = next(
                (
                    entry for entry in join_tables
                    if str(entry.get("name") or "")
                    == str(item.get("join_table_name") or "")
                ),
                None,
            )
            if not join_table:
                raise PolicyViolation("应用前关系映射的中间表已不存在")
            join_columns = {
                str(column.get("name") or "")
                for column in (join_table.get("columns") or [])
            }
            if any(
                str(item.get(field) or "") not in join_columns
                for field in ("source_key_column", "target_key_column")
            ):
                raise PolicyViolation("应用前关系映射的中间表键列已不存在")

        plan = item.get("apply_plan") or {}
        relation_id = (
            str((item.get("relation") or {}).get("id") or "")
            if (item.get("relation") or {}).get("kind") == "existing"
            else ""
        )
        current = db.execute(
            select(RelationDataMapping)
            .where(
                RelationDataMapping.id == str(plan.get("existing_id") or "")
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if plan.get("mode") in {"update", "skip"}:
            if (
                not current
                or current.scenario_id != scenario.id
                or current.relation_id != relation_id
                or _relation_mapping_fingerprint(current) != plan.get("fingerprint")
            ):
                raise PolicyViolation("关系映射定义已变化，请重新编译")
        elif relation_id:
            raced = db.execute(
                select(RelationDataMapping).where(
                    RelationDataMapping.scenario_id == scenario.id,
                    RelationDataMapping.relation_id == relation_id,
                )
            ).scalar_one_or_none()
            if raced:
                raise PolicyViolation("关系已新增数据映射，请重新编译")

        source_mapping_id = _planned_mapping_id(
            payload.get("mappings") or [], item.get("source_mapping")
        )
        target_mapping_id = _planned_mapping_id(
            payload.get("mappings") or [], item.get("target_mapping")
        )
        # When every prerequisite already has a persisted identity, call the
        # same formal preflight as the CRUD API before entering the savepoint.
        if relation_id and source_mapping_id and target_mapping_id:
            formal_payload = RelationDataMappingIn(
                relation_id=relation_id,
                source_mapping_id=source_mapping_id,
                target_mapping_id=target_mapping_id,
                mode=mode,
                foreign_key_column=str(item.get("foreign_key_column") or ""),
                join_data_source_id=(
                    str((item.get("join_data_source") or {}).get("id") or "")
                    if mode == "join_table" else ""
                ),
                join_table_name=str(item.get("join_table_name") or ""),
                source_key_column=str(item.get("source_key_column") or ""),
                target_key_column=str(item.get("target_key_column") or ""),
            )
            try:
                ontology_service.validate_relation_data_mapping(
                    db, scenario, formal_payload
                )
            except Exception as exc:  # noqa: BLE001
                raise PolicyViolation(f"关系映射正式预检未通过：{exc}") from exc


def apply_scenario_model(
    db: Session,
    scenario: BusinessScenario,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply in the caller transaction and fail by rolling the whole unit back."""
    preflight_scenario_model(db, scenario, payload, inspect_mappings=True)
    try:
        return _apply_scenario_model_mutations(db, scenario, payload)
    except Exception:
        # The compiler is an all-or-nothing application boundary.  A nested
        # SAVEPOINT is insufficient on SQLite when it is the first write in a
        # deferred transaction (releasing it can make changes durable).  A
        # full caller-transaction rollback is the only backend-neutral zero-
        # write guarantee after a late formal relation-mapping preflight fails.
        db.rollback()
        raise


def _apply_scenario_model_mutations(
    db: Session,
    scenario: BusinessScenario,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Mutate only after public preflight inside the caller transaction."""
    created: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)
    entity_by_id = {entity.id: entity for entity in scenario.entities}
    entity_by_name = {entity.name: entity for entity in scenario.entities}
    for item in payload.get("entities") or []:
        existing_id = str(item.get("existing_id") or "")
        entity = entity_by_id.get(existing_id) if existing_id else None
        if existing_id and (entity is None or entity.name != item["name"]):
            raise PolicyViolation("对象类型定义已变化，请重新编译")
        if not existing_id and entity_by_name.get(item["name"]) is not None:
            raise PolicyViolation("同名对象类型已存在，不能把新增静默改为更新")
        if entity is None:
            try:
                entity_api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyEntity,
                    scope_field="scenario_id",
                    scope_id=scenario.id,
                    value=item.get("api_name"),
                    display_name=item["name"],
                    prefix="entity",
                    stable_key=item["key"],
                )
            except ValueError as exc:
                raise PolicyViolation(str(exc)) from exc
            entity = OntologyEntity(
                scenario_id=scenario.id,
                name=item["name"],
                api_name=entity_api_name,
                namespace=scenario.namespace or "default",
                description=item.get("description", ""),
                is_abstract=bool(item.get("is_abstract", False)),
                state_property=item.get("state_property", ""),
            )
            db.add(entity)
            db.flush()
            entity_by_id[entity.id] = entity
            entity_by_name[entity.name] = entity
            counts["entities_added"] += 1
        else:
            try:
                entity.api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyEntity,
                    scope_field="scenario_id",
                    scope_id=scenario.id,
                    value=item.get("api_name"),
                    display_name=entity.name,
                    prefix="entity",
                    stable_key=entity.id,
                    current=entity.api_name,
                    resource_id=entity.id,
                )
            except ValueError as exc:
                raise PolicyViolation(str(exc)) from exc
            if item.get("operation") == "update":
                entity.description = item.get("description", "")
                entity.is_abstract = bool(item.get("is_abstract", False))
                entity.state_property = item.get("state_property", "")
            counts["entities_extended"] += 1
        created[item["key"]] = entity.id
        existing_props = {prop.name: prop for prop in entity.properties}
        for prop in item.get("properties") or []:
            current = existing_props.get(prop["name"])
            if current is not None:
                try:
                    current.api_name = ontology_service.allocate_resource_api_name(
                        db,
                        OntologyProperty,
                        scope_field="entity_id",
                        scope_id=entity.id,
                        value=prop.get("api_name"),
                        display_name=current.name,
                        prefix="property",
                        stable_key=current.id,
                        current=current.api_name,
                        resource_id=current.id,
                    )
                except ValueError as exc:
                    raise PolicyViolation(str(exc)) from exc
                if prop.get("_operation") == "update":
                    current.description = prop.get("description", "")
                    current.default_value = prop.get("default_value", "")
                    if prop.get("_structural_update") == "enum_upgrade":
                        current.is_enum = True
                        current.enum_values = list(prop.get("enum_values") or [])
                    if prop.get("_structural_update") == "title_fallback":
                        current.is_title = True
                    counts["properties_updated"] += 1
                else:
                    counts["properties_skipped"] += 1
                continue
            if prop.get("_operation") == "skip":
                counts["properties_skipped"] += 1
                continue
            try:
                property_api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyProperty,
                    scope_field="entity_id",
                    scope_id=entity.id,
                    value=prop.get("api_name"),
                    display_name=prop["name"],
                    prefix="property",
                    stable_key=f"{item['key']}.{prop['name']}",
                )
            except ValueError as exc:
                raise PolicyViolation(str(exc)) from exc
            db.add(OntologyProperty(
                entity_id=entity.id,
                name=prop["name"],
                api_name=property_api_name,
                data_type=prop.get("data_type", "string"),
                description=prop.get("description", ""),
                is_key=bool(prop.get("is_key", False)),
                is_title=bool(prop.get("is_title", False)),
                is_required=bool(prop.get("is_required", False)),
                is_enum=bool(prop.get("is_enum", False)),
                enum_values=prop.get("enum_values") or [],
                default_value=prop.get("default_value", ""),
                constraints=prop.get("constraints") or {},
                is_sensitive=bool(prop.get("is_sensitive", False)),
            ))
            counts["properties_added"] += 1
    db.flush()
    # Properties are added by foreign-key id so an entity collection that was
    # read earlier in this transaction may still be cached as empty.  The
    # formal relation-mapping validator deliberately reads the ORM collection;
    # expire it after the flush so preflight sees the persisted key/title set.
    for entity in entity_by_id.values():
        db.expire(entity, ["properties"])

    existing_relations_by_id = {relation.id: relation for relation in scenario.relations}
    existing_relations_by_name = {relation.name: relation for relation in scenario.relations}
    applied_relations: dict[str, OntologyRelation] = {}
    for item in payload.get("relations") or []:
        existing_id = str(item.get("existing_id") or "")
        relation = existing_relations_by_id.get(existing_id) if existing_id else None
        if existing_id and (relation is None or relation.name != item["name"]):
            raise PolicyViolation("关系定义已变化，请重新编译")
        if not existing_id and existing_relations_by_name.get(item["name"]) is not None:
            raise PolicyViolation("同名关系已存在，不能把新增静默改为更新")
        if relation is not None:
            try:
                relation.api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyRelation,
                    scope_field="scenario_id",
                    scope_id=scenario.id,
                    value=item.get("api_name"),
                    display_name=relation.name,
                    prefix="relation",
                    stable_key=relation.id,
                    current=relation.api_name,
                    resource_id=relation.id,
                )
                navigation = ontology_service.normalize_relation_navigation(
                    relation_name=relation.name,
                    relation_api_name=relation.api_name,
                    source_display_name=item.get("source_display_name"),
                    source_api_name=item.get("source_api_name"),
                    target_display_name=item.get("target_display_name"),
                    target_api_name=item.get("target_api_name"),
                    current=relation,
                )
            except ValueError as exc:
                raise PolicyViolation(str(exc)) from exc
            for field, value in navigation.items():
                setattr(relation, field, value)
            relation.storage_kind = ontology_service.normalize_relation_storage_kind(
                item.get("storage_kind"), current=relation.storage_kind
            )
            created[item["key"]] = relation.id
            applied_relations[item["key"]] = relation
            if item.get("operation") == "update":
                relation.description = item.get("description", "")
                relation.constraints = ontology_service.normalize_relation_constraints(
                    item.get("constraints") or {},
                    relation_type=item.get("relation_type", "N:M"),
                )
                counts["relations_updated"] += 1
            else:
                counts["relations_skipped"] += 1
            continue
        try:
            relation_api_name = ontology_service.allocate_resource_api_name(
                db,
                OntologyRelation,
                scope_field="scenario_id",
                scope_id=scenario.id,
                value=item.get("api_name"),
                display_name=item["name"],
                prefix="relation",
                stable_key=item["key"],
            )
            navigation = ontology_service.normalize_relation_navigation(
                relation_name=item["name"],
                relation_api_name=relation_api_name,
                source_display_name=item.get("source_display_name"),
                source_api_name=item.get("source_api_name"),
                target_display_name=item.get("target_display_name"),
                target_api_name=item.get("target_api_name"),
            )
        except ValueError as exc:
            raise PolicyViolation(str(exc)) from exc
        relation = OntologyRelation(
            scenario_id=scenario.id,
            name=item["name"],
            api_name=relation_api_name,
            namespace=scenario.namespace or "default",
            source_entity_id=_resolved_id(item.get("source"), created),
            target_entity_id=_resolved_id(item.get("target"), created),
            **navigation,
            storage_kind=ontology_service.normalize_relation_storage_kind(
                item.get("storage_kind")
            ),
            relation_type=item.get("relation_type", "1:N"),
            description=item.get("description", ""),
            constraints=ontology_service.normalize_relation_constraints(
                item.get("constraints") or {},
                relation_type=item.get("relation_type", "N:M"),
            ),
        )
        db.add(relation)
        applied_relations[item["key"]] = relation
        counts["relations_added"] += 1
    db.flush()
    for item in payload.get("relations") or []:
        relation = applied_relations[item["key"]]
        created[item["key"]] = relation.id
    for item in payload.get("relations") or []:
        relation = applied_relations[item["key"]]
        constraints = ontology_service.normalize_relation_constraints(
            item.get("constraints") or {},
            relation_type=item.get("relation_type", "N:M"),
        )
        inverse_ref = item.get("inverse_relation")
        if inverse_ref:
            constraints["inverse_relation_id"] = _resolved_id(inverse_ref, created)
        constraints = ontology_service.normalize_relation_constraints(
            constraints, relation_type=item.get("relation_type", "N:M")
        )
        ontology_service.validate_inverse_relation(
            db,
            scenario_id=scenario.id,
            relation_id=relation.id,
            source_entity_id=relation.source_entity_id,
            target_entity_id=relation.target_entity_id,
            constraints=constraints,
        )
        relation.constraints = constraints

    for item in payload.get("functions") or []:
        definition = _function_definition(item)
        function = FunctionDefinition(scenario_id=scenario.id, **definition)
        db.add(function)
        db.flush()
        created[item["key"]] = function.id
        counts["functions_added"] += 1

    for item in payload.get("actions") or []:
        action = OntologyAction(
            scenario_id=scenario.id,
            entity_id=_resolved_id(item.get("entity"), created),
            name=item["name"],
            description=item.get("description", ""),
            input_schema=item.get("input_schema") or {},
            executor_type="unbound",
            executor_config={},
            precondition=item.get("precondition", ""),
            postcondition=item.get("postcondition", ""),
            enabled=False,
            requires_confirmation=True,
            idempotency_required=True,
            permission_scope="scenario",
            access_scope="tenant",
        )
        db.add(action)
        db.flush()
        created[item["key"]] = action.id
        counts["actions_added"] += 1

    for item in payload.get("rules") or []:
        rule = OntologyRule(
            scenario_id=scenario.id,
            entity_id=_resolved_id(item.get("entity"), created) if item.get("entity") else None,
            name=item["name"],
            description=item.get("description", ""),
            condition=item.get("condition") or {},
            action_on_match=item.get("action_on_match", ""),
            trigger_action_ids=[_resolved_id(ref, created) for ref in item.get("trigger_actions") or []],
            severity=item.get("severity", "info"),
            enabled=False,
        )
        db.add(rule)
        db.flush()
        created[item["key"]] = rule.id
        counts["rules_added"] += 1

    for item in payload.get("events") or []:
        event = OntologyEvent(
            scenario_id=scenario.id,
            name=item["name"],
            description=item.get("description", ""),
            payload_schema=item.get("payload_schema") or {},
            trigger_source=item.get("trigger_source", ""),
            enabled=False,
        )
        db.add(event)
        db.flush()
        created[item["key"]] = event.id
        counts["events_added"] += 1

    for item in payload.get("workflows") or []:
        nodes: list[dict[str, Any]] = []
        id_keys = {"action": "action_id", "rule": "rule_id", "event": "event_id"}
        for raw_node in item.get("nodes") or []:
            node = dict(raw_node)
            data = dict(node.get("data") or {})
            if node.get("type") in id_keys:
                data[id_keys[node["type"]]] = _resolved_id(data.pop("resource", None), created)
            node["data"] = data
            nodes.append(node)
        workflow_service.validate_workflow_definition(nodes, item.get("edges") or [])
        workflow_service.validate_workflow_references(db, scenario.id, steps=[], nodes=nodes)
        operations_service.validate_approval_nodes(nodes, [])
        trigger_config = dict(item.get("trigger_config") or {})
        if item.get("trigger_type") == "event":
            trigger_config["event_id"] = _resolved_id(item.get("trigger_event"), created)
        operations_service.validate_trigger_config(item.get("trigger_type", "manual"), trigger_config)
        operations_service.validate_event_feedback_loops(
            db,
            scenario.id,
            trigger_type=item.get("trigger_type", "manual"),
            trigger_config=trigger_config,
            nodes=nodes,
            steps=[],
        )
        workflow = OntologyWorkflow(
            scenario_id=scenario.id,
            name=item["name"],
            description=item.get("description", ""),
            trigger_type=item.get("trigger_type", "manual"),
            trigger_config=trigger_config,
            steps=[],
            nodes=nodes,
            edges=item.get("edges") or [],
            status="draft",
            enabled=False,
            access_scope="tenant",
        )
        db.add(workflow)
        db.flush()
        created[item["key"]] = workflow.id
        counts["workflows_added"] += 1

    applied_mappings: dict[str, DataMapping] = {}
    pending_mapping_deletes: dict[str, DataMapping] = {}
    for item in payload.get("mappings") or []:
        entity_id = _resolved_id(item.get("entity"), created)
        source_id = _resolved_id(item.get("data_source"), created)
        source = tenant_service.get_visible(db, DataSource, source_id)
        if not source:
            raise PolicyViolation("数据映射引用的数据源已不可访问")
        existing = list(db.execute(
            select(DataMapping).where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.entity_id == entity_id,
            ).order_by(DataMapping.created_at, DataMapping.id)
        ).scalars().all())
        plan = item.get("apply_plan") or {}
        if plan.get("mode") in {"update", "skip"}:
            mapping = next(
                (candidate for candidate in existing if candidate.id == plan.get("canonical_id")),
                None,
            )
            if mapping is None:
                raise PolicyViolation("数据映射的规范身份已变化，请重新编译")
            if plan.get("mode") == "update":
                before = mapping_refresh_service.mapping_fingerprint(mapping)
                mapping.column_map = item["column_map"]
                if mapping_refresh_service.mapping_fingerprint(mapping) != before:
                    mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                        db, mapping.id, reason="复合业务文档确认的数据映射已更新，请重新提交刷新"
                    )
                    mapping_refresh_service.invalidate_mapping_runtime_state(mapping)
                counts["mappings_updated"] += 1
            else:
                counts["mappings_skipped"] += 1
        else:
            if plan.get("mode") == "add" and existing:
                raise PolicyViolation("对象类型已新增数据映射，请重新编译")
            metadata = connector_service.runtime_binding_metadata(
                "data_source",
                {"name": source.name, "type": source.type},
                path=f"scenario:{scenario.id}:entity:{entity_id}",
            )
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=entity_id,
                data_source_id=source_id,
                data_source_binding_key=metadata["binding_key"],
                data_source_binding_ref=connector_service.with_required_capabilities(
                    metadata["reference"], "sql_read"
                ),
                table_name=item["table_name"],
                column_map=item["column_map"],
                transform_rules={},
            )
            db.add(mapping)
            counts["mappings_added"] += 1
        if not str(mapping.data_source_binding_key or ""):
            metadata = connector_service.runtime_binding_metadata(
                "data_source",
                {"name": source.name, "type": source.type},
                path=f"scenario:{scenario.id}:entity:{entity_id}",
            )
            mapping.data_source_binding_key = str(metadata["binding_key"])
            mapping.data_source_binding_ref = connector_service.with_required_capabilities(
                metadata["reference"], "sql_read"
            )
            counts["mapping_bindings_filled"] += 1
        applied_mappings[item["key"]] = mapping
        delete_ids = set(str(value) for value in (plan.get("delete_ids") or []))
        for duplicate in existing:
            if duplicate.id not in delete_ids:
                continue
            pending_mapping_deletes[duplicate.id] = duplicate
    db.flush()
    for key, mapping in applied_mappings.items():
        created[key] = str(mapping.id)

    for item in payload.get("relation_mappings") or []:
        mode = str(item.get("mode") or "")
        formal_payload = RelationDataMappingIn(
            relation_id=_resolved_id(item.get("relation"), created),
            source_mapping_id=_resolved_id(item.get("source_mapping"), created),
            target_mapping_id=_resolved_id(item.get("target_mapping"), created),
            mode=mode,
            foreign_key_column=str(item.get("foreign_key_column") or ""),
            join_data_source_id=(
                _resolved_id(item.get("join_data_source"), created)
                if mode == "join_table" else ""
            ),
            join_table_name=str(item.get("join_table_name") or ""),
            source_key_column=str(item.get("source_key_column") or ""),
            target_key_column=str(item.get("target_key_column") or ""),
        )
        try:
            derived, _preview = ontology_service.validate_relation_data_mapping(
                db, scenario, formal_payload
            )
        except Exception as exc:  # noqa: BLE001
            raise PolicyViolation(f"关系映射正式预检未通过：{exc}") from exc
        plan = item.get("apply_plan") or {}
        existing_id = str(plan.get("existing_id") or "")
        relation_mapping = db.get(RelationDataMapping, existing_id) if existing_id else None
        if existing_id:
            if (
                relation_mapping is None
                or relation_mapping.scenario_id != scenario.id
                or _relation_mapping_fingerprint(relation_mapping)
                != str(plan.get("fingerprint") or "")
            ):
                raise PolicyViolation("关系映射定义已变化，请重新编译")
        else:
            raced = db.execute(
                select(RelationDataMapping).where(
                    RelationDataMapping.scenario_id == scenario.id,
                    RelationDataMapping.relation_id == derived["relation_id"],
                )
            ).scalar_one_or_none()
            if raced:
                raise PolicyViolation("关系已存在数据映射，不能把新增静默改为更新")
        if relation_mapping is None:
            relation_mapping = RelationDataMapping(
                scenario_id=scenario.id,
                status="ready",
                last_error="",
                last_checked_at=datetime.now(timezone.utc),
                **derived,
            )
            db.add(relation_mapping)
            for mapping_id in sorted({
                str(derived["source_mapping_id"]),
                str(derived["target_mapping_id"]),
            }):
                mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                    db,
                    mapping_id,
                    reason="复合业务文档已创建关系映射，请重新提交对象映射刷新",
                )
            counts["relation_mappings_added"] += 1
        else:
            previous_endpoint_ids = {
                str(relation_mapping.source_mapping_id),
                str(relation_mapping.target_mapping_id),
            }
            changed = any(
                getattr(relation_mapping, field) != value
                for field, value in derived.items()
            )
            if plan.get("mode") == "skip" and changed:
                raise PolicyViolation("关系映射已变化，请重新编译")
            if plan.get("mode") == "update" and not changed:
                raise PolicyViolation("关系映射已无需更新，请重新编译")
            if changed:
                ontology_service.purge_relation_mapping_instances(
                    db, relation_mapping.id
                )
                for field, value in derived.items():
                    setattr(relation_mapping, field, value)
                relation_mapping.last_refreshed_at = None
                relation_mapping.last_link_count = 0
                for mapping_id in sorted(
                    previous_endpoint_ids
                    | {
                        str(relation_mapping.source_mapping_id),
                        str(relation_mapping.target_mapping_id),
                    }
                ):
                    mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                        db,
                        mapping_id,
                        reason="复合业务文档确认的关系映射已更新，请重新提交对象映射刷新",
                    )
                counts["relation_mappings_updated"] += 1
            else:
                counts["relation_mappings_skipped"] += 1
            relation_mapping.status = "ready"
            relation_mapping.last_error = ""
            relation_mapping.last_checked_at = datetime.now(timezone.utc)
        db.flush()
        created[item["key"]] = str(relation_mapping.id)

    db.flush()
    for mapping_id, duplicate in pending_mapping_deletes.items():
        referenced = db.execute(
            select(RelationDataMapping.id).where(
                or_(
                    RelationDataMapping.source_mapping_id == mapping_id,
                    RelationDataMapping.target_mapping_id == mapping_id,
                )
            ).limit(1)
        ).scalar_one_or_none()
        if referenced:
            raise PolicyViolation("旧对象映射仍被关系映射引用，不能删除")
        mapping_refresh_service.cancel_active_mapping_refresh_jobs(
            db,
            mapping_id,
            reason="复合业务文档确认的新映射身份已替换旧定义",
        )
        db.delete(duplicate)
        counts["mappings_deleted"] += 1
    db.flush()
    return {
        "kind": "scenario_model",
        "schema_version": SCHEMA_VERSION,
        "counts": dict(counts),
        "source_documents": len(payload.get("source_manifest") or []),
        "source_paragraphs": int(payload.get("source_paragraph_count") or 0),
    }
