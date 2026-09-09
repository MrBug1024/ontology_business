<template>
  <el-dialog :model-value="open" title="管理平台账户" width="min(500px, 94vw)" :close-on-click-modal="false" :before-close="close" @update:model-value="emit('update:open', $event)">
    <el-form v-if="account" label-position="top" @submit.prevent="submit">
      <p><strong>{{ account.display_name || account.email }}</strong><br /><span class="access-muted">{{ account.email }}</span></p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-form-item label="系统角色"><el-select v-model="systemRole" aria-label="系统角色" :disabled="busy"><el-option label="普通账户" value="user" /><el-option label="超级管理员" value="superadmin" /></el-select></el-form-item>
      <el-form-item label="账户状态"><el-select v-model="status" aria-label="账户状态" :disabled="busy"><el-option :label="account.email_verified ? '正常' : '待验证邮箱'" :value="account.email_verified ? 'active' : 'pending'" /><el-option label="已禁用" value="disabled" /></el-select></el-form-item>
      <el-form-item label="变更原因" required><el-input v-model="reason" aria-label="变更原因" type="textarea" :rows="3" maxlength="500" show-word-limit :disabled="busy" /></el-form-item>
      <p class="access-muted">系统角色不赋予工作区权限。保存将撤销该账户的旧会话和凭据；禁用不会删除数据。</p>
      <div class="actions"><el-button :disabled="busy" @click="close">取消</el-button><el-button type="primary" native-type="submit" :loading="busy">保存变更</el-button></div>
    </el-form>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { Account, AccountStatus, SystemRole } from '@/types/access'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ open: boolean; account: Account | null }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [] }>()
const auth = useAuthStore()
const systemRole = ref<SystemRole>('user')
const status = ref<AccountStatus>('active')
const reason = ref('')
const busy = ref(false)
const error = ref('')
watch(() => props.account, account => {
  if (account) { systemRole.value = account.system_role; status.value = account.status; reason.value = ''; error.value = '' }
})
function close() { if (!busy.value) emit('update:open', false) }
async function submit() {
  const account = props.account
  if (!account || busy.value) return
  if (!reason.value.trim()) { error.value = '请填写变更原因'; return }
  busy.value = true; error.value = ''
  try {
    await ElMessageBox.confirm(`确认更改 ${account.email} 的系统权限或状态？该操作会记录审计。`, '确认账户变更', { confirmButtonText: '确认保存', cancelButtonText: '返回编辑', type: 'warning' })
    const result = await workspaceAccess.updateAccount(account.id, { expected_revision: account.revision, system_role: systemRole.value, status: status.value, reason: reason.value.trim() })
    ElMessage.success(result.message)
    if (account.id === auth.user?.id) { window.location.assign('/login'); return }
    emit('saved'); emit('update:open', false)
  } catch (failure: unknown) { if (failure !== 'cancel' && failure !== 'close') error.value = failure instanceof Error ? failure.message : '保存失败，请重试' }
  finally { busy.value = false }
}
</script>

<style scoped>
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 22px; }
.el-alert { margin-bottom: 16px; }
</style>
