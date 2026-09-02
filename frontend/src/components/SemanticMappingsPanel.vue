<template>
  <section class="semantic-panel" aria-labelledby="semantic-mapping-heading" :aria-busy="loading">
    <header class="semantic-toolbar">
      <div>
        <h2 id="semantic-mapping-heading">Catalog 语义映射</h2>
        <p>用固定 DatasetVersion 定义字段语义；正式调用仍需在每次 Invocation 中显式提供受管数据。</p>
      </div>
      <div class="semantic-actions">
        <el-button text :loading="loading" title="刷新语义映射" @click="loadAll">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="canWrite" plain @click="openBindingDialog">
          <el-icon><Link /></el-icon>
          固定建模版本
        </el-button>
        <el-tooltip :disabled="Boolean(bindings.length && entities.length)" content="请先固定一个建模数据版本并创建对象类型" placement="top">
          <span>
            <el-button
              v-if="canWrite"
              type="primary"
              :disabled="!bindings.length || !entities.length"
              @click="openMappingDialog"
            >
              <el-icon><Plus /></el-icon>
              添加语义映射
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
      title="Catalog 语义映射加载失败"
      :description="error"
    />

    <div class="semantic-summary" role="status" aria-live="polite">
      <span>建模版本 <b>{{ bindings.length }}</b></span>
      <span>已激活对象映射 <b>{{ activeMappingCount }}</b></span>
    </div>

    <el-table v-loading="loading" :data="mappings" empty-text="尚未创建 Catalog 语义映射">
      <el-table-column label="对象类型" min-width="170">
        <template #default="{ row }">
          <div class="semantic-cell">
            <strong>{{ entityName(row.entity_id) }}</strong>
            <small>{{ row.mapping_key }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="建模数据" min-width="190">
        <template #default="{ row }">
          <div class="semantic-cell">
            <strong>{{ bindingName(row.scenario_dataset_binding_id) }}</strong>
            <small>{{ relationName(row.dataset_schema_id, row.dataset_relation_id) }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="字段映射" min-width="260">
        <template #default="{ row }">
          <div class="field-tags">
            <el-tag v-for="field in row.fields" :key="field.id" size="small" effect="plain">
              {{ propertyName(row.entity_id, field.ontology_property_id) }} ← {{ datasetFieldName(row.dataset_schema_id, field.dataset_field_id) }}
            </el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="105">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
            {{ row.status === 'active' ? '已激活' : row.status }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="bindingDialog" title="固定建模数据版本" width="min(560px, calc(100vw - 32px))" @closed="resetBindingForm">
      <el-alert
        class="dialog-note"
        type="info"
        :closable="false"
        show-icon
        title="该绑定只固定建模时使用的 Schema，不会成为运行时默认数据。"
      />
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="逻辑数据集" required :error="bindingErrors.dataset_id">
          <el-select v-model="bindingForm.dataset_id" filterable style="width: 100%" placeholder="选择已物化的数据集" @change="onBindingDatasetChange">
            <el-option v-for="dataset in datasets" :key="dataset.id" :label="dataset.name" :value="dataset.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="固定版本" required :error="bindingErrors.dataset_version_id">
          <el-select v-model="bindingForm.dataset_version_id" filterable style="width: 100%" placeholder="选择 ready 版本" :loading="contextLoading">
            <el-option
              v-for="version in selectedVersions"
              :key="version.id"
              :label="`版本 ${version.version_number} · ${version.record_count.toLocaleString()} 行`"
              :value="version.id"
              :disabled="version.status !== 'ready'"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定 key" required :error="bindingErrors.binding_key">
          <el-input v-model="bindingForm.binding_key" maxlength="180" placeholder="如 audit_modeling_v1" @blur="validateBinding" />
          <div class="form-help">使用稳定的小写英文、数字、点、下划线或连字符。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="bindingDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveBinding">固定版本</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mappingDialog" title="添加对象语义映射" width="min(760px, calc(100vw - 32px))" @closed="resetMappingForm">
      <el-form label-position="top" @submit.prevent>
        <div v-if="mappingError" class="form-error-summary" role="alert" tabindex="-1">{{ mappingError }}</div>
        <div class="form-grid">
          <el-form-item label="建模数据版本" required>
            <el-select v-model="mappingForm.binding_id" style="width: 100%" @change="onMappingBindingChange">
              <el-option v-for="binding in bindings" :key="binding.id" :label="bindingName(binding.id)" :value="binding.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="对象类型" required>
            <el-select v-model="mappingForm.entity_id" filterable style="width: 100%" @change="mappingForm.field_map = {}">
              <el-option v-for="entity in entities" :key="entity.id" :label="entity.name" :value="entity.id" :disabled="!entity.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="数据关系" required>
            <el-select v-model="mappingForm.relation_id" filterable style="width: 100%" :loading="contextLoading" @change="mappingForm.field_map = {}">
              <el-option v-for="relation in selectedSchema?.relations || []" :key="relation.id" :label="relation.display_name" :value="relation.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="映射 key" required>
            <el-input v-model="mappingForm.mapping_key" maxlength="180" placeholder="如 service_item_details" />
          </el-form-item>
        </div>

        <el-alert
          v-if="selectedEntity && !selectedEntity.properties.some((property) => property.is_key)"
          type="warning"
          :closable="false"
          show-icon
          title="该对象类型还没有主键属性，请先回到本体模型补充主键。"
        />
        <div v-if="selectedEntity && selectedRelation" class="field-map-list">
          <div class="field-map-head"><span>对象属性</span><span>Dataset 字段</span></div>
          <div v-for="property in selectedEntity.properties" :key="property.id || property.api_name || property.name" class="field-map-row">
            <label :for="`semantic-field-${property.id}`">
              {{ property.name }}
              <el-tag v-if="property.is_key" size="small" type="warning" effect="plain">主键</el-tag>
            </label>
            <el-select
              :id="`semantic-field-${property.id}`"
              v-model="mappingForm.field_map[property.id || '']"
              clearable
              filterable
              style="width: 100%"
              :disabled="!property.id"
              placeholder="不映射"
            >
              <el-option v-for="field in selectedRelation.fields" :key="field.id" :label="field.source_name" :value="field.id" />
            </el-select>
          </div>
        </div>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="mappingDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveMapping">创建并激活</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type {
  DatasetRelation,
  DatasetSchema,
  DatasetVersion,
  Entity,
  LogicalDataset,
  ScenarioDatasetBinding,
  SemanticMapping,
} from '@/types'

const props = defineProps<{
  scenarioId: string
  canWrite: boolean
  entities: Entity[]
}>()

const datasets = ref<LogicalDataset[]>([])
const bindings = ref<ScenarioDatasetBinding[]>([])
const mappings = ref<SemanticMapping[]>([])
const versionsByDataset = ref<Record<string, DatasetVersion[]>>({})
const schemasByDataset = ref<Record<string, DatasetSchema[]>>({})
const loading = ref(false)
const contextLoading = ref(false)
const saving = ref(false)
const error = ref('')
const bindingDialog = ref(false)
const mappingDialog = ref(false)
const mappingError = ref('')
const bindingErrors = ref<Record<string, string>>({})
const bindingForm = ref({ dataset_id: '', dataset_version_id: '', binding_key: '' })
const mappingForm = ref({ binding_id: '', entity_id: '', relation_id: '', mapping_key: '', field_map: {} as Record<string, string> })

const activeMappingCount = computed(() => mappings.value.filter((item) => item.status === 'active').length)
const selectedVersions = computed(() => versionsByDataset.value[bindingForm.value.dataset_id] || [])
const selectedBinding = computed(() => bindings.value.find((item) => item.id === mappingForm.value.binding_id))
const selectedSchema = computed(() => {
  const binding = selectedBinding.value
  if (!binding) return undefined
  const versions = versionsByDataset.value[binding.dataset_id] || []
  const version = versions.find((item) => item.id === binding.resolved_dataset_version_id || item.id === binding.dataset_version_id)
  return (schemasByDataset.value[binding.dataset_id] || []).find((item) => item.id === version?.schema_id)
})
const selectedEntity = computed(() => props.entities.find((item) => item.id === mappingForm.value.entity_id))
const selectedRelation = computed<DatasetRelation | undefined>(() => selectedSchema.value?.relations.find((item) => item.id === mappingForm.value.relation_id))

async function ensureDatasetContext(datasetId: string) {
  if (!datasetId || (versionsByDataset.value[datasetId] && schemasByDataset.value[datasetId])) return
  contextLoading.value = true
  try {
    const [versions, schemas] = await Promise.all([
      api.listDatasetVersions(datasetId),
      api.listDatasetSchemas(datasetId),
    ])
    versionsByDataset.value = { ...versionsByDataset.value, [datasetId]: versions }
    schemasByDataset.value = { ...schemasByDataset.value, [datasetId]: schemas }
  } finally {
    contextLoading.value = false
  }
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [datasetRows, bindingRows, mappingRows] = await Promise.all([
      api.listLogicalDatasets(),
      api.listScenarioDatasetBindings(props.scenarioId),
      api.listSemanticMappings(props.scenarioId),
    ])
    datasets.value = datasetRows.filter((item) => item.lifecycle_status === 'active')
    bindings.value = bindingRows.filter((item) => item.status === 'active' && item.role === 'modeling_evidence')
    mappings.value = mappingRows
    await Promise.all([...new Set(bindings.value.map((item) => item.dataset_id))].map(ensureDatasetContext))
  } catch (reason: any) {
    error.value = reason?.message || '无法读取 Catalog 语义映射'
  } finally {
    loading.value = false
  }
}

function resetBindingForm() {
  bindingForm.value = { dataset_id: '', dataset_version_id: '', binding_key: '' }
  bindingErrors.value = {}
}

function resetMappingForm() {
  mappingForm.value = { binding_id: '', entity_id: '', relation_id: '', mapping_key: '', field_map: {} }
  mappingError.value = ''
}

function openBindingDialog() {
  resetBindingForm()
  bindingDialog.value = true
}

async function onBindingDatasetChange(datasetId: string) {
  bindingForm.value.dataset_version_id = ''
  await ensureDatasetContext(datasetId)
}

function validateBinding() {
  const errors: Record<string, string> = {}
  if (!bindingForm.value.dataset_id) errors.dataset_id = '请选择逻辑数据集'
  if (!bindingForm.value.dataset_version_id) errors.dataset_version_id = '请选择固定版本'
  if (!/^[a-z0-9][a-z0-9._-]{0,179}$/.test(bindingForm.value.binding_key)) {
    errors.binding_key = '绑定 key 格式无效'
  }
  bindingErrors.value = errors
  return !Object.keys(errors).length
}

async function saveBinding() {
  if (!validateBinding() || saving.value) return
  saving.value = true
  try {
    await api.createScenarioDatasetBinding(props.scenarioId, {
      dataset_id: bindingForm.value.dataset_id,
      binding_key: bindingForm.value.binding_key,
      environment: 'dev',
      role: 'modeling_evidence',
      binding_mode: 'pinned',
      dataset_version_id: bindingForm.value.dataset_version_id,
      dataset_head_id: null,
      is_required: false,
      status: 'active',
      config: {},
    })
    bindingDialog.value = false
    ElMessage.success('建模数据版本已固定')
    await loadAll()
  } catch (reason: any) {
    ElMessage.error(reason?.message || '固定建模数据版本失败')
  } finally {
    saving.value = false
  }
}

async function openMappingDialog() {
  resetMappingForm()
  mappingDialog.value = true
  const binding = bindings.value[0]
  if (binding) {
    mappingForm.value.binding_id = binding.id
    await onMappingBindingChange(binding.id)
  }
}

async function onMappingBindingChange(bindingId: string) {
  mappingForm.value.relation_id = ''
  mappingForm.value.field_map = {}
  const binding = bindings.value.find((item) => item.id === bindingId)
  if (binding) await ensureDatasetContext(binding.dataset_id)
}

async function saveMapping() {
  if (saving.value) return
  mappingError.value = ''
  const binding = selectedBinding.value
  const schema = selectedSchema.value
  const entity = selectedEntity.value
  const relation = selectedRelation.value
  if (!binding || !schema || !entity?.id || !relation || !/^[a-z0-9][a-z0-9._-]{0,179}$/.test(mappingForm.value.mapping_key)) {
    mappingError.value = '请完整选择建模版本、对象类型和数据关系，并填写有效的映射 key。'
    return
  }
  const keyProperties = entity.properties.filter((property) => property.is_key)
  if (!keyProperties.length || keyProperties.some((property) => !property.id || !mappingForm.value.field_map[property.id])) {
    mappingError.value = '对象类型的每个主键属性都必须映射到 Dataset 字段。'
    return
  }
  const fields = entity.properties.flatMap((property) => {
    const datasetFieldId = property.id ? mappingForm.value.field_map[property.id] : ''
    return property.id && datasetFieldId ? [{
      ontology_property_id: property.id,
      dataset_field_id: datasetFieldId,
      direction: 'input' as const,
      is_required: Boolean(property.is_required || property.is_key),
      transform: {},
    }] : []
  })
  if (!fields.length) {
    mappingError.value = '请至少映射一个对象属性。'
    return
  }
  saving.value = true
  try {
    await api.createSemanticMapping(props.scenarioId, {
      scenario_dataset_binding_id: binding.id,
      entity_id: entity.id,
      dataset_schema_id: schema.id,
      dataset_relation_id: relation.id,
      mapping_key: mappingForm.value.mapping_key,
      status: 'active',
      identifier_strategy: {},
      filter_expression: {},
      fields,
    })
    mappingDialog.value = false
    ElMessage.success('对象语义映射已创建并激活')
    await loadAll()
  } catch (reason: any) {
    mappingError.value = reason?.message || '对象语义映射创建失败'
  } finally {
    saving.value = false
  }
}

function entityName(entityId: string) {
  return props.entities.find((item) => item.id === entityId)?.name || entityId
}

function propertyName(entityId: string, propertyId: string) {
  return props.entities.find((item) => item.id === entityId)?.properties.find((item) => item.id === propertyId)?.name || propertyId
}

function bindingName(bindingId: string) {
  const binding = bindings.value.find((item) => item.id === bindingId)
  const dataset = datasets.value.find((item) => item.id === binding?.dataset_id)
  const versions = binding ? versionsByDataset.value[binding.dataset_id] || [] : []
  const version = versions.find((item) => item.id === binding?.resolved_dataset_version_id || item.id === binding?.dataset_version_id)
  return binding ? `${dataset?.name || binding.binding_key} · 版本 ${version?.version_number || '?'}` : bindingId
}

function relationName(schemaId: string, relationId: string) {
  for (const schemas of Object.values(schemasByDataset.value)) {
    const relation = schemas.find((item) => item.id === schemaId)?.relations.find((item) => item.id === relationId)
    if (relation) return relation.display_name
  }
  return relationId
}

function datasetFieldName(schemaId: string, fieldId: string) {
  for (const schemas of Object.values(schemasByDataset.value)) {
    const schema = schemas.find((item) => item.id === schemaId)
    for (const relation of schema?.relations || []) {
      const field = relation.fields.find((item) => item.id === fieldId)
      if (field) return field.source_name
    }
  }
  return fieldId
}

onMounted(loadAll)
</script>

<style scoped>
.semantic-panel { display: flex; flex-direction: column; gap: 14px; padding-bottom: 24px; }
.semantic-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.semantic-toolbar h2 { margin: 0; color: var(--text-1); font-size: 16px; letter-spacing: 0; }
.semantic-toolbar p { margin: 4px 0 0; color: var(--text-3); font-size: 12px; }
.semantic-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.semantic-summary { display: flex; flex-wrap: wrap; gap: 18px; color: var(--text-2); font-size: 12px; }
.semantic-summary b { color: var(--text-1); font-variant-numeric: tabular-nums; }
.semantic-cell { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.semantic-cell strong, .semantic-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.semantic-cell strong { color: var(--text-1); font-size: 13px; }
.semantic-cell small { color: var(--text-3); font-size: 11px; }
.field-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.dialog-note { margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.form-help { margin-top: 5px; color: var(--text-3); font-size: 12px; line-height: 1.5; }
.form-error-summary { margin-bottom: 16px; padding: 10px 12px; border: 1px solid var(--el-color-danger-light-5); border-radius: 6px; color: var(--el-color-danger); background: var(--el-color-danger-light-9); font-size: 13px; }
.field-map-list { display: flex; flex-direction: column; margin-top: 12px; border-top: 1px solid var(--border); }
.field-map-head, .field-map-row { display: grid; grid-template-columns: minmax(160px, .8fr) minmax(240px, 1.2fr); align-items: center; gap: 16px; padding: 9px 0; border-bottom: 1px solid var(--border); }
.field-map-head { color: var(--text-3); font-size: 12px; }
.field-map-row label { display: flex; align-items: center; gap: 8px; color: var(--text-1); font-size: 13px; }
@media (max-width: 720px) {
  .semantic-toolbar { align-items: stretch; flex-direction: column; }
  .semantic-actions { justify-content: flex-start; }
  .form-grid, .field-map-head, .field-map-row { grid-template-columns: 1fr; }
  .field-map-head { display: none; }
}
</style>
