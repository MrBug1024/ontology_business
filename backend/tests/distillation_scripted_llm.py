"""Explicit synthetic model adapter for isolated acceptance, never production code."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from sqlalchemy.orm import Session

from app.distillation_schemas import DistillationDocument
from app.models import LLMConfig
from app.services import llm_service


def call(name: str, arguments: dict) -> dict:
    return {"id": "call_" + uuid4().hex, "type": "function", "function": {"name": name, "arguments": arguments}}


def scripted_chat(_cfg, messages, **kwargs):
    if kwargs.get("operation") != "distillation_conversation":
        raise RuntimeError("Synthetic acceptance adapter does not permit other model calls")
    callback = kwargs.get("before_provider_call")
    if callback:
        callback()
    last_user = max(index for index, item in enumerate(messages) if item["role"] == "user")
    current = messages[last_user]["content"]
    document_text = current.split("\n本次问题：\n", 1)[0].split("\n", 1)[1]
    document = DistillationDocument.model_validate(json.loads(document_text))
    completed = [item for item in messages[last_user + 1:] if item["role"] == "tool"]
    completed_ids = {item["tool_call_id"] for item in completed}
    completed_names = {
        tool["function"]["name"]
        for item in messages[last_user + 1:] if item["role"] == "assistant"
        for tool in item.get("tool_calls", []) if tool["id"] in completed_ids
    }
    if not completed:
        return {"content": "我先检查已有调查来源，并从真正需要改善的业务结果开始。", "tool_calls": [call("list_evidence", {})]}
    attachments = json.loads(completed[0]["content"]).get("attachments", [])
    if attachments and "read_attachment" not in completed_names:
        return {"content": "我先阅读你在本轮提交的临时附件。", "tool_calls": [call("read_attachment", {"attachment_id": attachments[0]["id"]})]}
    if "review_business" not in completed_names:
        return {"content": "我将核对受益者、痛点和结果之间的关系。", "tool_calls": [call("review_business", {"focus": "business_value"})]}
    answered = any("待澄清问题：" in item.get("content", "") for item in messages[:last_user])
    if not answered:
        return {"content": "", "tool_calls": [call("ask_human", {
            "message": "现有信息还不足以判断业务价值，请先确认一个关键问题。",
            "questions": [{"id": "beneficiary", "title": "谁需要结果", "question": "最终由谁确认问题真正得到解决？",
                "reason": "确认人决定我们应追踪的业务结果，不能只用归档数量替代。", "options": ["请求人确认", "处理负责人确认", "双方共同确认"]}]
        })]}
    proposed = document.model_dump()
    proposed.update(beneficiary="提出请求并确认结果的业务使用者", pain="现有流程只有记录，缺少解决结果的确认",
        desired_outcome="请求得到核验、处理，并由使用者确认解决结果", success_metric="确认解决的请求比例及解决耗时",
        scope="一个有界的请求处理流程", non_goals="不自动执行外部业务操作",
        as_is={"nodes": [{"key": "record", "name": "登记请求"}, {"key": "archive", "name": "归档记录"}],
            "edges": [{"source": "record", "target": "archive", "label": "完成登记"}]},
        to_be={"nodes": [{"key": "verify", "name": "核验请求", "owner": "处理负责人", "outcome": "确认事实与责任"},
            {"key": "resolve", "name": "确认解决结果", "owner": "请求人", "outcome": "形成解决结果证据"}],
            "edges": [{"source": "verify", "target": "resolve", "label": "处理后共同确认"}]},
        entities=[{"key": "request", "name": "业务请求", "attributes": ["请求标识", "目标结果"]},
            {"key": "result", "name": "解决结果", "attributes": ["状态", "确认时间"]}],
        relations=[{"source": "request", "target": "result", "label": "对应结果", "cardinality": "one_to_one"}],
        lineage=[{"source": "request", "target": "result", "transformation": "核验、处理并确认", "evidence_refs": []}],
        open_questions=["试点中用什么证据验证解决效果？"])
    return {"content": "", "tool_calls": [call("propose_document", {"message": "根据你的回答形成了待核对的成果建议。请审阅并决定是否采用。", "document": proposed})]}


def install_scripted_llm(engine, workspace):
    if not re.fullmatch(r"ontology_acceptance_[0-9a-f]{16}", engine.url.database or ""):
        raise RuntimeError("Synthetic model adapter requires the isolated acceptance database")
    with Session(engine) as db:
        db.add(LLMConfig(tenant_id=workspace["tenant_id"], name="Synthetic investigation acceptance",
            model="synthetic-investigation", capabilities=["chat", "tool"], enabled=True))
        db.commit()
    original = llm_service.chat
    llm_service.chat = scripted_chat

    def restore():
        llm_service.chat = original
    return restore
