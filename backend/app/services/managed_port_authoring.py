"""Expose managed-port declaration rules to authors without relaxing validation."""
from __future__ import annotations

import json

from .assistant_capability_modeling_service import _MEDIA_BINDING_KINDS


def authoring_context() -> str:
    return (
        "\n【受管输入端口契约】\n"
        "direction=input 时 role 只能是 invocation_input、reference、rules；本次用户输入使用 invocation_input，"
        "不能使用 input、attachment、data 等自造角色。direction=output 时 role=output。"
        "cardinality=one 或 many。输入 binding_policy=per_invocation、scenario_default 或 release_pinned；"
        "本次上传使用 per_invocation。输出 binding_policy=none。\n"
        "表格附件转换为受管数据集后用于查询：evidence_kind=versioned_data，media_kind=structured 或 dataset；"
        "document_attachment 仅用于 document/artifact 文档端口，不用于结构化表格查询。"
        "端口 evidence_refs 必须为当前能力 evidence_refs 的非空子集。"
        "media_kind 对应允许的 binding_kinds："
        + json.dumps(_MEDIA_BINDING_KINDS, ensure_ascii=False)
        + "\n不得填写当前上传文件、数据集版本或存储位置。schema_document 必须为对象，不能是 null。"
        "若受信执行器已通过语义映射校验数据字段，且本端口无附加内容约束，明确写 schema_document={}；"
        "不能把合法的空附加约束标为业务资料缺失。\n"
    )
