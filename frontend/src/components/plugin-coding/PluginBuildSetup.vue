<template>
  <section class="task-start" :class="{ compact }" aria-label="新建插件编码任务">
    <div class="task-transcript" tabindex="0" aria-label="新任务的场景上下文">
      <header><el-icon :size="28" aria-hidden="true"><Cpu /></el-icon><h2>一起构建场景插件</h2><p>描述使用者、业务任务和交付方式。我会读取这个版本的能力契约，规划插件、编写代码并检查和修正。</p></header>
      <div class="context-summary"><b>{{ release.scenario_name || '当前业务场景' }}</b><span>{{ release.name }} · {{ selectedCapabilities.length }} 项能力</span><p>输入、输出、运行确认与幂等要求随能力契约提供。</p></div>
      <div class="host-selector"><label for="plugin-task-host">安装到哪里</label><el-select id="plugin-task-host" v-model="target" aria-label="插件安装宿主" :disabled="building"><el-option value="claude_code" label="Claude Code" /><el-option value="codex" label="OpenAI Codex" /></el-select><p>选择宿主后，AI 将按对应的插件、MCP 与 Skill 规范组织交付。</p></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon /><el-button v-if="error" :disabled="building || loading" @click="load">重新读取能力</el-button>
      <PluginScenarioBlueprint :blueprint="context?.scenario_blueprint" :profile="context?.delivery_profile" :selected-capabilities="selectedCapabilities" :loading="loading" draft-selection />
      <details class="task-options"><summary>能力范围与插件版本</summary><div class="options-body"><el-checkbox v-for="capability in capabilities" :key="`${capability.kind}:${capability.key}`" v-model="selected[`${capability.kind}:${capability.key}`]" :disabled="building">{{ capability.name }}<small v-if="!capability.readiness.ready">有待解决的条件</small></el-checkbox><label for="plugin-task-version">插件版本</label><el-input id="plugin-task-version" v-model="pluginVersion" aria-label="插件版本" maxlength="14" :disabled="building" /><p v-if="!validVersion" role="alert">版本使用三段数字，例如 1.0.1。</p><p>业务案例验收和代码审阅在完成开发后进行。</p></div></details>
      <details v-if="capabilities.length" class="context-disclosure"><summary>查看 AI 将使用的选中能力与契约</summary><PluginCapabilityContext :capabilities="selectedCapabilities" /></details>
    </div>
    <PluginCodingComposer v-model="instruction" input-id="plugin-task-instruction" label="插件编码目标" placeholder="描述接下来要构建的插件：服务谁，完成什么业务，如何使用…" :model-label="modelLabel" :skill-count="resourceSelection.skill_ids.length" :mcp-count="resourceSelection.mcp_ids.length" :busy="building" :can-send="canStart" :hint="loading ? '正在读取完整能力契约与编码资源…' : '先生成候选代码，审阅和业务验收后再定版发布。'" @submit="build" @settings="openSettings" />
    <PluginCodingSettings v-model="settingsOpen" :selection="resourceSelection" :catalog="catalog" :loading="loading" :saving="building" :error="error" :opener="settingsOpener" draft-task @save="saveSettings" @reload="load" @manage="manage" />
  </section>
</template>
<script setup lang="ts">
import { computed, ref, shallowRef, toRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Cpu } from '@element-plus/icons-vue'
import { usePluginTaskStart } from '@/composables/usePluginTaskStart'
import { platformSettingsQuery, type PlatformSettingsTab } from '@/utils/platformSettings'
import PluginCapabilityContext from './PluginCapabilityContext.vue'
import PluginScenarioBlueprint from './PluginScenarioBlueprint.vue'
import PluginCodingComposer from './PluginCodingComposer.vue'
import PluginCodingSettings from './PluginCodingSettings.vue'
import type { CodingResourceSelection, CodingWorkspace } from '@/types/pluginCoding'
import type { ScenarioRelease } from '@/types/scenarioRelease'

const props = defineProps<{ release: ScenarioRelease; initialInstruction?: string; initialSettings?: CodingResourceSelection; compact?: boolean }>()
const emit = defineEmits<{ created: [workspace: CodingWorkspace]; unsaved: [value: boolean]; instruction: [value: string]; settings: [value: CodingResourceSelection] }>()
const { context, capabilities, catalog, resourceSelection, selected, instruction, pluginVersion, target, loading, building, error, validVersion, canStart, load, start } = usePluginTaskStart(toRef(props, 'release'), props.initialSettings)
const route = useRoute()
const router = useRouter()
const settingsOpen = ref(false)
const settingsOpener = shallowRef<HTMLElement | null>(null)
const settingsChanged = ref(false)
instruction.value = props.initialInstruction || ''
watch(instruction, value => emit('instruction', value))
const modelLabel = computed(() => catalog.value?.models.find(model => model.id === resourceSelection.value.llm_config_id)?.name || '')
const selectedCapabilities = computed(() => capabilities.value.filter(item => selected[`${item.kind}:${item.key}`]))
watch([instruction, pluginVersion, target, settingsChanged], () => emit('unsaved', Boolean(instruction.value.trim() || pluginVersion.value !== '1.0.0' || target.value !== 'claude_code' || settingsChanged.value)))
watch(() => route.query.platform_settings, (value, previous) => { if (!value && previous) void load() })
function saveSettings(value: CodingResourceSelection) { resourceSelection.value = value; settingsChanged.value = true; emit('settings', value); settingsOpen.value = false }
function openSettings(opener: HTMLElement | null) { settingsOpener.value = opener; settingsOpen.value = true }
function manage(tab: PlatformSettingsTab) { settingsOpener.value = null; settingsOpen.value = false; void router.replace({ query: platformSettingsQuery(route.query, tab) }) }
async function build() { const value = await start(); if (value) { emit('unsaved', false); emit('created', value) } }
</script>
<style scoped>
.task-start { display: flex; flex-direction: column; min-height: 0; flex: 1; width: 100%; background: var(--surface-2); }
.task-transcript { overflow: auto; flex: 1; min-height: 0; padding: 24px 20px; overscroll-behavior: contain; }
header { margin-bottom: 22px; }
header > .el-icon { color: var(--primary); }
h2 { font-size: 18px; font-weight: 600; margin: 12px 0 8px; text-wrap: balance; }
p { color: var(--text-2); font-size: 13px; line-height: 1.8; }
.context-summary { display: grid; gap: 8px; padding: 14px; margin-bottom: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-size: 13px; }
.context-summary span { font-size: 12px; color: var(--text-2); }
.context-summary p { margin: 0; font-size: 12px; }
.host-selector { margin-bottom: 18px; }
.host-selector label { display: block; margin-bottom: 8px; font-size: 13px; }
.host-selector .el-select { width: min(100%, 280px); }
.host-selector p { margin: 8px 0 0; font-size: 12px; }
.task-options, .context-disclosure { border-top: 1px solid var(--border); padding: 14px 0; font-size: 12px; color: var(--text-2); }
summary { cursor: pointer; line-height: 1.8; }
.options-body { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding-top: 16px; }
.options-body .el-input { max-width: 180px; }
.options-body :deep(.el-checkbox) { max-width: 100%; height: auto; min-height: 44px; }
.options-body :deep(.el-checkbox__label) { white-space: normal; line-height: 1.65; }
small { margin-left: 8px; color: var(--warning); }
summary:focus-visible, .task-transcript:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (max-width: 650px) { .task-transcript { padding: 20px 16px; } }
</style>
