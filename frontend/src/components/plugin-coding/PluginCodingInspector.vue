<template>
  <section class="inspector" aria-label="插件主代码编辑区">
    <nav class="editor-file-tabs" aria-label="打开的插件文件"><div v-for="file in openedFiles" :key="file.path" :class="['file-tab', { active: file.path === selectedPath }]">
      <button type="button" :disabled="busy" :aria-pressed="file.path === selectedPath" @click="emit('select', file.path)"><el-icon aria-hidden="true"><Document v-if="file.editable" /><Lock v-else /></el-icon><span>{{ file.path.split('/').slice(-1)[0] }}</span><i v-if="file.path === selectedPath && dirty" aria-label="未保存" /></button><button v-if="openedFiles.length > 1" type="button" class="close-tab" :disabled="busy || (file.path === selectedPath && dirty)" :aria-label="`关闭 ${file.path}`" @click="close(file.path)"><el-icon aria-hidden="true"><Close /></el-icon></button>
    </div></nav>
    <header class="file-heading"><code>{{ selectedPath }}</code><span>{{ selectedFile?.editable ? dirty ? '未保存' : '可编辑' : '只读' }}</span></header>
    <nav class="inspector-tabs" aria-label="代码工作区视图"><button v-for="item in tabs" :key="item.key" type="button" :aria-pressed="mode === item.key" :class="{ active: mode === item.key }" @click="mode = item.key">{{ item.label }}<span v-if="item.key === 'checks' && workspace.validation.length">{{ workspace.validation.length }}</span></button></nav>
    <div v-if="mode === 'checks'" class="checks-panel"><el-icon :size="26" aria-hidden="true"><Warning v-if="workspace.validation.length || workspace.run_status === 'failed'" /><CircleCheck v-else /></el-icon><h3>{{ workspace.validation.length ? '需要修正' : workspace.phase === 'generating' ? '编码进行中' : workspace.run_status === 'failed' ? '本轮编码未完成' : '结构校验通过' }}</h3><p>依据真实输入契约检查文件结构与示例。代码审阅和业务案例验收仍需人工完成。</p><ol v-if="workspace.validation.length"><li v-for="issue in workspace.validation" :key="issue">{{ issue }}</li></ol><dl><div><dt>业务发布</dt><dd>固定版本</dd></div><div><dt>运行适配器</dt><dd>受保护</dd></div><div><dt>交付版本</dt><dd>{{ workspace.plugin_version }}</dd></div></dl></div>
    <div v-else-if="mode === 'capabilities'" class="contracts-panel"><PluginScenarioBlueprint :blueprint="workspace.scenario_blueprint" :profile="workspace.delivery_profile" :selected-capabilities="workspace.capabilities || []" :files="workspace.files" legacy-workspace /><PluginCapabilityContext :capabilities="workspace.capabilities || []" /></div>
    <template v-else-if="selectedFile">
      <div v-if="mode === 'diff'" class="diff-content" aria-label="逐行修改差异"><div v-for="(line, index) in lines.slice(0, 2000)" :key="index" :class="['diff-line', line.kind]"><span class="line-no">{{ line.before }}</span><span class="line-no">{{ line.after }}</span><span class="diff-sign">{{ line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' ' }}</span><code>{{ line.text || ' ' }}</code></div><p v-if="lines.length > 2000" class="no-change">差异较长，预览前 2000 行。请切换“代码”查看完整内容。</p><p v-if="!lines.length" class="no-change">文件为空</p></div>
      <PluginSourceEditor v-else :model-value="selectedFile.editable ? draft : selectedFile.content" :path="selectedPath" :readonly="!selectedFile.editable || busy" @update:model-value="emit('update:draft', $event)" @save="save" @cursor="setCursor" />
    </template>
    <p v-else class="no-change">从资源管理器打开文件</p>
    <div v-if="dirty && basisChanged" class="basis-warning" role="alert"><p>后台候选已变化。先核对差异，再保留草稿更新基准。</p><el-button size="small" :disabled="busy" @click="emit('rebase')">已核对，保留草稿更新基准</el-button><details><summary>查看后台当前候选</summary><pre>{{ selectedFile?.content }}</pre></details></div>
    <footer class="editor-footer"><span>{{ mode === 'diff' ? dirty ? '后台候选 → 未保存草稿' : '上次文件 → 当前候选' : `行 ${cursorLine}，列 ${cursorColumn} · ${language}` }}</span><div v-if="selectedFile?.editable"><button v-if="dirty" type="button" :disabled="busy" @click="emit('discard')">恢复候选</button><el-button size="small" :disabled="!dirty || busy || basisChanged" @click="save">保存并校验</el-button></div></footer>
    <p v-if="mode === 'source'" class="keyboard-hint">Tab / Shift + Tab 缩进 · Ctrl / ⌘ + S 保存 · Esc 后按 Tab 离开编辑器</p>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CircleCheck, Close, Document, Lock, Warning } from '@element-plus/icons-vue'
import { codingDiff } from '@/utils/pluginCodingDiff'
import PluginCapabilityContext from './PluginCapabilityContext.vue'
import PluginScenarioBlueprint from './PluginScenarioBlueprint.vue'
import PluginSourceEditor from './PluginSourceEditor.vue'
import type { CodingFile, CodingWorkspace } from '@/types/pluginCoding'
const props = defineProps<{ workspace: CodingWorkspace; selectedPath: string; selectedFile: CodingFile | undefined; draft: string; dirty: boolean; basisChanged: boolean; busy: boolean }>()
const emit = defineEmits<{ select: [path: string]; 'update:draft': [value: string]; save: []; discard: []; rebase: [] }>()
const mode = ref<'source' | 'diff' | 'checks' | 'capabilities'>('source')
const openedPaths = ref<string[]>([])
const cursorLine = ref(1)
const cursorColumn = ref(1)
const tabs: { key: typeof mode.value; label: string }[] = [{ key: 'source', label: '代码' }, { key: 'diff', label: '差异' }, { key: 'checks', label: '校验' }, { key: 'capabilities', label: '能力契约' }]
const openedFiles = computed(() => openedPaths.value.map(path => props.workspace.files.find(file => file.path === path)).filter((file): file is CodingFile => Boolean(file)))
const lines = computed(() => codingDiff(props.dirty ? props.selectedFile?.content || '' : props.selectedFile?.previous || '', props.dirty ? props.draft : props.selectedFile?.content || ''))
const language = computed(() => props.selectedPath.endsWith('.py') ? 'Python' : props.selectedPath.endsWith('.json') ? 'JSON' : 'Markdown')
watch(() => props.selectedPath, path => { if (path && !openedPaths.value.includes(path)) openedPaths.value.push(path) }, { immediate: true })
function setCursor(line: number, column: number) { cursorLine.value = line; cursorColumn.value = column }
function save() { if (props.selectedFile?.editable && props.dirty && !props.busy && !props.basisChanged) emit('save') }
function close(path: string) { if (props.busy || (path === props.selectedPath && props.dirty)) return; const next = openedPaths.value.filter(item => item !== path); if (path === props.selectedPath && next[0]) emit('select', next[0]); openedPaths.value = next }
function inspect(path?: string) { mode.value = 'source'; if (path) emit('select', path) }
defineExpose({ inspect, checks: () => { mode.value = 'checks' }, changes: () => { mode.value = 'diff' } })
</script>
<style scoped>
.inspector { display: flex; flex-direction: column; min-height: 0; min-width: 0; background: var(--surface); }
.editor-file-tabs { display: flex; flex: 0 0 auto; min-height: 43px; overflow-x: auto; background: var(--surface-2); border-bottom: 1px solid var(--border); }
.file-tab { display: flex; flex: 0 0 auto; align-items: center; border-right: 1px solid var(--border); border-top: 2px solid transparent; }
.file-tab.active { background: var(--surface); border-top-color: var(--primary); }
.file-tab button { min-height: 39px; display: flex; gap: 7px; align-items: center; padding: 8px 12px; border: 0; background: transparent; font-size: 12px; }
.file-tab .close-tab { padding: 8px 6px; margin-right: 5px; }
.file-tab i { height: 6px; width: 6px; background: var(--warning); border-radius: 50%; }
.file-heading { display: flex; flex: 0 0 auto; padding: 7px 14px; gap: 12px; font-size: 11px; border-bottom: 1px solid var(--border); }
.file-heading code { flex: 1; overflow-wrap: anywhere; }
.file-heading span { color: var(--text-2); white-space: nowrap; }
.inspector-tabs { display: flex; flex: 0 0 auto; gap: 14px; padding: 0 14px; border-bottom: 1px solid var(--border); }
.inspector-tabs button { min-height: 34px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-2); font-size: 11px; }
.inspector-tabs button.active { color: var(--text); border-bottom-color: var(--primary); }
.inspector-tabs span { margin-left: 5px; }
.diff-content { flex: 1; overflow: auto; min-height: 0; margin: 0; padding: 16px 0; }
.diff-line { display: flex; font: 13px/1.9 ui-monospace, Consolas, monospace; min-height: 25px; }
.diff-line.added { background: color-mix(in srgb, var(--success-soft) 70%, var(--surface)); }
.diff-line.removed { background: color-mix(in srgb, var(--danger-soft) 70%, var(--surface)); }
.line-no { width: 28px; flex: 0 0 28px; text-align: right; color: var(--text-2); padding-right: 4px; user-select: none; }
.diff-sign { width: 22px; flex: 0 0 22px; text-align: center; }
.diff-line code { white-space: pre-wrap; overflow-wrap: anywhere; padding: 0 12px 0 4px; }
.editor-footer { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 8px; min-height: 42px; padding: 6px 12px; border-top: 1px solid var(--border); font-size: 10px; color: var(--text-2); }
.editor-footer div { display: flex; gap: 6px; align-items: center; }
.editor-footer button:not(.el-button) { border: 0; background: transparent; font-size: 11px; }
.keyboard-hint { flex: 0 0 auto; margin: 0; padding: 4px 12px 7px; color: var(--text-2); font-size: 10px; }
.checks-panel { padding: 24px; overflow: auto; min-height: 0; flex: 1; }
h3 { margin: 10px 0; font-size: 18px; }
.checks-panel p { color: var(--text-2); font-size: 13px; line-height: 1.8; }
ol { padding-left: 18px; font-size: 13px; line-height: 1.8; }
li { margin: 12px 0; overflow-wrap: anywhere; }
dl { margin-top: 32px; font-size: 12px; }
dl div { display: flex; padding: 12px 0; border-top: 1px solid var(--border); justify-content: space-between; }
dt { color: var(--text-2); }
dd { margin: 0; }
.basis-warning { max-height: 150px; flex: 0 0 auto; overflow: auto; background: var(--warning-soft); padding: 10px 14px; font-size: 12px; }
.basis-warning p { margin: 0 0 8px; }
.basis-warning pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.no-change { padding: 24px; color: var(--text-2); }
.contracts-panel { flex: 1; min-height: 0; overflow: auto; padding: 0 20px; }
.contracts-panel > .capability-context { padding-inline: 0; overflow: visible; }
button { cursor: pointer; font: inherit; color: inherit; }
button:disabled { cursor: not-allowed; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (max-width: 600px) { .editor-footer { flex-wrap: wrap; } .keyboard-hint { font-size: 9px; } .inspector-tabs { gap: 12px; } }
</style>
