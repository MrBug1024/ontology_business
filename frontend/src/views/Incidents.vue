<template>
  <main class="incident-page" aria-labelledby="incident-page-title">
    <header class="incident-header">
      <div>
        <span class="eyebrow">OPERATIONS CASES</span>
        <h1 id="incident-page-title">事件中心</h1>
        <p>在场景范围内记录、确认和闭环异常事项。所有状态变化和说明都会保留为可追溯历史。</p>
      </div>
      <div class="incident-header-actions">
        <el-select
          v-model="scenarioId"
          class="scenario-select"
          aria-label="选择业务场景"
          :disabled="initialLoading || !scenarios.length"
          placeholder="选择业务场景"
          @change="selectScenario"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button :loading="initialLoading || casesLoading" :disabled="!scenarioId" @click="loadCases">
          <el-icon aria-hidden="true"><Refresh /></el-icon> 刷新
        </el-button>
        <el-button
          type="primary"
          :disabled="!scenarioId || listForbidden || scenarioReadOnly || scenarioPermissionChecking"
          :title="scenarioPermissionChecking ? '正在核验当前场景的 Case 写入权限' : scenarioReadOnly ? '当前账号仅有此场景的 Case 查看权限' : ''"
          @click="openCreateDialog"
        >
          <el-icon aria-hidden="true"><Plus /></el-icon> 新建 Case
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="pageError"
      class="page-alert"
      type="error"
      :title="pageError"
      show-icon
      :closable="false"
      role="alert"
    >
      <template #default><el-button size="small" type="primary" plain @click="initialize">重新加载</el-button></template>
    </el-alert>

    <section v-if="initialLoading && !scenarios.length" class="loading-card card" aria-live="polite" aria-label="正在加载可访问场景">
      <el-skeleton :rows="7" animated />
    </section>

    <section v-else-if="!scenarios.length && !pageError" class="empty-card card" aria-labelledby="incident-scenario-empty-title">
      <el-icon aria-hidden="true" :size="30"><OfficeBuilding /></el-icon>
      <h3 id="incident-scenario-empty-title">暂无可处理的业务场景</h3>
      <p>事件 Case 仅属于当前租户内、你有读取权限的场景。获得场景访问权限后即可在这里查看运营事项。</p>
    </section>

    <template v-else-if="scenarioId">
      <el-alert
        v-if="scenarioReadOnly"
        class="page-alert"
        type="warning"
        :title="scenarioPermissionChecking ? '正在核验当前场景权限' : '当前场景为只读模式'"
        :description="scenarioPermissionChecking ? '为避免误触写入，Case 操作暂时保持只读；核验完成后会自动更新。' : '你可以查看 Case 详情和历史；服务端已拒绝写入操作，因此新建、编辑、确认和解决均已禁用。权限调整后可重新检查。'"
        show-icon
        :closable="false"
        role="status"
      >
        <template #default><el-button v-if="!scenarioPermissionChecking" size="small" plain @click="retryWritePermission">重新检查权限</el-button></template>
      </el-alert>

      <section class="incident-summary" aria-label="Case 状态概览" aria-live="polite">
        <button class="summary-card summary-card--open" type="button" :aria-pressed="statusFilter === 'open'" @click="setStatusFilter('open')">
          <span class="summary-icon" aria-hidden="true"><el-icon><WarningFilled /></el-icon></span>
          <span><b>{{ openCount }}</b><small>待处理</small></span>
          <p>尚未确认的异常事项</p>
        </button>
        <button class="summary-card summary-card--acknowledged" type="button" :aria-pressed="statusFilter === 'acknowledged'" @click="setStatusFilter('acknowledged')">
          <span class="summary-icon" aria-hidden="true"><el-icon><CircleCheck /></el-icon></span>
          <span><b>{{ acknowledgedCount }}</b><small>处理中</small></span>
          <p>已确认，等待处理闭环</p>
        </button>
        <button class="summary-card summary-card--resolved" type="button" :aria-pressed="statusFilter === 'resolved'" @click="setStatusFilter('resolved')">
          <span class="summary-icon" aria-hidden="true"><el-icon><Finished /></el-icon></span>
          <span><b>{{ resolvedCount }}</b><small>已解决</small></span>
          <p>已记录解决说明和责任人</p>
        </button>
        <button class="summary-card summary-card--critical" type="button" :aria-pressed="severityFilter === 'critical'" @click="setSeverityFilter('critical')">
          <span class="summary-icon" aria-hidden="true"><el-icon><Warning /></el-icon></span>
          <span><b>{{ criticalCount }}</b><small>严重</small></span>
          <p>需要优先关注的 Case</p>
        </button>
      </section>

      <section class="incident-filter card" aria-label="Case 筛选">
        <el-select v-model="statusFilter" clearable placeholder="全部状态" aria-label="按 Case 状态筛选" class="filter-select">
          <el-option v-for="option in STATUS_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="severityFilter" clearable placeholder="全部严重级别" aria-label="按严重级别筛选" class="filter-select">
          <el-option v-for="option in SEVERITY_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-button type="primary" :loading="casesLoading" @click="loadCases"><el-icon aria-hidden="true"><Filter /></el-icon> 应用筛选</el-button>
        <el-button text :disabled="!hasFilters" @click="clearFilters">清除</el-button>
      </section>

      <section class="incident-list card" aria-labelledby="incident-list-title" v-loading="casesLoading">
        <header class="section-header">
          <div>
            <span class="section-kicker">SCENARIO CASES</span>
            <h3 id="incident-list-title">{{ selectedScenario?.name || '当前场景' }} 的 Case</h3>
          </div>
          <span class="case-count" role="status" aria-atomic="true">当前显示 {{ incidents.length }} 条</span>
        </header>

        <section v-if="listForbidden" class="access-card" aria-labelledby="incident-access-title">
          <el-icon aria-hidden="true" :size="28"><Lock /></el-icon>
          <div>
            <h4 id="incident-access-title">无法读取此场景的 Case</h4>
            <p>{{ listError || '该场景的异常事项不随公开内容共享。请申请此场景的读取权限后重试。' }}</p>
          </div>
          <el-button @click="loadCases"><el-icon aria-hidden="true"><Refresh /></el-icon>重试</el-button>
        </section>
        <el-alert v-else-if="listError" class="list-alert" type="error" :title="listError" show-icon :closable="false" role="alert">
          <template #default><el-button size="small" plain @click="loadCases">重新加载</el-button></template>
        </el-alert>
        <el-table v-else :data="incidents" class="incident-table" empty-text="暂无符合条件的 Case">
          <el-table-column label="Case" min-width="250">
            <template #default="{ row }">
              <button class="case-title" type="button" :aria-label="`查看 Case：${row.title}`" @click="openIncident(row.id)">
                <b>{{ row.title }}</b>
                <small>{{ sourceLabel(row.source) }} · {{ shortId(row.id) }}</small>
              </button>
            </template>
          </el-table-column>
          <el-table-column label="级别" width="96">
            <template #default="{ row }"><el-tag size="small" effect="plain" :type="severityMeta(row.severity).type">{{ severityMeta(row.severity).label }}</el-tag></template>
          </el-table-column>
          <el-table-column label="状态" min-width="136">
            <template #default="{ row }">
              <div class="state-stack">
                <el-tag size="small" effect="plain" :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
                <small>{{ statusMeta(row.status).description }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="负责人" min-width="135" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono muted-id">{{ shortId(row.assignee_user_id) || '未分派' }}</span></template>
          </el-table-column>
          <el-table-column label="更新" min-width="148">
            <template #default="{ row }"><div class="time-stack"><span>{{ formatDate(row.updated_at) || '—' }}</span><small>{{ row.history_count }} 条历史</small></div></template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" text type="primary" @click="openIncident(row.id)">详情</el-button></template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <el-dialog v-model="createDialog" title="新建 Case" width="min(680px, calc(100vw - 28px))" destroy-on-close @closed="resetCreateForm">
      <el-form label-position="top">
        <div v-if="createError" ref="createErrorRef" class="dialog-error" role="alert" tabindex="-1"><el-alert type="error" :title="createError" :closable="false" show-icon /></div>
        <el-form-item label="Case 标题" required :error="createTitleError">
          <el-input v-model="createForm.title" maxlength="300" show-word-limit placeholder="简明描述异常或需要跟进的事项" aria-describedby="create-title-help" @input="createTitleError = ''" />
          <div id="create-title-help" class="field-help">标题会出现在运营列表和不可变历史中。</div>
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="严重级别" required>
            <el-select v-model="createForm.severity" style="width:100%" aria-label="选择严重级别">
              <el-option v-for="option in SEVERITY_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="来源" required>
            <el-select v-model="createForm.source" style="width:100%" aria-label="选择 Case 来源">
              <el-option v-for="option in SOURCE_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="异常描述">
          <el-input v-model="createForm.description" type="textarea" :rows="4" maxlength="12000" show-word-limit placeholder="说明影响、发现方式、已知上下文或后续处理建议" />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="负责人用户 ID（可选）">
            <el-input v-model.trim="createForm.assignee_user_id" maxlength="32" placeholder="输入当前组织有效成员的用户 ID" />
            <div class="field-help with-action">成员目录受独立权限保护；可输入稳定 ID，或使用“指派给我”。<el-button v-if="auth.user?.id" text type="primary" @click="assignToMe(createForm)">指派给我</el-button></div>
          </el-form-item>
          <el-form-item label="关联对象（可选）">
            <el-select v-model="createForm.related_object_id" filterable remote allow-create clearable reserve-keyword :remote-method="queueObjectSearch" :loading="objectsLoading" style="width:100%" placeholder="搜索对象名称，或粘贴对象 ID">
              <el-option v-for="object in relatedObjects" :key="object.id" :label="`${object.name} · ${object.entity_name || '对象'}`" :value="object.id" />
            </el-select>
            <div class="field-help">仅显示当前场景中可读取的前 50 个匹配对象；服务端会再次校验关联关系。</div>
          </el-form-item>
        </div>
        <el-form-item label="来源引用（可选）">
          <el-input v-model="createForm.source_ref" maxlength="180" placeholder="例如规则编号、工作流运行 ID 或外部工单号" />
        </el-form-item>
        <el-form-item label="创建说明（可选）">
          <el-input v-model="createForm.comment" type="textarea" :rows="2" maxlength="2000" show-word-limit placeholder="这条说明会作为首条审计历史保留" />
        </el-form-item>
        <el-alert v-if="objectsError" type="warning" :title="objectsError" :closable="false" show-icon />
      </el-form>
      <template #footer>
        <el-button :disabled="creating" @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createIncident">创建 Case</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editDialog" title="编辑 Case" width="min(680px, calc(100vw - 28px))" destroy-on-close @closed="resetEditForm">
      <el-form label-position="top">
        <div v-if="editError" ref="editErrorRef" class="dialog-error" role="alert" tabindex="-1"><el-alert type="error" :title="editError" :closable="false" show-icon /></div>
        <el-form-item label="Case 标题" required :error="editTitleError">
          <el-input v-model="editForm.title" maxlength="300" show-word-limit @input="editTitleError = ''" />
        </el-form-item>
        <el-form-item label="异常描述">
          <el-input v-model="editForm.description" type="textarea" :rows="4" maxlength="12000" show-word-limit />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="严重级别" required>
            <el-select v-model="editForm.severity" style="width:100%" aria-label="编辑严重级别"><el-option v-for="option in SEVERITY_OPTIONS" :key="option.value" :label="option.label" :value="option.value" /></el-select>
          </el-form-item>
          <el-form-item label="负责人用户 ID（可选）">
            <el-input v-model.trim="editForm.assignee_user_id" maxlength="32" placeholder="输入当前组织有效成员的用户 ID" />
            <div class="field-help with-action"><span>清空可取消分派。</span><el-button v-if="auth.user?.id" text type="primary" @click="assignToMe(editForm)">指派给我</el-button></div>
          </el-form-item>
        </div>
        <el-form-item label="关联对象（可选）">
          <el-select v-model="editForm.related_object_id" filterable remote allow-create clearable reserve-keyword :remote-method="queueObjectSearch" :loading="objectsLoading" style="width:100%" placeholder="搜索对象名称，或粘贴对象 ID">
            <el-option v-for="object in relatedObjects" :key="object.id" :label="`${object.name} · ${object.entity_name || '对象'}`" :value="object.id" />
          </el-select>
          <div class="field-help">清空可移除关联对象。已有对象在权限变化后可能仅显示其稳定 ID。</div>
        </el-form-item>
        <el-form-item label="变更说明（可选）">
          <el-input v-model="editForm.comment" type="textarea" :rows="2" maxlength="2000" show-word-limit placeholder="说明本次变更原因，便于后续审计" />
        </el-form-item>
        <el-alert v-if="objectsError" type="warning" :title="objectsError" :closable="false" show-icon />
      </el-form>
      <template #footer>
        <el-button :disabled="savingEdit" @click="editDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingEdit" @click="saveEdit">保存变更</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resolveDialog" title="解决 Case" width="min(620px, calc(100vw - 28px))" destroy-on-close @closed="resetResolveForm">
      <el-form label-position="top">
        <div v-if="resolveError" ref="resolveErrorRef" class="dialog-error" role="alert" tabindex="-1"><el-alert type="error" :title="resolveError" :closable="false" show-icon /></div>
        <el-form-item label="解决说明" required :error="resolveResolutionError">
          <el-input v-model="resolveForm.resolution" type="textarea" :rows="5" maxlength="12000" show-word-limit placeholder="记录根因、处置措施和验证结论" @input="resolveResolutionError = ''" />
        </el-form-item>
        <el-form-item label="补充审计说明（可选）">
          <el-input v-model="resolveForm.comment" type="textarea" :rows="2" maxlength="2000" show-word-limit placeholder="例如通知范围、复盘链接或后续动作" />
        </el-form-item>
        <el-alert type="info" title="解决后 Case 将不可直接编辑或再次确认；如有新问题，请新建后续 Case。" :closable="false" show-icon />
      </el-form>
      <template #footer>
        <el-button :disabled="resolving" @click="resolveDialog = false">取消</el-button>
        <el-button type="success" :loading="resolving" @click="resolveIncident">确认解决</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailDrawer" class="incident-detail-drawer" size="min(700px, 100vw)" :with-header="false" @closed="clearSelectedIncident">
      <section v-if="selectedIncident" class="incident-detail" aria-labelledby="incident-detail-title" v-loading="detailLoading">
        <header class="detail-header">
          <div>
            <span class="eyebrow">CASE DETAIL</span>
            <h3 id="incident-detail-title">{{ selectedIncident.title }}</h3>
            <p class="mono">{{ selectedIncident.id }}</p>
          </div>
          <div class="detail-tools">
            <div class="detail-tags">
              <el-tag effect="plain" :type="severityMeta(selectedIncident.severity).type">{{ severityMeta(selectedIncident.severity).label }}</el-tag>
              <el-tag effect="plain" :type="statusMeta(selectedIncident.status).type">{{ statusMeta(selectedIncident.status).label }}</el-tag>
            </div>
            <el-button class="drawer-close" text circle aria-label="关闭 Case 详情" title="关闭 Case 详情" @click="detailDrawer = false">
              <el-icon aria-hidden="true"><Close /></el-icon>
            </el-button>
          </div>
        </header>

        <el-alert v-if="detailError" class="detail-alert" type="error" :title="detailError" :closable="false" show-icon role="alert" />
        <el-alert v-else-if="scenarioReadOnly" class="detail-alert" type="warning" title="只读模式：无权修改此场景的 Case" :closable="false" show-icon role="status" />
        <el-alert v-else-if="selectedIncident.status === 'resolved'" class="detail-alert" type="success" title="此 Case 已解决并锁定" description="解决说明和历史记录会保留；如出现后续问题，请新建新的 Case。" :closable="false" show-icon />

        <template v-if="!detailError">
          <section class="case-facts" aria-label="Case 基本信息">
            <div><span>所属场景</span><b>{{ selectedScenario?.name || shortId(selectedIncident.scenario_id) }}</b></div>
            <div><span>来源</span><b>{{ sourceLabel(selectedIncident.source) }}</b></div>
            <div><span>负责人</span><b class="mono">{{ shortId(selectedIncident.assignee_user_id) || '未分派' }}</b></div>
            <div><span>关联对象</span><b class="mono">{{ shortId(selectedIncident.related_object_id) || '未关联' }}</b></div>
            <div><span>创建人</span><b class="mono">{{ shortId(selectedIncident.created_by_user_id) || '—' }}</b></div>
            <div><span>最后更新</span><b>{{ formatDate(selectedIncident.updated_at) || '—' }}</b></div>
          </section>

          <section v-if="selectedIncident.description" class="detail-section">
            <h4>异常描述</h4>
            <p class="detail-copy">{{ redactDisplayText(selectedIncident.description) }}</p>
          </section>
          <section v-if="selectedIncident.source_ref" class="detail-section">
            <h4>来源引用</h4>
            <p class="detail-copy mono">{{ redactDisplayText(selectedIncident.source_ref) }}</p>
          </section>
          <section v-if="selectedIncident.status === 'acknowledged'" class="acknowledgement-panel" aria-label="确认信息">
            <el-icon aria-hidden="true"><CircleCheck /></el-icon>
            <span>已由 <b class="mono">{{ shortId(selectedIncident.acknowledged_by_user_id) || '—' }}</b> 于 {{ formatDate(selectedIncident.acknowledged_at) || '—' }} 确认。</span>
          </section>
          <section v-if="selectedIncident.status === 'resolved'" class="resolution-panel" aria-labelledby="resolution-title">
            <div class="resolution-heading"><el-icon aria-hidden="true"><Finished /></el-icon><h4 id="resolution-title">解决说明</h4></div>
            <p>{{ redactDisplayText(selectedIncident.resolution) || '服务端未返回解决说明。' }}</p>
            <small>由 <span class="mono">{{ shortId(selectedIncident.resolved_by_user_id) || '—' }}</span> 于 {{ formatDate(selectedIncident.resolved_at) || '—' }} 解决</small>
          </section>

          <section class="history-section" aria-labelledby="incident-history-title">
            <div class="detail-section-heading">
              <div><span class="section-kicker">AUDIT TRAIL</span><h4 id="incident-history-title">变更历史</h4></div>
              <el-button text :loading="historyLoading" @click="loadHistory(selectedIncident.id)"><el-icon aria-hidden="true"><Refresh /></el-icon>刷新历史</el-button>
            </div>
            <el-alert v-if="historyError" type="warning" :title="historyError" :closable="false" show-icon role="status" />
            <el-timeline v-else class="history-timeline">
              <el-timeline-item v-for="entry in history" :key="entry.id" :type="historyMeta(entry.action).type" :timestamp="formatDate(entry.created_at)" placement="top">
                <article class="history-entry">
                  <header><b>{{ historyMeta(entry.action).label }}</b><span class="mono">{{ shortId(entry.actor_user_id) || '系统' }}</span></header>
                  <p v-if="entry.from_status || entry.to_status" class="history-transition">{{ statusLabel(entry.from_status) || '—' }} → {{ statusLabel(entry.to_status) || '—' }}</p>
                  <p v-if="entry.comment" class="history-comment">{{ redactDisplayText(entry.comment) }}</p>
                  <pre v-if="Object.keys(entry.changes || {}).length" class="history-changes mono">{{ formatChanges(entry.changes) }}</pre>
                </article>
              </el-timeline-item>
              <li v-if="!history.length && !historyLoading" class="history-empty">暂无历史记录</li>
            </el-timeline>
          </section>
        </template>

        <footer v-if="!detailError && (canEdit(selectedIncident) || canAcknowledge(selectedIncident) || canResolve(selectedIncident))" class="detail-footer">
          <el-button v-if="canEdit(selectedIncident)" @click="openEditDialog"><el-icon aria-hidden="true"><EditPen /></el-icon>编辑</el-button>
          <el-button v-if="canAcknowledge(selectedIncident)" type="primary" plain :loading="acknowledging" @click="acknowledgeIncident"><el-icon aria-hidden="true"><CircleCheck /></el-icon>确认 Case</el-button>
          <el-button v-if="canResolve(selectedIncident)" type="success" :loading="resolving" @click="openResolveDialog"><el-icon aria-hidden="true"><Finished /></el-icon>解决 Case</el-button>
        </footer>
      </section>
    </el-drawer>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  IncidentCase,
  IncidentCaseCreateInput,
  IncidentCaseHistory,
  IncidentCaseUpdateInput,
  IncidentSeverity,
  IncidentStatus,
  ObjectSearchItem,
  Scenario,
} from '@/types'

type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary' | ''
type StatusMeta = { label: string; type: TagType; description: string }
type SeverityMeta = { label: string; type: TagType }
type IncidentForm = {
  title: string
  description: string
  severity: IncidentSeverity
  source: string
  source_ref: string
  related_object_id: string
  assignee_user_id: string
  comment: string
}

const STATUS_OPTIONS: Array<{ value: IncidentStatus; label: string }> = [
  { value: 'open', label: '待处理' },
  { value: 'acknowledged', label: '已确认' },
  { value: 'resolved', label: '已解决' },
]
const SEVERITY_OPTIONS: Array<{ value: IncidentSeverity; label: string }> = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'critical', label: '严重' },
]
const SOURCE_OPTIONS = [
  { value: 'manual', label: '人工发现' },
  { value: 'rule', label: '规则触发' },
  { value: 'workflow', label: '工作流' },
  { value: 'agent', label: 'Agent' },
  { value: 'import', label: '导入' },
]
const STATUS_META: Record<string, StatusMeta> = {
  open: { label: '待处理', type: 'danger', description: '尚未确认归属' },
  acknowledged: { label: '已确认', type: 'warning', description: '正在处理或等待闭环' },
  resolved: { label: '已解决', type: 'success', description: '已记录解决结论' },
}
const SEVERITY_META: Record<string, SeverityMeta> = {
  low: { label: '低', type: 'info' },
  medium: { label: '中', type: 'warning' },
  high: { label: '高', type: 'danger' },
  critical: { label: '严重', type: 'danger' },
}
const HISTORY_META: Record<string, { label: string; type: TagType }> = {
  created: { label: '创建 Case', type: 'primary' },
  updated: { label: '更新 Case', type: 'info' },
  acknowledged: { label: '确认 Case', type: 'warning' },
  resolved: { label: '解决 Case', type: 'success' },
}
const SENSITIVE_KEY_PATTERN = /secret|password|passwd|token|credential|api[_-]?key|authorization|bearer|private[_-]?key|access[_-]?key|client[_-]?secret|signature|cookie|session/i
const INLINE_SECRET_PATTERN = /((?:["']?(?:api[_\s-]?key|access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|client[_\s-]?secret|private[_\s-]?key|authorization|bearer|secret|password|passwd|credential|token|signature|cookie|session(?:[_\s-]?id)?)["']?\s*[:=]\s*)(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;)\]}]+))/gi
const QUERY_SECRET_PATTERN = /([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|token|secret|password|credential|signature|sig|key)=)[^&#\s]+/gi
const OPAQUE_SECRET_PATTERN = /\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const scenarios = ref<Scenario[]>([])
const scenarioId = ref('')
const incidents = ref<IncidentCase[]>([])
const initialLoading = ref(false)
const casesLoading = ref(false)
const pageError = ref('')
const listError = ref('')
const listForbidden = ref(false)
const readOnlyScenarioIds = ref<Record<string, true>>({})
const scenarioPermissionCheckingId = ref('')
let scenarioPermissionRequest = 0
const statusFilter = ref<IncidentStatus | ''>('')
const severityFilter = ref<IncidentSeverity | ''>('')

const createDialog = ref(false)
const creating = ref(false)
const createError = ref('')
const createTitleError = ref('')
const createErrorRef = ref<HTMLElement | null>(null)
const createForm = ref<IncidentForm>(newIncidentForm())

const editDialog = ref(false)
const savingEdit = ref(false)
const editError = ref('')
const editTitleError = ref('')
const editErrorRef = ref<HTMLElement | null>(null)
const editForm = ref<IncidentForm>(newIncidentForm())

const resolveDialog = ref(false)
const resolving = ref(false)
const resolveError = ref('')
const resolveResolutionError = ref('')
const resolveErrorRef = ref<HTMLElement | null>(null)
const resolveForm = ref({ resolution: '', comment: '' })
const acknowledging = ref(false)

const relatedObjects = ref<ObjectSearchItem[]>([])
const objectsLoading = ref(false)
const objectsError = ref('')
let objectSearchTimer: ReturnType<typeof setTimeout> | undefined
let objectSearchRequest = 0

const detailDrawer = ref(false)
const selectedIncident = ref<IncidentCase | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const history = ref<IncidentCaseHistory[]>([])
const historyLoading = ref(false)
const historyError = ref('')

const selectedScenario = computed(() => scenarios.value.find((scenario) => scenario.id === scenarioId.value) || null)
const scenarioReadOnly = computed(() => Boolean(readOnlyScenarioIds.value[scenarioId.value]))
const scenarioPermissionChecking = computed(() => scenarioPermissionCheckingId.value === scenarioId.value)
const hasFilters = computed(() => Boolean(statusFilter.value || severityFilter.value))
const openCount = computed(() => incidents.value.filter((incident) => incident.status === 'open').length)
const acknowledgedCount = computed(() => incidents.value.filter((incident) => incident.status === 'acknowledged').length)
const resolvedCount = computed(() => incidents.value.filter((incident) => incident.status === 'resolved').length)
const criticalCount = computed(() => incidents.value.filter((incident) => incident.severity === 'critical').length)

function newIncidentForm(): IncidentForm {
  return {
    title: '', description: '', severity: 'medium', source: 'manual', source_ref: '',
    related_object_id: '', assignee_user_id: '', comment: '',
  }
}
function queryValue(value: unknown): string {
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function errorMessage(cause: unknown, fallback: string) {
  const error = cause as { message?: string; response?: { data?: { detail?: string } } }
  return error?.response?.data?.detail || error?.message || fallback
}
function errorStatus(cause: unknown) {
  const error = cause as { status?: number; response?: { status?: number } }
  return Number(error?.status || error?.response?.status || 0)
}
function isDismissal(cause: unknown) {
  if (cause === 'cancel' || cause === 'close') return true
  const error = cause as { message?: string }
  return error?.message === 'cancel' || error?.message === 'close'
}
function statusMeta(status?: string): StatusMeta {
  return STATUS_META[status || ''] || { label: status || '未知', type: 'info', description: '服务端返回的状态' }
}
function severityMeta(severity?: string): SeverityMeta {
  return SEVERITY_META[severity || ''] || { label: severity || '未知', type: 'info' }
}
function historyMeta(action?: string) {
  return HISTORY_META[action || ''] || { label: action || '记录', type: 'info' as TagType }
}
function statusLabel(status?: string) { return statusMeta(status).label }
function sourceLabel(source?: string) {
  return SOURCE_OPTIONS.find((option) => option.value === source)?.label || source || '人工发现'
}
function shortId(value?: string | null) {
  if (!value) return ''
  return value.length > 12 ? `${value.slice(0, 8)}…` : value
}
function formatDate(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}
function normalizeOptional(value: string) { return value.trim() || null }
function isSensitiveKey(key: string) { return SENSITIVE_KEY_PATTERN.test(key) }
function redactDisplayText(value: string) {
  return value
    .replace(/-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----/gi, '[已脱敏私钥]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [已脱敏]')
    .replace(INLINE_SECRET_PATTERN, '$1[已脱敏]')
    .replace(QUERY_SECRET_PATTERN, '$1[已脱敏]')
    .replace(OPAQUE_SECRET_PATTERN, '[已脱敏]')
}
function sanitizeForDisplay(value: unknown, depth = 0, seen = new WeakSet<object>()): unknown {
  if (typeof value === 'string') return redactDisplayText(value)
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value !== 'object') return redactDisplayText(String(value))
  if (seen.has(value)) return '[循环引用已省略]'
  if (depth >= 4) return Array.isArray(value) ? `[已折叠的数组，${value.length} 项]` : '[已折叠的嵌套对象]'
  seen.add(value)
  if (Array.isArray(value)) {
    const values = value.slice(0, 24).map((item) => sanitizeForDisplay(item, depth + 1, seen))
    if (value.length > values.length) values.push(`[另有 ${value.length - values.length} 项已省略]`)
    return values
  }
  const result: Record<string, unknown> = {}
  let omitted = 0
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (isSensitiveKey(key) || Object.keys(result).length >= 24) {
      omitted += 1
      continue
    }
    result[key] = sanitizeForDisplay(item, depth + 1, seen)
  }
  if (omitted) result['…'] = `[已省略 ${omitted} 个敏感或多余字段]`
  return result
}
function formatChangeValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  const sanitized = sanitizeForDisplay(value)
  if (typeof sanitized === 'string' || typeof sanitized === 'number' || typeof sanitized === 'boolean') return String(sanitized)
  try { return JSON.stringify(sanitized, null, 2) } catch { return '[无法安全展示的内容]' }
}
function formatChanges(changes: Record<string, unknown>) {
  return Object.entries(changes).map(([field, value]) => {
    if (value && typeof value === 'object' && 'from' in value && 'to' in value) {
      const diff = value as { from?: unknown; to?: unknown }
      return `${field}: ${formatChangeValue(diff.from)} → ${formatChangeValue(diff.to)}`
    }
    return `${field}: ${formatChangeValue(value)}`
  }).join('\n')
}
function baseQuery(incidentId?: string) {
  return { scenario_id: scenarioId.value || undefined, case: incidentId || undefined }
}
function updateIncident(incident: IncidentCase) {
  const index = incidents.value.findIndex((item) => item.id === incident.id)
  if (index >= 0) incidents.value.splice(index, 1)
  const matchesStatus = !statusFilter.value || incident.status === statusFilter.value
  const matchesSeverity = !severityFilter.value || incident.severity === severityFilter.value
  if (incident.scenario_id === scenarioId.value && matchesStatus && matchesSeverity) incidents.value.unshift(incident)
  selectedIncident.value = incident
}
function isRelatedObjectAccessError(cause: unknown) {
  return errorStatus(cause) === 403 && /关联对象|对象/.test(errorMessage(cause, ''))
}
function setScenarioReadOnly(targetScenarioId: string, readOnly: boolean) {
  const { [targetScenarioId]: _discarded, ...remaining } = readOnlyScenarioIds.value
  readOnlyScenarioIds.value = readOnly ? { ...remaining, [targetScenarioId]: true } : remaining
  if (!readOnly || targetScenarioId !== scenarioId.value) return
  createDialog.value = false
  editDialog.value = false
  resolveDialog.value = false
}
async function refreshScenarioWriteAccess(targetScenarioId = scenarioId.value) {
  if (!targetScenarioId) return
  const request = ++scenarioPermissionRequest
  scenarioPermissionCheckingId.value = targetScenarioId
  // The list endpoint intentionally does not expose all scenario details. Keep
  // write controls closed until the detail endpoint resolves the current ACL.
  setScenarioReadOnly(targetScenarioId, true)
  try {
    const scenario = await api.getScenario(targetScenarioId)
    if (request !== scenarioPermissionRequest || targetScenarioId !== scenarioId.value) return
    setScenarioReadOnly(targetScenarioId, scenario.can_write !== true)
  } catch {
    if (request === scenarioPermissionRequest && targetScenarioId === scenarioId.value) {
      setScenarioReadOnly(targetScenarioId, true)
    }
  } finally {
    if (request === scenarioPermissionRequest && scenarioPermissionCheckingId.value === targetScenarioId) {
      scenarioPermissionCheckingId.value = ''
    }
  }
}
function markScenarioReadOnly(cause: unknown) {
  // A related-object denial is narrower than Case write access. Keep the Case
  // editor usable so the operator can remove/replace that object instead of
  // incorrectly branding the entire scenario as read-only.
  if (errorStatus(cause) !== 403 || isRelatedObjectAccessError(cause) || !scenarioId.value) return false
  setScenarioReadOnly(scenarioId.value, true)
  ElMessage.warning('当前账号可查看 Case，但没有该场景的写入权限，已切换为只读模式')
  return true
}
function setCreateError(message: string) {
  createError.value = message
  void nextTick(() => createErrorRef.value?.focus())
}
function setEditError(message: string) {
  editError.value = message
  void nextTick(() => editErrorRef.value?.focus())
}
function setResolveError(message: string) {
  resolveError.value = message
  void nextTick(() => resolveErrorRef.value?.focus())
}

async function initialize() {
  initialLoading.value = true
  pageError.value = ''
  try {
    // 发布治理选择器严格限于当前租户；普通场景目录还会包含可公开浏览的外部场景，不能作为 Case 目标。
    scenarios.value = await api.listReleaseScenarios()
    const requestedScenarioId = queryValue(route.query.scenario_id)
    scenarioId.value = scenarios.value.some((scenario) => scenario.id === requestedScenarioId)
      ? requestedScenarioId
      : scenarios.value[0]?.id || ''
    if (scenarioId.value && !requestedScenarioId) {
      await router.replace({ name: 'incidents', query: baseQuery() })
    }
    if (scenarioId.value) {
      const permissionCheck = refreshScenarioWriteAccess(scenarioId.value)
      await loadCases()
      await permissionCheck
      const requestedIncidentId = queryValue(route.query.case)
      if (requestedIncidentId) await openIncident(requestedIncidentId)
    }
  } catch (cause) {
    scenarios.value = []
    scenarioId.value = ''
    pageError.value = errorMessage(cause, '可访问场景加载失败')
  } finally {
    initialLoading.value = false
  }
}
async function selectScenario(value: string | number | boolean) {
  const nextScenarioId = String(value || '')
  if (!nextScenarioId) return
  scenarioId.value = nextScenarioId
  statusFilter.value = ''
  severityFilter.value = ''
  listForbidden.value = false
  listError.value = ''
  createDialog.value = false
  editDialog.value = false
  detailDrawer.value = false
  const permissionCheck = refreshScenarioWriteAccess(nextScenarioId)
  await router.replace({ name: 'incidents', query: baseQuery() })
  await loadCases()
  await permissionCheck
}
async function loadCases() {
  if (!scenarioId.value) return
  casesLoading.value = true
  listError.value = ''
  listForbidden.value = false
  try {
    incidents.value = await api.listIncidents(scenarioId.value, {
      status: statusFilter.value || undefined,
      severity: severityFilter.value || undefined,
      limit: 100,
    })
  } catch (cause) {
    incidents.value = []
    listError.value = errorMessage(cause, 'Case 列表加载失败')
    listForbidden.value = errorStatus(cause) === 403
  } finally {
    casesLoading.value = false
  }
}
function clearFilters() {
  statusFilter.value = ''
  severityFilter.value = ''
  void loadCases()
}
function setStatusFilter(status: IncidentStatus) {
  statusFilter.value = statusFilter.value === status ? '' : status
  void loadCases()
}
function setSeverityFilter(severity: IncidentSeverity) {
  severityFilter.value = severityFilter.value === severity ? '' : severity
  void loadCases()
}
function retryWritePermission() {
  if (!scenarioId.value) return
  void refreshScenarioWriteAccess(scenarioId.value)
}

async function searchRelatedObjects(query = '') {
  if (!scenarioId.value) return
  const request = ++objectSearchRequest
  objectsLoading.value = true
  objectsError.value = ''
  try {
    const result = await api.searchObjects(scenarioId.value, { q: query.trim() || undefined, limit: 50, offset: 0 })
    if (request !== objectSearchRequest) return
    relatedObjects.value = result.items
  } catch (cause) {
    if (request !== objectSearchRequest) return
    relatedObjects.value = []
    objectsError.value = errorMessage(cause, '关联对象暂时无法加载；仍可粘贴稳定对象 ID，由服务端校验')
  } finally {
    if (request === objectSearchRequest) objectsLoading.value = false
  }
}
function queueObjectSearch(query: string) {
  if (objectSearchTimer) clearTimeout(objectSearchTimer)
  objectSearchTimer = setTimeout(() => { void searchRelatedObjects(query) }, 220)
}
function assignToMe(form: IncidentForm) {
  form.assignee_user_id = auth.user?.id || ''
}
function resetCreateForm() {
  createForm.value = newIncidentForm()
  createError.value = ''
  createTitleError.value = ''
  objectsError.value = ''
}
function resetEditForm() {
  editForm.value = newIncidentForm()
  editError.value = ''
  editTitleError.value = ''
  objectsError.value = ''
}
function resetResolveForm() {
  resolveForm.value = { resolution: '', comment: '' }
  resolveError.value = ''
  resolveResolutionError.value = ''
}
function openCreateDialog() {
  if (scenarioReadOnly.value || !scenarioId.value) return
  resetCreateForm()
  createDialog.value = true
  void searchRelatedObjects()
}
async function createIncident() {
  if (scenarioReadOnly.value || !scenarioId.value) return
  const title = createForm.value.title.trim()
  if (!title) {
    createTitleError.value = '请输入 Case 标题'
    setCreateError('请先补充必填的 Case 标题')
    return
  }
  creating.value = true
  createError.value = ''
  try {
    const payload: IncidentCaseCreateInput = {
      title,
      description: createForm.value.description,
      severity: createForm.value.severity,
      source: createForm.value.source,
      source_ref: createForm.value.source_ref,
      related_object_id: normalizeOptional(createForm.value.related_object_id),
      assignee_user_id: normalizeOptional(createForm.value.assignee_user_id),
      comment: createForm.value.comment,
    }
    const created = await api.createIncident(scenarioId.value, payload)
    updateIncident(created)
    createDialog.value = false
    await loadHistory(created.id)
    ElMessage.success('Case 已创建，并写入第一条审计历史')
  } catch (cause) {
    if (!markScenarioReadOnly(cause)) setCreateError(errorMessage(cause, 'Case 创建失败'))
  } finally {
    creating.value = false
  }
}

async function openIncident(incidentId: string) {
  detailDrawer.value = true
  detailLoading.value = true
  detailError.value = ''
  historyError.value = ''
  history.value = []
  try {
    const incident = await api.getIncident(incidentId)
    updateIncident(incident)
    if (queryValue(route.query.case) !== incidentId) {
      await router.replace({ name: 'incidents', query: baseQuery(incidentId) })
    }
    void loadHistory(incident.id)
  } catch (cause) {
    detailError.value = errorStatus(cause) === 403
      ? '没有读取该 Case 的权限；事件历史不会通过公开场景内容暴露。'
      : errorMessage(cause, 'Case 详情加载失败')
  } finally {
    detailLoading.value = false
  }
}
async function loadHistory(incidentId: string) {
  historyLoading.value = true
  historyError.value = ''
  try {
    history.value = await api.listIncidentHistory(incidentId)
  } catch (cause) {
    historyError.value = errorStatus(cause) === 403
      ? '没有读取该 Case 历史的权限。'
      : errorMessage(cause, 'Case 历史加载失败')
  } finally {
    historyLoading.value = false
  }
}
function clearSelectedIncident() {
  selectedIncident.value = null
  history.value = []
  detailError.value = ''
  if (queryValue(route.query.case)) void router.replace({ name: 'incidents', query: baseQuery() })
}
function canEdit(incident: IncidentCase) {
  return !scenarioReadOnly.value && incident.status !== 'resolved'
}
function canAcknowledge(incident: IncidentCase) {
  return !scenarioReadOnly.value && incident.status === 'open'
}
function canResolve(incident: IncidentCase) {
  return !scenarioReadOnly.value && ['open', 'acknowledged'].includes(incident.status)
}
function openEditDialog() {
  if (!selectedIncident.value || !canEdit(selectedIncident.value)) return
  editForm.value = {
    title: selectedIncident.value.title,
    description: selectedIncident.value.description || '',
    severity: selectedIncident.value.severity,
    source: selectedIncident.value.source || 'manual',
    source_ref: selectedIncident.value.source_ref || '',
    related_object_id: selectedIncident.value.related_object_id || '',
    assignee_user_id: selectedIncident.value.assignee_user_id || '',
    comment: '',
  }
  editError.value = ''
  editTitleError.value = ''
  editDialog.value = true
  void searchRelatedObjects()
}
async function saveEdit() {
  if (scenarioReadOnly.value || !selectedIncident.value) return
  const title = editForm.value.title.trim()
  if (!title) {
    editTitleError.value = '请输入 Case 标题'
    setEditError('请先补充必填的 Case 标题')
    return
  }
  savingEdit.value = true
  editError.value = ''
  try {
    const current = selectedIncident.value
    const payload: IncidentCaseUpdateInput = {}
    if (title !== current.title) payload.title = title
    if (editForm.value.description !== (current.description || '')) payload.description = editForm.value.description
    if (editForm.value.severity !== current.severity) payload.severity = editForm.value.severity
    const relatedObjectId = normalizeOptional(editForm.value.related_object_id)
    if (relatedObjectId !== (current.related_object_id || null)) payload.related_object_id = relatedObjectId
    const assigneeUserId = normalizeOptional(editForm.value.assignee_user_id)
    if (assigneeUserId !== (current.assignee_user_id || null)) payload.assignee_user_id = assigneeUserId
    if (!Object.keys(payload).length) {
      setEditError('没有检测到需要保存的变更；变更说明不能单独作为一条 Case 更新。')
      return
    }
    if (editForm.value.comment.trim()) payload.comment = editForm.value.comment
    const updated = await api.updateIncident(selectedIncident.value.id, payload)
    updateIncident(updated)
    editDialog.value = false
    await loadHistory(updated.id)
    ElMessage.success('Case 已更新，变更说明已进入审计历史')
  } catch (cause) {
    if (!markScenarioReadOnly(cause)) setEditError(errorMessage(cause, 'Case 保存失败'))
  } finally {
    savingEdit.value = false
  }
}
async function acknowledgeIncident() {
  if (!selectedIncident.value || !canAcknowledge(selectedIncident.value)) return
  try {
    const { value } = await ElMessageBox.prompt(
      '可选：记录确认范围、响应人或下一步安排。',
      '确认 Case',
      { inputType: 'textarea', inputPlaceholder: '确认说明（可选）', confirmButtonText: '确认 Case', cancelButtonText: '取消', type: 'warning' },
    )
    acknowledging.value = true
    const updated = await api.acknowledgeIncident(selectedIncident.value.id, value || '')
    updateIncident(updated)
    await loadHistory(updated.id)
    ElMessage.success('Case 已确认，处理状态已更新')
  } catch (cause) {
    if (!isDismissal(cause) && !markScenarioReadOnly(cause)) ElMessage.error(errorMessage(cause, 'Case 确认失败'))
  } finally {
    acknowledging.value = false
  }
}
function openResolveDialog() {
  if (!selectedIncident.value || !canResolve(selectedIncident.value)) return
  resetResolveForm()
  resolveDialog.value = true
}
async function resolveIncident() {
  if (scenarioReadOnly.value || !selectedIncident.value) return
  const resolution = resolveForm.value.resolution.trim()
  if (!resolution) {
    resolveResolutionError.value = '请填写解决说明'
    setResolveError('解决前必须记录处置与验证结论')
    return
  }
  resolving.value = true
  resolveError.value = ''
  try {
    const updated = await api.resolveIncident(selectedIncident.value.id, { resolution, comment: resolveForm.value.comment })
    updateIncident(updated)
    resolveDialog.value = false
    await loadHistory(updated.id)
    ElMessage.success('Case 已解决并锁定，完整历史已保留')
  } catch (cause) {
    if (!markScenarioReadOnly(cause)) setResolveError(errorMessage(cause, 'Case 解决失败'))
  } finally {
    resolving.value = false
  }
}

onMounted(() => { void initialize() })
onBeforeUnmount(() => {
  if (objectSearchTimer) clearTimeout(objectSearchTimer)
})
</script>

<style scoped>
.incident-page { min-height: 100%; padding: 24px 28px 34px; }
.incident-header, .incident-header-actions, .section-header, .detail-header, .detail-section-heading, .resolution-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.incident-header { margin-bottom: 18px; }
.incident-header h1 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.incident-header p { max-width: 710px; margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.65; }
.incident-header-actions { flex-wrap: wrap; justify-content: flex-end; }
.eyebrow, .section-kicker { color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .14em; }
.scenario-select { width: min(290px, 100%); }
.page-alert { margin-bottom: 16px; }
.loading-card { padding: 22px; }
.empty-card, .access-card { display: flex; align-items: center; gap: 16px; min-height: 164px; color: var(--text-2); }
.empty-card > .el-icon, .access-card > .el-icon { flex: 0 0 auto; color: var(--primary); }
.empty-card h3, .access-card h4 { margin: 0 0 5px; color: var(--text); font-size: 16px; }
.empty-card p, .access-card p { max-width: 560px; margin: 0; font-size: 13px; line-height: 1.65; }
.access-card { margin: 6px 0; padding: 20px; border: 1px dashed color-mix(in srgb, var(--danger) 35%, var(--border)); border-radius: 12px; background: var(--danger-soft); }
.access-card > .el-icon { color: var(--danger); }
.access-card > div { min-width: 0; flex: 1; }
.incident-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.summary-card { min-height: 98px; display: grid; grid-template-columns: 38px minmax(0, 1fr); grid-template-rows: auto auto; column-gap: 11px; padding: 14px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--text); cursor: pointer; text-align: left; box-shadow: var(--shadow-xs); transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.summary-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.summary-card[aria-pressed="true"] { border-color: color-mix(in srgb, var(--primary) 55%, var(--border)); box-shadow: var(--shadow-primary); }
.summary-card:focus-visible, .case-title:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }
.summary-icon { grid-row: span 2; width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center; border-radius: 11px; font-size: 18px; }
.summary-card > span:nth-child(2) { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.summary-card b { font-size: 21px; line-height: 1.1; font-variant-numeric: tabular-nums; }
.summary-card small { color: var(--text-2); font-size: 11px; font-weight: 650; }
.summary-card p { grid-column: 2; margin: 5px 0 0; color: var(--text-3); font-size: 10.5px; line-height: 1.45; }
.summary-card--open .summary-icon, .summary-card--critical .summary-icon { color: var(--danger); background: var(--danger-soft); }
.summary-card--acknowledged .summary-icon { color: var(--warning); background: var(--warning-soft); }
.summary-card--resolved .summary-icon { color: var(--success); background: var(--success-soft); }
.incident-filter { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; padding: 12px; }
.filter-select { width: min(190px, 100%); }
.incident-list { min-height: 420px; overflow: hidden; }
.section-header { align-items: center; margin-bottom: 13px; }
.section-header h3 { margin: 3px 0 0; color: var(--text); font-size: 16px; }
.case-count { color: var(--text-3); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.list-alert { margin-bottom: 12px; }
.incident-table { width: 100%; }
.case-title { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; max-width: 100%; padding: 2px 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.case-title b { max-width: 100%; overflow: hidden; color: var(--text); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.case-title small, .state-stack small, .time-stack small { color: var(--text-3); font-size: 11px; }
.state-stack, .time-stack { display: flex; flex-direction: column; gap: 4px; }
.muted-id { color: var(--text-2); font-size: 11px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.field-help { margin-top: 5px; color: var(--text-3); font-size: 11px; line-height: 1.5; }
.field-help.with-action { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.field-help.with-action .el-button { flex: 0 0 auto; padding: 0; height: auto; }
.dialog-error { margin-bottom: 12px; outline: none; }
.incident-detail-drawer :deep(.el-drawer__body) { padding: 0; scroll-padding-bottom: 88px; }
.incident-detail { min-height: 100%; padding: 26px; background: var(--surface); }
.detail-header { margin-bottom: 18px; }
.detail-header h3 { max-width: 480px; margin: 5px 0; color: var(--text); font-size: 20px; letter-spacing: -.025em; overflow-wrap: anywhere; }
.detail-header p { margin: 0; color: var(--text-3); font-size: 10px; overflow-wrap: anywhere; }
.detail-tools { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: flex-end; gap: 7px; }
.detail-tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; padding-top: 2px; }
.drawer-close { min-width: 44px; min-height: 44px; margin: -9px -10px -9px 0; color: var(--text-2); }
.detail-alert { margin-bottom: 14px; }
.case-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-bottom: 18px; }
.case-facts div { min-width: 0; padding: 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.case-facts span { display: block; color: var(--text-3); font-size: 10px; }
.case-facts b { display: block; margin-top: 4px; color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
.detail-section { margin-top: 15px; }
.detail-section h4, .resolution-panel h4, .detail-section-heading h4 { margin: 3px 0 0; color: var(--text); font-size: 14px; }
.detail-copy, .resolution-panel p { margin: 8px 0 0; color: var(--text-2); font-size: 13px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.acknowledgement-panel, .resolution-panel { margin-top: 16px; padding: 14px; border-radius: 12px; }
.acknowledgement-panel { display: flex; align-items: flex-start; gap: 9px; border: 1px solid color-mix(in srgb, var(--warning) 35%, var(--border)); background: var(--warning-soft); color: var(--text-2); font-size: 12px; line-height: 1.65; }
.acknowledgement-panel > .el-icon { flex: 0 0 auto; margin-top: 2px; color: var(--warning); }
.resolution-panel { border: 1px solid color-mix(in srgb, var(--success) 32%, var(--border)); background: var(--success-soft); }
.resolution-heading { justify-content: flex-start; align-items: center; gap: 7px; }
.resolution-heading > .el-icon { color: var(--success); }
.resolution-panel small { display: block; margin-top: 9px; color: var(--text-3); font-size: 11px; }
.history-section { margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); }
.detail-section-heading { align-items: center; margin-bottom: 12px; }
.history-timeline { margin: 16px 0 0 4px; padding-left: 4px; }
.history-entry { padding: 2px 0 11px; }
.history-entry header { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.history-entry header b { color: var(--text); font-size: 12px; }
.history-entry header span { color: var(--text-3); font-size: 10px; overflow-wrap: anywhere; text-align: right; }
.history-transition, .history-comment { margin: 6px 0 0; color: var(--text-2); font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.history-transition { color: var(--primary-600); font-weight: 650; }
.history-changes { max-height: 180px; margin: 8px 0 0; padding: 9px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text-2); font-size: 10.5px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.history-empty { list-style: none; padding: 5px 0 14px 25px; color: var(--text-3); font-size: 12px; }
.detail-footer { position: sticky; bottom: 0; z-index: 2; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin: 22px -26px -26px; padding: 14px 26px max(14px, env(safe-area-inset-bottom)); border-top: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 96%, transparent); backdrop-filter: blur(12px); }
@media (max-width: 1080px) { .incident-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .incident-page { padding: 18px 14px 22px; } .incident-header { flex-direction: column; } .incident-header-actions { width: 100%; justify-content: flex-start; } .scenario-select { flex: 1 1 220px; } .form-grid { grid-template-columns: 1fr; } .access-card { align-items: flex-start; flex-wrap: wrap; } }
@media (max-width: 480px) { .incident-summary, .case-facts { grid-template-columns: 1fr; } .summary-card p { grid-column: 2; } .detail-header, .detail-section-heading { flex-direction: column; } .detail-tools { width: 100%; justify-content: space-between; } .detail-tags { justify-content: flex-start; } .case-count { white-space: normal; } .field-help.with-action { align-items: flex-start; flex-direction: column; gap: 2px; } }
</style>
