<template>
  <el-dialog :model-value="open" title="账户变更记录" width="min(760px, 94vw)" @update:model-value="emit('update:open', $event)">
    <p class="access-muted">{{ account?.email }}</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-loading="loading"><el-table :data="data || []" empty-text="暂无账户变更记录">
      <el-table-column label="操作人" prop="actor_name" min-width="110" />
      <el-table-column label="变更" min-width="180"><template #default="{ row }">{{ describe(row.before_value) }} → {{ describe(row.after_value) }}</template></el-table-column>
      <el-table-column label="原因" prop="reason" min-width="180" />
      <el-table-column label="时间" min-width="175"><template #default="{ row }">{{ accessDate(row.created_at) }}</template></el-table-column>
    </el-table></div>
    <template #footer><el-button :disabled="page === 1 || loading" @click="page -= 1">上一页</el-button><el-button :disabled="(data?.length || 0) < 20 || loading" @click="page += 1">下一页</el-button><el-button :loading="loading" @click="reload">刷新</el-button></template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Account } from '@/types/access'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAccessResource } from '@/composables/useAccessResource'
import { accessDate } from '@/utils/accessPresentation'

const props = defineProps<{ open: boolean; account: Account | null }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const page = ref(1)
const { data, loading, error, reload } = useAccessResource(signal => props.account && props.open ? workspaceAccess.audit(props.account.id, page.value, signal) : Promise.resolve([]))
watch(() => props.account?.id, () => { page.value = 1 })
watch([() => props.open, () => props.account?.id, page], reload)
function describe(value: string): string {
  const labels: Record<string, string> = { user: '普通账户', superadmin: '超级管理员', active: '正常', pending: '待验证', disabled: '已禁用' }
  return value ? value.split('/').map(part => labels[part] || part).join(' / ') : '初始状态'
}
</script>
