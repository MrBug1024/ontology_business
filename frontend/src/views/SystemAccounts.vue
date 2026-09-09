<template>
  <div class="access-page">
    <header class="access-heading"><div><span class="access-eyebrow">SYSTEM ACCOUNTS</span><h1>账户管理</h1><p>了解平台账户的使用情况，管理系统角色与账户状态。</p></div><el-button :loading="loading" @click="reload">刷新</el-button></header>
    <el-alert title="系统超级管理员负责平台账户治理；工作区角色仍由各工作区独立管理。" type="info" :closable="false" show-icon />
    <section class="access-card">
      <el-form class="search-form" inline @submit.prevent="search">
        <el-form-item label="账户搜索"><el-input v-model="searchText" aria-label="按邮箱或名称搜索账户" placeholder="邮箱或显示名称" clearable maxlength="120" /></el-form-item>
        <el-form-item label="状态"><el-select v-model="statusFilter" aria-label="筛选账户状态" placeholder="全部状态" style="width: 160px"><el-option label="全部状态" value="" /><el-option label="正常" value="active" /><el-option label="已禁用" value="disabled" /><el-option label="待验证邮箱" value="pending" /></el-select></el-form-item>
        <el-form-item><el-button type="primary" native-type="submit" :loading="loading">查询</el-button></el-form-item>
      </el-form>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <p class="access-muted">共 {{ data?.total ?? 0 }} 个账户</p>
      <div v-loading="loading" :aria-busy="loading"><el-table :data="data?.items || []" row-key="id" empty-text="没有匹配的账户" aria-label="平台账户列表">
        <el-table-column label="账户" min-width="220"><template #default="{ row }"><strong>{{ row.display_name || row.email }}</strong><div class="access-secondary">{{ row.email }}</div></template></el-table-column>
        <el-table-column label="系统角色" min-width="125"><template #default="{ row }"><el-tag :type="row.system_role === 'superadmin' ? 'warning' : 'info'">{{ systemRoleLabels[row.system_role as SystemRole] }}</el-tag></template></el-table-column>
        <el-table-column label="状态" min-width="120"><template #default="{ row }"><el-tag :type="row.status === 'disabled' ? 'danger' : row.status === 'active' ? 'success' : 'info'">{{ accountStatusLabels[row.status as AccountStatus] }}</el-tag></template></el-table-column>
        <el-table-column label="工作区数" prop="workspace_count" width="105" />
        <el-table-column label="注册时间" min-width="175"><template #default="{ row }">{{ accessDate(row.created_at) }}</template></el-table-column>
        <el-table-column label="最近登录" min-width="175"><template #default="{ row }">{{ accessDate(row.last_login_at) }}</template></el-table-column>
        <el-table-column label="操作" width="180" fixed="right"><template #default="{ row }"><el-button size="small" :disabled="loading" @click="edit(row)">管理账户</el-button><el-button size="small" :disabled="loading" @click="audit(row)">变更记录</el-button></template></el-table-column>
      </el-table></div>
      <el-pagination v-if="(data?.total || 0) > 20" v-model:current-page="page" :total="data?.total || 0" :page-size="20" layout="prev, pager, next" aria-label="账户分页" />
    </section>
    <AccountEditDialog v-model:open="editOpen" :account="selected" @saved="reload" />
    <AccountAuditDialog v-model:open="auditOpen" :account="audited" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { workspaceAccess } from '@/api/workspaceAccess'
import { useAccessResource } from '@/composables/useAccessResource'
import { accessDate, accessPageNumber, accountStatusLabels, systemRoleLabels } from '@/utils/accessPresentation'
import type { Account, AccountStatus, SystemRole } from '@/types/access'
import AccountEditDialog from '@/components/access/AccountEditDialog.vue'
import AccountAuditDialog from '@/components/access/AccountAuditDialog.vue'
import '@/styles/access.css'

const route = useRoute()
const router = useRouter()
const queryText = computed(() => typeof route.query.q === 'string' ? route.query.q.slice(0, 120) : '')
const queryStatus = computed(() => ['active', 'disabled', 'pending'].includes(String(route.query.status)) ? String(route.query.status) : '')
const searchText = ref(queryText.value)
const statusFilter = ref(queryStatus.value)
const page = computed({ get: () => accessPageNumber(route.query.page), set: value => { void router.push({ query: { ...route.query, page: String(value) } }) } })
const { data, loading, error, reload } = useAccessResource(signal => workspaceAccess.accounts(page.value, queryText.value, queryStatus.value, signal))
watch([page, queryText, queryStatus], reload, { immediate: true })
watch([queryText, queryStatus], () => { searchText.value = queryText.value; statusFilter.value = queryStatus.value })
const selected = ref<Account | null>(null)
const audited = ref<Account | null>(null)
const editOpen = ref(false)
const auditOpen = ref(false)
function search() { void router.push({ query: { q: searchText.value.trim(), status: statusFilter.value, page: '1' } }) }
function edit(account: Account) { selected.value = account; editOpen.value = true }
function audit(account: Account) { audited.value = account; auditOpen.value = true }
</script>
