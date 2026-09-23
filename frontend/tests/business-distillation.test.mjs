import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { computed, createRenderer, h, nextTick, ref } from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { handoffDecision } from '../src/utils/distillationHandoff.ts'
import { draftOf, emptyDistillationDocument, isMaterialReferenceOnlyChange, removeEvidence, removeProcessNode, reviewQuestions } from '../src/utils/businessDistillation.ts'

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

test('removing a process node removes only its attached edges', () => {
  const graph = { nodes: [{ key: 'a' }, { key: 'b' }, { key: 'c' }], edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 'a', target: 'c' }] }
  assert.deepEqual(removeProcessNode(graph, 'b'), { nodes: [{ key: 'a' }, { key: 'c' }], edges: [{ source: 'a', target: 'c' }] })
  assert.equal(graph.nodes.length, 3)
})

test('editing a draft does not mutate saved evidence or historical versions', () => {
  const row = { id: 'p', name: 'Original', scenario_id: null, revision: 1, document: emptyDistillationDocument() }
  const draft = draftOf(row)
  draft.document.to_be.nodes.push({ key: 'new' })
  assert.equal(row.document.to_be.nodes.length, 0)
  assert.ok(reviewQuestions(draft.document).some(value => value.includes('最终结果')))
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

test('published business products can be removed independently from their conversation', async () => {
  const publication = { id: 'publication-1', project_id: null, scenario_id: null, project_revision: 3, data_source_id: 'source-1', artifacts: [], created_at: '2026-09-21T00:00:00Z' }
  const previousGet = fakeApi.get, previousPublications = fakeApi.publications, previousDelete = fakeApi.deleteProduct
  let deleted
  fakeApi.get = async id => row(id)
  fakeApi.publications = async () => [publication]
  fakeApi.deleteProduct = async id => { deleted = id }
  const { state, stop } = mount('product')
  try {
    await flush()
    assert.deepEqual(state.publications.value, [publication])
    assert.equal(await state.removePublication(publication), true)
    assert.equal(deleted, publication.id)
    assert.deepEqual(state.publications.value, [])
  } finally {
    stop()
    fakeApi.get = previousGet
    fakeApi.publications = previousPublications
    fakeApi.deleteProduct = previousDelete
  }
})

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function row(id, revision = 1) { return { id, revision, name: id, scenario_id: null, document: emptyDistillationDocument(), can_write: true } }
const fakeApi = {
  list: async () => [], scenarios: async () => [], materials: async () => ({ items: [], has_more: false, next_offset: null }), publications: async () => [],
  scenarioState: async scenarioId => ({ scenario_id: scenarioId, revision: 1, document: emptyDistillationDocument(), updated_at: '2026-09-21T00:00:00Z' }),
  scenarioPublications: async () => [],
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

test('canceled analysis cannot overwrite a later proposal or save automatically', async () => {
  const pending = deferred()
  fakeApi.analyze = async () => pending.promise
  const { state, stop } = mount('project')
  await flush()
  const analysis = state.analyze('Investigate')
  state.cancelAnalysis()
  pending.resolve({ base_revision: 1, document: { ...emptyDistillationDocument(), pain: 'AI proposal' }, limitations: [] })
  await analysis
  assert.equal(state.proposal.value, null)
  assert.equal(state.draft.value.document.pain, '')
  state.proposal.value = { base_revision: 1, document: { ...emptyDistillationDocument(), pain: 'Proposal' }, limitations: [] }
  state.draft.value.document.pain = 'Human changes'
  state.applyProposal()
  assert.equal(state.draft.value.document.pain, 'Human changes')
  assert.match(state.error.value, /重新保存/)
  stop()
})

test('missing model configuration keeps the actionable server explanation and draft', async () => {
  fakeApi.analyze = async () => { throw Object.assign(new Error('请先配置可用的大模型'), { status: 409 }) }
  const { state, stop } = mount('project')
  await flush()
  await state.analyze('Find the root cause')
  assert.match(state.error.value, /请先配置可用的大模型/)
  assert.equal(state.project.value.revision, 1)
  assert.equal(state.proposal.value, null)
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
  assert.match(workspace, /props\.scenarioId \|\|/)
  assert.match(workspace, /useBusinessDistillation\(projectId, historyScope, props\.embedded\)/)
  assert.match(workspace, /const authorizedProjectId = computed/)
  assert.match(workspace, /useDistillationConversation\(authorizedProjectId, draftKey\)/)
assert.match(workspace, /useDistillationAttachments\(authorizedProjectId, selectedScenario\)/)
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


test('publishing needs a real human direction while preserving an optional explanation', () => {
  assert.throws(() => handoffDecision('undecided', ''), /请先明确/)
  assert.deepEqual(handoffDecision('stop', '  缺少结果证据  '), { decision: 'stop', decision_reason: '缺少结果证据' })
  for (const decision of ['continue', 'adjust', 'stop']) {
    const result = handoffDecision(decision, '')
    assert.equal(result.decision, decision)
    assert.ok(result.decision_reason.length > 10)
  }
})

test('explicit handoff decision survives a failed publication and can be retried at its saved revision', async () => {
  fakeApi.get = async id => row(id)
  fakeApi.update = async (id, revision, draft) => ({ ...row(id, revision + 1), ...JSON.parse(JSON.stringify(draft)) })
  const revisions = []
  let fail = true
  fakeApi.publish = async (id, revision) => {
    revisions.push(revision)
    if (fail) throw new Error('Connection interrupted')
    return { id: 'saved-publication', project_id: id, data_source_id: 'material', source_revision: revision }
  }
  const { state, stop } = mount('handoff')
  await flush()
  Object.assign(state.draft.value.document, handoffDecision('adjust', '采用改进后的闭环流程'))
  assert.equal(state.dirty.value, true)
  await state.save()
  assert.equal(state.dirty.value, false)
  assert.equal(state.project.value.document.decision, 'adjust')
  assert.equal(await state.publish(), null)
  assert.equal(state.project.value.document.decision_reason, '采用改进后的闭环流程')
  fail = false
  assert.equal((await state.publish()).id, 'saved-publication')
  assert.deepEqual(revisions, [2, 2])
  stop()
})

const decisionViewSource = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
const decisionPublishFunction = decisionViewSource.slice(decisionViewSource.indexOf('async function publishDecision('), decisionViewSource.indexOf('\nfunction openMaterial('))
const decisionPublisherModule = ts.transpileModule(`
  export function decisionPublisher(project, draft, dirty, save, publish) {
    const actionBusy = { value: false }, active = { value: false }, publishDialog = { value: true }
    let publicationOwner = { id: project.value.id, revision: project.value.revision }
    ${decisionPublishFunction}
    return { publishDecision, publishDialog }
  }
`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { decisionPublisher } = await import(encode(decisionPublisherModule))
function publisher(failingSave = async () => null) {
  const project = ref(row('deciding')), draft = ref(draftOf(project.value)), publications = []
  const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(draftOf(project.value)))
  return { project, draft, dirty, publications, ...decisionPublisher(project, draft, dirty, failingSave, async () => { publications.push(true) }) }
}

test('failed decision save restores only injected fields so the user can continue dialogue or retry', async () => {
  const state = publisher()
  await state.publishDecision(handoffDecision('stop', '等待补充依据'))
  assert.equal(state.draft.value.document.decision, 'undecided')
  assert.equal(state.draft.value.document.decision_reason, '')
  assert.equal(state.dirty.value, false)
  assert.equal(state.publishDialog.value, true)
  assert.deepEqual(state.publications, [])
})

test('failed decision rollback preserves unrelated edits and cannot overwrite a different project version or decision', async () => {
  for (const update of ['unrelated', 'project', 'revision', 'decision', 'reason', 'document']) {
    const pending = deferred(), state = publisher(async () => pending.promise)
    const saving = state.publishDecision(handoffDecision('stop', '等待依据'))
    if (update === 'unrelated') state.draft.value.document.pain = 'Newly supplied evidence'
    if (update === 'project') state.project.value.id = 'other-project'
    if (update === 'revision') state.project.value.revision = 2
    if (update === 'decision') state.draft.value.document.decision = 'continue'
    if (update === 'reason') state.draft.value.document.decision_reason = 'New human reason'
    if (update === 'document') state.draft.value = draftOf({ ...state.project.value, document: { ...state.draft.value.document, pain: 'New document' } })
    const latest = structuredClone(JSON.parse(JSON.stringify(state.draft.value.document)))
    pending.resolve(null)
    await saving
    if (update === 'unrelated') {
      assert.equal(state.draft.value.document.pain, 'Newly supplied evidence')
      assert.equal(state.draft.value.document.decision, 'undecided')
      assert.equal(state.draft.value.document.decision_reason, '')
    } else assert.deepEqual(state.draft.value.document, latest, update)
  }
})

const { descriptor: decisionDescriptor } = parse(readFileSync(new URL('../src/components/distillation/DistillationPublishDecisionDialog.vue', import.meta.url), 'utf8'))
const decisionComponentSource = compileScript(decisionDescriptor, { id: 'decision-dialog-test', inlineTemplate: true }).content
const decisionComponentModule = ts.transpileModule(decisionComponentSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
  .replaceAll('from "vue"', `from '${vueUrl}'`).replaceAll("from 'vue'", `from '${vueUrl}'`)
  .replace("from '@/utils/distillationHandoff'", `from '${new URL('../src/utils/distillationHandoff.ts', import.meta.url).href}'`)
const { default: DecisionDialog } = await import(encode(decisionComponentModule))
async function dialog(reason) {
  const open = ref(false), document = ref({ ...emptyDistillationDocument(), decision: 'stop', decision_reason: reason })
  const controls = new Map(), confirmations = []
  const app = renderer.createApp({ setup() { return () => h(DecisionDialog, { modelValue: open.value, document: document.value, busy: false, error: '', onConfirm: value => confirmations.push(value) }) } })
  for (const component of ['el-dialog', 'el-radio-group', 'el-radio', 'el-input', 'el-alert', 'el-button']) {
    app.component(component, { inheritAttrs: false, props: ['modelValue'], setup(props, context) { return () => {
      controls.set(component === 'el-button' && context.attrs.type === 'primary' ? 'confirm' : component, { ...context.attrs, ...props })
      return h('div', [context.slots.default?.(), context.slots.footer?.()])
    } } })
  }
  app.mount({})
  open.value = true
  await nextTick()
  return { controls, confirmations, document, stop: () => app.unmount() }
}

test('changing an existing default handoff reason updates it to the newly selected direction', async () => {
  const state = await dialog(handoffDecision('stop', '').decision_reason)
  state.controls.get('el-radio-group')['onUpdate:modelValue']('continue')
  await nextTick()
  state.controls.get('confirm').onClick()
  assert.deepEqual(state.confirmations, [handoffDecision('continue', '')])
  state.stop()
})

test('changing direction preserves custom reasons and a failed save rollback leaves dialog choices available for retry', async () => {
  const state = await dialog('来自访谈的人工理由')
  state.controls.get('el-radio-group')['onUpdate:modelValue']('adjust')
  await nextTick()
  state.document.value.decision = 'undecided'
  state.document.value.decision_reason = ''
  await nextTick()
  assert.equal(state.controls.get('el-radio-group').modelValue, 'adjust')
  assert.equal(state.controls.get('el-input').modelValue, '来自访谈的人工理由')
  state.controls.get('confirm').onClick()
  assert.deepEqual(state.confirmations, [handoffDecision('adjust', '来自访谈的人工理由')])
  state.stop()
})
