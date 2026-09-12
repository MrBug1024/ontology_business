import assert from 'node:assert/strict'
import test from 'node:test'
import { capabilityInvocationId, receiptResourceId } from '../src/utils/agentCapabilityReceipt.ts'
import { workflowArtifacts } from '../src/utils/workflowArtifacts.ts'
import { managedFileDownloadUrl, managedFileTextPath } from '../src/utils/managedFileUrls.ts'
import { plainMessage } from '../src/utils/plainMessage.ts'
import { readFileSync } from 'node:fs'
import { executionAnalysis, executionSteps, executionStatusLabel } from '../src/utils/agentExecutionTrace.ts'

test('message channels render structured markdown as ordinary text without rich controls', () => {
  assert.equal(plainMessage('# 结果\n\n**通过**，`数量=2`'), '结果\n\n通过，数量=2')
  assert.equal(plainMessage('[文件](https://example.test/report)'), '文件 (https://example.test/report)')
  assert.equal(plainMessage('| 项目 | 结果 |\n| --- | --- |\n| 检查 | 通过 |'), '项目 | 结果\n检查 | 通过')
  const component = readFileSync(new URL('../src/components/agent/AgentCapabilityReceipt.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(component, /ElMessageBox|router.push|\.confirm\(/)
  assert.match(component, /receipt.delivery.text/)
  assert.match(component, /onBeforeUnmount\(stop\)/)
})

test('unified capability previews are discoverable even when the receipt was projected', () => {
  const id = 'a'.repeat(32)
  assert.equal(capabilityInvocationId({ name: 'invoke_capability', result: JSON.stringify({ invocation_id: id, status: 'awaiting_confirmation' }) }), id)
  assert.equal(capabilityInvocationId({ name: 'invoke_capability', result: { invocation_id: id, result_omitted: true } }), id)
  assert.equal(capabilityInvocationId({ name: 'execute_action', result: { invocation_id: id } }), '')
  assert.equal(capabilityInvocationId({ name: 'invoke_capability', result: 'broken' }), '')
})

test('receipt links only accept managed identifiers', () => {
  for (const value of ['https://example.test', '../secret', 'javascript:alert(1)', {}, null]) assert.equal(receiptResourceId(value), '')
  assert.equal(receiptResourceId('b'.repeat(32)), 'b'.repeat(32))
})

test('managed file links carry Agent scope while legacy modeling links stay compatible', () => {
  const fileId = 'f'.repeat(32)
  const agentId = 'agent-one'
  assert.equal(
    managedFileDownloadUrl(fileId, agentId),
    `/api/data-sources/files/${fileId}/download?agent_id=${agentId}`,
  )
  assert.equal(
    managedFileTextPath(fileId, agentId),
    `/data-sources/files/${fileId}/text?agent_id=${agentId}`,
  )
  assert.equal(managedFileDownloadUrl(fileId), `/api/data-sources/files/${fileId}/download`)
})

test('workflow files require a successful artifact receipt and ignore arbitrary URLs', () => {
  const artifact = { id: 'c'.repeat(32), filename: 'review.docx', format: 'docx', mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size: 512, sha256: 'd'.repeat(64), download_url: 'https://example.test/unsafe' }
  const step = { type: 'action', status: 'success', result: { result: { artifact } } }
  assert.equal(workflowArtifacts({ steps: [step] })[0].url, `/api/data-sources/files/${artifact.id}/download`)
  assert.deepEqual(workflowArtifacts({ steps: [{ ...step, status: 'failed' }] }), [])
  assert.deepEqual(workflowArtifacts({ steps: [{ ...step, type: 'llm' }] }), [])
})

test('execution history retains failed and waiting steps without treating returned chat as business success', () => {
  const events = [
    { revision: 3, type: 'invoking_tools', created_at: '2026-09-09T01:00:00Z', data: { tool_step: { call_id: 'a', name: 'invoke_capability', phase: 'started', status: 'running' } } },
    { revision: 4, type: 'invoking_tools', created_at: '2026-09-09T01:00:01Z', data: { tool_step: { call_id: 'a', name: 'invoke_capability', phase: 'finished', status: 'failed', error_code: 'provider_execution_failed' } } },
  ]
  const steps = executionSteps(events, [
    { id: 'a', name: 'invoke_capability', result: JSON.stringify({ status: 'failed', error: { code: 'provider_execution_failed' } }) },
    { id: 'b', name: 'invoke_capability', result: { status: 'awaiting_approval' } },
    { id: 'c', name: 'list_capabilities' },
  ], false)
  assert.equal(steps.length, 3)
  assert.equal(steps[0].status, 'failed')
  assert.equal(steps[0].started_at, events[0].created_at)
  assert.equal(steps[0].finished_at, events[1].created_at)
  assert.equal(executionStatusLabel(steps[1].status), '等待审批')
  assert.equal(executionStatusLabel(steps[2].status), '未记录结果')
  assert.equal(executionSteps([], [{ id: 'd', name: 'x', result: { ok: false } }], false)[0].status, 'failed')
  assert.equal(executionSteps([], [{ id: 'e', name: 'x', result: { status: 'indeterminate', error: { code: 'unknown_result' } } }], false)[0].status, 'indeterminate')
})

test('analysis replay keeps public preambles and excludes final answer and private reasoning', () => {
  const event = (revision, type, data = {}) => ({ revision, type, data, created_at: '2026-09-09T01:00:00Z' })
  const events = [
    event(1, 'answer_reset'), event(2, 'answer_delta', { offset: 0, delta: '先核对契约。' }),
    event(3, 'invoking_tools', { tool_step: { call_id: 'a', name: 'list_capabilities', phase: 'started', status: 'running' } }),
    event(4, 'invoking_tools', { tool_step: { call_id: 'a', name: 'list_capabilities', phase: 'finished', status: 'returned' } }),
    event(5, 'answer_reset'), event(6, 'answer_delta', { offset: 0, delta: '这是最终回答', reasoning_content: 'private' }),
  ]
  assert.deepEqual(executionAnalysis(events), [{ revision: 3, text: '先核对契约。' }])
  assert.equal(executionSteps(events.slice(0, 3), [], false)[0].status, 'unknown')
})

test('reused model tool identifiers preserve each recorded attempt', () => {
  const event = (revision, phase, status) => ({ revision, type: 'invoking_tools', created_at: `2026-09-09T01:00:0${revision}Z`, data: { tool_step: { call_id: 'same', name: 'invoke_capability', phase, status } } })
  const events = [event(1, 'started', 'running'), event(2, 'finished', 'failed'), event(3, 'started', 'running'), event(4, 'finished', 'awaiting_approval')]
  const steps = executionSteps(events, [{ id: 'same', result: { status: 'awaiting_approval' } }, { id: 'same', result: { status: 'awaiting_approval' } }], false)
  assert.equal(steps.length, 2)
  assert.equal(steps[0].status, 'failed')
  assert.equal(steps[1].status, 'awaiting_approval')
  assert.notEqual(steps[0].step_key, steps[1].step_key)
  assert.equal(steps[0].finished_at, events[1].created_at)
  assert.equal(steps[1].started_at, events[2].created_at)
})
