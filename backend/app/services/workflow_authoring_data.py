"""Bounded node values and template dependencies shared by workflow authoring."""
from __future__ import annotations

import copy
import json
import math
import re


VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
MAX_NODE_DATA_BYTES = 32_768
MAX_NODE_DATA_DEPTH = 20
MAX_NODE_DATA_VALUES = 4_096
EXECUTION_FIELDS = {
    "action": ("params",), "rule": ("record",), "event": ("payload",),
    "end": ("output", "summary"), "approval": ("instructions",),
}
TEMPLATE_FIELDS = {**EXECUTION_FIELDS, "llm": ("prompt",), "approval": ()}


def _validate_json(value: object, *, depth: int = 0, budget: list[int]) -> None:
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_NODE_DATA_DEPTH:
        raise ValueError("节点执行数据超过数量或嵌套上限")
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        raise ValueError("节点执行数据必须为有限数值及标准 JSON 类型")
    for child in children:
        _validate_json(child, depth=depth + 1, budget=budget)


def execution_data(node_type: str, data: dict) -> dict:
    """Keep supported runtime values intact; reject invalid data instead of coercing it."""
    result = {key: data[key] for key in EXECUTION_FIELDS.get(node_type, ()) if key in data}
    _validate_json(result, budget=[MAX_NODE_DATA_VALUES])
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_NODE_DATA_BYTES:
        raise ValueError("节点执行数据超过字节上限")
    for key, value in result.items():
        if key in {"summary", "instructions"} and not isinstance(value, str):
            raise ValueError(f"节点的 {key} 必须为文本")
        if key in {"params", "record", "payload"} and not (
            isinstance(value, dict) or isinstance(value, str) and VARIABLE_PATTERN.fullmatch(value.strip())
        ):
            raise ValueError(f"节点的 {key} 必须为对象或完整的对象模板引用")
    return copy.deepcopy(result)


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def input_references(nodes: list[dict]) -> set[str]:
    refs: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            continue
        for field in TEMPLATE_FIELDS.get(node.get("type"), ()):
            for value in _strings(node["data"].get(field)):
                refs.update(match.group(1) for match in VARIABLE_PATTERN.finditer(value)
                            if match.group(1).startswith("params.") and ".." not in match.group(1))
    return refs


def validate_templates(nodes: list[dict], edges: list[dict]) -> None:
    """A referenced node must run on every path reaching its consumer."""
    if any(not isinstance(node.get("id"), str) or not node["id"]
           or not isinstance(node.get("data") or {}, dict) for node in nodes):
        raise ValueError("工作流节点必须有非空 ID 和对象格式的执行数据")
    identifiers = {node["id"] for node in nodes}
    node_types = {node["id"]: node["type"] for node in nodes}
    parents = {identifier: set() for identifier in identifiers}
    for edge in edges:
        if edge.get("label") and node_types[edge["source"]] != "rule":
            raise ValueError("非规则节点的顺序连线 label 必须为空，执行器不支持文字分支条件")
        parents[edge["target"]].add(edge["source"])
    starts = {node["id"] for node in nodes if node["type"] == "start"}
    dominators = {identifier: {identifier} if identifier in starts else set(identifiers)
                  for identifier in identifiers}
    for _ in range(len(nodes)):
        changed = False
        for identifier, incoming in parents.items():
            if not incoming:
                continue
            current = {identifier} | set.intersection(*(dominators[parent] for parent in incoming))
            if current != dominators[identifier]:
                dominators[identifier] = current
                changed = True
        if not changed:
            break
    for node in nodes:
        data = node.get("data") or {}
        execution_data(node["type"], data)
        allowed = (dominators[node["id"]] - {node["id"]}) | {"params"}
        for field in TEMPLATE_FIELDS.get(node["type"], ()):
            for value in _strings(data.get(field)):
                if re.search(r"\}\}\.[a-zA-Z_]|\}\}\[", value):
                    raise ValueError(f"节点 {node['id']} 的 {field} 属性路径必须放在模板大括号内")
                remainder = VARIABLE_PATTERN.sub("", value)
                if "{{" in remainder or "}}" in remainder:
                    raise ValueError(f"节点 {node['id']} 的 {field} 包含不支持的模板语法")
                for match in VARIABLE_PATTERN.finditer(value):
                    path = match.group(1).split(".")
                    if not all(path) or path[0] not in allowed:
                        raise ValueError(f"节点 {node['id']} 的 {field} 必须引用 params 或每条路径都会执行的上游节点")


AUTHORING_GUIDANCE = """
节点必须使用 id/type/name/data 键，不使用 key/node_type；连线必须使用 source/target/label，端点引用节点 id。
例如 nodes=[{id:start,type:start,name:开始,data:{}},{id:organize,type:llm,name:整理,data:{prompt:整理本次输入 {{params.materials}}}},
{id:review,type:approval,name:人工复核,data:{instructions:核对原材料与整理结果}},
{id:end,type:end,name:结束,data:{output:{materials:{{organize.parsed}}}}}]。
对应 edges=[{source:start,target:organize,label:""},{source:organize,target:review,label:""},{source:review,target:end,label:""}]。
非规则节点的顺序连线 label 必须为空；只有 rule 节点的出边使用 true/false。confidence 必须为 0 到 1 的 JSON 数字。
普通 params 文本或 JSON 参数不创建 managed_data_ports，节点不支持 managed_data_ports。
工作流节点必须在 data 中提供执行数据：action.params、rule.record、event.payload 是对象或对象模板；
end.output 是真实业务输出（支持结构化对象/数组及模板），end.summary 是可选文本；approval.instructions 是复核说明。
模板使用 {{params.field}} 引用本次输入，使用 {{upstream_node.field}} 引用每条到达路径都会执行的上游节点。
llm 节点输出包含 result（文本）和 parsed（解析 JSON），例如 end.output={{summarize.parsed}}。
approval.instructions 是静态复核说明，不展开模板；服务端暂停并记录批准或拒绝，批准后输出只含 node_id。
不得虚构审批节点返回复核表单、意见或业务判定；需要这些内容时将其作为明确的本次输入或记录尚缺的能力。
end.output 必须引用实际输入或上游结果，不得用“自动生成编号”“复核时间”“总数”等说明性占位词冒充真实运行值。
不存在 input 这个模板根；不允许引用自身、后续节点或只在另一分支执行的节点，不支持模板内代码/表达式。
"""
