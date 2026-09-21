import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('access center uses one server manifest for REST and MCP', () => {
  const view = readFileSync(new URL('../src/views/CapabilityAccess.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/api/capabilityAccess.ts', import.meta.url), 'utf8')

  assert.match(api, /\/developer\/capability-access\/\$\{scenarioId\}\/manifest/)
  assert.match(api, /params: \{ release_id: releaseId \}, signal/)
  assert.match(view, /manifest\.deployment\.definition_hash/)
  assert.match(view, /protocol === 'rest'/)
  assert.match(view, /protocol === 'mcp'/)
  assert.match(view, /adapter\.managed_input_upload/)
  assert.match(view, /adapter\.optional_scopes/)
  assert.match(view, /value="assets:write"/)
  assert.match(view, /ScenarioReleaseList/)
  const releases = readFileSync(new URL('../src/api/scenarioReleases.ts', import.meta.url), 'utf8')
  assert.match(releases, /scenario-releases/)
  assert.match(releases, /expected_revision/)
  assert.match(releases, /confirmed: true/)
  assert.doesNotMatch(view, /environment|canWithdrawRelease/)
  assert.match(view, /密钥仅显示一次/)
  assert.doesNotMatch(view, /Agent 发布|兼容发布|section: 'published'/)
  assert.doesNotMatch(view, /data_source_id|dataset_version_id|provider_key|runtime_config/)
})

test('navigation starts from scenarios while legacy material and distillation routes remain compatible', () => {
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const router = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')
  const nav = app.slice(app.indexOf('<nav class="side-nav"'), app.indexOf('</nav>'))

  for (const label of ['场景能力', '验证中心', '发布与接入', '运行治理']) {
    assert.match(nav, new RegExp(label))
  }
  assert.doesNotMatch(nav, /index="\/(?:data-sources|business-distillation)"/)
  for (const path of ['/scenarios', '/data-sources', '/business-distillation/:id?', '/agents', '/access', '/tasks', '/templates', '/mcp']) {
    assert.match(router, new RegExp(`path: '${path.replace('/', '\\/')}`))
  }
  assert.ok(nav.indexOf('index="/scenarios"') < nav.indexOf('index="/agents"'))
  assert.ok(nav.indexOf('index="/agents"') < nav.indexOf('index="/access"'))
})

test('access center keeps loading feedback around manifests without flashing empty states', () => {
  const view = readFileSync(new URL('../src/views/CapabilityAccess.vue', import.meta.url), 'utf8')

  assert.match(
    view,
    /<div v-loading="loadingManifest" class="adapter-grid">[\s\S]*?<el-empty v-if="!loadingManifest && !manifest"/,
  )
  assert.match(
    view,
    /<div v-loading="loadingManifest" class="manifest-tab">[\s\S]*?<el-empty v-else-if="!loadingManifest"/,
  )
  assert.doesNotMatch(view, /<el-empty v-(?:if|else-if)="!manifest"/)
})
