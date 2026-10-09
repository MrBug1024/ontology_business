<template>
  <el-drawer :model-value="modelValue" title="编码 AI 设置" size="min(480px, 100vw)" append-to-body :close-on-click-modal="false" :before-close="beforeClose" @update:model-value="emit('update:modelValue', $event)" @closed="restoreFocus">
    <div class="coding-settings">
      <p class="settings-intro">为当前插件项目选择模型和扩展。技能提供开发方法，MCP 提供只读资料；实际读取会在对话中记录。</p>
      <p v-if="draftTask" class="section-note">这些选择随新任务创建时持久保存。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div v-if="loading" class="catalog-state" role="status">正在读取可用模型与受信扩展…</div>
      <template v-if="catalog">
        <section aria-labelledby="coding-model-title"><div class="section-heading"><h2 id="coding-model-title">编码模型</h2><button type="button" @click="emit('manage', 'llm')">管理模型</button></div><label class="sr-only" for="coding-ai-model">编码模型</label><el-select id="coding-ai-model" v-model="draft.llm_config_id" aria-label="编码模型" :disabled="saving" placeholder="选择可用模型"><el-option v-if="draft.llm_config_id && !selectedModel" :value="draft.llm_config_id" label="已不可用的模型，请重新选择" disabled /><el-option v-for="model in catalog.models" :key="model.id" :value="model.id" :label="model.name" /></el-select><p v-if="!catalog.models.length" class="catalog-state">尚无可用编码模型，请先配置并启用模型。</p><p v-if="draft.mcp_ids.length && !selectedModel?.supports_tools" class="field-error" role="alert">所选模型不支持工具调用，请更换模型或卸载 MCP。</p></section>
        <section aria-labelledby="coding-core-title"><div class="section-heading"><h2 id="coding-core-title">基础能力</h2><span>内置</span></div><ul class="base-tools"><li v-for="tool in catalog.base_tools" :key="tool.key"><el-icon aria-hidden="true"><CircleCheck /></el-icon><div><b>{{ tool.title }}</b><p>{{ tool.description }}</p></div></li></ul></section>
        <section aria-labelledby="coding-skills-title"><div class="section-heading"><h2 id="coding-skills-title">技能</h2><button type="button" @click="emit('manage', 'skills')">管理受信技能</button></div><p class="section-note">将受信方法指令安装到当前编码项目，最多 10 个。</p><div v-for="skill in catalog.skills" :key="skill.id" class="extension-row"><div><b>{{ skill.name }}</b><small>{{ skill.version ? `v${skill.version} · ` : '' }}方法指令</small><p>{{ skill.description }}</p></div><button type="button" :class="{ installed: draft.skill_ids.includes(skill.id) }" :disabled="saving || (!draft.skill_ids.includes(skill.id) && draft.skill_ids.length >= 10)" :aria-label="`${draft.skill_ids.includes(skill.id) ? '卸载' : '安装'}技能 ${skill.name}`" :aria-pressed="draft.skill_ids.includes(skill.id)" @click="toggle('skill_ids', skill.id)">{{ draft.skill_ids.includes(skill.id) ? '已安装 · 卸载' : '安装' }}</button></div><p v-if="!catalog.skills.length" class="catalog-state">暂无可用受信技能，由管理员安装并启用后刷新。</p></section>
        <section aria-labelledby="coding-mcp-title"><div class="section-heading"><h2 id="coding-mcp-title">MCP</h2><button type="button" @click="emit('manage', 'mcp')">添加 / 管理 MCP</button></div><p class="section-note">使用已配置服务的只读资料，最多 10 个。</p><div v-for="mcp in catalog.mcps" :key="mcp.id" class="extension-row"><div><b>{{ mcp.name }}</b><small>只读资料</small></div><button type="button" :class="{ installed: draft.mcp_ids.includes(mcp.id) }" :disabled="saving || (!draft.mcp_ids.includes(mcp.id) && (!selectedModel?.supports_tools || draft.mcp_ids.length >= 10))" :aria-label="`${draft.mcp_ids.includes(mcp.id) ? '卸载' : '安装'} MCP ${mcp.name}`" :aria-pressed="draft.mcp_ids.includes(mcp.id)" @click="toggle('mcp_ids', mcp.id)">{{ draft.mcp_ids.includes(mcp.id) ? '已安装 · 卸载' : '安装' }}</button></div><p v-if="!catalog.mcps.length" class="catalog-state">暂无可用 MCP 服务，可在平台设置中添加。</p></section>
        <div v-if="missingResources" class="missing-resources"><p class="field-error" role="alert">已有扩展已停用或不可访问。请卸载不可用项后保存，未保存配置已保留。</p><button v-for="id in missingSkillIds" :key="id" type="button" :disabled="saving" @click="toggle('skill_ids', id)">卸载不可用技能</button><button v-for="id in missingMcpIds" :key="id" type="button" :disabled="saving" @click="toggle('mcp_ids', id)">卸载不可用 MCP</button></div>
      </template>
      <button class="refresh-resources" type="button" :disabled="loading || saving" @click="emit('reload')"><el-icon aria-hidden="true"><Refresh /></el-icon>刷新可用资源</button>
      <el-alert v-if="activeRun" title="本轮正在编码。先停止当前轮次，再保存设置；新设置在下一轮使用。" type="info" :closable="false" /><button v-if="activeRun" class="stop-current" type="button" :disabled="saving" @click="emit('stop')">停止当前编码轮次</button>
    </div>
    <template #footer><div class="settings-footer"><button type="button" :disabled="saving" @click="emit('update:modelValue', false)">取消</button><button class="save-settings" type="button" :disabled="!valid || loading || saving || activeRun" @click="emit('save', copy(draft))">{{ saving ? '保存中…' : draftTask ? '应用到新任务' : '保存设置' }}</button></div></template>
  </el-drawer>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { CircleCheck, Refresh } from '@element-plus/icons-vue'
import type { CodingResourceCatalog, CodingResourceSelection } from '@/types/pluginCoding'
import type { PlatformSettingsTab } from '@/utils/platformSettings'

const props = defineProps<{ modelValue: boolean; selection: CodingResourceSelection; catalog: CodingResourceCatalog | null; loading: boolean; saving: boolean; error: string; activeRun?: boolean; draftTask?: boolean; opener?: HTMLElement | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; save: [selection: CodingResourceSelection]; reload: []; stop: []; manage: [tab: PlatformSettingsTab] }>()
function copy(value: CodingResourceSelection): CodingResourceSelection { return { llm_config_id: value.llm_config_id, skill_ids: [...value.skill_ids], mcp_ids: [...value.mcp_ids] } }
const draft = ref(copy(props.selection))
const selectedModel = computed(() => props.catalog?.models.find(model => model.id === draft.value.llm_config_id))
const missingSkillIds = computed(() => props.catalog ? draft.value.skill_ids.filter(id => !props.catalog?.skills.some(item => item.id === id)) : [])
const missingMcpIds = computed(() => props.catalog ? draft.value.mcp_ids.filter(id => !props.catalog?.mcps.some(item => item.id === id)) : [])
const missingResources = computed(() => Boolean(missingSkillIds.value.length || missingMcpIds.value.length))
const valid = computed(() => Boolean(selectedModel.value && !missingResources.value && (!draft.value.mcp_ids.length || selectedModel.value.supports_tools)))
watch(() => props.modelValue, value => { if (value) draft.value = copy(props.selection) })
watch(() => props.selection, value => { if (!props.modelValue) draft.value = copy(value) })
function toggle(key: 'skill_ids' | 'mcp_ids', id: string) { const values = draft.value[key]; draft.value[key] = values.includes(id) ? values.filter(value => value !== id) : [...values, id] }
function beforeClose(done: () => void) { if (!props.saving) done() }
let disposed = false
async function restoreFocus() {
  // Wait for Escape's model update and the drawer's own focus trap release.
  await nextTick()
  const target = props.opener
  if (!disposed && !props.modelValue && target?.isConnected) target.focus({ preventScroll: true })
}
onBeforeUnmount(() => { disposed = true })
</script>
<style scoped>
.coding-settings { display: grid; gap: 20px; min-width: 0; color: var(--text); }
.settings-intro, .section-note, .catalog-state { margin: 0; font-size: 13px; line-height: 1.7; color: var(--text-2); }
section { min-width: 0; border-bottom: 1px solid var(--border); padding-bottom: 18px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2 { margin: 0; font-size: 14px; font-weight: 600; }
.section-heading > span { color: var(--text-2); font-size: 12px; }
.section-heading button { color: var(--primary); background: transparent; padding: 8px 0; }
.el-select { width: 100%; margin-top: 12px; }
.section-note { font-size: 12px; margin: 8px 0; }
.base-tools { padding: 0; list-style: none; margin: 12px 0 0; }
.base-tools li { display: flex; align-items: flex-start; gap: 9px; padding: 8px 0; }
.base-tools .el-icon { flex-shrink: 0; margin-top: 3px; color: var(--success); }
b { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.base-tools p, .extension-row p { margin: 5px 0 0; color: var(--text-2); line-height: 1.65; font-size: 12px; overflow-wrap: anywhere; }
.extension-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 0; }
.extension-row > div { min-width: 0; }
small { display: block; margin-top: 5px; color: var(--text-2); font-size: 11px; }
button { min-height: 44px; border: 0; border-radius: 7px; font: inherit; font-size: 12px; cursor: pointer; }
.extension-row button { flex-shrink: 0; padding: 8px 12px; background: var(--surface-3); color: var(--text); }
.extension-row button.installed { background: var(--primary-soft); color: var(--primary); }
button:disabled { opacity: .45; cursor: not-allowed; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.field-error { font-size: 12px; line-height: 1.7; color: var(--danger); }
.missing-resources button { margin: 4px 8px 4px 0; padding: 8px 12px; color: var(--text); background: var(--surface-3); }
.refresh-resources, .stop-current { justify-self: start; color: var(--primary); background: transparent; display: flex; align-items: center; gap: 7px; padding: 6px 0; }
.settings-footer { display: flex; justify-content: flex-end; gap: 12px; }
.settings-footer button { min-width: 84px; padding: 10px 16px; color: var(--text); background: var(--surface-3); }
.settings-footer .save-settings { color: var(--surface); background: var(--primary); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
</style>
