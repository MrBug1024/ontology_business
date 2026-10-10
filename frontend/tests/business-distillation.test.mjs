import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, h, nextTick, ref } from 'vue'
import { draftOf, emptyDistillationDocument, isMaterialReferenceOnlyChange, removeEvidence } from '../src/utils/businessDistillation.ts'

test('removing proof clears graph citations and downgrades an unsupported fact', () => {
  const document = emptyDistillationDocument()
  document.evidence = [{ key: 'proof' }]
  document.assertions = [{ key: 'claim', statement: 'Completed', status: 'fact', evidence_refs: ['proof'] }]
  document.as_is.nodes = [{ key: 'step', evidence_refs: ['proof'] }]
  document.lineage = [{ source: 'a', target: 'b', evidence_refs: ['proof'] }]
  removeEvidence(document, 'proof')
  assert.deepEqual(document.evidence, [])
  assert.equal(document.assertions[0].status, 'hypothesis')
  assert.deepEqual(document.assertions[0].evidence_refs, [])
  assert.deepEqual(document.as_is.nodes[0].evidence_refs, [])
  assert.deepEqual(document.lineage[0].evidence_refs, [])
})

test('editing a draft does not mutate saved evidence or historical versions', () => {
  const row = { id: 'p', name: 'Original', scenario_id: null, revision: 1, document: emptyDistillationDocument() }
  const draft = draftOf(row)
  draft.document.to_be.nodes.push({ key: 'new' })
  assert.equal(row.document.to_be.nodes.length, 0)
})

test('a library reference is the only draft change that can be persisted with the next question', () => {
  const row = { id: 'p', name: 'Original', scenario_id: null, revision: 1, document: emptyDistillationDocument() }
  const baseline = draftOf(row)
  const withReference = draftOf(row)
  withReference.document.evidence.push({ key: 'library_source', title: '业务资料', kind: 'material', role: 'reference', data_source_id: 'source', bucket_file_id: null, summary: '', coverage: '', limitations: '' })
  assert.equal(isMaterialReferenceOnlyChange(withReference, baseline), true)
  withReference.document.pain = '新增的阶段编辑'
  assert.equal(isMaterialReferenceOnlyChange(withReference, baseline), false)
})

test('sending a reference selection saves only that selection before enqueueing the turn', () => {
  const source = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
  const send = source.slice(source.indexOf('async function sendMessage'), source.indexOf('function acceptProjectUpdate'))
  assert.match(send, /unsavedStageChanges\.value/)
  assert.match(send, /if \(row && materialOnlyDirty\.value\) \{[\s\S]*?row = await saveDraft\(false\)/)
  assert.match(send, /if \(!row\) return/)
  assert.ok(send.indexOf('saveDraft(false)') < send.indexOf('await send(text'))
})

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function row(id, revision = 1) { return { id, revision, name: id, scenario_id: null, document: emptyDistillationDocument(), can_write: true } }
const fakeApi = {
  list: async () => [], scenarios: async () => [], materials: async () => ({ items: [], has_more: false, next_offset: null }),
  scenarioState: async scenarioId => ({ scenario_id: scenarioId, revision: 1, document: emptyDistillationDocument(), updated_at: '2026-09-21T00:00:00Z' }),
  get: async id => row(id), update: async (id, revision, draft) => ({ ...row(id, revision + 1), ...draft }),
}
globalThis.__distillationTestApi = fakeApi
const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const transpile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const utilityUrl = encode(transpile('../src/utils/businessDistillation.ts'))
const apiUrl = encode('export const businessDistillationApi = globalThis.__distillationTestApi')
const composableSource = transpile('../src/composables/useBusinessDistillation.ts')
  .replace("from 'vue'", `from '${vueUrl}'`)
  .replace("from '@/api/businessDistillation'", `from '${apiUrl}'`)
  .replace("from '@/utils/businessDistillation'", `from '${utilityUrl}'`)
const { useBusinessDistillation } = await import(encode(composableSource))
const renderer = createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
function mount(id, scope = '', lockScope = false) {
  const projectId = ref(id)
  const historyScope = ref(scope)
  let state
  const app = renderer.createApp({ setup() { state = useBusinessDistillation(projectId, historyScope, lockScope); return () => h('div') } })
  app.mount({})
  return { state, projectId, historyScope, stop: () => app.unmount() }
}
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

test('switching project aborts prior reads and ignores a late response', async () => {
  const old = deferred()
  let oldSignal
  fakeApi.get = (id, signal) => id === 'old' ? (oldSignal = signal, old.promise) : Promise.resolve(row(id))
  const { state, projectId, stop } = mount('old')
  projectId.value = 'new'
  await flush()
  assert.equal(oldSignal.aborted, true)
  assert.equal(state.project.value.id, 'new')
  old.resolve(row('old'))
  await flush()
  assert.equal(state.project.value.id, 'new')
  stop()
})

test('CAS conflict keeps the edited draft and previous revision', async () => {
  fakeApi.get = async id => row(id)
  fakeApi.update = async () => { throw Object.assign(new Error('conflict'), { status: 409 }) }
  const { state, stop } = mount('project')
  await flush()
  state.draft.value.document.pain = 'Unsaved reasoning'
  assert.equal(await state.save(), null)
  assert.equal(state.draft.value.document.pain, 'Unsaved reasoning')
  assert.equal(state.project.value.revision, 1)
  assert.equal(state.dirty.value, true)
  assert.match(state.error.value, /草稿已保留/)
  stop()
})

test('new conversations inherit the chosen scenario without becoming an unsaved shared draft', async () => {
  let saved
  fakeApi.create = async draft => { saved = JSON.parse(JSON.stringify(draft)); return { ...row('created'), ...draft } }
  const { state, stop } = mount('', 'scenario-a', true)
  await flush()
  assert.equal(state.draft.value.scenario_id, 'scenario-a')
  assert.equal(state.dirty.value, false)
  state.draft.value.name = 'First question'
  await state.save()
  assert.equal(saved.scenario_id, 'scenario-a')
  assert.equal(saved.expected_scenario_revision, 1)
  stop()
})

test('scenario history is filtered on the server, and the default history is the unscoped project list', async () => {
  const old = deferred(); const scopes = []; let oldSignal
  fakeApi.list = async (offset, signal, scope) => { scopes.push(scope); if (scope === 'a') { oldSignal = signal; return old.promise }; return [row(scope)] }
  const { state, historyScope, stop } = mount('', 'a')
  historyScope.value = 'b'
  await flush()
  assert.equal(oldSignal.aborted, true)
  old.resolve([row('old-a')])
  await flush()
  assert.deepEqual(state.projects.value.map(project => project.id), ['b'])
  assert.equal(scopes.at(-1), 'b')
  assert.equal(state.draft.value.scenario_id, 'b')
  historyScope.value = ''
  await flush()
  assert.deepEqual(state.projects.value.map(project => project.id), ['shared'])
  assert.equal(scopes.at(-1), 'shared')
  assert.equal(state.draft.value.scenario_id, null)
  stop()
})

test('scenario workspace fixes distillation to the current scenario while the old route remains a thin compatibility shell', async () => {
  const legacyView = readFileSync(new URL('../src/views/BusinessDistillation.vue', import.meta.url), 'utf8')
  const workspace = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
  const scenarioDetail = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')

  assert.match(legacyView, /<DistillationWorkspace\s*\/>/)
  assert.match(scenarioDetail, /<DistillationWorkspace[^>]*embedded[^>]*:scenario-id="scenarioId"/)
  assert.match(workspace, /useBusinessDistillation\(projectId, historyScope, props\.embedded\)/)
  assert.match(workspace, /const authorizedProjectId = computed/)
  assert.match(workspace, /useDistillationConversation\(authorizedProjectId, draftKey\)/)
  assert.match(workspace, /useDistillationAttachments\(authorizedProjectId, selectedScenario\)/)
  assert.match(workspace, /row\.scenario_id !== props\.scenarioId/)
  assert.match(workspace, /v-if="!embedded" class="discovery-sources"/)
  assert.match(workspace, /v-if="!embedded"[\s\S]*?placeholder="选择场景"/)
  assert.match(scenarioDetail, /\['postgres', 'mysql', 'sqlite3', 'dataset'\]\.includes\(source\.type\)/)

  fakeApi.get = async id => ({ ...row(id), scenario_id: 'other-scenario' })
  const { state, stop } = mount('foreign-project', 'scenario-a', true)
  await flush()
  assert.equal(state.project.value, null)
  assert.match(state.error.value, /不属于当前业务场景/)
  stop()
})

test('scenario material picker uses the bounded catalog and resets its page when scope changes', async () => {
  const requests = []
  fakeApi.materials = async (scenarioId, offset, limit) => {
    requests.push({ scenarioId, offset, limit })
    return { items: [{ id: `${scenarioId}-${offset}`, name: 'Material', scenario_id: scenarioId, type: 'file_bucket', config: {} }], has_more: offset === 0, next_offset: offset === 0 ? 50 : null }
  }
  const { state, historyScope, stop } = mount('', 'scenario-a', true)
  await flush()
  assert.deepEqual(requests.at(-1), { scenarioId: 'scenario-a', offset: 0, limit: 50 })
  assert.equal(state.materials.value[0].id, 'scenario-a-0')
  state.nextMaterialPage()
  await flush()
  assert.deepEqual(requests.at(-1), { scenarioId: 'scenario-a', offset: 50, limit: 50 })
  historyScope.value = 'scenario-b'
  await flush()
  assert.equal(state.materialOffset.value, 0)
  assert.deepEqual(requests.at(-1), { scenarioId: 'scenario-b', offset: 0, limit: 50 })
  stop()
})

test('scenario readers can inspect distillation context and browse material pages without write access', () => {
  const workspace = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
  const picker = readFileSync(new URL('../src/components/distillation/DistillationLibraryPicker.vue', import.meta.url), 'utf8')

  assert.match(workspace, /:disabled="\(!canEdit && !project\) \|\| actionBusy \|\| !!active \|\| loading"/)
  assert.match(workspace, /function openSources\(\)[\s\S]*?if \(\(!canEdit\.value && !project\.value\) \|\| actionBusy\.value \|\| active\.value \|\| loading\.value\) return/)
  assert.match(workspace, /async function openSystems\(\)[\s\S]*?if \(\(!canEdit\.value && !project\.value\) \|\| actionBusy\.value \|\| active\.value \|\| loading\.value\) return/)
  assert.match(picker, /:disabled="loading \|\| !offset"/)
  assert.match(picker, /:disabled="loading \|\| !hasMore"/)
})

test('distillation stays a single evolving conclusion set without in-workspace publication or version management', () => {
  const workspace = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
  const canvas = readFileSync(new URL('../src/components/distillation/DistillationCanvas.vue', import.meta.url), 'utf8')
  const conversation = readFileSync(new URL('../src/components/distillation/DistillationConversation.vue', import.meta.url), 'utf8')

  assert.doesNotMatch(workspace, /DistillationPublishDecisionDialog|ScenarioBusinessContextPanel/)
  assert.doesNotMatch(workspace, /保存到资料库|交付物|showScenarioContext/)
  assert.doesNotMatch(canvas, /保存到资料库|交付物|publications|版本/)
  assert.doesNotMatch(conversation, /版本/)
  assert.match(conversation, /openDelivery\(step\.delivery\)/)
  assert.match(conversation, /已提交到场景资料/)
})
