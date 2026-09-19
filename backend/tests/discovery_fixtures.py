"""Synthetic, evidence-complete business understanding; no real customer data."""
from app.distillation_schemas import DistillationDocument


def reviewed_document() -> DistillationDocument:
    return DistillationDocument.model_validate({
        "beneficiary": "请求人与经办人", "pain": "重复核对耗时", "desired_outcome": "确认处理结果",
        "success_metric": "从接收到确认的时间", "scope": "一个有明确边界的请求流程",
        "decision": "continue", "decision_reason": "先进行可核验的有限试点",
        "evidence": [{"key": "input", "title": "合成输入记录", "role": "input", "summary": "请求编号和内容"},
            {"key": "output", "title": "合成结果记录", "role": "result", "summary": "结果编号和确认内容"},
            {"key": "log", "title": "合成过程记录", "role": "process", "summary": "同一请求的处理时间和操作人"}],
        "as_is": {"nodes": [{"key": "review", "name": "核对请求", "owner": "经办人", "inputs": "请求内容",
            "outcome": "确认结果", "evidence_refs": ["log"]}], "edges": []},
        "to_be": {"nodes": [{"key": "review", "name": "核对请求", "owner": "经办人", "inputs": "请求内容",
            "outcome": "确认结果", "evidence_refs": ["log"]}], "edges": []},
        "entities": [{"key": "request", "name": "请求", "identity": "请求编号与受理期", "evidence_refs": ["input"]},
            {"key": "result", "name": "结果", "identity": "结果编号", "evidence_refs": ["output"]}],
        "relations": [{"source": "request", "target": "result", "cardinality": "one_to_many",
            "rationale": "同一请求可多次处理并保留各次结果", "evidence_refs": ["log"]}],
        "lineage": [{"source": "request", "target": "result", "transformation": "经办人核对后确认", "evidence_refs": ["log"]}],
        "historical_cases": [{"key": "example", "title": "合成正常案例", "scope": "一次完整处理",
            "result_summary": "完成并确认", "input_refs": ["input"], "result_refs": ["output"], "process_refs": ["log"],
            "association_basis": "按请求编号与受理期匹配过程和结果", "limitations": "单一合成案例，仍需真实业务验证",
            "steps": [{"node_key": "review", "input_summary": "请求内容", "action": "核对", "output_summary": "确认结果", "evidence_refs": ["log"]}]}],
    })
