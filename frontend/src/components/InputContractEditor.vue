<template>
  <section class="contract-editor" aria-labelledby="input-contract-heading">
    <div class="contract-heading-row">
      <div>
        <h3 id="input-contract-heading">结构校验</h3>
        <span class="contract-state">{{ modelValue.enabled ? '已启用' : '未启用' }}</span>
      </div>
      <el-switch
        :model-value="modelValue.enabled"
        :disabled="!supportsStructuralContract"
        aria-label="启用结构校验"
        @update:model-value="updateEnabled"
      />
    </div>

    <div v-if="modelValue.enabled" class="contract-body">
      <el-form-item label="契约来源" required>
        <el-radio-group
          :model-value="modelValue.source"
          aria-label="结构契约来源"
          @update:model-value="updateSource"
        >
          <el-radio-button v-if="canUseDatasetSource" value="dataset_schema">
            Dataset Schema
          </el-radio-button>
          <el-radio-button value="manual">手工配置</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <div v-if="modelValue.source === 'dataset_schema'" class="contract-source-grid">
        <el-form-item label="建模逻辑数据集" required>
          <el-select
            :model-value="modelValue.datasetId"
            filterable
            :loading="datasetsLoading"
            placeholder="选择建模资料数据集"
            style="width: 100%"
            @update:model-value="updateDataset"
          >
            <el-option
              v-for="dataset in datasets"
              :key="dataset.id"
              :label="dataset.name"
              :value="dataset.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Dataset Schema" required>
          <el-select
            :model-value="modelValue.datasetSchemaId"
            :disabled="!modelValue.datasetId"
            :loading="schemasLoading"
            placeholder="选择结构版本"
            style="width: 100%"
            @update:model-value="updateDatasetSchema"
          >
            <el-option
              v-for="schema in schemas"
              :key="schema.id"
              :label="`Schema v${schema.schema_version}`"
              :value="schema.id"
            />
          </el-select>
        </el-form-item>
      </div>

      <div v-else class="manual-contract">
        <fieldset
          v-for="(relation, relationIndex) in modelValue.relations"
          :key="relation.clientId"
          class="contract-relation"
        >
          <legend>关系 {{ relationIndex + 1 }}</legend>
          <el-tooltip content="删除关系" placement="top">
            <el-button
              class="relation-remove icon-action"
              text
              type="danger"
              :disabled="modelValue.relations.length === 1"
              :aria-label="`删除关系 ${relationIndex + 1}`"
              @click="removeRelation(relation.clientId)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </el-tooltip>

          <div class="relation-options">
            <el-form-item label="最小数据行数">
              <el-input-number
                :model-value="relation.minimumDataRows"
                :min="0"
                :max="1000000000"
                :step="1"
                controls-position="right"
                :aria-label="`关系 ${relationIndex + 1} 最小数据行数`"
                @update:model-value="updateMinimumRows(relation.clientId, $event)"
              />
            </el-form-item>
            <el-checkbox
              :model-value="relation.allowAdditionalFields"
              @update:model-value="updateAdditionalFields(relation.clientId, $event)"
            >
              允许额外字段
            </el-checkbox>
          </div>

          <div
            class="contract-fields"
            role="group"
            :aria-label="`关系 ${relationIndex + 1} 字段`"
          >
            <div
              v-for="(field, fieldIndex) in relation.fields"
              :key="field.clientId"
              class="contract-field-row"
            >
              <label class="field-control">
                <span>字段名</span>
                <el-input
                  :model-value="field.name"
                  maxlength="180"
                  :aria-label="`关系 ${relationIndex + 1} 字段 ${fieldIndex + 1} 名称`"
                  @update:model-value="updateFieldName(relation.clientId, field.clientId, $event)"
                />
              </label>
              <label class="field-control">
                <span>字段类型</span>
                <el-select
                  :model-value="field.logicalType"
                  :aria-label="`关系 ${relationIndex + 1} 字段 ${fieldIndex + 1} 类型`"
                  @update:model-value="updateFieldType(relation.clientId, field.clientId, $event)"
                >
                  <el-option
                    v-for="option in logicalTypeOptions"
                    :key="option.value"
                    :label="option.label"
                    :value="option.value"
                  />
                </el-select>
              </label>
              <el-checkbox
                :model-value="field.required"
                @update:model-value="updateFieldRequired(relation.clientId, field.clientId, $event)"
              >
                必填
              </el-checkbox>
              <el-tooltip content="删除字段" placement="top">
                <el-button
                  class="icon-action"
                  text
                  type="danger"
                  :disabled="relation.fields.length === 1"
                  :aria-label="`删除关系 ${relationIndex + 1} 的字段 ${fieldIndex + 1}`"
                  @click="removeField(relation.clientId, field.clientId)"
                >
                  <el-icon><Delete /></el-icon>
                </el-button>
              </el-tooltip>
            </div>
          </div>

          <el-button
            text
            type="primary"
            :disabled="totalFieldCount >= MAX_INPUT_CONTRACT_FIELDS"
            @click="addField(relation.clientId)"
          >
            <el-icon><Plus /></el-icon>
            添加字段
          </el-button>
        </fieldset>

        <div class="manual-contract-actions">
          <el-button
            text
            type="primary"
            :disabled="modelValue.relations.length >= MAX_INPUT_CONTRACT_RELATIONS"
            @click="addRelation"
          >
            <el-icon><Plus /></el-icon>
            添加关系
          </el-button>
          <el-checkbox
            :model-value="modelValue.allowAdditionalRelations"
            @update:model-value="updateAdditionalRelations"
          >
            允许额外关系
          </el-checkbox>
        </div>
      </div>
    </div>

    <p v-else-if="!supportsStructuralContract" class="contract-unavailable">
      当前输入介质不支持表格结构校验。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import type { DatasetSchema, LogicalDataset, ScenarioCapabilityPort } from '@/types'
import {
  MAX_INPUT_CONTRACT_FIELDS,
  MAX_INPUT_CONTRACT_RELATIONS,
  createInputContractField,
  createInputContractRelation,
} from '@/utils/inputContracts'
import type {
  InputContractDraft,
  InputContractFieldDraft,
  InputContractLogicalType,
  InputContractRelationDraft,
  InputContractSource,
} from '@/utils/inputContracts'

const props = defineProps<{
  modelValue: InputContractDraft
  mediaKind: ScenarioCapabilityPort['media_kind']
  datasets: LogicalDataset[]
  datasetsLoading: boolean
  schemas: DatasetSchema[]
  schemasLoading: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: InputContractDraft]
  'dataset-change': [datasetId: string]
}>()

const logicalTypeOptions: Array<{ value: InputContractLogicalType; label: string }> = [
  { value: 'string', label: '文本' },
  { value: 'integer', label: '整数' },
  { value: 'number', label: '数值' },
  { value: 'boolean', label: '布尔值' },
  { value: 'date', label: '日期' },
  { value: 'datetime', label: '日期时间' },
  { value: 'object', label: '对象' },
  { value: 'null', label: '空值' },
  { value: 'unknown', label: '未知' },
]

const supportsStructuralContract = computed(() => (
  ['dataset', 'structured', 'connector'].includes(props.mediaKind)
))
const canUseDatasetSource = computed(() => props.mediaKind === 'dataset')
const totalFieldCount = computed(() => props.modelValue.relations.reduce(
  (total, relation) => total + relation.fields.length,
  0,
))

function publish(patch: Partial<InputContractDraft>) {
  emit('update:modelValue', { ...props.modelValue, ...patch })
}

function updateEnabled(value: string | number | boolean) {
  const enabled = Boolean(value) && supportsStructuralContract.value
  publish({
    enabled,
    source: canUseDatasetSource.value ? props.modelValue.source : 'manual',
  })
}

function updateSource(value: string | number | boolean | undefined) {
  const source: InputContractSource = value === 'dataset_schema' && canUseDatasetSource.value
    ? 'dataset_schema'
    : 'manual'
  publish({ source })
}

function updateDataset(value: unknown) {
  const datasetId = typeof value === 'string' ? value : ''
  publish({ datasetId, datasetSchemaId: '' })
  emit('dataset-change', datasetId)
}

function updateDatasetSchema(value: unknown) {
  publish({ datasetSchemaId: typeof value === 'string' ? value : '' })
}

function updateRelation(
  relationId: string,
  patch: Partial<InputContractRelationDraft>,
) {
  publish({
    relations: props.modelValue.relations.map((relation) => (
      relation.clientId === relationId ? { ...relation, ...patch } : relation
    )),
  })
}

function updateMinimumRows(relationId: string, value: number | undefined) {
  updateRelation(relationId, {
    minimumDataRows: Number.isInteger(value) ? Number(value) : 0,
  })
}

function updateAdditionalFields(relationId: string, value: unknown) {
  updateRelation(relationId, { allowAdditionalFields: Boolean(value) })
}

function addRelation() {
  if (props.modelValue.relations.length >= MAX_INPUT_CONTRACT_RELATIONS) return
  publish({ relations: [...props.modelValue.relations, createInputContractRelation()] })
}

function removeRelation(relationId: string) {
  if (props.modelValue.relations.length <= 1) return
  publish({
    relations: props.modelValue.relations.filter((relation) => relation.clientId !== relationId),
  })
}

function updateField(
  relationId: string,
  fieldId: string,
  patch: Partial<InputContractFieldDraft>,
) {
  const relation = props.modelValue.relations.find((item) => item.clientId === relationId)
  if (!relation) return
  updateRelation(relationId, {
    fields: relation.fields.map((field) => (
      field.clientId === fieldId ? { ...field, ...patch } : field
    )),
  })
}

function updateFieldType(relationId: string, fieldId: string, value: unknown) {
  const selected = logicalTypeOptions.find((option) => option.value === value)
  if (selected) updateField(relationId, fieldId, { logicalType: selected.value })
}

function updateFieldName(relationId: string, fieldId: string, value: unknown) {
  updateField(relationId, fieldId, { name: typeof value === 'string' ? value : '' })
}

function updateFieldRequired(relationId: string, fieldId: string, value: unknown) {
  updateField(relationId, fieldId, { required: Boolean(value) })
}

function updateAdditionalRelations(value: unknown) {
  publish({ allowAdditionalRelations: Boolean(value) })
}

function addField(relationId: string) {
  if (totalFieldCount.value >= MAX_INPUT_CONTRACT_FIELDS) return
  const relation = props.modelValue.relations.find((item) => item.clientId === relationId)
  if (!relation) return
  updateRelation(relationId, { fields: [...relation.fields, createInputContractField()] })
}

function removeField(relationId: string, fieldId: string) {
  const relation = props.modelValue.relations.find((item) => item.clientId === relationId)
  if (!relation || relation.fields.length <= 1) return
  updateRelation(relationId, {
    fields: relation.fields.filter((field) => field.clientId !== fieldId),
  })
}
</script>

<style scoped>
.contract-editor {
  margin-top: 4px;
  padding-top: 14px;
  border-top: 1px solid var(--border-soft);
}
.contract-heading-row,
.relation-options,
.manual-contract-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.contract-heading-row h3 {
  display: inline;
  margin: 0;
  color: var(--text-1);
  font-size: 14px;
  letter-spacing: 0;
}
.contract-state {
  margin-left: 8px;
  color: var(--text-3);
  font-size: 12px;
}
.contract-body { margin-top: 14px; }
.contract-source-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}
.manual-contract { display: flex; flex-direction: column; gap: 14px; }
.contract-relation {
  position: relative;
  min-width: 0;
  margin: 0;
  padding: 12px 0 0;
  border: 0;
  border-top: 1px solid var(--border-soft);
}
.contract-relation legend {
  padding: 0 44px 0 0;
  color: var(--text-1);
  font-size: 13px;
  font-weight: 600;
}
.relation-remove { position: absolute; top: 0; right: 0; }
.icon-action { width: 44px; height: 44px; }
.relation-options { align-items: flex-end; justify-content: flex-start; flex-wrap: wrap; }
.relation-options :deep(.el-form-item) { margin-bottom: 10px; }
.contract-fields { display: flex; flex-direction: column; gap: 8px; margin-bottom: 4px; }
.contract-field-row {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) minmax(120px, 150px) auto 44px;
  align-items: center;
  gap: 8px;
}
.field-control { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.field-control > span { color: var(--text-2); font-size: 12px; }
.contract-unavailable {
  margin: 8px 0 0;
  color: var(--text-3);
  font-size: 12px;
}
@media (max-width: 720px) {
  .contract-source-grid,
  .contract-field-row { grid-template-columns: 1fr; }
  .contract-field-row { padding-bottom: 10px; border-bottom: 1px solid var(--border-soft); }
  .contract-field-row .icon-action { justify-self: end; }
  .relation-options,
  .manual-contract-actions { align-items: flex-start; flex-direction: column; }
}
</style>
