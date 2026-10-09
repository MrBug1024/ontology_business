<template>
  <div class="plugin-studio">
    <main class="studio-main"><header class="studio-page-header"><RouterLink class="back-link" to="/plugin-studio" aria-label="返回插件开发"><el-icon aria-hidden="true"><Box /></el-icon>插件开发</RouterLink><div class="context-name"><h1>{{ release?.scenario_name || '插件工作区' }}</h1><p>{{ release?.name || '正在读取场景能力…' }}</p></div><el-select :model-value="workspaceId" aria-label="选择编码会话" class="session-picker" @change="selectWorkspace"><el-option label="新建编码任务" value="" /><el-option v-for="(item, i) in recent" :key="item.id" :value="item.id" :label="`项目 ${i + 1} · v${item.plugin_version}`" /></el-select><RouterLink class="publish-link" :to="{ name: 'capability-access', query: { scenario_id: release?.scenario_id, release_id: release?.id } }">发布中心<el-icon aria-hidden="true"><ArrowRight /></el-icon></RouterLink></header>
      <div v-if="error" class="context-error"><el-alert :title="error" type="error" :closable="false" show-icon /><el-button @click="load">重新加载</el-button></div>
      <PluginCodingWorkbench v-if="workspaceId && release" :key="`${release.id}:${workspaceId}`" :workspace-id="workspaceId" :release-id="release.id" @updated="updateRecent" @unsaved="unsaved = $event" />
      <PluginIdeStart v-else-if="release" :tasks="[]" :loading="loading"><div class="new-context"><b>{{ release.scenario_name }}</b><p>{{ release.name }} · {{ release.enabled ? '已启用' : '已停用' }}</p></div><PluginBuildSetup :key="release.id" :release="release" compact @created="created" @unsaved="unsaved = $event" /></PluginIdeStart>
      <p v-else class="context-empty" role="status">{{ loading ? '正在加载发布…' : '发布不可用，请重试或返回发布列表。' }}</p>
    </main>
  </div>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowRight, Box } from '@element-plus/icons-vue'
import PluginCodingWorkbench from '@/components/PluginCodingWorkbench.vue'
import PluginBuildSetup from '@/components/plugin-coding/PluginBuildSetup.vue'
import PluginIdeStart from '@/components/plugin-coding/PluginIdeStart.vue'
import { usePluginStudioContext } from '@/composables/usePluginStudioContext'
import type { CodingWorkspace } from '@/types/pluginCoding'
const route = useRoute()
const router = useRouter()
const releaseId = computed(() => typeof route.params.releaseId === 'string' ? route.params.releaseId : '')
const workspaceId = computed(() => typeof route.query.workspace === 'string' ? route.query.workspace : '')
const { release, recent, loading, error, load, updateRecent } = usePluginStudioContext(releaseId)
const unsaved = ref(false)
function selectWorkspace(id: string) { if (id !== workspaceId.value) void router.push({ query: { ...route.query, workspace: id || undefined, new: id ? undefined : '1' } }) }
function created(value: CodingWorkspace) { unsaved.value = false; updateRecent(value); selectWorkspace(value.id) }
watch(loading, value => { if (!value && !workspaceId.value && route.query.new !== '1' && recent.value[0]) void router.replace({ query: { ...route.query, workspace: recent.value[0].id } }) })
watch([workspaceId, releaseId], () => { unsaved.value = false })
async function keepDraft() {
  if (!unsaved.value) return true
  try { await ElMessageBox.confirm('还有未提交的修改或输入。离开会丢失这些本地草稿，已保存的会话仍可恢复。', '保留当前工作？', { confirmButtonText: '离开', cancelButtonText: '继续编辑', type: 'warning' }); return true } catch { return false }
}
onBeforeRouteLeave(keepDraft)
onBeforeRouteUpdate((to, from) => to.params.releaseId !== from.params.releaseId || to.query.workspace !== from.query.workspace ? keepDraft() : true)
function beforeUnload(event: BeforeUnloadEvent) { if (unsaved.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
</script>
<style scoped>
.plugin-studio { --studio-muted: var(--text-2); height: 100%; min-height: 0; background: var(--surface); color: var(--text); }
.studio-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; }
.studio-page-header { display: flex; align-items: center; gap: 16px; padding: 8px 16px; border-bottom: 1px solid var(--border); min-height: 54px; flex: 0 0 auto; background: var(--surface-2); }
.context-name { min-width: 0; flex: 1; }
h1 { font-size: 12px; font-weight: 500; margin: 0 0 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.context-name p { margin: 0; color: var(--text-2); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.back-link, .publish-link { display: inline-flex; align-items: center; gap: 6px; color: var(--text-2); font-size: 12px; text-decoration: none; white-space: nowrap; }
.session-picker { width: 180px; }
.context-error { padding: 12px; }
.context-empty { padding: 24px; color: var(--text-2); }
.new-context { padding: 16px; border-bottom: 1px solid var(--border); font-size: 12px; }
.new-context p { color: var(--text-2); font-size: 11px; }
.studio-main > .coding-workbench { flex: 1; height: auto; }
a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
@media (max-width: 800px) { .studio-page-header { padding: 8px 12px; gap: 10px; flex-wrap: wrap; } .context-name { flex-basis: calc(100% - 120px); } .session-picker { width: 160px; flex: 1; } }
</style>
