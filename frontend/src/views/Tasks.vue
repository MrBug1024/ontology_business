<template>
  <main class="task-page" aria-labelledby="task-page-title">
    <header class="task-header">
      <div>
        <div class="eyebrow">OPERATIONS</div>
        <h1 id="task-page-title">任务中心</h1>
        <p>跟踪工作流队列、重试和人工审批，所有状态均可回溯。</p>
      </div>
      <div class="task-header-actions">
        <el-button :loading="loading" @click="loadTasks()">
          <el-icon aria-hidden="true"><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </header>

    <section v-if="workflowIdFilter || returnTo" class="workflow-context card" aria-label="当前任务上下文">
      <div class="workflow-context-copy">
        <span class="eyebrow">CURRENT CONTEXT</span>
        <p v-if="workflowIdFilter">仅显示工作流 <code class="mono">{{ workflowIdFilter }}</code> 的任务。</p>
        <p v-if="returnTo">当前任务来自上一工作区，处理完成后可以原路返回。</p>
      </div>
      <div class="workflow-context-actions">
        <el-button v-if="returnTo" type="primary" plain @click="returnToWorkspace">返回上一工作区</el-button>
        <el-button v-if="workflowIdFilter" text @click="clearWorkflowContext">清除工作流范围</el-button>
        <el-button v-if="returnTo" text @click="clearReturnContext">清除返回位置</el-button>
      </div>
    </section>

    <section class="task-summary" aria-label="任务状态概览" aria-live="polite">
      <button class="summary-card summary-card--approval" type="button" @click="setStatusFilter('awaiting_approval')">
        <span class="summary-icon" aria-hidden="true"><el-icon><UserFilled /></el-icon></span>
        <span><b>{{ approvalCount }}</b><small>待审批</small></span>
      </button>
      <button class="summary-card summary-card--active" type="button" @click="setStatusFilter('running')">
        <span class="summary-icon" aria-hidden="true"><el-icon><Loading /></el-icon></span>
        <span><b>{{ activeCount }}</b><small>执行中 / 排队中</small></span>
      </button>
      <button class="summary-card summary-card--retry" type="button" @click="setStatusFilter('retry_waiting')">
        <span class="summary-icon" aria-hidden="true"><el-icon><RefreshRight /></el-icon></span>
        <span><b>{{ retryCount }}</b><small>等待重试</small></span>
      </button>
      <button class="summary-card summary-card--issue" type="button" @click="setStatusFilter('failed')">
        <span class="summary-icon" aria-hidden="true"><el-icon><WarningFilled /></el-icon></span>
        <span><b>{{ problemCount }}</b><small>失败或超时</small></span>
      </button>
    </section>

    <section class="task-filter card" aria-label="任务筛选">
      <el-select v-model="scenarioFilter" clearable filterable placeholder="全部业务场景" aria-label="按业务场景筛选" class="filter-scenario">
        <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
      </el-select>
      <el-select v-model="statusFilter" clearable placeholder="全部状态" aria-label="按任务状态筛选" class="filter-status">
        <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
      </el-select>
      <el-button type="primary" @click="applyFilters"><el-icon aria-hidden="true"><Filter /></el-icon> 应用筛选</el-button>
      <el-button text :disabled="!hasFilters" @click="clearFilters">清除</el-button>
    </section>

    <section class="task-list card" aria-label="任务列表">
      <el-table v-loading="loading" :data="visibleTasks" empty-text="暂无符合条件的任务" class="task-table">
        <el-table-column label="工作流" min-width="190">
          <template #default="{ row }">
            <button class="task-name" type="button" @click="showTask(row.id)">
              <b>{{ row.workflow_name || '未命名工作流' }}</b>
              <small>{{ triggerSourceLabel(row.trigger_source) }} · {{ shortId(row.id) }}</small>
            </button>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="150">
          <template #default="{ row }">
            <div class="status-stack">
              <el-tag size="small" effect="plain" :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
              <small>{{ statusMeta(row.status).description }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="104">
          <template #default="{ row }">
            <span class="attempt-label">第 {{ Math.max(row.attempt || 0, 1) }} / {{ Math.max(row.max_attempts || 0, 1) }} 次</span>
          </template>
        </el-table-column>
        <el-table-column label="计划 / 更新时间" min-width="158">
          <template #default="{ row }">
            <div class="time-stack">
              <span>{{ formatDate(row.scheduled_for || row.available_at || row.created_at) }}</span>
              <small>更新 {{ formatDate(row.updated_at || row.created_at) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="异常" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'task-error': row.error }">{{ row.error || (row.next_retry_at ? `下次重试：${formatDate(row.next_retry_at)}` : '—') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="205" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text :type="canApprove(row) ? 'primary' : undefined" @click="showTask(row.id)">
              {{ canApprove(row) ? '审批' : '详情' }}
            </el-button>
            <el-button v-if="canRetry(row)" size="small" text type="warning" @click="retryTask(row)">重试</el-button>
            <el-button v-if="canCancel(row)" size="small" text type="danger" :loading="cancellingTaskId === row.id" @click="cancelTask(row)">取消</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-drawer v-model="taskDrawer" class="task-detail-drawer" size="min(620px, 100vw)" :with-header="false" @closed="clearSelectedTaskQuery">
      <section v-if="selectedTask" class="task-detail" aria-labelledby="task-detail-title" v-loading="detailLoading">
        <header class="task-detail-header">
          <div>
            <div class="eyebrow">WORKFLOW RUN</div>
            <h3 id="task-detail-title">{{ selectedTask.workflow_name || '工作流任务' }}</h3>
            <p class="mono">{{ selectedTask.id }}</p>
          </div>
          <div class="task-detail-tools">
            <el-tag effect="plain" :type="statusMeta(selectedTask.status).type">{{ statusMeta(selectedTask.status).label }}</el-tag>
            <el-button class="drawer-close" text circle aria-label="关闭任务详情" title="关闭任务详情" @click="taskDrawer = false">
              <el-icon aria-hidden="true"><Close /></el-icon>
            </el-button>
          </div>
        </header>

        <el-alert v-if="selectedTask.error" class="task-error-alert" type="error" :closable="false" show-icon>
          <template #title>任务异常：{{ selectedTask.error }}</template>
          <template #default>检查执行详情后可重新提交；重试会沿用服务端定义的策略。</template>
        </el-alert>

        <section v-if="selectedTask.status === 'awaiting_approval'" class="approval-panel" aria-labelledby="approval-title">
          <div class="approval-panel-head">
            <div>
              <span class="eyebrow">HUMAN APPROVAL</span>
              <h4 id="approval-title">等待人工审批</h4>
            </div>
            <span v-if="activeApproval?.expires_at" class="approval-expiry">截止 {{ formatDate(activeApproval.expires_at) }}</span>
          </div>
          <p>{{ activeApproval?.instructions || '请核对本次任务的业务影响、参数和执行上下文，再作出决定。' }}</p>
          <dl v-if="activeApproval" class="approval-meta">
            <div><dt>审批节点</dt><dd>{{ activeApproval.node_name || activeApproval.node_id }}</dd></div>
            <div><dt>发起时间</dt><dd>{{ formatDate(activeApproval.requested_at) }}</dd></div>
          </dl>
          <div v-if="canApprove(selectedTask)" class="approval-actions">
            <el-button type="danger" plain :loading="approvalSubmitting" @click="requestApproval('reject')">驳回</el-button>
            <el-button type="primary" :loading="approvalSubmitting" @click="requestApproval('approve')">批准并继续</el-button>
          </div>
          <p v-else class="approval-readonly-hint" role="status">当前账号仅可查看审批上下文，没有审批权限。</p>
        </section>

        <section class="detail-grid" aria-label="任务信息">
          <div><span>触发来源</span><b>{{ triggerSourceLabel(selectedTask.trigger_source) }}</b></div>
          <div><span>定义依据</span><b>{{ selectedTask.definition_source === 'release' ? '已固定的场景定义' : '当前场景定义' }}</b></div>
          <div><span>执行尝试</span><b>{{ Math.max(selectedTask.attempt || 0, 1) }} / {{ Math.max(selectedTask.max_attempts || 0, 1) }}</b></div>
          <div><span>超时设置</span><b>{{ selectedTask.timeout_seconds || 0 }} 秒</b></div>
          <div><span>下次重试</span><b>{{ formatDate(selectedTask.next_retry_at) || '—' }}</b></div>
          <div><span>开始时间</span><b>{{ formatDate(selectedTask.started_at) || '—' }}</b></div>
          <div><span>完成时间</span><b>{{ formatDate(selectedTask.completed_at) || '—' }}</b></div>
        </section>

        <section class="detail-section">
          <h4>输入参数</h4>
          <StructuredValueViewer :value="selectedTask.input_params" empty-text="无需输入参数" />
        </section>
        <section v-if="selectedTask.result && Object.keys(selectedTask.result).length" class="detail-section">
          <h4>执行结果</h4>
          <StructuredValueViewer :value="selectedTask.result" empty-text="暂无执行结果" />
        </section>

        <footer v-if="canCancel(selectedTask) || canRetry(selectedTask)" class="task-detail-footer">
          <el-button v-if="canCancel(selectedTask)" type="danger" plain :loading="cancellingTaskId === selectedTask.id" @click="cancelTask(selectedTask)">取消任务</el-button>
          <el-button v-if="canRetry(selectedTask)" type="warning" plain :loading="retrySubmitting" @click="retryTask(selectedTask)">重新提交</el-button>
        </footer>
      </section>
    </el-drawer>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { Scenario, WorkflowApproval, WorkflowRun } from '@/types'
import StructuredValueViewer from '@/components/StructuredValueViewer.vue'

type StatusTagType = 'success' | 'warning' | 'danger' | 'info' | 'primary' | ''
type StatusMeta = { label: string; type: StatusTagType; description: string }

const route = useRoute()
const router = useRouter()
const tasks = ref<WorkflowRun[]>([])
const approvals = ref<WorkflowApproval[]>([])
const scenarios = ref<Scenario[]>([])
const loading = ref(false)
const detailLoading = ref(false)
const approvalSubmitting = ref(false)
const retrySubmitting = ref(false)
const cancellingTaskId = ref<string | null>(null)
const taskDrawer = ref(false)
const selectedTask = ref<WorkflowRun | null>(null)
const scenarioFilter = ref('')
const statusFilter = ref('')
const workflowIdFilter = ref('')
const returnTo = ref('')

const statusOptions = [
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '执行中' },
  { value: 'awaiting_approval', label: '等待审批' },
  { value: 'retry_waiting', label: '等待重试' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'timed_out', label: '超时' },
  { value: 'rejected', label: '已驳回' },
  { value: 'cancelled', label: '已取消' },
]

const STATUS_META: Record<string, StatusMeta> = {
  queued: { label: '排队中', type: 'info', description: '等待调度器领取' },
  running: { label: '执行中', type: 'primary', description: '正在处理流程节点' },
  awaiting_approval: { label: '等待审批', type: 'warning', description: '需要人工决定后继续' },
  retry_waiting: { label: '等待重试', type: 'warning', description: '将在退避时间后重试' },
  succeeded: { label: '已完成', type: 'success', description: '工作流已成功结束' },
  failed: { label: '失败', type: 'danger', description: '需要检查错误并决定是否重试' },
  timed_out: { label: '超时', type: 'danger', description: '超过允许执行时间' },
  rejected: { label: '已驳回', type: 'info', description: '审批人拒绝继续执行' },
  cancelled: { label: '已取消', type: 'info', description: '任务已停止' },
}

const visibleTasks = computed(() => workflowIdFilter.value
  ? tasks.value.filter((task) => task.workflow_id === workflowIdFilter.value)
  : tasks.value,
)
const activeCount = computed(() => tasks.value.filter((task) => ['queued', 'running'].includes(task.status)).length)
const approvalCount = computed(() => tasks.value.filter((task) => task.status === 'awaiting_approval').length || approvals.value.filter(isWaitingApproval).length)
const retryCount = computed(() => tasks.value.filter((task) => task.status === 'retry_waiting').length)
const problemCount = computed(() => tasks.value.filter((task) => ['failed', 'timed_out'].includes(task.status)).length)
const hasFilters = computed(() => Boolean(scenarioFilter.value || statusFilter.value))
const activeApproval = computed(() => approvalFor(selectedTask.value))

function queryValue(value: unknown): string {
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function safeReturnPath(value: unknown): string {
  const candidate = queryValue(value).trim()
  if (!candidate.startsWith('/') || candidate.startsWith('//') || candidate.includes('\\')) return ''
  try {
    const url = new URL(candidate, window.location.origin)
    if (url.origin !== window.location.origin) return ''
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return ''
  }
}
function statusMeta(status: string): StatusMeta {
  return STATUS_META[status] || { label: status || '未知', type: 'info', description: '服务端返回的任务状态' }
}
function triggerSourceLabel(source?: string | null) {
  return ({
    manual: '人工发起',
    agent: 'Agent 对话',
    event: '业务事件',
    workflow: '工作流',
    api: '外部系统',
    retry: '自动重试',
  } as Record<string, string>)[String(source || 'manual').toLowerCase()] || '系统触发'
}
function isWaitingApproval(approval: WorkflowApproval) {
  return ['pending', 'awaiting_approval', 'awaiting', 'requested'].includes(approval.status)
}
function approvalFor(task: WorkflowRun | null): WorkflowApproval | null {
  if (!task) return null
  if (task.pending_approval && typeof task.pending_approval === 'object') return task.pending_approval
  return approvals.value.find((approval) => approval.workflow_run_id === task.id && isWaitingApproval(approval)) || null
}
function canApprove(task: WorkflowRun | null) {
  return Boolean(task?.can_approve) && task?.status === 'awaiting_approval'
}
function canRetry(task: WorkflowRun) {
  return task.can_execute === true && ['failed', 'timed_out', 'cancelled'].includes(task.status)
}
function canCancel(task: WorkflowRun) {
  // A running task may have crossed an external boundary. The backend
  // deliberately rejects it rather than reporting a misleading cancellation.
  return task.can_execute === true && ['queued', 'retry_waiting', 'awaiting_approval'].includes(task.status)
}
function shortId(value: string) {
  return value.length > 10 ? `${value.slice(0, 8)}…` : value
}
function formatDate(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}
function isCancelled(error: unknown) {
  if (error === 'cancel' || error === 'close') return true
  const message = typeof error === 'object' && error ? (error as { message?: string }).message : ''
  return message === 'cancel' || message === 'close'
}
function updateTask(task: WorkflowRun) {
  const index = tasks.value.findIndex((item) => item.id === task.id)
  if (index >= 0) tasks.value.splice(index, 1, task)
  else tasks.value.unshift(task)
  selectedTask.value = task
}
function readRoute() {
  scenarioFilter.value = queryValue(route.query.scenario_id)
  statusFilter.value = queryValue(route.query.status)
  workflowIdFilter.value = queryValue(route.query.workflow_id)
  returnTo.value = safeReturnPath(route.query.return_to)
}
function baseQuery(taskId?: string) {
  return {
    scenario_id: scenarioFilter.value || undefined,
    status: statusFilter.value || undefined,
    workflow_id: workflowIdFilter.value || undefined,
    return_to: returnTo.value || undefined,
    task: taskId || undefined,
  }
}

async function loadApprovals() {
  try {
    approvals.value = await api.listTaskApprovals({ scenario_id: scenarioFilter.value || undefined })
  } catch {
    // 任务列表仍可独立展示；审批端点短暂不可用时不遮蔽运行状态。
  }
}
async function loadTasks(silent = false) {
  if (!silent) loading.value = true
  try {
    const freshTasks = await api.listTasks({
      scenario_id: scenarioFilter.value || undefined,
      status: statusFilter.value || undefined,
      limit: 100,
    })
    tasks.value = freshTasks
    // 保持已打开抽屉与轮询结果一致，避免列表完成而详情仍显示旧状态。
    if (selectedTask.value) {
      const refreshed = freshTasks.find((task) => task.id === selectedTask.value?.id)
      if (refreshed) selectedTask.value = refreshed
    }
    void loadApprovals()
  } catch (error: any) {
    if (!silent) ElMessage.error(error?.message || '任务列表加载失败')
  } finally {
    if (!silent) loading.value = false
  }
}
async function loadScenarios() {
  try {
    scenarios.value = await api.listScenarios()
  } catch {
    // 场景选择器无数据时仍允许通过 URL 查询任务。
  }
}
async function showTask(id: string, syncRoute = true) {
  taskDrawer.value = true
  detailLoading.value = true
  try {
    const task = await api.getTask(id)
    updateTask(task)
    if (syncRoute && queryValue(route.query.task) !== id) {
      await router.replace({ name: 'tasks', query: baseQuery(id) })
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '任务详情加载失败')
  } finally {
    detailLoading.value = false
  }
}
async function requestApproval(decision: 'approve' | 'reject') {
  const task = selectedTask.value
  if (!task || !canApprove(task)) {
    ElMessage.warning('当前账号没有审批此任务的权限')
    return
  }
  const isApprove = decision === 'approve'
  try {
    const { value } = await ElMessageBox.prompt(
      isApprove ? '可选：记录本次批准意见。' : '请说明驳回原因，方便发起人修正后重新提交。',
      isApprove ? '批准任务' : '驳回任务',
      {
        inputType: 'textarea',
        inputPlaceholder: isApprove ? '审批意见（可选）' : '请输入驳回原因',
        inputValidator: isApprove ? undefined : (input: string) => input.trim() ? true : '请输入驳回原因',
        confirmButtonText: isApprove ? '批准并继续' : '确认驳回',
        cancelButtonText: '取消',
        type: isApprove ? 'warning' : 'error',
      },
    )
    approvalSubmitting.value = true
    const updatedTask = isApprove
      ? await api.approveTask(task.id, value || '')
      : await api.rejectTask(task.id, value || '')
    updateTask(updatedTask)
    await loadApprovals()
    ElMessage.success(isApprove ? '任务已批准，正在继续执行' : '任务已驳回')
  } catch (error: any) {
    if (!isCancelled(error)) ElMessage.error(error?.message || '审批操作失败')
  } finally {
    approvalSubmitting.value = false
  }
}
async function retryTask(task: WorkflowRun) {
  if (!canRetry(task)) {
    ElMessage.warning(task.can_execute ? '此任务当前不能重新提交' : '当前账号没有重新提交此任务的权限')
    return
  }
  try {
    await ElMessageBox.confirm('将按该工作流的重试策略重新排队。确定继续吗？', '重新提交任务', {
      type: 'warning', confirmButtonText: '重新提交', cancelButtonText: '取消',
    })
    retrySubmitting.value = true
    const updated = await api.retryTask(task.id)
    updateTask(updated)
    ElMessage.success('任务已重新排队')
  } catch (error: any) {
    if (!isCancelled(error)) ElMessage.error(error?.message || '重新提交失败')
  } finally {
    retrySubmitting.value = false
  }
}
async function cancelTask(task: WorkflowRun) {
  if (!canCancel(task)) {
    ElMessage.warning(task.can_execute ? '此任务当前不能取消' : '当前账号没有取消此任务的权限')
    return
  }
  try {
    await ElMessageBox.confirm(
      '这会停止排队、重试或等待审批中的任务，并保留完整审计记录。已开始执行的外部调用不会在这里被强制中断。确定取消吗？',
      '取消任务',
      { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '返回' },
    )
    cancellingTaskId.value = task.id
    const updated = await api.cancelTask(task.id)
    updateTask(updated)
    await loadApprovals()
    ElMessage.success('任务已取消，审计记录已保留')
  } catch (error: any) {
    if (!isCancelled(error)) ElMessage.error(error?.message || '取消任务失败')
  } finally {
    cancellingTaskId.value = null
  }
}
async function applyFilters() {
  await router.replace({ name: 'tasks', query: baseQuery() })
}
function clearFilters() {
  scenarioFilter.value = ''
  statusFilter.value = ''
  void router.replace({ name: 'tasks', query: baseQuery() })
}
function clearWorkflowContext() {
  workflowIdFilter.value = ''
  void router.replace({ name: 'tasks', query: baseQuery() })
}
function clearReturnContext() {
  returnTo.value = ''
  void router.replace({ name: 'tasks', query: baseQuery() })
}
function returnToWorkspace() {
  const target = safeReturnPath(returnTo.value)
  if (!target) {
    clearReturnContext()
    ElMessage.warning('返回位置无效，已从任务上下文中移除')
    return
  }
  void router.push(target)
}
function setStatusFilter(status: string) {
  statusFilter.value = status
  void applyFilters()
}
function clearSelectedTaskQuery() {
  selectedTask.value = null
  if (queryValue(route.query.task)) void router.replace({ name: 'tasks', query: baseQuery() })
}

watch(() => route.fullPath, () => {
  readRoute()
  void loadTasks()
  const taskId = queryValue(route.query.task)
  if (taskId && taskId !== selectedTask.value?.id) void showTask(taskId, false)
}, { immediate: true })

let pollTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  void loadScenarios()
  pollTimer = setInterval(() => {
    if (!document.hidden && tasks.value.some((task) => !['succeeded', 'failed', 'timed_out', 'rejected', 'cancelled'].includes(task.status))) {
      void loadTasks(true)
    }
  }, 4000)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.task-page { min-height: 100%; padding: 24px 28px 32px; }
.task-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.eyebrow { color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .15em; }
.task-header h1 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.task-header p { margin: 0; color: var(--text-2); font-size: 13px; }
.workflow-context { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; padding: 13px 14px; border-color: color-mix(in srgb, var(--primary) 28%, var(--border)); background: color-mix(in srgb, var(--primary-soft) 62%, var(--surface)); }
.workflow-context-copy { min-width: 0; }
.workflow-context-copy p { margin: 4px 0 0; color: var(--text-2); font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
.workflow-context-copy code { color: var(--primary-600); font-size: 11px; }
.workflow-context-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.task-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.summary-card { min-height: 84px; display: flex; align-items: center; gap: 11px; padding: 14px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--text); cursor: pointer; text-align: left; box-shadow: var(--shadow-xs); transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.summary-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.summary-card:focus-visible, .task-name:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }
.summary-icon { width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center; border-radius: 11px; font-size: 18px; }
.summary-card span:last-child { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.summary-card b { font-size: 21px; line-height: 1.1; }
.summary-card small { color: var(--text-2); font-size: 11px; font-weight: 650; }
.summary-card--approval .summary-icon { color: var(--warning); background: var(--warning-soft); }
.summary-card--active .summary-icon { color: var(--primary); background: var(--primary-soft); }
.summary-card--retry .summary-icon { color: var(--info); background: var(--info-soft); }
.summary-card--issue .summary-icon { color: var(--danger); background: var(--danger-soft); }
.task-filter { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; padding: 12px; }
.filter-scenario { width: min(280px, 100%); }
.filter-status { width: 160px; }
.task-list { min-height: 420px; overflow: hidden; }
.task-table { width: 100%; }
.task-name { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; max-width: 100%; padding: 2px 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.task-name b { color: var(--text); font-size: 13px; }
.task-name small, .status-stack small, .time-stack small { color: var(--text-3); font-size: 11px; }
.status-stack, .time-stack { display: flex; flex-direction: column; gap: 4px; }
.attempt-label { color: var(--text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.task-error { color: var(--danger); }
.mono { font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; }
.task-detail-drawer :deep(.el-drawer__body) { padding: 0; scroll-padding-bottom: 88px; }
.task-detail { min-height: 100%; padding: 26px; background: var(--surface); }
.task-detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 18px; }
.task-detail-header h3 { margin: 5px 0; color: var(--text); font-size: 20px; letter-spacing: -.025em; }
.task-detail-header p { margin: 0; color: var(--text-3); font-size: 10px; overflow-wrap: anywhere; }
.task-detail-tools { display: flex; flex: 0 0 auto; align-items: center; gap: 7px; }
.drawer-close { min-width: 44px; min-height: 44px; margin: -9px -10px -9px 0; color: var(--text-2); }
.task-error-alert { margin-bottom: 14px; }
.approval-panel { margin-bottom: 16px; padding: 16px; border: 1px solid color-mix(in srgb, var(--warning) 36%, var(--border)); border-radius: 14px; background: var(--warning-soft); }
.approval-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.approval-panel h4 { margin: 4px 0 0; color: var(--text); font-size: 15px; }
.approval-panel p { margin: 12px 0; color: var(--text-2); font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
.approval-expiry { color: var(--text-2); font-size: 11px; text-align: right; }
.approval-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin: 0 0 14px; }
.approval-meta div { min-width: 0; }
.approval-meta dt { color: var(--text-3); font-size: 10px; }
.approval-meta dd { margin: 3px 0 0; color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
.approval-actions { display: flex; justify-content: flex-end; gap: 8px; }
.approval-readonly-hint { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.55; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-bottom: 18px; }
.detail-grid div { min-width: 0; padding: 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.detail-grid span { display: block; color: var(--text-3); font-size: 10px; }
.detail-grid b { display: block; margin-top: 4px; color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
.detail-section { margin-top: 14px; }
.detail-section h4 { margin: 0 0 7px; color: var(--text-2); font-size: 12px; }
.detail-code { max-height: 260px; margin: 0; padding: 12px; overflow: auto; border-radius: 10px; background: #1d2930; color: #e2e8f0; font-size: 11px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
.task-detail-footer { position: sticky; bottom: 0; z-index: 2; display: flex; justify-content: flex-end; gap: 8px; margin: 22px -26px -26px; padding: 14px 26px max(14px, env(safe-area-inset-bottom)); border-top: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 96%, transparent); backdrop-filter: blur(12px); }
@media (max-width: 960px) { .task-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .task-page { padding: 18px 14px 22px; } .task-header, .workflow-context { align-items: flex-start; flex-direction: column; } .workflow-context-actions { width: 100%; justify-content: flex-start; } .task-filter { flex-wrap: wrap; } .filter-scenario, .filter-status { flex: 1 1 180px; } }
@media (max-width: 460px) { .task-summary, .detail-grid, .approval-meta { grid-template-columns: 1fr; } .task-detail-header, .approval-panel-head { flex-direction: column; } .task-detail-tools { width: 100%; justify-content: space-between; } .approval-expiry { text-align: left; } }
</style>
