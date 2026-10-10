<template>
  <section class="file-preview" aria-label="插件源码只读预览">
    <header class="preview-heading">
      <div class="preview-title"><code>{{ file.path }}</code><p>插件源码 · {{ hostLabel(project.host) }} · {{ projectLabel }}</p></div>
      <span class="state-badge" :class="file.editable ? 'editable' : 'locked'"><el-icon aria-hidden="true"><Lock v-if="!file.editable" /><EditPen v-else /></el-icon>{{ file.editable ? '工作台中可编辑' : '受保护只读' }}</span>
    </header>
    <PluginSourceEditor :model-value="file.content" :path="file.path" readonly />
    <footer class="preview-foot">
      <span>{{ file.content.split('\n').length }} 行 · 只读预览</span>
      <button type="button" class="open-link" @click="emit('open-project', project)">在本页继续编码<el-icon aria-hidden="true"><ArrowRight /></el-icon></button>
    </footer>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, EditPen, Lock } from '@element-plus/icons-vue'
import PluginSourceEditor from './PluginSourceEditor.vue'
import type { CodingFile, CodingProjectSummary } from '@/types/pluginCoding'

const props = defineProps<{ project: CodingProjectSummary; file: CodingFile }>()
const emit = defineEmits<{ 'open-project': [project: CodingProjectSummary] }>()
const phaseLabels: Record<string, string> = { draft: '草稿', generating: '编码中', ready_for_review: '待审阅', validation_failed: '需修正', released: '已定版' }
const projectLabel = computed(() => {
  const phase = phaseLabels[props.project.phase] || '可继续'
  return props.project.plugin_version ? `${phase} · 最近定版 v${props.project.plugin_version}` : phase
})
function hostLabel(value: string) { return value === 'codex' ? 'OpenAI Codex' : 'Claude Code' }
</script>
<style scoped>
.file-preview { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; background: var(--surface); }
.preview-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 14px; border-bottom: 1px solid var(--border); flex: 0 0 auto; }
.preview-title { min-width: 0; }
.preview-title code { font-size: 12px; overflow-wrap: anywhere; }
.preview-title p { margin: 3px 0 0; color: var(--text-2); font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.state-badge { display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0; padding: 3px 9px; border-radius: 999px; font-size: 10px; }
.state-badge .el-icon { font-size: 11px; }
.state-badge.editable { color: var(--primary); background: var(--primary-soft); }
.state-badge.locked { color: var(--text-2); background: var(--surface-3); }
.preview-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; padding: 6px 12px; border-top: 1px solid var(--border); font-size: 10px; color: var(--text-2); flex: 0 0 auto; }
.open-link { display: inline-flex; align-items: center; gap: 5px; color: var(--primary); font-size: 11px; text-decoration: none; border: 0; background: transparent; cursor: pointer; font: inherit; padding: 4px 6px; border-radius: 6px; }
.open-link:hover { color: var(--primary-600); background: var(--primary-soft); }
.open-link:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
</style>
