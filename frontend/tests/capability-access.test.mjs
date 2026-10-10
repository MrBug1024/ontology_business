import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { mountPlatformNavigation } from './helpers/plugin-navigation-harness.mjs'

test('publishing selects reviewed plugins and only exposes MCP configuration', () => {
  const view = readFileSync(new URL('../src/views/CapabilityAccess.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/api/capabilityAccess.ts', import.meta.url), 'utf8')

  assert.match(api, /\/developer\/capability-access\/\$\{scenarioId\}\/manifest/)
  assert.match(api, /params: \{ release_id: releaseId \}, signal/)
  assert.match(view, /manifest\.deployment\.definition_hash/)
  assert.doesNotMatch(view, /protocol === 'rest'|REST API|curl -H/)
  assert.match(view, /adapters\.filter\(item => item\.protocol === 'mcp'\)/)
  assert.match(view, /PluginPublishing :scenario-id="scenarioId" :artifact-id=/)
  assert.match(view, /protocol === 'mcp'/)
  assert.match(view, /adapter\.managed_input_upload/)
  assert.match(view, /adapter\.optional_scopes/)
  assert.match(view, /value="assets:write"/)
  // 能力版本的生命周期治理是发布中心的职责：创建/启用/停用/退役都在这里，
  // 场景详情只保留能力建设页签。
  assert.match(view, /<el-tab-pane label="能力版本" name="releases">[\s\S]*?ScenarioReleaseList/)
  const development = readFileSync(new URL('../src/views/PluginDevelopment.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(development, /ScenarioReleaseList/)
  assert.match(development, /PluginBuildSetup/)
  // 场景的插件项目与编码会话由资源管理器 composable 按场景读取；会话不承载版本。
  assert.match(development, /usePluginProjectExplorer/)
  const explorer = readFileSync(new URL('../src/composables/usePluginProjectExplorer.ts', import.meta.url), 'utf8')
  assert.match(explorer, /pluginCodingApi\.projects/)
  assert.match(explorer, /pluginCodingApi\.sessions/)
  assert.doesNotMatch(explorer, /plugin_version/)
  const chatHome = readFileSync(new URL('../src/components/plugin-coding/PluginCodingChatHome.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(chatHome, /plugin_version/)
  assert.match(development, /capability-access', query: \{ scenario_id: scenarioId, tab: 'releases' \}/)
  const detail = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(detail, /ScenarioReleaseList/)
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

  for (const label of ['场景能力', '验证中心', '插件开发', '发布中心', '运行治理']) {
    assert.match(nav, new RegExp(label))
  }
  assert.doesNotMatch(nav, /index="\/(?:data-sources|business-distillation)"/)
  for (const path of ['/scenarios', '/data-sources', '/business-distillation/:id?', '/agents', '/access', '/tasks', '/templates', '/mcp']) {
    assert.match(router, new RegExp(`path: '${path.replace('/', '\\/')}`))
  }
  assert.ok(nav.indexOf('index="/scenarios"') < nav.indexOf('index="/agents"'))
  assert.ok(nav.indexOf('index="/agents"') < nav.indexOf('index="/plugin-studio"'))
  assert.ok(nav.indexOf('index="/plugin-studio"') < nav.indexOf('index="/access"'))
  assert.match(router, /path: '\/plugin-studio', name: 'plugin-development'/)
  assert.match(router, /path: '\/plugin-studio\/:releaseId'/)
})

test('clicking the real plugin menu opens the plugin development route and page', async () => {
  const view = await mountPlatformNavigation()
  try {
    assert.equal(view.router.currentRoute.value.name, 'scenarios')
    await view.clickMenu('插件开发')
    assert.equal(view.router.currentRoute.value.path, '/plugin-studio')
    assert.equal(view.router.currentRoute.value.name, 'plugin-development')
    assert.equal(view.router.currentRoute.value.meta.focusWorkspace, true)
    assert.ok(view.find(target => target.props['data-page'] === 'PluginDevelopment'))
  } finally { view.stop() }
})

test('an unknown plugin URL stays in plugin development instead of becoming a scenario page', async () => {
  const view = await mountPlatformNavigation('/plugin-studio/missing/nested?scenario_id=synthetic-scene')
  try {
    assert.equal(view.router.currentRoute.value.path, '/plugin-studio')
    assert.equal(view.router.currentRoute.value.name, 'plugin-development')
    assert.equal(view.router.currentRoute.value.query.scenario_id, 'synthetic-scene')
    assert.ok(view.find(target => target.props['data-page'] === 'PluginDevelopment'))
  } finally { view.stop() }
})

test('the plugin fallback preserves workspace deep links on the single-page IDE', async () => {
  const view = await mountPlatformNavigation('/plugin-studio/synthetic-release?workspace=synthetic-workspace')
  try {
    assert.equal(view.router.currentRoute.value.path, '/plugin-studio')
    assert.equal(view.router.currentRoute.value.name, 'plugin-development')
    assert.equal(view.router.currentRoute.value.query.workspace, 'synthetic-workspace')
    assert.ok(view.find(target => target.props['data-page'] === 'PluginDevelopment'))
  } finally { view.stop() }
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
