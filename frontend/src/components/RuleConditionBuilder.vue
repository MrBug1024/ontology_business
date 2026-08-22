<script setup lang="ts">
import { computed, ref, watch } from 'vue'

type ConditionRow = {
  id: string
  field: string
  op: string
  valueType: 'text' | 'number' | 'boolean'
  value: string | number | boolean
}

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  fields?: string[]
}>(), {
  modelValue: () => ({}),
  fields: () => [],
})
const emit = defineEmits<{ (event: 'update:modelValue', value: Record<string, any>): void }>()

const logic = ref<'and' | 'or'>('and')
const rows = ref<ConditionRow[]>([])
let lastEmitted = ''

const operators = [
  { label: '等于', value: '==' },
  { label: '不等于', value: '!=' },
  { label: '大于', value: '>' },
  { label: '大于等于', value: '>=' },
  { label: '小于', value: '<' },
  { label: '小于等于', value: '<=' },
  { label: '包含', value: 'contains' },
  { label: '不包含', value: 'not_contains' },
  { label: '属于列表', value: 'in' },
  { label: '不属于列表', value: 'not_in' },
  { label: '为空', value: 'is_null' },
  { label: '不为空', value: 'is_not_null' },
]
const noValueOperators = new Set(['is_null', 'is_not_null'])
const listOperators = new Set(['in', 'not_in'])

function inferValueType(value: any): ConditionRow['valueType'] {
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'number') return 'number'
  return 'text'
}
function newRow(condition: Record<string, any> = {}): ConditionRow {
  const sourceValue = Array.isArray(condition.value) ? condition.value.join(', ') : condition.value
  return {
    id: `${Date.now()}-${Math.random()}`,
    field: String(condition.field || ''),
    op: String(condition.op || '=='),
    valueType: inferValueType(sourceValue),
    value: sourceValue ?? '',
  }
}
function leafConditions(value: Record<string, any>) {
  if (value?.op === 'and' || value?.op === 'or') {
    logic.value = value.op
    return Array.isArray(value.conditions) ? value.conditions.filter((item) => item && typeof item === 'object') : []
  }
  logic.value = 'and'
  return value?.field ? [value] : []
}
function load(value: Record<string, any>) {
  const normalized = JSON.stringify(value || {})
  if (normalized === lastEmitted) return
  rows.value = leafConditions(value || {}).map((condition) => newRow(condition))
}
function castValue(row: ConditionRow) {
  if (noValueOperators.has(row.op)) return null
  if (listOperators.has(row.op)) {
    return String(row.value ?? '').split(/[,，\n]/).map((item) => item.trim()).filter(Boolean).map((item) => (
      row.valueType === 'number' ? Number(item) : row.valueType === 'boolean' ? item === 'true' : item
    ))
  }
  if (row.valueType === 'number') return Number(row.value)
  if (row.valueType === 'boolean') return row.value === true || row.value === 'true'
  return String(row.value ?? '')
}
function toCondition() {
  const conditions = rows.value
    .filter((row) => row.field.trim())
    .map((row) => ({ field: row.field.trim(), op: row.op, value: castValue(row) }))
  if (!conditions.length) return {}
  return conditions.length === 1 ? conditions[0] : { op: logic.value, conditions }
}
function addCondition() {
  rows.value.push(newRow())
}
function removeCondition(id: string) {
  rows.value = rows.value.filter((row) => row.id !== id)
}

const summary = computed(() => rows.value.length
  ? `${logic.value === 'and' ? '全部满足' : '任一满足'} · ${rows.value.length} 个条件`
  : '尚未配置判断条件')

watch(() => props.modelValue, (value) => load(value || {}), { immediate: true, deep: true })
watch([rows, logic], () => {
  const condition = toCondition()
  lastEmitted = JSON.stringify(condition)
  emit('update:modelValue', condition)
}, { deep: true })
</script>

<template>
  <div class="condition-builder">
    <div class="condition-toolbar">
      <span>{{ summary }}</span>
      <el-radio-group v-model="logic" size="small" aria-label="条件组合方式">
        <el-radio-button value="and">全部满足</el-radio-button>
        <el-radio-button value="or">任一满足</el-radio-button>
      </el-radio-group>
    </div>
    <div v-if="rows.length" class="condition-list">
      <div v-for="(row, index) in rows" :key="row.id" class="condition-row">
        <span class="condition-index">{{ index + 1 }}</span>
        <el-select v-model="row.field" filterable allow-create default-first-option placeholder="选择或输入属性" aria-label="规则属性">
          <el-option v-for="field in fields" :key="field" :label="field" :value="field" />
        </el-select>
        <el-select v-model="row.op" aria-label="比较方式">
          <el-option v-for="operator in operators" :key="operator.value" :label="operator.label" :value="operator.value" />
        </el-select>
        <template v-if="!noValueOperators.has(row.op)">
          <el-select v-model="row.valueType" class="value-type" aria-label="比较值类型">
            <el-option label="文本" value="text" />
            <el-option label="数值" value="number" />
            <el-option label="是 / 否" value="boolean" />
          </el-select>
          <el-switch v-if="row.valueType === 'boolean'" v-model="row.value" inline-prompt active-text="是" inactive-text="否" aria-label="比较值" />
          <el-input-number v-else-if="row.valueType === 'number' && !listOperators.has(row.op)" v-model="row.value" controls-position="right" aria-label="比较数值" />
          <el-input v-else v-model="row.value" :placeholder="listOperators.has(row.op) ? '多个值用逗号分隔' : '比较值'" aria-label="比较值" />
        </template>
        <span v-else class="no-value">无需填写值</span>
        <el-button text type="danger" circle aria-label="删除条件" @click="removeCondition(row.id)"><el-icon><Delete /></el-icon></el-button>
      </div>
    </div>
    <div v-else class="condition-empty">添加条件后，系统会按属性值判断规则是否命中。</div>
    <el-button plain @click="addCondition"><el-icon><Plus /></el-icon>添加条件</el-button>
  </div>
</template>

<style scoped>
.condition-builder { display: grid; gap: 10px; width: 100%; }
.condition-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.condition-toolbar > span { color: var(--text-3); font-size: 11px; }
.condition-list { display: grid; gap: 8px; }
.condition-row { display: grid; grid-template-columns: 24px minmax(130px, .8fr) minmax(120px, .7fr) 82px minmax(130px, 1fr) 36px; gap: 7px; align-items: center; padding: 8px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.condition-index { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; background: var(--primary-soft); color: var(--primary-600); font-size: 10px; font-weight: 760; }
.no-value { grid-column: 4 / 6; color: var(--text-3); font-size: 11px; }
.condition-empty { padding: 17px; border: 1px dashed var(--border-strong); border-radius: 10px; color: var(--text-3); font-size: 11px; text-align: center; }
@media (max-width: 760px) {
  .condition-toolbar { align-items: flex-start; flex-direction: column; }
  .condition-row { grid-template-columns: 24px minmax(0, 1fr) 36px; }
  .condition-row > :not(.condition-index):not(.el-button) { grid-column: 2; }
  .condition-row > .el-button { grid-column: 3; grid-row: 1; }
}
</style>
