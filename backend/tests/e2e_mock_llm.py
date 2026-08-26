"""Tiny OpenAI-compatible provider used only by the isolated browser E2E run.

Run with ``python tests/e2e_mock_llm.py --port 8033`` after seeding the
matching fixture. It deliberately exposes no credentials and never forwards a
request outside localhost.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    server_version = "P1E2EMockLLM/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, payload: dict | str) -> None:
        encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {encoded}\n\n".encode("utf-8"))
        self.wfile.flush()

    @staticmethod
    def _structured_model_response(messages: list[dict]) -> dict | None:
        """Return a deterministic document-aware contract for local browser E2E.

        The production compiler still owns normalization and validation.  This
        fixture only makes the local provider return a contract-shaped result,
        while deriving evidence refs from the exact prompt chunk so provenance
        scope checks remain exercised.
        """
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        if "业务本体文档编译器" not in prompt:
            return None
        match = re.search(r"待逐段编译的业务语义来源：\n(\[.*\])\s*$", prompt, re.S)
        try:
            paragraphs = json.loads(match.group(1)) if match else []
        except json.JSONDecodeError:
            paragraphs = []
        refs = [str(item.get("ref") or "") for item in paragraphs if item.get("ref")]
        evidence = refs[:2] or ["request:local-e2e"]

        def resource(key: str, **values: object) -> dict:
            return {"key": key, "evidence_refs": evidence, "confidence": 0.96, **values}

        def prop(
            name: str,
            data_type: str = "string",
            *,
            key: bool = False,
            title: bool = False,
            required: bool = False,
            enum_values: list[str] | None = None,
        ) -> dict:
            return {
                "name": name,
                "data_type": data_type,
                "description": "建筑领域欠薪预警业务字段",
                "is_key": key,
                "is_title": title,
                "is_required": key or required,
                "is_enum": bool(enum_values),
                "enum_values": list(enum_values or []),
                "default_value": None,
                "constraints": {},
                "is_sensitive": False,
            }

        entities = [
            resource(
                "entity.project",
                name="建设项目",
                description="建筑施工项目及其欠薪风险治理范围。",
                is_abstract=False,
                state_property="项目状态",
                properties=[
                    prop("项目编号", key=True, title=True),
                    prop("项目名称"),
                    prop("建设单位"),
                    prop(
                        "项目状态",
                        required=True,
                        enum_values=["筹备", "在建", "停工", "竣工"],
                    ),
                ],
            ),
            resource(
                "entity.worker",
                name="农民工",
                description="参与建设项目施工并产生工资权益的劳动者。",
                is_abstract=False,
                state_property="劳动状态",
                properties=[
                    prop("人员编号", key=True, title=True),
                    prop("姓名"),
                    prop("所属班组"),
                    prop("进场日期", "date"),
                    prop(
                        "劳动状态",
                        required=True,
                        enum_values=["待进场", "在场", "退场"],
                    ),
                ],
            ),
            resource(
                "entity.contractor",
                name="施工企业",
                description="承担施工、用工和工资支付责任的参与主体。",
                is_abstract=False,
                state_property="企业状态",
                properties=[
                    prop("统一社会信用代码", key=True, title=True),
                    prop("企业名称"),
                    prop("信用等级"),
                    prop(
                        "企业状态",
                        required=True,
                        enum_values=["正常", "整改中", "限制承接"],
                    ),
                ],
            ),
            resource(
                "entity.wage_ledger",
                name="工资台账",
                description="记录应发工资、实发工资、支付日期和支付凭证。",
                is_abstract=False,
                state_property="支付状态",
                properties=[
                    prop("台账编号", key=True, title=True),
                    prop("人员编号"),
                    prop("应发工资", "number"),
                    prop("实发工资", "number"),
                    prop("应付日期", "date"),
                    prop("支付日期", "date"),
                    prop("逾期天数", "integer"),
                    prop(
                        "支付状态",
                        required=True,
                        enum_values=["待支付", "部分支付", "已支付", "逾期"],
                    ),
                ],
            ),
            resource(
                "entity.wage_risk",
                name="欠薪风险",
                description="由工资、考勤、专户和投诉等证据识别出的欠薪风险。",
                is_abstract=False,
                state_property="风险状态",
                properties=[
                    prop("风险编号", key=True, title=True),
                    prop("风险等级"),
                    prop("逾期天数", "integer"),
                    prop("风险原因"),
                    prop(
                        "风险状态",
                        required=True,
                        enum_values=["待识别", "待复核", "已确认", "已消除"],
                    ),
                ],
            ),
            resource(
                "entity.warning",
                name="欠薪预警",
                description="面向监管人员的欠薪风险预警及处置记录。",
                is_abstract=False,
                state_property="处置状态",
                properties=[
                    prop("预警编号", key=True, title=True),
                    prop("预警级别"),
                    prop("生成时间", "datetime"),
                    prop(
                        "处置状态",
                        required=True,
                        enum_values=["待处置", "处置中", "已关闭"],
                    ),
                ],
            ),
        ]
        relations = [
            resource("relation.project_worker", name="项目雇佣农民工", source_ref="entity.project", target_ref="entity.worker", relation_type="N:M", constraints={}),
            resource("relation.project_contractor", name="项目由施工企业承建", source_ref="entity.project", target_ref="entity.contractor", relation_type="N:1", constraints={}),
            resource("relation.worker_wage", name="农民工拥有工资台账", source_ref="entity.worker", target_ref="entity.wage_ledger", relation_type="1:N", constraints={}),
            resource("relation.wage_risk", name="工资台账触发欠薪风险", source_ref="entity.wage_ledger", target_ref="entity.wage_risk", relation_type="1:N", constraints={}),
            resource("relation.risk_warning", name="欠薪风险生成预警", source_ref="entity.wage_risk", target_ref="entity.warning", relation_type="1:N", constraints={}),
        ]
        instances = [
            resource("instance.worker.demo", entity_ref="entity.worker", display_name="示例农民工", values={"人员编号": "WORKER-001", "姓名": "示例农民工"}),
            resource("instance.project.demo", entity_ref="entity.project", display_name="示例建设项目", values={"项目编号": "PROJECT-001", "项目名称": "示例建设项目"}),
        ]
        functions = [
            resource("function.calculate_overdue_days", name="计算工资逾期天数", description="根据应付日期和支付日期计算逾期天数。", input_schema={"type": "object", "properties": {"应付日期": {"type": "string", "format": "date"}, "支付日期": {"type": "string", "format": "date"}}, "required": ["应付日期"], "additionalProperties": False}, output_schema={"type": "object", "properties": {"逾期天数": {"type": "integer"}}, "additionalProperties": False}, tags=["欠薪", "风险"]),
        ]
        actions = [
            resource("action.create_warning", name="生成欠薪预警", entity_ref="entity.warning", description="根据确认的欠薪风险生成预警记录。", input_schema={"type": "object", "properties": {"风险编号": {"type": "string"}}, "required": ["风险编号"], "additionalProperties": False}, precondition="欠薪风险已确认", postcondition="生成欠薪预警并进入处置流程",),
        ]
        rules = [
            resource("rule.overdue_wage_warning", name="工资逾期欠薪预警", entity_ref="entity.wage_ledger", description="工资逾期超过 90 天或实发工资低于应发工资时触发欠薪风险。", condition={"op": "or", "conditions": [{"field": "逾期天数", "op": ">=", "value": 90}, {"field": "实发工资", "op": "<", "value_field": "应发工资"}]}, action_on_match="生成欠薪预警", trigger_action_refs=["action.create_warning"], severity="critical"),
        ]
        events = [
            resource("event.wage_warning_created", name="欠薪预警已生成", description="欠薪风险通过规则后发布预警事件。", payload_schema={"type": "object", "properties": {"预警编号": {"type": "string"}}, "required": ["预警编号"], "additionalProperties": False}, trigger_source="欠薪风险识别"),
        ]
        workflows = [
            resource(
                "workflow.wage_warning_disposal",
                name="欠薪预警处置流程",
                description="识别风险、生成预警并等待监管人员处置。",
                trigger_type="event",
                trigger_config={"event_ref": "event.wage_warning_created"},
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "开始"}},
                    {
                        "id": "create_warning",
                        "type": "action",
                        "data": {
                            "label": "生成欠薪预警",
                            "resource_ref": "action.create_warning",
                        },
                    },
                    {
                        "id": "review",
                        "type": "approval",
                        "data": {"label": "监管人员复核"},
                    },
                    {"id": "end", "type": "end", "data": {"label": "结束"}},
                ],
                edges=[
                    {"id": "e1", "source": "start", "target": "create_warning"},
                    {"id": "e2", "source": "create_warning", "target": "review"},
                    {"id": "e3", "source": "review", "target": "end"},
                ],
            ),
        ]
        conceptual_mappings = [
            resource("mapping.wage_ledger", mapping_kind="entity", entity_ref="entity.wage_ledger", relation_ref="", source_label="建筑领域工资台账", table_name="工资台账", column_map={"人员编号": "人员编号", "应发工资": "应发工资", "实发工资": "实发工资", "应付日期": "应付日期", "支付日期": "支付日期"}, binding_requirements=["补充建筑领域工资台账数据源"]),
        ]
        unresolved = [{"code": "MAPPING_DEFERRED_NO_DATA_SOURCE", "message": "工资台账尚未绑定物理数据源，补齐后自动修复映射绑定。", "source_refs": evidence, "blocking": False}]
        coverage = [{"source_ref": ref, "status": "modeled", "reason": "已纳入建筑领域欠薪预警模型", "change_keys": ["entity.project", "entity.worker", "entity.wage_ledger", "rule.overdue_wage_warning"]} for ref in refs]
        return {
            "schema_version": "scenario_model.v1",
            "entities": entities,
            "relations": relations,
            "instances": instances,
            "functions": functions,
            "actions": actions,
            "rules": rules,
            "events": events,
            "workflows": workflows,
            "mappings": [],
            "relation_mappings": [],
            "conceptual_mappings": conceptual_mappings,
            "unresolved": unresolved,
            "coverage": coverage,
        }

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        if self.path.rstrip("/").endswith("/embeddings"):
            items = request.get("input") or []
            if isinstance(items, str):
                items = [items]
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": index}
                        for index, _ in enumerate(items)
                    ],
                    "model": request.get("model", "e2e-model"),
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return

        structured = self._structured_model_response(request.get("messages") or [])
        if structured is not None:
            reply = json.dumps(structured, ensure_ascii=False)
        else:
            reply = "E2E 模拟模型已响应：当前上下文已按权限范围处理。"
        model = str(request.get("model") or "e2e-model")
        created = int(time.time())
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self._sse(
                {
                    "id": "e2e-chat-stream",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}
                    ],
                }
            )
            self._sse(
                {
                    "id": "e2e-chat-stream",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self._sse("[DONE]")
            return
        self._json(
            200,
            {
                "id": "e2e-chat",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8033)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"e2e mock llm listening on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
