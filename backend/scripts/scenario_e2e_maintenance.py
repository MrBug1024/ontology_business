"""Real, scenario-scoped maintenance approval acceptance; no mocked business writes."""
from __future__ import annotations

import argparse
import ast
from difflib import unified_diff
from pathlib import Path
import re
import time
from uuid import uuid4


SPEC = {
    'scenario': '设备维修审批',
    'input_path': 'maintenance_request.estimated_amount',
    'identity_path': 'maintenance_request.request_id',
    'automatic_limit': 100,
    'approval_limit': 1000,
    'approval_roles': ['owner', 'admin'],
    'zero_data': True,
    'external_business_writes': False,
}

MODELING_PROMPT = """请在当前新场景真实建设“设备维修审批”，按平台当前阶段逐项形成候选并人工确认晋级。
这是合成业务验收，不连接客户系统，没有库存、资金、通知或其他外部副作用。
本体：只能建设一个“维修申请”对象，api_name=maintenance_request。
其唯一主键兼标题属性 name/api_name均为request_id，string、is_key=true、is_title=true、is_required=true；
另一个属性 name/api_name均为estimated_amount，非负 number、is_required=true，constraints.minimum=0。
这两个属性非枚举，is_enum=false、enum_values=[]。不得把工作流、规则、审批配置或协议输出建成对象类型或关系。
本次调用格式必须为 {"maintenance_request":{"request_id":"本次申请标识","estimated_amount":数值}}；
不从建模资料、数据库或历史对话补输入。
低额规则：维修申请 estimated_amount <= 100；预算规则：estimated_amount <= 1000。
确定性工作流：一个 start -> 低额规则，true -> 自动通过结束，false -> 预算规则；
预算规则 true -> 人工审批 -> 批准结束，false -> 超预算结束。规则分支必须显式 label=true/false；其余顺序 label=""。
规则节点 resource_ref 引用已定义规则，data.record 使用完整对象模板 {{params.maintenance_request}}。
审批 data 配置 approver_roles=["owner","admin"]、requires_evidence=false、timeout_seconds=3600、on_timeout="reject"；
instructions说明仅核对本次合成申请，等待明确同意或驳回。不得自己批准，也不把审批结果虚构为表单返回。
全部三个结束节点 data.output 返回同一个封闭结构：decision、reason、estimated_amount。
自动通过 decision="auto_approved"，reason="金额不超过自动通过上限"；
批准结束 decision="approved"，reason="已获得人工审批"；
超预算结束 decision="over_budget"，reason="金额超过预算上限"。
estimated_amount 必须引用 {{params.maintenance_request.estimated_amount}}，不能写示例金额常量。
workflow trigger_type=manual，trigger_config.ontology_contract version=1；entity_ids引用当前已正式维修申请对象的精确ID，
input_bindings=[{path:"maintenance_request",entity_id:该真实ID,many:false}]，
output_node_ids列出全部三个end，不能混用output_node_id。output_schema为上述3字段required、additionalProperties=false，
decision是3个上述字符串enum，reason是string，estimated_amount为number>=0。
不得生成LLM运行节点、SQL/HTTP/script/action/event副作用或固定运行数据；不要造尚不存在的对象ID。
人工拒绝由平台审批服务将实际WorkflowRun置为rejected，不声明已执行批准结束节点。
验收：50自动通过；100边界自动通过；101等人工拒绝；另一次101等人工批准；1000人工批准；1001超预算拒绝输出。
所有这些规则、属性、流程、输入输出与审批权限是明确用户需求，请完整保留，不用相似功能替换。
"""

PLUGIN_INSTRUCTION = """为当前设备维修审批精确发布开发一个可安装Claude Code插件。
覆盖固定发布工作流的真实业务场景：读取当前维修申请estimated_amount，零数据即可调用；
金额不超过100自动通过，超过100且不超过1000等待owner/admin明确人工审批，超过1000输出超预算。
必须先读取server.py受信客户端工具签名与当前场景契约，使用真实inputs
{"maintenance_request":{"request_id":"本次申请标识","estimated_amount":数值}}；插件不能自造审批或外部执行能力。
完整编写skills/run-scenario/SKILL.md、README.md、examples/invoke.py；需要时可增加辅助模块。
示例使用invoke_scenario_capability(kind,key,inputs, idempotency_key=...)，本次合成示例50；
结构化输出保留invocation_id、status、output；queued/running和awaiting_approval均不表示业务完成。
等待审批时显示真实delivery.interactions，读取read_scenario_approval，只有明确用户同意/驳回后
reply_scenario_interaction；传真实interaction revision和唯一message_id；轮询get_scenario_receipt至终态。
人工拒绝诚实显示rejected，不能继续执行或用新幂等键自动重放；批准只恢复同一个工作流。
幂等重试复用同键，输入改变新建键；失效/停用/退役版本拒绝，不切当前草稿。
README解释Python3.12、Claude Code、HTTPS SCENARIO_MCP_URL和场景专属SCENARIO_API_KEY外部配置，
不把凭据写进源码、聊天、命令行或包；安装/静态检查与真实业务执行/人工验收分别说明。
不要宣称本轮编码工具已实际运行业务或安装成功；按公开候选文件步骤提交，最后运行已有静态检查。
"""

CODING_REPAIR_INSTRUCTION = """继续当前实际源码并修复本轮失败，不创建新项目。先inspect当前server.py和场景契约。
当前唯一静态问题是examples第三实参必须直接是ast.Dict，不能先赋给inputs再传变量：使用
invoke_scenario_capability("workflow","WORKFLOW_CAPABILITY_KEY",{"maintenance_request":{"request_id":"SYNTHETIC-MR-001","estimated_amount":50}},idempotency_key="synthetic-mr-001-50")。
这是明确合成示例，不是客户数据。不要在examples加轮询/分支/新imports，保留main+asyncio.run+json.dumps直接真实调用。
将全部三个file工具调用放在同一次模型响应内提交，减少轮数；下一响应validate_plugin_project，再summary，禁止反复检索。8轮预算不能扩大。
SKILL.md frontmatter description改为短于200字符、带YAML引号的一行简短描述；JSON示例及详细说明全部放正文，不能将冒号JSON直接放未引号的description。
真实业务输出只有decision/reason/estimated_amount，decision为auto_approved/approved/over_budget，删除所有臆造route=auto/declined说明。
依次完整提交README.md、examples/invoke.py、skills/run-scenario/SKILL.md这三个文件；本轮不要新增辅助文件。
examples必须使用受信invoke_scenario_capability(kind,key,inputs,idempotency_key=...)，显式幂等键，同输入重试复用。
queued/running/awaiting_approval均不能声称完成。先get_scenario_receipt轮询，实际待审批时read_scenario_approval(invocation_id,interaction_id)。
只有明确人类同意/驳回才reply_scenario_interaction(invocation_id,kind,interaction_id,text,message_id,expected_revision)，不能自造决定、ID、revision。
终态为succeeded/rejected/failed/cancelled/timed_out/indeterminate；indeterminate要求对账，不自动盲重试。
业务结果在真实receipt.output.result.steps里已成功end的result，不是直接output.result.decision。Skill明确这一层，使用真实end输出，诚实展示原回执。
Skill等待轮询必须明确有界并收indeterminate为停止/对账状态；不能用无限while。人工回复只能用read_scenario_approval返回的reply_texts及code，不能造英文Approved/Rejected。
README必须定制说明维修阈值100/1000、6个合成用例、Python3.12、HTTPS外部SCENARIO_MCP_URL与SCENARIO_API_KEY，以及准确scope capability:read/capability:invoke（不是capabilities复数）。
README从包根运行python -m examples.invoke，不能python examples/invoke.py导致找不到server。明确安装/静态检查与实际运行/人工验收的区别，不写凭据，不复写阈值算法。
使用submit_plugin_step逐文件提交后调用validate_plugin_project查看真实诊断，再提交summary；不以解释文本代替文件。
"""


def known_workflow(harness):
    from app.models import BusinessScenario
    from app.services import capability_application_service

    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        capabilities = capability_application_service.list_capabilities(
            db, scene, release_id=harness.release['id'])
    workflows = [item for item in capabilities if item['kind'] == 'workflow']
    if len(workflows) != 1:
        raise ValueError('The fixed scenario must expose exactly one maintenance workflow')
    capability = workflows[0]
    if not capability['readiness']['ready']:
        raise ValueError('The real workflow is not ready')
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(capability['input_schema'])
    validator.validate({'maintenance_request': {'request_id': 'synthetic-request', 'estimated_amount': 50}})
    invalid_inputs = [
        {'maintenance_request': {'request_id': 'synthetic-request', 'estimated_amount': '50'}},
        {'maintenance_request': {'request_id': 'synthetic-request', 'estimated_amount': -1}},
        {'maintenance_request': {'estimated_amount': 50}},
        {'maintenance_request': {'request_id': 'synthetic-request', 'estimated_amount': 50, 'amount': 50}},
    ]
    if any(validator.is_valid(value) for value in invalid_inputs):
        raise ValueError('The workflow lacks the required closed typed ontology input contract')
    harness.checkpoint('workflow_contract', capability=capability)
    return capability


def verify_release_scope(harness):
    from app.models import BusinessScenario
    from app.services import capability_application_service, release_service

    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        deployment, _ = capability_application_service.resolve_deployment(
            db, scene, release_id=harness.release['id'])
        content = release_service._snapshot_for_scenario(db, scene, deployment.snapshot_id).content
        entities = content['entities']
        if len(entities) != 1 or entities[0]['api_name'] != 'entity_maintenance_request':
            raise ValueError('The immutable release contains an unauthorized technical object')
        properties = {item['api_name']: item for item in entities[0]['properties']}
        if set(properties) != {'request_id', 'estimated_amount'}:
            raise ValueError('The immutable maintenance request does not match the fixed two-field scope')
        if not properties['request_id']['is_key'] or not properties['request_id']['is_title']:
            raise ValueError('The immutable request identity lost its governed primary/title contract')
        if any(content.get(key) for key in ('relations', 'actions', 'events', 'functions', 'mappings')):
            raise ValueError('The immutable release contains excluded business resources')
        if len(content['rules']) != 2 or len(content['workflows']) != 1:
            raise ValueError('The immutable release must contain exactly two rules and one workflow')
        frozen = {'entity': entities[0], 'rules': content['rules'], 'workflow': content['workflows'][0],
                  'snapshot_id': deployment.snapshot_id, 'definition_hash': deployment.definition_hash}
    harness.checkpoint('frozen_business_scope_verified', frozen_business_definition=frozen)


def remaining_model_rationale(harness):
    from app.models import BusinessScenario

    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        entities = [item for item in scene.entities if item.api_name == 'entity_maintenance_request']
        if len(scene.entities) != 1 or len(entities) != 1:
            raise ValueError('Remaining construction requires the exact governed maintenance object')
        entity_id = entities[0].id
    return (
        '纠正上轮模型的错误候选，不改变原业务需求：当前本体已全部正确正式晋级，不再建设ontology。'
        '本次任务范围只选择rules和workflows两个任务，依次完成，不新增任何events/actions/技术对象/关系。'
        f'已有正式维修申请对象唯一ID={entity_id}，entity_ref必须直接使用这个ID；'
        '其属性name/api_name均为request_id、estimated_amount，禁止generated引用或重复entities候选。'
        '低额规则condition必须为{"field":"estimated_amount","op":"<=","value":100}；'
        '预算规则condition必须为{"field":"estimated_amount","op":"<=","value":1000}。'
        '两者severity=info、action_on_match=""、trigger_action_refs=[]。'
        '不得使用{"and":[...]}，逻辑组合只允许{"op":"and","conditions":[...]}。'
        '原错误events和配置对象不是用户要求，保留历史审计但排除本次建设来源及要求覆盖；'
        '不能把旧working_draft引用当作原始业务资料要求。rules阶段只生成两条规则；'
        '尚待正常下一阶段生成的workflow不是规则阶段缺项，不应反向重建已确认本体。'
        '\n完整不变业务要求（复用上述已正式对象，不重新创建）：\n' + MODELING_PROMPT)


def activate_governed_capabilities(harness):
    from app.models import BusinessScenario

    if harness.release:
        return
    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        if len(scene.rules) != 2 or len(scene.workflows) != 1:
            raise ValueError('The governed maintenance dependency closure is not complete')
        capabilities = [{'kind': 'rule', 'key': item.id} for item in scene.rules]
        capabilities += [{'kind': 'workflow', 'key': scene.workflows[0].id}]
    harness.enable_capabilities(capabilities)


def workflow_model_rationale(harness):
    from app.models import BusinessScenario

    with harness.actor_db() as db:
        scene = db.get(BusinessScenario, harness.scene_id)
        if len(scene.entities) != 1 or len(scene.rules) != 2:
            raise ValueError('Workflow construction requires the already governed dependency closure')
        entity_id = scene.entities[0].id
        references = {}
        for amount in (100, 1000):
            matching = [rule for rule in scene.rules
                if rule.entity_id == entity_id and rule.condition ==
                {'field': 'estimated_amount', 'op': '<=', 'value': amount}]
            if len(matching) != 1:
                raise ValueError('The existing rules do not implement the two exact declared thresholds')
            references[amount] = matching[0].id
    return (
        '当前唯一需要建设的任务是workflows，scope=workflow。只生成一个完整工作流候选。'
        '上轮模型已生成工作流但服务端保存出现lifecycle冲突，未形成正式流程；本轮经修复后正常重建。'
        '所有本体和规则已正确晋级，禁止重复创建entities/properties/rules/events/actions/relations。'
        f'正式维修申请对象entity_id={entity_id}；'
        f'低额规则resource_ref={references[100]}，预算规则resource_ref={references[1000]}。'
        '这是现有定义精确ID，规则节点不得使用generated key或自行重写算法。'
        '输入是{"maintenance_request":{"request_id":"本次申请标识","estimated_amount":非负数}}；'
        'request_id和estimated_amount是现有正式对象直接属性。input_binding.path=maintenance_request、'
        f'entity_id={entity_id}、many=false。\n'
        + MODELING_PROMPT[MODELING_PROMPT.index('确定性工作流：'):])


def create_workflow_draft(harness):
    from fastapi import Response
    from app.routers import assistant
    from app.schemas import AssistantChatRequest
    from scenario_e2e_construction import HarnessBlocked, plain

    with harness.actor_db() as db:
        from app.models import BusinessScenario
        if db.get(BusinessScenario, harness.scene_id).workflows:
            raise ValueError('A new workflow draft must not recreate an existing governed workflow')
    request_id = uuid4().hex
    rationale = workflow_model_rationale(harness)
    harness.checkpoint('explicit_workflow_creation_started', workflow_request_id=request_id,
        workflow_creation_mode='normal workflow draft; no existing workflow candidate mutation')
    with harness.actor_db() as db:
        result = plain(assistant.chat(AssistantChatRequest(
            message=rationale, request_id=request_id,
            scenario_id=harness.scene_id, thread_id=harness.report['thread_id'],
            llm_config_id=harness.model['id'], page='业务场景', path=harness.path,
            mode='draft', draft_kind='workflow'), db=db))
    harness.checkpoint('explicit_workflow_creation_reply', proposal=result['proposal'],
        construction_reply=result['reply'], construction_questions=result['questions'])
    if result['proposal']:
        return result['proposal']
    with harness.actor_db() as db:
        jobs = assistant.list_thread_compilation_jobs(harness.report['thread_id'], Response(),
            scenario_id=harness.scene_id, page='业务场景', path=harness.path, db=db)
    if jobs and plain(jobs[0])['status'] == 'running':
        return harness.wait_compilation(plain(jobs[0])['id'])
    raise HarnessBlocked('The explicit workflow creation did not produce a candidate')


def repair_coding(harness):
    from app.plugin_coding_schemas import PluginCodingUpdate
    from app.routers import plugin_coding
    from scenario_e2e_construction import plain

    workspace_id = harness.report['workspace_id']
    with harness.actor_db() as db:
        workspace = plain(plugin_coding.get(workspace_id, db=db))
        request_id = uuid4().hex
        instruction = CODING_REPAIR_INSTRUCTION.replace('WORKFLOW_CAPABILITY_KEY', harness.report['capability']['key'])
        result = plain(plugin_coding.revise(PluginCodingUpdate(
            expected_revision=workspace['revision'], request_id=request_id, action='generate',
            base_files_hash=workspace['files_hash'], instruction=instruction),
            workspace_id=workspace_id, db=db))
    harness.checkpoint('coding_explicit_repair', coding_repair_request_id=request_id,
        coding_repair_instruction=instruction, prior_validation=workspace['validation'], workspace=result)


def replace_source_snippet(source, old, new):
    if source.count(old) != 1:
        raise ValueError('The reviewed source changed; inspect the new version before correcting it')
    return source.replace(old, new)


def corrected_model_source(files):
    source = files['examples/invoke.py']
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == 'invoke_scenario_capability']
    if len(calls) != 1 or not isinstance(calls[0].args[2], ast.Name):
        raise ValueError('Only the reviewed example input variable needs a mechanical correction')
    argument = calls[0].args[2]
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == argument.id and isinstance(node.value, ast.Dict)]
    if len(assignments) != 1:
        raise ValueError('The reviewed example does not contain a single explicit synthetic input dictionary')
    ast.literal_eval(assignments[0].value)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[argument.lineno - 1] + argument.col_offset
    end = offsets[argument.end_lineno - 1] + argument.end_col_offset
    source = source[:start] + ast.unparse(assignments[0].value) + source[end:]
    skill = files['skills/run-scenario/SKILL.md']
    skill = re.sub(r'^description: .*$',
        'description: "Use the pinned maintenance workflow, typed current input, idempotent retries and explicit human approval."',
        skill, count=1, flags=re.MULTILINE)
    skill = replace_source_snippet(skill,
        '| output.result | Structured business fields: decision, reason, estimated_amount |',
        '| output.result | Workflow execution trace; successful end output is in result.steps[].result |')
    skill = replace_source_snippet(skill, '**Business outputs (from receipt.output.result):**',
        '**Business outputs:** Read the single successful end step in `receipt.output.result.steps` '
        'with `contract_validation="passed"`; its `result` contains the following fields. '
        'A rejected approval has no successful end output.')
    skill = replace_source_snippet(skill, 'user_decision_text,   # e.g., "Approved" or "Rejected"',
        'user_decision_text,   # User explicitly chooses the advertised approval["reply_texts"]; retain its code')
    skill = replace_source_snippet(skill,
        '**Terminal statuses:** succeeded, rejected, failed, cancelled, timed_out',
        '**Stop polling at:** succeeded, rejected, failed, cancelled, timed_out, indeterminate. '
        'Indeterminate requires reconciliation and does not prove business completion.')
    skill = replace_source_snippet(skill,
        '   while receipt["status"] not in ("succeeded", "rejected", "failed", "cancelled", "timed_out"):\n'
        '       await asyncio.sleep(2)\n       receipt = await get_scenario_receipt(invocation_id)',
        '   for attempt in range(90):\n'
        '       if receipt["status"] in ("succeeded", "rejected", "failed", "cancelled", "timed_out", "indeterminate"):\n'
        '           break\n       await asyncio.sleep(2)\n       receipt = await get_scenario_receipt(invocation_id)\n'
        '   else:\n       print("Still pending; retain invocation_id and check again later. Do not re-invoke.")')
    readme = files['README.md'].replace('capabilities:read', 'capability:read').replace('capabilities:invoke', 'capability:invoke')
    readme = replace_source_snippet(readme, '# Scenario plugin\n', '# Maintenance approval plugin\n')
    readme += ('\nThis pinned workflow consumes the current maintenance_request with request_id and non-negative '
        'estimated_amount. Amounts <=100 finish automatically; 100<amount<=1000 wait for owner/admin approval; '
        'amounts >1000 finish with decision=over_budget. The platform computes these rules. '
        'The end output contains decision, reason and estimated_amount.\n'
        'Synthetic platform acceptance covers 50, 100, 101/reject, 101/approve, 1000/approve and 1001. '
        'These are explicit test values, not runtime defaults. From the extracted package root, '
        'run `python -m examples.invoke` for the synthetic 50 example. A queued receipt is not completed business work.\n')
    return {'examples/invoke.py': source, 'skills/run-scenario/SKILL.md': skill, 'README.md': readme}


def save_reviewed_corrections(harness):
    from app.plugin_coding_schemas import PluginCodingUpdate
    from app.routers import plugin_coding
    from scenario_e2e_construction import plain

    workspace_id = harness.report['workspace_id']
    with harness.actor_db() as db:
        workspace = plain(plugin_coding.get(workspace_id, db=db))
        original = {item['path']: item['content'] for item in workspace['files']}
        corrected = corrected_model_source(original)
        differences = {path: ''.join(unified_diff(original[path].splitlines(keepends=True),
            content.splitlines(keepends=True), fromfile='ai/' + path, tofile='reviewed/' + path))
            for path, content in corrected.items()}
        result = plain(plugin_coding.revise(PluginCodingUpdate(expected_revision=workspace['revision'],
            request_id=uuid4().hex, action='save', base_files_hash=workspace['files_hash'],
            files=[{'path': path, 'content': content} for path, content in corrected.items()]),
            workspace_id=workspace_id, db=db))
    if result['validation'] or result['phase'] != 'draft' or result['active_run_id'] is not None:
        raise ValueError('Reviewed source was saved but has not passed the real project validation')
    harness.checkpoint('human_source_mechanically_corrected', workspace=result,
        source_files={item['path']: item['content'] for item in result['files']},
        human_source_edit={'actor': harness.user_id, 'files_hash': result['files_hash'],
            'expected_revision': workspace['revision'], 'diff': differences,
            'original_files': {path: original[path] for path in corrected},
            'note': 'Reviewed existing AI source; corrected inline typed dictionary, actual end result location, '
                'advertised human replies, bounded polling/reconciliation and exact credential scopes via normal CAS save.'})


def receipt_and_run(harness, invocation_id):
    from app.models import CapabilityInvocation, WorkflowRun
    from app.services import capability_application_service

    with harness.actor_db() as db:
        receipt = capability_application_service.get_receipt(db, harness.actor, invocation_id)
        row = db.get(CapabilityInvocation, invocation_id)
        run_id = receipt.get('output', {}).get('workflow_run_id')
        run = db.get(WorkflowRun, run_id)
        if (row is None or run is None or row.scenario_id != harness.scene_id
                or run.scenario_id != harness.scene_id or run.release_id != harness.release['id']):
            raise ValueError('Receipt does not belong to this fixed maintenance scenario')
        result = {'invocation_id': invocation_id, 'stored_invocation_status': row.status,
            'workflow_run_id': run.id, 'workflow_status': run.status, 'attempt': run.attempt,
            'result': run.result or {}, 'error': run.error or '',
            'approved_node_ids': run.approved_node_ids or []}
    return receipt, result


def wait_case(harness, invocation_id, *, allow_approval=False, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt, result = receipt_and_run(harness, invocation_id)
        status = result['workflow_status']
        if status in {'succeeded', 'failed', 'rejected', 'cancelled', 'timed_out', 'indeterminate'}:
            return receipt, result
        if status == 'awaiting_approval' and allow_approval:
            return receipt, result
        time.sleep(2)
    raise ValueError('The known maintenance run did not reach the required state in time')


def human_reply(harness, receipt, *, approved):
    from app.channel_interaction_schemas import ChannelReplyIn
    from app.services import channel_interaction_service

    advertised = [item for item in receipt['delivery']['interactions']
        if item['kind'] == 'workflow_approval' and item['status'] == 'pending']
    if len(advertised) != 1:
        raise ValueError('The receipt must advertise exactly one pending maintenance approval')
    interaction = advertised[0]
    with harness.actor_db() as db:
        actual = channel_interaction_service.read_approval(db, harness.actor, interaction['id'])
        if actual['recipient_roles'] != ['owner', 'admin'] and set(actual['recipient_roles']) != {'owner', 'admin'}:
            raise ValueError('The generated approval lost the owner/admin audience restriction')
        reply = ChannelReplyIn(text=('同意 ' if approved else '驳回 ') + actual['code'],
            message_id='maintenance-acceptance-' + uuid4().hex, expected_revision=actual['revision'])
        result = channel_interaction_service.reply_approval(db, harness.actor, actual['id'], reply)
        db.commit()
    return {'request': actual, 'result': result, 'approved': approved,
        'authorization': 'current user explicitly authorized synthetic approval branch acceptance'}


def end_output(result):
    ends = [item for item in result['result'].get('steps', [])
        if item.get('type') == 'end' and item.get('status') == 'success']
    if len(ends) != 1 or ends[0].get('contract_validation') != 'passed':
        raise ValueError('The terminal branch did not produce its declared business output')
    return ends[0]['node'], ends[0]['result']


def execute_cases(harness):
    capability = known_workflow(harness)
    cases = []
    specifications = [
        ('success', 50, None, 'succeeded', 'auto_approved'),
        ('boundary', 100, None, 'succeeded', 'auto_approved'),
        ('failure', 101, False, 'rejected', None),
        ('human_approved', 101, True, 'succeeded', 'approved'),
        ('budget_boundary', 1000, True, 'succeeded', 'approved'),
        ('over_budget', 1001, None, 'succeeded', 'over_budget'),
    ]
    completed = {item['role']: item for item in harness.report.get('business_cases', [])}
    for role, amount, approval, expected, decision in specifications:
        if role in completed:
            cases.append(completed[role])
            continue
        inputs = {'maintenance_request': {'request_id': 'synthetic-maintenance-' + role, 'estimated_amount': amount}}
        progress = harness.report.get('maintenance_case_progress') or {}
        if progress.get('role') == role:
            key, initial = progress['idempotency_key'], progress['initial_receipt']
        else:
            key = 'maintenance-' + role + '-' + uuid4().hex
            initial = harness.invoke('workflow', capability['key'], inputs, idempotency_key=key)
            progress = {'role': role, 'idempotency_key': key, 'inputs': inputs, 'initial_receipt': initial}
            harness.checkpoint('maintenance_case_started', maintenance_case_progress=progress)
        receipt, result = wait_case(harness, initial['invocation_id'], allow_approval=approval is not None)
        evidence = {'role': role, 'inputs': inputs, 'idempotency_key': key,
            'decision': ('approve' if approval else 'reject') if approval is not None else None,
            'initial_status': initial['status'], **result}
        if approval is not None:
            if result['workflow_status'] not in {'awaiting_approval', expected}:
                raise ValueError('The 101 maintenance branch did not pause for human approval')
            if result['workflow_status'] == 'awaiting_approval':
                evidence['approval'] = human_reply(harness, receipt, approved=approval)
                progress['approval'] = evidence['approval']
                harness.checkpoint('maintenance_approval_replied', maintenance_case_progress=progress)
            elif progress.get('approval'):
                evidence['approval'] = progress['approval']
            else:
                raise ValueError('The approval branch lacks a recorded authorized human reply')
            receipt, result = wait_case(harness, initial['invocation_id'])
            evidence.update(result)
        if result['workflow_status'] != expected or receipt['status'] != expected:
            raise ValueError(f'Maintenance branch {role} produced an unexpected terminal status')
        evidence.update(projected_receipt_status=receipt['status'], terminal_output=None, output_node_id=None)
        if decision is not None:
            node_id, output = end_output(result)
            if output.get('decision') != decision or output.get('estimated_amount') != amount:
                raise ValueError(f'Maintenance branch {role} did not implement the fixed business rule')
            evidence.update(output_node_id=node_id, terminal_output=output)
        replay = harness.invoke('workflow', capability['key'], inputs, idempotency_key=key)
        if replay['invocation_id'] != initial['invocation_id']:
            raise ValueError('An identical retry created a second maintenance workflow')
        evidence['same_input_replay_invocation_id'] = replay['invocation_id']
        cases.append(evidence)
        harness.checkpoint('execution_cases', business_cases=cases, maintenance_case_progress=None)
    return capability, cases


def package_cases(capability, evidence):
    return [{'kind': 'workflow', 'key': capability['key'], 'role': item['role'],
        'invocation_id': item['invocation_id'], 'expected_status': item['stored_invocation_status']}
        for item in evidence if item['role'] in {'success', 'boundary', 'failure'}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario-id', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--stage', choices=['model', 'execute', 'coding', 'review'], required=True)
    parser.add_argument('--review-note', default='')
    parser.add_argument('--retry-model', action='store_true')
    parser.add_argument('--resolve-model', action='store_true')
    parser.add_argument('--resolve-remaining', action='store_true')
    parser.add_argument('--resolve-workflow', action='store_true')
    parser.add_argument('--create-workflow', action='store_true')
    parser.add_argument('--repair-coding', action='store_true')
    parser.add_argument('--correct-coding', action='store_true')
    args = parser.parse_args()
    from scenario_e2e_construction import ScenarioHarness

    harness = ScenarioHarness(args.scenario_id, args.username, args.report, SPEC)
    harness.checkpoint('fixed_specification_loaded', spec=SPEC)
    if args.stage == 'model':
        if args.retry_model:
            previous = [*harness.report.get('previous_construction_requests', []),
                harness.report.get('construction_request_id')]
            harness.checkpoint('explicit_model_retry', construction_request_id=uuid4().hex,
                previous_construction_requests=previous)
        if args.resolve_model:
            harness.resolve_model('明确本次需求纠正并重新规划，保留本场景原候选审计但纠正错误超范围对象：\n' + MODELING_PROMPT)
        if args.resolve_remaining:
            harness.resolve_model(remaining_model_rationale(harness))
        if args.resolve_workflow:
            harness.resolve_model(workflow_model_rationale(harness))
        if args.create_workflow:
            create_workflow_draft(harness)
        harness.build_model(MODELING_PROMPT)
        activate_governed_capabilities(harness)
        harness.govern_and_release()
        verify_release_scope(harness)
    elif args.stage == 'execute':
        execute_cases(harness)
    else:
        capability = known_workflow(harness)
        selection = [{'kind': 'workflow', 'key': capability['key']}]
        if args.stage == 'coding':
            if args.repair_coding:
                repair_coding(harness)
            if args.correct_coding:
                save_reviewed_corrections(harness)
            harness.start_coding(selection, PLUGIN_INSTRUCTION)
        else:
            if not args.review_note.strip():
                raise ValueError('Read and review the actual model-authored source before publishing')
            evidence = harness.report['business_cases']
            harness.code_review_publish(selection, package_cases(capability, evidence),
                PLUGIN_INSTRUCTION, review_note=args.review_note)
            from app.routers import plugin_coding
            from scenario_e2e_construction import plain
            with harness.actor_db() as db:
                current = plain(plugin_coding.get(harness.report['workspace_id'], db=db))
            harness.checkpoint('application_chain_completed', application_chain_completed=True,
                workspace=current, source_files={item['path']: item['content'] for item in current['files']},
                publication_scope='explicit local acceptance distribution',
                remaining_verification=['root independent installation', 'root HTTPS REST/MCP', 'root browser'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Maintenance acceptance failed: ' + type(error).__name__)
        raise SystemExit(1) from None
