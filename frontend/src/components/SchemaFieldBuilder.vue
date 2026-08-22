<script setup lang="ts">
import { computed, ref, watch } from 'vue'

type SchemaObject = Record<string, any>
type FieldRow = {
  id: string
  name: string
  type: string
  description: string
  required: boolean
  enumText: string
}

const props = withDefaults(defineProps<{
  modelValue?: SchemaObject
  readonly?: boolean
  emptyText?: string
}>(), {
  modelValue: () => ({}),
  readonly: false,
  emptyText: '尚未定义字段',
})
const emit = defineEmits<{ (event: 'update:modelValue', value: SchemaObject): void }>()

const rows = ref<FieldRow[]>([])
let lastEmitted = ''

function rowType(schema: SchemaObject) {
  if (schema.type === 'string' && schema.format === 'date') return 'date'
  if (schema.type === 'string' && schema.format === 'date-time') return 'datetime'
  return String(schema.type || 'string')
}
function schemaRoot(value: SchemaObject) {
  if (value?.properties && typeof value.properties === 'object') {
    return { properties: value.properties as SchemaObject, required: Array.isArray(value.required) ? value.required : [] }
  }
  const properties = value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  return { properties, required: [] as string[] }
}
function load(value: SchemaObject) {
  const normalized = JSON.stringify(value || {})
  if (normalized === lastEmitted) return
  const root = schemaRoot(value || {})
  rows.value = Object.entries(root.properties).map(([name, raw], index) => {
    const schema = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as SchemaObject : { type: 'string' }
    return {
      id: `${Date.now()}-${index}-${name}`,
      name,
      type: rowType(schema),
      description: String(schema.description || ''),
      required: root.required.includes(name) || schema.required === true,
      enumText: Array.isArray(schema.enum) ? schema.enum.join(', ') : '',
    }
  })
}

function castEnumValue(value: string, type: string) {
  const trimmed = value.trim()
  if (type === 'integer') return Number.parseInt(trimmed, 10)
  if (type === 'number') return Number(trimmed)
  if (type === 'boolean') return trimmed === 'true' ? true : trimmed === 'false' ? false : trimmed
  return trimmed
}
function toSchema() {
  const properties: SchemaObject = {}
  const required: string[] = []
  for (const row of rows.value) {
    const name = row.name.trim()
    if (!name) continue
    const schema: SchemaObject = {
      type: row.type === 'date' || row.type === 'datetime' ? 'string' : row.type,
    }
    if (row.type === 'date') schema.format = 'date'
    if (row.type === 'datetime') schema.format = 'date-time'
    if (row.description.trim()) schema.description = row.description.trim()
    const enumValues = row.enumText.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
    if (enumValues.length) schema.enum = enumValues.map((item) => castEnumValue(item, row.type))
    properties[name] = schema
    if (row.required) required.push(name)
  }
  return { type: 'object', properties, required, additionalProperties: false }
}
function typeLabel(type: string) {
  return ({ string: '文本', integer: '整数', number: '数值', boolean: '是/否', date: '日期', datetime: '日期时间', array: '列表', object: '对象' } as Record<string, string>)[type] || type
}
function addField() {
  rows.value.push({ id: `${Date.now()}-${Math.random()}`, name: '', type: 'string', description: '', required: false, enumText: '' })
}
function removeField(id: string) {
  rows.value = rows.value.filter((row) => row.id !== id)
}

const duplicateNames = computed(() => {
  const counts = new Map<string, number>()
  for (const row of rows.value) {
    const key = row.name.trim()
    if (key) counts.set(key, (counts.get(key) || 0) + 1)
  }
  return new Set([...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name))
})

watch(() => props.modelValue, (value) => load(value || {}), { immediate: true, deep: true })
watch(rows, () => {
  if (props.readonly) return
  const schema = toSchema()
  lastEmitted = JSON.stringify(schema)
  emit('update:modelValue', schema)
}, { deep: true })
</script>

<template>
  <div class="schema-builder" :class="{ 'is-readonly': readonly }">
    <div v-if="rows.length" class="schema-fields">
      <div v-if="!readonly" class="schema-head" aria-hidden="true">
        <span>字段名称</span><span>类型</span><span>说明</span><span>必填</span><span></span>
      </div>
      <div v-for="row in rows" :key="row.id" class="schema-row">
        <template v-if="readonly">
          <div class="schema-summary-name"><b>{{ row.name }}</b><small>{{ row.description || '未填写说明' }}</small></div>
          <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
          <el-tag v-if="row.required" size="small" type="warning" effect="plain">必填</el-tag>
          <span v-if="row.enumText" class="schema-enum">可选：{{ row.enumText }}</span>
        </template>
        <template v-else>
          <el-form-item :error="duplicateNames.has(row.name.trim()) ? '字段名称重复' : ''">
            <el-input v-model.trim="row.name" placeholder="如 project_id" aria-label="字段名称" />
          </el-form-item>
          <el-select v-model="row.type" aria-label="字段类型">
            <el-option label="文本" value="string" />
            <el-option label="整数" value="integer" />
            <el-option label="数值" value="number" />
            <el-option label="是 / 否" value="boolean" />
            <el-option label="日期" value="date" />
            <el-option label="日期时间" value="datetime" />
            <el-option label="列表" value="array" />
            <el-option label="对象" value="object" />
          </el-select>
          <div class="schema-description">
            <el-input v-model="row.description" placeholder="这个字段表示什么" aria-label="字段说明" />
            <el-input v-model="row.enumText" placeholder="可选值，用逗号分隔（可选）" aria-label="字段可选值" />
          </div>
          <el-switch v-model="row.required" aria-label="是否必填" />
          <el-button text type="danger" circle aria-label="删除字段" @click="removeField(row.id)"><el-icon><Delete /></el-icon></el-button>
        </template>
      </div>
    </div>
    <div v-else class="schema-empty">{{ emptyText }}</div>
    <el-button v-if="!readonly" class="add-field" plain @click="addField"><el-icon><Plus /></el-icon>添加字段</el-button>
  </div>
</template>

<style scoped>
.schema-builder { display: grid; gap: 10px; width: 100%; }
.schema-fields { display: grid; gap: 8px; }
.schema-head, .schema-row { display: grid; grid-template-columns: minmax(135px, .8fr) minmax(105px, .55fr) minmax(210px, 1.4fr) 48px 36px; gap: 8px; align-items: start; }
.schema-head { padding: 0 2px; color: var(--text-3); font-size: 10px; font-weight: 700; }
.schema-row { padding: 9px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.schema-row :deep(.el-form-item) { margin: 0; }
.schema-description { display: grid; gap: 6px; }
.schema-empty { padding: 18px; border: 1px dashed var(--border-strong); border-radius: 10px; color: var(--text-3); font-size: 11px; text-align: center; }
.add-field { justify-self: start; }
.is-readonly .schema-fields { gap: 5px; }
.is-readonly .schema-row { grid-template-columns: minmax(150px, 1fr) auto auto; align-items: center; padding: 7px 9px; }
.schema-summary-name { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.schema-summary-name b { color: var(--text); font-size: 11px; }
.schema-summary-name small { overflow: hidden; color: var(--text-3); font-size: 9.5px; text-overflow: ellipsis; white-space: nowrap; }
.schema-enum { grid-column: 1 / -1; color: var(--text-3); font-size: 9.5px; }
@media (max-width: 760px) {
  .schema-head { display: none; }
  .schema-row { grid-template-columns: 1fr 110px 44px 36px; }
  .schema-description { grid-column: 1 / -1; grid-row: 2; }
}
</style>
