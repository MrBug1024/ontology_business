<template>
  <div class="development-home">
    <header class="development-toolbar">
      <div><el-icon aria-hidden="true"><Box /></el-icon><b>插件开发</b><span>AI 编码 · 源码审阅 · 沙箱隔离</span></div>
      <div v-if="workspace" class="workspace-state">
        <span class="phase-dot" :class="workspace.phase" aria-hidden="true" />
        <span role="status">{{ phaseLabel }}</span>
        <span v-if="runningMode === 'discuss'" class="source-state">{{ sourcePhaseLabel }}</span>
        <span v-if="lastPublished" class="version">最近定版 v{{ lastPublished }}</span>
        <span v-else class="version">未定版</span>
        <el-button text :loading="workspaceLoading" aria-label="刷新编码进度" @click="reloadWorkspace"><el-icon aria-hidden="true"><Refresh /></el-icon></el-button>
        <el-button size="small" :disabled="busy" @click="delivery?.open()"><el-icon aria-hidden="true"><Box /></el-icon>审阅定版</el-button>
        <el-button text size="small" @click="exitWorkspace">会话列表</el-button>
      </div>
      <RouterLink v-else to="/access">发布中心<el-icon aria-hidden="true"><ArrowRight /></el-icon></RouterLink>
    </header>
    <el-alert v-if="error || workspaceError || editorMessage" class="page-error" :title="error || workspaceError || editorMessage" type="error" :closable="false" show-icon />
    <section class="ide-start" :class="`show-${panel}`" aria-label="插件开发工作区">
      <nav class="mobile-panels" aria-label="开发工作区面板"><button v-for="item in panels" :key="item.key" type="button" :aria-pressed="panel === item.key" @click="panel = item.key">{{ item.label }}</button></nav>
      <PluginProjectExplorer class="ide-explorer" :scenarios="scenarios" :loading="loading" :explorer="explorer" :selected-scenario-id="scenarioId" @select-scenario="selectScenario" />
      <section class="editor" aria-label="代码编辑器">
        <p v-if="workspace && !workspace.files.length && workspaceLoading" class="editor-loading">{{ '正在恢复编码会话…' }}</p>
        <PluginCodingInspector v-else-if="workspace" ref="inspector" :workspace="workspace" :selected-path="editor.selectedPath.value" :selected-file="editor.selectedFile.value" :draft="editor.draft.value" :dirty="editor.dirty.value" :basis-changed="editor.basisChanged.value" :busy="busy" @select="openEditorFile" @update:draft="editor.draft.value = $event" @save="editor.submit('save')" @discard="editor.discardDraft" @rebase="editor.baseHash.value = workspace.files_hash" />
        <PluginFilePreview v-else-if="explorer.selectedFile && explorer.selectedProject" class="preview-slot" :project="explorer.selectedProject" :file="explorer.selectedFile" @open-project="openProject" />
        <div v-else class="editor-welcome"><el-icon :size="50" aria-hidden="true"><Box /></el-icon><h1>插件开发</h1><p>在左侧选择业务场景并展开插件源码，或从右侧会话历史继续；点击「新建任务」让 AI 在同一份源码上开始编码——全部在本页完成。</p><dl><div><dt>场景上下文</dt><dd>每个场景维护一份插件源码，新建任务时固定能力版本</dd></div><div><dt>AI 编码</dt><dd>生成 Skill、工具脚本与安装说明</dd></div><div><dt>源代码审阅</dt><dd>查看文件、差异、校验结果并修正</dd></div><div><dt>沙箱隔离</dt><dd>插件源码存放于平台侧隔离工作区，平台只校验、从不执行</dd></div><div><dt>发布安装</dt><dd>定版产生版本快照，进入发布中心生成安装命令</dd></div></dl></div>
      </section>
      <aside class="assistant" aria-label="AI 编码">
        <template v-if="workspace">
          <header class="ai-heading"><el-icon aria-hidden="true"><Cpu /></el-icon><b>AI 编码助手</b><small role="status">{{ activeRun ? runningMode === 'discuss' ? '正在分析项目' : '正在编写代码' : interactionMode === 'discuss' ? '讨论方案' : '编码与修正' }}</small><button type="button" class="heading-settings" aria-label="打开编码 AI 设置" @click="openSettingsFromEvent"><el-icon aria-hidden="true"><Setting /></el-icon></button></header>
          <PluginCodingConversation :workspace="workspace" @inspect="inspect" @checks="showChecks" @changes="showChanges" @retry="retryTurn" />
          <PluginCodingComposer ref="composer" v-model="editor.feedback.value" v-model:mode="interactionMode" input-id="coding-instruction" label="插件编码修正意见" :placeholder="interactionMode === 'discuss' ? '讨论方案、解释文件或分析场景能力…' : '描述接下来的修改，或纠正当前方向…'" :model-label="modelLabel" :skill-count="resourceSelection.skill_ids.length" :mcp-count="resourceSelection.mcp_ids.length" :busy="busy" :generating="activeRun" :can-send="!(editor.dirty.value && editor.basisChanged.value) && !(interactionMode === 'discuss' && editor.dirty.value)" :hint="composerHint" allow-discussion @submit="editor.submit(interactionMode)" @stop="editor.stopCoding" @settings="openSettings" />
        </template>
        <PluginCodingChatHome v-else ref="chatHome" :scenario="selectedScenario" :sessions="scenarioSessions" :explorer="explorer" :active-session-id="sessionId" :busy="loadingReleases" :initial-new="Boolean(releaseId) && Boolean(scenarioId)" @start-new="startNew" @open-session="openSession">
          <section class="scene-context" aria-label="插件任务的能力版本"><div v-if="scenarioId"><label for="development-version">能力版本</label><el-select id="development-version" v-model="releaseId" aria-label="插件开发能力版本" :loading="loadingReleases" placeholder="选择已启用的明确版本"><el-option v-for="item in releases" :key="item.id" :value="item.id" :label="item.name" :disabled="!item.enabled || item.status !== 'released'" /></el-select><p v-if="!loadingReleases && !releases.some(item => item.enabled)" class="version-hint">此场景尚无启用版本<RouterLink :to="{ name: 'capability-access', query: { scenario_id: scenarioId, tab: 'releases' } }">前往发布中心管理</RouterLink></p><p v-else-if="existingProject && releaseId" class="version-hint">已有插件源码；新任务在当前源码上继续，能力版本仅在切换时刷新契约。</p></div><div v-else><p class="version-hint">请先在左侧资源管理器中选择业务场景。</p></div></section>
          <PluginBuildSetup v-if="release" :key="release.id" :release="release" :existing-project="existingProject" :initial-instruction="taskInstruction" :initial-settings="taskSettings" compact @instruction="taskInstruction = $event" @settings="updateTaskSettings" @created="created" @unsaved="draftUnsaved = $event || taskSettingsDirty" />
          <section v-else class="unbound-task" aria-label="新建插件编码任务"><div class="unbound-transcript"><el-icon :size="28" aria-hidden="true"><Cpu /></el-icon><h2>一起构建场景插件</h2><p>{{ scenarioId ? '选择已启用的能力版本，让 AI 了解要封装的业务。可以先写下目标，或在设置中选择模型、安装技能与 MCP。' : '在左侧选择业务场景后，选择明确的能力版本开始编码。' }}</p><div v-if="scenarioId" class="unbound-context"><b>等待绑定场景能力</b><p>{{ loadingReleases ? '正在读取能力版本…' : '选定版本后，完整输入、输出和执行约束将提供给 AI。' }}</p></div></div><PluginCodingComposer v-model="taskInstruction" input-id="unbound-instruction" label="插件编码目标" placeholder="描述插件服务谁、需要完成什么业务，以及你希望的使用方式…" :model-label="modelLabel" :skill-count="taskSettings.skill_ids.length" :mcp-count="taskSettings.mcp_ids.length" :can-send="false" hint="选择能力版本后开始编码，需求与设置会保留。" @update:model-value="draftUnsaved = Boolean($event.trim())" @settings="openSettings" /></section>
        </PluginCodingChatHome>
      </aside>
    </section>
    <PluginCodingDelivery v-if="workspace" ref="delivery" :workspace="workspace" :dirty="unsaved" :busy="busy" :last-published="lastPublished" @refresh="onReviewed" />
    <PluginCodingSettings v-model="settingsOpen" :selection="resourceSelection" :catalog="resourceCatalog" :loading="resourcesLoading" :saving="busy || settingsSaving" :error="settingsError || workspaceError" :active-run="activeRun" :opener="settingsOpener" @save="saveSettings" @reload="loadResources" @stop="editor.stopCoding" @manage="manage" />
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowRight, Box, Cpu, Refresh, Setting } from '@element-plus/icons-vue'
import { api } from '@/api'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import PluginBuildSetup from '@/components/plugin-coding/PluginBuildSetup.vue'
import PluginCodingChatHome from '@/components/plugin-coding/PluginCodingChatHome.vue'
import PluginCodingComposer from '@/components/plugin-coding/PluginCodingComposer.vue'
import PluginCodingConversation from '@/components/plugin-coding/PluginCodingConversation.vue'
import PluginCodingDelivery from '@/components/plugin-coding/PluginCodingDelivery.vue'
import PluginCodingInspector from '@/components/plugin-coding/PluginCodingInspector.vue'
import PluginCodingSettings from '@/components/plugin-coding/PluginCodingSettings.vue'
import PluginFilePreview from '@/components/plugin-coding/PluginFilePreview.vue'
import PluginProjectExplorer from '@/components/plugin-coding/PluginProjectExplorer.vue'
import { emptyCodingSelection, usePluginCodingSettings } from '@/composables/usePluginCodingSettings'
import { usePluginCodingEditor } from '@/composables/usePluginCodingEditor'
import { usePluginCodingWorkspace } from '@/composables/usePluginCodingWorkspace'
import { usePluginProjectExplorer } from '@/composables/usePluginProjectExplorer'
import { platformSettingsQuery, type PlatformSettingsTab } from '@/utils/platformSettings'
import { createClientRequestId } from '@/utils/clientRequestId'
import type { Scenario } from '@/types'
import type { ScenarioRelease } from '@/types/scenarioRelease'
import type { CodingProjectSummary, CodingResourceSelection, CodingSession, CodingWorkspace } from '@/types/pluginCoding'

const route = useRoute()
const router = useRouter()
const scenarios = ref<Scenario[]>([])
const releases = ref<ScenarioRelease[]>([])
const loading = ref(false)
const loadingReleases = ref(false)
const error = ref('')
const draftUnsaved = ref(false)
const taskInstruction = ref('')
const explorer = usePluginProjectExplorer()
const chatHome = ref<InstanceType<typeof PluginCodingChatHome> | null>(null)
const inspector = ref<InstanceType<typeof PluginCodingInspector> | null>(null)
const composer = ref<InstanceType<typeof PluginCodingComposer> | null>(null)
const delivery = ref<InstanceType<typeof PluginCodingDelivery> | null>(null)
const settingsOpener = shallowRef<HTMLElement | null>(null)
const panel = ref('assistant')
const panels = [{ key: 'explorer', label: '场景' }, { key: 'editor', label: '代码' }, { key: 'assistant', label: 'AI 编码' }]

const scenarioId = computed(() => typeof route.query.scenario_id === 'string' ? route.query.scenario_id : '')
const workspaceId = computed(() => typeof route.query.workspace === 'string' ? route.query.workspace : '')
const sessionId = computed(() => typeof route.query.session === 'string' ? route.query.session : '')
const releaseId = computed({ get: () => typeof route.query.release_id === 'string' ? route.query.release_id : '', set: (value: string) => { void router.replace({ query: { ...route.query, release_id: value || undefined } }) } })
const release = computed(() => releases.value.find(item => item.id === releaseId.value))
const selectedScenario = computed(() => scenarios.value.find(item => item.id === scenarioId.value) || null)
const scenarioSessions = computed(() => explorer.sessionsFor(scenarioId.value))
const existingProject = computed(() => explorer.projectsFor(scenarioId.value)[0] || null)
const lastPublished = computed(() => explorer.projectSummary(workspaceId.value)?.plugin_version || '')

const workspaceRef = computed(() => workspaceId.value)
const sessionRef = computed(() => sessionId.value)
const { workspace, loading: workspaceLoading, busy, error: workspaceError, load: reloadWorkspace, revise, settings: applySettings } = usePluginCodingWorkspace(workspaceRef, sessionRef)
const editor = usePluginCodingEditor(workspace, revise)
const editorMessage = editor.message
const interactionMode = ref<'generate' | 'discuss'>('generate')
const resourceSelection = ref<CodingResourceSelection>(emptyCodingSelection())
const { catalog: resourceCatalog, open: settingsOpen, loading: resourcesLoading, saving: settingsSaving, error: settingsError, modelLabel, load: loadResources, save: saveDraftSettings } = usePluginCodingSettings(scenarioId, resourceSelection, async value => {
  const current = workspace.value
  if (!current) return false
  return applySettings({ expected_revision: current.revision, request_id: createClientRequestId(), ...value })
})
const taskSettings = ref<CodingResourceSelection>(emptyCodingSelection())
const taskSettingsDirty = ref(false)

const activeRun = computed(() => Boolean(workspace.value?.run_status && ['waiting_upload', 'queued', 'running'].includes(workspace.value.run_status)))
const runningMode = computed(() => { const turns = workspace.value?.turns || []; return turns[turns.length - 1]?.mode || 'generate' })
const phaseLabels: Record<string, string> = { draft: '修订待审阅', generating: 'AI 编码中', ready_for_review: '等待审阅', validation_failed: '需要修正', released: '已定版（历史状态）' }
const phaseLabel = computed(() => {
  const value = workspace.value
  if (!value) return ''
  if (runningMode.value === 'discuss') {
    if (value.run_status === 'failed') return '讨论未完成，可重试'
    if (value.run_status === 'cancelled') return '讨论已停止，可继续'
    if (activeRun.value) return 'AI 讨论中'
  }
  return value.run_status === 'failed' ? '本轮未完成，可继续修正' : phaseLabels[value.phase]
})
const sourcePhaseLabel = computed(() => workspace.value ? ({ draft: '候选草稿', generating: '候选生成中', ready_for_review: '候选待审阅', validation_failed: '候选待修正', released: '插件已定版' })[workspace.value.source_phase || workspace.value.phase] : '')
const composerHint = computed(() => interactionMode.value === 'discuss' ? editor.dirty.value ? '讨论基于已保存文件，请先保存当前代码；草稿会保留。' : activeRun.value ? '发送讨论将停止当前轮次，基于已保存文件分析方案。' : '讨论基于当前已保存文件，分析方案与能力契约。' : activeRun.value ? '发送修正将停止旧轮次，并从当前文件开始新轮次。' : editor.dirty.value ? '发送时将连同未保存文件一起提交。' : '候选代码由人工审阅和业务验收后定版。')
const unsaved = computed(() => editor.dirty.value || Boolean(editor.feedback.value.trim()) || draftUnsaved.value)

async function load() {
  loading.value = true
  error.value = ''
  try { scenarios.value = await api.listScenarios() }
  catch (caught: unknown) { error.value = caught instanceof Error ? caught.message : '业务场景加载失败' }
  finally { loading.value = false }
}
function selectScenario(id: string) {
  if (id === scenarioId.value) return
  // Switching scenarios leaves the coding state; release choice starts over.
  void router.replace({ query: { ...route.query, scenario_id: id || undefined, release_id: undefined, workspace: undefined, session: undefined, new: undefined } })
}
let releaseGeneration = 0
let releaseController: AbortController | undefined
async function loadReleases() {
  releaseController?.abort()
  const current = ++releaseGeneration
  releases.value = []
  if (!scenarioId.value) { loadingReleases.value = false; return }
  releaseController = new AbortController()
  loadingReleases.value = true
  try {
    const page = await scenarioReleasesApi.list(scenarioId.value, 0, releaseController.signal)
    if (current !== releaseGeneration) return
    releases.value = page.items
    const enabled = page.items.filter(item => item.enabled && item.status === 'released')
    if (!releaseId.value && enabled.length === 1) releaseId.value = enabled[0]?.id || ''
  } catch (caught: unknown) { if (current === releaseGeneration) error.value = caught instanceof Error ? caught.message : '能力版本加载失败' }
  finally { if (current === releaseGeneration) loadingReleases.value = false }
}
function openProject(project: CodingProjectSummary) {
  // Opening the plugin source enters the coding state on its latest session.
  const sessions = explorer.sessionsFor(project.scenario_id || scenarioId.value)
  const target = sessions.find(session => session.project_id === project.id && !session.frozen)
  if (target) { openSession(target); return }
  if (project.id === workspaceId.value) return
  void router.replace({ query: { ...route.query, scenario_id: project.scenario_id || scenarioId.value, workspace: project.id, session: undefined, release_id: undefined, new: undefined } })
  panel.value = 'editor'
}
function openSession(session: CodingSession) {
  const workspaceTarget = session.frozen ? session.id : session.project_id
  const sessionTarget = session.frozen ? undefined : session.id
  if (workspaceTarget === workspaceId.value && (sessionTarget || undefined) === (sessionId.value || undefined)) return
  void router.replace({ query: { ...route.query, scenario_id: session.scenario_id, workspace: workspaceTarget, session: sessionTarget, release_id: undefined, new: undefined } })
  explorer.expandScenario(session.scenario_id)
  panel.value = 'editor'
}
async function exitWorkspace() {
  if (unsaved.value) {
    try { await ElMessageBox.confirm('还有未提交的修改或输入。返回会话列表不会丢弃已保存的会话内容。', '保留当前工作？', { confirmButtonText: '返回', cancelButtonText: '继续编辑', type: 'warning' }) }
    catch { return }
  }
  editor.feedback.value = ''
  void router.replace({ query: { ...route.query, workspace: undefined, session: undefined } })
}
async function startNew() {
  if (draftUnsaved.value || editor.dirty.value) {
    try { await ElMessageBox.confirm('当前需求还未提交。开始新任务会清空输入。', '开始新任务？', { confirmButtonText: '清空并开始', cancelButtonText: '继续编辑' }) }
    catch { return }
  }
  taskInstruction.value = ''
  draftUnsaved.value = false
  if (releaseId.value) releaseId.value = ''
  chatHome.value?.openNew()
}
function created(value: CodingWorkspace) {
  taskSettingsDirty.value = false
  draftUnsaved.value = false
  explorer.reloadScenario(scenarioId.value)
  void router.replace({ query: { ...route.query, workspace: value.id, session: value.session_id || undefined, release_id: undefined, new: undefined } })
  panel.value = 'editor'
}

async function onReviewed() {
  // Reviews never touch the project, so only snapshot-derived data refreshes.
  await reloadWorkspace()
  explorer.reloadScenario(scenarioId.value)
}
function openEditorFile(path: string) { if (!busy.value) editor.selectFile(path) }
function inspect(path: string) { if (busy.value) return; editor.selectFile(path); inspector.value?.inspect(); panel.value = 'editor' }
function showChecks() { inspector.value?.checks(); panel.value = 'editor' }
function showChanges() { inspector.value?.changes(); panel.value = 'editor' }
async function retryTurn(instruction: string, mode: 'generate' | 'discuss') {
  if (busy.value || !editor.restoreInstruction(instruction)) return
  interactionMode.value = mode
  panel.value = 'assistant'
  await nextTick()
  composer.value?.focus()
}
function openSettings(opener: HTMLElement | null) { settingsOpener.value = opener; settingsOpen.value = true }
function openSettingsFromEvent(event: MouseEvent) { openSettings(event.currentTarget as HTMLElement | null) }
function manage(tab: PlatformSettingsTab) { settingsOpener.value = null; settingsOpen.value = false; void router.replace({ query: platformSettingsQuery(route.query, tab) }) }
function updateTaskSettings(value: CodingResourceSelection) { taskSettings.value = value; taskSettingsDirty.value = true; draftUnsaved.value = true }
async function saveSettings(value: CodingResourceSelection) { if (await saveDraftSettings(value)) updateTaskSettings(value) }

watch(scenarioId, value => { if (value) explorer.expandScenario(value) }, { immediate: true })
watch(scenarioId, loadReleases, { immediate: true })
watch(() => route.query.platform_settings, (value, previous) => { if (!value && previous) void loadResources() })
watch(workspace, value => { if (value) resourceSelection.value = value.resource_selection || emptyCodingSelection() })
watch(() => explorer.selection, value => { if (value?.projectId === workspaceId.value) openEditorFile(value.path) })
watch(() => workspace.value?.scenario_id, value => {
  // Deep links carry only the workspace id; recover the owning scenario so the
  // explorer highlights it without leaving the coding state.
  if (value && value !== scenarioId.value) void router.replace({ query: { ...route.query, scenario_id: value } })
})
async function keepDraft() {
  if (!unsaved.value) return true
  try { await ElMessageBox.confirm('还有未提交的修改或输入。离开会丢失这些本地草稿，已保存的会话仍可恢复。', '保留当前工作？', { confirmButtonText: '离开', cancelButtonText: '继续编辑' }); return true } catch { return false }
}
onBeforeRouteLeave(keepDraft)
function beforeUnload(event: BeforeUnloadEvent) { if (unsaved.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { void load(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload) )
</script>
<style scoped>
.development-home { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--surface); }
.development-toolbar { display: flex; justify-content: space-between; align-items: center; padding: 0 16px; border-bottom: 1px solid var(--border); gap: 12px; min-height: 46px; flex: 0 0 auto; font-size: 12px; background: var(--surface-2); }
.development-toolbar > div:first-child { display: flex; align-items: center; gap: 10px; min-width: 0; }
.development-toolbar span { color: var(--text-2); font-size: 11px; }
.development-toolbar a { display: inline-flex; align-items: center; gap: 6px; text-decoration: none; color: var(--text-2); white-space: nowrap; }
.workspace-state { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.workspace-state .version { color: var(--text-2); }
.workspace-state .source-state { color: var(--text-2); font-size: 11px; }
.phase-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-2); }
.phase-dot.generating { background: var(--primary); }
.phase-dot.ready_for_review { background: var(--success); }
.phase-dot.validation_failed { background: var(--warning); }
.page-error { flex: 0 0 auto; }
.ide-start { display: grid; grid-template-columns: 288px minmax(300px, 1fr) 400px; flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.ide-explorer { border-right: 1px solid var(--border); }
.editor { display: flex; flex-direction: column; min-height: 0; min-width: 0; background: var(--surface); }
.editor-loading { padding: 32px; color: var(--text-2); }
.preview-slot { flex: 1; min-height: 0; }
.editor-welcome { margin: auto; max-width: 420px; padding: 28px; color: var(--text-2); }
.editor-welcome > .el-icon { opacity: .45; }
h1 { font-size: 26px; font-weight: 500; color: var(--text); margin: 18px 0 8px; }
.editor-welcome > p { font-size: 12px; line-height: 1.8; }
dl { margin-top: 28px; font-size: 12px; }
dl > div { padding: 11px 0; border-bottom: 1px solid var(--border); }
dt { color: var(--text); margin-bottom: 5px; }
dd { margin: 0; line-height: 1.6; }
.assistant { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-left: 1px solid var(--border); background: var(--surface); }
.ai-heading { display: flex; align-items: center; gap: 7px; padding: 11px 14px; min-height: 43px; border-bottom: 1px solid var(--border); background: var(--surface); font-size: 12px; flex: 0 0 auto; }
.ai-heading small { color: var(--text-2); font-size: 11px; }
.heading-settings { margin-left: auto; display: grid; place-items: center; width: 44px; height: 44px; border: 0; border-radius: 7px; color: var(--text-2); background: transparent; cursor: pointer; }
.heading-settings:hover { color: var(--text); background: var(--surface-3); }
.scene-context { display: grid; flex: 0 0 auto; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.scene-context label { display: block; font-size: 11px; color: var(--text-2); margin-bottom: 6px; }
.scene-context .el-select { width: 100%; min-width: 0; }
.version-hint { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 12px; color: var(--text-2); flex-wrap: wrap; }
.version-hint a { color: var(--primary); font-size: 12px; }
.unbound-task { display: flex; flex-direction: column; flex: 1; min-height: 0; background: var(--surface-2); }
.unbound-transcript { flex: 1; min-height: 0; overflow: auto; padding: 24px 20px; }
.unbound-transcript > .el-icon { color: var(--primary); }
.unbound-transcript h2 { font-size: 18px; font-weight: 600; margin: 12px 0; }
.unbound-context { padding: 14px; margin-top: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-size: 13px; }
.unbound-task p { font-size: 12px; color: var(--text-2); line-height: 1.8; }
.mobile-panels { display: none; }
a:focus-visible, button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (min-width: 1600px) { .ide-start { grid-template-columns: 320px minmax(360px, 1fr) 460px; } }
@media (max-width: 1200px) { .ide-start { grid-template-columns: 248px minmax(260px, 1fr) 350px; } }
@media (max-width: 980px) {
  .ide-start { display: flex; flex-direction: column; }
  .mobile-panels { display: flex; min-height: 44px; flex: 0 0 auto; border-bottom: 1px solid var(--border); }
  .mobile-panels button { flex: 1; min-height: 44px; background: transparent; border: 0; color: var(--text-2); cursor: pointer; font: inherit; font-size: 12px; }
  .mobile-panels button[aria-pressed='true'] { color: var(--text); border-bottom: 2px solid var(--primary); }
  .ide-start > .ide-explorer, .ide-start > .editor, .ide-start > .assistant { display: none; flex: 1; border: 0; }
  .show-explorer > .ide-explorer, .show-editor > .editor, .show-assistant > .assistant { display: flex; }
}
</style>
