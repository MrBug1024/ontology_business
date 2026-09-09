import assert from 'node:assert/strict'
import test from 'node:test'
import { capabilityInvocationId, receiptResourceId } from '../src/utils/agentCapabilityReceipt.ts'
import { workflowArtifacts } from '../src/utils/workflowArtifacts.ts'
import { plainMessage } from '../src/utils/plainMessage.ts'
import { readFileSync } from 'node:fs'

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

test('workflow files require a successful artifact receipt and ignore arbitrary URLs', () => {
  const artifact = { id: 'c'.repeat(32), filename: 'review.docx', format: 'docx', mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size: 512, sha256: 'd'.repeat(64), download_url: 'https://example.test/unsafe' }
  const step = { type: 'action', status: 'success', result: { result: { artifact } } }
  assert.equal(workflowArtifacts({ steps: [step] })[0].url, `/api/data-sources/files/${artifact.id}/download`)
  assert.deepEqual(workflowArtifacts({ steps: [{ ...step, status: 'failed' }] }), [])
  assert.deepEqual(workflowArtifacts({ steps: [{ ...step, type: 'llm' }] }), [])
})
