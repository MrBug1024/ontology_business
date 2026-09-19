<template>
  <section class="tools-settings" aria-labelledby="tool-settings-title">
    <header class="tools-heading">
      <div>
        <h2 id="tool-settings-title">工具</h2>
        <p>各类 AI 的工具按职责提供。这里展示业务蒸馏可用的调查工具和资料连接，在该对话的设置中选择。</p>
      </div>
      <el-button plain @click="emit('configure-mcp')">配置 MCP</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon>
      <el-button text type="primary" @click="load">重新加载</el-button>
    </el-alert>
    <el-skeleton v-else-if="loading" :rows="5" animated />
    <template v-else-if="resources">
      <section class="tool-group" aria-labelledby="investigation-tools-title">
        <h3 id="investigation-tools-title">业务蒸馏 · 内置调查工具</h3>
        <el-empty v-if="!resources.investigation_tools.tools.length" description="当前没有可用调查工具" :image-size="64" />
        <dl v-else class="tool-list">
          <div v-for="tool in resources.investigation_tools.tools" :key="tool.key">
            <dt>{{ tool.title }}</dt>
            <dd>{{ tool.description }}</dd>
            <el-tag size="small" :type="tool.always_available ? 'success' : 'info'" effect="plain">
              {{ tool.always_available ? '始终可用' : '按对话选择' }}
            </el-tag>
          </div>
        </dl>
      </section>
      <section class="tool-group" aria-labelledby="mcp-resource-title">
        <h3 id="mcp-resource-title">业务蒸馏 · MCP 资料连接</h3>
        <p class="group-description">AI 可查找并读取已选 MCP 的资料，用于调查、核对和追问。</p>
        <el-empty v-if="!resources.mcps.length" description="尚无可用于业务蒸馏的 MCP 资料连接" :image-size="64" />
        <div v-else class="connector-list">
          <article v-for="connector in resources.mcps" :key="connector.id" class="connector-entry">
            <el-icon aria-hidden="true"><Connection /></el-icon>
            <div><strong>{{ connector.name }}</strong><span>{{ connector.description || '在业务蒸馏对话中选择后使用' }}</span></div>
            <el-tag size="small" effect="plain">{{ connector.mode === 'resources' ? '只读资料' : '资料连接' }}</el-tag>
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
  .tools-heading { flex-direction: column; }
  .tool-list > div { grid-template-columns: 1fr; gap: 6px; }
  .tool-list .el-tag { justify-self: start; }
  .connector-entry { flex-wrap: wrap; }
}
</style>
