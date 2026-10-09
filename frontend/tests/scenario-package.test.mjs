import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
const source = fs.readFileSync(new URL('../src/utils/scenarioPackage.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { packageCases } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const capabilities = [{ kind: 'workflow', key: 'review' }]
const evidence = ['success', 'boundary', 'failure'].map(role => ({ invocation_id: role, kind: 'workflow', key: 'review', status: 'succeeded', workflow_status: 'succeeded', eligible: true }))
const choices = Object.fromEntries(evidence.map(item => [`workflow:review:${item.invocation_id}`, item.invocation_id]))
test('plugin accepts three distinct completed cases and preserves server statuses', () => {
  assert.equal(packageCases(capabilities, evidence, choices).length, 3)
  assert.equal(packageCases(capabilities, evidence, choices)[0].expected_status, 'succeeded')
})
test('plugin rejects reused, pending, foreign or failed happy-path receipts', () => {
  assert.throws(() => packageCases(capabilities, evidence, { ...choices, 'workflow:review:failure': 'success' }), /不同案例/)
  for (const patch of [{ eligible: false }, { key: 'other' }, { workflow_status: 'queued' }, { status: 'failed' }]) {
    assert.throws(() => packageCases(capabilities, evidence.map((item, i) => i ? item : { ...item, ...patch }), choices))
  }
})
