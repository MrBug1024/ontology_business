<template>
  <el-drawer v-model="visible" title="审阅定版" size="min(460px, 100vw)" :close-on-click-modal="!exporting" :close-on-press-escape="!exporting" :show-close="!exporting">
    <div class="delivery-intro"><el-icon :size="28" aria-hidden="true"><Box /></el-icon><h2>完成开发，固定这个版本</h2><p>审阅代码、差异和使用步骤后保存不可变版本。发布中心将从已定版插件中选择安装包或市场发布材料。</p></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-if="dirty" title="还有未保存的文件或修正意见，请先回工作台处理。" type="warning" :closable="false" />
    <el-form label-position="top"><el-form-item label="插件版本"><el-input v-model="version" aria-label="插件交付版本" maxlength="14" :disabled="busy || exporting" /><p v-if="versionDirty && !validVersion" class="version-error">使用三段数字，例如 1.0.1，不含前导零。</p><el-button v-if="versionDirty" size="small" :disabled="!validVersion || dirty || busy || exporting" @click="emit('version', version)">保存新版本</el-button></el-form-item></el-form>
    <div class="review-scope"><span>{{ workspace.files.filter(file => file.editable).length }} 个定制文件</span><span>{{ workspace.validation.length ? '校验尚有缺口' : '结构校验通过' }}</span></div>
    <PluginBusinessAcceptance v-if="visible && workspace.business_acceptance_required" :workspace="workspace" :disabled="exporting || busy" @change="acceptance = $event" />
    <el-checkbox v-model="reviewed" :disabled="dirty || versionDirty || workspace.phase === 'generating'">我已审阅当前代码、差异和业务场景使用步骤</el-checkbox>
    <div class="delivery-options"><el-button type="primary" :disabled="!canExport" :loading="exporting" @click="review">完成审阅并定版</el-button><RouterLink v-if="completed" :to="{ name: 'capability-access', query: { scenario_id: completed.scenario_id, release_id: completed.release_id, artifact: completed.id } }">到发布中心选择此版本<el-icon aria-hidden="true"><ArrowRight /></el-icon></RouterLink></div>
    <p v-if="exporting" role="status">正在校验并保存审阅版本…</p><p v-if="completed" role="status">v{{ completed.plugin_version }} 已定版，可在发布中心交付。后续修改需要使用新版本。</p>
  </el-drawer>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, Box } from '@element-plus/icons-vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import PluginBusinessAcceptance from './PluginBusinessAcceptance.vue'
import type { ScenarioPackageBuild } from '@/types/scenarioPackage'
import type { CodingWorkspace } from '@/types/pluginCoding'
import type { PluginArtifact } from '@/types/pluginArtifact'
const props = defineProps<{ workspace: CodingWorkspace; dirty: boolean; busy: boolean }>()
const emit = defineEmits<{ version: [version: string]; refresh: [] }>()
const visible = ref(false)
const reviewed = ref(false)
const version = ref(props.workspace.plugin_version)
const exporting = ref(false)
const error = ref('')
const completed = ref<PluginArtifact | null>(null)
const acceptance = ref<ScenarioPackageBuild | null>(null)
const versionDirty = computed(() => version.value !== props.workspace.plugin_version)
const validVersion = computed(() => /^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$/.test(version.value))
const canExport = computed(() => reviewed.value && (!props.workspace.business_acceptance_required || acceptance.value !== null) && !props.dirty && !versionDirty.value && !props.busy && !exporting.value && props.workspace.phase !== 'generating' && props.workspace.run_status !== 'failed' && !props.workspace.validation.length)
let controller: AbortController | undefined
let disposed = false
watch(() => props.workspace.plugin_version, value => { version.value = value })
watch(() => [props.workspace.revision, props.dirty, version.value], () => { reviewed.value = false })
async function review() {
  if (!canExport.value) return
  const value = props.workspace
  exporting.value = true
  error.value = ''
  completed.value = null
  controller = new AbortController()
  try {
    const artifact = await pluginCodingApi.review(value.id, { expected_revision: value.revision, files_hash: value.files_hash, confirmed_code_review: true, ...(value.business_acceptance_required && acceptance.value ? { acceptance: acceptance.value } : {}) }, controller.signal)
    if (disposed) return
    completed.value = artifact
    emit('refresh')
  } catch (caught: unknown) { if (!disposed) error.value = caught instanceof Error ? caught.message : '定版失败，文件已保留，请刷新后重新审阅' }
  finally { if (!disposed) exporting.value = false }
}
onBeforeUnmount(() => { disposed = true; controller?.abort() })
defineExpose({ open: () => { visible.value = true } })
</script>
<style scoped>
.delivery-intro { margin-bottom: 28px; }
h2 { font-size: 20px; font-weight: 600; margin: 14px 0 8px; }
p { line-height: 1.8; color: var(--text-2); font-size: 13px; }
.el-alert { margin-bottom: 16px; }
.review-scope { display: flex; justify-content: space-between; border-block: 1px solid var(--border); padding: 14px 0; margin-bottom: 22px; font-size: 12px; }
.delivery-options { display: flex; flex-direction: column; gap: 12px; margin: 28px 0; }
.delivery-options a { display: inline-flex; align-items: center; justify-content: center; gap: 8px; color: var(--primary); font-size: 13px; text-decoration: none; }
.delivery-options :deep(.el-button) { margin: 0; }
.delivery-options button { display: flex; gap: 12px; align-items: center; padding: 18px 14px; border: 1px solid var(--border); border-radius: 10px; text-align: left; background: var(--surface); color: var(--text); cursor: pointer; }
.delivery-options button:disabled { opacity: .5; cursor: not-allowed; }
.delivery-options button:not(:disabled):hover { border-color: var(--primary); background: var(--primary-soft); }
.delivery-options span { flex: 1; }
b, small { display: block; }
b { font-size: 14px; font-weight: 600; }
small { font-size: 12px; margin-top: 5px; color: var(--text-2); }
.version-error { color: var(--danger); }
:deep(.el-checkbox) { height: auto; align-items: flex-start; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
:deep(.el-checkbox__input) { margin-top: 5px; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
</style>
