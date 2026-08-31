<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>业务场景</h1>
        <div class="sub">从业务描述开始完成建模、验证与发布；退役可恢复，永久删除需单独确认</div>
      </div>
      <div class="header-actions">
        <el-radio-group v-model="viewMode" size="small" aria-label="场景状态筛选">
          <el-radio-button value="current">当前场景</el-radio-button>
          <el-radio-button value="retired">已退役</el-radio-button>
        </el-radio-group>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建场景</el-button>
      </div>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="s in visibleScenarios" :key="s.id">
        <article class="card scenario-card" :class="{ 'is-retired': s.status === 'retired' }">
          <div class="sc-head">
            <div class="sc-icon"><el-icon :size="22"><OfficeBuilding /></el-icon></div>
            <div class="sc-title">
              <div class="sc-name">{{ s.name }}</div>
              <div class="sc-tags">
                <el-tag size="small" type="info" effect="light">{{ s.industry || '通用' }}</el-tag>
                <el-tag v-if="s.status === 'retired'" size="small" type="info" effect="plain">已退役</el-tag>
              </div>
            </div>
            <el-icon class="sc-arrow" aria-hidden="true"><ArrowRight /></el-icon>
          </div>
          <div class="sc-desc">{{ s.description || '暂无描述' }}</div>
          <div class="sc-stats">
            <div><b>{{ s.entity_count || 0 }}</b><span>对象类型</span></div>
            <div class="sc-sep"></div>
            <div><b>{{ s.relation_count || 0 }}</b><span>关系类型</span></div>
            <div class="sc-sep"></div>
            <div><b>{{ s.data_source_count || 0 }}</b><span>建模接入</span></div>
          </div>
          <div class="sc-actions">
            <el-button size="small" type="primary" plain @click="$router.push('/scenarios/' + s.id)"><el-icon><ArrowRight /></el-icon> {{ s.status === 'retired' ? '查看审计' : '进入场景' }}</el-button>
            <el-button v-if="s.status !== 'retired'" size="small" text type="primary" @click="openEdit(s)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button v-if="s.status !== 'retired'" size="small" text type="danger" @click="remove(s)"><el-icon><Delete /></el-icon> 退役</el-button>
            <el-button v-if="s.status === 'retired'" size="small" text type="primary" :loading="restoringId === s.id" @click="restore(s)"><el-icon><RefreshLeft /></el-icon> 恢复</el-button>
            <el-button v-if="s.status === 'retired' && canManage" size="small" text type="danger" @click="openPurge(s)"><el-icon><DeleteFilled /></el-icon> 永久删除</el-button>
          </div>
        </article>
      </el-col>
    </el-row>
    <div v-if="!loading && !visibleScenarios.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><OfficeBuilding /></el-icon></div>
      <div>{{ viewMode === 'retired' ? '暂无已退役场景' : '暂无业务场景' }}</div>
      <el-button v-if="viewMode === 'current'" type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建场景</el-button>
    </div>

    <el-dialog v-model="dlg" class="scenario-dialog" :title="form.id ? '编辑场景' : '新建业务场景'" width="min(560px, 94vw)">
      <el-form :model="form" label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：运营分析、供应链、人力资源…" />
        </el-form-item>
        <el-form-item label="行业">
          <el-input v-model="form.industry" placeholder="可选，填写所属行业或业务领域" />
        </el-form-item>
        <el-form-item label="业务描述">
          <el-input v-model="form.description" type="textarea" :rows="4" maxlength="4000" show-word-limit placeholder="说明业务目标、核心对象、关键规则和希望完成的工作" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ form.id ? '保存修改' : '创建场景' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="purgeVisible"
      class="scenario-dialog"
      title="永久删除业务场景"
      width="min(680px, 94vw)"
      :close-on-click-modal="!purging"
    >
      <div v-loading="purgeLoading" class="purge-dialog-body">
        <el-alert
          type="error"
          :closable="false"
          show-icon
          title="这是不可恢复操作"
          description="场景定义、验证 Agent、会话、运行审计和场景自有文件将被删除。独立目录数据集不会被连带误删，删除后可在资源页单独治理。"
        />
        <template v-if="purgePlan">
          <div v-if="purgePlan.blockers.length" class="purge-blockers" role="alert">
            <strong>当前不能永久删除</strong>
            <ul><li v-for="blocker in purgePlan.blockers" :key="blocker">{{ blocker }}</li></ul>
          </div>
          <dl class="purge-counts" aria-label="永久删除影响范围">
            <div v-for="item in visiblePurgeCounts" :key="item.key">
              <dt>{{ item.label }}</dt><dd>{{ item.value }}</dd>
            </div>
            <div>
              <dt>保留的独立数据集</dt><dd>{{ purgePlan.retained.logical_datasets || 0 }}</dd>
            </div>
          </dl>
          <el-checkbox v-if="purgePlan.requires_audit_confirmation" v-model="purgeAuditConfirmed">
            我确认同时删除该场景的验证、运行与发布审计历史
          </el-checkbox>
          <label class="purge-name-field">
            <span>输入场景名称 <strong>{{ purgePlan.scenario_name }}</strong> 以确认</span>
            <el-input v-model="purgeExpectedName" :disabled="!purgePlan.can_purge || purging" autocomplete="off" />
          </label>
        </template>
      </div>
      <template #footer>
        <el-button :disabled="purging" @click="purgeVisible = false">取消</el-button>
        <el-button type="danger" :loading="purging" :disabled="!canConfirmPurge" @click="confirmPurge">永久删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { Scenario, ScenarioPurgePlan } from '@/types'

const scenarios = ref<Scenario[]>([])
const viewMode = ref<'current' | 'retired'>('current')
const visibleScenarios = computed(() => scenarios.value.filter((scenario) =>
  viewMode.value === 'retired'
    ? scenario.status === 'retired'
    : scenario.status !== 'retired',
))
const router = useRouter()
const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<Scenario>>({})
const restoringId = ref('')
const purgeVisible = ref(false)
const purgeLoading = ref(false)
const purging = ref(false)
const purgeTarget = ref<Scenario | null>(null)
const purgePlan = ref<ScenarioPurgePlan | null>(null)
const purgeExpectedName = ref('')
const purgeAuditConfirmed = ref(false)
const purgeCountLabels: Record<string, string> = {
  object_types: '对象类型', relation_types: '关系类型', object_instances: '对象实例', relation_instances: '关系实例',
  mappings: '数据映射', data_sources: '场景自有接入', dataset_bindings: '资料用途绑定', connector_bindings: '运行连接绑定',
  agents: '验证 Agent', conversations: '验证会话', messages: '对话消息', assistant_threads: '顾问会话',
  assistant_attachments: '顾问附件', capability_invocations: '能力调用记录', action_logs: '操作审计',
  workflow_runs: '工作流记录', releases: '发布记录', llm_traces: '模型调用审计',
  assertions: '验证结论', derivation_runs: '推理运行', derivation_evidence: '推理证据',
}
const visiblePurgeCounts = computed(() => Object.entries(purgePlan.value?.counts || {})
  .filter(([, value]) => value > 0)
  .map(([key, value]) => ({ key, value, label: purgeCountLabels[key] || key })))
const canConfirmPurge = computed(() => Boolean(
  purgePlan.value?.can_purge
  && purgeExpectedName.value === purgePlan.value.scenario_name
  && (!purgePlan.value.requires_audit_confirmation || purgeAuditConfirmed.value),
))

async function load() {
  loading.value = true
  try {
    scenarios.value = await api.listScenarios(true)
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function openCreate() {
  form.value = {}
  dlg.value = true
}
function openEdit(s: Scenario) {
  form.value = { ...s }
  dlg.value = true
}
async function save() {
  if (!form.value.name) return ElMessage.warning('请填写名称')
  saving.value = true
  try {
    if (form.value.id) {
      await api.updateScenario(form.value.id, form.value)
      ElMessage.success('场景信息已保存')
      dlg.value = false
      await load()
      return
    }
    const created = await api.createScenario(form.value)
    dlg.value = false
    ElMessage.success('场景已创建')
    await router.push({ name: 'scenario-detail', params: { id: created.id }, query: { stage: 'ontology' } })
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function remove(s: Scenario) {
  try {
    await ElMessageBox.confirm(
      `退役场景「${s.name}」？退役会暂停新的验证和调用，但保留全部配置；之后可以随时恢复或由管理员永久删除。`,
      '确认退役',
      { type: 'warning' },
    )
    await api.deleteScenario(s.id)
    ElMessage.success('场景已退役')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '退役失败')
  }
}
async function restore(s: Scenario) {
  restoringId.value = s.id
  try {
    await api.restoreScenario(s.id)
    ElMessage.success(`场景「${s.name}」已恢复，可继续建模和验证`)
    viewMode.value = 'current'
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '恢复失败')
  } finally {
    restoringId.value = ''
  }
}
async function openPurge(s: Scenario) {
  purgeTarget.value = s
  purgePlan.value = null
  purgeExpectedName.value = ''
  purgeAuditConfirmed.value = false
  purgeVisible.value = true
  purgeLoading.value = true
  try {
    purgePlan.value = await api.getScenarioPurgePlan(s.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '无法读取永久删除影响范围')
    purgeVisible.value = false
  } finally {
    purgeLoading.value = false
  }
}
async function confirmPurge() {
  if (!purgeTarget.value || !purgePlan.value || !canConfirmPurge.value) return
  purging.value = true
  try {
    await api.purgeScenario(purgeTarget.value.id, {
      expected_name: purgeExpectedName.value,
      confirmed: true,
      delete_audit_history: purgeAuditConfirmed.value,
    })
    ElMessage.success(`场景「${purgeTarget.value.name}」已永久删除`)
    purgeVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '永久删除失败')
  } finally {
    purging.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.scenario-card {
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
  margin-bottom: 16px;
  position: relative;
  overflow: hidden;
}
.scenario-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--grad);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.scenario-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.scenario-card:hover::before { opacity: 1; }
.scenario-card.is-retired { border-style: dashed; }
.header-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.sc-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.sc-icon {
  width: 44px; height: 44px;
  border-radius: 12px;
  background: var(--primary-soft);
  color: var(--primary-600);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.sc-title { flex: 1; min-width: 0; }
.sc-tags { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.sc-name {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sc-arrow {
  color: var(--text-3);
  transition: transform var(--dur) var(--ease), color var(--dur);
}
.scenario-card:hover .sc-arrow {
  transform: translateX(3px);
  color: var(--primary);
}
.sc-desc {
  color: var(--text-2);
  font-size: 13px;
  height: 40px;
  overflow: hidden;
  margin-bottom: 14px;
  line-height: 1.5;
}
.sc-stats {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 12px 0;
  border-top: 1px solid var(--border);
}
.sc-stats div { display: flex; flex-direction: column; }
.sc-stats b { font-size: 18px; font-weight: 800; letter-spacing: -0.3px; }
.sc-stats span { color: var(--text-3); font-size: 12px; margin-top: 1px; }
.sc-sep {
  width: 1px;
  height: 22px;
  background: var(--border);
}
.sc-actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
:global(.scenario-dialog) { display: flex; max-height: calc(100dvh - 32px); flex-direction: column; }
:global(.scenario-dialog .el-dialog__body) { min-height: 0; overflow-y: auto; }
:global(.scenario-dialog .el-dialog__header),
:global(.scenario-dialog .el-dialog__footer) { flex: 0 0 auto; }
.purge-dialog-body { display: grid; gap: 16px; min-height: 120px; }
.purge-blockers { padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--border)); border-radius: 8px; background: color-mix(in srgb, var(--danger) 7%, var(--surface)); color: var(--text-1); }
.purge-blockers ul { margin: 8px 0 0; padding-left: 20px; color: var(--text-2); }
.purge-counts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.purge-counts > div { padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.purge-counts dt { color: var(--text-3); font-size: 12px; }
.purge-counts dd { margin: 4px 0 0; color: var(--text-1); font-size: 18px; font-weight: 750; }
.purge-name-field { display: grid; gap: 8px; color: var(--text-2); font-size: 13px; }
@media (max-width: 640px) {
  .purge-counts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
