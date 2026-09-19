"""Deterministic, inert Markdown/Mermaid files for a reviewed business document."""
from __future__ import annotations

import hashlib
import html
import json

from ..distillation_schemas import DistillationDocument, ProcessGraph


HANDOFF_GUIDANCE = (
    "这是人工交接的业务蒸馏资料，仅用于理解业务和生成待治理的能力候选。"
    "先核对受益者、核心痛点、可验证结果与业务边界；区分现状流程与目标流程。"
    "事实需核对证据，推断、假设、冲突不得升级为已证实事实。"
    "ER 描述逻辑业务概念，血缘描述证据到结果的转换，不是生产数据绑定。"
    "停止/调整决策及未决问题必须先向用户说明；不得据此自动发布能力或执行副作用。"
)


def _label(text: str) -> str:
    # Mermaid node syntax never comes from user text; angle brackets and quotes
    # are encoded, so imported labels cannot inject directives or hyperlinks.
    return html.escape(text.replace("\r", " ").replace("\n", " "), quote=True).replace("`", "&#96;").replace("|", "&#124;")


def _markdown(text: str) -> str:
    return html.escape(text, quote=False).replace("`", "\\`")


def process_diagram(graph: ProcessGraph) -> str:
    lines = ["flowchart TD"]
    for node in graph.nodes:
        label = node.name + (f" · {node.owner}" if node.owner else "")
        lines.append(f'  n_{node.key}["{_label(label)}"]')
    for edge in graph.edges:
        label = f'|"{_label(edge.label)}"|' if edge.label else ""
        lines.append(f"  n_{edge.source} -->{label} n_{edge.target}")
    if not graph.nodes:
        lines.append('  empty["尚未确认流程节点"]')
    return "\n".join(lines) + "\n"


def er_diagram(document: DistillationDocument) -> str:
    lines = ["erDiagram"]
    for entity in document.entities:
        lines.append(f'  e_{entity.key}["{_label(entity.name)}"] {{')
        for index, attribute in enumerate(entity.attributes, 1):
            lines.append(f'    string attribute_{index} "{_label(attribute)}"')
        lines.append("  }")
    cardinalities = {"one_to_one": "||--||", "one_to_many": "||--o{", "many_to_many": "}o--o{"}
    for relation in document.relations:
        if relation.cardinality == "unconfirmed":
            # Mermaid ER has no unknown-cardinality glyph. Keep the candidate
            # in the source and structured handoff without inventing a glyph.
            lines.append(f'  %% 基数待核对: e_{relation.source} -> e_{relation.target} : {_label(relation.label)}')
            continue
        lines.append(f'  e_{relation.source} {cardinalities[relation.cardinality]} e_{relation.target} : "{_label(relation.label)}"')
    return "\n".join(lines) + "\n"


def lineage_diagram(document: DistillationDocument) -> str:
    lines = ["flowchart LR"]
    for entity in document.entities:
        lines.append(f'  e_{entity.key}["{_label(entity.name)}"]')
    for edge in document.lineage:
        lines.append(f'  e_{edge.source} -->|"{_label(edge.transformation)}"| e_{edge.target}')
    if not document.entities:
        lines.append('  empty["尚未确认数据血缘"]')
    return "\n".join(lines) + "\n"


def modeling_brief(name: str, revision: int, document: DistillationDocument) -> str:
    lines = [f"# {_markdown(name)}：业务蒸馏交接", f"\n文档版本：{revision}\n", HANDOFF_GUIDANCE]
    for title, value in (
        ("受益者", document.beneficiary), ("核心痛点", document.pain),
        ("期望业务结果", document.desired_outcome), ("成功标准", document.success_metric),
        ("业务范围", document.scope), ("不做什么", document.non_goals),
        ("人工决策", document.decision), ("决策理由", document.decision_reason),
    ):
        lines.extend([f"\n## {title}\n", _markdown(value) or "待确认"])
    lines.append("\n## 证据及覆盖限制\n")
    for item in document.evidence:
        lines.append(f"- [{item.key}] {_markdown(item.title)}（角色：{item.role}）：{_markdown(item.summary)}；覆盖：{_markdown(item.coverage)}；限制：{_markdown(item.limitations)}")
    lines.append("\n## 事实、推断与待验证假设\n")
    for item in document.assertions:
        lines.append(f"- {item.status} [{item.key}] {_markdown(item.statement)}；证据：{', '.join(item.evidence_refs) or '无'}")
    for title, diagram in (("现状流程", process_diagram(document.as_is)), ("目标流程", process_diagram(document.to_be)), ("ER 关系", er_diagram(document)), ("数据血缘", lineage_diagram(document))):
        lines.extend([f"\n## {title}\n", "```mermaid\n" + diagram + "```"])
    lines.append("\n## 流程取舍\n")
    for item in document.improvements:
        lines.append(f"- {item.existing_node_key} / {item.decision}：{_markdown(item.rationale)}；预期效果：{_markdown(item.expected_benefit)}")
    lines.append("\n## 未决问题\n")
    lines.extend(f"- {_markdown(question)}" for question in document.open_questions)
    lines.append("\n## 历史结果逆向案例（核对范围与限制见每个案例）\n")
    for case in document.historical_cases:
        lines.extend([f"\n### {_markdown(case.title)}\n", f"范围：{_markdown(case.scope)}",
            f"结果：{_markdown(case.result_summary)}", f"关联依据：{_markdown(case.association_basis)}",
            f"差异：{_markdown(case.discrepancies) or '未记录；不能据此断言没有差异'}", f"限制：{_markdown(case.limitations)}"])
        for step in case.steps:
            lines.append(f"- {step.node_key}：{_markdown(step.input_summary)} → {_markdown(step.action)} → {_markdown(step.output_summary)}；依据：{', '.join(step.evidence_refs)}")
    # Structured data preserves node outcome/ownership and all logical metadata
    # even when a diagram intentionally uses a concise label.
    lines.extend(["\n## 结构化业务契约（候选）\n", "```json", json.dumps(document.model_dump(exclude={"target_systems"}), ensure_ascii=False, sort_keys=True, indent=2).replace("`", "\\u0060"), "```"])
    return "\n".join(lines) + "\n"


def generate_artifacts(name: str, revision: int, document: DistillationDocument) -> list[dict[str, str]]:
    contract = document.model_dump(exclude={"target_systems"})
    contents = [
        ("brief", "business-brief.md", "text/markdown", modeling_brief(name, revision, document)),
        ("as_is", "process-as-is.mmd", "text/plain", process_diagram(document.as_is)),
        ("to_be", "process-to-be.mmd", "text/plain", process_diagram(document.to_be)),
        ("er", "entity-relations.mmd", "text/plain", er_diagram(document)),
        ("lineage", "data-lineage.mmd", "text/plain", lineage_diagram(document)),
        ("contract", "business-contract.json", "application/json", json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2)),
    ]
    return [{"key": key, "filename": filename, "mime": mime, "content": content,
             "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            for key, filename, mime, content in contents]
