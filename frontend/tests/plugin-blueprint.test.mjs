import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, ref } from 'vue'

const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const source = compileScript(parse(readFileSync(new URL('../src/components/plugin-coding/PluginScenarioBlueprint.vue', import.meta.url), 'utf8')).descriptor, { id: 'blueprint-test', inlineTemplate: true, templateOptions: { compilerOptions: { hoistStatic: false } } }).content
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText.replace(/from (["'])vue\1/g, `from '${vueUrl}'`)
const { default: Blueprint } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)

function mount(initial) {
  const props = ref(initial)
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const remove = child => { const parent = child.parent; if (parent) parent.children.splice(parent.children.indexOf(child), 1) }
  const renderer = createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: () => node('#comment'),
    insert(child, parent, anchor) { remove(child); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove, setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
    patchProp: (target, key, old, value) => { target.props[key] = value }, parentNode: target => target.parent,
    nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  })
  const root = node('root')
  const app = renderer.createApp({ setup: () => () => h(Blueprint, props.value) })
  app.mount(root)
  const all = () => { const items = []; const visit = item => { items.push(item); item.children.forEach(visit) }; visit(root); return items }
  const textOf = item => [item.text, ...item.children.map(textOf)].join('')
  return { props, all, textOf, text: () => textOf(root), stop: () => app.unmount() }
}

const refOf = (kind, key) => ({ kind, key })
const capability = (kind, key, name, overrides = {}) => ({ kind, key, name, description: `${name}的正式业务说明`, selected: false, available: kind !== 'event', dependency: false, invocation_supported: kind !== 'event', invocation_authorized: true, enabled: true, ready: true, semantic: { role: '业务角色', runtime_kind: null, trigger_type: null, node_counts: [], requires_approval: false, object_keys: [], dependencies: [], input_bindings: [], output_node_keys: [] }, ...overrides })
function blueprint() {
  const rule = capability('rule', 'rule-internal', '费用合规', { invocation_authorized: false })
  const workflow = capability('workflow', 'workflow-internal', '采购审批', { semantic: { ...rule.semantic, requires_approval: true, object_keys: ['object-internal'], dependencies: [refOf('rule', 'rule-internal'), refOf('event', 'event-internal')], input_bindings: [{ path: 'request', object_key: 'object-internal', many: false, partial: false }], output_node_keys: ['end-internal'] } })
  const event = capability('event', 'event-internal', '审批完成事件', { dependency: true, invocation_authorized: false })
  return {
    version: 'scenario-capability-blueprint.v1', completeness: 'complete_authorized_projection',
    scenario: { id: 'scene-internal', name: '采购场景的固定目标', description: '按申请金额分流审批 <img src=x onerror=alert(1)>' },
    deployment: { definition_source: 'release', release_id: 'release-internal', snapshot_id: 'snapshot-internal', definition_hash: 'a'.repeat(64) },
    stages: [{ key: 'materials', label: '场景资料', contribution: '资料支持术语与业务约束理解。', boundary: '这是阶段职责，不证明发布使用某份资料。' }],
    ontology: { objects: [{ key: 'object-internal', api_name: 'purchase_request', name: '采购申请', description: '申请的身份与金额', properties: [{ key: 'property-internal', api_name: 'request_id', name: '申请编号', data_type: 'string', description: '唯一身份', is_key: true, is_required: true, is_title: true, is_enum: false, enum_values: [] }] }], relations: [] },
    capabilities: [rule, workflow, event], coverage: { selected: [], available: [refOf('rule', rule.key), refOf('workflow', workflow.key)], dependencies: [], unselected_available: [refOf('rule', rule.key), refOf('workflow', workflow.key)] },
  }
}
function profile() {
  return {
    version: 'scenario-plugin-delivery-profile.v1', host: { key: 'claude_code', label: 'Claude Code', scope: 'host_specific' },
    standards: [{ key: 'agent_skills', label: 'Agent Skills', purpose: '方法包格式不代表执行权限。', url: 'https://agentskills.io/specification' }],
    components: [{ key: 'skill', label: '入口技能', purpose: '发现选定能力并询问输入。', required: true, supported: true }, { key: 'client_scripts', label: '客户端脚本', purpose: '只做输入与受信调用编排。', required: false, supported: true }, { key: 'hooks', label: '宿主 hooks', purpose: '当前平台交付范围禁用。', required: false, supported: false }],
    boundaries: [{ key: 'coding_extensions', label: '编码 AI 扩展', purpose: '开发指令和只读 MCP 不自动装入发布插件。' }], platform_rules: [{ key: 'evidence', label: '完成证据', purpose: '源码校验、真实调用和安装分别需要证据。' }], protected_references: ['references/scenario-blueprint.json'],
  }
}

test('blueprint shows frozen semantics and independent permission, readiness and selection facts', () => {
  const image = blueprint()
  const view = mount({ blueprint: image, selectedCapabilities: [image.capabilities[0]], draftSelection: true })
  try {
    const disclosure = view.all().find(item => item.props['aria-label'] === '场景能力画像与插件交付')
    assert.equal(disclosure.type, 'details')
    assert.equal(disclosure.props.open, undefined)
    assert.match(view.text(), /采购场景的固定目标/)
    assert.match(view.text(), /身份属性 · 必填 · 显示标题/)
    assert.match(view.text(), /目录可发现 2 项能力/)
    assert.match(view.text(), /1 项拟封装能力/)
    assert.match(view.text(), /尚未代表已创建插件/)
    assert.match(view.text(), /无调用权限 · 已启用 · 运行条件齐备/)
    assert.match(view.text(), /<img src=x onerror=alert\(1\)>/)
    assert.equal(view.all().some(item => item.type === 'img' || 'innerHTML' in item.props), false)
    assert.doesNotMatch(view.text(), /scene-internal|release-internal|snapshot-internal|object-internal|property-internal|rule-internal|a{64}/)
  } finally { view.stop() }
})

test('selection preview changes without exposing events as selected invocation tools', async () => {
  const image = blueprint()
  const view = mount({ blueprint: image, selectedCapabilities: [image.capabilities[1]], draftSelection: true })
  try {
    const selectedList = () => view.all().find(item => item.props.class === 'capabilities')
    assert.match(view.textOf(selectedList()), /工作流 · 采购审批/)
    assert.doesNotMatch(view.textOf(selectedList()), /事件 · 审批完成事件/)
    assert.match(view.text(), /事件 · 审批完成事件/)
    assert.match(view.text(), /事件是事件语义，不作为插件调用工具/)
    assert.match(view.text(), /输入 request 绑定一个采购申请，按完整对象契约校验/)
    assert.match(view.text(), /通过 1 个流程输出节点返回结果/)
    view.props.value = { ...view.props.value, selectedCapabilities: [image.capabilities[0]] }
    await nextTick()
    assert.match(view.textOf(selectedList()), /规则 · 费用合规/)
    assert.doesNotMatch(view.textOf(selectedList()), /采购审批/)
    assert.doesNotMatch(view.text(), /事件 · 审批完成事件/)
  } finally { view.stop() }
})

test('zero ontology is a valid capability picture and missing legacy picture remains explicit', async () => {
  const image = blueprint()
  image.ontology = { objects: [], relations: [] }
  const view = mount({ blueprint: image, selectedCapabilities: [image.capabilities[0]] })
  try {
    assert.match(view.text(), /未定义本体对象。纯计算、规则等能力仍可/)
    view.props.value = { selectedCapabilities: [], legacyWorkspace: true }
    await nextTick()
    assert.match(view.text(), /旧编码任务尚未保存画像/)
    assert.match(view.text(), /业务蒸馏/)
    assert.match(view.text(), /不证明当前发布曾使用某份蒸馏成果/)
    assert.doesNotMatch(view.text(), /固定版本的业务目标|采购场景的固定目标/)
    view.props.value = { selectedCapabilities: [], loading: true }
    await nextTick()
    assert.match(view.text(), /正在读取固定版本的场景画像/)
    assert.doesNotMatch(view.text(), /旧编码任务尚未保存画像/)
  } finally { view.stop() }
})

test('delivery profile distinguishes supported components, actual files, official standards and coding extensions', () => {
  const view = mount({ blueprint: blueprint(), profile: profile(), selectedCapabilities: [], files: [{ path: 'README.md', content: '', previous: '', editable: true }] })
  try {
    assert.match(view.text(), /安装目标：Claude Code/)
    assert.match(view.text(), /当前项目包含 1 个文件/)
    assert.match(view.text(), /支持范围不等于这些文件已经生成或执行/)
    const components = view.all().find(item => item.props.class === 'components')
    assert.match(view.textOf(components), /入口技能必需组成/)
    assert.match(view.textOf(components), /客户端脚本按需定制/)
    assert.match(view.textOf(components), /宿主 hooks本平台未支持/)
    assert.match(view.text(), /开发指令和只读 MCP 不自动装入发布插件/)
    assert.match(view.text(), /项目中尚无此文件/)
    const link = view.all().find(item => item.type === 'a')
    assert.equal(link.props.href, 'https://agentskills.io/specification')
    assert.equal(link.props.rel, 'noopener noreferrer')
    assert.equal(link.props.target, '_blank')
    assert.equal(view.all().some(item => 'innerHTML' in item.props), false)
  } finally { view.stop() }
})
