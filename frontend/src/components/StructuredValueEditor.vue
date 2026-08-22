<script setup lang="ts">
import { ref, watch } from 'vue'

defineOptions({ name: 'StructuredValueEditor' })

type ValueKind = 'object' | 'array' | 'text' | 'number' | 'boolean' | 'null'
type ObjectRow = { id: string; key: string; value: unknown }
type ArrayRow = { id: string; value: unknown }

const props = withDefaults(defineProps<{
  modelValue?: unknown
  root?: boolean
}>(), {
  modelValue: () => ({}),
  root: false,
})
const emit = defineEmits<{ (event: 'update:modelValue', value: unknown): void }>()

const kind = ref<ValueKind>('text')
const scalar = ref<string | number | boolean>('')
const objectRows = ref<ObjectRow[]>([])
const arrayRows = ref<ArrayRow[]>([])
let lastEmitted = ''

function uid() { return `${Date.now()}-${Math.random()}` }
function kindOf(value: unknown): ValueKind {
  if (Array.isArray(value)) return 'array'
  if (value && typeof value === 'object') return 'object'
  if (props.root) return 'object'
  if (typeof value === 'number') return 'number'
  if (typeof value === 'boolean') return 'boolean'
  if (value === null) return 'null'
  return 'text'
}
function serialized(value: unknown) {
  try { return JSON.stringify(value) } catch { return '' }
}
function load(value: unknown) {
  if (serialized(value) === lastEmitted) return
  kind.value = kindOf(value)
  if (kind.value === 'object') {
    objectRows.value = Object.entries(value as Record<string, unknown>).map(([key, item]) => ({ id: uid(), key, value: item }))
    arrayRows.value = []
  } else if (kind.value === 'array') {
    arrayRows.value = (value as unknown[]).map((item) => ({ id: uid(), value: item }))
    objectRows.value = []
  } else {
    scalar.value = value === null || value === undefined ? '' : value as string | number | boolean
    objectRows.value = []
    arrayRows.value = []
  }
}
function publish(value: unknown) {
  lastEmitted = serialized(value)
  emit('update:modelValue', value)
}
function changeKind(next: ValueKind) {
  kind.value = next
  if (next === 'object') { objectRows.value = []; publish({}) }
  else if (next === 'array') { arrayRows.value = []; publish([]) }
  else if (next === 'number') { scalar.value = 0; publish(0) }
  else if (next === 'boolean') { scalar.value = false; publish(false) }
  else if (next === 'null') { scalar.value = ''; publish(null) }
  else { scalar.value = ''; publish('') }
}
function publishScalar() {
  if (kind.value === 'number') publish(Number(scalar.value || 0))
  else if (kind.value === 'boolean') publish(Boolean(scalar.value))
  else if (kind.value === 'null') publish(null)
  else publish(String(scalar.value ?? ''))
}
function publishObject() {
  const value: Record<string, unknown> = {}
  objectRows.value.forEach((row) => { if (row.key.trim()) value[row.key.trim()] = row.value })
  publish(value)
}
function publishArray() { publish(arrayRows.value.map((row) => row.value)) }
function addObjectRow() { objectRows.value.push({ id: uid(), key: '', value: '' }) }
function addArrayRow() { arrayRows.value.push({ id: uid(), value: '' }); publishArray() }
function removeObjectRow(id: string) { objectRows.value = objectRows.value.filter((row) => row.id !== id); publishObject() }
function removeArrayRow(id: string) { arrayRows.value = arrayRows.value.filter((row) => row.id !== id); publishArray() }

watch(() => props.modelValue, load, { immediate: true, deep: true })
</script>

<template>
  <div class="structured-editor" :class="{ 'is-root': root }">
    <el-select :model-value="kind" size="small" aria-label="值类型" class="kind-select" @change="changeKind">
      <el-option label="对象" value="object" />
      <el-option label="列表" value="array" />
      <el-option v-if="!root" label="文本" value="text" />
      <el-option v-if="!root" label="数值" value="number" />
      <el-option v-if="!root" label="是 / 否" value="boolean" />
      <el-option v-if="!root" label="空值" value="null" />
    </el-select>

    <div v-if="kind === 'object'" class="container-editor">
      <div v-for="row in objectRows" :key="row.id" class="object-row">
        <el-input v-model="row.key" size="small" placeholder="字段名称" aria-label="字段名称" @input="publishObject" />
        <StructuredValueEditor v-model="row.value" @update:model-value="publishObject" />
        <el-button text type="danger" circle aria-label="删除字段" @click="removeObjectRow(row.id)"><el-icon><Delete /></el-icon></el-button>
      </div>
      <span v-if="!objectRows.length" class="empty-hint">暂无字段</span>
      <el-button size="small" plain @click="addObjectRow"><el-icon><Plus /></el-icon>添加字段</el-button>
    </div>

    <div v-else-if="kind === 'array'" class="container-editor">
      <div v-for="(row, index) in arrayRows" :key="row.id" class="array-row">
        <span class="array-index">{{ index + 1 }}</span>
        <StructuredValueEditor v-model="row.value" @update:model-value="publishArray" />
        <el-button text type="danger" circle aria-label="删除列表项" @click="removeArrayRow(row.id)"><el-icon><Delete /></el-icon></el-button>
      </div>
      <span v-if="!arrayRows.length" class="empty-hint">暂无列表项</span>
      <el-button size="small" plain @click="addArrayRow"><el-icon><Plus /></el-icon>添加列表项</el-button>
    </div>

    <el-switch v-else-if="kind === 'boolean'" v-model="scalar" inline-prompt active-text="是" inactive-text="否" aria-label="布尔值" @change="publishScalar" />
    <span v-else-if="kind === 'null'" class="empty-hint">空值</span>
    <el-input-number v-else-if="kind === 'number'" v-model="scalar" size="small" controls-position="right" aria-label="数值" @change="publishScalar" />
    <el-input v-else v-model="scalar" size="small" placeholder="文本值" aria-label="文本值" @input="publishScalar" />
  </div>
</template>

<style scoped>
.structured-editor { display: grid; grid-template-columns: 88px minmax(120px, 1fr); gap: 7px; width: 100%; align-items: start; }
.kind-select { width: 88px; }
.container-editor { display: grid; gap: 7px; min-width: 0; }
.object-row { display: grid; grid-template-columns: minmax(90px, .65fr) minmax(150px, 1.35fr) 30px; gap: 6px; align-items: start; padding: 7px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.array-row { display: grid; grid-template-columns: 24px minmax(150px, 1fr) 30px; gap: 6px; align-items: start; padding: 7px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.array-index { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; color: var(--primary-600); background: var(--primary-soft); font-size: 9px; font-weight: 800; }
.empty-hint { padding: 5px 0; color: var(--text-3); font-size: 10px; }
.is-root { padding: 8px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); }
@media (max-width: 620px) {
  .structured-editor, .object-row { grid-template-columns: 1fr; }
  .object-row > .el-button { justify-self: end; }
}
</style>
