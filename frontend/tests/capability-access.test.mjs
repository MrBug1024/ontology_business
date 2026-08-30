import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('access center uses one server manifest for REST and MCP', () => {
  const view = readFileSync(new URL('../src/views/CapabilityAccess.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/api/capabilityAccess.ts', import.meta.url), 'utf8')

  assert.match(api, /\/developer\/capability-access\/\$\{scenarioId\}\/manifest/)
  assert.match(api, /\/scenarios\/\$\{scenarioId\}\/releases\/\$\{environment\}\/withdraw/)
  assert.match(view, /manifest\.deployment\.definition_hash/)
  assert.match(view, /protocol === 'rest'/)
  assert.match(view, /protocol === 'mcp'/)
  assert.match(view, /adapter\.managed_input_upload/)
  assert.match(view, /adapter\.optional_scopes/)
  assert.match(view, /value="assets:write"/)
  assert.match(view, /canWithdrawRelease/)
  assert.match(view, /撤下原因不能为空/)
  assert.match(api, /confirmed:\s*true/)
  assert.match(view, /密钥仅显示一次/)
  assert.doesNotMatch(view, /Agent 发布|兼容发布|section: 'published'/)
  assert.doesNotMatch(view, /data_source_id|dataset_version_id|provider_key|runtime_config/)
})

test('navigation exposes six target domains and preserves old routes', () => {
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const router = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')

  for (const label of ['场景能力', '建模资料', '验证中心', '发布与接入', '运行治理', '平台配置']) {
    assert.match(app, new RegExp(label))
  }
  for (const path of ['/scenarios', '/data-sources', '/agents', '/access', '/tasks', '/templates', '/mcp']) {
    assert.match(router, new RegExp(`path: '${path.replace('/', '\\/')}`))
  }
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
