<template>
  <section class="ports-panel" aria-labelledby="capability-ports-heading" :aria-busy="loading">
    <header class="ports-toolbar">
      <div>
        <h2 id="capability-ports-heading">能力输入契约</h2>
        <p>审核并激活能力需要的业务数据、附件或数据库连接。</p>
      </div>
      <div class="ports-actions">
        <el-button text :loading="loading" title="刷新能力输入" @click="loadPorts">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-tooltip :disabled="Boolean(capabilityOptions.length)" content="请先创建函数、操作或工作流" placement="top">
          <span>
            <el-button v-if="canWrite" type="primary" :disabled="!capabilityOptions.length" @click="openCreateDialog">
              <el-icon><Plus /></el-icon>
              添加输入契约
            </el-button>
          </span>
        </el-tooltip>
      </div>
    </header>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      title="能力输入加载失败"
      :description="error"
    />

    <el-table v-loading="loading" :data="ports" class="ports-table" empty-text="当前场景尚未声明能力输入">
      <el-table-column label="所属能力" min-width="190">
        <template #default="{ row }">
          <div class="owner-cell">
            <strong>{{ ownerName(row) }}</strong>
            <small>{{ kindLabel(row.capability_kind) }} · {{ row.port_key }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="输入" min-width="190">
        <template #default="{ row }">
          <div class="owner-cell">
            <strong>{{ row.name }}</strong>
            <small>{{ row.description || roleLabel(row.role) }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="接入方式" min-width="180">
        <template #default="{ row }">
          <div class="binding-tags">
            <el-tag v-for="kind in bindingKinds(row)" :key="kind" size="small" effect="plain">
              {{ bindingKindLabel(kind) }}
            </el-tag>
            <span v-if="!bindingKinds(row).length" class="muted">不接收运行资料</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="约束" width="120">
        <template #default="{ row }">
          <span>{{ row.is_required ? '必填' : '可选' }} · {{ row.cardinality === 'many' ? '多个' : '单个' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="105">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'draft'"
            text
            type="primary"
            :loading="busyId === row.id"
            @click="activate(row)"
          >
            <el-icon><CircleCheck /></el-icon>
            激活
          </el-button>
          <el-button
            v-else-if="row.status === 'active'"
            text
            type="warning"
            :loading="busyId === row.id"
            @click="retire(row)"
          >
            <el-icon><Remove /></el-icon>
            退役
          </el-button>
          <span v-else class="muted">已结束</span>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="createDialog" title="添加能力输入契约" width="min(620px, calc(100vw - 32px))" @closed="resetCreateForm">
      <el-form label-position="top" @submit.prevent>
        <div v-if="createError" class="form-error-summary" role="alert" tabindex="-1">{{ createError }}</div>
        <div class="form-grid">
          <el-form-item label="所属能力" required>
            <el-select v-model="createForm.capability_ref" filterable style="width: 100%" placeholder="选择函数、操作或工作流">
              <el-option v-for="option in capabilityOptions" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="输入介质" required>
            <el-select v-model="createForm.media_kind" style="width: 100%" @change="onMediaKindChange">
              <el-option label="结构化数据集" value="dataset" />
              <el-option label="文档附件" value="document" />
              <el-option label="受管连接" value="connector" />
              <el-option label="受管产物" value="artifact" />
            </el-select>
          </el-form-item>
          <el-form-item label="端口 key" required>
            <el-input v-model="createForm.port_key" maxlength="180" placeholder="如 audit_data" />
          </el-form-item>
          <el-form-item label="显示名称" required>
            <el-input v-model="createForm.name" maxlength="300" placeholder="如本次审计数据" />
          </el-form-item>
          <el-form-item v-if="createForm.media_kind === 'dataset'" label="逻辑数据集" required>
            <el-select v-model="createForm.dataset_id" filterable style="width: 100%" placeholder="选择兼容数据集" @change="onDatasetChange">
              <el-option v-for="dataset in datasets" :key="dataset.id" :label="dataset.name" :value="dataset.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="createForm.media_kind === 'dataset'" label="Dataset Schema" required>
            <el-select v-model="createForm.dataset_schema_id" style="width: 100%" placeholder="选择调用数据必须满足的 Schema" :loading="schemasLoading">
              <el-option v-for="schema in schemas" :key="schema.id" :label="`Schema v${schema.schema_version}`" :value="schema.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="用途" required>
            <el-select v-model="createForm.role" style="width: 100%">
              <el-option label="每次调用业务输入" value="invocation_input" />
              <el-option label="参考资料" value="reference" />
              <el-option label="规则资料" value="rules" />
            </el-select>
          </el-form-item>
          <el-form-item label="数量" required>
            <el-radio-group v-model="createForm.cardinality">
              <el-radio-button value="one">单个</el-radio-button>
              <el-radio-button value="many">多个</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </div>
        <el-form-item label="说明">
          <el-input v-model="createForm.description" type="textarea" :rows="2" maxlength="8000" show-word-limit placeholder="说明该能力如何使用这份输入" />
        </el-form-item>
        <el-checkbox v-model="createForm.is_required">调用时必须提供</el-checkbox>
        <div class="form-help">新契约先保存为“待激活”，复核后再从列表中激活。正式调用只接受受管引用，不接收路径、连接串或凭据。</div>
      </el-form>
      <template #footer>
        <el-button :disabled="creating" @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createPort">保存契约</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { DatasetSchema, LogicalDataset, ScenarioCapabilityPort, ScenarioCapabilityPortWrite } from '@/types'

const props = defineProps<{
  scenarioId: string
  canWrite: boolean
  capabilityNames?: Record<string, string>
}>()

const ports = ref<ScenarioCapabilityPort[]>([])
const loading = ref(false)
const busyId = ref('')
const error = ref('')
const createDialog = ref(false)
const creating = ref(false)
const createError = ref('')
const datasets = ref<LogicalDataset[]>([])
const schemas = ref<DatasetSchema[]>([])
const schemasLoading = ref(false)
const emptyCreateForm = () => ({
  capability_ref: '',
  media_kind: 'dataset' as ScenarioCapabilityPort['media_kind'],
  port_key: '',
  name: '',
  description: '',
  dataset_id: '',
  dataset_schema_id: '',
  role: 'invocation_input' as ScenarioCapabilityPort['role'],
  cardinality: 'one' as ScenarioCapabilityPort['cardinality'],
  is_required: true,
})
const createForm = ref(emptyCreateForm())
const capabilityOptions = computed(() => Object.entries(props.capabilityNames || {}).map(([value, label]) => ({
  value,
  label: `${label} · ${kindLabel(value.split(':', 1)[0])}`,
})))

function portWrite(row: ScenarioCapabilityPort, status: ScenarioCapabilityPort['status']): ScenarioCapabilityPortWrite {
  return {
    capability_kind: row.capability_kind,
    capability_key: row.capability_key,
    port_key: row.port_key,
    name: row.name,
    description: row.description,
    direction: row.direction,
    role: row.role,
    media_kind: row.media_kind,
    dataset_id: row.dataset_id,
    dataset_schema_id: row.dataset_schema_id,
    schema_document: row.schema_document || {},
    is_required: row.is_required,
    cardinality: row.cardinality,
    binding_policy: row.binding_policy,
    status,
    config: row.config || {},
  }
}

async function loadPorts() {
  loading.value = true
  error.value = ''
  try {
    ports.value = await api.listScenarioCapabilityPorts(props.scenarioId)
  } catch (reason: any) {
    error.value = reason?.message || '无法读取能力输入契约'
  } finally {
    loading.value = false
  }
}

async function openCreateDialog() {
  resetCreateForm()
  createDialog.value = true
  try {
    datasets.value = (await api.listLogicalDatasets()).filter((item) => item.lifecycle_status === 'active')
  } catch (reason: any) {
    createError.value = reason?.message || '无法读取逻辑数据集'
  }
}

function resetCreateForm() {
  createForm.value = emptyCreateForm()
  schemas.value = []
  createError.value = ''
}

function onMediaKindChange() {
  createForm.value.dataset_id = ''
  createForm.value.dataset_schema_id = ''
  schemas.value = []
}

async function onDatasetChange(datasetId: string) {
  createForm.value.dataset_schema_id = ''
  schemas.value = []
  if (!datasetId) return
  schemasLoading.value = true
  try {
    schemas.value = await api.listDatasetSchemas(datasetId)
  } catch (reason: any) {
    createError.value = reason?.message || '无法读取 Dataset Schema'
  } finally {
    schemasLoading.value = false
  }
}

async function createPort() {
  if (creating.value) return
  createError.value = ''
  const [capabilityKind, ...keyParts] = createForm.value.capability_ref.split(':')
  const capabilityKey = keyParts.join(':')
  if (!['function', 'action', 'workflow'].includes(capabilityKind) || !capabilityKey) {
    createError.value = '请选择所属能力。'
    return
  }
  if (!/^[a-z0-9][a-z0-9._-]{0,179}$/.test(createForm.value.port_key) || !createForm.value.name.trim()) {
    createError.value = '请填写有效的端口 key 和显示名称。'
    return
  }
  if (createForm.value.media_kind === 'dataset' && (!createForm.value.dataset_id || !createForm.value.dataset_schema_id)) {
    createError.value = '结构化数据集端口必须绑定逻辑数据集和 Dataset Schema。'
    return
  }
  const bindingKind = createForm.value.media_kind === 'dataset'
    ? 'dataset_version'
    : createForm.value.media_kind === 'connector'
      ? 'connector_binding'
      : 'asset_version'
  creating.value = true
  try {
    await api.createScenarioCapabilityPort(props.scenarioId, {
      capability_kind: capabilityKind as 'function' | 'action' | 'workflow',
      capability_key: capabilityKey,
      port_key: createForm.value.port_key,
      name: createForm.value.name.trim(),
      description: createForm.value.description.trim(),
      direction: 'input',
      role: createForm.value.role,
      media_kind: createForm.value.media_kind,
      dataset_id: createForm.value.media_kind === 'dataset' ? createForm.value.dataset_id : null,
      dataset_schema_id: createForm.value.media_kind === 'dataset' ? createForm.value.dataset_schema_id : null,
      schema_document: {},
      is_required: createForm.value.is_required,
      cardinality: createForm.value.cardinality,
      binding_policy: 'per_invocation',
      status: 'draft',
      config: { allowed_binding_kinds: [bindingKind] },
    })
    createDialog.value = false
    ElMessage.success('能力输入契约已保存，请复核后激活')
    await loadPorts()
  } catch (reason: any) {
    createError.value = reason?.message || '能力输入契约保存失败'
  } finally {
    creating.value = false
  }
}

async function changeStatus(row: ScenarioCapabilityPort, status: ScenarioCapabilityPort['status']) {
  busyId.value = row.id
  try {
    const updated = await api.updateScenarioCapabilityPort(
      props.scenarioId,
      row.id,
      portWrite(row, status),
    )
    ports.value = ports.value.map((item) => item.id === updated.id ? updated : item)
    ElMessage.success(status === 'active' ? '能力输入已激活，可用于验证和发布' : '能力输入已退役')
  } catch (reason: any) {
    ElMessage.error(reason?.message || '能力输入状态更新失败')
  } finally {
    busyId.value = ''
  }
}

function activate(row: ScenarioCapabilityPort) {
  void changeStatus(row, 'active')
}

async function retire(row: ScenarioCapabilityPort) {
  await ElMessageBox.confirm(
    `退役“${row.name}”后，新验证不会再接受该输入；历史发布快照不变。`,
    '退役能力输入',
    { type: 'warning', confirmButtonText: '退役', cancelButtonText: '取消' },
  )
  await changeStatus(row, 'retired')
}

function ownerName(row: ScenarioCapabilityPort) {
  return props.capabilityNames?.[`${row.capability_kind}:${row.capability_key}`] || row.capability_key
}

function bindingKinds(row: ScenarioCapabilityPort): string[] {
  const raw = row.config?.allowed_binding_kinds ?? row.config?.binding_kinds
  return Array.isArray(raw) ? raw.map(String) : []
}

function kindLabel(value: string) {
  return ({ function: '函数', action: '操作', workflow: '工作流' } as Record<string, string>)[value] || value
}

function roleLabel(value: string) {
  return ({ invocation_input: '每次调用业务输入', reference: '参考资料', rules: '规则资料', output: '输出' } as Record<string, string>)[value] || value
}

function bindingKindLabel(value: string) {
  return ({
    dataset_version: '上传数据集',
    dataset_head: '数据集最新版本',
    connector_binding: '远程数据库',
    asset_version: '文件附件',
  } as Record<string, string>)[value] || value
}

function statusLabel(value: string) {
  return ({ draft: '待激活', active: '已激活', retired: '已退役' } as Record<string, string>)[value] || value
}

function statusType(value: string): 'success' | 'warning' | 'info' {
  return value === 'active' ? 'success' : value === 'draft' ? 'warning' : 'info'
}

onMounted(loadPorts)
</script>

<style scoped>
.ports-panel { display: flex; flex-direction: column; gap: 14px; }
.ports-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.ports-toolbar h2 { margin: 0; color: var(--text-1); font-size: 16px; letter-spacing: 0; }
.ports-toolbar p { margin: 4px 0 0; color: var(--text-3); font-size: 12px; }
.ports-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.ports-table { width: 100%; }
.owner-cell { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.owner-cell strong { overflow: hidden; color: var(--text-1); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.owner-cell small { overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.binding-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.muted { color: var(--text-3); font-size: 12px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.form-help { margin-top: 10px; color: var(--text-3); font-size: 12px; line-height: 1.5; }
.form-error-summary { margin-bottom: 16px; padding: 10px 12px; border: 1px solid var(--el-color-danger-light-5); border-radius: 6px; color: var(--el-color-danger); background: var(--el-color-danger-light-9); font-size: 13px; }
@media (max-width: 720px) {
  .ports-toolbar { align-items: stretch; flex-direction: column; }
  .ports-actions { justify-content: flex-start; }
  .form-grid { grid-template-columns: 1fr; }
}
</style>
