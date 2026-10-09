import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
const source = fs.readFileSync(new URL('../src/utils/agentValidationTarget.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { createAgentValidationLoader, forwardedValidationRelease } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const fixedInputSource = fs.readFileSync(new URL('../src/utils/agentFixedInputs.ts', import.meta.url), 'utf8')
const fixedInputCompiled = ts.transpileModule(fixedInputSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { parseFixedBusinessInputs } = await import(`data:text/javascript;base64,${Buffer.from(fixedInputCompiled).toString('base64')}`)

test('fixed business inputs preserve missing fields and never parse ordinary chat', () => {
  assert.deepEqual(parseFixedBusinessInputs(false, '{broken'), { error: '' })
  const result = parseFixedBusinessInputs(true, '{"record":{"priority":"normal"}}')
  assert.deepEqual(result.inputs, { record: { priority: 'normal' } })
  assert.equal(result.error, '')
  assert.equal('applicant_id' in result.inputs.record, false)
})

test('fixed business inputs reject invalid structure and retain source for correction', () => {
  for (const value of ['', '[1]', 'null', '"value"', '{broken', '{"value":1e999}', '{"__proto__":{}}', JSON.stringify({value:'x'.repeat(65536)})]) {
    assert.ok(parseFixedBusinessInputs(true, value).error)
  }
  assert.deepEqual(parseFixedBusinessInputs(true, '{}').inputs, {})
})

test('catalog and readiness request the same exact release and cancellation signal', async () => {
  const calls = []
  const loader = createAgentValidationLoader({
    getAgent: async (...args) => { calls.push(args); return { id: 'agent' } },
    getAgentRuntimeCapabilities: async (...args) => { calls.push(args); return [{ key: 'released' }] },
  })
  assert.deepEqual((await loader.load('agent', 'release')).capabilities, [{ key: 'released' }])
  assert.deepEqual(calls.map(args => args.slice(0, 2)), [['agent', 'release'], ['agent', 'release']])
  assert.equal(calls[0][2], calls[1][2])
  loader.dispose()
  assert.equal(calls[0][2].aborted, true)
})

test('a late release response cannot replace the newly selected release', async () => {
  let finishOld
  let oldSignal
  const loader = createAgentValidationLoader({
    getAgent: async (id, release, signal) => {
      if (release === 'old') { oldSignal = signal; return new Promise(resolve => { finishOld = resolve }) }
      return { id, name: release }
    },
    getAgentRuntimeCapabilities: async (id, release) => [{ key: release }],
  })
  const old = loader.load('agent', 'old')
  const latest = await loader.load('agent', 'new')
  assert.equal(latest.agent.name, 'new')
  assert.equal(oldSignal.aborted, true)
  finishOld({ id: 'agent', name: 'old' })
  assert.equal(await old, null)
  loader.dispose()
})

test('failed released catalog fails closed and a retry uses the same release', async () => {
  const releases = []
  let failed = true
  const loader = createAgentValidationLoader({
    getAgent: async () => ({ id: 'agent', readiness: { validation: { ready: true } } }),
    getAgentRuntimeCapabilities: async (id, release) => {
      releases.push(release)
      if (failed) throw new Error('发布已停用')
      return [{ key: 'released' }]
    },
  })
  assert.deepEqual(await loader.load('agent', 'release'), { agent: null, capabilities: [], error: '发布已停用' })
  failed = false
  assert.equal((await loader.load('agent', 'release')).agent.id, 'agent')
  assert.deepEqual(releases, ['release', 'release'])
  loader.dispose()
})

test('view submits the selected release through the existing durable turn', () => {
  const view = fs.readFileSync(new URL('../src/views/AgentChat.vue', import.meta.url), 'utf8')
  assert.match(view, /sendTurn\(\{ \.\.\.payload, release_id: selectedReleaseId\.value \|\| undefined \}\)/)
  assert.match(view, /watch\(selectedReleaseId,[\s\S]*resetTurnScope\(\)/)
  assert.match(view, /!targetLoading\.value && !targetError\.value/)
})

test('development validation preserves a fixed release only for its selected scenario', () => {
  const id = 'a'.repeat(32)
  assert.equal(forwardedValidationRelease(id, 'scene', 'scene'), id)
  for (const [value, target, source] of [[id, 'other', 'scene'], [id, '', ''], [['a'], 'scene', 'scene'], ['invalid', 'scene', 'scene']]) {
    assert.equal(forwardedValidationRelease(value, target, source), undefined)
  }
  const list = fs.readFileSync(new URL('../src/components/ScenarioReleaseList.vue', import.meta.url), 'utf8')
  const agents = fs.readFileSync(new URL('../src/views/Agents.vue', import.meta.url), 'utf8')
  assert.match(list, /scenario_id: row.scenario_id, release_id: row.id/)
  assert.match(agents, /release_id: forwardedValidationRelease\(route.query.release_id/)
})
