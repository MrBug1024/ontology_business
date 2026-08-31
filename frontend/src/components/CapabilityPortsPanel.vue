<template>
  <section class="ports-panel" aria-labelledby="capability-ports-heading" :aria-busy="loading">
    <header class="ports-toolbar">
      <div>
        <h2 id="capability-ports-heading">能力输入契约</h2>
        <p>审核并激活能力需要的业务数据、附件或数据库连接。</p>
      </div>
      <el-button text :loading="loading" title="刷新能力输入" @click="loadPorts">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
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
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { ScenarioCapabilityPort, ScenarioCapabilityPortWrite } from '@/types'

const props = defineProps<{
  scenarioId: string
  canWrite: boolean
  capabilityNames?: Record<string, string>
}>()

const ports = ref<ScenarioCapabilityPort[]>([])
const loading = ref(false)
const busyId = ref('')
const error = ref('')

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
.ports-table { width: 100%; }
.owner-cell { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.owner-cell strong { overflow: hidden; color: var(--text-1); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.owner-cell small { overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.binding-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.muted { color: var(--text-3); font-size: 12px; }
@media (max-width: 720px) {
  .ports-toolbar { align-items: stretch; flex-direction: column; }
}
</style>
