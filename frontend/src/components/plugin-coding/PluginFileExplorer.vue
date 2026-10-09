<template>
  <aside class="explorer" aria-label="插件资源管理器">
    <header><b>资源管理器</b><span>{{ files.length }} 个文件</span></header>
    <nav class="file-tree" aria-label="插件文件"><div v-for="entry in entries" :key="entry.node.path" class="tree-entry">
      <button type="button" :style="{ paddingLeft: `${12 + entry.depth * 14}px` }" :class="{ selected: entry.node.path === selectedPath }" :disabled="busy" :aria-expanded="entry.node.directory ? !collapsed.has(entry.node.path) : undefined" :aria-pressed="entry.node.directory ? undefined : entry.node.path === selectedPath" @click="open(entry.node)">
        <el-icon aria-hidden="true"><ArrowRight v-if="entry.node.directory && collapsed.has(entry.node.path)" /><ArrowDown v-else-if="entry.node.directory" /><Document v-else-if="isEditable(entry.node.path)" /><Lock v-else /></el-icon><span>{{ entry.node.name }}</span><i v-if="modified(entry.node.path)" class="modified-dot" aria-label="已修改" />
      </button>
    </div></nav>
    <footer><span>场景插件项目</span><p>Skill、脚本与说明可修改。运行适配器和清单保持只读。</p></footer>
  </aside>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowDown, ArrowRight, Document, Lock } from '@element-plus/icons-vue'
import { pluginFileTree, type PluginFileNode } from '@/utils/pluginCodeEditor'
import type { CodingFile } from '@/types/pluginCoding'
const props = defineProps<{ files: CodingFile[]; selectedPath: string; busy: boolean }>()
const emit = defineEmits<{ select: [path: string] }>()
const collapsed = ref(new Set<string>())
const filesByPath = computed(() => new Map(props.files.map(file => [file.path, file])))
const tree = computed(() => pluginFileTree(props.files.map(file => file.path)))
const entries = computed(() => {
  const result: { node: PluginFileNode; depth: number }[] = []
  function visit(nodes: PluginFileNode[], depth: number) { for (const node of nodes) { result.push({ node, depth }); if (node.directory && !collapsed.value.has(node.path)) visit(node.children, depth + 1) } }
  visit(tree.value, 0)
  return result
})
function isEditable(path: string) { return filesByPath.value.get(path)?.editable }
function modified(path: string) { const file = filesByPath.value.get(path); return file?.editable && file.content !== file.previous }
function open(node: PluginFileNode) { if (node.directory) { const next = new Set(collapsed.value); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); collapsed.value = next } else emit('select', node.path) }
</script>
<style scoped>
.explorer { display: flex; flex-direction: column; min-height: 0; min-width: 0; background: var(--surface-2); border-right: 1px solid var(--border); }
header { display: flex; align-items: center; justify-content: space-between; min-height: 43px; padding: 10px 12px; font-size: 11px; border-bottom: 1px solid var(--border); }
header span { color: var(--text-2); font-size: 10px; }
.file-tree { overflow: auto; min-height: 0; flex: 1; padding-block: 8px; }
.tree-entry button { display: flex; align-items: center; gap: 7px; width: 100%; min-height: 31px; padding: 5px 10px; border: 0; color: var(--text); background: transparent; text-align: left; font: 12px ui-monospace, Consolas, monospace; cursor: pointer; }
.tree-entry button.selected { background: var(--primary-soft); color: var(--primary); }
.tree-entry button:hover { background: var(--surface-3); }
.tree-entry button:disabled { cursor: wait; }
.tree-entry button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.tree-entry span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tree-entry .el-icon { flex: 0 0 14px; font-size: 13px; }
.modified-dot { margin-left: auto; flex: 0 0 5px; height: 5px; background: var(--warning); border-radius: 50%; }
footer { padding: 12px; border-top: 1px solid var(--border); font-size: 10px; color: var(--text-2); }
footer p { margin: 6px 0 0; line-height: 1.7; }
</style>
