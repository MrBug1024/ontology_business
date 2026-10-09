import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, h, nextTick, ref } from 'vue'
const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { codingDiff } = await import(encode(compile('../src/utils/pluginCodingDiff.ts')))

const taskStartApi = {}
globalThis.__pluginTaskStartApi = taskStartApi
const taskStartApiUrl = encode('export const api = globalThis.__pluginTaskStartApi; export const pluginCodingApi = globalThis.__pluginTaskStartApi')
const taskRequestIdUrl = encode('let counter = 0; export const createClientRequestId = () => `synthetic-request-${++counter}`')
const taskStartSource = compile('../src/composables/usePluginTaskStart.ts').replace("from 'vue'", `from '${new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href}'`).replace("from '@/api'", `from '${taskStartApiUrl}'`).replace("from '@/api/pluginCoding'", `from '${taskStartApiUrl}'`).replace("from '@/utils/clientRequestId'", `from '${taskRequestIdUrl}'`)
const { usePluginTaskStart } = await import(encode(taskStartSource))
function taskContext(id) { return { scenario: { id: 'scene' }, deployment: { release_id: id }, capabilities: [{ kind: 'workflow', key: id, name: id, readiness: { ready: true, issues: [] } }] } }
const taskStartRenderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function mountTaskStart() { const release = ref({ id: 'first', scenario_id: 'scene', revision: 1, enabled: true }); let state; const app = taskStartRenderer.createApp({ setup() { state = usePluginTaskStart(release); return () => h('div') } }); app.mount({}); return { release, state, stop: () => app.unmount() } }
function resourceCatalog(supportsTools = true) { return { models: [{ id: 'model', name: 'Model', model: 'model', supports_tools: supportsTools }], skills: [{ id: 'authoring-skill', name: 'Authoring', version: '1.0.0', mode: 'instructions' }], mcps: [{ id: 'reference-mcp', name: 'References', transport: 'streamable_http', mode: 'read_only_resources' }], base_tools: [] } }
function resetTaskApi() { taskStartApi.context = async id => taskContext(id); taskStartApi.resources = async () => resourceCatalog(); taskStartApi.start = async () => ({ id: 'workspace' }) }

test('installed coding skills and MCP selection enter the created task and survive a rejected submission', async () => {
  resetTaskApi()
  const submitted = []
  taskStartApi.start = async (id, body) => { submitted.push(body); if (submitted.length === 1) throw new Error('Please retry'); return { id: 'workspace' } }
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build using installed methods and managed references'
    view.state.resourceSelection.value = { llm_config_id: 'model', skill_ids: ['authoring-skill'], mcp_ids: ['reference-mcp'] }
    assert.equal(await view.state.start(), null)
    assert.deepEqual(view.state.resourceSelection.value.skill_ids, ['authoring-skill'])
    assert.deepEqual(view.state.resourceSelection.value.mcp_ids, ['reference-mcp'])
    assert.equal((await view.state.start()).id, 'workspace')
    assert.deepEqual(submitted[1].skill_ids, ['authoring-skill'])
    assert.deepEqual(submitted[1].mcp_ids, ['reference-mcp'])
    assert.equal(submitted[0].request_id, submitted[1].request_id)
  } finally { view.stop() }
})

test('coding task refuses a removed extension and a model without native MCP tools', async () => {
  resetTaskApi()
  taskStartApi.resources = async () => resourceCatalog(false)
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build a plugin'
    view.state.resourceSelection.value = { llm_config_id: 'model', skill_ids: [], mcp_ids: ['reference-mcp'] }
    assert.equal(await view.state.start(), null)
    view.state.resourceSelection.value = { llm_config_id: 'model', skill_ids: ['removed'], mcp_ids: [] }
    assert.equal(await view.state.start(), null)
  } finally { view.stop() }
})

test('a coding task starts with scenario contracts and instructions without fabricated acceptance', async () => {
  resetTaskApi()
  let submitted
  taskStartApi.start = async (id, body) => { submitted = { id, body }; return { id: 'workspace' } }
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build a custom Skill and client script'
    assert.equal(view.state.canStart.value, true)
    assert.equal((await view.state.start()).id, 'workspace')
    assert.equal(submitted.id, 'first')
    assert.deepEqual(submitted.body.capabilities, [{ kind: 'workflow', key: 'first' }])
    assert.equal('acceptance_cases' in submitted.body, false)
    assert.equal('confirmed_business_acceptance' in submitted.body, false)
  } finally { view.stop() }
})

test('choosing Codex refreshes its delivery contract and changes the durable task target without losing requirements', async () => {
  resetTaskApi()
  const contexts = [], requests = []
  taskStartApi.context = async (id, signal, target) => { contexts.push(target); return { ...taskContext(id), delivery_profile: { host: { key: target } } } }
  taskStartApi.start = async (id, body) => { requests.push(body); throw new Error('Retry') }
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Keep the business requirements'
    await view.state.start()
    view.state.target.value = 'codex'
    await flush()
    assert.equal(view.state.instruction.value, 'Keep the business requirements')
    assert.equal(view.state.context.value.delivery_profile.host.key, 'codex')
    await view.state.start()
    await view.state.start()
    assert.deepEqual(contexts, ['claude_code', 'codex'])
    assert.equal(requests[1].target, 'codex')
    assert.notEqual(requests[0].request_id, requests[1].request_id)
    assert.equal(requests[1].request_id, requests[2].request_id)
  } finally { view.stop() }
})

test('a late host profile cannot replace the selected Codex contract', async () => {
  resetTaskApi()
  let resolveClaude, oldSignal
  taskStartApi.context = (id, signal, target) => target === 'claude_code' ? new Promise(resolve => { resolveClaude = resolve; oldSignal = signal }) : Promise.resolve({ ...taskContext(id), delivery_profile: { host: { key: target } } })
  const view = mountTaskStart()
  try {
    view.state.target.value = 'codex'
    await flush()
    assert.equal(oldSignal.aborted, true)
    resolveClaude({ ...taskContext('first'), delivery_profile: { host: { key: 'claude_code' } } })
    await flush()
    assert.equal(view.state.context.value.delivery_profile.host.key, 'codex')
  } finally { view.stop() }
})

test('changing scenario contract cancels and rejects old context while preserving user requirements', async () => {
  resetTaskApi()
  let resolveOld
  let oldSignal
  taskStartApi.context = (id, signal) => id === 'first' ? new Promise(resolve => { resolveOld = resolve; oldSignal = signal }) : Promise.resolve(taskContext(id))
  const view = mountTaskStart()
  try {
    view.state.instruction.value = 'Keep these requirements'
    view.release.value = { id: 'second', scenario_id: 'scene', revision: 2, enabled: true }
    await flush()
    assert.equal(oldSignal.aborted, true)
    resolveOld(taskContext('first'))
    await flush()
    assert.equal(view.state.context.value.deployment.release_id, 'second')
    assert.equal(view.state.instruction.value, 'Keep these requirements')
  } finally { view.stop() }
})

test('retry reuses the same task identity and preserves instructions after a transport failure', async () => {
  resetTaskApi()
  const requests = []
  taskStartApi.start = async (id, body) => { requests.push(body); if (requests.length === 1) throw new Error('Request failed'); return { id: 'workspace' } }
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build plugin'
    assert.equal(await view.state.start(), null)
    assert.equal(view.state.instruction.value, 'Build plugin')
    assert.equal((await view.state.start()).id, 'workspace')
    assert.equal(requests[0].request_id, requests[1].request_id)
  } finally { view.stop() }
})

test('a catalog from a different scene or release cannot start a coding task', async () => {
  resetTaskApi()
  taskStartApi.context = async () => taskContext('other-release')
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build plugin'
    assert.equal(view.state.context.value, null)
    assert.equal(await view.state.start(), null)
    assert.match(view.state.error.value, /不属于当前场景版本/)
  } finally { view.stop() }
})

test('task start refuses invalid versions, missing scope and repeated pending submissions', async () => {
  resetTaskApi()
  let resolveTask
  let calls = 0
  taskStartApi.start = () => { calls++; return new Promise(resolve => { resolveTask = resolve }) }
  const view = mountTaskStart()
  try {
    await flush()
    view.state.instruction.value = 'Build plugin'
    view.state.pluginVersion.value = '01.0.0'
    assert.equal(await view.state.start(), null)
    view.state.pluginVersion.value = '1.0.0'
    view.state.selected['workflow:first'] = false
    assert.equal(await view.state.start(), null)
    view.state.selected['workflow:first'] = true
    const pending = view.state.start()
    assert.equal(await view.state.start(), null)
    resolveTask({ id: 'workspace' })
    await pending
    assert.equal(calls, 1)
  } finally { view.stop() }
})
test('line review represents insertion, deletion and unchanged context with correct line numbers', () => {
  const diff = codingDiff('first\nold\nlast', 'first\nnew\nlast')
  assert.deepEqual(diff.map(line => [line.kind, line.before, line.after]), [['same', 1, 1], ['removed', 2, null], ['added', null, 2], ['same', 3, 3]])
  assert.equal(diff.filter(line => line.kind !== 'removed').map(line => line.text).join('\n'), 'first\nnew\nlast')
})
test('large changed regions remain bounded and reconstruct both files without inventing lines', () => {
  const before = Array.from({ length: 500 }, (_, i) => `old-${i}`).join('\n')
  const after = Array.from({ length: 500 }, (_, i) => `next-${i}`).join('\n')
  const lines = codingDiff(before, after)
  assert.equal(lines.length, 1000)
  assert.equal(lines.filter(line => line.kind !== 'added').map(line => line.text).join('\n'), before)
  assert.equal(lines.filter(line => line.kind !== 'removed').map(line => line.text).join('\n'), after)
  assert.deepEqual(codingDiff('', ''), [])
})
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const requestUrl = encode(compile('../src/utils/clientRequestId.ts'))
const source = compile('../src/composables/usePluginCodingEditor.ts').replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/utils/clientRequestId'", `from '${requestUrl}'`)
const { usePluginCodingEditor } = await import(encode(source))
const renderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function value(hash = 'a', content = 'original') { return { revision: 1, files_hash: hash, files: [{ path: 'skills/run-scenario/SKILL.md', content, editable: true }, { path: 'server.py', content: 'trusted', editable: false }] } }
function mount(revise) { const workspace = ref(value()); let editor; const app = renderer.createApp({ setup() { editor = usePluginCodingEditor(workspace, revise); return () => h('div') } }); app.mount({}); return { editor, workspace, stop: () => app.unmount() } }
test('file conflict preserves the local edit and feedback until an explicit basis review', async () => {
  let calls = 0
  const view = mount(async () => { calls++; return false })
  try {
    await nextTick()
    view.editor.draft.value = 'my correction'
    view.editor.feedback.value = 'complete the example'
    assert.equal(await view.editor.submit('generate'), false)
    view.workspace.value = value('b', 'concurrent correction')
    await nextTick()
    assert.equal(view.editor.draft.value, 'my correction')
    assert.equal(view.editor.basisChanged.value, true)
    assert.equal(await view.editor.submit('generate'), false)
    assert.equal(calls, 1)
    view.editor.baseHash.value = 'b'
    await view.editor.submit('generate')
    assert.equal(calls, 2)
    assert.equal(view.editor.feedback.value, 'complete the example')
  } finally { view.stop() }
})
test('unsubmitted file edits cannot be silently discarded by choosing another file', async () => {
  const view = mount(async () => true)
  try {
    await nextTick()
    view.editor.draft.value = 'my draft'
    view.editor.selectFile('server.py')
    assert.equal(view.editor.selectedPath.value, 'skills/run-scenario/SKILL.md')
    assert.match(view.editor.message.value, /未保存/)
    view.editor.discardDraft()
    view.editor.selectFile('server.py')
    assert.equal(view.editor.dirty.value, false)
    assert.equal(view.editor.draft.value, 'trusted')
  } finally { view.stop() }
})
test('stopping a round keeps local drafts and never claims they were persisted', async () => {
  let submitted
  const view = mount(async payload => { submitted = payload; return true })
  try {
    await nextTick()
    view.editor.draft.value = 'local draft'
    view.editor.feedback.value = 'next requirement'
    await view.editor.stopCoding()
    assert.deepEqual(submitted.files, [])
    assert.equal(submitted.action, 'stop')
    assert.equal(view.editor.dirty.value, true)
    assert.equal(view.editor.feedback.value, 'next requirement')
  } finally { view.stop() }
})

const contextApi = {}
globalThis.__pluginStudioContextApi = contextApi
const contextApiUrl = encode('export const pluginCodingApi = globalThis.__pluginStudioContextApi; export const scenarioReleasesApi = globalThis.__pluginStudioContextApi')
const contextSource = compile('../src/composables/usePluginStudioContext.ts').replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/pluginCoding'", `from '${contextApiUrl}'`).replace("from '@/api/scenarioReleases'", `from '${contextApiUrl}'`)
const { usePluginStudioContext } = await import(encode(contextSource))
function mountContext() { const id = ref('first'); let state; const app = renderer.createApp({ setup() { state = usePluginStudioContext(id); return () => h('div') } }); app.mount({}); return { id, state, stop: () => app.unmount() } }
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
test('release parameter change aborts old context and refuses its late response', async () => {
  let resolveOld
  let oldSignal
  const delayed = new Promise(resolve => { resolveOld = resolve })
  contextApi.get = (id, signal) => { if (id === 'first') { oldSignal = signal; return delayed }; return Promise.resolve({ id }) }
  contextApi.recent = async () => []
  const view = mountContext()
  try {
    view.id.value = 'second'
    await flush()
    assert.equal(oldSignal.aborted, true)
    resolveOld({ id: 'first' })
    await flush()
    assert.equal(view.state.release.value.id, 'second')
    assert.equal(view.state.loading.value, false)
  } finally { view.stop() }
})
test('context retry preserves saved history and recovers from a failed read', async () => {
  let failing = false
  contextApi.get = async id => ({ id })
  contextApi.recent = async () => { if (failing) throw new Error('会话暂不可读'); return [{ id: 'saved' }] }
  const view = mountContext()
  try {
    await flush()
    failing = true
    await view.state.load()
    assert.equal(view.state.recent.value[0].id, 'saved')
    assert.equal(view.state.error.value, '会话暂不可读')
    failing = false
    await view.state.load()
    assert.equal(view.state.error.value, '')
  } finally { view.stop() }
})

const artifactApi = {}
globalThis.__pluginArtifactTestApi = artifactApi
const artifactApiUrl = encode('export const pluginArtifactsApi = globalThis.__pluginArtifactTestApi')
const artifactSource = compile('../src/composables/usePluginArtifacts.ts').replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/pluginArtifacts'", `from '${artifactApiUrl}'`)
const { usePluginArtifacts } = await import(encode(artifactSource))
function mountArtifacts(scope = 'first', id = '') {
  const scenarioId = ref(scope)
  const artifactId = ref(id)
  let state
  const app = renderer.createApp({ setup() { state = usePluginArtifacts(scenarioId, artifactId); return () => h('div') } })
  app.mount({})
  return { scenarioId, artifactId, state, stop: () => app.unmount() }
}
const pageOf = items => ({ items, offset: 0, limit: 50, has_more: false })

test('publishing refuses a late version list after the scenario changes', async () => {
  let resolveOld
  let oldSignal
  artifactApi.list = (id, offset, signal) => id === 'first' ? (oldSignal = signal, new Promise(resolve => { resolveOld = resolve })) : Promise.resolve(pageOf([{ id: 'second-plugin' }]))
  const view = mountArtifacts()
  try {
    view.scenarioId.value = 'second'
    await flush()
    assert.equal(oldSignal.aborted, true)
    resolveOld(pageOf([{ id: 'old-plugin' }]))
    await flush()
    assert.equal(view.state.items.value[0].id, 'second-plugin')
    assert.equal(view.state.selected.value, null)
  } finally { view.stop() }
})

test('publishing deep link restores its reviewed version even outside the first page', async () => {
  artifactApi.list = async () => pageOf([{ id: 'newest', scenario_id: 'first' }])
  artifactApi.get = async id => ({ id, scenario_id: 'first', available: true })
  const view = mountArtifacts('first', 'historical')
  try {
    await flush()
    assert.equal(view.state.selected.value.id, 'historical')
    assert.equal(view.state.items.value[0].id, 'newest')
  } finally { view.stop() }
})

test('a selected plugin from another scenario never enters the publication detail', async () => {
  artifactApi.list = async () => pageOf([])
  artifactApi.get = async id => ({ id, scenario_id: 'other' })
  const view = mountArtifacts('first', 'foreign')
  try {
    await flush()
    assert.equal(view.state.selected.value, null)
    assert.match(view.state.error.value, /不属于当前业务场景/)
  } finally { view.stop() }
})

test('publication retry recovers a failed read and preserves disabled state from the server', async () => {
  let failing = true
  artifactApi.list = async () => { if (failing) throw new Error('暂不可读'); return pageOf([{ id: 'disabled', scenario_id: 'first', available: false }]) }
  const view = mountArtifacts('first', 'disabled')
  try {
    await flush()
    assert.equal(view.state.selected.value, null)
    assert.equal(view.state.error.value, '暂不可读')
    failing = false
    await view.state.load()
    assert.equal(view.state.selected.value.available, false)
    assert.equal(view.state.error.value, '')
  } finally { view.stop() }
})

const publicationApi = {}
globalThis.__pluginPublicationTestApi = publicationApi
const publicationApiUrl = encode('export const pluginPublicationsApi = globalThis.__pluginPublicationTestApi')
const publicationSource = compile('../src/composables/usePluginPublication.ts').replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/pluginPublications'", `from '${publicationApiUrl}'`)
const { usePluginPublication } = await import(encode(publicationSource))
function publicationState(id = 'first', revision = 2, status = 'unpublished') { return { publication_id: 'publication', artifact_id: id, revision, status, configuration_ready: true, available: true, unavailable_reason: '', installation: null } }
function mountPublication() {
  const artifact = ref({ id: 'first', artifact_hash: 'a'.repeat(64), available: true })
  let state
  const app = renderer.createApp({ setup() { state = usePluginPublication(artifact); return () => h('div') } })
  app.mount({})
  return { artifact, state, stop: () => app.unmount() }
}

test('publishing requires explicit confirmation and sends the selected immutable hash and revision', async () => {
  publicationApi.get = async id => publicationState(id)
  let submitted
  publicationApi.update = async (id, body) => { submitted = { id, body }; return publicationState(id, 3, 'published') }
  const view = mountPublication()
  try {
    await flush()
    assert.equal(await view.state.update('publish', false), false)
    assert.equal(submitted, undefined)
    assert.equal(await view.state.update('publish', true), true)
    assert.deepEqual(submitted, { id: 'first', body: { artifact_hash: 'a'.repeat(64), expected_revision: 2, action: 'publish', confirmed_publication: true } })
    assert.equal(view.state.publication.value.status, 'published')
  } finally { view.stop() }
})

test('duplicate publication submits are blocked while the server result is pending', async () => {
  publicationApi.get = async id => publicationState(id)
  let resolveUpdate
  let calls = 0
  publicationApi.update = id => { calls++; return new Promise(resolve => { resolveUpdate = () => resolve(publicationState(id, 3, 'published')) }) }
  const view = mountPublication()
  try {
    await flush()
    const first = view.state.update('publish', true)
    assert.equal(await view.state.update('publish', true), false)
    assert.equal(calls, 1)
    resolveUpdate()
    assert.equal(await first, true)
    assert.equal(view.state.saving.value, false)
  } finally { view.stop() }
})

test('changing selected artifact aborts a publication and ignores its late success', async () => {
  publicationApi.get = async id => publicationState(id)
  let resolveUpdate
  let updateSignal
  publicationApi.update = (id, body, signal) => { updateSignal = signal; return new Promise(resolve => { resolveUpdate = resolve }) }
  const view = mountPublication()
  try {
    await flush()
    const pending = view.state.update('publish', true)
    view.artifact.value = { id: 'second', artifact_hash: 'b'.repeat(64), available: true }
    await flush()
    assert.equal(updateSignal.aborted, true)
    resolveUpdate(publicationState('first', 3, 'published'))
    assert.equal(await pending, false)
    assert.equal(view.state.publication.value.artifact_id, 'second')
    assert.equal(view.state.publication.value.status, 'unpublished')
  } finally { view.stop() }
})

test('publication errors preserve the last server state and refresh obtains the authoritative revision', async () => {
  let revision = 2
  publicationApi.get = async id => publicationState(id, revision)
  publicationApi.update = async () => { throw new Error('发布状态已变化，请刷新') }
  const view = mountPublication()
  try {
    await flush()
    assert.equal(await view.state.update('publish', true), false)
    assert.equal(view.state.publication.value.revision, 2)
    assert.equal(view.state.publication.value.status, 'unpublished')
    assert.match(view.state.error.value, /请刷新/)
    revision = 3
    await view.state.load()
    assert.equal(view.state.publication.value.revision, 3)
    assert.equal(view.state.error.value, '')
  } finally { view.stop() }
})

test('foreign publication results cannot replace the selected plugin state', async () => {
  publicationApi.get = async id => publicationState(id)
  publicationApi.update = async () => publicationState('foreign', 3, 'published')
  const view = mountPublication()
  try {
    await flush()
    assert.equal(await view.state.update('publish', true), false)
    assert.equal(view.state.publication.value.artifact_id, 'first')
    assert.match(view.state.error.value, /不一致/)
  } finally { view.stop() }
})

test('publication unmount cancels the request without accepting a late result', async () => {
  let signal
  let resolveRead
  publicationApi.get = (id, requestSignal) => { signal = requestSignal; return new Promise(resolve => { resolveRead = resolve }) }
  const view = mountPublication()
  view.stop()
  assert.equal(signal.aborted, true)
  resolveRead(publicationState())
  await flush()
  assert.equal(view.state.publication.value, null)
})
