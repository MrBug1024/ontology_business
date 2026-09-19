<template>
  <el-popover placement="top-start" :width="380" trigger="click" @show="load">
    <template #reference><el-button text circle :disabled="disabled" aria-label="设置业务蒸馏 AI" title="设置业务蒸馏 AI"><el-icon><Setting /></el-icon></el-button></template>
    <section class="resource-settings" aria-label="业务蒸馏 AI 配置">
      <header><strong>业务蒸馏 AI</strong><el-button text :loading="loading" @click="load">刷新配置</el-button></header>
      <p>选择本次调查使用的模型、方法和资料工具。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <el-alert v-if="unavailable" title="部分已选配置已停用或无权使用，请重新选择后发送。" type="warning" :closable="false" />
      <label for="distillation-resource-llm">AI 模型</label>
      <el-select id="distillation-resource-llm" v-model="selection.llm_config_id" clearable :disabled="loading || disabled" placeholder="自动选择可用模型"><el-option v-for="item in options.models" :key="item.id" :value="item.id" :label="item.name" /></el-select>
      <p v-if="!loading && loaded && !options.models.length">暂无可用模型，请在平台设置中添加支持工具调用的模型。</p>
      <label for="distillation-resource-tools">调查工具</label>
      <el-select id="distillation-resource-tools" :model-value="selection.investigation_tool_keys ?? options.investigation_tools.default_tool_keys" multiple collapse-tags collapse-tags-tooltip :disabled="loading || disabled" placeholder="仅对话澄清与提出建议" @update:model-value="selectTools"><el-option v-for="item in selectableTools" :key="item.key" :value="item.key" :label="item.title" :title="item.description" /></el-select>
      <el-button text size="small" :disabled="disabled" @click="selection.investigation_tool_keys = null">恢复默认调查工具</el-button>
      <label for="distillation-resource-skills">技能 · 方法指导</label>
      <el-select id="distillation-resource-skills" v-model="selection.skill_ids" multiple collapse-tags collapse-tags-tooltip :disabled="loading || disabled" placeholder="选择已配置的受信技能"><el-option v-for="item in options.skills" :key="item.id" :value="item.id" :label="item.name" :title="item.description" /></el-select>
      <label for="distillation-resource-mcps">MCP · 只读资料</label>
      <el-select id="distillation-resource-mcps" v-model="selection.mcp_ids" multiple collapse-tags collapse-tags-tooltip :disabled="loading || disabled" placeholder="选择已配置的资料连接"><el-option v-for="item in options.mcps" :key="item.id" :value="item.id" :label="item.name" /></el-select>
      <p>AI 可阅读所选技能的方法说明，并查找、读取 MCP 提供的资料。发现歧义时会向你提问；结论由你决定是否采用。</p>
      <el-button text type="primary" @click="openSettings">打开平台设置</el-button>
    </section>
  </el-popover>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Setting } from '@element-plus/icons-vue'
import { distillationConversationApi } from '@/api/distillationConversation'
import type { DistillationResourceOptions, DistillationResourceSelection, InvestigationToolKey } from '@/types/distillationConversation'
const props = defineProps<{ disabled: boolean; scopeKey: string }>()
const selection = defineModel<DistillationResourceSelection>({ required: true })
const route = useRoute(), router = useRouter()
const emptyOptions = (): DistillationResourceOptions => ({ models: [], skills: [], mcps: [], investigation_tools: { default_tool_keys: [], always_available_tool_keys: [], tools: [] } })
const options = ref(emptyOptions()), loading = ref(false), loaded = ref(false), error = ref('')
let controller: AbortController | undefined
const selectableTools = computed(() => options.value.investigation_tools.tools.filter(item => item.selectable))
const unavailable = computed(() => loaded.value && (
  !!selection.value.llm_config_id && !options.value.models.some(item => item.id === selection.value.llm_config_id)
  || (selection.value.skill_ids || []).some(id => !options.value.skills.some(item => item.id === id))
  || (selection.value.mcp_ids || []).some(id => !options.value.mcps.some(item => item.id === id))
))
function selectTools(value: InvestigationToolKey[]) { selection.value.investigation_tool_keys = [...value] }
function openSettings() { void router.push({ path: route.path, query: { ...route.query, platform_settings: 'llm' }, hash: route.hash }) }
async function load() {
  controller?.abort()
  const current = new AbortController()
  controller = current
  loading.value = true; error.value = ''
  try {
    const result = await distillationConversationApi.resources(current.signal)
    if (!current.signal.aborted) { options.value = result; loaded.value = true }
  } catch (caught: unknown) {
    if (!current.signal.aborted) error.value = caught instanceof Error ? caught.message : '配置加载失败，请重试。'
  } finally { if (controller === current) { loading.value = false; controller = undefined } }
}
watch(() => props.scopeKey, () => {
  controller?.abort(); controller = undefined
  options.value = emptyOptions(); loaded.value = false; loading.value = false; error.value = ''
  selection.value = { llm_config_id: null, skill_ids: [], mcp_ids: [], investigation_tool_keys: null }
})
onBeforeUnmount(() => controller?.abort())
</script>

<style scoped>
.resource-settings { display: grid; gap: 8px; max-height: min(620px, 70vh); overflow-y: auto; padding: 2px; color: var(--text); }
.resource-settings header { display: flex; align-items: center; justify-content: space-between; }
.resource-settings p { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-2); }
.resource-settings label { margin-top: 5px; font-size: 13px; font-weight: 600; }
.resource-settings :deep(.el-select) { width: 100%; }
.resource-settings :deep(:focus-visible) { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
@media (max-width: 480px) { .resource-settings { max-width: calc(100vw - 52px); } }
</style>
