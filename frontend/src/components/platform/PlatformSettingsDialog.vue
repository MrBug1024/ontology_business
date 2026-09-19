<template>
  <el-dialog
    :model-value="modelValue"
    title="平台设置"
    class="platform-settings-dialog"
    width="min(1120px, calc(100vw - 28px))"
    top="max(14px, 4vh)"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="settings-dialog-title">
        <span class="settings-dialog-icon" aria-hidden="true"><el-icon><Setting /></el-icon></span>
        <span><strong>平台设置</strong><small>{{ workspaceName }}</small></span>
      </div>
    </template>
    <el-tabs
      v-if="modelValue"
      :key="auth.user?.tenant_id"
      :model-value="initialTab"
      class="settings-tabs"
      @tab-change="changeTab"
    >
      <el-tab-pane label="通用" name="general">
        <section class="general-settings" aria-labelledby="general-settings-title">
          <h2 id="general-settings-title">通用</h2>
          <p>从任意页面管理平台共享资源，再由各类 AI 在自己的对话设置中选择。</p>
          <dl class="workspace-facts">
            <div><dt>当前工作区</dt><dd>{{ workspaceName }}</dd></div>
            <div><dt>你的角色</dt><dd>{{ workspaceRole }}</dd></div>
            <div><dt>配置权限</dt><dd>{{ canManage ? '可管理' : '只读' }}</dd></div>
          </dl>
          <div class="appearance-setting">
            <div><strong>界面主题</strong><span>应用于整个平台</span></div>
            <el-switch :model-value="theme === 'dark'" active-text="深色" inactive-text="浅色" aria-label="深色主题" @change="emit('toggle-theme')" />
          </div>
          <p class="settings-note">模型、技能与 MCP 在当前工作区内共享，切换工作区后使用对应配置。模板附件可在资料库中管理。</p>
        </section>
      </el-tab-pane>
      <el-tab-pane label="AI 模型" name="llm" lazy><ModelSettingsPanel /></el-tab-pane>
      <el-tab-pane label="工具" name="tools" lazy><ToolSettingsPanel @configure-mcp="emit('tab-change', 'mcp')" /></el-tab-pane>
      <el-tab-pane label="技能" name="skills" lazy><SkillSettingsPanel /></el-tab-pane>
      <el-tab-pane label="MCP" name="mcp" lazy><McpSettingsPanel /></el-tab-pane>
    </el-tabs>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { Setting } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { platformSettingsTabFromQuery, type PlatformSettingsTab } from '@/utils/platformSettings'

const ModelSettingsPanel = defineAsyncComponent(() => import('./ModelSettingsPanel.vue'))
const ToolSettingsPanel = defineAsyncComponent(() => import('./ToolSettingsPanel.vue'))
const SkillSettingsPanel = defineAsyncComponent(() => import('./SkillSettingsPanel.vue'))
const McpSettingsPanel = defineAsyncComponent(() => import('./McpSettingsPanel.vue'))

defineProps<{
  modelValue: boolean
  initialTab: PlatformSettingsTab
  theme: 'light' | 'dark'
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  'tab-change': [value: PlatformSettingsTab]
  'toggle-theme': []
}>()
const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const workspaceName = computed(() => auth.user?.workspace_name || '当前工作区')
const workspaceRole = computed(() => ({
  owner: '所有者', admin: '管理员', operator: '操作员', viewer: '查看者',
} as Record<string, string>)[auth.user?.workspace_role || ''] || '成员')

function changeTab(value: string | number) {
  const tab = platformSettingsTabFromQuery(value)
  if (tab) emit('tab-change', tab)
}
</script>

<style scoped>
.settings-dialog-title { display: flex; align-items: center; gap: 10px; }
.settings-dialog-title > span:last-child { display: grid; min-width: 0; gap: 3px; }
.settings-dialog-title strong { color: var(--text); font-size: 16px; }
.settings-dialog-title small { color: var(--text-3); font-size: 12px; }
.settings-dialog-icon { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; border-radius: 8px; background: var(--primary-soft); color: var(--primary); }
.settings-tabs { min-height: min(560px, calc(100dvh - 180px)); }
.settings-tabs :deep(.el-tabs__header) { margin: 0; padding: 0 24px; border-bottom: 1px solid var(--border); }
.settings-tabs :deep(.el-tabs__item) { min-height: 46px; padding: 0 16px; font-size: 13px; }
.settings-tabs :deep(.el-tabs__nav-wrap::after) { height: 0; }
.general-settings { padding: 24px; }
.general-settings h2 { margin: 0 0 8px; color: var(--text); font-size: 20px; }
.general-settings p { margin: 0 0 20px; color: var(--text-2); font-size: 13px; line-height: 1.6; }
.workspace-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin: 0 0 24px; }
.workspace-facts > div { min-width: 0; padding: 14px; border: 1px solid var(--border); border-radius: 8px; }
.workspace-facts dt { color: var(--text-3); font-size: 12px; }
.workspace-facts dd { margin: 6px 0 0; overflow-wrap: anywhere; color: var(--text); font-size: 14px; }
.appearance-setting { display: flex; justify-content: space-between; gap: 16px; padding: 18px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.appearance-setting > div { display: grid; gap: 4px; color: var(--text); font-size: 14px; }
.appearance-setting span { color: var(--text-3); font-size: 12px; }
.general-settings .settings-note { margin: 18px 0 0; color: var(--text-3); }
@media (max-width: 720px) {
  .settings-tabs { min-height: 0; }
  .settings-tabs :deep(.el-tabs__header) { padding: 0 12px; }
  .settings-tabs :deep(.el-tabs__item) { padding: 0 12px; }
  .general-settings { padding: 18px 14px 24px; }
  .workspace-facts { grid-template-columns: 1fr; gap: 10px; }
}
</style>

<style>
.platform-settings-dialog { padding: 0; }
.platform-settings-dialog > .el-dialog__header { margin: 0; padding: 18px 24px; border-bottom: 1px solid var(--border); }
.platform-settings-dialog > .el-dialog__body { max-height: calc(100dvh - 140px); padding: 0; overflow: auto; scrollbar-gutter: stable; }
.platform-settings-dialog :focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
</style>
