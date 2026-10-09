import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterLink, RouterView } from 'vue-router'

const fakeApi = {}
function resourceCatalog() { return { models: [{ id: 'model', name: 'Coding model', model: 'synthetic', supports_tools: true }, { id: 'plain', name: 'Plain model', model: 'synthetic-plain', supports_tools: false }], skills: [{ id: 'skill-one', name: 'Plugin review', description: 'Review published plugin contracts', version: '1.0.0', mode: 'instructions' }], mcps: [{ id: 'mcp-one', name: 'Developer docs', transport: 'streamable_http', mode: 'read_only_resources' }], base_tools: [{ key: 'read_files', title: '读取项目文件', description: 'Read current candidate source files' }] } }
globalThis.__pluginCodingTestApi = fakeApi
const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const apiUrl = encode('export const pluginCodingApi = globalThis.__pluginCodingTestApi')
const source = ts.transpileModule(readFileSync(new URL('../src/composables/usePluginCodingWorkspace.ts', import.meta.url), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const workspaceComposableUrl = encode(source.replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/pluginCoding'", `from '${apiUrl}'`))
const { usePluginCodingWorkspace } = await import(workspaceComposableUrl)
const renderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function value(id, revision = 1, run_status = null) { return { id, revision, run_status, files_hash: `${id}-${revision}`, files: [], events: [], validation: [] } }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
function mount(id = 'one', releaseId) { const workspaceId = ref(id); let state; const app = renderer.createApp({ setup() { state = usePluginCodingWorkspace(workspaceId, releaseId); return () => h('div') } }); app.mount({}); return { state, workspaceId, stop: () => app.unmount() } }
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const payload = { expected_revision: 1, request_id: 'synthetic-edit', action: 'save', base_files_hash: 'a'.repeat(64), instruction: '', files: [] }

test('changing coding workspace aborts its read and refuses the late response', async () => {
  const old = deferred()
  let signal
  fakeApi.get = (id, current) => { if (id === 'one') { signal = current; return old.promise } return Promise.resolve(value(id)) }
  const view = mount()
  try {
    view.workspaceId.value = 'two'
    await flush()
    assert.equal(signal.aborted, true)
    old.resolve(value('one'))
    await flush()
    assert.equal(view.state.workspace.value.id, 'two')
    assert.equal(view.state.loading.value, false)
  } finally { view.stop() }
})

test('workspace from another release is rejected before it enters the editor', async () => {
  fakeApi.get = async id => ({ ...value(id), release_id: 'other' })
  const view = mount('one', ref('current'))
  try {
    await flush()
    assert.equal(view.state.workspace.value, null)
    assert.match(view.state.error.value, /不属于当前业务发布/)
  } finally { view.stop() }
})

test('manual revision cancels an outstanding poll and the poll cannot overwrite the saved files', async () => {
  const poll = deferred()
  let count = 0
  let pollSignal
  fakeApi.get = (id, signal) => { count++; if (count === 2) { pollSignal = signal; return poll.promise } return Promise.resolve(value(id, count > 2 ? 2 : 1)) }
  fakeApi.revise = async () => value('one', 2)
  const view = mount()
  try {
    await flush()
    const pending = view.state.load()
    assert.equal(await view.state.revise(payload), true)
    assert.equal(pollSignal.aborted, true)
    poll.resolve(value('one', 1))
    await pending
    await flush()
    assert.equal(view.state.workspace.value.revision, 2)
  } finally { view.stop() }
})

test('revision conflict refreshes the file basis while preserving the actionable error', async () => {
  let revision = 1
  fakeApi.get = async id => value(id, revision)
  fakeApi.revise = async () => { revision = 3; throw new Error('文件已变化，草稿已保留') }
  const view = mount()
  try {
    await flush()
    assert.equal(await view.state.revise(payload), false)
    assert.equal(view.state.workspace.value.revision, 3)
    assert.equal(view.state.error.value, '文件已变化，草稿已保留')
    assert.equal(view.state.busy.value, false)
  } finally { view.stop() }
})

test('unmount aborts an active revision and refuses repeated submissions and late writes', async () => {
  const action = deferred()
  let signal
  let submissions = 0
  fakeApi.get = async id => value(id)
  fakeApi.revise = (id, body, current) => { submissions++; signal = current; return action.promise }
  const view = mount()
  await flush()
  const pending = view.state.revise(payload)
  assert.equal(await view.state.revise(payload), false)
  view.stop()
  assert.equal(signal.aborted, true)
  action.resolve(value('one', 9))
  assert.equal(await pending, false)
  assert.equal(submissions, 1)
  assert.equal(view.state.workspace.value.revision, 1)
})

test('read failure can retry without clearing the last saved workspace', async () => {
  let failed = false
  fakeApi.get = async id => { if (failed) throw new Error('读取失败'); return value(id) }
  const view = mount()
  try {
    await flush()
    failed = true
    await view.state.load()
    assert.equal(view.state.workspace.value.id, 'one')
    assert.equal(view.state.error.value, '读取失败')
    failed = false
    await view.state.load()
    assert.equal(view.state.error.value, '')
  } finally { view.stop() }
})

test('saving coding settings uses the workspace revision and refuses stale polling results', async () => {
  const pendingRead = deferred()
  let reads = 0
  let staleSignal
  let submitted
  fakeApi.get = (id, signal) => { reads++; if (reads === 2) { staleSignal = signal; return pendingRead.promise } return Promise.resolve(value(id, reads > 2 ? 2 : 1)) }
  fakeApi.settings = async (id, body) => { submitted = { id, body }; return { ...value(id, 2), resource_selection: body } }
  const view = mount()
  try {
    await flush()
    const reading = view.state.load()
    const selection = { expected_revision: 1, request_id: 'synthetic-settings', llm_config_id: 'model', skill_ids: ['skill-one'], mcp_ids: ['mcp-one'] }
    assert.equal(await view.state.settings(selection), true)
    assert.deepEqual(submitted, { id: 'one', body: selection })
    assert.equal(staleSignal.aborted, true)
    pendingRead.resolve(value('one', 1))
    await reading
    await flush()
    assert.equal(view.state.workspace.value.revision, 2)
  } finally { view.stop() }
})

test('settings conflict preserves the server error and restores the current revision for retry', async () => {
  let revision = 1
  fakeApi.get = async id => value(id, revision)
  fakeApi.settings = async () => { revision = 4; throw new Error('配置已变化，请重新审阅') }
  const view = mount()
  try {
    await flush()
    assert.equal(await view.state.settings({ expected_revision: 1, request_id: 'synthetic-settings', llm_config_id: 'model', skill_ids: [], mcp_ids: [] }), false)
    assert.equal(view.state.workspace.value.revision, 4)
    assert.equal(view.state.error.value, '配置已变化，请重新审阅')
    assert.equal(view.state.busy.value, false)
  } finally { view.stop() }
})

const compile = code => ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const editorUtilsUrl = encode(compile(readFileSync(new URL('../src/utils/pluginCodeEditor.ts', import.meta.url), 'utf8')))
const { indentSource, sourceTokens, pluginFileTree } = await import(editorUtilsUrl)

test('source indentation updates actual code and selection for insert, block indent and outdent', () => {
  assert.deepEqual(indentSource('return value', 0, 0, false, 4), { value: '    return value', start: 4, end: 4 })
  assert.deepEqual(indentSource('one\ntwo\nthree', 0, 8), { value: '  one\n  two\nthree', start: 2, end: 12 })
  assert.deepEqual(indentSource('  one\n\ttwo\nthree', 0, 10, true), { value: 'one\ntwo\nthree', start: 0, end: 7 })
  assert.deepEqual(indentSource('\nnext', 0, 0, true), { value: '\nnext', start: 0, end: 0 })
})

test('syntax tokens preserve code exactly and keep markup as untrusted text', () => {
  const python = 'async def run():\n    return "<script>alert(1)</script>" # note'
  const lines = sourceTokens(python, 'scripts/run.py')
  assert.equal(lines.map(line => line.map(token => token.text).join('')).join('\n'), python)
  assert.equal(lines[0][0].kind, 'keyword')
  assert.equal(lines[1].at(-1).kind, 'comment')
  assert.equal(sourceTokens('\n\n', 'scripts/run.py').map(line => line.map(token => token.text).join('')).join('\n'), '\n\n')
  assert.equal(sourceTokens('{"active": true, "count": 1}', 'manifest.json')[0].find(token => token.text === 'true').kind, 'keyword')
})

test('file explorer builds real nested directories and keeps equal names in separate folders', () => {
  const tree = pluginFileTree(['README.md', 'skills/one/SKILL.md', 'skills/two/SKILL.md', 'scripts/run.py'])
  assert.deepEqual(tree.map(node => node.path), ['scripts', 'skills', 'README.md'])
  assert.deepEqual(tree[1].children.map(node => node.children[0].path), ['skills/one/SKILL.md', 'skills/two/SKILL.md'])
  assert.equal(tree[1].children[0].directory, true)
  assert.equal(tree[1].children[0].children[0].directory, false)
})

const componentCache = new Map()
const emptyComponentUrl = encode('export default { render: () => null }')
const developmentApi = {}
globalThis.__pluginDevelopmentApi = developmentApi
const developmentApiUrl = encode('export const api = globalThis.__pluginDevelopmentApi; export const pluginCodingApi = globalThis.__pluginDevelopmentApi; export const scenarioReleasesApi = globalThis.__pluginDevelopmentApi')
const developmentMessageUrl = encode('export const ElMessageBox = { confirm: (...args) => globalThis.__pluginDevelopmentConfirm(...args) }')
const taskRequestUrl = encode('export const createClientRequestId = () => "synthetic-task-id"')
const taskStartUrl = encode(compile(readFileSync(new URL('../src/composables/usePluginTaskStart.ts', import.meta.url), 'utf8')).replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api'", `from '${developmentApiUrl}'`).replace("from '@/api/pluginCoding'", `from '${developmentApiUrl}'`).replace("from '@/utils/clientRequestId'", `from '${taskRequestUrl}'`))
const codingSettingsUrl = encode(compile(readFileSync(new URL('../src/composables/usePluginCodingSettings.ts', import.meta.url), 'utf8')).replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/pluginCoding'", `from '${developmentApiUrl}'`))
const { usePluginCodingSettings } = await import(codingSettingsUrl)
const safeMarkdownUrl = encode(`import { h } from '${vueUrl}'; export default { props: ['content'], setup: props => () => h('p', props.content) }`)
function componentUrl(relative) {
  const url = new URL(relative, import.meta.url)
  if (componentCache.has(url.href)) return componentCache.get(url.href)
  const script = compileScript(parse(readFileSync(url, 'utf8')).descriptor, { id: url.pathname, inlineTemplate: true, templateOptions: { compilerOptions: { hoistStatic: false } } }).content
  const result = encode(compile(script).replace(/from (["'])([^"']+)\1/g, (match, quote, name) => {
    let target
    if (name === 'vue') target = vueUrl
    else if (name === '@/utils/pluginCodeEditor') target = editorUtilsUrl
    else if (name === '@/utils/pluginCodingDiff') target = encode(compile(readFileSync(new URL('../src/utils/pluginCodingDiff.ts', import.meta.url), 'utf8')))
    else if (['@/api', '@/api/pluginCoding', '@/api/scenarioReleases'].includes(name)) target = developmentApiUrl
    else if (name === '@/composables/usePluginTaskStart') target = taskStartUrl
    else if (name === '@/composables/usePluginCodingSettings') target = codingSettingsUrl
    else if (name === '@/composables/usePluginCodingWorkspace') target = workspaceComposableUrl
    else if (name === '@/composables/usePluginCodingEditor') target = encode(compile(readFileSync(new URL('../src/composables/usePluginCodingEditor.ts', import.meta.url), 'utf8')).replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/utils/clientRequestId'", `from '${taskRequestUrl}'`))
    else if (name === '@/utils/platformSettings') target = encode(compile(readFileSync(new URL('../src/utils/platformSettings.ts', import.meta.url), 'utf8')))
    else if (name === '@/utils/clientRequestId') target = taskRequestUrl
    else if (name === 'element-plus') target = developmentMessageUrl
    else if (name === '@/components/SafeMarkdown.vue') target = safeMarkdownUrl
    else if (['@/components/plugin-coding/PluginCodingInspector.vue', '@/components/plugin-coding/PluginCodingDelivery.vue'].includes(name)) target = emptyComponentUrl
    else if (name.startsWith('@/components/') && name.endsWith('.vue')) target = componentUrl(`../src/${name.slice(2)}`)
    else if (name.endsWith('.vue')) target = componentUrl(new URL(name, url).href)
    else target = import.meta.resolve(name)
    return `from ${quote}${target}${quote}`
  }))
  componentCache.set(url.href, result)
  return result
}
const { default: SourceEditor } = await import(componentUrl('../src/components/plugin-coding/PluginSourceEditor.vue'))
const { default: Inspector } = await import(componentUrl('../src/components/plugin-coding/PluginCodingInspector.vue'))
const { default: Explorer } = await import(componentUrl('../src/components/plugin-coding/PluginFileExplorer.vue'))
const { default: IdeStart } = await import(componentUrl('../src/components/plugin-coding/PluginIdeStart.vue'))
const { default: Development } = await import(componentUrl('../src/views/PluginDevelopment.vue'))
const { default: Composer } = await import(componentUrl('../src/components/plugin-coding/PluginCodingComposer.vue'))
const { default: CodingSettings } = await import(componentUrl('../src/components/plugin-coding/PluginCodingSettings.vue'))
const { default: Conversation } = await import(componentUrl('../src/components/plugin-coding/PluginCodingConversation.vue'))
const { default: Workbench } = await import(componentUrl('../src/components/PluginCodingWorkbench.vue'))
const { default: BuildSetup } = await import(componentUrl('../src/components/plugin-coding/PluginBuildSetup.vue'))
function mountEditorComponent(component, initial, handlers = {}, options = {}) {
  const props = ref(initial)
  const node = (type, text = '') => ({ type, tagName: type.toUpperCase(), text, props: {}, children: [], parent: null, value: '', events: {}, selectionStart: 0, selectionEnd: 0, scrollTop: 0, scrollLeft: 0, isConnected: true, focus() { this.focused = true }, setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end }, addEventListener(name, handler) { this.events[name] = handler }, removeEventListener(name) { delete this.events[name] } })
  const remove = child => { const parent = child.parent; if (parent) parent.children.splice(parent.children.indexOf(child), 1) }
  const host = createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: () => node('#comment'),
    insert(child, parent, anchor) { remove(child); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove, setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
    patchProp: (target, key, old, value) => { target.props[key] = value; if (key === 'value') target.value = value },
    parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  })
  const root = node('root')
  const app = host.createApp({ setup: () => () => options.routerView ? h(RouterView) : h(component, { ...props.value, ...handlers }, options.slots) })
  for (const [name, tag] of [['el-button', 'button'], ['el-icon', 'i'], ['el-select', 'select'], ['el-option', 'option'], ['el-alert', 'div'], ['el-checkbox', 'label'], ['el-input', 'input']]) app.component(name, { setup: (unused, context) => () => h(tag, context.attrs, context.slots.default?.()) })
  app.component('el-drawer', { props: ['modelValue'], setup: (props, context) => () => props.modelValue ? h('section', context.attrs, [context.slots.default?.(), context.slots.footer?.()]) : null })
  if (!options.plugins?.length) app.component('RouterLink', RouterLink)
  for (const plugin of options.plugins || []) app.use(plugin)
  app.mount(root)
  const all = () => { const found = []; const visit = target => { found.push(target); target.children.forEach(visit) }; visit(root); return found }
  const textOf = target => [target.text, ...target.children.map(textOf)].join('')
  return { props, all, textOf, button: label => all().find(target => target.type === 'button' && textOf(target) === label), stop: () => app.unmount() }
}

test('new task contract preview follows selected capabilities without claiming unselected tools are provided', async () => {
  const capability = (key, name) => ({ kind: 'rule', key, name, description: `${name}的业务描述`, definition_hash: 'a'.repeat(64), input_schema: { type: 'object', properties: {} }, output_schema: { type: 'object', properties: {} }, side_effect: false, requires_confirmation: false, idempotency_required: false, data_ports: [], readiness: { ready: true, issues: [] } })
  developmentApi.context = async id => ({ scenario: { id: 'scene', name: '采购分流' }, deployment: { release_id: id }, capabilities: [capability('one', '费用合规'), capability('two', '采购分流')] })
  developmentApi.resources = async () => resourceCatalog()
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/new-task', component: { render: () => null } }] })
  await router.push('/new-task')
  const view = mountEditorComponent(BuildSetup, { release: { id: 'release', scenario_id: 'scene', scenario_name: '采购分流', name: '固定版本', revision: 1, enabled: true } }, {}, { plugins: [router] })
  try {
    await flush()
    const preview = () => view.all().find(node => node.props['aria-label'] === 'AI 可读取的场景能力')
    assert.match(view.textOf(preview()), /费用合规/)
    assert.match(view.textOf(preview()), /采购分流/)
    const choice = view.all().find(node => node.type === 'label' && view.textOf(node) === '采购分流')
    choice.props['onUpdate:modelValue'](false)
    await nextTick()
    assert.match(view.textOf(preview()), /费用合规/)
    assert.doesNotMatch(view.textOf(preview()), /采购分流/)
    assert.match(view.all().map(view.textOf).join(''), /1 项能力/)
    assert.equal(view.all().some(node => 'innerHTML' in node.props), false)
  } finally { view.stop() }
})

test('plugin host selector refreshes the delivery profile while preserving the goal for explicit submission', async () => {
  const targets = [], goals = []
  developmentApi.context = async (id, signal, target) => { targets.push(target); return { scenario: { id: 'scene', name: '任务场景' }, deployment: { release_id: id }, capabilities: [], delivery_profile: { host: { key: target, label: target === 'codex' ? 'OpenAI Codex' : 'Claude Code' }, components: [], standards: [], boundaries: [], platform_rules: [], protected_references: [] } } }
  developmentApi.resources = async () => resourceCatalog()
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/new-task', component: { render: () => null } }] })
  await router.push('/new-task')
  const view = mountEditorComponent(BuildSetup, { initialInstruction: '保留业务目标', release: { id: 'release', scenario_id: 'scene', name: '固定版本', revision: 1, enabled: true } }, { onInstruction: value => goals.push(value) }, { plugins: [router] })
  try {
    await flush()
    const selector = view.all().find(node => node.props['aria-label'] === '插件安装宿主')
    assert.ok(selector)
    assert.equal(view.all().find(node => node.type === 'label' && node.props.for === 'plugin-task-host') !== undefined, true)
    selector.props['onUpdate:modelValue']('codex')
    await flush()
    assert.deepEqual(targets, ['claude_code', 'codex'])
    const composer = view.all().find(node => node.type === 'textarea' && node.props.id === 'plugin-task-instruction')
    assert.equal(composer.props.value, '保留业务目标')
    assert.deepEqual(goals, [])
  } finally { view.stop() }
})

test('actual source editor indents, saves with keyboard, and lets Escape then Tab leave it', async () => {
  let saves = 0
  let view
  view = mountEditorComponent(SourceEditor, { path: 'scripts/run.py', modelValue: 'return value', readonly: false }, {
    'onUpdate:modelValue': value => { view.props.value = { ...view.props.value, modelValue: value } }, onSave: () => { saves++ },
  })
  try {
    const input = view.all().find(node => node.type === 'textarea')
    let prevented = false
    await input.props.onKeydown({ key: 'Tab', preventDefault: () => { prevented = true } })
    assert.equal(prevented, true)
    assert.equal(view.props.value.modelValue, '    return value')
    assert.equal(input.selectionStart, 4)
    await input.props.onKeydown({ key: 's', ctrlKey: true, preventDefault() {} })
    assert.equal(saves, 1)
    await input.props.onKeydown({ key: 'Tab', shiftKey: true, preventDefault() {} })
    assert.equal(view.props.value.modelValue, 'return value')
    await input.props.onKeydown({ key: 'Escape' })
    prevented = false
    await input.props.onKeydown({ key: 'Tab', preventDefault: () => { prevented = true } })
    assert.equal(prevented, false)
    view.props.value = { ...view.props.value, readonly: true }
    await nextTick()
    await input.props.onKeydown({ key: 's', ctrlKey: true, preventDefault() {} })
    assert.equal(saves, 1)
    assert.equal(view.all().some(node => 'innerHTML' in node.props), false)
  } finally { view.stop() }
})

test('main editor opens source by default, keeps multiple file tabs and switches to real differences', async () => {
  const files = [{ path: 'README.md', content: 'readme', previous: 'before', editable: true }, { path: 'scripts/run.py', content: 'return value', previous: 'return None', editable: true }]
  const view = mountEditorComponent(Inspector, { workspace: { files, validation: [], plugin_version: '1.0.0' }, selectedPath: 'README.md', selectedFile: files[0], draft: 'readme', dirty: false, basisChanged: false, busy: false })
  try {
    assert.equal(view.all().find(node => node.type === 'textarea').props['aria-label'], '编辑 README.md')
    assert.equal(view.button('代码').props['aria-pressed'], true)
    view.props.value = { ...view.props.value, selectedPath: 'scripts/run.py', selectedFile: files[1], draft: 'return value' }
    await nextTick()
    const tabs = view.all().find(node => node.props['aria-label'] === '打开的插件文件')
    assert.match(view.textOf(tabs), /README.md/)
    assert.match(view.textOf(tabs), /run.py/)
    view.button('差异').props.onClick()
    await nextTick()
    assert.ok(view.all().some(node => node.props['aria-label'] === '逐行修改差异'))
    assert.equal(view.all().some(node => node.type === 'textarea'), false)
  } finally { view.stop() }
})

test('legacy workspace exposes missing blueprint only in its capability view and preserves the code editor', async () => {
  const file = { path: 'README.md', content: 'saved source', previous: '', editable: true }
  const view = mountEditorComponent(Inspector, { workspace: { files: [file], capabilities: [], validation: [], plugin_version: '1.0.0' }, selectedPath: file.path, selectedFile: file, draft: file.content, dirty: false, basisChanged: false, busy: false })
  try {
    assert.equal(view.all().find(item => item.type === 'textarea').props.value, 'saved source')
    assert.doesNotMatch(view.all().map(view.textOf).join(''), /旧编码任务尚未保存画像/)
    view.button('能力契约').props.onClick()
    await nextTick()
    assert.match(view.all().map(view.textOf).join(''), /旧编码任务尚未保存画像/)
    view.button('代码').props.onClick()
    await nextTick()
    assert.equal(view.all().find(item => item.type === 'textarea').props.value, 'saved source')
  } finally { view.stop() }
})

test('file tree folders expand and collapse, live files appear, and pending saves disable selection', async () => {
  const files = [{ path: 'skills/run/SKILL.md', content: 'skill', previous: '', editable: true }]
  const selected = []
  const view = mountEditorComponent(Explorer, { files, selectedPath: files[0].path, busy: false }, { onSelect: path => selected.push(path) })
  try {
    assert.ok(view.button('SKILL.md'))
    view.button('skills').props.onClick()
    await nextTick()
    assert.equal(view.button('SKILL.md'), undefined)
    view.button('skills').props.onClick()
    await nextTick()
    view.button('SKILL.md').props.onClick()
    assert.deepEqual(selected, ['skills/run/SKILL.md'])
    view.props.value = { ...view.props.value, busy: true, files: [...files, { path: 'scripts/new.py', content: 'return None', previous: '', editable: true }] }
    await nextTick()
    assert.ok(view.button('new.py'))
    assert.equal(view.button('new.py').props.disabled, true)
  } finally { view.stop() }
})

test('pending source saves freeze editing and dirty tabs cannot close without preserving their draft', async () => {
  const files = [{ path: 'README.md', content: 'readme', previous: 'before', editable: true }, { path: 'scripts/run.py', content: 'return value', previous: '', editable: true }]
  const view = mountEditorComponent(Inspector, { workspace: { files, validation: [], plugin_version: '1.0.0' }, selectedPath: 'README.md', selectedFile: files[0], draft: 'readme', dirty: false, basisChanged: false, busy: false })
  try {
    view.props.value = { ...view.props.value, selectedPath: 'scripts/run.py', selectedFile: files[1], draft: 'my pending draft', dirty: true, busy: true }
    await nextTick()
    assert.equal(view.all().find(node => node.type === 'textarea').props.readonly, true)
    assert.equal(view.button('保存并校验').props.disabled, true)
    assert.equal(view.all().find(node => node.props['aria-label'] === '关闭 scripts/run.py').props.disabled, true)
    assert.equal(view.button('README.md').props.disabled, true)
    assert.equal(view.all().find(node => node.type === 'textarea').props.value, 'my pending draft')
  } finally { view.stop() }
})

test('empty plugin IDE renders explorer, primary code area and a usable AI slot without fake files', async () => {
  const view = mountEditorComponent(IdeStart, { tasks: [], loading: false }, {}, { slots: { default: () => h('textarea', { 'aria-label': '真实 AI 编码目标', value: 'My plugin goal' }) } })
  try {
    for (const label of ['插件资源管理器', '代码编辑器', 'AI 编码']) assert.ok(view.all().find(node => ['aside', 'section'].includes(node.type) && node.props['aria-label'] === label), `Missing IDE landmark: ${label}`)
    const projectList = view.all().find(node => node.props['aria-label'] === '最近插件开发任务')
    assert.match(view.textOf(projectList), /尚无插件项目/)
    assert.equal(projectList.children.some(node => node.type === 'a'), false)
    assert.equal(view.all().some(node => node.props['aria-label'] === '插件文件'), false)
    assert.equal(view.all().some(node => /(?:README\.md|SKILL\.md|server\.py)/.test(node.text)), false)
    assert.equal(view.all().find(node => node.props['aria-label'] === '真实 AI 编码目标').props.value, 'My plugin goal')
    assert.equal(view.button('AI 编码').props['aria-pressed'], true)
    view.button('代码').props.onClick()
    await nextTick()
    assert.equal(view.button('代码').props['aria-pressed'], true)
    assert.match(view.all().find(node => node.props['aria-label'] === '插件开发工作区').props.class, /show-editor/)
  } finally { view.stop() }
})

test('recent IDE projects use real release and workspace identities in RouterLink destinations', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/plugin-studio', component: { render: () => null } }, { path: '/plugin-studio/:releaseId', name: 'plugin-coding-studio', component: { render: () => null } }] })
  await router.push('/plugin-studio')
  const tasks = [{ id: 'workspace-one', release_id: 'release-one', title: 'Build first plugin', plugin_version: '1.0.0', phase: 'generating' }, { id: 'workspace-two', release_id: 'release-two', title: 'Continue second plugin', plugin_version: '2.1.0', phase: 'released' }]
  const view = mountEditorComponent(IdeStart, { tasks, loading: false }, {}, { plugins: [router] })
  try {
    const links = view.all().filter(node => node.type === 'a')
    assert.deepEqual(links.map(link => link.props.href), ['/plugin-studio/release-one?workspace=workspace-one', '/plugin-studio/release-two?workspace=workspace-two'])
    assert.match(view.textOf(links[0]), /Build first plugin/)
    assert.match(view.textOf(links[1]), /v2\.1\.0 · 已定版/)
    await links[1].props.onClick({ button: 0, preventDefault() {}, currentTarget: { getAttribute: () => null } })
    await nextTick()
    assert.equal(router.currentRoute.value.params.releaseId, 'release-two')
    assert.equal(router.currentRoute.value.query.workspace, 'workspace-two')
  } finally { view.stop() }
})

test('typing a plugin goal before selecting scene and version preserves it without a leave warning', async () => {
  const globals = new Map(['window', 'document', '__pluginDevelopmentApi', '__pluginDevelopmentConfirm'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const listeners = new Map()
  globalThis.window = { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name) }
  globalThis.document = { activeElement: null }
  let confirmations = 0
  globalThis.__pluginDevelopmentConfirm = async () => { confirmations++; throw new Error('Keep draft') }
  Object.assign(developmentApi, {
    listScenarios: async () => [{ id: 'scene-one', name: 'Scene one' }, { id: 'scene-two', name: 'Scene two' }], tasks: async () => [],
    list: async scenario => ({ items: [1, 2].map(version => ({ id: `${scenario}-v${version}`, scenario_id: scenario, name: `Version ${version}`, status: 'released', enabled: true, revision: 1 })) }),
    context: async release => ({ scenario: { id: release.startsWith('scene-one') ? 'scene-one' : 'scene-two' }, deployment: { release_id: release }, capabilities: [{ kind: 'workflow', key: 'example', name: 'Example', description: '', definition_hash: 'a'.repeat(64), input_schema: {}, output_schema: {}, side_effect: false, requires_confirmation: false, idempotency_required: false, data_ports: [], readiness: { ready: true, issues: [] } }] }),
    resources: async () => resourceCatalog(),
  })
  let view
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/plugin-studio', component: Development }, { path: '/access', component: { render: () => null } }, { path: '/scenarios/:id', name: 'scenario-detail', component: { render: () => null } }] })
    await router.push('/plugin-studio')
    view = mountEditorComponent(Development, {}, {}, { plugins: [router], routerView: true })
    await flush()
    const goal = 'Build a plugin that completes this business process'
    const textarea = view.all().find(node => node.props['aria-label'] === '插件编码目标')
    textarea.value = goal
    textarea.props.onInput({ target: textarea })
    await flush()
    const scenarioSelect = () => view.all().find(node => node.props['aria-label'] === '插件开发业务场景')
    const versionSelect = () => view.all().find(node => node.props['aria-label'] === '插件开发能力版本')
    scenarioSelect().props['onUpdate:modelValue']('scene-one')
    await flush()
    assert.equal(confirmations, 0, 'Selecting a scene must preserve the goal without a leave confirmation')
    assert.equal(router.currentRoute.value.query.scenario_id, 'scene-one')
    versionSelect().props['onUpdate:modelValue']('scene-one-v1')
    await flush()
    assert.equal(router.currentRoute.value.query.release_id, 'scene-one-v1')
    assert.equal(view.all().find(node => node.props['aria-label'] === '插件编码目标').value, goal)
    versionSelect().props['onUpdate:modelValue']('scene-one-v2')
    await flush()
    assert.equal(view.all().find(node => node.props['aria-label'] === '插件编码目标').value, goal)
    scenarioSelect().props['onUpdate:modelValue']('scene-two')
    await flush()
    versionSelect().props['onUpdate:modelValue']('scene-two-v1')
    await flush()
    assert.equal(router.currentRoute.value.query.scenario_id, 'scene-two')
    assert.equal(view.all().find(node => node.props['aria-label'] === '插件编码目标').value, goal)
    assert.equal(confirmations, 0)
    let unloadingBlocked = false
    listeners.get('beforeunload')({ preventDefault() { unloadingBlocked = true } })
    assert.equal(unloadingBlocked, true)
    await router.push('/access')
    assert.equal(confirmations, 1)
    assert.equal(router.currentRoute.value.path, '/plugin-studio')
    assert.equal(view.all().find(node => node.props['aria-label'] === '插件编码目标').value, goal)
  } finally {
    view?.stop()
    for (const [key, descriptor] of globals) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key] }
  }
})

test('chat composer sends with Enter, preserves Shift Enter and refuses an active Chinese composition', async () => {
  let submissions = 0
  let settings = 0
  let mode
  const view = mountEditorComponent(Composer, { modelValue: 'Build my plugin', inputId: 'chat-goal', label: '插件编码目标', placeholder: 'Goal', canSend: true, allowDiscussion: true }, { onSubmit: () => { submissions++ }, onSettings: () => { settings++ }, 'onUpdate:mode': value => { mode = value } })
  try {
    const input = view.all().find(node => node.type === 'textarea')
    let prevented = 0
    const key = extra => input.props.onKeydown({ key: 'Enter', preventDefault: () => { prevented++ }, ...extra })
    key({ isComposing: true })
    key({ keyCode: 229 })
    key({ shiftKey: true })
    assert.equal(submissions, 0)
    assert.equal(prevented, 0)
    key({})
    key({ ctrlKey: true })
    assert.equal(submissions, 2)
    assert.equal(prevented, 2)
    view.button('讨论').props.onClick()
    assert.equal(mode, 'discuss')
    view.all().find(node => node.props['aria-label'] === '编码 AI 设置：模型、技能与 MCP').props.onClick()
    assert.equal(settings, 1)
    view.props.value = { ...view.props.value, busy: true }
    await nextTick()
    key({})
    assert.equal(submissions, 2)
    assert.equal(view.all().find(node => node.props['aria-label'] === '发送插件编码目标').props.disabled, true)
  } finally { view.stop() }
})

test('coding settings install real catalog resources, keep failed choices and require stopping the active run', async () => {
  const saves = []
  let stopped = 0
  const view = mountEditorComponent(CodingSettings, { modelValue: true, selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, catalog: resourceCatalog(), loading: false, saving: false, error: '', activeRun: false }, { onSave: value => saves.push(value), onStop: () => { stopped++ } })
  try {
    view.button('安装').props.onClick()
    view.all().find(node => node.props['aria-label'] === '安装 MCP Developer docs').props.onClick()
    await nextTick()
    view.button('保存设置').props.onClick()
    assert.deepEqual(saves[0], { llm_config_id: 'model', skill_ids: ['skill-one'], mcp_ids: ['mcp-one'] })
    view.props.value = { ...view.props.value, error: '版本冲突，选择已保留', activeRun: true }
    await nextTick()
    assert.equal(view.button('保存设置').props.disabled, true)
    assert.equal(view.all().find(node => node.props['aria-label'] === '卸载技能 Plugin review').props['aria-pressed'], true)
    view.button('停止当前编码轮次').props.onClick()
    assert.equal(stopped, 1)
    view.props.value = { ...view.props.value, activeRun: false }
    await nextTick()
    view.button('保存设置').props.onClick()
    assert.deepEqual(saves[1], saves[0])
    view.all().find(node => node.props['aria-label'] === '编码模型').props['onUpdate:modelValue']('plain')
    await nextTick()
    assert.equal(view.button('保存设置').props.disabled, true)
    view.all().find(node => node.props['aria-label'] === '卸载 MCP Developer docs').props.onClick()
    await nextTick()
    assert.equal(view.button('保存设置').props.disabled, false)
  } finally { view.stop() }
})

test('unavailable installed extensions can be explicitly removed without showing internal identities', async () => {
  let saved
  const view = mountEditorComponent(CodingSettings, { modelValue: true, selection: { llm_config_id: 'model', skill_ids: ['private-skill-id'], mcp_ids: ['private-mcp-id'] }, catalog: resourceCatalog(), loading: false, saving: false, error: '' }, { onSave: value => { saved = value } })
  try {
    assert.equal(view.button('保存设置').props.disabled, true)
    assert.equal(view.all().some(node => node.text.includes('private-')), false)
    view.button('卸载不可用技能').props.onClick()
    view.button('卸载不可用 MCP').props.onClick()
    await nextTick()
    assert.equal(view.button('保存设置').props.disabled, false)
    view.button('保存设置').props.onClick()
    assert.deepEqual(saved, { llm_config_id: 'model', skill_ids: [], mcp_ids: [] })
  } finally { view.stop() }
})

test('conversation keeps a scrolled review in place and renders only actual resource read receipts', async () => {
  const workspace = { id: 'one', revision: 1, turns: [{ id: 'run-one', instruction: 'Explain the plugin', status: 'succeeded', mode: 'discuss', created_at: '2026-10-05T14:00:00Z' }], events: [], validation: [], phase: 'draft', resource_selection: { llm_config_id: 'model', skill_ids: ['skill-one'], mcp_ids: [] }, resource_receipts: [] }
  const view = mountEditorComponent(Conversation, { workspace })
  try {
    await nextTick()
    assert.equal(view.all().some(node => node.type === 'summary' && view.textOf(node).includes('工具资料')), false)
    const transcript = view.all().find(node => node.props['aria-label'] === '公开编码对话')
    transcript.scrollHeight = 1000
    transcript.clientHeight = 300
    transcript.scrollTop = 100
    transcript.props.onScroll()
    view.props.value = { workspace: { ...workspace, revision: 2, events: [{ sequence: 1, kind: 'summary', run_id: 'run-one', message: 'Public explanation', path: '' }], resource_receipts: [{ tool: 'read_skill', title: 'Plugin review', read_only: true, run_id: 'run-one', content_sha256: 'a'.repeat(64), retrieved_at: '2026-10-05T14:00:00Z' }] } }
    await flush()
    assert.equal(transcript.scrollTop, 100)
    assert.match(view.textOf(view.all().find(node => node.type === 'summary' && view.textOf(node).includes('工具资料'))), /已读取 1 项/)
    assert.equal(view.all().some(node => node.text.includes('a'.repeat(64))), false)
    view.button('回到最新消息').props.onClick()
    await nextTick()
    assert.equal(transcript.scrollTop, 1000)
    assert.equal(view.button('回到最新消息'), undefined)
  } finally { view.stop() }
})

test('failed and stopped public turns restore their exact instruction and mode without submitting', async () => {
  const requested = []
  const view = mountEditorComponent(Conversation, { workspace: { revision: 1, turns: [{ id: 'failed-discussion', instruction: 'Explain the capability requirements', mode: 'discuss', status: 'failed', created_at: '2026-10-05T14:00:00Z' }, { id: 'stopped-coding', instruction: 'Add an input helper', mode: 'generate', status: 'cancelled', created_at: '2026-10-05T14:01:00Z' }, { id: 'completed', instruction: 'Already completed', mode: 'generate', status: 'succeeded', created_at: '2026-10-05T14:02:00Z' }], events: [], validation: [], phase: 'draft', resource_receipts: [] } }, { onRetry: (instruction, mode) => requested.push({ instruction, mode }) })
  try {
    assert.equal(view.all().filter(node => node.type === 'button').length, 2)
    view.button('继续讨论').props.onClick()
    view.button('重试本轮').props.onClick()
    assert.deepEqual(requested, [{ instruction: 'Explain the capability requirements', mode: 'discuss' }, { instruction: 'Add an input helper', mode: 'generate' }])
  } finally { view.stop() }
})

test('retrying a failed discussion protects the composer draft then restores and focuses it for explicit resend', async () => {
  developmentApi.resources = async () => resourceCatalog()
  const workspace = { ...value('one'), release_id: 'release-one', phase: 'released', plugin_version: '1.0.0', turns: [{ id: 'failed-one', instruction: 'Explain the saved plugin', mode: 'discuss', status: 'failed', created_at: '2026-10-05T14:00:00Z' }], run_status: 'failed', active_run_id: null, resource_selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, resource_receipts: [] }
  let submissions = 0
  fakeApi.get = async () => workspace
  fakeApi.revise = async () => { submissions++; return workspace }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/project/:releaseId', component: Workbench }] })
  await router.push('/project/release-one')
  const view = mountEditorComponent(Workbench, { workspaceId: 'one', releaseId: 'release-one' }, {}, { plugins: [router] })
  try {
    await flush()
    const input = () => view.all().find(node => node.props['aria-label'] === '插件编码修正意见')
    input().props.onInput({ target: { value: 'My unsubmitted draft' } })
    await nextTick()
    view.button('继续讨论').props.onClick()
    await flush()
    assert.equal(input().value, 'My unsubmitted draft')
    assert.ok(view.all().find(node => typeof node.props.title === 'string' && node.props.title.includes('还有未提交内容')))
    assert.equal(submissions, 0)
    input().props.onInput({ target: { value: '' } })
    await nextTick()
    view.button('继续讨论').props.onClick()
    await flush()
    assert.equal(input().value, 'Explain the saved plugin')
    assert.equal(input().focused, true)
    assert.equal(view.button('讨论').props['aria-pressed'], true)
    assert.equal(submissions, 0)
    assert.ok(view.all().find(node => node.props['aria-label'] === '发送插件讨论'))
    assert.match(view.all().map(view.textOf).join(''), /讨论未完成，可重试/)
  } finally { view.stop() }
})

test('an active discussion reports analysis using the authoritative source phase while the workspace generates', async () => {
  developmentApi.resources = async () => resourceCatalog()
  fakeApi.get = async () => ({ ...value('one', 1, 'running'), release_id: 'release-one', phase: 'generating', source_phase: 'released', plugin_version: '1.0.0', turns: [{ id: 'discuss-one', instruction: 'Explain the saved plugin', mode: 'discuss', status: 'running', created_at: '2026-10-05T14:00:00Z' }], active_run_id: 'discuss-one', resource_selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, resource_receipts: [] })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/project/:releaseId', component: Workbench }] })
  await router.push('/project/release-one')
  const view = mountEditorComponent(Workbench, { workspaceId: 'one', releaseId: 'release-one' }, {}, { plugins: [router] })
  try {
    await flush()
    const status = view.all().filter(node => node.props.role === 'status').map(view.textOf)
    assert.ok(status.includes('AI 讨论中'))
    assert.ok(status.includes('正在分析项目'))
    assert.ok(view.all().find(node => node.type === 'span' && view.textOf(node) === '插件已定版'))
    assert.ok(view.all().find(node => node.type === 'span' && view.textOf(node) === '讨论 · 正在分析'))
    assert.equal(view.all().some(node => node.text === '候选生成中'), false)
    assert.equal(status.includes('正在编写代码'), false)
  } finally { view.stop() }
})

test('queued discussions wait for analysis and running discussions report analysis', async () => {
  const view = mountEditorComponent(Conversation, { workspace: { revision: 1, turns: [{ id: 'queued-discussion', instruction: 'Explain the capability requirements', mode: 'discuss', status: 'queued', created_at: '2026-10-05T14:00:00Z' }, { id: 'running-discussion', instruction: 'Explain the project files', mode: 'discuss', status: 'running', created_at: '2026-10-05T14:01:00Z' }], events: [], validation: [], phase: 'generating', source_phase: 'released', resource_receipts: [] } })
  try {
    const statuses = view.all().filter(node => node.props.class === 'turn-status').map(view.textOf)
    assert.deepEqual(statuses, ['讨论 · 等待分析', '讨论 · 正在分析'])
  } finally { view.stop() }
})

test('settings keyboard close and cancellation return focus to the actual header or composer opener', async () => {
  developmentApi.resources = async () => resourceCatalog()
  const workspace = { ...value('one'), release_id: 'release-one', phase: 'released', plugin_version: '1.0.0', turns: [], active_run_id: null, resource_selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, resource_receipts: [] }
  fakeApi.get = async () => workspace
  fakeApi.settings = async () => workspace
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/project/:releaseId', component: Workbench }] })
  await router.push('/project/release-one')
  const view = mountEditorComponent(Workbench, { workspaceId: 'one', releaseId: 'release-one' }, {}, { plugins: [router] })
  try {
    await flush()
    const header = view.all().find(node => node.props['aria-label'] === '打开编码 AI 设置')
    const composer = view.all().find(node => node.props['aria-label'] === '编码 AI 设置：模型、技能与 MCP')
    const drawer = () => view.all().find(node => node.type === 'section' && node.props.title === '编码 AI 设置')
    header.props.onClick({ currentTarget: header })
    await nextTick()
    const afterCancel = drawer().props.onClosed
    view.button('取消').props.onClick()
    await afterCancel()
    assert.equal(header.focused, true)
    header.focused = false
    composer.props.onClick()
    await nextTick()
    const keyboardDrawer = drawer()
    keyboardDrawer.props['before-close'](() => keyboardDrawer.props['onUpdate:modelValue'](false))
    await keyboardDrawer.props.onClosed()
    assert.equal(composer.focused, true)
    assert.equal(header.focused, false)
    composer.focused = false
    composer.props.onClick()
    await nextTick()
    const afterSave = drawer().props.onClosed
    view.button('保存设置').props.onClick()
    await flush()
    await afterSave()
    assert.equal(composer.focused, true)
  } finally { view.stop() }
})

test('settings do not return focus to a disconnected opener or a replaced modal', async () => {
  let focused = 0
  const opener = { isConnected: false, focus: () => { focused++ } }
  const view = mountEditorComponent(CodingSettings, { modelValue: true, selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, catalog: resourceCatalog(), loading: false, saving: false, error: '', opener })
  try {
    const drawer = () => view.all().find(node => node.type === 'section' && node.props.title === '编码 AI 设置')
    const closed = drawer().props.onClosed
    view.props.value = { ...view.props.value, modelValue: false }
    await closed()
    assert.equal(focused, 0)
    view.props.value = { ...view.props.value, modelValue: true, opener: { ...opener, isConnected: true } }
    await nextTick()
    const replacement = drawer().props.onClosed
    view.props.value = { ...view.props.value, modelValue: false, opener: null }
    await replacement()
    assert.equal(focused, 0)
  } finally { view.stop() }
})

test('unmounting the settings while its close callback is pending refuses stale focus restoration', async () => {
  let focused = 0
  const view = mountEditorComponent(CodingSettings, { modelValue: true, selection: { llm_config_id: 'model', skill_ids: [], mcp_ids: [] }, catalog: resourceCatalog(), loading: false, saving: false, error: '', opener: { isConnected: true, focus: () => { focused++ } } })
  const closed = view.all().find(node => node.type === 'section' && node.props.title === '编码 AI 设置').props.onClosed
  view.props.value = { ...view.props.value, modelValue: false }
  const pending = closed()
  view.stop()
  await pending
  assert.equal(focused, 0)
})

function mountCodingResources(persist) {
  const scenario = ref('one')
  const selection = ref({ llm_config_id: 'model', skill_ids: [], mcp_ids: [] })
  let state
  const app = renderer.createApp({ setup() { state = usePluginCodingSettings(scenario, selection, persist); return () => h('div') } })
  app.mount({})
  return { state, scenario, selection, stop: () => app.unmount() }
}

test('changing the settings scenario aborts the catalog read and ignores a late resource list', async () => {
  const old = deferred()
  let oldSignal
  developmentApi.resources = (id, signal) => { if (id === 'one') { oldSignal = signal; return old.promise } return Promise.resolve(resourceCatalog()) }
  const view = mountCodingResources()
  try {
    view.scenario.value = 'two'
    await flush()
    assert.equal(oldSignal.aborted, true)
    old.resolve({ models: [], skills: [], mcps: [], base_tools: [] })
    await flush()
    assert.equal(view.state.catalog.value.models[0].id, 'model')
    assert.equal(view.selection.value.llm_config_id, 'model')
  } finally { view.stop() }
})

test('failed or interrupted settings persistence does not claim a local resource selection was saved', async () => {
  developmentApi.resources = async () => resourceCatalog()
  const pending = deferred()
  let calls = 0
  const view = mountCodingResources(async () => { calls++; if (calls === 1) throw new Error('保存冲突'); return pending.promise })
  await flush()
  view.state.open.value = true
  const requested = { llm_config_id: 'model', skill_ids: ['skill-one'], mcp_ids: [] }
  assert.equal(await view.state.save(requested), false)
  assert.equal(view.state.open.value, true)
  assert.equal(view.state.error.value, '保存冲突')
  assert.deepEqual(view.selection.value.skill_ids, [])
  const saving = view.state.save(requested)
  assert.equal(await view.state.save(requested), false)
  view.stop()
  pending.resolve(true)
  assert.equal(await saving, false)
  assert.deepEqual(view.selection.value.skill_ids, [])
  assert.equal(calls, 2)
})
