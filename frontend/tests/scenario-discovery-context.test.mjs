import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const routerUrl = new URL('../node_modules/vue-router/dist/vue-router.mjs', import.meta.url).href
const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const sourceOf = file => readFileSync(new URL(file, import.meta.url), 'utf8')
const fakeApi = {}
globalThis.__scenarioDiscoveryContextApi = fakeApi
const apiUrl = encode('export const scenarioDiscoveryContextApi = globalThis.__scenarioDiscoveryContextApi')
const eventsUrl = encode(transpile(sourceOf('../src/utils/scenarioAdvisorEvents.ts')))
const events = await import(eventsUrl)
function replaceImports(source) {
  return source.replace(/from (["'])([^"']+)\1/g, (_match, quote, name) => {
    const targets = { vue: vueUrl, 'vue-router': routerUrl, '@/api/scenarioDiscoveryContext': apiUrl, '@/utils/scenarioAdvisorEvents': eventsUrl }
    return `from ${quote}${targets[name] || import.meta.resolve(name)}${quote}`
  })
}
const composableUrl = encode(replaceImports(transpile(sourceOf('../src/composables/useScenarioDiscoveryContext.ts'))))
const { useScenarioDiscoveryContext } = await import(composableUrl)
const componentSource = compileScript(parse(sourceOf('../src/components/ScenarioBusinessContextPanel.vue')).descriptor, { id: 'business-context-test', inlineTemplate: true, templateOptions: { compilerOptions: { hoistStatic: false } } }).content
const { default: Panel } = await import(encode(replaceImports(transpile(componentSource).replace("from '@/composables/useScenarioDiscoveryContext'", `from '${composableUrl}'`))))

function context(id = 'scene', overrides = {}) {
  return {
    version: 'scenario-discovery-context.v1', scenario: { id, name: '结果闭环', description: '受治理的业务目标' }, revision: 3,
    business: { beneficiary: '请求人与负责人', pain: '结果无人核验', desired_outcome: '请求人收到可核验结果', success_metric: '负责人核对完成回执', scope: '收集信息并受控处理', non_goals: '不自动发送外部通知', decision: 'continue', decision_reason: '业务价值已明确', open_questions: ['例外由谁处理？'] },
    handoff: { status: 'current', publication_id: 'private-publication-id', publication_revision: 3 }, construction: { can_continue: true, reason: '已交接当前结论，可继续建设。' },
    processes: { as_is: { nodes: [], total_nodes: 0, has_more: false }, to_be: { nodes: [{ key: 'private-node-id', name: '核验结果', owner: '负责人', outcome: '留存回执', trigger: '', inputs: '', rule: '核对输出', exceptions: '人工处理例外' }], total_nodes: 1, has_more: false } },
    historical_cases: { items: [{ key: 'private-case-id', title: '历史处理案例', result_summary: '发现缺少结果确认', limitations: '仅用于理解业务' }], total_count: 1, has_more: false },
    materials: { sources: [{ data_source_id: 'private-source-id', name: '流程说明 <img src=x>', type: 'file_bucket', scope: 'scenario', resource_scope: 'modeling', content_read: false }], has_more: true, next_offset: 20 },
    boundaries: ['建模资料不会自动成为本次调用输入。'], ...overrides,
  }
}
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
function createHost() {
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const remove = child => { const siblings = child.parent?.children; if (siblings) { const index = siblings.indexOf(child); if (index >= 0) siblings.splice(index, 1) } }
  const renderer = createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
    insert(child, parent, anchor) { remove(child); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove, setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
    patchProp: (target, key, old, value) => { target.props[key] = value }, parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  })
  return { renderer, root: node('root') }
}
function windowScope() {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const window = new EventTarget()
  globalThis.window = window
  return { window, restore: () => previous ? Object.defineProperty(globalThis, 'window', previous) : delete globalThis.window }
}
function mountState(id = 'scene') {
  const scope = windowScope(), host = createHost(), scenarioId = ref(id)
  let state
  const app = host.renderer.createApp({ setup() { state = useScenarioDiscoveryContext(scenarioId); return () => h('div') } })
  app.mount(host.root)
  return { scenarioId, state, window: scope.window, stop() { app.unmount(); scope.restore() } }
}
async function mountPanel(value = context(), compact = false) {
  fakeApi.get = async id => ({ ...value, scenario: { ...value.scenario, id } })
  const scope = windowScope(), host = createHost(), props = ref({ scenarioId: 'scene' })
  props.value = { scenarioId: 'scene', compact }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/scenarios/:id', name: 'scenario-detail', component: { render: () => null } }] })
  await router.push('/scenarios/scene?stage=distillation')
  const app = host.renderer.createApp({ setup: () => () => h(Panel, props.value) })
  app.use(router)
  app.mount(host.root)
  await flush()
  const all = () => { const items = []; const visit = item => { items.push(item); item.children.forEach(visit) }; visit(host.root); return items }
  const textOf = item => [item.text, ...item.children.map(textOf)].join('')
  return { props, router, window: scope.window, all, text: () => textOf(host.root), button: label => all().find(item => item.type === 'button' && textOf(item) === label), stop() { app.unmount(); scope.restore() } }
}

test('scene change aborts old understanding and ignores its late response', async () => {
  const old = deferred(); let signal
  fakeApi.get = async (id, control) => id === 'old' ? (signal = control, old.promise) : context(id)
  const view = mountState('old')
  try {
    view.scenarioId.value = 'new'
    await flush()
    assert.equal(signal.aborted, true)
    old.resolve(context('old'))
    await flush()
    assert.equal(view.state.context.value.scenario.id, 'new')
  } finally { view.stop() }
})

test('refresh clears inaccessible understanding and permits a successful explicit retry', async () => {
  fakeApi.get = async id => context(id)
  const view = mountState()
  try {
    await flush()
    fakeApi.get = async () => { throw Object.assign(new Error('Restricted'), { status: 403 }) }
    await view.state.load()
    assert.equal(view.state.context.value, null)
    assert.equal(view.state.unauthorized.value, true)
    assert.match(view.state.error.value, /不可访问/)
    fakeApi.get = async id => context(id)
    await view.state.load()
    assert.equal(view.state.context.value.scenario.id, 'scene')
    assert.equal(view.state.unauthorized.value, false)
  } finally { view.stop() }
})

test('foreign responses are refused and unmount cancels pending reads', async () => {
  fakeApi.get = async () => context('foreign')
  const view = mountState()
  await flush()
  assert.equal(view.state.context.value, null)
  assert.match(view.state.error.value, /不属于当前场景/)
  const pending = deferred(); let signal
  fakeApi.get = async (id, control) => (signal = control, pending.promise)
  const loading = view.state.load()
  view.stop()
  assert.equal(signal.aborted, true)
  pending.resolve(context())
  await loading
  assert.equal(view.state.context.value, null)
})

test('only matching saved scene events refresh its adopted understanding', async () => {
  let calls = 0
  fakeApi.get = async id => { calls++; return context(id) }
  const view = mountState()
  try {
    await flush()
    events.notifyScenarioDiscoveryContextChanged('other')
    await flush()
    assert.equal(calls, 1)
    events.notifyScenarioDiscoveryContextChanged('scene')
    await flush()
    assert.equal(calls, 2)
  } finally { view.stop() }
})

test('shared context explains accepted goals, boundaries and sources without exposing internal identities or markup', async () => {
  const view = await mountPanel()
  try {
    assert.match(view.text(), /建设交接已同步/)
    for (const value of ['请求人与负责人', '负责人核对完成回执', '不自动发送外部通知', '授权资料目录', '历史处理案例', '人工处理例外']) assert.match(view.text(), new RegExp(value))
    assert.doesNotMatch(view.text(), /private-publication-id|private-source-id|private-node-id|private-case-id/)
    assert.equal(view.all().some(item => item.type === 'img' || 'innerHTML' in item.props), false)
    assert.equal(view.all().some(item => item.type === 'details' && item.children.some(child => child.type === 'summary')), true)
    assert.equal(view.button('继续场景建设').props.disabled, false)
    await view.button('流程说明 <img src=x>').props.onClick()
    await flush()
    assert.equal(view.router.currentRoute.value.query.stage, 'materials')
    assert.equal(view.router.currentRoute.value.query.source_id, 'private-source-id')
  } finally { view.stop() }
})

test('distillation shows a compact handoff status without duplicating the canvas findings', async () => {
  const view = await mountPanel(context(), true)
  try {
    assert.match(view.text(), /场景业务认知/)
    assert.match(view.text(), /建设交接已同步/)
    assert.match(view.text(), /请求人收到可核验结果/)
    assert.equal(view.all().some(item => item.type === 'details'), false)
    assert.doesNotMatch(view.text(), /AI 可查阅的资料|如何完成业务|历史业务案例/)
    assert.equal(view.button('继续场景建设').props.disabled, false)
  } finally { view.stop() }
})

test('missing and stale handoffs remain explicit and server refusal controls continuation', async () => {
  for (const status of ['missing', 'stale']) {
    const view = await mountPanel(context('scene', { handoff: { status, publication_id: null, publication_revision: null }, construction: { can_continue: false, reason: '请重新交接当前结论。' } }))
    try {
      assert.match(view.text(), status === 'missing' ? /尚未交接建设资料/ : /待重新交接/)
      assert.equal(view.button('继续场景建设').props.disabled, true)
      assert.ok(view.button('回到业务蒸馏'))
    } finally { view.stop() }
  }
})

test('continuation changes to ontology before opening the existing advisor without sending a message', async () => {
  const view = await mountPanel(), opened = []
  view.window.addEventListener(events.OPEN_SCENARIO_MODELING_ADVISOR_EVENT, event => opened.push({ detail: event.detail, stage: view.router.currentRoute.value.query.stage }))
  try {
    await view.button('继续场景建设').props.onClick()
    assert.equal(opened.length, 1)
    assert.equal(opened[0].stage, 'ontology')
    assert.equal(opened[0].detail.scenario_id, 'scene')
    assert.match(opened[0].detail.prompt, /成功标准和已交接资料/)
  } finally { view.stop() }
})

test('canceled navigation never opens an advisor in the previous workspace', async () => {
  const view = await mountPanel(), opened = []
  view.router.beforeEach(() => false)
  view.window.addEventListener(events.OPEN_SCENARIO_MODELING_ADVISOR_EVENT, event => opened.push(event.detail))
  try {
    await view.button('继续场景建设').props.onClick()
    assert.equal(view.router.currentRoute.value.query.stage, 'distillation')
    assert.deepEqual(opened, [])
  } finally { view.stop() }
})

test('continuation already in ontology opens the advisor after duplicated navigation', async () => {
  const view = await mountPanel(), opened = []
  await view.router.push('/scenarios/scene?stage=ontology')
  view.window.addEventListener(events.OPEN_SCENARIO_MODELING_ADVISOR_EVENT, event => opened.push(event.detail))
  try {
    await view.button('继续场景建设').props.onClick()
    assert.equal(opened.length, 1)
    assert.equal(opened[0].scenario_id, 'scene')
  } finally { view.stop() }
})

test('scenario business understanding is scoped to the business distillation stage', () => {
  const detail = sourceOf('../src/views/ScenarioDetail.vue')
  const workspace = sourceOf('../src/components/distillation/DistillationWorkspace.vue')
  const materialsStart = detail.indexOf('<el-tab-pane name="materials"')
  const distillationStart = detail.indexOf('<el-tab-pane name="distillation"')
  const distillationEnd = detail.indexOf('</el-tab-pane>', distillationStart)

  assert.ok(materialsStart >= 0)
  assert.ok(distillationStart > materialsStart)
  assert.ok(distillationEnd > distillationStart)
  assert.doesNotMatch(detail.slice(materialsStart, distillationStart), /ScenarioBusinessContextPanel/)
  assert.doesNotMatch(detail.slice(0, distillationEnd), /<ScenarioBusinessContextPanel/)
  assert.match(detail.slice(distillationStart, distillationEnd), /<DistillationWorkspace[^>]*:show-scenario-context="detail\.can_read_workspace_context"/)
  assert.match(workspace, /<ScenarioBusinessContextPanel[^>]*v-if="props\.embedded && props\.scenarioId && props\.showScenarioContext"[^>]*compact/)
})

const librarySource = sourceOf('../src/views/DataSources.vue')
const functionSlice = (start, end) => librarySource.slice(librarySource.indexOf(start), librarySource.indexOf(end, librarySource.indexOf(start)))
const libraryActionsSource = `export function libraryActions(dependencies) {
  const {canWrite,selected,routeScenarioId,uploadList,uploading,uploadFailures,api,loadFiles,ElMessage,ElMessageBox,notifyScenarioDiscoveryContextChanged,editingSource,props,catalogOffset,router,sourceLocation,load,clearSelection} = dependencies
  ${functionSlice('async function doUpload()', 'async function loadFiles()')}
  ${functionSlice('async function removeFile(', '// ── 资料库创建')}
  ${functionSlice('async function onLibrarySaved(', 'async function returnToPreviousFlow()')}
  return {doUpload,removeFile,onLibrarySaved,remove}
}`
const { libraryActions } = await import(encode(transpile(libraryActionsSource)))
function materialActions() {
  const changes = []
  const dependencies = { canWrite: ref(true), selected: ref({ id: 'source', scenario_id: 'scene', can_write: true }), routeScenarioId: ref('scene'), uploadList: ref([{ uid: 1, name: 'first', raw: {} }, { uid: 2, name: 'second', raw: {} }]), uploading: ref(false), uploadFailures: ref([]), api: {}, loadFiles: async () => {}, ElMessage: { success() {}, warning() {}, error() {} }, ElMessageBox: { confirm: async () => {} }, notifyScenarioDiscoveryContextChanged: id => changes.push(id), editingSource: ref(null), props: { embedded: true }, catalogOffset: ref(0), router: { replace: async () => {} }, sourceLocation: id => id, load: async () => {}, clearSelection() {} }
  return { changes, dependencies, ...libraryActions(dependencies) }
}

test('material uploads refresh once after acknowledged partial success and failures retain files without a false refresh', async () => {
  const partial = materialActions(); let uploads = 0
  partial.dependencies.api.uploadFiles = async () => { if (++uploads === 2) throw new Error('Retry'); return [] }
  await partial.doUpload()
  assert.deepEqual(partial.changes, ['scene'])
  assert.deepEqual(partial.dependencies.uploadList.value.map(item => item.uid), [2])
  const failed = materialActions()
  failed.dependencies.api.uploadFiles = async () => { throw new Error('Retry') }
  await failed.doUpload()
  assert.deepEqual(failed.changes, [])
  assert.equal(failed.dependencies.uploadList.value.length, 2)
})

test('material deletion refreshes its scene only after server acknowledgement', async () => {
  const view = materialActions()
  view.dependencies.api.deleteDataSource = async () => { throw new Error('Denied') }
  await view.remove({ id: 'source', name: '说明', type: 'file_bucket', scenario_id: 'scene' })
  assert.deepEqual(view.changes, [])
  view.dependencies.api.deleteDataSource = async () => ({ message: 'Deleted' })
  await view.remove({ id: 'source', name: '说明', type: 'file_bucket', scenario_id: 'scene' })
  assert.deepEqual(view.changes, ['scene'])
  view.dependencies.api.deleteFile = async () => { throw new Error('Denied') }
  await view.removeFile({ id: 'file', filename: '说明' })
  assert.deepEqual(view.changes, ['scene'])
  view.dependencies.api.deleteFile = async () => {}
  await view.removeFile({ id: 'file', filename: '说明' })
  assert.deepEqual(view.changes, ['scene', 'scene'])
})

test('moving a saved material refreshes both its previous and new scene catalogs', async () => {
  const view = materialActions()
  view.dependencies.editingSource.value = { id: 'source', scenario_id: 'previous' }
  await view.onLibrarySaved({ id: 'source', scenario_id: 'scene' })
  assert.deepEqual(view.changes, ['previous', 'scene'])
})
