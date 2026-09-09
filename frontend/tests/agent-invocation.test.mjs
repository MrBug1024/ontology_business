import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { agentToolResultStatus } from '../src/utils/agentToolResult.ts'
import { applyAgentTurnEvent } from '../src/utils/agentTurnProgress.ts'

test('text deltas resume from the persisted snapshot and ignore duplicate revisions', () => {
  const current = { status: 'responding', revision: 3, result: { answer: '你好😀' } }
  const event = { revision: 4, type: 'answer_delta', data: { offset: 4, delta: '世界' } }
  const next = applyAgentTurnEvent(current, event)
  assert.equal(next.result.answer, '你好😀世界')
  assert.equal(next.status, 'responding')
  assert.equal(applyAgentTurnEvent(next, event), next)
  assert.throws(() => applyAgentTurnEvent(current, { ...event, data: { offset: 2, delta: '丢字' } }))
  const reset = applyAgentTurnEvent(next, { revision: 5, type: 'answer_reset', data: { status: 'invoking_tools' } })
  assert.equal(reset.result.answer, '')
  const final = applyAgentTurnEvent(reset, { revision: 6, type: 'succeeded', data: { result: { answer: '权威结果' } } })
  assert.equal(final.result.answer, '权威结果')
  assert.equal(final.status, 'succeeded')
})

import {
  isAttachmentInputPort,
  isSupportedInvocationFile,
  isTabularInvocationAsset,
  managedBindingKindsForPort,
  parseStructuredInputs,
  validateAgentInvocationDraft,
} from '../src/utils/agentInvocation.ts'

test('failed capability receipts remain failed after conversation history is restored', () => {
  const receipt = { status: 'failed', error: { code: 'provider_execution_failed', message: 'Execution failed' } }
  assert.equal(agentToolResultStatus(JSON.stringify(receipt)), 'error')
  assert.equal(agentToolResultStatus(receipt), 'error')
  assert.equal(agentToolResultStatus({ error: { code: 'REQUIRED_RUNTIME_INPUTS_MISSING' } }), 'error')
  assert.equal(agentToolResultStatus({ status: 'timed_out' }), 'error')
})

test('successful empty results and confirmation previews are not failures', () => {
  assert.equal(agentToolResultStatus({ status: 'succeeded', output: { records: [] }, error: null }), 'done')
  assert.equal(agentToolResultStatus({ status: 'awaiting_confirmation', error: null }), 'done')
  assert.equal(agentToolResultStatus([]), 'done')
  assert.equal(agentToolResultStatus('A completed legacy text result'), 'done')
  assert.equal(agentToolResultStatus(undefined), 'running')
})

function attachment(overrides = {}) {
  return {
    uid: 'file-1',
    file: { name: 'requirements.xlsx' },
    filename: 'requirements.xlsx',
    size: 128,
    progress: 100,
    status: 'ready',
    portKey: 'project.inputs',
    assetVersionId: 'version-1',
    ...overrides,
  }
}

function port(overrides = {}) {
  return {
    port_key: 'project.inputs',
    name: 'Project inputs',
    direction: 'input',
    media_kind: 'document',
    required: true,
    binding_policy: 'per_invocation',
    binding_kinds: ['asset_version'],
    allow_override: true,
    ...overrides,
  }
}

test('structured invocation only accepts a JSON object', () => {
  assert.deepEqual(parseStructuredInputs(''), { value: {} })
  assert.deepEqual(parseStructuredInputs('{"priority": 2}'), { value: { priority: 2 } })
  assert.match(parseStructuredInputs('[1, 2]').error, /JSON 对象/)
  assert.match(parseStructuredInputs('{broken').error, /JSON 格式/)
})

test('managed attachments become per-invocation asset-version references', () => {
  const result = validateAgentInvocationDraft({
    message: '分析这份材料',
    structuredJson: '{"language":"zh-CN"}',
    attachments: [attachment()],
    portContracts: [port()],
    capability: { kind: 'function', key: 'function-1' },
  })

  assert.deepEqual(result.payload, {
    message: '分析这份材料',
    inputs: { language: 'zh-CN' },
    managed_inputs: [{ port_key: 'project.inputs', asset_version_id: 'version-1' }],
    capability: { kind: 'function', key: 'function-1' },
  })
  assert.equal(JSON.stringify(result.payload).includes('data_source'), false)
  assert.equal(JSON.stringify(result.payload).includes('object_key'), false)
})

test('upload failures and duplicate ports block submission without dropping draft data', () => {
  const draft = {
    message: 'keep me',
    structuredJson: '{"keep":true}',
    attachments: [
      attachment(),
      attachment({ uid: 'file-2', filename: 'notes.docx', portKey: 'PROJECT.INPUTS' }),
    ],
    portContracts: [port()],
  }
  const result = validateAgentInvocationDraft(draft)

  assert.equal(result.payload, undefined)
  assert.match(result.attachmentErrors['file-2'], /不能重复/)
  assert.equal(draft.message, 'keep me')
  assert.equal(draft.structuredJson, '{"keep":true}')
})

test('managed selectors serialize all governed reference kinds without changing typed inputs', () => {
  const result = validateAgentInvocationDraft({
    message: '',
    structuredJson: '{"fragment":"ordinary typed value","limit":5}',
    attachments: [],
    capability: { kind: 'workflow', key: 'workflow-1' },
    portContracts: [
      port({ port_key: 'records.version', media_kind: 'dataset', binding_kinds: ['dataset_version'] }),
      port({ port_key: 'records.head', media_kind: 'dataset', binding_kinds: ['dataset_head'] }),
      port({ port_key: 'reference.document' }),
      port({ port_key: 'warehouse.binding', media_kind: 'connector', binding_kinds: ['connector_binding'] }),
    ],
    managedInputs: [
      { portKey: 'records.version', bindingKind: 'dataset_version', referenceId: 'dataset-version-1' },
      { portKey: 'records.head', bindingKind: 'dataset_head', referenceId: 'dataset-head-1' },
      { portKey: 'reference.document', bindingKind: 'asset_version', referenceId: 'asset-version-1' },
      { portKey: 'warehouse.binding', bindingKind: 'connector_binding', referenceId: 'warehouse.current' },
    ],
  })

  assert.deepEqual(result.payload.inputs, {
    fragment: 'ordinary typed value',
    limit: 5,
  })
  assert.deepEqual(result.payload.managed_inputs, [
    { port_key: 'records.version', dataset_version_id: 'dataset-version-1' },
    { port_key: 'records.head', dataset_head_id: 'dataset-head-1' },
    { port_key: 'reference.document', asset_version_id: 'asset-version-1' },
    { port_key: 'warehouse.binding', binding_key: 'warehouse.current' },
  ])
})

test('port media and explicit binding kinds constrain attachment and selector choices', () => {
  assert.deepEqual(managedBindingKindsForPort(port({
    media_kind: 'dataset', binding_kinds: undefined,
  })), ['dataset_head', 'dataset_version'])
  assert.equal(isAttachmentInputPort(port()), true)
  assert.equal(isAttachmentInputPort(port({ media_kind: 'dataset' })), false)

  const unsupported = validateAgentInvocationDraft({
    message: 'analyze',
    structuredJson: '',
    attachments: [attachment({ portKey: 'records' })],
    portContracts: [port({
      port_key: 'records', media_kind: 'dataset', binding_kinds: ['dataset_version'],
    })],
  })
  assert.match(unsupported.attachmentErrors['file-1'], /文档或制品端口/)
})

test('duplicate managed selector and uploaded attachment are rejected across input modes', () => {
  const result = validateAgentInvocationDraft({
    message: 'analyze',
    structuredJson: '',
    attachments: [attachment()],
    portContracts: [port()],
    managedInputs: [{
      portKey: 'project.inputs', bindingKind: 'asset_version', referenceId: 'existing-version',
    }],
  })
  assert.equal(result.payload, undefined)
  assert.match(result.attachmentErrors['file-1'], /不能重复/)
})

test('supported input files cover tabular and document formats', () => {
  for (const name of [
    'rows.csv', 'rows.tsv', 'rows.xlsx', 'rows.xlsm',
    'brief.docx', 'slides.pptx', 'manual.pdf',
    'notes.markdown', 'payload.yaml', 'events.log', 'scan.tiff',
  ]) {
    assert.equal(isSupportedInvocationFile(name), true, name)
  }
  assert.equal(isSupportedInvocationFile('legacy.doc'), false)
  assert.equal(isSupportedInvocationFile('script.exe'), false)
})

test('server profile category is authoritative for formal input binding', () => {
  assert.equal(isTabularInvocationAsset('table', 'opaque.bin'), true)
  assert.equal(isTabularInvocationAsset('document', 'misleading.xlsx'), false)
  assert.equal(isTabularInvocationAsset(undefined, 'legacy.xlsx'), false)
  assert.equal(isTabularInvocationAsset(undefined, 'legacy.pdf'), false)
})

test('Agent chat clears the composer only after durable turn acceptance', () => {
  const viewSource = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const turnSource = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')
  const sendBlock = turnSource.slice(turnSource.indexOf('async function send('), turnSource.indexOf('function canRetryTurn('))
  const acceptedAt = sendBlock.indexOf('await api.createAgentTurn')
  const clearedAt = sendBlock.indexOf('options.clearComposerAfterAccepted()')

  assert.notEqual(acceptedAt, -1)
  assert.ok(clearedAt > acceptedAt)
  assert.match(sendBlock, /pendingComposerRequests\.value\.add\(pendingRequest\)/)
  assert.match(sendBlock, /const placeholdersVisible = [\s\S]*if \(placeholdersVisible\) \{[\s\S]*clearComposerAfterAccepted\(\)/)
  assert.match(viewSource, /clearComposerAfterAccepted: \(\) => composerRef\.value\?\.clearAfterAccepted\(\)/)
  assert.match(viewSource, /:disabled="conversationNavigationLocked"/)
  assert.match(sendBlock, /catch[\s\S]*草稿已保留/)
  assert.doesNotMatch(`${viewSource}\n${turnSource}`, /clearAfterSuccess|streamChat\(/)
})

test('Agent chat cancellation uses authoritative revision while navigation only detaches', () => {
  const viewSource = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const turnSource = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')
  const resetStart = turnSource.indexOf('function resetTurnScope()')
  const stopStart = turnSource.indexOf('async function stopTurn(')
  const globalStopStart = turnSource.indexOf('async function stop()', stopStart)
  const routeWatchStart = viewSource.indexOf('watch(() => route.params.id')
  const unmountStart = viewSource.indexOf('onBeforeUnmount(', routeWatchStart)
  const resetSource = turnSource.slice(resetStart, turnSource.indexOf('async function send(', resetStart))
  const stopSource = turnSource.slice(stopStart, globalStopStart)
  const routeWatchSource = viewSource.slice(routeWatchStart, unmountStart)

  assert.notEqual(resetStart, -1)
  assert.match(resetSource, /for \(const controller of turnControllers\.values\(\)\) controller\.abort\(\)/)
  assert.doesNotMatch(resetSource, /cancelAgentTurn/)
  assert.match(stopSource, /api\.cancelAgentTurn\(run\.id, run\.revision\)/)
  assert.match(stopSource, /api\.getAgentTurn\(run\.id\)/)
  assert.match(stopSource, /if \(!isTurnScopeActive\(scope, currentAgentId\)\) return/)
  assert.match(routeWatchSource, /resetTurnScope\(\)/)
  assert.doesNotMatch(routeWatchSource, /cancelAgentTurn/)
})

test('conversation history ignores stale responses after the selection changes', () => {
  const source = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const turnSource = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')
  const openStart = source.indexOf('async function openConv(')
  const deleteStart = source.indexOf('async function delConv(', openStart)
  const openSource = source.slice(openStart, deleteStart)

  assert.match(source, /let conversationLoadRequest = 0/)
  assert.match(openSource, /const request = \+\+conversationLoadRequest/)
  assert.match(openSource, /const requestedConversationId = c\.id/)
  assert.match(openSource, /request !== conversationLoadRequest/)
  assert.match(openSource, /curConv\.value\?\.id !== requestedConversationId/)
  assert.match(openSource, /messages\.value = loadedMessages\.map/)
  assert.doesNotMatch(openSource, /messages\.value\.push/)
  assert.ok(openSource.indexOf('messages.value = loadedMessages.map') < openSource.indexOf('api.listAgentTurns'))
  assert.match(openSource, /finally[\s\S]*conversationLoading\.value = false/)
  assert.match(source, /:disabled="!agentValidationReady \|\| conversationLoading \|\| currentTurnPending"/)
  assert.match(turnSource, /if \(options\.conversationLoading\.value \|\| streaming\.value\) return/)
})

test('invocation composer submits managed file references without pre-send materialization', () => {
  const source = readFileSync(new URL('../src/components/AgentInvocationComposer.vue', import.meta.url), 'utf8')
  assert.match(source, /api\.createManagedUploadRun/)
  assert.match(source, /api\.uploadManagedRunContent/)
  assert.match(source, /purpose: item\.persistent \? 'validation_asset' : 'invocation_attachment'/)
  assert.match(source, /api\.listCatalogAssets/)
  assert.match(source, /attachments: submittableAttachments\.value\.map/)
  assert.match(source, /asset_version_id: item\.assetVersionId/)
  assert.match(source, /upload_run_id: String\(item\.uploadRunId\)/)
  assert.match(source, /item\.status === 'registering' \|\| item\.status === 'error'/)
  assert.match(source, /defineExpose\(\{ clearAfterAccepted, submitMessage \}\)/)
  assert.doesNotMatch(source, /api\.uploadCatalogAttachment/)
  assert.doesNotMatch(source, /buildValidationDataset|waitForValidationDatasetJob/)
  assert.doesNotMatch(source, /sessionStorage|PendingValidationPreparation/)
  assert.doesNotMatch(source, /isTabularInvocationAsset/)
  assert.doesNotMatch(source, /FileReader|\.arrayBuffer\(/)
  assert.match(source, /api\.deleteCatalogAsset/)
})

test('durable turn recovery restores every active stream and terminal retry state', () => {
  const apiSource = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')
  const viewSource = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const turnSource = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')

  assert.doesNotMatch(apiSource, /export function streamChat|\/agents\/\$\{agentId\}\/chat/)
  assert.match(apiSource, /headers\['Last-Event-ID'\] = String\(revision\)/)
  assert.match(apiSource, /nextRevision <= revision/)
  assert.match(apiSource, /onConnectionState\?\.\('reconnecting'\)/)
  assert.match(apiSource, /eventName === 'error'/)
  assert.match(viewSource, /activeOnly: true, limit: 100/)
  assert.match(viewSource, /recoverActiveTurns\(activeRuns\)/)
  assert.match(viewSource, /conversationId: requestedConversationId,[\s\S]*limit: 100/)
  assert.match(viewSource, /recoverConversationTurns\(\[\.\.\.recentConversationRuns, \.\.\.activeConversationRuns\]\)/)
  assert.match(turnSource, /for \(const run of latestRuns\)[\s\S]*applyTurnRun\(run, assistant\)/)
  assert.match(turnSource, /const turnControllers = new Map<string, AbortController>\(\)/)
  assert.match(turnSource, /turnControllers\.set\(run\.id, streamAgentTurn\(/)
  assert.match(turnSource, /function isTurnSubscriptionCurrent\(/)
  assert.match(turnSource, /for \(const controller of turnControllers\.values\(\)\) controller\.abort\(\)/)
  assert.match(viewSource, /canRetryTurn\(m\)[\s\S]*retryTurn\(m\)/)
})

test('turn recovery and mutations reject late responses from an obsolete view scope', () => {
  const viewSource = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const turnSource = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')
  const loadStart = viewSource.indexOf('async function loadConvs(')
  const newConversationStart = viewSource.indexOf('async function newConv()', loadStart)
  const retryStart = turnSource.indexOf('async function retryTurn(')
  const stopStart = turnSource.indexOf('async function stopTurn(', retryStart)
  const globalStopStart = turnSource.indexOf('async function stop()', stopStart)
  const loadSource = viewSource.slice(loadStart, newConversationStart)
  const retrySource = turnSource.slice(retryStart, stopStart)
  const stopSource = turnSource.slice(stopStart, globalStopStart)

  assert.match(viewSource, /runtimeCapabilities\.value = loadedCapabilities[\s\S]*loadConvs\(true\)/)
  assert.match(loadSource, /const selectionRequest = conversationLoadRequest/)
  assert.match(loadSource, /const listRequest = \+\+conversationListRequest/)
  assert.match(loadSource, /if \(listRequest === conversationListRequest\) conversations\.value = loaded/)
  assert.match(loadSource, /selectionRequest !== conversationLoadRequest/)
  assert.match(loadSource, /\(curConv\.value\?\.id \|\| ''\) !== selectedConversationId/)
  assert.match(retrySource, /await api\.retryAgentTurn\(turnId, turnRevision, retryIdempotencyKey\(message\)\)[\s\S]*if \(!isTurnScopeActive\(scope, currentAgentId\)\) return/)
  assert.match(stopSource, /await api\.cancelAgentTurn\(run\.id, run\.revision\)[\s\S]*if \(!isTurnScopeActive\(scope, currentAgentId\)\) return/)
  assert.match(stopSource, /await api\.getAgentTurn\(run\.id\)[\s\S]*if \(!isTurnScopeActive\(scope, currentAgentId\)\) return/)
  assert.match(turnSource, /run\.conversation_id === conversationId/)
  assert.match(turnSource, /scope === turnScopeVersion/)
})

test('terminal reconciliation is isolated from optional history refresh', () => {
  const source = readFileSync(new URL('../src/composables/useAgentDurableTurns.ts', import.meta.url), 'utf8')
  const refreshStart = source.indexOf('async function refreshTurnHistory(')
  const finishStart = source.indexOf('async function finishTurn(', refreshStart)
  const bindStart = source.indexOf('function bindTurn(', finishStart)
  const refreshSource = source.slice(refreshStart, finishStart)
  const finishSource = source.slice(finishStart, bindStart)
  const bindSource = source.slice(bindStart, source.indexOf('function recoverActiveTurns(', bindStart))

  assert.match(finishSource, /run = await api\.getAgentTurn\(runId\)/)
  assert.match(finishSource, /if \(assistant\) applyTurnRun\(run, assistant\)[\s\S]*activeTurns\.value\.delete\(runId\)/)
  assert.match(finishSource, /try \{[\s\S]*await refreshTurnHistory\(run, scope\)[\s\S]*\} catch \{[\s\S]*任务状态已更新，但会话记录刷新失败/)
  assert.match(refreshSource, /await options\.refreshConversations\(\)[\s\S]*await options\.openConversation\(conversation, false\)/)
  assert.match(bindSource, /state === 'reconnecting'[\s\S]*TURN_STATUS_LABELS\[latestRun\.status\]/)
  assert.match(source, /message\.turnStatus === 'indeterminate'[\s\S]*已核对，继续重试/)
  assert.match(source, /if \(message\.retryIdempotencyKey\) return message\.retryIdempotencyKey/)
})
