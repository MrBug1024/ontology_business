import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { normalizeAgentReadiness } from '../src/utils/agentReadiness.ts'
import { filterAgentsByScenario, scenarioIdFromQuery } from '../src/utils/agentValidationView.ts'

test('legacy Agent without fixed data remains validation-ready', () => {
  const readiness = normalizeAgentReadiness({
    name: 'Requirements advisor',
    scenario_id: 'scenario-1',
    llm_config_id: 'llm-1',
    data_source_ids: [],
  })

  assert.equal(readiness.source, 'legacy')
  assert.equal(readiness.definition.ready, true)
  assert.equal(readiness.validation.ready, true)
  assert.deepEqual(readiness.validation.missing, [])
  assert.equal(readiness.release.ready, false)
  assert.equal(readiness.runtime.ready, false)
  assert.doesNotMatch(
    readiness.validation.missing.map((issue) => issue.label).join(','),
    /数据|映射/,
  )
})

test('legacy validation fallback only requires scenario and model', () => {
  const readiness = normalizeAgentReadiness({ name: 'Incomplete', data_source_ids: [] })

  assert.equal(readiness.validation.ready, false)
  assert.deepEqual(
    readiness.validation.missing.map((issue) => issue.code),
    ['scenario_required', 'llm_required'],
  )
  assert.deepEqual(
    readiness.validation.missing.map((issue) => issue.label),
    ['业务场景', '大模型'],
  )
})

test('canonical server readiness is authoritative over legacy fields', () => {
  const readiness = normalizeAgentReadiness({
    name: 'Server governed',
    data_source_ids: [],
    readiness: {
      definition: { ready: true, missing: [] },
      validation: { ready: true, missing: [] },
      release: {
        ready: false,
        missing: [{ code: 'approval_required', label: '等待发布审批', target: '/releases', blocking: true }],
      },
      runtime: { ready: true, missing: [] },
    },
  })

  assert.equal(readiness.source, 'server')
  assert.equal(readiness.validation.ready, true)
  assert.equal(readiness.release.ready, false)
  assert.deepEqual(readiness.release.missing, [
    { code: 'approval_required', label: '等待发布审批', target: '/releases', blocking: true },
  ])
  assert.equal(readiness.runtime.ready, true)
})

test('flat four-axis server response and string issues are normalized', () => {
  const readiness = normalizeAgentReadiness({
    name: 'Flat response',
    scenario_id: 'legacy-scenario',
    llm_config_id: 'legacy-model',
    data_source_ids: [],
    readiness: {
      definition_valid: true,
      validation_ready: false,
      release_ready: false,
      runtime_ready: true,
      missing: {
        validation: ['验证样例尚未通过'],
        release: [{ code: 'release_missing', label: '尚未发布' }],
      },
    },
  })

  assert.equal(readiness.source, 'server')
  assert.equal(readiness.definition.ready, true)
  assert.equal(readiness.validation.ready, false)
  assert.equal(readiness.validation.missing[0]?.label, '验证样例尚未通过')
  assert.equal(readiness.release.missing[0]?.code, 'release_missing')
  assert.equal(readiness.runtime.ready, true)
})

test('top-level flat readiness remains compatible during rollout', () => {
  const readiness = normalizeAgentReadiness({
    name: 'Early response',
    data_source_ids: [],
    definition_valid: true,
    validation_ready: true,
    release_ready: true,
    runtime_ready: false,
  })

  assert.equal(readiness.source, 'server')
  assert.equal(readiness.definition.ready, true)
  assert.equal(readiness.validation.ready, true)
  assert.equal(readiness.release.ready, true)
  assert.equal(readiness.runtime.ready, false)
})

test('new validation Agents use capability mode without fixed data authoring', () => {
  const source = readFileSync(new URL('../src/views/Agents.vue', import.meta.url), 'utf8')
  const createStart = source.indexOf('function openCreate()')
  const editStart = source.indexOf('function openEdit(', createStart)
  const createSource = source.slice(createStart, editStart)
  const saveStart = source.indexOf('async function save()')
  const removeStart = source.indexOf('async function remove(', saveStart)
  const saveSource = source.slice(saveStart, removeStart)

  assert.match(createSource, /runtime_binding_mode:\s*'capability_only'/)
  assert.match(createSource, /data_source_ids:\s*\[\]/)
  assert.match(saveSource, /data_source_ids:\s*\[\]/)
  assert.match(saveSource, /runtime_binding_mode:\s*'capability_only'/)
  assert.match(source, /capability_scope:\s*allAgentCapabilityScope\(\)/)
  assert.match(source, /<h3 id="runtime-connection-heading">业务数据库<\/h3>/)
  assert.doesNotMatch(source, /兼容资源|showLegacyResources/)
})

test('validation Agent capability totals only count directly invocable categories', () => {
  const source = readFileSync(new URL('../src/views/Agents.vue', import.meta.url), 'utf8')

  assert.doesNotMatch(source, /\{ key: 'events', label: '事件'/)
  assert.match(source, /capabilityCategories\.map\(\(category\) => agent\.capability_summary\?\.\[category\.key\]\)/)
  assert.match(source, /保存时会固定当前可见/)
  assert.match(source, /以后新增需再次保存授权/)
  assert.doesNotMatch(source, /以后新增的可见.*都会自动授权/)
})

test('validation Agent scenario filtering follows the current route query', () => {
  const source = readFileSync(new URL('../src/views/Agents.vue', import.meta.url), 'utf8')
  const loadStart = source.indexOf('async function load()')
  const createStart = source.indexOf('function openCreate()', loadStart)
  const loadSource = source.slice(loadStart, createStart)

  assert.match(source, /:model-value="scenarioScope"/)
  assert.doesNotMatch(source, /v-model="scenarioScope"/)
  assert.match(source, /v-for="a in visibleAgents"/)
  assert.match(source, /!loading && !visibleAgents\.length/)
  assert.match(source, /const scenarioScope = computed\(\(\) => scenarioIdFromQuery\(route\.query\.scenario_id\)\)/)
  assert.match(source, /const visibleAgents = computed\(\(\) => filterAgentsByScenario\(agents\.value, scenarioScope\.value\)\)/)
  assert.doesNotMatch(loadSource, /scope|\.filter\(/)

  const agents = [
    { id: 'agent-a', scenario_id: 'scenario-a' },
    { id: 'agent-b', scenario_id: 'scenario-b' },
  ]
  assert.equal(scenarioIdFromQuery(['scenario-b', 'ignored']), 'scenario-b')
  assert.equal(scenarioIdFromQuery(undefined), '')
  assert.deepEqual(filterAgentsByScenario(agents, 'scenario-b'), [agents[1]])
  assert.equal(filterAgentsByScenario(agents, ''), agents)
})

test('new Agent runtime connections do not prefill a host port or username', () => {
  const source = readFileSync(new URL('../src/views/Agents.vue', import.meta.url), 'utf8')
  const addStart = source.indexOf('function addRuntimeConnection()')
  const removeStart = source.indexOf('function removeRuntimeConnection(', addStart)
  const addSource = source.slice(addStart, removeStart)

  assert.match(addSource, /host:\s*''/)
  assert.match(addSource, /port:\s*undefined/)
  assert.match(addSource, /username:\s*''/)
  assert.doesNotMatch(addSource, /127\.0\.0\.1|5432|username:\s*['"]postgres['"]/)
})
