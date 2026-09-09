<template>
  <section aria-label="工作区邀请记录">
    <div class="panel-toolbar"><span>共 {{ data?.total ?? 0 }} 条邀请</span><el-button :loading="loading" @click="reload">刷新</el-button></div>
    <el-alert v-if="error || actionError" :title="error || actionError" type="error" :closable="false" show-icon />
    <div v-loading="loading" class="table-scroll" :aria-busy="loading">
      <el-table :data="data?.items || []" empty-text="暂无工作区邀请" row-key="id">
        <el-table-column :label="inbox ? '工作区' : '受邀成员'" min-width="210">
          <template #default="{ row }"><strong>{{ inbox ? row.workspace_name : (row.display_name || row.email) }}</strong><div class="secondary">{{ row.email }}</div></template>
        </el-table-column>
        <el-table-column label="工作区角色" min-width="105"><template #default="{ row }">{{ workspaceRoleLabels[row.role as WorkspaceRole] }}</template></el-table-column>
        <el-table-column label="状态" min-width="160"><template #default="{ row }"><el-tag :type="row.status === 'accepted' ? 'success' : 'info'">{{ invitationStatusLabels[row.status as Invitation['status']] }}</el-tag><div v-if="!inbox" class="secondary">{{ deliveryStatusLabels[row.delivery_status as Invitation['delivery_status']] }}</div></template></el-table-column>
        <el-table-column label="有效期至" min-width="170"><template #default="{ row }">{{ accessDate(row.expires_at) }}</template></el-table-column>
        <el-table-column label="操作" min-width="200"><template #default="{ row }">
          <template v-if="inbox && row.status === 'pending'"><el-button type="primary" size="small" :disabled="busy || loading" @click="act(row, 'accept')">同意加入</el-button><el-button size="small" :disabled="busy || loading" @click="act(row, 'decline')">拒绝</el-button></template>
          <template v-else-if="!inbox">
            <el-button v-if="row.status !== 'accepted'" size="small" :disabled="busy || loading || ['queued', 'sending'].includes(row.delivery_status)" @click="act(row, 'resend')">重新邀请</el-button>
            <el-button v-if="row.status === 'pending'" size="small" :disabled="busy || loading" @click="act(row, 'revoke')">撤销</el-button>
          </template>
          <span v-else class="secondary">{{ row.status === 'accepted' ? '可通过顶部菜单切换工作区' : '无需操作' }}</span>
        </template></el-table-column>
      </el-table>
    </div>
    <el-pagination v-if="(data?.total || 0) > 20" v-model:current-page="page" :total="data?.total || 0" :page-size="20" layout="prev, pager, next" aria-label="邀请分页" />
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAccessResource } from '@/composables/useAccessResource'
import { accessDate, deliveryStatusLabels, invitationStatusLabels, workspaceRoleLabels } from '@/utils/accessPresentation'
import type { Invitation, WorkspaceRole } from '@/types/access'

const props = withDefaults(defineProps<{ inbox?: boolean; refreshKey?: number }>(), { inbox: false, refreshKey: 0 })
const page = ref(1)
const busy = ref(false)
const actionError = ref('')
const { data, loading, error, reload } = useAccessResource(signal => workspaceAccess.invitations(props.inbox, page.value, signal))
watch([page, () => props.inbox, () => props.refreshKey], reload, { immediate: true })
async function act(row: Invitation, action: 'accept' | 'decline' | 'resend' | 'revoke') {
  if (busy.value) return
  busy.value = true
  actionError.value = ''
  const prompts = { accept: `同意以${workspaceRoleLabels[row.role]}身份加入「${row.workspace_name}」？`, decline: '拒绝这条工作区邀请？', resend: '重新发送邀请邮件？之前的邀请将失效；若投递结果待确认，对方可能已收到上一封邮件。', revoke: '撤销邀请后，对方将无法通过该邀请加入。确认撤销？' }
  try {
    await ElMessageBox.confirm(prompts[action], '确认操作', { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' })
    const result = action === 'accept' || action === 'decline'
      ? await workspaceAccess.respond(row.id, action, row.revision)
      : await workspaceAccess.manageInvitation(row.id, action, row.revision)
    ElMessage.success(result.message)
    await reload()
  } catch (reason: unknown) {
    if (reason !== 'cancel' && reason !== 'close') actionError.value = reason instanceof Error ? reason.message : '操作失败，请重试'
  } finally { busy.value = false }
}
</script>

<style scoped>
.panel-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; color: var(--text-3); }
.secondary { color: var(--text-3); font-size: 12px; margin-top: 5px; overflow-wrap: anywhere; }
.table-scroll { min-height: 150px; margin: 16px 0; }
</style>
