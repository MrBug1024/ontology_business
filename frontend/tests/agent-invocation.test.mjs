import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  isAttachmentInputPort,
  isSupportedInvocationFile,
  managedBindingKindsForPort,
  parseStructuredInputs,
  validateAgentInvocationDraft,
} from '../src/utils/agentInvocation.ts'

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
  for (const name of ['rows.csv', 'rows.xlsx', 'brief.docx', 'manual.pdf', 'payload.json']) {
    assert.equal(isSupportedInvocationFile(name), true, name)
  }
  assert.equal(isSupportedInvocationFile('script.exe'), false)
})

test('Agent chat clears the composer only after a successful stream', () => {
  const source = readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  const sendBlock = source.slice(source.indexOf('function send('), source.indexOf('function handleEvent('))
  const finishBlock = source.slice(source.indexOf('async function finish('), source.indexOf('function stop('))

  assert.doesNotMatch(sendBlock, /clearAfterSuccess/)
  assert.match(finishBlock, /if \(succeeded\) composerRef\.value\?\.clearAfterSuccess\(\)/)
  assert.match(source, /activeStreamFailed = true[\s\S]*case 'error'/)
})

test('invocation composer reuses validation assets and materializes tables as datasets', () => {
  const source = readFileSync(new URL('../src/components/AgentInvocationComposer.vue', import.meta.url), 'utf8')
  assert.match(source, /api\.uploadCatalogAttachment/)
  assert.match(source, /purpose: uploadMode\.value/)
  assert.match(source, /api\.listCatalogAssets/)
  assert.match(source, /api\.buildValidationDataset/)
  assert.match(source, /api\.deleteCatalogAsset/)
  assert.match(source, /dataset_version_id: tableDataset\.dataset_version_id/)
  assert.doesNotMatch(source, /disabled \|\| busy \|\| !attachmentsEnabled/)
  assert.doesNotMatch(source, /当前授权能力没有兼容/)
  assert.doesNotMatch(source, /listLogicalDatasets|listScenarioConnectorBindings/)
  assert.doesNotMatch(source, /structured_inputs|managed_inputs|port_key/)
})
