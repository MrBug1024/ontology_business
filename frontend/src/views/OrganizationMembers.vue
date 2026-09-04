<template>
  <main class="page members-page">
    <header class="page-header members-header">
      <div>
        <div class="members-kicker">WORKSPACE ACCESS</div>
        <h1>成员与权限</h1>
        <div class="sub">管理当前工作区内的协作者、角色、邀请与访问范围。</div>
      </div>
      <div v-if="canManage" class="members-header-actions">
        <el-button :loading="loading" @click="load">
          <el-icon aria-hidden="true"><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" @click="openInvite">
          <el-icon aria-hidden="true"><UserFilled /></el-icon>
          邀请协作者
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="!canManage"
      type="warning"
      title="当前账户没有管理工作区成员的权限。"
      show-icon
      :closable="false"
    />

    <section v-else class="card members-surface" v-loading="loading" aria-label="工作区成员列表">
      <el-alert
        v-if="loadError"
        class="members-alert"
        type="error"
        :title="loadError"
        show-icon
        :closable="false"
        role="alert"
      >
        <template #default><el-button text type="primary" @click="load">重新加载</el-button></template>
      </el-alert>

      <template v-else>
        <div class="members-summary" aria-live="polite">
          <span>共 {{ members.length }} 名成员</span>
          <span v-if="activeCount">{{ activeCount }} 名已加入</span>
          <span v-if="invitedCount">{{ invitedCount }} 个待接受邀请</span>
        </div>

        <el-table :data="members" row-key="id" empty-text="当前工作区还没有成员" class="members-table">
          <el-table-column label="成员" min-width="260">
            <template #default="{ row }">
              <div class="member-identity">
                <span class="member-avatar" aria-hidden="true">{{ initials(row) }}</span>
                <div class="member-copy">
                  <strong :title="row.display_name || row.email">{{ row.display_name || '未命名成员' }}</strong>
                  <span :title="row.email">{{ row.email }}</span>
                </div>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="角色" min-width="154">
            <template #default="{ row }">
              <el-select
                :model-value="row.role_key"
                :disabled="!canChangeMemberRole(row) || changingMemberId === row.id"
                aria-label="调整成员角色"
                @change="updateRole(row, String($event))"
              >
                <el-option
                  v-for="role in roleOptionsFor(row)"
                  :key="role.key"
                  :label="role.name"
                  :value="role.key"
                >
                  <div class="role-option">
                    <span>{{ role.name }}</span>
                    <small>{{ role.description }}</small>
                  </div>
                </el-option>
              </el-select>
              <span v-if="isCurrentMember(row)" class="current-member-note">当前账户</span>
              <span v-else-if="!canChangeMemberRole(row)" class="current-member-note">已移出工作区</span>
            </template>
          </el-table-column>

          <el-table-column label="状态" min-width="152">
            <template #default="{ row }">
              <div class="status-stack">
                <el-tag :type="memberStatusType(row)" effect="light">{{ memberStatusLabel(row) }}</el-tag>
                <span class="status-detail">{{ memberStatusDetail(row) }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="加入时间" min-width="146">
            <template #default="{ row }"><span class="date-cell">{{ formatDate(row.created_at) }}</span></template>
          </el-table-column>

          <el-table-column label="操作" width="88" fixed="right" align="right">
            <template #default="{ row }">
              <el-dropdown
                trigger="click"
                :disabled="!canManageMember(row) || actionMemberId === row.id"
                @command="runMemberCommand(row, String($event))"
              >
                <el-button
                  text
                  circle
                  :loading="actionMemberId === row.id"
                  :disabled="!canManageMember(row)"
                  :aria-label="`打开 ${row.display_name || row.email} 的成员操作`"
                  :title="memberActionTitle(row)"
                ><el-icon aria-hidden="true"><MoreFilled /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="row.status === 'active'" command="remove"><el-icon aria-hidden="true"><RemoveFilled /></el-icon>移出工作区</el-dropdown-item>
                    <el-dropdown-item v-else-if="row.status === 'invited'" command="revoke-invitation"><el-icon aria-hidden="true"><RemoveFilled /></el-icon>撤销邀请</el-dropdown-item>
                    <el-dropdown-item v-else command="reinvite"><el-icon aria-hidden="true"><RefreshRight /></el-icon>重新邀请</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </section>

    <el-dialog v-model="inviteDialogVisible" title="邀请成员到工作区" width="min(520px, calc(100vw - 28px))" destroy-on-close @closed="inviteError = ''">
      <el-form label-position="top" @submit.prevent="submitInvite">
        <el-form-item label="邮箱" required><el-input v-model.trim="inviteForm.email" type="email" autocomplete="email" placeholder="name@company.com" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model.trim="inviteForm.display_name" autocomplete="name" placeholder="可选，默认使用邮箱前缀" /></el-form-item>
        <el-form-item label="角色" required><el-select v-model="inviteForm.role_key" style="width: 100%"><el-option v-for="role in assignableRoleOptions" :key="role.key" :label="role.name" :value="role.key"><div class="role-option"><span>{{ role.name }}</span><small>{{ role.description }}</small></div></el-option></el-select></el-form-item>
        <el-alert class="invite-note" type="info" :closable="false" show-icon title="已注册账户可在站内直接同意；邀请码 24 小时内有效。" />
        <el-alert v-if="inviteError" class="dialog-error" type="error" :title="inviteError" show-icon :closable="false" role="alert" />
      </el-form>
      <template #footer>
        <el-button @click="inviteDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="inviteSaving" @click="submitInvite">发送邀请</el-button>
      </template>
    </el-dialog>

  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  OrganizationInvitation,
  OrganizationMember,
  OrganizationRole,
  OrganizationRoleKey,
} from '@/types'

const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const members = ref<OrganizationMember[]>([])
const roles = ref<OrganizationRole[]>([])
const loading = ref(false)
const loadError = ref('')
const changingMemberId = ref<string | null>(null)
const actionMemberId = ref<string | null>(null)
const inviteDialogVisible = ref(false)
const inviteSaving = ref(false)
const inviteError = ref('')

const defaultRoles: OrganizationRole[] = [
  { key: 'owner', name: '所有者', description: '管理工作区与成员' },
  { key: 'admin', name: '管理员', description: '管理平台配置与成员' },
  { key: 'operator', name: '运营成员', description: '创建、编辑和调试业务内容' },
  { key: 'viewer', name: '查看成员', description: '查看已授权的业务内容' },
]
const roleOptions = computed(() => roles.value.length ? roles.value : defaultRoles)
const currentRoleKey = computed(() => members.value.find((member) => member.user_id === auth.user?.id)?.role_key)
const assignableRoleOptions = computed(() => {
  if (currentRoleKey.value === 'owner') return roleOptions.value
  return roleOptions.value.filter((role) => role.key !== 'owner' && role.key !== 'admin')
})
const activeCount = computed(() => members.value.filter((member) => member.status === 'active' && member.email_verified).length)
const invitedCount = computed(() => members.value.filter((member) => member.status === 'invited').length)

function newInviteForm(): OrganizationInvitation {
  return { email: '', display_name: '', role_key: 'operator' }
}

const inviteForm = ref<OrganizationInvitation>(newInviteForm())

function detail(error: unknown, fallback: string) {
  const responseDetail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof responseDetail === 'string' && responseDetail) return responseDetail
  return error instanceof Error && error.message ? error.message : fallback
}

function initials(member: OrganizationMember) {
  return (member.display_name || member.email || 'U').slice(0, 1).toUpperCase()
}

function isCurrentMember(member: OrganizationMember) {
  return member.user_id === auth.user?.id
}

function canManageMember(member: OrganizationMember) {
  if (isCurrentMember(member)) return false
  if (currentRoleKey.value !== 'owner' && (member.role_key === 'owner' || member.role_key === 'admin')) {
    return false
  }
  return true
}

function canChangeMemberRole(member: OrganizationMember) {
  return canManageMember(member) && member.status !== 'removed' && member.status !== 'disabled'
}

function roleOptionsFor(member: OrganizationMember) {
  if (canChangeMemberRole(member)) return assignableRoleOptions.value
  const currentRole = roleOptions.value.find((role) => role.key === member.role_key)
  return currentRole ? [currentRole] : roleOptions.value
}

function memberActionTitle(member: OrganizationMember) {
  if (isCurrentMember(member)) return '不能操作当前登录账户'
  if (!canManageMember(member)) return '仅所有者可以管理该成员'
  if (member.status === 'removed' || member.status === 'disabled') return '重新邀请成员'
  return '成员操作'
}

function memberStatusLabel(member: OrganizationMember) {
  if (member.status === 'active' && !member.email_verified) return '待完成邮箱验证'
  if (member.status === 'invited' && member.has_pending_invitation === false) return '邀请已过期'
  return ({
    active: '已加入工作区',
    invited: '待接受邀请',
    removed: '已移出工作区',
    disabled: '已移出工作区',
  } as Record<OrganizationMember['status'], string>)[member.status]
}

function memberStatusType(member: OrganizationMember) {
  if (member.status === 'active' && !member.email_verified) return 'warning'
  if (member.status === 'invited' && member.has_pending_invitation === false) return 'info'
  return ({ active: 'success', invited: 'warning', removed: 'info', disabled: 'info' } as const)[member.status]
}

function memberStatusDetail(member: OrganizationMember) {
  if (member.status === 'invited' && member.invitation_expires_at) {
    return member.has_pending_invitation === false
      ? '请重新发送邀请'
      : `有效至 ${formatDate(member.invitation_expires_at)}`
  }
  if (member.status === 'removed' || member.status === 'disabled') return '可重新邀请加入当前工作区'
  return member.email_verified ? '邮箱已验证' : '邮箱未验证'
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function isRoleKey(value: string): value is OrganizationRoleKey {
  return value === 'owner' || value === 'admin' || value === 'operator' || value === 'viewer'
}

function validEmail(value: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)
}

async function load() {
  if (!canManage.value) return
  loading.value = true
  loadError.value = ''
  try {
    const [nextRoles, nextMembers] = await Promise.all([
      api.listOrganizationRoles(),
      api.listOrganizationMembers(),
    ])
    roles.value = nextRoles
    members.value = nextMembers
  } catch (error) {
    loadError.value = detail(error, '成员信息加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

function openInvite() {
  inviteForm.value = newInviteForm()
  inviteError.value = ''
  inviteDialogVisible.value = true
}

async function submitInvite() {
  inviteError.value = ''
  if (!validEmail(inviteForm.value.email)) {
    inviteError.value = '请输入有效的邮箱地址'
    return
  }
  inviteSaving.value = true
  try {
    const result = await api.inviteOrganizationMember(inviteForm.value)
    inviteDialogVisible.value = false
    ElMessage.success(result.message || '邀请已发送')
    await load()
  } catch (error) {
    inviteError.value = detail(error, '邀请发送失败，请稍后重试')
  } finally {
    inviteSaving.value = false
  }
}

async function updateRole(member: OrganizationMember, value: string) {
  if (isCurrentMember(member)) return
  if (!isRoleKey(value)) {
    ElMessage.error('角色值无效')
    return
  }
  changingMemberId.value = member.id
  try {
    await api.updateOrganizationMemberRole(member.id, value)
    ElMessage.success('成员角色已更新')
    await load()
  } catch (error) {
    ElMessage.error(detail(error, '角色更新失败'))
  } finally {
    changingMemberId.value = null
  }
}

async function runMemberCommand(member: OrganizationMember, command: string) {
  if (isCurrentMember(member)) return
  actionMemberId.value = member.id
  try {
    if (command === 'reinvite') {
      const result = await api.reinviteOrganizationMember(member.id)
      ElMessage.success(result.message || '新的邀请已发送')
      await load()
    } else if (command === 'revoke-invitation') {
      await ElMessageBox.confirm(
        `撤销对「${member.display_name || member.email}」的邀请后，对方将无法加入当前工作区；不会禁用或删除其平台账户。`,
        '确认撤销邀请',
        { type: 'warning', confirmButtonText: '撤销邀请', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
      )
      const result = await api.removeOrganizationMember(member.id)
      ElMessage.success(result.message || '工作区邀请已撤销')
      await load()
    } else if (command === 'remove') {
      await ElMessageBox.confirm(
        `移出「${member.display_name || member.email}」后，其当前工作区访问将立即被收回。该账户与原工作区数据不会受影响；在本工作区创建的共享资源将转交给邀请人。`,
        '确认移出成员',
        { type: 'warning', confirmButtonText: '移出', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
      )
      const result = await api.removeOrganizationMember(member.id)
      ElMessage.success(result.message || '成员已移出工作区')
      await load()
    }
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(detail(error, command === 'remove' ? '移出成员失败' : command === 'revoke-invitation' ? '撤销邀请失败' : '操作失败，请稍后重试'))
    }
  } finally {
    actionMemberId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.members-page { min-height: 100%; }
.members-header { align-items: flex-end; }
.members-kicker { margin-bottom: 5px; color: var(--primary); font-size: 10px; font-weight: 750; letter-spacing: .14em; }
.members-header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.members-surface { min-height: 260px; overflow: hidden; padding: 0; }
.members-alert { margin: 16px 16px 0; }
.members-summary { display: flex; flex-wrap: wrap; gap: 7px 14px; padding: 15px 18px; border-bottom: 1px solid var(--border); color: var(--text-2); font-size: 12px; }
.members-summary span + span::before { margin-right: 14px; color: var(--border-strong); content: '•'; }
.members-table { width: 100%; }
.member-identity { display: flex; min-width: 0; align-items: center; gap: 10px; }
.member-avatar { display: inline-flex; width: 32px; height: 32px; flex: 0 0 32px; align-items: center; justify-content: center; border: 1px solid var(--border-strong); border-radius: 9px; background: var(--primary-soft); color: var(--primary-600); font-size: 12px; font-weight: 750; }
.member-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.member-copy strong, .member-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member-copy strong { color: var(--text); font-size: 13px; font-weight: 700; }
.member-copy span { color: var(--text-3); font-size: 12px; }
.status-stack { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.status-detail, .current-member-note { color: var(--text-3); font-size: 11px; line-height: 1.25; }
.current-member-note { display: block; margin-top: 4px; }
.date-cell { color: var(--text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.role-option { display: flex; min-width: 0; flex-direction: column; gap: 2px; padding: 3px 0; }
.role-option small { overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.dialog-error { margin-top: 4px; }
.invite-note { margin-top: 2px; }
.dialog-form-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 12px; }

@media (max-width: 768px) {
  .members-header { align-items: flex-start; }
  .members-header-actions { width: 100%; justify-content: flex-start; }
  .members-header-actions .el-button { flex: 1 1 auto; }
  .members-surface { margin-inline: -2px; border-radius: 12px !important; }
  .members-summary { padding-inline: 14px; }
  .dialog-form-grid { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  .members-page { animation: none; }
}
</style>
