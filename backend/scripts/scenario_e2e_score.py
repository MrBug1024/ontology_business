"""Run the authorized supplier score scene through real platform services.

The scene is created in the UI first. Model, governance, release, coding and
publication orchestration belongs to the shared ScenarioHarness. This consumer
asserts deterministic business results; it never creates ORM business rows or
substitutes providers/model responses.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import logging
from pathlib import Path
from uuid import uuid4

from scenario_e2e_construction import ScenarioHarness


MODEL_PROMPT = """请实际构建这个零数据业务场景：供应履约评分。
场景只使用每次调用显式提供的合成参数，不接入数据源，不创建实例、操作、事件或工作流。
只创建一个对象类型 supplier_evaluation（供应履约评价），属性 evaluation_id 为 string、
is_key true、is_title true、is_required true，用于评价对象的唯一标识与展示。
score 为非负 number 且必填，complete 为 boolean 且必填，二者 is_key/is_title 均 false；
非枚举属性 enum_values 必须为 []。score.constraints 只含 minimum:0，
其他不适用的约束键直接省略，禁止 maximum:null 或 enum_values:null。
评价对象不定义生命周期状态，state_property 为空，不引用未定义属性。
不创建 evaluation_input/evaluation_output 或额外关系：quality/timeliness 仅为本次函数输入，
函数输出 schema 不意味着要创建输出对象实例。不要为 number 设 maximum，有限大数算术溢出属于明确的失败验收。
请创建可实际执行的函数“履约加权评分”：输入为封闭 JSON Schema object，
properties 只有 quality 与 timeliness，二者均 type number、minimum 0、required；
additionalProperties false。输出为封闭 object，只有 required score:number。
runtime_kind 必须为受信 weighted_score，runtime_config 为
{"weights":{"quality":2.0,"timeliness":1.0},"bias":0}。
业务分数为 quality × 2 + timeliness；30/20 得到 80，0/0 得到 0。
有限 1e308/1e308 输入必须产生安全失败回执，不得返回 Infinity/NaN 或伪造分数。
请创建规则“履约评分达标”，绑定 supplier_evaluation，对象契约校验 input_validation object；
condition 为 and(score >= 80, complete == true)，enabled true，不触发操作。
规则只消费 score 和 complete，调用 envelope 是 {record:{score,complete}}。
80 且完整应 matched true，79 且完整或资料不完整应 matched false。
规则业务不通过仍是 succeeded 执行，禁止把它描述为系统异常。
函数与规则都必须成为可晋级的真实候选，保持来源与依赖、输入输出契约。
不生成 Python/SQL/URL，不创建 managed_data_ports，不把本说明中的例子持久为运行数据。
请覆盖 objects、capabilities（functions）和 rules 阶段；其余阶段明确无需求。
"""

CODING_INSTRUCTION = """请为供应履约评分编写可安装的真实插件客户端，覆盖已选择的函数与规则全部能力。
交付完整 README.md、skills/run-scenario/SKILL.md、examples/invoke.py，
以及 scripts/score_supplier.py 和 references/score-validation.md。
scripts/score_supplier.py 定义无默认值的 async score_supplier(quality, timeliness, complete)，
先 await invoke_scenario_capability 调用固定加权函数，typed 输入 quality/timeliness。
只在真实函数回执 status 为 succeeded 时，取其 output.score 再 await 调用固定规则，
输入严格为 {record:{score:函数真实返回的分数,complete:本次参数}}；
不得在客户端重写 quality*2+timeliness 或 score>=80，不能凭空补造评分/达标结果。
返回两次真实回执便于核对；函数失败时保留失败回执且不调用规则。
只允许受信 server 工具与 asyncio/json，不读文件、环境或任意网络，不执行 shell。
入口 Skill 追问缺少的 quality/timeliness/complete，区分模型说明与实际执行回执，
规则 matched false 为业务未达标；failed 为系统安全失败，不宣称已执行或发布。
README 明确 Python 3.12、外部环境 SCENARIO_API_KEY 配置、宿主安装、
脚本只是可导入函数而非运行 python -m scripts.score_supplier 会自动提交业务。
examples/invoke.py 独立展示两个直接调用：函数字面量合成输入 30/20，规则字面量合成输入
{record:{score:80,complete:true}}；不要在 examples 中拼接动态输入。
这只是独立接口示例，真实函数输出到规则的组合只由 scripts/score_supplier.py 实现。
严格遵守真实工具签名与回执字段。
references/score-validation.md 写明 30/20=80、0/0=0、有限大数溢出安全失败、
80达标、79未达标、不完整未达标、非法输入/无权/失效版本/重试的验证方法；
不要把静态源码校验等同于业务调用、宿主安装或人工发布。
编码工具没有业务执行权限，别声称本轮执行上述案例。
"""

CODING_CORRECTION = """显式修正上一轮真实候选，不修改业务权重、规则或受保护模板。
现有 score_supplier 的真实函数output.score到规则数据流以及失败短路正确，保留该逻辑。
修复校验：scripts/score_supplier.py 中每次 invoke_scenario_capability 调用的第一、第二参数
必须直接使用字面量能力类型与固定 ID，不允许 FUNCTION_KEY/RULE_KEY 变量作调用参数。
函数调用直接写 'function','4d01c9882b204d339daac4d005819c8e'；
规则调用直接写 'rule','63d7ec48b63042be914260f46663dce2'。
只允许第三参数为本次 typed 参数构造的字典；函数值为 quality/timeliness，
规则值为真实函数output.score与本次complete，不重写业务算法。
现有纯客户端语法不支持任何 dict.get/任意对象方法！全部改为明确的下标读取：
if function_receipt['status'] == 'succeeded':
    actual_score = function_receipt['output']['score']
然后把 actual_score 传给规则；此处受信函数成功输出契约保证 score 存在。
不使用 .get，也不新增 try/raise、默认值、网络、环境或 shell。
Skill 必须说明 get_scenario_receipt(invocation_id) 读取当前调用者拥有的真实回执，
只使用实际 invocation_id，不虚构执行状态。规则结果始终位于 receipt.output.matched，
rule_name/rule_id 也位于 output；不要把 matched 当 receipt 顶层字段。
输入 schema 或权限拒绝可能是安全协议错误而没有执行回执，不宣称一定status failed；
已有真实失败回执按 status failed 描述，matched false 则是 succeeded业务未达标。
特别完整修改 references/score-validation.md 中非法负数、非法输入、无权访问的表格：
预期必须写“请求在执行前拒绝，可能无 invocation_id/执行回执”，检查安全错误响应；
不得为这些拒绝断言 status failed，不得写 matched保持预期；仅有限大数算术溢出是
本场景的真实 status failed 验收，error为安全错误码而非原始溢出堆栈。
同时修正 Skill 的无权访问小节为请求拒绝且可能无回执。
README 只提环境变量名称 SCENARIO_API_KEY 和 SCENARIO_MCP_URL 由宿主外部配置，
不要输出任何密钥示例、占位密钥值或HTTPURL字符串；宿主安装步骤保持可操作。
完整保留并修正五个已生成 authored files，再用summary确认完整交付，
每条summary<=1000字符。模型不能执行实际业务，别声称已调用、已审阅、已发布或已安装。
"""

FINAL_DOCUMENT_CORRECTION = """当前五个文件已通过静态校验，脚本逻辑正确。
只修正两个说明文件，完整保留现有helper、Skill、examples及受保护文件。
1. references/score-validation.md 有限大数溢出行，验证方法目前声称error包含溢出说明，
这是不准确的：实际安全回执是 status=failed、output={}、error.code=provider_output_invalid，
error.message 只含安全通用说明，永远不返回原始异常、算术细节或堆栈。
该行验证方法直接写检查 status failed、output空字典和上述安全错误码，
不要要求error.message含溢出详情。说明“本次失败验收覆盖有限大数溢出”，
不承诺它是所有可能失败的唯一原因。
2. README.md 的安装第2步删去包含角括号占位的bash环境赋值块，
直接说明由宿主在外部配置SCENARIO_MCP_URL及SCENARIO_API_KEY；
不输出凭据值、密钥占位字面量或任意URL。其余可安装步骤及能力说明保留。
只生成这两个完整文件，再提交<=1000字符summary。没有业务执行/安装/发布工具，
不可宣称已业务验证、已源码审阅、已发布或已安装。
"""

SPEC = {
    "name": "供应履约评分",
    "goal": "quality × 2 + timeliness；score ≥ 80 且资料完整才达标",
    "entity_api_name": "supplier_evaluation",
    "function_name": "履约加权评分",
    "rule_name": "履约评分达标",
    "required_sections": ["entities", "functions", "rules"],
}


class ScoreAcceptanceError(ValueError):
    """A fixed, value-free acceptance code suitable for a synthetic report."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ScoreAcceptanceError(code)


def review_rule_candidate(harness: ScenarioHarness) -> None:
    """Correct only the reviewed AI rule through its normal revisioned API."""
    from app.routers import scenarios
    from app.schemas import ScenarioModelDraftResourcePatch

    proposal = harness.latest_proposal()
    candidates = [item for item in harness.candidates(proposal["proposal_id"])
                  if item["resource_kind"] == "rule"
                  and item["draft_status"] not in {"resolved", "applied", "superseded"}]
    require(len(candidates) == 1, "rule_review_requires_one_current_ai_candidate")
    candidate = candidates[0]
    original = deepcopy(candidate["payload"])
    corrected = deepcopy(original)
    corrected.update(condition={"op": "and", "conditions": [
        {"field": "score", "op": ">=", "value": 80},
        {"field": "complete", "op": "==", "value": True}]},
        input_validation="object", action_on_match="", severity="info")
    with harness.actor_db() as db:
        result = scenarios.update_scenario_model_draft(harness.scene_id, candidate["id"],
            ScenarioModelDraftResourcePatch(expected_revision=candidate["revision"], payload=corrected), db=db)
    harness.checkpoint("ai_rule_candidate_reviewed", candidate_review={
        "id": candidate["id"], "actor": harness.user_id, "expected_revision": candidate["revision"],
        "new_revision": result.revision, "before": original, "after": corrected,
        "note": "真实AI候选的type/operator错误改为既有op语法，complete自比较改为既定true条件；"
                "空操作说明和info严重性使用封闭DTO有效值，不改变评分权重或80阈值。"},
        modeling_method="real_ai_candidates_with_explicit_revisioned_rule_review")


def verify_definition(definition: object) -> dict:
    """Check the exact reviewed business contract before and after freezing."""
    require(len(definition.entities) == 1 and len(definition.functions) == 1
            and len(definition.rules) == 1, "exact_current_resource_closure_required")
    require(not definition.relations and not definition.actions and not definition.events
            and not definition.workflows and not definition.mappings
            and not definition.relation_mappings and not definition.capability_ports,
            "unrequested_current_resources_forbidden")
    entity = next(iter(definition.entities.values()))
    # The compiler owns stable API aliases; the explicit source names identify
    # these business properties without forcing cosmetic alias rewrites.
    properties = {prop.name: prop for prop in entity.properties}
    require(set(properties) == {"evaluation_id", "score", "complete"}, "evaluation_properties_mismatch")
    require(properties["evaluation_id"].data_type == "string"
            and properties["evaluation_id"].is_key and properties["evaluation_id"].is_title,
            "evaluation_identity_mismatch")
    require(properties["score"].data_type == "number"
            and properties["complete"].data_type == "boolean"
            and all(prop.is_required for prop in properties.values()), "evaluation_property_contract_mismatch")
    require(properties["score"].constraints.get("minimum") == 0
            and "maximum" not in properties["score"].constraints,
            "evaluation_score_constraints_mismatch")
    function = next(iter(definition.functions.values()))
    require(function.runtime_kind == "weighted_score"
            and function.runtime_config == {"weights": {"quality": 2.0, "timeliness": 1.0}, "bias": 0},
            "weighted_score_runtime_mismatch")
    schema = function.input_schema
    require(schema.get("type") == "object" and schema.get("additionalProperties") is False
            and set(schema.get("properties", {})) == {"quality", "timeliness"}
            and set(schema.get("required", [])) == {"quality", "timeliness"}, "current_function_schema_mismatch")
    require(all(value.get("type") == "number" and value.get("minimum") == 0
                and "maximum" not in value for value in schema["properties"].values()),
            "current_function_number_contract_mismatch")
    output = function.output_schema
    require(output.get("type") == "object" and output.get("additionalProperties") is False
            and set(output.get("properties", {})) == {"score"}
            and set(output.get("required", [])) == {"score"}
            and output["properties"]["score"].get("type") == "number", "current_function_output_mismatch")
    rule = next(iter(definition.rules.values()))
    require(rule.entity_id == entity.id and rule.input_validation == "object"
            and rule.condition == {"op": "and", "conditions": [
                {"field": "score", "op": ">=", "value": 80},
                {"field": "complete", "op": "==", "value": True}]}
            and not rule.action_on_match and not rule.trigger_action_ids,
            "current_rule_business_ast_mismatch")
    require(rule.severity == "info", "reviewed_rule_severity_mismatch")
    return {"entity_id": entity.id, "entity_api_name": entity.api_name,
            "property_ids": sorted(prop.id for prop in entity.properties),
            "function_id": function.id, "rule_id": rule.id, "runtime_kind": function.runtime_kind,
            "weights": dict(function.runtime_config["weights"]), "bias": function.runtime_config["bias"],
            "rule_condition": {"op": "and", "conditions": [
                {"field": "score", "op": ">=", "value": 80},
                {"field": "complete", "op": "==", "value": True}]}, "rule_severity": rule.severity}


def acknowledge_reviewed_model(harness: ScenarioHarness) -> None:
    from app.models import BusinessScenario
    from app.services import runtime_definition_service

    with harness.actor_db() as db:
        scenario = db.get(BusinessScenario, harness.scene_id)
        definition = runtime_definition_service.resolve_authoring(db, scenario)
        reviewed = verify_definition(definition)
    harness.checkpoint("formal_definition_reviewed", formal_definition_review=reviewed)
    proposal = harness.latest_proposal()
    candidates = harness.candidates(proposal["proposal_id"])
    require(len(candidates) == 6 and all(row["draft_status"] == "resolved" for row in candidates),
            "selected_closure_must_be_fully_resolved")
    require({row["resolved_resource_id"] for row in candidates}
            == {reviewed["entity_id"], reviewed["function_id"], reviewed["rule_id"], *reviewed["property_ids"]},
            "selected_closure_must_match_reviewed_formal_definition")
    payload = proposal.get("payload") or {}
    harness.checkpoint("human_governance_acknowledged", proposal=proposal,
        human_governance_acknowledgement={"actor": harness.user_id,
            "selected_proposal_id": proposal["proposal_id"],
            "assistant_execution_status": payload.get("execution_status"),
            "preserved_tasks": [{"id": task["id"], "status": task["status"]}
                                for task in payload.get("tasks", [])],
            "preserved_issue_codes": [issue.get("code") for issue in payload.get("unresolved", [])],
            "note": "本批明确人工审阅：最新选定对象、三个属性、2/1权重函数和score>=80且complete=true"
                    "规则的6个候选均已正常晋级且精确对应正式定义。类型、非负无上限输入、AST、"
                    "零数据及资源范围通过检查。保留助手旧任务/gaps及原AI候选，不将投影改为自动完成；"
                    "本批用户已明确授权人工发布，正式发布仍由现有release服务权威检查。"})


def selected_capabilities(harness: ScenarioHarness, release: dict) -> tuple[dict, dict]:
    from app.models import BusinessScenario
    from app.services import capability_application_service

    with harness.actor_db() as db:
        scenario = db.get(BusinessScenario, harness.scene_id)
        deployment, _inputs = capability_application_service.resolve_deployment(
            db, scenario, release_id=release["id"])
        definition = deployment.definition
        reviewed = verify_definition(definition)
        require(len(definition.entities) == 1, "release_must_contain_only_selected_evaluation_object")
        evaluation = next(iter(definition.entities.values()))
        require(not definition.relations and not definition.actions and not definition.events
                and not definition.workflows, "release_must_not_include_unrequested_resources")
        capabilities = capability_application_service.list_capabilities(
            db, scenario, release_id=release["id"], definition=definition)
    functions = [item for item in capabilities if item["kind"] == "function"]
    rules = [item for item in capabilities if item["kind"] == "rule"]
    require(len(functions) == 1 and len(rules) == 1, "exact_function_rule_selection_required")
    function, rule = functions[0], rules[0]
    require(function["readiness"]["ready"] and rule["readiness"]["ready"], "capability_not_ready")
    schema = function["input_schema"]
    require(schema.get("additionalProperties") is False, "function_input_must_be_closed")
    require(set(schema.get("properties", {})) == {"quality", "timeliness"}, "function_input_fields_mismatch")
    require(set(schema.get("required", [])) == {"quality", "timeliness"}, "function_required_fields_mismatch")
    for field in ("quality", "timeliness"):
        value = schema["properties"][field]
        require(value.get("type") == "number" and value.get("minimum") == 0
                and "maximum" not in value, "function_number_contract_mismatch")
    record = rule["input_schema"].get("properties", {}).get("record", {})
    require(set(record.get("properties", {})) == {"score", "complete"}
            and set(record.get("required", [])) == {"score", "complete"}
            and record.get("additionalProperties") is False, "rule_record_contract_mismatch")
    require(record["properties"]["score"].get("type") == "number"
            and record["properties"]["score"].get("minimum") == 0
            and record["properties"]["complete"].get("type") == "boolean", "rule_property_types_mismatch")
    require(not function["data_ports"] and not rule["data_ports"], "zero_data_contract_required")
    harness.checkpoint("frozen_contract_verified", frozen_definition_review=reviewed,
        object_identity={"id": evaluation.id,
        "api_name": evaluation.api_name, "properties": [{"name": prop.name, "api_name": prop.api_name,
            "data_type": prop.data_type} for prop in evaluation.properties]},
        selected_capabilities=[{"kind": item["kind"], "key": item["key"],
            "name": item["name"], "input_schema": item["input_schema"],
            "output_schema": item["output_schema"]} for item in (function, rule)])
    return function, rule


def accept(harness: ScenarioHarness, function: dict, rule: dict) -> list[dict]:
    cases: list[dict] = []
    summaries: list[dict] = []

    def run(capability: dict, role: str, inputs: dict, expected_status: str) -> dict:
        receipt = harness.invoke(
            capability["kind"], capability["key"], inputs,
            idempotency_key=f"score-e2e:{harness.scene_id}:{capability['kind']}:{role}")
        require(receipt["status"] == expected_status, f"{capability['kind']}_{role}_status_mismatch")
        cases.append({"kind": capability["kind"], "key": capability["key"], "role": role,
                      "invocation_id": receipt["invocation_id"], "expected_status": expected_status})
        summaries.append({"kind": capability["kind"], "key": capability["key"], "role": role,
                          "invocation_id": receipt["invocation_id"], "status": receipt["status"],
                          "inputs": inputs, "expected_status": expected_status,
                          "output": receipt["output"],
                          "error_code": (receipt.get("error") or {}).get("code")})
        return receipt

    score = run(function, "success", {"quality": 30, "timeliness": 20}, "succeeded")
    require(score["output"] == {"score": 80.0}, "weighted_score_business_result_mismatch")
    boundary = run(function, "boundary", {"quality": 0, "timeliness": 0}, "succeeded")
    require(boundary["output"] == {"score": 0.0}, "weighted_score_zero_boundary_mismatch")
    overflow = run(function, "failure", {"quality": 1e308, "timeliness": 1e308}, "failed")
    require(bool((overflow.get("error") or {}).get("code")) and overflow["output"] == {},
            "weighted_score_overflow_must_fail_safely")

    success = run(rule, "success", {"record": {"score": score["output"]["score"], "complete": True}}, "succeeded")
    require(success["output"].get("matched") is True, "score_rule_actual_function_result_mismatch")
    threshold = run(rule, "boundary", {"record": {"score": 80, "complete": True}}, "succeeded")
    require(threshold["output"].get("matched") is True, "score_rule_threshold_mismatch")
    negative = run(rule, "failure", {"record": {"score": 79, "complete": True}}, "succeeded")
    require(negative["output"].get("matched") is False, "score_rule_business_negative_mismatch")
    incomplete = harness.invoke(rule["kind"], rule["key"], {"record": {"score": 80, "complete": False}},
        idempotency_key=f"score-e2e:{harness.scene_id}:rule:incomplete")
    require(incomplete["status"] == "succeeded" and incomplete["output"].get("matched") is False,
            "score_rule_incomplete_mismatch")
    require(len({item["invocation_id"] for item in cases}) == 6, "distinct_receipts_required")
    harness.checkpoint("business_acceptance", cases=summaries, acceptance_cases=cases,
        selected_capabilities=[{"kind": item["kind"], "key": item["key"],
                                "name": item["name"], "input_schema": item["input_schema"],
                                "output_schema": item["output_schema"]} for item in (function, rule)],
        composition={"function_invocation_id": score["invocation_id"],
                     "rule_invocation_id": success["invocation_id"],
                     "used_actual_function_output": True},
        incomplete={"invocation_id": incomplete["invocation_id"], "matched": False}, passed=True)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--username", default="mrbug")
    parser.add_argument("--report", type=Path,
                        default=Path("docs/multi-scenario-artifacts-2026-10-06/score.json"))
    parser.add_argument("--review-note", default="")
    parser.add_argument("--retry-model", action="store_true")
    parser.add_argument("--resolve-model", action="store_true")
    parser.add_argument("--review-rule", action="store_true")
    parser.add_argument("--acknowledge-model", action="store_true")
    parser.add_argument("--revise-coding", action="store_true")
    parser.add_argument("--finalize-docs", action="store_true")
    args = parser.parse_args()
    # Application loggers may attach provider tracebacks. The durable report
    # below records only safe stage identities and fixed acceptance failures.
    logging.disable(logging.CRITICAL)
    harness = ScenarioHarness(args.scene_id, args.username, args.report, SPEC)
    try:
        if args.retry_model:
            require(not harness.latest_proposal(), "model_retry_requires_no_existing_proposal")
            harness.checkpoint("construction_explicit_retry", construction_request_id=uuid4().hex)
        if args.resolve_model:
            harness.resolve_model("原候选额外扩展了输入、输出对象和关系；本次明确不需要这些资源。"
                "本体的身份要求此前未说明，现明确评价唯一标识 evaluation_id 为主键兼标题。"
                "该标识不加入未消费它的函数或规则运行输入。非枚举属性 enum_values=[]。"
                "请依照以下完整固定范围实际重新规划，并保留原业务评分及失败验收：\n" + MODEL_PROMPT)
        if args.review_rule:
            review_rule_candidate(harness)
        if args.acknowledge_model or harness.report.get("human_governance_acknowledgement"):
            acknowledge_reviewed_model(harness)
        else:
            harness.build_model(MODEL_PROMPT)
        if not harness.release:
            proposal = harness.latest_proposal()
            rule_candidates = [row for row in harness.candidates(proposal["proposal_id"])
                               if row["resource_kind"] == "rule" and row["resolved_resource_id"]]
            require(len(rule_candidates) == 1, "exact_formal_rule_activation_required")
            harness.enable_capabilities([{"kind": "rule", "key": rule_candidates[0]["resolved_resource_id"]}])
        release = harness.govern_and_release()
        function, rule = selected_capabilities(harness, release)
        cases = accept(harness, function, rule)
        capabilities = [{"kind": item["kind"], "key": item["key"]} for item in (function, rule)]
        if args.revise_coding:
            require(bool(harness.report.get("workspace_id")), "coding_revision_requires_existing_workspace")
            harness.checkpoint("source_revision_requested", initial_source_review={
                "note": "真实首轮helper返回function_receipt/rule_receipt，失败短路及真实output.score数据流正确；"
                        "固定能力常量不符合现有字面量调用合同、Skill缺回执查询；文档output层级与拒绝语义需修正。",
                "requires_revision": True})
            harness.revise_coding(CODING_CORRECTION)
        if args.finalize_docs:
            require(not args.revise_coding and bool(harness.report.get("workspace_id")),
                    "document_revision_requires_existing_workspace")
            harness.checkpoint("document_revision_requested", document_review={
                "note": "最终helper/Skill/examples已审阅；真实溢出回执只有安全通用错误，"
                        "文档不能承诺错误含算术细节；宿主环境配置不用bash角括号占位。",
                "requires_revision": True})
            harness.revise_coding(FINAL_DOCUMENT_CORRECTION)
        harness.start_coding(capabilities, CODING_INSTRUCTION)
        if not args.review_note.strip():
            harness.checkpoint("source_review_required", passed=False)
            print("Supplier score candidate generated; read its actual source before reviewing and publishing.")
            return
        result = harness.code_review_publish(
            capabilities=capabilities, cases=cases, instruction=CODING_INSTRUCTION,
            review_note=args.review_note)
        harness.checkpoint("complete", result=result, passed=True, error_type=None, code=None)
        print("Supplier score scene: business assertions and real AI plugin publication passed.")
    except Exception as error:
        safe_code = str(error) if isinstance(error, ScoreAcceptanceError) else "platform_stage_failed"
        harness.checkpoint("failure", error_type=type(error).__name__, code=safe_code, passed=False)
        print(f"Supplier score scene stopped: {type(error).__name__}; see the safe stage report.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
