import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, h, nextTick, ref } from 'vue'
import { composeClarificationAnswer, conversationTitle, mergeTurns, latestArtifactProposal, splitAssistantMessage, visibleTurnError } from '../src/utils/distillationConversation.ts'

const now = '2026-09-18T02:00:00Z'
function turn(id, status = 'waiting', overrides = {}) { return { id, project_id: 'p', turn_number: 1, request_id: 'request', status, base_revision: 1, message: 'question', assistant_message: 'Please clarify', steps: [], questions: [], proposal: null, applied_revision: null, error: '', created_at: now, updated_at: now, completed_at: null, ...overrides } }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const fakeApi = { list: async () => ({ turns: [], has_more: false }), get: async () => turn('1'), send: async () => turn('1'), stream: () => new AbortController(), cancel: async () => turn('1', 'cancelled'), apply: async () => ({ id: 'p', revision: 2 }) }
globalThis.__distillationConversationApi = fakeApi
const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const transpile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const utilityUrl = encode(transpile('../src/utils/distillationConversation.ts'))
const apiUrl = encode('export const distillationConversationApi = globalThis.__distillationConversationApi')
const { useDistillationConversation } = await import(encode(transpile('../src/composables/useDistillationConversation.ts').replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/distillationConversation'", `from '${apiUrl}'`).replace("from '@/utils/distillationConversation'", `from '${utilityUrl}'`)))
const renderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function mount(id = 'p') { const projectId = ref(id); let state; const app = renderer.createApp({ setup() { state = useDistillationConversation(projectId); return () => h('div') } }); app.mount({}); return { state, projectId, stop: () => app.unmount() } }
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

test('first sentence becomes a short project title without requiring a form', () => {
  assert.equal(conversationTitle('  想弄清真实价值。这里是后续事实。'), '想弄清真实价值')
  assert.equal(conversationTitle('x'.repeat(70)).length, 36)
})

test('model thinking remains a live collapsible section while the answer streams', () => {
  const live = splitAssistantMessage('<think>先核对结果来源\n正在追')
  assert.deepEqual(live, [{ kind: 'thinking', content: '先核对结果来源\n正在追', streaming: true }])
  const completed = splitAssistantMessage('<think>先核对结果来源<\\think>\n\n结论需要继续澄清输入与结果的一对多关系。')
  assert.deepEqual(completed, [
    { kind: 'thinking', content: '先核对结果来源', streaming: false },
    { kind: 'answer', content: '\n\n结论需要继续澄清输入与结果的一对多关系。', streaming: false },
  ])
})

test('legacy investigation errors get a tool-specific recovery message without changing new errors', () => {
  const legacy = turn('legacy', 'failed', { error: '项目、权限或资料已变化，或缺少可用工具模型；请刷新并核对后重新发送。', steps: [{ id: 'step', tool_name: 'read_database_sample', title: '读取历史数据样本', status: 'failed', summary: '', started_at: now, completed_at: now }] })
  const current = turn('current', 'failed', { error: '本轮使用的临时附件已被移除或已过期，请重新上传后再发送。' })
  assert.match(visibleTurnError(legacy), /读取历史数据样本时未完成/)
  assert.equal(visibleTurnError(current), current.error)
})

test('an empty conversation history does not read status from an absent turn', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const { state, stop } = mount()
  await flush()
  assert.equal(state.error.value, '')
  stop()
})

test('a newly created project can enqueue its first turn before route authorization catches up', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const requests = []
  fakeApi.send = async (project) => { requests.push(project); return turn('first', 'running') }
  const projectId = ref('')
  let state
  const app = renderer.createApp({ setup() { state = useDistillationConversation(projectId); return () => h('div') } })
  app.mount({})
  state.input.value = 'First question in a new conversation'
  await flush()
  assert.equal(await state.send(state.input.value, 1, [], {}, 'created-project'), true)
  assert.deepEqual(requests, ['created-project'])
  assert.equal(state.turns.value[0].id, 'first')
  assert.equal(state.input.value, '')
  app.unmount()
})
test('multiple clarification answers preserve other selections and free-form evidence', () => {
  let input = '补充现场观察：结果未得到请求人确认。'
  const first = '由谁确认？\n请求人'
  const second = '优先处理什么？\n结果闭环'
  input = composeClarificationAnswer(input, first)
  input = composeClarificationAnswer(input, second)
  input = composeClarificationAnswer(input, '由谁确认？\n请求人与负责人共同确认', first)
  assert.equal(input, '补充现场观察：结果未得到请求人确认。\n\n由谁确认？\n请求人与负责人共同确认\n\n优先处理什么？\n结果闭环')
})
test('selecting an option preserves a previous answer manually edited by the user', () => {
  const input = '由谁确认？\n请求人确认，负责人复核。'
  assert.equal(composeClarificationAnswer(input, '由谁确认？\n联合确认', '由谁确认？\n请求人'), `${input}\n\n由谁确认？\n联合确认`)
})
test('late history never removes an acknowledged proposal adoption', () => {
  const adopted = turn('1', 'succeeded', { applied_revision: 3 })
  assert.equal(mergeTurns([adopted], [turn('1', 'succeeded')])[0].applied_revision, 3)
  assert.equal(mergeTurns([turn('1', 'cancelled', { updated_at: '2026-09-18T02:01:00Z' })], [turn('1', 'running')])[0].status, 'cancelled')
})
test('switching project aborts stale history and preserves the unsent message per project', async () => {
  const old = deferred(); let signal
  fakeApi.list = async (id, before, control) => id === 'old' ? (signal = control, old.promise) : { turns: [turn('new', 'waiting', { project_id: id })], has_more: false }
  const { state, projectId, stop } = mount('old')
  state.input.value = 'Unsent evidence'
  projectId.value = 'new'
  await flush()
  assert.equal(signal.aborted, true)
  old.resolve({ turns: [turn('stale')], has_more: false })
  await flush()
  assert.deepEqual(state.turns.value.map(row => row.id), ['new'])
  projectId.value = 'old'
  await flush()
  assert.equal(state.input.value, 'Unsent evidence')
  stop()
})
test('uncertain send retries with the same durable request ID and retains input until acknowledged', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const ids = []; let attempts = 0
  fakeApi.send = async (project, id) => { ids.push(id); if (++attempts === 1) throw new Error('Connection interrupted'); return turn('ack') }
  const { state, stop } = mount()
  await flush()
  state.input.value = 'Current question'
  assert.equal(await state.send(state.input.value, 1), false)
  assert.equal(state.input.value, 'Current question')
  assert.equal(await state.send(state.input.value, 1), true)
  assert.equal(ids[0], ids[1])
  assert.equal(state.input.value, '')
  assert.equal(state.turns.value[0].id, 'ack')
  stop()
})
test('late send acknowledgement cannot overwrite another project or clear its draft', async () => {
  const pending = deferred(); let signal
  fakeApi.send = async (project, id, message, revision, control) => (signal = control, pending.promise)
  const { state, projectId, stop } = mount()
  await flush()
  state.input.value = 'old input'
  const sending = state.send(state.input.value, 1)
  projectId.value = 'other'
  await flush()
  state.input.value = 'new input'
  pending.resolve(turn('late'))
  await sending
  assert.equal(signal.aborted, true)
  assert.deepEqual(state.turns.value, [])
  assert.equal(state.input.value, 'new input')
  stop()
})
test('refresh recovers active turn and cancellation waits for the server result', async () => {
  fakeApi.list = async () => ({ turns: [turn('active', 'running')], has_more: false })
  const pending = deferred()
  fakeApi.cancel = async () => pending.promise
  const { state, stop } = mount()
  await flush()
  assert.equal(state.active.value.id, 'active')
  const cancelling = state.cancel()
  assert.equal(state.active.value.status, 'running')
  pending.resolve(turn('active', 'cancelled', { updated_at: '2026-09-18T02:01:00Z' }))
  await cancelling
  assert.equal(state.active.value, undefined)
  assert.equal(state.turns.value[0].status, 'cancelled')
  stop()
})
test('clarification waits without starting another turn or adopting AI content automatically', async () => {
  fakeApi.list = async () => ({ turns: [turn('waiting', 'waiting', { proposal: { pain: 'Unverified hypothesis' }, questions: [{ id: 'q', question: 'Who benefits?' }] })], has_more: false })
  let sends = 0, applies = 0
  fakeApi.send = async () => { sends++; return turn('next') }
  fakeApi.apply = async () => { applies++; return { id: 'p', revision: 2 } }
  const { state, stop } = mount()
  await flush()
  assert.equal(state.active.value, undefined)
  assert.equal(sends, 0)
  assert.equal(applies, 0)
  await state.apply(state.turns.value[0], 1)
  assert.equal(applies, 1)
  assert.equal(state.turns.value[0].applied_revision, 2)
  stop()
})
test('proposal CAS conflict preserves the conversation and does not claim adoption', async () => {
  fakeApi.list = async () => ({ turns: [turn('proposal', 'succeeded', { proposal: { pain: 'Hypothesis' } })], has_more: false })
  fakeApi.apply = async () => { throw Object.assign(new Error('版本已变化，请重新核对'), { status: 409 }) }
  const { state, stop } = mount()
  await flush()
  const result = await state.apply(state.turns.value[0], 1)
  assert.equal(result, null)
  assert.equal(state.turns.value[0].applied_revision, null)
  assert.equal(state.turns.value[0].proposal.pain, 'Hypothesis')
  assert.match(state.error.value, /重新核对/)
  stop()
})

test('attachment identity participates in retry identity and only accepted sends clear input', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const requests = []
  fakeApi.send = async (project, requestId, message, revision, signal, attachments) => { requests.push({ requestId, attachments }); throw new Error('Connection interrupted') }
  const { state, stop } = mount()
  await flush()
  state.input.value = 'Please read the attachment'
  await state.send(state.input.value, 1, ['first'])
  await state.send(state.input.value, 1, ['first'])
  await state.send(state.input.value, 1, ['second'])
  assert.equal(requests[0].requestId, requests[1].requestId)
  assert.notEqual(requests[0].requestId, requests[2].requestId)
  assert.deepEqual(requests[2].attachments, ['second'])
  assert.equal(state.input.value, 'Please read the attachment')
  stop()
})

test('an attachment-only turn is submitted and clears the draft after acknowledgement', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const requests = []
  fakeApi.send = async (project, requestId, message, revision, signal, attachments) => {
    requests.push({ project, message, attachments })
    return turn('attachment-turn', 'running', { message, project_id: project })
  }
  const { state, stop } = mount()
  await flush()
  assert.equal(await state.send('', 1, ['attachment-1']), true)
  assert.deepEqual(requests[0], { project: 'p', message: '', attachments: ['attachment-1'] })
  assert.equal(state.input.value, '')
  stop()
})

test('resource selection participates in retry identity and reaches the API unchanged in meaning', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const requests = []
  fakeApi.send = async (project, requestId, message, revision, signal, attachments, resources) => {
    requests.push({ requestId, resources })
    throw new Error('Connection interrupted')
  }
  const { state, stop } = mount()
  await flush()
  state.input.value = 'Use the configured investigation resources'
  await state.send(state.input.value, 1, [], { llm_config_id: 'model-a', skill_ids: ['skill-b', 'skill-a'], mcp_ids: ['mcp-a'] })
  await state.send(state.input.value, 1, [], { llm_config_id: 'model-a', skill_ids: ['skill-a', 'skill-b'], mcp_ids: ['mcp-a'] })
  await state.send(state.input.value, 1, [], { llm_config_id: 'model-b', skill_ids: ['skill-a', 'skill-b'], mcp_ids: ['mcp-a'] })
  assert.equal(requests[0].requestId, requests[1].requestId)
  assert.notEqual(requests[0].requestId, requests[2].requestId)
  assert.deepEqual(requests[0].resources, { llm_config_id: 'model-a', skill_ids: ['skill-a', 'skill-b'], mcp_ids: ['mcp-a'], investigation_tool_keys: null })
  stop()
})

test('unsent first messages stay separate when switching scenario before a project exists', async () => {
  const projectId = ref(''), draftKey = ref('new:a'); let state
  const app = renderer.createApp({ setup() { state = useDistillationConversation(projectId, draftKey); return () => h('div') } })
  app.mount({})
  state.input.value = 'Question about A'
  draftKey.value = 'new:b'
  await flush()
  assert.equal(state.input.value, '')
  state.input.value = 'Question about B'
  draftKey.value = 'new:a'
  await flush()
  assert.equal(state.input.value, 'Question about A')
  app.unmount()
})


test('explicit empty investigation tools differs from defaults and preserves retry identity', async () => {
  fakeApi.list = async () => ({ turns: [], has_more: false })
  const requests = []
  fakeApi.send = async (project, requestId, message, revision, signal, attachments, resources) => {
    requests.push({ requestId, resources })
    throw new Error('Connection interrupted')
  }
  const { state, stop } = mount()
  await flush()
  state.input.value = 'Clarify before reading more'
  await state.send(state.input.value, 1, [], {})
  await state.send(state.input.value, 1, [], { investigation_tool_keys: [] })
  await state.send(state.input.value, 1, [], { investigation_tool_keys: [] })
  await state.send(state.input.value, 1, [], { investigation_tool_keys: ['read_evidence', 'list_evidence'] })
  await state.send(state.input.value, 1, [], { investigation_tool_keys: ['list_evidence', 'read_evidence'] })
  assert.equal(requests[0].resources.investigation_tool_keys, null)
  assert.deepEqual(requests[1].resources.investigation_tool_keys, [])
  assert.notEqual(requests[0].requestId, requests[1].requestId)
  assert.equal(requests[1].requestId, requests[2].requestId)
  assert.equal(requests[3].requestId, requests[4].requestId)
  assert.equal(state.input.value, 'Clarify before reading more')
  stop()
})


test('only current unadopted server proposals appear on the artifact canvas', () => {
  const candidate = turn('candidate', 'succeeded', { proposal: { beneficiary: 'Requester' }, turn_number: 2 })
  const stale = turn('old', 'succeeded', { proposal: {}, base_revision: 0, turn_number: 5 })
  const foreign = turn('foreign', 'succeeded', { proposal: {}, project_id: 'another', turn_number: 6 })
  const adopted = turn('adopted', 'succeeded', { proposal: {}, applied_revision: 1, turn_number: 7 })
  assert.equal(latestArtifactProposal([stale, foreign, adopted, candidate], 'p', 1).id, 'candidate')
  assert.equal(latestArtifactProposal([candidate], 'p', 2), undefined)
  assert.equal(latestArtifactProposal([candidate], 'another', 1), undefined)
})

test('a proposal produced by a running turn is visible before the model round finishes', () => {
  const running = turn('running', 'running', { proposal: { beneficiary: 'Requester' } })
  assert.equal(latestArtifactProposal([running], 'p', 1).id, 'running')
})

test('a first message creates the durable project before enqueueing its turn and only then changes route', () => {
  const source = readFileSync(new URL('../src/components/distillation/DistillationWorkspace.vue', import.meta.url), 'utf8')
  const send = source.slice(source.indexOf('async function sendMessage'), source.indexOf('function acceptProjectUpdate'))
  assert.match(send, /ensureProject\(text, false\)/)
  assert.ok(send.indexOf('ensureProject(text, false)') < send.indexOf('await send(text'))
  assert.ok(send.indexOf('await send(text') < send.indexOf('await changeWorkspace'))
})

test('the live activity summary reflects only authoritative server investigation states', () => {
  const source = readFileSync(new URL('../src/components/distillation/DistillationLiveActivity.vue', import.meta.url), 'utf8')
  assert.match(source, /turn\.questions\.length/)
  assert.match(source, /turn\.steps\.filter/)
  assert.match(source, /turn\.proposal/)
  assert.doesNotMatch(source, /readiness|canPublish|自动采用|autoApply/)
  assert.match(readFileSync(new URL('../src/styles/distillation-workspace.css', import.meta.url), 'utf8'), /prefers-reduced-motion: reduce/)
})
