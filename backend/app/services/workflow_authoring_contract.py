"""Connect generated workflow contracts to the existing runtime validator."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from . import workflow_ontology_contract
from .policies import PolicyViolation


def validate(scenario: Any, nodes: list[dict], trigger_config: dict, *, db: Any = None) -> None:
    workflow = SimpleNamespace(nodes=nodes, trigger_config=trigger_config)
    contract = workflow_ontology_contract.contract_for(workflow)
    if any(node.get('type') == 'llm' for node in nodes) and (
        contract is None or not workflow_ontology_contract.output_nodes(contract) or not contract.output_schema
    ):
        raise PolicyViolation('生成的大模型工作流必须在 trigger_config.ontology_contract 声明 output_node_id 和 output_schema')
    definition = SimpleNamespace(entities={entity.id: entity for entity in scenario.entities})
    # Same validator as execution, including current property permissions.
    # Generated entity keys are not existing IDs and must not be guessed.
    workflow_ontology_contract.validate_declaration(workflow, definition, db=db)


AUTHORING_GUIDANCE = """
大模型工作流必须在 trigger_config 内完整声明 ontology_contract（不是顶层字段）：
{"version":1,"entity_ids":[],"input_bindings":[],"output_node_id":"end",
"output_schema":{"type":"object","properties":{"materials":{"type":"array","items":{"type":"string"},"maxItems":1000}},"required":["materials"],"additionalProperties":false}}。
output_schema 必须按本轮真实业务输出设计，嵌套对象也应声明字段和 additionalProperties:false；不得照抄无关示例。
entity_ids/input_bindings 只引用当前可访问的已有本体对象 ID；没有本体输入绑定时保留空数组。
输出引用整份 JSON 用 {{organize.parsed}}，取字段用 {{organize.parsed.materials}}；
确定性规则分支可有多个结束节点，但必须用 output_node_ids 列出全部结束节点，并声明同一个封闭 output_schema；与 output_node_id 互斥。
触发类型只用 manual/scheduled/event；普通本次调用使用 manual、trigger_config 仅声明 ontology_contract，不填 event_ref/interval_seconds。
禁止 {{organize.parsed}}.materials 这种模板外属性访问，它会变成字符串而不是字段值。
整理节点的 prompt 必须明确说明与 output_schema 一致的 JSON 结构，结束节点引用真实上游结果。
"""
