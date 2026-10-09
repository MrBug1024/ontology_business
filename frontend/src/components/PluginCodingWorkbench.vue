<template>
  <section class="coding-workbench" aria-label="插件 AI 编码工作台">
    <header class="workspace-toolbar"><div><span class="phase-dot" :class="workspace?.phase" aria-hidden="true" /><span role="status">{{ phaseLabel }}</span><span v-if="workspace && runningMode === 'discuss'" class="source-state">{{ sourcePhaseLabel }}</span><span v-if="workspace" class="version">v{{ workspace.plugin_version }}</span></div><div><el-button text :loading="loading" aria-label="刷新编码进度" @click="load"><el-icon aria-hidden="true"><Refresh /></el-icon></el-button><el-button :disabled="!workspace || busy" @click="delivery?.open()"><el-icon aria-hidden="true"><Box /></el-icon>审阅定版</el-button></div></header>
    <el-alert v-if="error || message" class="workspace-error" :title="error || message" type="error" :closable="false" show-icon />
    <p v-if="!workspace" class="workspace-empty">{{ loading ? '正在恢复编码会话…' : '读取失败，请点击刷新重试。' }}</p>
    <template v-else>
      <nav class="mobile-panels" aria-label="工作区面板"><button type="button" :aria-pressed="mobilePanel === 'explorer'" @click="mobilePanel = 'explorer'">文件</button><button type="button" :aria-pressed="mobilePanel === 'inspector'" @click="mobilePanel = 'inspector'">代码</button><button type="button" :aria-pressed="mobilePanel === 'conversation'" @click="mobilePanel = 'conversation'">AI 助手</button></nav>
      <div class="workspace-panels" :class="`show-${mobilePanel}`">
        <PluginFileExplorer :files="workspace.files" :selected-path="selectedPath" :busy="busy" @select="inspect" />
        <PluginCodingInspector ref="inspector" :workspace="workspace" :selected-path="selectedPath" :selected-file="selectedFile" :draft="draft" :dirty="dirty" :basis-changed="basisChanged" :busy="busy" @select="openFile" @update:draft="draft = $event" @save="submit('save')" @discard="discardDraft" @rebase="baseHash = workspace.files_hash" />
        <div class="conversation-pane"><header class="ai-heading"><el-icon aria-hidden="true"><Cpu /></el-icon><b>AI 编码助手</b><small role="status">{{ activeRun ? runningMode === 'discuss' ? '正在分析项目' : '正在编写代码' : interactionMode === 'discuss' ? '讨论方案' : '编码与修正' }}</small><button type="button" class="heading-settings" aria-label="打开编码 AI 设置" @click="openSettingsFromEvent"><el-icon aria-hidden="true"><Setting /></el-icon></button></header><PluginCodingConversation :workspace="workspace" @inspect="inspect" @checks="showChecks" @changes="showChanges" @retry="retryTurn" />
          <PluginCodingComposer ref="composer" v-model="feedback" v-model:mode="interactionMode" input-id="coding-instruction" label="插件编码修正意见" :placeholder="interactionMode === 'discuss' ? '讨论方案、解释文件或分析场景能力…' : '描述接下来的修改，或纠正当前方向…'" :model-label="modelLabel" :skill-count="resourceSelection.skill_ids.length" :mcp-count="resourceSelection.mcp_ids.length" :busy="busy" :generating="activeRun" :can-send="!(dirty && basisChanged) && !(interactionMode === 'discuss' && dirty)" :hint="composerHint" allow-discussion @submit="submit(interactionMode)" @stop="stopCoding" @settings="openSettings" />
        </div>
      </div>
      <PluginCodingDelivery ref="delivery" :workspace="workspace" :dirty="unsaved" :busy="busy" @version="submit('save', $event)" @refresh="load" />
      <PluginCodingSettings v-model="settingsOpen" :selection="resourceSelection" :catalog="resourceCatalog" :loading="resourcesLoading" :saving="busy || settingsSaving" :error="settingsError || error" :active-run="activeRun" :opener="settingsOpener" @save="saveSettings" @reload="loadResources" @stop="stopCoding" @manage="manage" />
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, toRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Box, Cpu, Refresh, Setting } from '@element-plus/icons-vue'
import { usePluginCodingWorkspace } from '@/composables/usePluginCodingWorkspace'
import { usePluginCodingEditor } from '@/composables/usePluginCodingEditor'
import { emptyCodingSelection, usePluginCodingSettings } from '@/composables/usePluginCodingSettings'
import { createClientRequestId } from '@/utils/clientRequestId'
import { platformSettingsQuery, type PlatformSettingsTab } from '@/utils/platformSettings'
import PluginCodingConversation from '@/components/plugin-coding/PluginCodingConversation.vue'
import PluginCodingInspector from '@/components/plugin-coding/PluginCodingInspector.vue'
import PluginCodingDelivery from '@/components/plugin-coding/PluginCodingDelivery.vue'
import PluginFileExplorer from '@/components/plugin-coding/PluginFileExplorer.vue'
import PluginCodingComposer from '@/components/plugin-coding/PluginCodingComposer.vue'
import PluginCodingSettings from '@/components/plugin-coding/PluginCodingSettings.vue'
import type { CodingResourceSelection, CodingWorkspace } from '@/types/pluginCoding'
const props = defineProps<{ workspaceId: string; releaseId: string }>()
const emit = defineEmits<{ updated: [workspace: CodingWorkspace]; unsaved: [value: boolean] }>()
const { workspace, loading, busy, error, load, revise, settings } = usePluginCodingWorkspace(toRef(props, 'workspaceId'), toRef(props, 'releaseId'))
const { selectedPath, selectedFile, draft, feedback, dirty, basisChanged, baseHash, message, selectFile, discardDraft, submit, stopCoding, restoreInstruction } = usePluginCodingEditor(workspace, revise)
const composer = ref<InstanceType<typeof PluginCodingComposer> | null>(null)
const inspector = ref<InstanceType<typeof PluginCodingInspector> | null>(null)
const delivery = ref<InstanceType<typeof PluginCodingDelivery> | null>(null)
const mobilePanel = ref('inspector')
const interactionMode = ref<'generate' | 'discuss'>('generate')
const resourceSelection = ref<CodingResourceSelection>(emptyCodingSelection())
const settingsOpener = shallowRef<HTMLElement | null>(null)
const { catalog: resourceCatalog, open: settingsOpen, loading: resourcesLoading, saving: settingsSaving, error: settingsError, modelLabel, load: loadResources, save: saveSettings } = usePluginCodingSettings(ref(''), resourceSelection, async value => {
  const current = workspace.value
  if (!current) return false
  return settings({ expected_revision: current.revision, request_id: createClientRequestId(), ...value })
})
const route = useRoute()
const router = useRouter()
const activeRun = computed(() => Boolean(workspace.value?.run_status && ['waiting_upload', 'queued', 'running'].includes(workspace.value.run_status)))
const runningMode = computed(() => { const turns = workspace.value?.turns || []; return turns[turns.length - 1]?.mode || 'generate' })
const composerHint = computed(() => interactionMode.value === 'discuss' ? dirty.value ? '讨论基于已保存文件，请先保存当前代码；草稿会保留。' : activeRun.value ? '发送讨论将停止当前轮次，基于已保存文件分析方案。' : '讨论基于当前已保存文件，分析方案与能力契约。' : activeRun.value ? '发送修正将停止旧轮次，并从当前文件开始新轮次。' : dirty.value ? '发送时将连同未保存文件一起提交。' : '候选代码由人工审阅和业务验收后发布。')
const unsaved = computed(() => dirty.value || Boolean(feedback.value.trim()))
const labels = { draft: '修订待审阅', generating: 'AI 编码中', ready_for_review: '等待审阅', validation_failed: '需要修正', released: '已定版，可到发布中心交付' }
const sourcePhaseLabel = computed(() => workspace.value ? ({ draft: '候选草稿', generating: '候选生成中', ready_for_review: '候选待审阅', validation_failed: '候选待修正', released: '插件已定版' })[workspace.value.source_phase || workspace.value.phase] : '')
const phaseLabel = computed(() => {
  const value = workspace.value
  if (!value) return '恢复会话'
  if (runningMode.value === 'discuss') {
    if (value.run_status === 'failed') return '讨论未完成，可重试'
    if (value.run_status === 'cancelled') return '讨论已停止，可继续'
    if (activeRun.value) return 'AI 讨论中'
  }
  return value.run_status === 'failed' ? '本轮未完成，可继续修正' : labels[value.phase]
})
watch(workspace, value => { if (value) { emit('updated', value); resourceSelection.value = value.resource_selection || emptyCodingSelection() } })
watch(() => route.query.platform_settings, (value, previous) => { if (!value && previous) void loadResources() })
watch(unsaved, value => emit('unsaved', value))
function openFile(path: string) { if (!busy.value) selectFile(path) }
function inspect(path: string) { if (busy.value) return; selectFile(path); inspector.value?.inspect(); mobilePanel.value = 'inspector' }
function showChecks() { inspector.value?.checks(); mobilePanel.value = 'inspector' }
function showChanges() { inspector.value?.changes(); mobilePanel.value = 'inspector' }
async function retryTurn(instruction: string, mode: 'generate' | 'discuss') {
  if (busy.value || !restoreInstruction(instruction)) return
  interactionMode.value = mode
  mobilePanel.value = 'conversation'
  await nextTick()
  composer.value?.focus()
}
function openSettings(opener: HTMLElement | null) { settingsOpener.value = opener; settingsOpen.value = true }
function openSettingsFromEvent(event: MouseEvent) { openSettings(event.currentTarget as HTMLElement | null) }
function manage(tab: PlatformSettingsTab) { settingsOpener.value = null; settingsOpen.value = false; void router.replace({ query: platformSettingsQuery(route.query, tab) }) }
</script>
<style scoped>
.coding-workbench { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--surface); }
.workspace-toolbar { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--border); min-height: 47px; }
.workspace-toolbar > div { display: flex; align-items: center; gap: 8px; }
.workspace-toolbar > div:first-child { flex: 1; min-width: 0; flex-wrap: wrap; }
.workspace-toolbar > div:last-child { flex-shrink: 0; }
.workspace-toolbar .el-button + .el-button { margin-left: 0; }
.workspace-toolbar span { font-size: 12px; }
.version { color: var(--studio-muted); margin-left: 8px; }
.source-state { color: var(--text-2); font-size: 11px !important; }
.phase-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--studio-muted); }
.phase-dot.generating { background: var(--primary); }
.phase-dot.ready_for_review { background: var(--success); }
.phase-dot.validation_failed { background: var(--warning); }
.workspace-panels { display: grid; grid-template-columns: 200px minmax(0, 1fr) minmax(280px, 31%); min-height: 0; flex: 1; }
.conversation-pane { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-left: 1px solid var(--border); background: var(--surface-2); }
.ai-heading { display: flex; align-items: center; gap: 7px; padding: 11px 14px; min-height: 43px; border-bottom: 1px solid var(--border); background: var(--surface); font-size: 12px; }
.ai-heading small { color: var(--text-2); font-size: 11px; }
.heading-settings { margin-left: auto; display: grid; place-items: center; width: 44px; height: 44px; border: 0; border-radius: 7px; color: var(--text-2); background: transparent; }
.heading-settings:hover { color: var(--text); background: var(--surface-3); }
.workspace-error { flex: 0 0 auto; }
.workspace-empty { padding: 32px; color: var(--studio-muted); }
.mobile-panels { display: none; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
button { cursor: pointer; font: inherit; color: inherit; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
@media (min-width: 1450px) { .workspace-panels { grid-template-columns: 230px minmax(0, 1fr) 390px; } }
@media (max-width: 1100px) { .workspace-panels { grid-template-columns: 166px minmax(0, 1fr) 280px; } .ai-heading span { display: none; } }
@media (max-width: 900px) {
  .workspace-toolbar { padding: 10px 14px; }
  .workspace-toolbar .version { display: none; }
  .mobile-panels { display: flex; gap: 8px; padding: 8px 14px; border-bottom: 1px solid var(--border); }
  .mobile-panels button { border: 0; border-radius: 6px; padding: 7px 12px; background: transparent; }
  .mobile-panels button[aria-pressed='true'] { background: var(--surface-3); }
  .workspace-panels { grid-template-columns: minmax(0, 1fr); }
  .workspace-panels > * { display: none; }
  .show-conversation > .conversation-pane, .show-inspector > .inspector, .show-explorer > .explorer { display: flex; }
}
</style>
