"""Real synthetic procurement scene; resume each durable stage safely."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from uuid import uuid4

try:
    from scenario_e2e_construction import HarnessBlocked, ScenarioHarness
    from app.models import BusinessScenario
    from app.schemas import ScenarioModelDraftResourcePatch
    from app.routers import scenarios
except Exception as exc:  # Settings validation must not print configuration values.
    print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__}, ensure_ascii=True), flush=True)
    raise SystemExit(1) from None


SPEC = {
    'name': '办公采购分流',
    'goal': '金额<=100、类别standard/priority且资料完整才通过',
    'zero_data': True,
}
PROMPT = '''请现在创建当前办公采购分流场景的业务本体和一条可执行规则候选，不是仅解释方案。
明确建设范围只有ontology和rules两个任务，不需要mapping、functions/actions或workflow。
这是用户授权的合成零数据业务场景。创建采购申请实体purchase_request，金额amount为必填number且minimum=0，类别category为必填string且enum=[standard,priority]，资料完整标记complete为必填boolean。
创建规则“办公采购通过”，rule_type=validation、input_validation=object，并精确引用上面实体。规则采用受支持的组合表达式：amount<=100，category属于standard/priority，complete==true，三项必须同时满足。
每次执行仅使用本次显式输入record，不连接DataSource、Dataset、Mapping、客户数据库或外部写工具；没有固定默认运行输入。
50/standard/true通过；100/priority/true边界通过；101/standard/true业务不通过；50/standard/false不通过。业务不通过是成功完成规则计算，不是系统故障。
请真实生成可校验的实体属性Schema和规则表达式、来源覆盖，并按平台任务顺序完成候选，不能任意Python代码或占位文本。'''
INSTRUCTION = '''为办公采购分流精确发布实现一个可安装的Claude Code插件。用已提供的不可变能力契约与受信适配器，创建可发现的SKILL、明确README、当前输入示例以及纯客户端函数scripts/check_purchase.py。函数async check_purchase(amount, category, complete)要求调用方显式提供所有参数，无默认业务输入；只构造record并await受信adapter调用选择的rule，不本地重写阈值算法。当前输入由Skill/MCP向用户收集，结构化结果使用真实matched与invocation_id，并明确业务不通过仍可为succeeded。合成输入仅在examples覆盖50、100、101与资料不完整；脚本不自行运行合成案例，不承诺未实现的CLI。保持HTTPS验证、运行时外部配置凭据、只用精确发布，不固定平台地址、密钥、场景以外资源，也不能伪造调用成功。完成文件后调用真实校验工具并给出完成摘要。'''
CONDITION = {'op': 'and', 'conditions': [
    {'field': 'amount', 'op': '<=', 'value': 100},
    {'field': 'category', 'op': 'in', 'value': ['standard', 'priority']},
    {'field': 'complete', 'op': '==', 'value': True},
]}


def repair_rule(harness: ScenarioHarness) -> None:
    proposal = harness.latest_proposal()
    rows = [row for row in harness.candidates(proposal['proposal_id'])
            if row['resource_kind'] == 'rule' and row['payload'].get('name') == '办公采购通过'
            and row['draft_status'] not in {'resolved', 'superseded'}]
    if len(rows) != 1:
        raise HarnessBlocked('人工语法审阅需要当前唯一实际AI规则候选')
    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        entities = [entity for entity in scene.entities if entity.name == '采购申请'
                    and {prop.name for prop in entity.properties} == {'request_id', 'amount', 'category', 'complete'}]
        if len(entities) != 1:
            raise HarnessBlocked('实际采购对象及字段不符合已澄清规格')
        entity_id = entities[0].id
    row = rows[0]
    before = copy.deepcopy(row['payload'])
    payload = {**before, 'condition': copy.deepcopy(CONDITION), 'entity_ref': entity_id,
               'entity': {'kind': 'existing', 'id': entity_id, 'display_name': '采购申请'}}
    changed = {key: {'before': before.get(key), 'after': payload[key]}
               for key in ('condition', 'entity_ref', 'entity') if before.get(key) != payload[key]}
    if not changed:
        return
    review = {'draft_id': row['id'], 'actor': harness.user_id, 'expected_revision': row['revision'],
             'original_ai_payload': before, 'diff': changed,
             'note': '仅修正受支持op/conditions语法和精确已正式对象引用，100阈值/类别/完整性语义不变；非全自动建模'}
    harness.checkpoint('human_candidate_review_started', human_candidate_review=review)
    with harness.actor_db() as db:
        result = scenarios.update_scenario_model_draft(harness.scene_id, row['id'],
                    ScenarioModelDraftResourcePatch(expected_revision=row['revision'], payload=payload), db=db)
    review['result_revision'] = result.revision
    harness.checkpoint('human_candidate_review_saved', human_candidate_review=review)


def execute(args: argparse.Namespace) -> None:
    harness = ScenarioHarness(args.scene_id, args.username, Path(args.report), SPEC)
    if args.retry_construction:
        if harness.latest_proposal():
            raise HarnessBlocked('已有实际候选时不能从头重复建设')
        harness.checkpoint('construction_explicit_retry', construction_request_id=uuid4().hex)
    if args.clarify_model:
        harness.resolve_model('本合成场景明确补充purchase_request.request_id为必填string，作为唯一is_key=true且is_title=true的属性，用于标识采购申请；amount、category、complete仍为原始规格。所有非枚举属性enum_values=[]而不是null，category enum_values=[standard,priority]。保留ontology与rules两任务以及原三项同时满足的采购规则，禁止添加其他能力或数据源。request_id仅属于对象标识；规则只读取amount/category/complete，本次rule.record运行Schema不增加request_id。')
    if args.repair_rule:
        repair_rule(harness)
    try:
        harness.build_model(PROMPT)
    except HarnessBlocked:
        if not harness.report.get('human_candidate_review', {}).get('result_revision'):
            raise
        harness.acknowledge_governed_model(review_note='采购AI规则已通过正常候选CAS语法/引用修正、真实重校验与原子晋级；原模型摘要仍保留旧错误，未宣称全自动模型完成。正式定义需再断言精确AST/资源范围并经过权威release校验。')
    if not harness.release:
        with harness.actor_db() as db:
            scene = db.get(BusinessScenario, harness.scene_id)
            if len(scene.entities) != 1 or len(scene.rules) != 1 or scene.function_definitions or scene.actions or scene.workflows or scene.events:
                raise HarnessBlocked('实际正式定义含未授权额外资源或缺目标能力')
            rule = scene.rules[0]
            if rule.name != '办公采购通过' or rule.condition != CONDITION or rule.input_validation != 'object':
                raise HarnessBlocked('正式规则不符合固定业务语义，不能发布')
            rule_id = rule.id
            severity = rule.severity
        overrides = {rule_id: 'warning'} if severity == 'medium' else None
        harness.enable_capabilities([{'kind': 'rule', 'key': rule_id}], rule_severity_overrides=overrides)
    harness.govern_and_release()
    rules = [item for item in harness.report['capability_catalog'] if item['kind'] == 'rule']
    if len(rules) != 1 or rules[0]['name'] != '办公采购通过':
        raise HarnessBlocked('实际发布未包含唯一目标规则，不按位置或相似功能替代')
    capability = {'kind': 'rule', 'key': rules[0]['key']}
    cases = harness.report.get('acceptance_cases', [])
    fixtures = [('success', 50, 'standard', True, True),
                ('boundary', 100, 'priority', True, True),
                ('failure', 101, 'standard', True, False),
                ('incomplete', 50, 'standard', False, False)]
    for role, amount, category, complete, expected in fixtures:
        if any(item['role'] == role for item in harness.report['business_cases']):
            continue
        inputs = {'record': {'amount': amount, 'category': category, 'complete': complete}}
        receipt = harness.invoke('rule', capability['key'], inputs)
        if receipt['status'] != 'succeeded' or receipt['output'].get('matched') is not expected:
            harness.checkpoint('business_assertion_failed', unexpected_case={'role': role, 'inputs': inputs,
                                'expected_matched': expected, 'receipt': receipt})
            raise HarnessBlocked('真实规则结构化业务结果与固定规格不一致')
        harness.report['business_cases'].append({'role': role, 'inputs': inputs,
            'expected_assertion': {'status': 'succeeded', 'matched': expected},
            'actual_output': receipt['output'], 'invocation_id': receipt['invocation_id']})
        if role in {'success', 'boundary', 'failure'}:
            cases.append({**capability, 'role': role, 'invocation_id': receipt['invocation_id'],
                          'expected_status': 'succeeded'})
        harness.checkpoint('business_case_verified', acceptance_cases=cases, capabilities=[capability])
    if args.retry_coding:
        harness.revise_coding(INSTRUCTION + '\n只修复当前缺口，之前的examples四次直接await调用已正确。scripts/check_purchase.py仅从server导入invoke_scenario_capability，定义async def check_purchase(amount, category, complete)（无默认），return await invoke_scenario_capability("rule",精确已选key,{"record":{"amount":amount,"category":category,"complete":complete}})。不要result.get等对象属性、argparse/sys/float、同步wrapper、asyncio.run未await客户端、脚本__main__固定案例、任何本地规则判定。README说明调用方显式await此函数，删除旧CLI命令及承诺；文档不要写任何密钥字面量，只说明环境变量名称。使用submit_plugin_step实际提交完整scripts/check_purchase.py及README.md，validate_plugin_project通过后提交非空summary。')
    workspace = harness.start_coding([capability], INSTRUCTION)
    if not args.review_note:
        harness.checkpoint('awaiting_source_review', files_hash=workspace['files_hash'])
        return
    harness.code_review_publish([capability], cases, INSTRUCTION, review_note=args.review_note)
    harness.checkpoint('application_chain_completed', application_chain_completed=True,
                       remaining_verification=['root actual browser', 'root HTTP/MCP', 'root independent installation'])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene-id', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--review-note', default='')
    parser.add_argument('--retry-construction', action='store_true')
    parser.add_argument('--clarify-model', action='store_true')
    parser.add_argument('--repair-rule', action='store_true')
    parser.add_argument('--retry-coding', action='store_true')
    args = parser.parse_args()
    try:
        execute(args)
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__}, ensure_ascii=True), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
