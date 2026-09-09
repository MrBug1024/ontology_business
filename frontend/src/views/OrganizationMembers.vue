<template>
  <div class="access-page">
    <header class="access-heading"><div><span class="access-eyebrow">WORKSPACE ACCESS</span><h1>成员与权限</h1><p>管理当前工作区内的协作者、角色与邀请。</p></div><div class="access-actions"><el-button :loading="loading" @click="reload">刷新</el-button><el-button v-if="data?.can_manage" type="primary" @click="inviteOpen = true">邀请协作者</el-button></div></header>
    <el-alert v-if="error || actionError" :title="error || actionError" type="error" :closable="false" show-icon />
    <el-alert v-if="data && !data.can_manage" title="你可以查看成员；邀请、移除和角色调整需工作区所有者或管理员操作。" type="info" :closable="false" />
    <section class="access-card">
      <el-tabs v-model="tab">
        <el-tab-pane label="工作区成员" name="members">
          <p class="access-muted">共 {{ data?.total ?? 0 }} 名成员。所有者和管理员权限仅在当前工作区生效。</p>
          <div v-loading="loading" :aria-busy="loading"><MemberTable :members="data?.items || []" :actor-role="data?.role || 'viewer'" :current-user-id="auth.user?.id || ''" :busy="busy || loading" @role="changeRole" @remove="removeMember" @reinvite="reinvite" /></div>
          <el-pagination v-if="(data?.total || 0) > 20" v-model:current-page="page" :total="data?.total || 0" :page-size="20" layout="prev, pager, next" aria-label="成员分页" />
        </el-tab-pane>
        <el-tab-pane v-if="data?.can_manage" label="邀请记录" name="invitations"><InvitationsPanel :refresh-key="refreshKey" /></el-tab-pane>
      </el-tabs>
    </section>
    <InviteMemberDialog v-if="data?.can_manage" v-model:open="inviteOpen" :actor-role="data.role" :member="invitedMember" @saved="invited" @show-invitations="showInvitations" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAccessResource } from '@/composables/useAccessResource'
import MemberTable from '@/components/access/MemberTable.vue'
import InviteMemberDialog from '@/components/access/InviteMemberDialog.vue'
import InvitationsPanel from '@/components/access/InvitationsPanel.vue'
import { accessPageNumber, workspaceRoleLabels } from '@/utils/accessPresentation'
import type { Member, WorkspaceRole } from '@/types/access'
import '@/styles/access.css'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const page = computed({ get: () => accessPageNumber(route.query.page), set: value => { void router.push({ query: { ...route.query, page: String(value) } }) } })
const tab = computed({ get: () => route.query.tab === 'invitations' ? 'invitations' : 'members', set: value => { void router.push({ query: { ...route.query, tab: value } }) } })
const inviteOpen = ref(false)
const invitedMember = ref<Member | null>(null)
const refreshKey = ref(0)
const busy = ref(false)
const actionError = ref('')
const { data, loading, error, reload } = useAccessResource(signal => workspaceAccess.members(page.value, signal))
watch(page, reload, { immediate: true })
function invited() { refreshKey.value += 1; tab.value = 'invitations' }
function reinvite(member: Member) { invitedMember.value = { ...member }; inviteOpen.value = true }
function showInvitations() { inviteOpen.value = false; invited() }
async function mutate(member: Member, role?: WorkspaceRole) {
  if (busy.value) return
  busy.value = true; actionError.value = ''
  try {
    const text = role ? `将 ${member.display_name || member.email} 的工作区角色改为${workspaceRoleLabels[role]}？` : `将 ${member.display_name || member.email} 移出当前工作区？其旧会话和当前工作区凭据将失效。`
    await ElMessageBox.confirm(text, '确认成员权限变更', { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' })
    const result = role ? await workspaceAccess.setRole(member.id, role, member.revision) : await workspaceAccess.remove(member.id, member.revision)
    ElMessage.success(result.message)
    refreshKey.value += 1
    await reload()
  } catch (reason: unknown) { if (reason !== 'cancel' && reason !== 'close') actionError.value = reason instanceof Error ? reason.message : '操作失败，请重试' }
  finally { busy.value = false }
}
function changeRole(member: Member, role: WorkspaceRole) { void mutate(member, role) }
function removeMember(member: Member) { void mutate(member) }
</script>
