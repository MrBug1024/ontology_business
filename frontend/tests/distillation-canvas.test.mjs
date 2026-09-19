import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'
import ts from 'typescript'
import { emptyDistillationDocument } from '../src/utils/businessDistillation.ts'

const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const modules = new Map()
function componentModule(url) {
  if (modules.has(url.href)) return modules.get(url.href)
  const text = readFileSync(url, 'utf8')
  const { descriptor } = parse(text, { filename: url.pathname })
  const script = compileScript(descriptor, { id: url.pathname, inlineTemplate: true })
  let source = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
  source = source.replace(/from (["'])([^"']+)\1/g, (_match, quote, name) => {
    let target
    if (name.endsWith('.vue')) target = componentModule(new URL(name, url))
    else if (name === '@/api/businessDistillation') target = encode('export const businessDistillationApi = new Proxy({}, { get: (_target, key) => (...args) => globalThis.__discoveryTestApi[key](...args) })')
    else if (name === '@/utils/businessDistillation') target = new URL('../src/utils/businessDistillation.ts', import.meta.url).href
    else target = import.meta.resolve(name)
    return `from ${quote}${target}${quote}`
  })
  const result = encode(source)
  modules.set(url.href, result)
  return result
}
const { default: Canvas } = await import(componentModule(new URL('../src/components/distillation/DistillationCanvas.vue', import.meta.url)))

function mount(document = emptyDistillationDocument(), overrides = {}) {
  let focused
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null, focus() { focused = this } })
  const remove = child => { const siblings = child.parent?.children; if (siblings) { const index = siblings.indexOf(child); if (index >= 0) siblings.splice(index, 1) } }
  const renderer = createRenderer({
    createElement: type => node(type), createText: text => node('text', text), createComment: () => node('comment'),
    setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
    patchProp: (target, key, _old, value) => { target.props[key] = value },
    insert(child, parent, anchor) { remove(child); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove, parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  })
  const root = node('root'), asks = [], publications = []
  const app = renderer.createApp(Canvas, { document, embedded: true, dirty: false, canEdit: true, canPublish: false, onAsk: message => asks.push(message), onPublish: () => publications.push(true), ...overrides })
  app.component('el-button', { setup: (_props, context) => () => h('button', context.attrs, context.slots.default?.()) })
  app.component('el-icon', { setup: (_props, context) => () => h('i', context.attrs, context.slots.default?.()) })
  app.mount(root)
  const all = () => { const found = []; function visit(value) { found.push(value); value.children.forEach(visit) }; visit(root); return found }
  const textOf = target => [target.text, ...target.children.map(textOf)].join('')
  const button = label => all().find(target => target.type === 'button' && textOf(target) === label)
  const tabs = () => all().filter(target => target.props.role === 'tab')
  const choose = async label => { const target = button(label); assert.ok(target, `Missing button: ${label}`); target.props.onClick(); await nextTick() }
  return { all, text: () => textOf(root), button, tabs, choose, asks, publications, focused: () => focused, stop: () => app.unmount() }
}

test('conclusion tabs have keyboard navigation, one tab stop, and a labelled active panel', async () => {
  const view = mount()
  assert.deepEqual(view.tabs().map(tab => tab.text.trim() || tab.children.filter(child => child.type === 'text').map(child => child.text.trim()).join('')), ['业务价值', 'ER', '流程', '血缘', '历史案例', '证据', '待澄清'])
  assert.equal(view.tabs().filter(tab => tab.props.tabindex === 0).length, 1)
  let prevented = false
  await view.tabs()[0].props.onKeydown({ key: 'ArrowLeft', preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(view.tabs()[6].props['aria-selected'], true)
  assert.equal(view.focused().props.id, view.tabs()[6].props.id)
  await view.tabs()[6].props.onKeydown({ key: 'Home', preventDefault() {} })
  await view.tabs()[0].props.onKeydown({ key: 'ArrowRight', preventDefault() {} })
  assert.equal(view.tabs()[1].props['aria-selected'], true)
  const panel = view.all().find(target => target.props.role === 'tabpanel')
  assert.equal(panel.props['aria-labelledby'], view.tabs()[1].props.id)
  assert.equal(view.tabs()[1].props['aria-controls'], panel.props.id)
  view.stop()
})

test('empty artifact tabs contain only concise empty states with no modeling forms or investigation buttons', async () => {
  const document = emptyDistillationDocument(), before = structuredClone(document)
  const view = mount(document)
  for (const label of ['业务价值', 'ER', '流程', '血缘', '历史案例', '证据', '待澄清']) {
    await view.choose(label)
    const panel = view.all().find(target => target.props.role === 'tabpanel')
    const descendants = []
    const visit = value => { descendants.push(value); value.children.forEach(visit) }
    visit(panel)
    assert.equal(descendants.filter(value => ['input', 'select', 'textarea', 'button', 'svg'].includes(value.type)).length, 0)
    const content = descendants.map(value => value.text).join('')
    assert.match(content, /暂无/)
    assert.ok(content.length < 60)
  }
  assert.deepEqual(document, before)
  assert.deepEqual(view.asks, [])
  assert.deepEqual(view.publications, [])
  assert.equal(view.button('保存到资料库').props.disabled, true)
  view.stop()
})

test('questions display AI findings without topic signoff forms or automatic changes', async () => {
  const document = emptyDistillationDocument()
  document.open_questions = ['结果应该由谁验收？']
  const view = mount(document, { canEdit: false })
  view.tabs()[6].props.onClick()
  await nextTick()
  assert.match(view.text(), /结果应该由谁验收/)
  assert.doesNotMatch(view.text(), /逐项核对|确认当前|采用 AI 建议后/)
  assert.equal(view.all().filter(target => ['input', 'select', 'textarea'].includes(target.type)).length, 0)
  assert.deepEqual(view.asks, [])
  view.stop()
})

test('unconfirmed entity relationships display uncertainty without a definite cardinality', async () => {
  const document = emptyDistillationDocument()
  document.entities = [{ key: 'request', name: '请求', description: '', attributes: [] }, { key: 'result', name: '结果', description: '', attributes: [] }]
  document.relations = [{ source: 'request', target: 'result', label: '候选关联', cardinality: 'unconfirmed', rationale: '尚无成对样本' }]
  const view = mount(document)
  await view.choose('ER')
  assert.match(view.text(), /基数待核对/)
  assert.doesNotMatch(view.text(), /一对一|一对多|多对多/)
  assert.ok(view.all().some(target => target.type === 'path' && target.props['stroke-dasharray'] === '5 4'))
  view.stop()
})

test('a saved target process is shown as an artifact even before value statements exist', async () => {
  const document = emptyDistillationDocument()
  document.to_be.nodes = [{ key: 'verify', name: '核对结果', owner: '', outcome: '', evidence_refs: [] }]
  const view = mount(document, { canPublish: true })
  await view.choose('流程')
  assert.match(view.text(), /核对结果/)
  assert.match(view.text(), /已保存的阶段产物/)
  assert.doesNotMatch(view.text(), /等待产物|暂无流程图谱/)
  assert.equal(view.button('保存到资料库').props.disabled, false)
  view.stop()
})

test('evidence view presents scope, limitations, confidence, and readable citations without internal identifiers', async () => {
  const document = emptyDistillationDocument()
  document.evidence = [{ key: 'synthetic-evidence-key', title: '访谈记录', role: 'reference', kind: 'material', summary: '仍有争议', coverage: '本次访谈', limitations: '尚无系统记录', data_source_id: 'synthetic-source-id' }]
  document.assertions = [{ key: 'synthetic-claim-id', statement: '流程存在重复确认', status: 'hypothesis', evidence_refs: ['synthetic-evidence-key'] }]
  const view = mount(document)
  await view.choose('证据')
  assert.match(view.text(), /本次访谈/)
  assert.match(view.text(), /尚无系统记录/)
  assert.match(view.text(), /待验证假设/)
  assert.match(view.text(), /依据：访谈记录/)
  assert.doesNotMatch(view.text(), /synthetic-/)
  assert.equal(view.all().filter(target => ['input', 'select', 'textarea'].includes(target.type)).length, 0)
  view.stop()
})

test('process conclusions preserve readable relationships, improvements, and source citations', async () => {
  const document = emptyDistillationDocument()
  document.evidence = [{ key: 'proof', title: '流程记录', kind: 'material', role: 'process' }]
  document.as_is = { nodes: [{ key: 'old', name: '人工检查', owner: '受理人', outcome: '检查结果', evidence_refs: ['proof'] }], edges: [] }
  document.to_be = { nodes: [{ key: 'start', name: '接收', owner: '受理人', outcome: '已收件', evidence_refs: ['proof'] }, { key: 'done', name: '确认', owner: '请求人', outcome: '已确认', evidence_refs: [] }], edges: [{ source: 'start', target: 'done', label: '移交' }] }
  document.improvements = [{ key: 'improvement', existing_node_key: 'old', decision: 'replace', rationale: '避免重复检查', expected_benefit: '缩短等待' }]
  const view = mount(document)
  await view.choose('流程')
  assert.match(view.text(), /依据：流程记录/)
  await view.choose('目标流程')
  assert.match(view.text(), /接收 → 确认.*移交/)
  assert.match(view.text(), /替代 · 人工检查/)
  assert.match(view.text(), /缩短等待/)
  assert.match(view.all().find(target => target.props.role === 'img').props['aria-label'], /文字说明/)
  view.stop()
})
