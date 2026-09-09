<template>
  <el-dropdown trigger="click" @visible-change="onOpen" @command="switchTo">
    <button class="workspace-trigger" type="button" :disabled="switching" aria-label="切换工作区">
      <span>{{ auth.user?.workspace_name || '选择工作区' }}</span><el-icon><ArrowDown /></el-icon>
    </button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item v-if="loading" disabled>加载工作区…</el-dropdown-item>
        <el-dropdown-item v-if="error" command="retry">{{ error }} · 重试</el-dropdown-item>
        <el-dropdown-item v-for="workspace in data" :key="workspace.tenant_id" :command="workspace.tenant_id" :disabled="workspace.tenant_id === auth.user?.tenant_id">
          {{ workspace.name }} · {{ workspaceRoleLabels[workspace.role] }}
        </el-dropdown-item>
        <el-dropdown-item v-if="!loading && !error && data?.length === 0" disabled>暂无可用工作区</el-dropdown-item>
        <el-dropdown-item divided command="invitations">工作区邀请</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAccessResource } from '@/composables/useAccessResource'
import { workspaceRoleLabels } from '@/utils/accessPresentation'

const auth = useAuthStore()
const router = useRouter()
const switching = ref(false)
const { data, loading, error, reload } = useAccessResource(workspaceAccess.workspaces)
function onOpen(open: boolean) { if (open) void reload() }
async function switchTo(command: string) {
  if (command === 'retry') return reload()
  if (command === 'invitations') return router.push('/invitations')
  if (switching.value) return
  switching.value = true
  try {
    await workspaceAccess.switchWorkspace(command)
    // Reload closes old workspace requests, SSE streams and component caches.
    window.location.assign('/scenarios')
  } catch (reason: unknown) {
    ElMessage.error(reason instanceof Error ? reason.message : '切换失败，请重试')
    switching.value = false
  }
}
</script>

<style scoped>
.workspace-trigger { display: flex; align-items: center; gap: 6px; min-height: 44px; max-width: min(30vw, 240px); border: 0; border-radius: 6px; background: transparent; color: var(--text-2); cursor: pointer; font: inherit; }
.workspace-trigger span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-trigger:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 600px) { .workspace-trigger { min-width: 0; max-width: min(100%, 130px); font-size: 12px; } }
</style>
