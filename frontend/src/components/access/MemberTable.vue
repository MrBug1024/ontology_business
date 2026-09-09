<template>
  <el-table :data="members" row-key="id" empty-text="暂无成员" aria-label="工作区成员列表">
    <el-table-column label="成员" min-width="220"><template #default="{ row }"><strong>{{ row.display_name || row.email }}</strong><div class="secondary">{{ row.email }}</div></template></el-table-column>
    <el-table-column label="工作区角色" min-width="165"><template #default="{ row }">
      <el-select :model-value="row.role" :aria-label="`调整 ${row.display_name || row.email} 的角色`" :disabled="busy || !row.can_edit || row.status !== 'active'" @change="emit('role', row, $event)">
        <el-option v-for="key in roles" :key="key" :label="workspaceRoleLabels[key]" :value="key" :disabled="actorRole !== 'owner' && ['owner', 'admin'].includes(key)" />
      </el-select>
      <div v-if="row.user_id === currentUserId" class="secondary">当前账户</div>
    </template></el-table-column>
    <el-table-column label="状态" min-width="155"><template #default="{ row }">
      <el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '已加入工作区' : '已移出工作区' }}</el-tag>
      <div class="secondary">{{ row.account_status === 'disabled' ? '平台账户已禁用' : row.email_verified ? '邮箱已验证' : '邮箱待验证' }}</div>
    </template></el-table-column>
    <el-table-column label="加入时间" min-width="175"><template #default="{ row }">{{ accessDate(row.created_at) }}</template></el-table-column>
    <el-table-column label="操作" min-width="130"><template #default="{ row }">
      <el-button v-if="row.can_edit && row.status === 'active'" type="danger" plain size="small" :disabled="busy" @click="emit('remove', row)">移除成员</el-button>
      <el-button v-else-if="row.status !== 'active' && row.can_edit" size="small" :disabled="busy" @click="emit('reinvite', row)">重新邀请</el-button>
      <span v-else class="secondary">—</span>
    </template></el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Member, WorkspaceRole } from '@/types/access'
import { accessDate, workspaceRoleLabels } from '@/utils/accessPresentation'

const props = defineProps<{ members: Member[]; actorRole: WorkspaceRole; currentUserId: string; busy: boolean }>()
const emit = defineEmits<{ role: [member: Member, role: WorkspaceRole]; remove: [member: Member]; reinvite: [member: Member] }>()
const roles = computed<WorkspaceRole[]>(() => props.actorRole === 'owner' ? ['owner', 'admin', 'operator', 'viewer'] : ['operator', 'viewer', 'admin', 'owner'])
</script>

<style scoped>
.secondary { margin-top: 5px; color: var(--text-3); font-size: 12px; overflow-wrap: anywhere; }
</style>
