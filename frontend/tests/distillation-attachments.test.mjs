import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, h, nextTick, ref } from 'vue'

const attachment = (id, project = 'p') => ({ id, request_id: id, project_id: project, filename: 'evidence.txt', media_type: 'text/plain', byte_size: 8, content_sha256: 'a'.repeat(64), status: 'ready', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400000).toISOString() })
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const fakeApi = { attachments: async () => [], upload: async () => attachment('a'), removeAttachment: async () => undefined }
globalThis.__attachmentTestApi = fakeApi
const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const source = ts.transpileModule(readFileSync(new URL('../src/composables/useDistillationAttachments.ts', import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const vueUrl = new URL('../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const apiUrl = encode('export const distillationConversationApi = globalThis.__attachmentTestApi')
const { useDistillationAttachments } = await import(encode(source.replace("from 'vue'", `from '${vueUrl}'`).replace("from '@/api/distillationConversation'", `from '${apiUrl}'`)))
const renderer = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
function mount() { const id = ref('p'); let state; const app = renderer.createApp({ setup() { state = useDistillationAttachments(id); return () => h('div') } }); app.mount({}); return { id, state, stop: () => app.unmount() } }
async function flush() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

test('failed upload retains its file and durable request ID for retry', async () => {
  const requests = []; let attempt = 0
  fakeApi.upload = async (project, requestId, file) => { requests.push({ requestId, file }); if (++attempt === 1) throw new Error('Disconnected'); return attachment('ready') }
  const { state, stop } = mount(); await flush()
  const file = new File(['evidence'], 'evidence.txt', { type: 'text/plain' })
  await state.add([file])
  assert.equal(state.attachments.value[0].status, 'failed')
  assert.equal(state.blocked.value, true)
  await state.retry(state.attachments.value[0].key)
  assert.equal(requests[0].requestId, requests[1].requestId)
  assert.equal(requests[1].file, file)
  assert.deepEqual(state.readyIds.value, ['ready'])
  state.sent(['ready'])
  assert.equal(state.attachments.value[0].status, 'bound')
  assert.deepEqual(state.composerAttachments.value, [])
  assert.deepEqual(state.readyIds.value, ['ready'])
  stop()
})
test('ready attachment remains visible until server confirms removal', async () => {
  fakeApi.attachments = async () => [attachment('pending')]
  const deletion = deferred(); fakeApi.removeAttachment = async () => deletion.promise
  const { state, stop } = mount(); await flush()
  const removing = state.remove('pending')
  assert.equal(state.attachments.value[0].status, 'removing')
  deletion.resolve(); await removing
  assert.deepEqual(state.attachments.value, [])
  stop()
})
test('bound attachments are restored for collaborators and remain selectable', async () => {
  fakeApi.attachments = async () => [{ ...attachment('bound'), status: 'bound' }]
  const { state, stop } = mount(); await flush()
  assert.equal(state.attachments.value[0].status, 'bound')
  assert.deepEqual(state.composerAttachments.value, [])
  assert.deepEqual(state.readyIds.value, ['bound'])
  assert.equal(state.blocked.value, false)
  stop()
})
test('removing a submitted attachment also clears its composer projection', async () => {
  fakeApi.attachments = async () => [{ ...attachment('submitted'), status: 'bound' }]
  fakeApi.removeAttachment = async () => undefined
  const { state, stop } = mount(); await flush()
  assert.deepEqual(state.readyIds.value, ['submitted'])
  assert.equal(await state.removeSubmitted('submitted'), true)
  assert.deepEqual(state.attachments.value, [])
  assert.deepEqual(state.readyIds.value, [])
  stop()
})
test('switching project aborts upload and never uploads remaining selected files into the new project', async () => {
  fakeApi.attachments = async () => []
  const pending = deferred(); let calls = 0, signal
  fakeApi.upload = async (project, requestId, file, control) => { calls++; signal = control; return pending.promise }
  const { id, state, stop } = mount(); await flush()
  const uploading = state.add([new File(['a'], 'a.txt'), new File(['b'], 'b.txt')])
  id.value = 'other'; await flush()
  pending.resolve(attachment('old')); await uploading
  assert.equal(signal.aborted, true)
  assert.equal(calls, 1)
  assert.deepEqual(state.attachments.value, [])
  stop()
})
test('expired attachment cannot be selected for a new turn and oversized files are rejected before upload', async () => {
  fakeApi.attachments = async () => [{ ...attachment('expired'), expires_at: '2020-01-01T00:00:00Z' }]
  let calls = 0; fakeApi.upload = async () => { calls++; return attachment('invalid') }
  const { state, stop } = mount(); await flush()
  assert.deepEqual(state.readyIds.value, [])
  assert.equal(state.blocked.value, true)
  await state.add([new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'large.txt')])
  assert.equal(calls, 0)
  assert.match(state.attachments.value[1].error, /10 MB/)
  stop()
})
test('a late pending list does not duplicate an attachment already acknowledged as sent', async () => {
  fakeApi.attachments = async () => [attachment('sent')]
  const { state, stop } = mount(); await flush()
  const pending = deferred(); fakeApi.attachments = async () => pending.promise
  const reloading = state.load()
  state.sent(['sent'])
  pending.resolve([attachment('sent')]); await reloading
  assert.equal(state.attachments.value.length, 1)
  assert.equal(state.attachments.value[0].status, 'bound')
  stop()
})
test('restoring after a lost upload response merges by server request identity without duplicate file cards', async () => {
  fakeApi.attachments = async () => []
  let request
  fakeApi.upload = async (project, requestId) => { request = requestId; throw new Error('Response lost') }
  const { state, stop } = mount(); await flush()
  await state.add([new File(['evidence'], 'evidence.txt')])
  fakeApi.attachments = async () => [{ ...attachment('durable'), request_id: request }]
  await state.load()
  assert.equal(state.attachments.value.length, 1)
  assert.equal(state.attachments.value[0].status, 'ready')
  assert.deepEqual(state.readyIds.value, ['durable'])
  stop()
})
test('removing an upload with unknown outcome resolves its durable identity before deleting it', async () => {
  fakeApi.attachments = async () => []
  const requests = []; let deleted
  fakeApi.upload = async (project, requestId) => { requests.push(requestId); if (requests.length === 1) throw new Error('Response lost'); return { ...attachment('durable'), request_id: requestId } }
  fakeApi.removeAttachment = async (project, id) => { deleted = id }
  const { state, stop } = mount(); await flush()
  await state.add([new File(['evidence'], 'evidence.txt')])
  await state.remove(state.attachments.value[0].key)
  assert.equal(requests[0], requests[1])
  assert.equal(deleted, 'durable')
  assert.deepEqual(state.attachments.value, [])
  stop()
})

test('pre-project upload works before project creation and survives project binding', async () => {
  fakeApi.attachments = async () => []
  fakeApi.upload = async () => attachment('preproject', null)
  const { id, state, stop } = mount(); id.value = ''; await flush()
  await state.add([new File(['evidence'], 'evidence.txt')])
  assert.deepEqual(state.readyIds.value, ['preproject'])
  id.value = 'created-project'; await flush()
  assert.deepEqual(state.readyIds.value, ['preproject'])
  state.sent(['preproject'])
  assert.equal(state.attachments.value[0].status, 'bound')
  stop()
})
