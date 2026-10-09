<template>
  <div class="development-home">
    <header class="development-toolbar"><div><el-icon aria-hidden="true"><Box /></el-icon><b>插件开发</b><span>新建工作区</span></div><RouterLink to="/access">发布中心<el-icon aria-hidden="true"><ArrowRight /></el-icon></RouterLink></header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" /><el-button v-if="error" @click="load">重新加载</el-button>
    <PluginIdeStart :tasks="tasks" :loading="loading">
      <section class="scene-context" aria-label="插件任务的业务场景"><div><label for="development-scenario">业务场景</label><el-select id="development-scenario" v-model="scenarioId" aria-label="插件开发业务场景" filterable placeholder="选择要封装的业务场景" :loading="loading"><el-option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id" :label="scenario.name" /></el-select></div><div v-if="scenarioId"><label for="development-version">能力版本</label><el-select id="development-version" v-model="releaseId" aria-label="插件开发能力版本" :loading="loadingReleases" placeholder="选择已启用的明确版本"><el-option v-for="item in releases" :key="item.id" :value="item.id" :label="item.name" :disabled="!item.enabled || item.status !== 'released'" /></el-select></div></section>
      <PluginBuildSetup v-if="release" :key="release.id" :release="release" :initial-instruction="taskInstruction" :initial-settings="taskSettings" compact @instruction="taskInstruction = $event" @settings="updateTaskSettings" @created="created" @unsaved="unsaved = $event || taskSettingsDirty" />
      <section v-else class="unbound-task" aria-label="新建插件编码任务"><div class="unbound-transcript"><el-icon :size="28" aria-hidden="true"><Cpu /></el-icon><h2>一起构建场景插件</h2><p>选择业务场景与明确的能力版本，让 AI 了解要封装的业务。可以先写下目标，或在设置中选择模型、安装技能与 MCP。</p><div class="unbound-context"><b>等待绑定场景能力</b><p>{{ loadingReleases ? '正在读取能力版本…' : '选定版本后，完整输入、输出和执行约束将提供给 AI。' }}</p><RouterLink v-if="scenarioId && !loadingReleases && !releases.some(item => item.enabled)" :to="{ name: 'scenario-detail', params: { id: scenarioId } }">此场景尚无启用版本，前往场景管理完善</RouterLink></div></div><PluginCodingComposer v-model="taskInstruction" input-id="unbound-instruction" label="插件编码目标" placeholder="描述插件服务谁、需要完成什么业务，以及你希望的使用方式…" :model-label="modelLabel" :skill-count="taskSettings.skill_ids.length" :mcp-count="taskSettings.mcp_ids.length" :can-send="false" hint="选择能力版本后开始编码，需求与设置会保留。" @update:model-value="unsaved = Boolean($event.trim())" @settings="openSettings" /></section>
    </PluginIdeStart>
    <PluginCodingSettings v-model="settingsOpen" :selection="taskSettings" :catalog="resourceCatalog" :loading="resourcesLoading" :saving="settingsSaving" :error="settingsError" :opener="settingsOpener" draft-task @save="saveDraftSettings" @reload="loadResources" @manage="manage" />
  </div>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowRight, Box, Cpu } from '@element-plus/icons-vue'
import { api } from '@/api'
import { pluginCodingApi } from '@/api/pluginCoding'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import PluginBuildSetup from '@/components/plugin-coding/PluginBuildSetup.vue'
import PluginIdeStart from '@/components/plugin-coding/PluginIdeStart.vue'
import PluginCodingComposer from '@/components/plugin-coding/PluginCodingComposer.vue'
import PluginCodingSettings from '@/components/plugin-coding/PluginCodingSettings.vue'
import { emptyCodingSelection, usePluginCodingSettings } from '@/composables/usePluginCodingSettings'
import { platformSettingsQuery, type PlatformSettingsTab } from '@/utils/platformSettings'
import type { Scenario } from '@/types'
import type { ScenarioRelease } from '@/types/scenarioRelease'
import type { CodingResourceSelection, CodingTask, CodingWorkspace } from '@/types/pluginCoding'
const route = useRoute()
const router = useRouter()
const scenarios = ref<Scenario[]>([])
const releases = ref<ScenarioRelease[]>([])
const tasks = ref<CodingTask[]>([])
const loading = ref(false)
const loadingReleases = ref(false)
const error = ref('')
const unsaved = ref(false)
const taskInstruction = ref('')
const scenarioId = computed({ get: () => typeof route.query.scenario_id === 'string' ? route.query.scenario_id : '', set: (value: string) => { void router.replace({ query: { scenario_id: value } }) } })
const releaseId = computed({ get: () => typeof route.query.release_id === 'string' ? route.query.release_id : '', set: (value: string) => { void router.replace({ query: { ...route.query, release_id: value } }) } })
const release = computed(() => releases.value.find(item => item.id === releaseId.value))
const taskSettings = ref<CodingResourceSelection>(emptyCodingSelection())
const taskSettingsDirty = ref(false)
const settingsOpener = shallowRef<HTMLElement | null>(null)
const { catalog: resourceCatalog, open: settingsOpen, loading: resourcesLoading, saving: settingsSaving, error: settingsError, modelLabel, load: loadResources, save: saveSettings } = usePluginCodingSettings(scenarioId, taskSettings)
let generation = 0
let releaseGeneration = 0
let controller: AbortController | undefined
let releaseController: AbortController | undefined
async function load() {
  controller?.abort()
  const current = ++generation
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const results = await Promise.allSettled([api.listScenarios(), pluginCodingApi.tasks('', controller.signal)])
    if (current !== generation) return
    if (results[0].status === 'fulfilled') scenarios.value = results[0].value
    if (results[1].status === 'fulfilled') tasks.value = results[1].value
    for (const result of results) if (result.status === 'rejected') throw result.reason
  } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '开发任务加载失败' }
  finally { if (current === generation) loading.value = false }
}
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
function created(value: CodingWorkspace) { taskSettingsDirty.value = false; unsaved.value = false; void router.push({ name: 'plugin-coding-studio', params: { releaseId: value.release_id }, query: { workspace: value.id } }) }
function updateTaskSettings(value: CodingResourceSelection) { taskSettings.value = value; taskSettingsDirty.value = true; unsaved.value = true }
async function saveDraftSettings(value: CodingResourceSelection) { if (await saveSettings(value)) updateTaskSettings(value) }
function openSettings(opener: HTMLElement | null) { settingsOpener.value = opener; settingsOpen.value = true }
function manage(tab: PlatformSettingsTab) { settingsOpener.value = null; settingsOpen.value = false; void router.replace({ query: platformSettingsQuery(route.query, tab) }) }
async function keepDraft() { if (!unsaved.value) return true; try { await ElMessageBox.confirm('当前需求还未提交。离开会丢失输入。', '保留开发需求？', { confirmButtonText: '离开', cancelButtonText: '继续编辑' }); return true } catch { return false } }
onBeforeRouteLeave(keepDraft)
watch(scenarioId, loadReleases, { immediate: true })
watch(() => route.query.platform_settings, (value, previous) => { if (!value && previous) void loadResources() })
function beforeUnload(event: BeforeUnloadEvent) { if (unsaved.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { void load(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { generation++; releaseGeneration++; controller?.abort(); releaseController?.abort(); window.removeEventListener('beforeunload', beforeUnload) })
</script>
<style scoped>
.development-home { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--surface); }
.development-toolbar { display: flex; justify-content: space-between; align-items: center; padding: 0 16px; border-bottom: 1px solid var(--border); gap: 16px; height: 46px; flex: 0 0 auto; font-size: 12px; background: var(--surface-2); }
.development-toolbar > div { display: flex; align-items: center; gap: 10px; }
.development-toolbar span { color: var(--text-2); font-size: 11px; }
.development-toolbar a { display: inline-flex; align-items: center; gap: 6px; text-decoration: none; color: var(--text-2); }
.scene-context { display: grid; flex: 0 0 auto; gap: 12px; padding: 16px; border-bottom: 1px solid var(--border); }
.scene-context label { display: block; font-size: 11px; color: var(--text-2); margin-bottom: 6px; }
.scene-context .el-select { width: 100%; min-width: 0; }
.unbound-task { display: flex; flex-direction: column; flex: 1; min-height: 0; background: var(--surface-2); }
.unbound-transcript { flex: 1; min-height: 0; overflow: auto; padding: 24px 20px; }
.unbound-transcript > .el-icon { color: var(--primary); }
.unbound-transcript h2 { font-size: 18px; font-weight: 600; margin: 12px 0; }
.unbound-context { padding: 14px; margin-top: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-size: 13px; }
.unbound-task p { font-size: 12px; color: var(--text-2); line-height: 1.8; }
.unbound-task a { color: var(--primary); font-size: 12px; }
a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
</style>
