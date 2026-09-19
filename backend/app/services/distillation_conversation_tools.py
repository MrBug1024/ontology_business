"""Static read-only investigation tools and explicit human stopping points."""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from ..distillation_conversation_schemas import ClarificationQuestion, MCPMaterialRead, WebsiteObservation
from ..distillation_sample_schemas import DatabaseSampleArguments, CompareSamplesArguments
from ..distillation_schemas import ClosedModel, DistillationDocument, Evidence, InvestigationSource
from . import distillation_analysis_service, distillation_target_service
from .distillation_proposal_service import normalize_proposal
from . import distillation_attachment_service, distillation_library_service
from . import distillation_resource_service
from . import distillation_browser_tools
from .distillation_skill_service import discovery_skill
from .distillation_tool_schema import portable_schema


class EmptyArguments(ClosedModel):
    pass


class EvidenceArguments(ClosedModel):
    evidence_key: str = Field(min_length=1, max_length=64)


class TargetArguments(ClosedModel):
    target_key: str = Field(min_length=1, max_length=64)
    page_path: str = Field(min_length=1, max_length=2048)


class ReviewArguments(ClosedModel):
    focus: Literal["business_value", "result_reversal", "process_challenge"]


class PageArguments(ClosedModel):
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=20, ge=1, le=20)


class LibraryFilesArguments(PageArguments):
    data_source_id: str = Field(min_length=1, max_length=32)


class LibraryReadArguments(ClosedModel):
    data_source_id: str = Field(min_length=1, max_length=32)
    bucket_file_id: str | None = Field(default=None, min_length=1, max_length=32)


class AttachmentArguments(ClosedModel):
    attachment_id: str = Field(min_length=1, max_length=32)
    offset: int = Field(default=0, ge=0, le=200_000)
    limit: int = Field(default=12_000, ge=1, le=12_000)


class AskArguments(ClosedModel):
    message: str = Field(min_length=1, max_length=4000)
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("澄清问题标识不能重复")
        return self


class ProposalArguments(ClosedModel):
    message: str = Field(min_length=1, max_length=4000)
    document: DistillationDocument


_TOOLS = {
    "list_evidence": (EmptyArguments, "查看调查来源", "列出当前证据、对话中仍可用的临时附件和目标系统；还可用list_library_sources查找授权资料库。"),
    "read_evidence": (EvidenceArguments, "阅读选定资料", "按已有 evidence_key 读取有界文件内容或数据库结构。未执行业务行查询和记录匹配。"),
    "read_current_document": (EmptyArguments, "阅读当前阶段结论", "读取人工已保存的阶段结论，不写入任何内容。"),
    "review_business": (ReviewArguments, "应用业务审查方法", "调用受信业务审查技能：价值追问、从结果逆向证据链、挑战无效流程。返回检查步骤和缺口，不证明事实。"),
    "read_target_system": (TargetArguments, "调查允许的系统页面", "按已配置 target_key 和精确允许 page_path 只读 GET。受保护系统仅使用专家另行授权的专用凭据；不能表单登录、点击、提交或读取其它地址。"),
    "list_library_sources": (PageArguments, "查找授权资料库", "列出当前场景及授权共享资料库，无须重复上传。使用返回的ID读取，不能跨场景猜ID。"),
    "list_library_files": (LibraryFilesArguments, "查看库内文件", "分页列出授权资料库中的文件，便于选择需要调查的具体材料。"),
    "read_library_source": (LibraryReadArguments, "读取资料库材料", "读取授权资料库有界原文。指定 bucket_file_id 的 Excel 直接读取真实单元格样本；数据库返回结构。更细的字段、行数和筛选请用 read_database_sample。冻结实际来源身份。"),
    "read_attachment": (AttachmentArguments, "阅读对话临时附件", "读取本轮固定的当前对话附件节选。附件24小时后失效；没有可用ID时不能声称读过原文。"),
    "ask_human": (AskArguments, "等待人工澄清", "遇到业务决策、证据缺口或匹配歧义时提出最多3个问题并结束本轮等待人回答。不能自己回答。"),
    "propose_document": (ProposalArguments, "提出阶段建议", "提交完整待人工采用的阶段建议，并结束本轮。不能修改人工证据、决策、调查授权或自动发布。"),
}
_TOOLS.update(distillation_browser_tools.TOOLS)
_TOOLS.update({
    "read_database_sample": (DatabaseSampleArguments, "读取历史数据样本", "读取真实业务行。数据库省略 table_key 可发现表字段；Excel 文件必须传 bucket_file_id，单工作表直接返回样本，多表返回目录供选择。可指定返回的字段引用和筛选条件。只读、有界，不接受 SQL、物理路径或凭据。"),
    "compare_database_samples": (CompareSamplesArguments, "核对样本关联", "对实际读取的两份样本执行单键/复合键精确匹配，返回重复键、多匹配、未匹配和排除记录。只证明该样本，不能代表全量唯一性或业务因果。"),
    "record_human_statement": (EmptyArguments, "引用专家陈述", "将本轮真实用户消息登记为可追溯的访谈证据，返回 evidence_key。只表示专家陈述，尚未独立核实；不自动登记所有消息。"),
})

ALWAYS_AVAILABLE_TOOL_KEYS = frozenset({"ask_human", "propose_document"})


def selectable_tool_keys() -> tuple[str, ...]:
    return tuple(name for name in _TOOLS if name not in ALWAYS_AVAILABLE_TOOL_KEYS)


def normalize_selected_tool_keys(tool_keys: Collection[str] | None) -> tuple[str, ...] | None:
    """Return stable selectable keys while reserving human stopping points."""
    if tool_keys is None:
        return None
    keys = tuple(tool_keys)
    if len(set(keys)) != len(keys) or set(keys).difference(selectable_tool_keys()):
        raise ValueError("所选调查工具不可用")
    return tuple(name for name in selectable_tool_keys() if name in keys)


def effective_tool_keys(tool_keys: Collection[str] | None) -> tuple[str, ...]:
    selected = normalize_selected_tool_keys(tool_keys)
    requested = set(selectable_tool_keys() if selected is None else selected)
    requested.update(ALWAYS_AVAILABLE_TOOL_KEYS)
    return tuple(name for name in _TOOLS if name in requested)


def catalog() -> list[dict[str, object]]:
    return [
        {
            "key": name,
            "title": title,
            "description": description,
            "selectable": name not in ALWAYS_AVAILABLE_TOOL_KEYS,
            "always_available": name in ALWAYS_AVAILABLE_TOOL_KEYS,
        }
        for name, (_schema, title, description) in _TOOLS.items()
    ]


def definitions(allowed_tool_keys: Collection[str] | None = None) -> list[dict]:
    allowed = effective_tool_keys(None) if allowed_tool_keys is None else tuple(allowed_tool_keys)
    unknown = set(allowed).difference(_TOOLS).difference(distillation_resource_service.TOOL_KEYS)
    if unknown:
        raise ValueError("不支持的调查工具")
    return [{"type": "function", "function": {"name": name, "description": description,
        "parameters": portable_schema(schema.model_json_schema())}} for name, (schema, _title, description) in _TOOLS.items()
        if name in allowed]


def tool_title(name: str) -> str:
    if name in distillation_resource_service.TOOL_KEYS:
        return distillation_resource_service.tool_title(name)
    if name not in _TOOLS:
        raise ValueError("不支持的调查工具")
    return _TOOLS[name][1]


def require_allowed(name: str, allowed_tool_keys: Collection[str]) -> None:
    if name not in allowed_tool_keys:
        raise ValueError("本轮未选择该调查工具")


@dataclass(frozen=True)
class ToolResult:
    content: dict
    summary: str
    message: str = ""
    questions: list[ClarificationQuestion] | None = None
    proposal: DistillationDocument | None = None
    source: WebsiteObservation | None = None
    library_read: distillation_library_service.LibraryRead | None = None
    resource_keys: dict[str, list[str]] | None = None
    mcp_read: MCPMaterialRead | None = None
    interview_source: Evidence | None = None


_REVIEW_METHODS = {
    "business_value": ["区分使用者、受益者、付费者和决策者", "追问痛点造成的实际损失和不解决的后果",
        "定义可观察结果及成功指标", "检验收集、存档、报表是否足以消除痛点", "允许建议停止、调整或拒绝无法产生价值的要求，并请人决定"],
    "result_reversal": ["选择真实历史结果与对应输入/知识依据", "从结果字段反向追踪生成规则、负责人和流程节点",
        "检查单键/复合键与多行匹配歧义", "把缺少过程记录和不可重算结果列成待问问题",
        "区分可验证映射、推断与假设；没有执行匹配的工具时不得声称匹配已验证"],
    "process_challenge": ["逐节点列出负责人、输入、动作、输出和受益者", "检查节点是否改变业务状态或降低风险",
        "比较删除、合并、替代与保留的结果和代价", "保留法定/控制要求的不确定性并向人求证",
        "分别画现状与目标流程，给出衡量改进的证据计划"],
}


def observations_from_steps(turn_id: str, steps: list[dict]) -> list[Evidence]:
    evidence = []
    for step in steps:
        source = step.get("source")
        if step["status"] != "succeeded" or not source or source.get("status") != "observed":
            continue
        evidence.append(Evidence(key="web_" + step["id"][:20], title=source["title"] or "目标系统页面观察",
            kind="system_export", role="reference", summary="实际只读获取的页面/接口观察；正文已保存在调查记录中，尚待专家核对业务含义。",
            coverage=source["url"], limitations="；".join(source["limitations"]),
            investigation_source=InvestigationSource(turn_id=turn_id, step_id=step["id"],
                content_sha256=source["content_sha256"])))
    return evidence


def execute(db: Session, name: str, arguments: dict, document: DistillationDocument,
            scenario_id: str | None, *, observations: list[Evidence], turn=None,
            allowed_tool_keys: Collection[str] | None = None, browser=None) -> ToolResult:
    tool_title(name)
    if allowed_tool_keys is not None:
        require_allowed(name, allowed_tool_keys)
    if name in distillation_resource_service.TOOL_KEYS:
        return distillation_resource_service.execute(db, name, arguments, turn)
    if name == "propose_document" and isinstance(arguments.get("document"), dict):
        # Evidence receipts and authorization are server-owned. Restore them
        # before reference validation so the model only needs to cite keys.
        arguments = {**arguments, "document": normalize_proposal(arguments["document"], document,
            observations=observations).model_dump()}
    payload = _TOOLS[name][0].model_validate(arguments)
    if name in distillation_browser_tools.TOOLS:
        return distillation_browser_tools.execute(db, name, payload, document, turn, browser)
    if isinstance(payload, (DatabaseSampleArguments, CompareSamplesArguments)):
        from . import distillation_sample_service

        if isinstance(payload, CompareSamplesArguments):
            content = distillation_sample_service.compare_samples(db, scenario_id, payload, [*document.evidence, *observations])
            return ToolResult(content, f"实际核对样本：{len(content['unique_matches'])} 条唯一匹配、{len(content['ambiguous_matches'])} 条多匹配、{len(content['unmatched_left_rows'])} 条未匹配；仅代表本次样本。")
        if turn is None or len(turn.context.get("library_reads", [])) >= distillation_library_service.MAX_LIBRARY_READS:
            raise ValueError("本轮资料读取已达上限，请在下一轮继续")
        try:
            reading = distillation_sample_service.read_sample(db, scenario_id, payload)
        except distillation_sample_service.SampleSelectionError as exc:
            return ToolResult({"status": "blocked", "reason": str(exc)}, "请选取具体文件后读取样本。")
        except ValueError:
            return ToolResult({"status": "blocked", "reason": "资料样本不可读取、引用已变化或超出边界；请重新发现表/字段，缩小样本或向专家核对连接权限。"}, "样本读取未完成，请核对资料范围。")
        return ToolResult(reading.content, "已实际读取资料表结构。" if reading.content.get("kind") == "database_catalog"
            else f"已实际读取 {len(reading.content.get('rows', []))} 行有界业务样本。", library_read=reading)
    if name == "record_human_statement":
        from .distillation_interview_service import evidence

        if turn is None or not turn.message:
            raise ValueError("本轮没有可引用的专家陈述")
        source = evidence(turn)
        return ToolResult({"evidence": source.model_dump()}, "已引用本轮专家陈述，尚未独立核实。", interview_source=source)
    if name == "list_evidence":
        return ToolResult({"evidence": [item.model_dump() for item in [*document.evidence, *observations]],
            "targets": [item.model_dump() for item in document.target_systems],
            "attachments": turn.context.get("available_attachments", []) if turn else []}, "已列出当前证据、可用对话附件和目标系统。")
    if isinstance(payload, LibraryFilesArguments):
        return ToolResult(distillation_library_service.list_files(db, scenario_id, payload.data_source_id,
            payload.offset, payload.limit), "已列出授权资料库中的文件。")
    if isinstance(payload, PageArguments):
        return ToolResult(distillation_library_service.list_sources(db, scenario_id, payload.offset, payload.limit),
            "已查找当前场景和授权共享资料库。")
    if isinstance(payload, LibraryReadArguments):
        if turn is None or len(turn.context.get("library_reads", [])) >= distillation_library_service.MAX_LIBRARY_READS:
            raise ValueError("本轮资料库读取已达边界，请缩小问题或在下一轮继续")
        reading = distillation_library_service.read_source(db, scenario_id, payload.data_source_id, payload.bucket_file_id)
        return ToolResult(reading.content, "已读取资料库内容或结构并冻结来源身份。", library_read=reading)
    if isinstance(payload, AttachmentArguments):
        if turn is None:
            raise ValueError("附件读取必须属于当前对话轮次")
        return ToolResult(distillation_attachment_service.read_attachment(db, turn, payload.attachment_id,
            payload.offset, payload.limit), "已读取对话临时附件的有界文本；不会自动进入资料库。")
    if name == "read_current_document":
        return ToolResult({"document": document.model_dump()}, "已读取当前保存的业务认知。")
    if isinstance(payload, ReviewArguments):
        missing = [key for key in ("beneficiary", "pain", "desired_outcome", "success_metric") if not getattr(document, key)]
        return ToolResult({"skill": "business_discovery", "version": "2", "focus": payload.focus,
            "instructions": discovery_skill(), "steps": _REVIEW_METHODS[payload.focus], "missing_value_fields": missing,
            "requires_human": True}, "已应用业务审查步骤，结论仍需证据和人工核对。")
    if isinstance(payload, EvidenceArguments):
        selected = next((item for item in [*document.evidence, *observations] if item.key == payload.evidence_key), None)
        if selected is None:
            raise ValueError("只能读取人工选择的证据")
        if selected.interview:
            from .distillation_interview_service import resolve

            return ToolResult(resolve(db, selected, scenario_id), "已读取实际专家陈述；尚未独立核实。")
        if selected.investigation_source:
            from .distillation_evidence_service import investigation_observation

            return ToolResult({"observation": investigation_observation(db, selected, scenario_id)}, "已读取此前保存的页面观察。")
        if selected.library_read:
            record = distillation_library_service.resolve_read(db, selected, scenario_id)
            return ToolResult(record["content"], "已核对资料身份并读取此前冻结的资料调查结果。")
        if selected.mcp_read:
            from .distillation_mcp_evidence_service import resolve_read

            return ToolResult({"evidence": selected.model_dump(), **resolve_read(db, selected, scenario_id)},
                "已核对 MCP 资料回执并读取历史文本快照；不代表来源的当前状态。")
        # The projection tool only sees this selected evidence. It does not scan
        # other sources just because their IDs happen to exist in the document.
        narrowed = document.model_copy(update={"evidence": [selected]})
        materials, databases, limits = distillation_analysis_service._collect_materials(db, narrowed, scenario_id)
        db.commit()
        schemas, schema_limits = distillation_analysis_service._database_schemas(databases)
        return ToolResult({"materials": materials, "database_schemas": schemas,
            "limitations": limits + schema_limits}, f"已检查选定证据 {selected.key}；读取范围和缺口见调查结果。")
    if isinstance(payload, TargetArguments):
        selected = next((item for item in document.target_systems if item.key == payload.target_key), None)
        if selected is None:
            raise ValueError("只能读取人工配置的目标系统")
        if selected.browser is not None:
            return ToolResult({"status": "blocked", "reason": "该系统配置为动态网站，请使用 open_business_system 和浏览器调查工具。"}, "该系统需要浏览器调查工具。")
        authorization = None
        if selected.access_mode == "authorized_readonly":
            from .distillation_access_service import read_authorization

            if turn is None:
                raise ValueError("受保护系统调查必须绑定当前对话")
            authorization = read_authorization(db, turn.project_id, selected)
        db.commit()
        reading = distillation_target_service.read_target_system(selected, payload.page_path, authorization=authorization) if authorization else distillation_target_service.read_target_system(selected, payload.page_path)
        source = WebsiteObservation.model_validate(reading)
        return ToolResult(source.model_dump(mode="json"), "已读取允许页面；" + "；".join(source.limitations), source=source)
    if isinstance(payload, AskArguments):
        return ToolResult({"waiting_for_human": True}, "已提出澄清问题，等待你的回答。",
            message=payload.message, questions=payload.questions)
    if isinstance(payload, ProposalArguments):
        proposal = normalize_proposal(payload.document.model_dump(), document, observations=observations)
        return ToolResult({"proposal_ready": True}, "已准备成果建议，等待你明确采用。",
            message=payload.message, proposal=proposal)
    raise ValueError("不支持的调查工具参数")
