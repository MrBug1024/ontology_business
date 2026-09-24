import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createRenderer, h, nextTick, ref } from 'vue'
import ts from 'typescript'
import { assistantSourceDisplay } from '../src/utils/assistantSourceDisplay.ts'

const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const transpile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const fakeHttp = { get: async () => [] }
globalThis.__assistantResourcesHttp = fakeHttp
const httpModule = encode('export const http = globalThis.__assistantResourcesHttp')
const { assistantResourcesApi } = await import(encode(transpile('../src/api/assistantResources.ts').replace("from '@/api'", `from '${httpModule}'`)))
const fakeApi = { list: async () => ({ models: [], skills: [], mcps: [] }) }
globalThis.__assistantResourcesApi = fakeApi
const apiModule = encode('export const assistantResourcesApi = globalThis.__assistantResourcesApi')
const { useModelingAdvisorResources, emptyModelingAdvisorSelection } = await import(encode(transpile('../src/composables/useModelingAdvisorResources.ts')
  .replace("from 'vue'", `from '${import.meta.resolve('vue')}'`)
  .replace("from '@/api/assistantResources'", `from '${apiModule}'`)))
const renderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function mount() {
  const scope = ref('user-a/workspace-a/scenario-a'), selection = ref(emptyModelingAdvisorSelection())
  let state
  const app = renderer.createApp({ setup() { state = useModelingAdvisorResources(scope, selection); return () => h('div') } })
  app.mount({})
  return { state, scope, selection, stop: () => app.unmount() }
}
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const options = name => ({ models: [{ id: name, name, description: 'Model' }], skills: [], mcps: [] })

test('advisor selector retains only display metadata and excludes disabled or incompatible configured resources', async () => {
  const signals = [], controller = new AbortController()
  fakeHttp.get = async (path, config) => {
    signals.push(config.signal)
    if (path === '/llm-configs') return [
      { id: 'chat', name: 'Chat', model: 'model', enabled: true, capabilities: ['chat'], api_key: 'ephemeral-test-secret', base_url: 'https://example.invalid' },
      { id: 'embedding', name: 'Embedding', capabilities: ['embedding'] },
      { id: 'disabled', name: 'Disabled', enabled: false },
    ]
    if (path === '/skills') return [{ id: 'method', name: 'Method', description: 'How to model', source: 'builtin', enabled: true, metadata: { script: 'private path' } }, { id: 'off', enabled: false }]
    return [{ id: 'catalog', name: 'Catalog', transport: 'streamable_http', enabled: true, headers: { Authorization: 'ephemeral-test-value' }, command: 'private command', env: { PRIVATE: 'ephemeral-test-value' } }]
  }
  const result = await assistantResourcesApi.list(controller.signal)
  assert.deepEqual(result.models, [{ id: 'chat', name: 'Chat', description: 'model' }])
  assert.deepEqual(result.skills, [{ id: 'method', name: 'Method', description: 'How to model' }])
  assert.deepEqual(result.mcps, [{ id: 'catalog', name: 'Catalog', description: '查看工具目录与输入契约' }])
  assert.doesNotMatch(JSON.stringify(result), /ephemeral|private|Authorization|api_key|base_url/)
  assert.ok(signals.every(signal => signal === controller.signal))
})

test('advisor catalogue offers only identified builtin skills and supported remote MCP transports', async () => {
  fakeHttp.get = async path => {
    if (path === '/llm-configs') return []
    if (path === '/skills') return [
      { id: 'builtin', name: 'Trusted method', description: 'Modeling', source: 'builtin', enabled: true },
      { id: 'uploaded', source: 'uploaded', enabled: true },
      { id: 'external', source: 'external', enabled: true },
      { id: 'disabled', source: 'builtin', enabled: false },
      { id: '', source: 'builtin', enabled: true },
      { source: 'builtin', enabled: true },
    ]
    return [
      ...['sse', 'streamable_http', 'http', 'stdio', 'unsupported'].map(transport => ({ id: transport, name: transport, transport, enabled: true })),
      { id: 'disabled', transport: 'sse', enabled: false },
      { name: 'Missing identifier', transport: 'sse', enabled: true },
    ]
  }
  const result = await assistantResourcesApi.list(new AbortController().signal)
  assert.deepEqual(result.skills.map(item => item.id), ['builtin'])
  assert.deepEqual(result.mcps.map(item => item.id), ['sse', 'streamable_http', 'http'])
})

test('every settings opening refreshes the catalogue and retains unavailable explicit selections', async () => {
  let calls = 0
  fakeApi.list = async () => options(++calls === 1 ? 'old' : 'new')
  const view = mount()
  await view.state.load()
  view.selection.value.llm_config_id = 'old'
  await view.state.load()
  assert.equal(calls, 2)
  assert.equal(view.state.options.value.models[0].id, 'new')
  assert.equal(view.selection.value.llm_config_id, 'old')
  assert.equal(view.state.unavailable.value, true)
  view.stop()
})

test('a late catalogue response cannot overwrite a later refresh', async () => {
  const old = deferred(), recent = deferred(); let calls = 0, oldSignal
  fakeApi.list = async signal => ++calls === 1 ? (oldSignal = signal, old.promise) : recent.promise
  const view = mount(), first = view.state.load(), second = view.state.load()
  assert.equal(oldSignal.aborted, true)
  recent.resolve(options('recent')); await second
  old.resolve(options('old')); await first
  assert.equal(view.state.options.value.models[0].id, 'recent')
  assert.equal(view.state.loading.value, false)
  view.stop()
})

test('account workspace or scenario changes clear the previous selection and abort in-flight catalogue reads', async () => {
  for (const scope of ['user-b/workspace-a/scenario-a', 'user-a/workspace-b/scenario-a', 'user-a/workspace-a/scenario-b']) {
    const pending = deferred(); let signal
    fakeApi.list = async control => (signal = control, pending.promise)
    const view = mount()
    view.selection.value = { llm_config_id: 'private-model', skill_ids: ['private-skill'], mcp_ids: ['private-mcp'] }
    const request = view.state.load()
    view.scope.value = scope
    assert.equal(signal.aborted, true)
    assert.deepEqual(view.selection.value, emptyModelingAdvisorSelection())
    pending.resolve(options('previous-scope')); await request
    assert.deepEqual(view.state.options.value.models, [])
    assert.equal(view.state.loaded.value, false)
    view.stop()
  }
})

test('load errors preserve an explicit model choice and closing settings cancels late updates', async () => {
  fakeApi.list = async () => { throw new Error('当前工作区无权读取此配置') }
  const view = mount()
  view.selection.value.llm_config_id = 'selected'
  await view.state.load()
  assert.match(view.state.error.value, /无权/)
  assert.equal(view.selection.value.llm_config_id, 'selected')
  const pending = deferred(); let signal
  fakeApi.list = async control => (signal = control, pending.promise)
  const request = view.state.load()
  view.state.cancelLoad()
  assert.equal(signal.aborted, true)
  pending.resolve(options('late')); await request
  assert.deepEqual(view.state.options.value.models, [])
  assert.equal(view.state.loading.value, false)
  view.stop()
})

test('the advisor remains a separate configured role with scoped in-memory choices and typed request fields', async () => {
  const advisor = readFileSync(new URL('../src/components/GlobalAssistant.vue', import.meta.url), 'utf8')
  const settings = readFileSync(new URL('../src/components/assistant/ModelingAdvisorSettings.vue', import.meta.url), 'utf8')
  assert.match(advisor, /ModelingAdvisorSettings v-model="assistantConfig" :scope-key="assistantResourceScopeKey"/)
  assert.match(advisor, /JSON\.stringify\(\[auth\.user\?\.id \|\| '', auth\.user\?\.tenant_id \|\| '', props\.context\.scenario_id \|\| ''\]\)/)
  assert.doesNotMatch(advisor, /assistantConfigStorageKey|restoreAssistantConfig|persistAssistantConfig|capabilitiesLoaded/)
  assert.match(advisor, /skill_ids: \[\.\.\.assistantConfig\.value\.skill_ids\]/)
  assert.doesNotMatch(advisor, /assistant_resource_context_service\.sources\(modeling_references\)/)
  assert.match(advisor, /本轮使用能力/)
  assert.match(advisor, /function capabilitiesOf\(message: AssistantMessage\)/)
  assert.match(settings, /MCP · 能力契约/)
  assert.match(settings, /Jev 决策能力由平台自动协同使用/)
  assert.match(settings, /<el-dialog v-model="open"[^>]*@open="load"[^>]*@close="cancelLoad"/)
  assert.doesNotMatch(settings, /el-popover/)
  assert.match(settings, /@click="open = true"/)
  assert.match(settings, /v-model="selection\.skill_ids" multiple :multiple-limit="5"/)
  assert.match(settings, /v-model="selection\.mcp_ids" multiple :multiple-limit="5"/)
  await nextTick()
})

const advisorSource = readFileSync(new URL('../src/components/GlobalAssistant.vue', import.meta.url), 'utf8')
const previewFunction = advisorSource.slice(advisorSource.indexOf('async function openSource('), advisorSource.indexOf('\nfunction toggleProposal('))
const previewHarness = ts.transpileModule(`
  import { assistantSourceDisplay } from '${new URL('../src/utils/assistantSourceDisplay.ts', import.meta.url).href}'
  export function createPreview(fileText, scenarioId = '') {
    const api = { fileText }, sourcePreview = { value: null }, sourcePreviewText = { value: '' }
    const sourcePreviewVisible = { value: false }, sourcePreviewLoading = { value: false }, errors = []
    const ElMessage = { error: message => errors.push(message) }, navigations = []
    const router = { push: async path => navigations.push(path) }
    const context = { value: { scenario_id: scenarioId } }
    let sourcePreviewRequest = 0
    ${previewFunction}
    return { openSource, sourcePreview, sourcePreviewText, sourcePreviewVisible, sourcePreviewLoading, errors, navigations }
  }
`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { createPreview } = await import(encode(previewHarness))

test('skill methods and MCP contract sources show an inline reference summary without reading a file', async () => {
  let reads = 0
  const preview = createPreview(async () => { reads++; return { text: 'Unexpected file body' } })
  for (const [kind, mark] of [['skill_method', '方法'], ['mcp_tool_catalog', '契约']]) {
    const source = { id: kind, kind, filename: 'Configured reference', snippet: '已经实际读取，未执行业务工具' }
    assert.equal(assistantSourceDisplay(source).mark, mark)
    assert.equal(assistantSourceDisplay(source).canPreview, true)
    assert.match(assistantSourceDisplay(source).origin, /建模参考/)
    await preview.openSource(source)
    assert.equal(preview.sourcePreviewVisible.value, true)
    assert.equal(preview.sourcePreviewText.value, source.snippet)
    assert.equal(preview.sourcePreviewLoading.value, false)
  }
  assert.equal(reads, 0)
})

test('late file previews cannot overwrite a newly selected skill or MCP reference summary', async () => {
  const pending = deferred()
  const preview = createPreview(async () => pending.promise)
  const old = preview.openSource({ id: 'file', kind: 'rag', filename: 'Old document', file_id: 'file' })
  await preview.openSource({ id: 'contract', kind: 'mcp_tool_catalog', filename: 'Contract', snippet: 'Read contract only' })
  pending.resolve({ text: 'Old file content' })
  await old
  assert.equal(preview.sourcePreviewText.value, 'Read contract only')
  assert.equal(preview.sourcePreview.value.id, 'contract')
  assert.equal(preview.sourcePreviewLoading.value, false)
  assert.deepEqual(preview.errors, [])
})

test('RAG file citations retain on-demand reads while temporary attachments remain non-clickable', async () => {
  const requested = []
  const preview = createPreview(async id => { requested.push(id); return { text: 'Current authorized document' } })
  await preview.openSource({ id: 'rag', kind: 'rag', filename: 'Document', file_id: 'authorized-file' })
  assert.deepEqual(requested, ['authorized-file'])
  assert.equal(preview.sourcePreviewText.value, 'Current authorized document')
  assert.equal(assistantSourceDisplay({ id: 'attachment', kind: 'attachment', filename: 'Upload' }).canPreview, false)
  assert.equal(assistantSourceDisplay({ id: 'rag', kind: 'rag', filename: 'Document', citation_id: 'R1' }).mark, 'R1')
})

test('immutable distillation citations open their governed library source instead of appearing as temporary attachments', async () => {
  let reads = 0
  const preview = createPreview(async () => { reads++; return { text: '' } })
  const source = { id: 'distillation:publication', kind: 'distillation', filename: '业务蒸馏 v3', data_source_id: 'source' }
  const display = assistantSourceDisplay(source)
  assert.equal(display.mark, '交接')
  assert.match(display.origin, /资料库/)
  assert.doesNotMatch(display.origin, /临时/)
  await preview.openSource(source)
  assert.deepEqual(preview.navigations, ['/data-sources?source_id=source'])
  assert.equal(preview.sourcePreviewVisible.value, false)
  assert.equal(reads, 0)
})

test('scene-scoped distillation citations stay inside the scene workspace', async () => {
  const preview = createPreview(async () => ({ text: '' }), 'scene-a')
  await preview.openSource({ id: 'distillation:publication', kind: 'distillation', filename: '业务蒸馏 v3', data_source_id: 'source' })
  assert.deepEqual(preview.navigations, [{
    name: 'scenario-detail',
    params: { id: 'scene-a' },
    query: { stage: 'materials', source_id: 'source' },
  }])
})
