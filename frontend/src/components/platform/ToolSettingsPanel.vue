<template>
  <section class="tools-settings" aria-labelledby="tool-settings-title">
    <header class="tools-heading">
      <div>
        <h2 id="tool-settings-title">工具</h2>
        <p>工具是平台统一执行的小颗粒能力：读取授权资料、访问受管系统或输出结构化结果。工具不承载业务方法，也不会因某个场景改写规则。</p>
      </div>
      <el-button plain @click="emit('configure-mcp')">配置 MCP</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon>
      <el-button text type="primary" @click="load">重新加载</el-button>
    </el-alert>
    <el-skeleton v-else-if="loading" :rows="5" animated />
    <template v-else-if="resources">
      <section class="tool-group" aria-labelledby="tool-skill-boundary-title">
        <h3 id="tool-skill-boundary-title">工具 / 技能边界</h3>
        <div class="boundary-grid">
          <article><strong>工具</strong><p>由平台内核注册、校验参数并执行；每次调用有权限、租户、输入边界和审计。</p></article>
          <article><strong>技能</strong><p>受信方法包，只提供调查步骤、验收标准和提示词指导；按对话显式选择，不能扩大工具权限。</p></article>
        </div>
      </section>
      <section class="tool-group" aria-labelledby="mcp-resource-title">
        <h3 id="mcp-resource-title">平台 MCP 能力</h3>
        <p class="group-description">MCP 为 AI 提供受信工具契约和执行能力，不会被当作业务输入资料。Jev 决策能力可在蒸馏与智能顾问中自动协同。</p>
        <el-empty v-if="!resources.mcps.length" description="尚未配置可用的 MCP 能力" :image-size="64" />
        <div v-else class="connector-list">
          <article v-for="connector in resources.mcps" :key="connector.id" class="connector-entry">
            <el-icon aria-hidden="true"><Connection /></el-icon>
            <div><strong>{{ connector.name }}</strong><span>{{ connector.description || '在业务蒸馏对话中选择后使用' }}</span></div>
            <el-tag size="small" effect="plain">{{ connector.name.trim().toLowerCase() === 'jev_decide' ? '自动决策能力' : '受信能力' }}</el-tag>
          </article>
        </div>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Connection } from '@element-plus/icons-vue'
import { distillationConversationApi } from '@/api/distillationConversation'
import type { DistillationResourceOptions } from '@/types/distillationConversation'

const emit = defineEmits<{ 'configure-mcp': [] }>()
const resources = ref<DistillationResourceOptions | null>(null)
const loading = ref(false)
const error = ref('')
let controller: AbortController | null = null

async function load() {
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const result = await distillationConversationApi.resources(request.signal)
    if (controller === request && !request.signal.aborted) resources.value = result
  } catch (cause: unknown) {
    if (controller === request && !request.signal.aborted) error.value = cause instanceof Error ? cause.message : '工具目录加载失败'
  } finally {
    if (controller === request && !request.signal.aborted) loading.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => { controller?.abort(); controller = null })
</script>

<style scoped>
.tools-settings { padding: 24px; }
.tools-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 20px; }
.tools-heading h2 { margin: 0 0 8px; font-size: 20px; color: var(--text); }
.tools-heading p, .group-description { margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.6; }
.tool-group + .tool-group { margin-top: 28px; }
.tool-group h3 { margin: 0 0 12px; color: var(--text); font-size: 15px; }
.boundary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.boundary-grid article { padding: 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.boundary-grid strong { display: block; margin-bottom: 6px; color: var(--text); font-size: 13px; }
.boundary-grid p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.6; }
.tool-list { margin: 0; }
.tool-list > div { display: grid; grid-template-columns: minmax(110px, 1fr) minmax(0, 3fr) auto; align-items: start; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--border); }
.tool-list dt { overflow-wrap: anywhere; color: var(--text); font-size: 13px; }
.tool-list dd { margin: 0; overflow-wrap: anywhere; color: var(--text-2); font-size: 13px; line-height: 1.6; }
.connector-entry { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--border); }
.connector-entry > div { display: grid; flex: 1; min-width: 0; gap: 5px; }
.connector-entry strong { color: var(--text); font-size: 13px; overflow-wrap: anywhere; }
.connector-entry span { color: var(--text-3); font-size: 12px; }
@media (max-width: 720px) {
  .tools-settings { padding: 18px 14px 24px; }
  .boundary-grid { grid-template-columns: 1fr; }
  .tools-heading { flex-direction: column; }
  .tool-list > div { grid-template-columns: 1fr; gap: 6px; }
  .tool-list .el-tag { justify-self: start; }
  .connector-entry { flex-wrap: wrap; }
}
</style>
