<template>
  <el-popover v-model:visible="open" placement="bottom-end" :width="360" trigger="click" @show="load" @hide="cancelLoad">
    <template #reference><el-button text circle :disabled="disabled" aria-label="设置智能业务顾问" title="设置智能业务顾问"><el-icon aria-hidden="true"><Setting /></el-icon></el-button></template>
    <section class="modeling-advisor-settings" aria-label="智能业务顾问配置">
      <header><strong>智能业务顾问</strong><el-button text :loading="loading" @click="load">刷新配置</el-button></header>
      <p>为当前场景选择建模使用的模型、方法和工具契约。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <el-alert v-if="unavailable" title="部分已选配置已停用或无权使用，请重新选择。原选择已保留。" type="warning" :closable="false" />
      <label :for="`${settingsId}-model`">AI 模型</label>
      <el-select :id="`${settingsId}-model`" v-model="selection.llm_config_id" clearable :disabled="loading || disabled" placeholder="使用平台默认可用模型">
        <el-option v-for="item in options.models" :key="item.id" :value="item.id" :label="`${item.name} · ${item.description}`" />
      </el-select>
      <p v-if="loaded && !options.models.length">暂无可用模型，可在平台设置中配置。</p>
      <label :for="`${settingsId}-skills`">建模工具与技能 · 方法指导</label>
      <el-select :id="`${settingsId}-skills`" v-model="selection.skill_ids" multiple :multiple-limit="5" collapse-tags collapse-tags-tooltip :disabled="loading || disabled" placeholder="选择已配置的受信技能（最多 5 项）">
        <el-option v-for="item in options.skills" :key="item.id" :value="item.id" :label="item.name" :title="item.description" />
      </el-select>
      <label :for="`${settingsId}-mcp`">MCP 工具契约 · 仅查看目录</label>
      <el-select :id="`${settingsId}-mcp`" v-model="selection.mcp_ids" multiple :multiple-limit="5" collapse-tags collapse-tags-tooltip :disabled="loading || disabled" placeholder="选择已配置的 MCP（最多 5 项）">
        <el-option v-for="item in options.mcps" :key="item.id" :value="item.id" :label="item.name" />
      </el-select>
      <p>顾问阅读技能的方法说明与 MCP 工具目录，辅助构建场景模型。此处选择不会执行技能脚本或 MCP 业务工具。</p>
      <el-button text type="primary" @click="openPlatformSettings">打开平台设置</el-button>
    </section>
  </el-popover>
</template>
<script setup lang="ts">
import { ref, toRef, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Setting } from '@element-plus/icons-vue'
import { useModelingAdvisorResources } from '@/composables/useModelingAdvisorResources'
import type { ModelingAdvisorResourceSelection } from '@/types/assistantResources'
const props = defineProps<{ scopeKey: string; disabled: boolean }>()
const selection = defineModel<ModelingAdvisorResourceSelection>({ required: true })
const route = useRoute(), router = useRouter(), open = ref(false)
const settingsId = `modeling-advisor-${useId()}`
const { options, loading, loaded, error, unavailable, load, cancelLoad } = useModelingAdvisorResources(toRef(props, 'scopeKey'), selection)
watch(() => props.scopeKey, () => { open.value = false })
function openPlatformSettings() {
  open.value = false
  void router.push({ path: route.path, query: { ...route.query, platform_settings: 'llm' }, hash: route.hash })
}
</script>
<style scoped>
.modeling-advisor-settings { display: grid; gap: 8px; max-height: min(620px, 70vh); overflow-y: auto; padding: 2px; color: var(--text); }
.modeling-advisor-settings header { display: flex; align-items: center; justify-content: space-between; }
.modeling-advisor-settings p { margin: 0; font-size: 12px; line-height: 1.65; color: var(--text-2); }
.modeling-advisor-settings label { margin-top: 6px; font-size: 13px; font-weight: 600; }
.modeling-advisor-settings :deep(.el-select) { width: 100%; }
.modeling-advisor-settings :deep(:focus-visible) { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
@media (max-width: 480px) { .modeling-advisor-settings { max-width: calc(100vw - 52px); } }
</style>
