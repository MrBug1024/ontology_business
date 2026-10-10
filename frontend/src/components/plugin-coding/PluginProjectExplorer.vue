<template>
  <aside class="explorer" aria-label="插件资源管理器">
    <header class="explorer-head"><el-icon aria-hidden="true"><FolderOpened /></el-icon><b>资源管理器</b><span v-if="scenarios.length">{{ scenarios.length }} 个场景</span></header>
    <nav class="project-list" aria-label="业务场景插件项目">
      <p v-if="!scenarios.length" class="list-hint">{{ loading ? '读取业务场景…' : '尚无业务场景' }}<small v-if="!loading">先在场景能力中建立业务场景，再回到这里开发插件。</small></p>
      <div v-for="scenario in scenarios" :key="scenario.id" class="scenario-node">
        <div class="scenario-row" :class="{ selected: selectedScenarioId === scenario.id, open: explorer.isScenarioExpanded(scenario.id) }">
          <button type="button" class="row-main" :aria-expanded="explorer.isScenarioExpanded(scenario.id)" :aria-controls="`scenario-${scenario.id}`" @click="choose(scenario)">
            <el-icon aria-hidden="true" class="chev"><ArrowRight /></el-icon>
            <el-icon aria-hidden="true" class="folder"><Folder /></el-icon>
            <span class="scenario-name">{{ scenario.name }}</span>
            <i v-if="explorer.isScenarioLoading(scenario.id)" class="loading-dot" aria-label="正在加载插件源码" />
            <span v-else-if="projectCount(scenario.id)" class="count">{{ fileCount(scenario.id) }}</span>
          </button>
        </div>
        <!-- Expanding a scenario shows its plugin source files directly; no
             intermediate "plugin" node wraps the tree. Host labels appear only
             when a scenario actually maintains more than one host project. -->
        <div v-if="explorer.isScenarioExpanded(scenario.id)" :id="`scenario-${scenario.id}`" class="scenario-body">
          <p v-if="explorer.isScenarioLoading(scenario.id)" class="branch-hint">正在读取插件源码…</p>
          <template v-else-if="explorer.scenarioErrors[scenario.id]">
            <p class="branch-hint error" role="alert">{{ explorer.scenarioErrors[scenario.id] }}</p>
            <button type="button" class="retry" @click="explorer.reloadScenario(scenario.id)">重新读取</button>
          </template>
          <p v-else-if="!explorer.projectsFor(scenario.id).length" class="branch-hint">此场景暂无插件源码<small>在右侧点击「新建任务」开始编码。</small></p>
          <template v-else>
            <template v-for="project in explorer.projectsFor(scenario.id)" :key="project.id">
              <p v-if="multiHost(scenario.id)" class="host-label">{{ hostLabel(project.host) }}<i v-if="project.phase === 'generating'" class="loading-dot" aria-label="AI 编码中" /><small v-if="project.plugin_version">最近定版 v{{ project.plugin_version }}</small></p>
              <p v-if="explorer.isLoading(project.id)" class="branch-hint">正在读取源码…</p>
              <template v-else-if="explorer.errors[project.id]">
                <p class="branch-hint error" role="alert">{{ explorer.errors[project.id] }}</p>
                <button type="button" class="retry" @click="explorer.reloadProject(project.id)">重新读取</button>
              </template>
              <template v-else-if="explorer.projectOf(project.id)">
                <button v-for="entry in entriesOf(project.id)" :key="`${project.id}:${entry.node.path}`" type="button" class="file-row" :class="{ selected: explorer.selection?.projectId === project.id && explorer.selection?.path === entry.node.path }" :style="{ paddingLeft: `${10 + entry.depth * 13}px` }" :aria-expanded="entry.node.directory ? !collapsedDirs.has(`${project.id}:${entry.node.path}`) : undefined" @click="openNode(project.id, entry.node)">
                  <el-icon aria-hidden="true"><ArrowRight v-if="entry.node.directory && collapsedDirs.has(`${project.id}:${entry.node.path}`)" /><ArrowDown v-else-if="entry.node.directory" /><Document v-else-if="isEditable(project.id, entry.node.path)" /><Lock v-else /></el-icon>
                  <span>{{ entry.node.name }}</span>
                  <i v-if="isModified(project.id, entry.node.path)" class="modified-dot" aria-label="相对上一版已修改" />
                </button>
              </template>
            </template>
          </template>
        </div>
      </div>
    </nav>
    <footer class="explorer-foot"><span><el-icon aria-hidden="true"><Lock /></el-icon>隔离沙箱</span><p>插件源码保存在平台侧隔离工作区，与平台自身源码和运行环境分离；平台只校验、从不执行插件代码。</p></footer>
  </aside>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowDown, ArrowRight, Document, Folder, FolderOpened, Lock } from '@element-plus/icons-vue'
import { pluginFileTree, type PluginFileNode } from '@/utils/pluginCodeEditor'
import type { PluginProjectExplorer } from '@/composables/usePluginProjectExplorer'
import type { Scenario } from '@/types'

const props = defineProps<{ scenarios: Scenario[]; loading: boolean; explorer: PluginProjectExplorer; selectedScenarioId: string }>()
const emit = defineEmits<{ 'select-scenario': [scenarioId: string] }>()
const collapsedDirs = ref(new Set<string>())
const entriesByProject = computed(() => {
  const result: Record<string, { node: PluginFileNode; depth: number }[]> = {}
  for (const projects of Object.values(props.explorer.projectsByScenario)) {
    for (const project of projects) {
      const files = props.explorer.projectOf(project.id)
      if (!files) continue
      const rows: { node: PluginFileNode; depth: number }[] = []
      const visit = (nodes: PluginFileNode[], depth: number) => {
        for (const node of nodes) {
          rows.push({ node, depth })
          if (node.directory && !collapsedDirs.value.has(`${project.id}:${node.path}`)) visit(node.children, depth + 1)
        }
      }
      visit(pluginFileTree(files.files.map(file => file.path)), 0)
      result[project.id] = rows
    }
  }
  return result
})
function entriesOf(projectId: string) { return entriesByProject.value[projectId] || [] }
function hostLabel(value: string) { return value === 'codex' ? 'OpenAI Codex' : 'Claude Code' }
function multiHost(scenarioId: string) { return props.explorer.projectsFor(scenarioId).length > 1 }
function projectCount(scenarioId: string) { return props.explorer.projectsFor(scenarioId).length }
function fileCount(scenarioId: string) {
  const projects = props.explorer.projectsFor(scenarioId)
  const files = projects.reduce((total, project) => total + (props.explorer.projectOf(project.id)?.files.length || 0), 0)
  return files || projects.length
}
function fileOf(projectId: string, path: string) { return props.explorer.projectOf(projectId)?.files.find(file => file.path === path) }
function isEditable(projectId: string, path: string) { return fileOf(projectId, path)?.editable ?? false }
function isModified(projectId: string, path: string) { const file = fileOf(projectId, path); return Boolean(file?.editable && file.content !== file.previous) }
function openNode(projectId: string, node: PluginFileNode) {
  if (node.directory) {
    const key = `${projectId}:${node.path}`
    const next = new Set(collapsedDirs.value)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    collapsedDirs.value = next
  } else {
    props.explorer.selectFile(projectId, node.path)
  }
}
function choose(scenario: Scenario) {
  if (!props.explorer.isScenarioExpanded(scenario.id)) props.explorer.expandScenario(scenario.id)
  emit('select-scenario', scenario.id)
}
</script>
<style scoped>
.explorer { display: flex; flex-direction: column; min-height: 0; min-width: 0; background: var(--surface-2); }
.explorer-head { display: flex; align-items: center; gap: 7px; min-height: 42px; padding: 0 12px; font-size: 11px; letter-spacing: .04em; border-bottom: 1px solid var(--border); flex: 0 0 auto; }
.explorer-head b { font-weight: 600; }
.explorer-head span { margin-left: auto; color: var(--text-2); font-size: 10px; letter-spacing: 0; }
.project-list { flex: 1; min-height: 0; overflow: auto; padding-block: 6px; }
.list-hint, .branch-hint { padding: 12px 12px; font-size: 12px; color: var(--text-2); }
.list-hint small, .branch-hint small { display: block; margin-top: 6px; font-size: 11px; line-height: 1.7; color: var(--text-3); }
.branch-hint { padding: 8px 10px 8px 24px; font-size: 11px; }
.branch-hint.error { color: var(--danger); overflow-wrap: anywhere; }
.retry { margin: 0 10px 8px 24px; padding: 4px 10px; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface); color: var(--text-2); font-size: 11px; cursor: pointer; }
.retry:hover { color: var(--text); border-color: var(--primary); }
.row-main { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; min-height: 30px; padding: 4px 8px 4px 2px; border: 0; background: transparent; color: var(--text); text-align: left; font-size: 12px; cursor: pointer; }
.row-main:hover { background: var(--surface-3); }
.row-main .chev { flex: 0 0 14px; font-size: 12px; color: var(--text-2); transition: transform var(--dur) var(--ease); }
.scenario-row { display: flex; align-items: stretch; margin: 2px 0; }
.scenario-row.selected > .row-main { background: var(--primary-soft); color: var(--primary); }
.scenario-row.selected > .row-main .chev, .scenario-row.selected > .row-main .folder { color: var(--primary); }
.scenario-row.open > .row-main .chev { transform: rotate(90deg); }
.scenario-row > .row-main { padding-left: 8px; font-weight: 600; }
.scenario-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.count { margin-left: auto; flex-shrink: 0; min-width: 18px; text-align: center; padding: 1px 5px; border-radius: 999px; background: var(--surface-3); color: var(--text-2); font-size: 10px; font-weight: 500; }
.loading-dot { margin-left: auto; flex: 0 0 5px; height: 5px; background: var(--primary); border-radius: 50%; animation: pulse 1.2s var(--ease) infinite; }
.scenario-body { border-left: 1px solid var(--border); margin-left: 13px; }
.host-label { display: flex; align-items: center; gap: 6px; margin: 4px 0 2px; padding: 2px 10px 2px 12px; font-size: 10px; font-weight: 600; color: var(--text-2); }
.host-label small { margin-left: auto; font-weight: 400; }
.file-row { display: flex; align-items: center; gap: 6px; width: 100%; min-height: 29px; padding: 4px 10px 4px 12px; border: 0; color: var(--text); background: transparent; text-align: left; font: 12px ui-monospace, Consolas, monospace; cursor: pointer; }
.file-row:hover { background: var(--surface-3); }
.file-row.selected { background: var(--primary-soft); color: var(--primary); }
.file-row .el-icon { flex: 0 0 14px; font-size: 12px; color: var(--text-2); }
.file-row.selected .el-icon { color: var(--primary); }
.file-row span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.modified-dot { margin-left: auto; flex: 0 0 5px; height: 5px; background: var(--warning); border-radius: 50%; }
.explorer-foot { flex: 0 0 auto; padding: 10px 12px; border-top: 1px solid var(--border); font-size: 10px; color: var(--text-2); }
.explorer-foot span { display: flex; align-items: center; gap: 5px; color: var(--text); font-size: 11px; }
.explorer-foot p { margin: 5px 0 0; line-height: 1.7; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@keyframes pulse { 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .row-main .chev { transition: none; } .loading-dot { animation: none; } }
</style>
