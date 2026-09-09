<template>
  <el-dialog :model-value="open" title="邀请成员到工作区" width="min(480px, 94vw)" :close-on-click-modal="false" :before-close="close" @update:model-value="emit('update:open', $event)">
    <el-form label-position="top" @submit.prevent="submit">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="form-error" />
      <el-button v-if="existingInvitation" text type="primary" @click="emit('show-invitations')">查看邀请记录并重新发送</el-button>
      <el-form-item label="邮箱" required><el-input v-model="email" type="email" aria-label="受邀成员邮箱" maxlength="320" autocomplete="email" :disabled="busy" /></el-form-item>
      <el-form-item label="显示名称"><el-input v-model="displayName" aria-label="受邀成员显示名称" maxlength="120" :disabled="busy" /></el-form-item>
      <el-form-item label="工作区角色" required>
        <el-select v-model="role" aria-label="受邀成员角色" :disabled="busy">
          <el-option v-for="key in roles" :key="key" :label="workspaceRoleLabels[key]" :value="key" />
        </el-select>
      </el-form-item>
      <p class="hint">邮件包含平台访问链接。对方需使用收件邮箱登录并同意加入；邀请 24 小时内有效。</p>
      <div class="dialog-actions"><el-button :disabled="busy" @click="close">取消</el-button><el-button type="primary" native-type="submit" :loading="busy">发送邀请</el-button></div>
    </el-form>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { workspaceAccess } from '@/api/workspaceAccess'
import type { Member, WorkspaceRole } from '@/types/access'
import { workspaceRoleLabels } from '@/utils/accessPresentation'

const props = defineProps<{ open: boolean; actorRole: WorkspaceRole; member?: Member | null }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: []; 'show-invitations': [] }>()
const email = ref('')
const displayName = ref('')
const role = ref<WorkspaceRole>('operator')
const error = ref('')
const busy = ref(false)
const existingInvitation = ref(false)
watch(() => props.member, member => {
  if (member) { email.value = member.email; displayName.value = member.display_name; role.value = member.role; error.value = ''; existingInvitation.value = false }
})
const roles = computed<WorkspaceRole[]>(() => props.actorRole === 'owner' ? ['owner', 'admin', 'operator', 'viewer'] : ['operator', 'viewer'])
function close() { if (!busy.value) emit('update:open', false) }
async function submit() {
  if (busy.value) return
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim())) { error.value = '请输入有效的邮箱地址'; return }
  busy.value = true
  error.value = ''
  existingInvitation.value = false
  try {
    await workspaceAccess.invite({ email: email.value.trim(), display_name: displayName.value.trim(), role: role.value })
    ElMessage.success('邀请已创建，邮件等待投递；可在邀请记录中查看结果')
    email.value = ''; displayName.value = ''; role.value = 'operator'
    emit('saved'); emit('update:open', false)
  } catch (reason: unknown) {
    error.value = reason instanceof Error ? reason.message : '邀请失败，请重试'
    existingInvitation.value = reason instanceof Error && 'status' in reason && reason.status === 409
  }
  finally { busy.value = false }
}
</script>

<style scoped>
.hint { color: var(--text-3); font-size: 13px; line-height: 1.7; }
.form-error { margin-bottom: 16px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 24px; }
</style>
