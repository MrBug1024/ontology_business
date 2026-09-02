import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('modeling materials keep the old route and never become runtime data', () => {
  const viewSource = readFileSync(new URL('../src/views/DataSources.vue', import.meta.url), 'utf8')
  const apiSource = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')
  const routerSource = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')

  assert.match(viewSource, /<h1>建模资料<\/h1>/)
  assert.match(viewSource, /本页全部都是建模资料/)
  assert.match(viewSource, /可绑定到一个业务场景，也可保留为租户共享建模资料/)
  assert.match(viewSource, /label="建模场景"/)
  assert.match(viewSource, /只影响建模时的资料选择与访问范围/)
  assert.match(viewSource, /本页任何资料都不会自动进入正式调用/)
  assert.doesNotMatch(viewSource, /目录与用途|物理接入与文件|ScenarioDatasetBinding/)
  assert.match(apiSource, /\/catalog\/assets/)
  assert.match(apiSource, /\/catalog\/datasets/)
  assert.match(apiSource, /\/scenarios\/\$\{scenarioId\}\/dataset-bindings/)
  assert.match(routerSource, /path: '\/data-sources'/)
})

test('scenario removal is an auditable retirement workflow', () => {
  const listView = readFileSync(new URL('../src/views/Scenarios.vue', import.meta.url), 'utf8')
  const detailView = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')
  const apiSource = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')

  assert.match(apiSource, /listScenarios: \(includeRetired = false\)/)
  assert.match(apiSource, /include_retired: includeRetired/)
  assert.match(listView, /api\.listScenarios\(true\)/)
  assert.match(listView, /已退役/)
  assert.match(listView, /退役会暂停新的验证和调用，但保留全部配置/)
  assert.match(listView, /s\.status !== 'retired'/)
  assert.match(detailView, /detail\.status === 'retired'/)
  assert.match(listView, /@click="restore\(s\)"/)
  assert.match(listView, /@click="openPurge\(s\)"/)
})

test('scenario lifecycle toggle binds stable values instead of display labels', () => {
  const view = readFileSync(new URL('../src/views/Scenarios.vue', import.meta.url), 'utf8')
  const toggleStart = view.indexOf('<el-radio-group v-model="viewMode"')
  const toggleEnd = view.indexOf('</el-radio-group>', toggleStart)

  assert.notEqual(toggleStart, -1)
  assert.notEqual(toggleEnd, -1)
  const toggle = view.slice(toggleStart, toggleEnd)
  assert.match(toggle, /aria-label="场景状态筛选"/)
  assert.match(toggle, /<el-radio-button value="current">当前场景<\/el-radio-button>/)
  assert.match(toggle, /<el-radio-button value="retired">已退役<\/el-radio-button>/)
  assert.doesNotMatch(toggle, /<el-radio-button\s+label=/)
})
