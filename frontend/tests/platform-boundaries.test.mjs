import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import ts from 'typescript'

async function loadApi() {
  const compile = (source) => ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
  const readinessUrl = moduleUrl(compile(readFileSync(
    new URL('../src/utils/agentReadiness.ts', import.meta.url), 'utf8',
  )))
  const require = createRequire(import.meta.url)
  const axiosUrl = pathToFileURL(require.resolve('axios')).href
  const source = compile(readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8'))
    .replace(/from ['"]axios['"]/, `from '${axiosUrl}'`)
    .replace(/from ['"]@\/utils\/agentReadiness['"]/, `from '${readinessUrl}'`)
  return import(moduleUrl(`${source}\n//# sourceURL=modeling-file-api-test.js`))
}

test('reparsing a large modeling file can complete after the ordinary request deadline', async () => {
  const { api, http } = await loadApi()
  const response = { id: 'modeling-file', modeling_contract_schema_id: 'schema-version' }
  const ordinaryTimeout = http.defaults.timeout
  http.defaults.adapter = async (config) => {
    const simulatedParsingDuration = 180_000
    assert.equal(config.url, '/data-sources/files/modeling-file/reparse')
    assert.equal(config.method, 'post')
    assert.ok(config.timeout > simulatedParsingDuration, 'parsing was cut off by the ordinary request timeout')
    return { data: response, status: 200, statusText: 'OK', headers: {}, config }
  }
  assert.deepEqual(await api.reparseFile('modeling-file'), response)
  assert.equal(http.defaults.timeout, ordinaryTimeout)
})

test('modeling materials keep the old route and never become runtime data', () => {
  const viewSource = readFileSync(new URL('../src/views/DataSources.vue', import.meta.url), 'utf8')
  const editorSource = readFileSync(new URL('../src/components/library/LibraryEditorDialog.vue', import.meta.url), 'utf8')
  const apiSource = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')
  const routerSource = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')

  assert.match(viewSource, /<h1>资料库<\/h1>/)
  assert.match(viewSource, /用于业务蒸馏与场景建模/)
  assert.match(viewSource, /可绑定到一个业务场景，也可保留为工作区共享资料/)
  assert.match(editorSource, /label="业务场景"/)
  assert.match(viewSource, /绑定场景后，该场景可选择这些资料进行蒸馏和建模/)
  assert.match(viewSource, /这里的资料不会自动进入正式调用/)
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

test('scenario workspace starts with distillation and keeps all twelve stages in order', () => {
  const detailSource = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')
  const scenariosSource = readFileSync(new URL('../src/views/Scenarios.vue', import.meta.url), 'utf8')
  const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const stages = [...detailSource.matchAll(/<el-tab-pane\b[^>]*\bname="([^"]+)"/g)].map((match) => match[1])

  assert.deepEqual(stages, [
    'distillation', 'materials', 'ontology', 'instances', 'mappings', 'functions',
    'actions', 'rules', 'events', 'workflows', 'capability-inputs', 'candidates',
  ])
  assert.match(detailSource, /<el-tab-pane name="distillation"[\s\S]*?业务蒸馏/)
  assert.match(detailSource, /<el-tab-pane name="materials"[\s\S]*?场景资料/)
  assert.match(detailSource, /normalizeScenarioStage\(route\.query\.stage\)/)
  assert.match(detailSource, /function goToDataSources\(\)[\s\S]*?stage: 'materials'/)
  assert.match(scenariosSource, /query:\s*\{ stage: 'distillation' \}/)
  assert.match(appSource, /normalizeScenarioStage\(route\.query\.stage\)/)
  assert.match(appSource, /!\['distillation', 'materials'\]\.includes\(scenarioStage\.value\)/)
  assert.match(appSource, /<GlobalAssistant v-if="showGlobalAssistant"/)
  assert.doesNotMatch(appSource, /<GlobalAssistant[^\n]*v-show="showGlobalAssistant"/)
})

test('scenario stage normalization gives navigation and the global advisor one fallback', async () => {
  const source = ts.transpileModule(
    readFileSync(new URL('../src/utils/scenarioStages.ts', import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
  ).outputText
  const { normalizeScenarioStage, SCENARIO_STAGES } = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
  )

  assert.equal(SCENARIO_STAGES.length, 12)
  assert.equal(normalizeScenarioStage('ontology'), 'ontology')
  for (const value of [undefined, null, '', 'unknown', ['invalid', 'ontology'], {}]) {
    assert.equal(normalizeScenarioStage(value), 'distillation')
  }
})

test('embedded scenario materials lock writes to the current scenario permission', () => {
  const viewSource = readFileSync(new URL('../src/views/DataSources.vue', import.meta.url), 'utf8')
  const editorSource = readFileSync(new URL('../src/components/library/LibraryEditorDialog.vue', import.meta.url), 'utf8')
  const scenarioSource = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')

  assert.match(viewSource, /:lock-scenario="embedded"/)
  assert.match(viewSource, /api\.listDataSourceCatalog\(\{ scenario_id: props\.scenarioId, offset: catalogOffset\.value, limit: CATALOG_PAGE_SIZE \}\)/)
  assert.match(viewSource, /catalogHasMore\.value = Array\.isArray\(sourceResult\) \? false : sourceResult\.has_more/)
  assert.match(viewSource, /else if \(props\.embedded\) \{\s*catalogOffset\.value = 0\s*await router\.replace\(sourceLocation\(id\)\)\s*await load\(\)/)
  assert.match(viewSource, /materials_offset/)
  assert.match(scenarioSource, /v-if="detail\.can_read_workspace_context"/)
  assert.match(scenarioSource, /:show-templates="detail\.can_read_workspace_context"/)
  assert.match(editorSource, /if \(props\.lockScenario\) form\.value\.scenario_id = props\.scenarioId/)
  assert.match(viewSource, /v-if="canWrite" type="primary" size="small" @click="openCreate"/)
  assert.match(viewSource, /:disabled="!canWrite \|\| !selected\.can_write \|\| !uploadList\.length"/)
  assert.match(viewSource, /:disabled="!canWrite \|\| !selected\.can_write" @click="reindexFiles"/)
  assert.match(viewSource, /async function doUpload\(\) \{\s*if \(!canWrite\.value \|\| !selected\.value\?\.can_write\) return/)
  assert.match(viewSource, /async function reindexFiles\(\) \{\s*if \(!canWrite\.value \|\| !selected\.value\?\.can_write\) return/)
  assert.match(viewSource, /\.resource-section-group \{[^}]*min-width: 0;[^}]*grid-template-columns: minmax\(0, 1fr\)/)
})

function routeDefinition(source, path) {
  const pathIndex = source.indexOf(`path: '${path}'`)
  assert.notEqual(pathIndex, -1, `missing route for ${path}`)
  const start = source.lastIndexOf('{', pathIndex)
  assert.notEqual(start, -1, `missing route object for ${path}`)

  let depth = 0
  for (let index = start; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(start, index + 1)
  }
  assert.fail(`unterminated route object for ${path}`)
}

test('platform settings move to the side footer and scenario modeling owns the global advisor', () => {
  const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const navStart = appSource.indexOf('<nav class="side-nav"')
  const navEnd = appSource.indexOf('</nav>', navStart)
  const footerStart = appSource.indexOf('<div class="side-footer">')
  const footerEnd = appSource.indexOf('</el-aside>', footerStart)
  const navigation = appSource.slice(navStart, navEnd)
  const footer = appSource.slice(footerStart, footerEnd)
  const advisorTags = appSource.match(/<GlobalAssistant\b/g) || []

  assert.notEqual(navStart, -1)
  assert.notEqual(navEnd, -1)
  assert.notEqual(footerStart, -1)
  assert.notEqual(footerEnd, -1)
  assert.doesNotMatch(navigation, /<el-sub-menu index="settings">/)
  assert.doesNotMatch(navigation, /index="\/(?:templates|llm|mcp|skills)"/)
  assert.match(footer, /aria-label="打开平台设置"/)
  assert.match(footer, /<Setting/)
  assert.match(appSource, /PlatformSettingsDialog/)
  assert.match(appSource, /v-model="platformSettingsOpen"/)
  assert.match(appSource, /route\.query\.platform_settings/)
  assert.match(appSource, /platformSettingsQuery\(route\.query, tab\)/)
  assert.match(appSource, /platformSettingsQuery\(route\.query, null\)/)
  assert.equal(advisorTags.length, 1, 'the modeling advisor must not mount on unrelated routes')
  assert.match(appSource, /<GlobalAssistant\b[^>]*\bv-if="[^"]+"/)
  assert.match(appSource, /route\.name\s*===\s*'scenario-detail'/)
})

test('legacy configuration URLs always open the global settings surface', () => {
  const routerSource = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')

  for (const [path, tab] of [
    ['/llm', 'llm'],
    ['/mcp', 'mcp'],
    ['/skills', 'skills'],
  ]) {
    const route = routeDefinition(routerSource, path)
    assert.match(route, new RegExp(`legacyPlatformSettingsRedirect\\('${tab}'\\)`))
  }

  assert.doesNotMatch(routerSource, /if \(to\.query\.manage === '1'\) return true/)
  assert.match(routerSource, /delete query\.manage/)
  assert.match(routerSource, /name: 'data-sources'/)
  assert.match(routerSource, /query\.platform_settings = tab/)

  const templatesRoute = routeDefinition(routerSource, '/templates')
  assert.match(templatesRoute, /redirect\s*:/)
  assert.match(templatesRoute, /(?:path:\s*'\/data-sources'|name:\s*'data-sources')/)
  assert.match(templatesRoute, /library_tab:\s*'templates'/)
  assert.doesNotMatch(templatesRoute, /component\s*:/)
})

test('settings queries retain the current work and close cleanly on navigation', async () => {
  const compiled = ts.transpileModule(readFileSync(new URL('../src/utils/platformSettings.ts', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const { platformSettingsQuery, platformSettingsTabFromQuery } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
  const context = { stage: 'actions', scenario_id: 'synthetic-scenario', filters: ['active', 'owned'] }
  const opened = platformSettingsQuery(context, 'llm')
  assert.deepEqual(opened, { ...context, platform_settings: 'llm' })
  assert.equal(platformSettingsTabFromQuery(opened.platform_settings), 'llm')
  const switched = platformSettingsQuery(opened, 'mcp')
  assert.equal(platformSettingsTabFromQuery(switched.platform_settings), 'mcp')
  assert.deepEqual(platformSettingsQuery(switched, null), context)
  assert.deepEqual(context, { stage: 'actions', scenario_id: 'synthetic-scenario', filters: ['active', 'owned'] })
  assert.equal(platformSettingsTabFromQuery(['skills', 'mcp']), 'skills')
  for (const invalid of [undefined, null, '', 'templates', '/llm', {}, [null, 'llm']]) {
    assert.equal(platformSettingsTabFromQuery(invalid), null)
  }
})

test('settings manage resources inside the modal and keep nested editors above it', () => {
  const dialog = readFileSync(new URL('../src/components/platform/PlatformSettingsDialog.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(dialog, /openPage|useRouter|完整管理/)
  for (const panel of ['ModelSettingsPanel', 'SkillSettingsPanel', 'McpSettingsPanel']) {
    assert.match(dialog, new RegExp(`<${panel}`))
    const source = readFileSync(new URL(`../src/components/platform/${panel}.vue`, import.meta.url), 'utf8')
    for (const tag of source.matchAll(/<el-(?:dialog|drawer)\b[^>]*>/g)) {
      assert.match(tag[0], /append-to-body/)
    }
  }
  assert.match(dialog, /:close-on-click-modal="false"/)
  assert.match(dialog, /v-if="modelValue"/)
})

test('global tool settings separate governed tools from skill methods', () => {
  const tools = readFileSync(new URL('../src/components/platform/ToolSettingsPanel.vue', import.meta.url), 'utf8')
  assert.match(tools, /distillationConversationApi\.resources\(request\.signal\)/)
  assert.match(tools, /工具 \/ 技能边界/)
  assert.match(tools, /工具是平台统一执行的小颗粒能力/)
  assert.match(tools, /受信方法包/)
  assert.doesNotMatch(tools, /resources\.investigation_tools\.tools/)
  assert.match(tools, /平台 MCP 能力/)
  assert.match(tools, /connector\.name\.trim\(\)\.toLowerCase\(\) === 'jev_decide'/)
  assert.match(tools, /不会被当作业务输入资料/)
  assert.doesNotMatch(tools, /api\.mcpTools|tools\/call/)
  assert.match(tools, /onBeforeUnmount\(\(\) => \{ controller\?\.abort\(\)/)
})

test('artifact templates stay manageable inside the library after the legacy route redirect', () => {
  const librarySource = readFileSync(new URL('../src/views/DataSources.vue', import.meta.url), 'utf8')
  const templateSource = readFileSync(new URL('../src/views/Templates.vue', import.meta.url), 'utf8')
  const scenarioSource = readFileSync(new URL('../src/views/ScenarioDetail.vue', import.meta.url), 'utf8')

  assert.match(librarySource, /<el-tab-pane[^>]*label="产物模板"[^>]*name="library-templates"/)
  assert.match(librarySource, /<el-tab-pane[^>]*label="资料文件与数据库"[^>]*name="library-materials"/)
  assert.match(librarySource, /<Templates embedded :scenario-id="routeScenarioId" :can-write="canWrite" :active="activeLibraryTab === 'templates'" @show-materials="showMaterials"/)
  assert.match(librarySource, /function libraryTabFromQuery[\s\S]*?candidate === 'templates'/)
  assert.match(librarySource, /watch\(showTemplates, \(visible\) => \{[\s\S]*?activeLibraryTab\.value = libraryTabFromQuery\(route\.query\.library_tab\)/)
  assert.doesNotMatch(librarySource, /onMounted\(\(\) => \{[\s\S]*?showTemplates\.value[^\n]*route\.query\.library_tab[^\n]*onLibraryTabChanged\('materials'\)/)
  assert.match(librarySource, /query\.library_tab = 'templates'/)
  assert.match(librarySource, /delete query\.library_tab/)
  assert.match(scenarioSource, /stage: 'materials', library_tab: 'templates'/)

  assert.match(templateSource, /defineProps<\{ embedded\?: boolean; scenarioId\?: string; canWrite\?: boolean; active\?: boolean \}>/)
  assert.match(templateSource, /template-section-toolbar/)
  assert.match(templateSource, /canMutateTemplate/)
  assert.match(templateSource, /api\.listDataSources\(props\.embedded \? props\.scenarioId : undefined\)/)
  assert.match(templateSource, /watch\(\(\) => props\.active[\s\S]*?loadResources\(\)/)
  assert.match(templateSource, /templatesController\?\.abort\(\)/)
  assert.match(templateSource, /request !== templateRequest \|\| controller\.signal\.aborted/)
  assert.match(templateSource, /route\.query\.q, route\.query\.scenario_id, route\.query\.artifact_format, route\.query\.status/)
  assert.match(templateSource, /emit\('showMaterials'\)/)
  assert.match(templateSource, /const query = \{ \.\.\.route\.query \}/)
  assert.match(templateSource, /await api\.uploadTemplateVersion/)
  assert.match(templateSource, /current_version_id: versionId/)
  assert.match(templateSource, /function goToAction/)
})
