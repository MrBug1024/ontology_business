<script setup lang="ts">
import { ref, watch } from 'vue'

type ValueType = 'text' | 'number' | 'boolean' | 'list'
type ValueRow = { id: string; key: string; type: ValueType; value: string | number | boolean | string[] }

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  readonly?: boolean
  keyPlaceholder?: string
  valuePlaceholder?: string
  emptyText?: string
  stringOnly?: boolean
  flatKeys?: boolean
  maskValues?: boolean
}>(), {
  modelValue: () => ({}),
  readonly: false,
  keyPlaceholder: '字段名称',
  valuePlaceholder: '字段值或变量引用',
  emptyText: '暂无字段，可按需添加',
  stringOnly: false,
  flatKeys: false,
  maskValues: false,
})
const emit = defineEmits<{ (event: 'update:modelValue', value: Record<string, any>): void }>()

const rows = ref<ValueRow[]>([])
let lastEmitted = ''

function inferType(value: any): ValueType {
  if (Array.isArray(value)) return 'list'
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'number') return 'number'
  return 'text'
}
function flatten(value: Record<string, any>, prefix = '', output: Array<[string, any]> = []) {
  for (const [key, item] of Object.entries(value || {})) {
    const path = prefix ? `${prefix}.${key}` : key
    if (item && typeof item === 'object' && !Array.isArray(item)) flatten(item, path, output)
    else output.push([path, item])
  }
  return output
}
function load(value: Record<string, any>) {
  const normalized = JSON.stringify(value || {})
  if (normalized === lastEmitted) return
  const entries = props.flatKeys ? Object.entries(value || {}) : flatten(value || {})
  rows.value = entries.map(([key, item], index) => ({
    id: `${Date.now()}-${index}-${key}`,
    key,
    type: inferType(item),
    value: Array.isArray(item) ? item.map((entry) => String(entry)) : item ?? '',
  }))
}
function cast(row: ValueRow) {
  if (props.stringOnly) return String(row.value ?? '')
  if (row.type === 'number') return Number(row.value)
  if (row.type === 'boolean') return row.value === true || row.value === 'true'
  if (row.type === 'list') return Array.isArray(row.value) ? row.value : String(row.value || '').split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
  return String(row.value ?? '')
}
function assignPath(target: Record<string, any>, path: string, value: any) {
  if (props.flatKeys) {
    const key = path.trim()
    if (key) target[key] = value
    return
  }
  const segments = path.split('.').map((part) => part.trim()).filter(Boolean)
  if (!segments.length) return
  let cursor = target
  segments.slice(0, -1).forEach((segment) => {
    if (!cursor[segment] || typeof cursor[segment] !== 'object' || Array.isArray(cursor[segment])) cursor[segment] = {}
    cursor = cursor[segment]
  })
  cursor[segments[segments.length - 1]] = value
}
function toObject() {
  const value: Record<string, any> = {}
  rows.value.forEach((row) => {
    if (row.key.trim()) assignPath(value, row.key, cast(row))
  })
  return value
}
function addRow() {
  rows.value.push({ id: `${Date.now()}-${Math.random()}`, key: '', type: 'text', value: '' })
}
function removeRow(id: string) {
  rows.value = rows.value.filter((row) => row.id !== id)
}

watch(() => props.modelValue, (value) => load(value || {}), { immediate: true, deep: true })
watch(rows, () => {
  if (props.readonly) return
  const value = toObject()
  lastEmitted = JSON.stringify(value)
  emit('update:modelValue', value)
}, { deep: true })
</script>

<template>
  <div class="kv-editor" :class="{ 'is-readonly': readonly, 'is-string-only': stringOnly }">
    <div v-if="rows.length" class="kv-list">
      <div v-for="row in rows" :key="row.id" class="kv-row">
        <template v-if="readonly">
          <b>{{ row.key }}</b>
          <span>{{ Array.isArray(row.value) ? row.value.join('、') : String(row.value) }}</span>
        </template>
        <template v-else>
          <el-input v-model.trim="row.key" :placeholder="keyPlaceholder" aria-label="字段名称" />
          <el-select v-if="!stringOnly" v-model="row.type" aria-label="字段值类型">
            <el-option label="文本" value="text" />
            <el-option label="数值" value="number" />
            <el-option label="是 / 否" value="boolean" />
            <el-option label="列表" value="list" />
          </el-select>
          <el-switch v-if="!stringOnly && row.type === 'boolean'" v-model="row.value" inline-prompt active-text="是" inactive-text="否" aria-label="字段值" />
          <el-input-number v-else-if="!stringOnly && row.type === 'number'" v-model="row.value" controls-position="right" aria-label="字段数值" />
          <el-select v-else-if="!stringOnly && row.type === 'list'" v-model="row.value" multiple filterable allow-create default-first-option :placeholder="valuePlaceholder" aria-label="字段列表值" />
          <el-input v-else v-model="row.value" :type="maskValues ? 'password' : 'text'" :show-password="maskValues" :placeholder="valuePlaceholder" aria-label="字段值" />
          <el-button text type="danger" circle aria-label="删除字段" @click="removeRow(row.id)"><el-icon><Delete /></el-icon></el-button>
        </template>
      </div>
    </div>
    <div v-else class="kv-empty">{{ emptyText }}</div>
    <el-button v-if="!readonly" plain @click="addRow"><el-icon><Plus /></el-icon>添加字段</el-button>
  </div>
</template>

<style scoped>
.kv-editor { display: grid; gap: 9px; width: 100%; }
.kv-list { display: grid; gap: 7px; }
.kv-row { display: grid; grid-template-columns: minmax(120px, .75fr) 88px minmax(145px, 1.25fr) 36px; gap: 7px; align-items: center; padding: 8px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }
.kv-empty { padding: 15px; border: 1px dashed var(--border-strong); border-radius: 9px; color: var(--text-3); font-size: 11px; text-align: center; }
.is-readonly .kv-row { grid-template-columns: minmax(120px, .7fr) minmax(0, 1.3fr); }
.is-string-only .kv-row { grid-template-columns: minmax(150px, .8fr) minmax(180px, 1.2fr) 36px; }
.is-readonly .kv-row b { color: var(--text-2); font-size: 11px; }
.is-readonly .kv-row span { overflow-wrap: anywhere; color: var(--text-3); font-size: 11px; }
@media (max-width: 680px) {
  .kv-row { grid-template-columns: minmax(0, 1fr) 88px 36px; }
  .is-string-only .kv-row { grid-template-columns: minmax(0, 1fr) 36px; }
  .is-string-only .kv-row > :nth-child(2) { grid-column: 1; grid-row: 2; }
  .is-string-only .kv-row > .el-button { grid-column: 2; grid-row: 1; }
  .kv-row > :nth-child(3) { grid-column: 1 / 3; grid-row: 2; }
  .kv-row > .el-button { grid-column: 3; grid-row: 1; }
}
</style>
