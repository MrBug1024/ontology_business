<script setup lang="ts">
import { computed } from 'vue'

defineOptions({ name: 'StructuredValueViewer' })

const props = withDefaults(defineProps<{
  value?: unknown
  label?: string
  depth?: number
  emptyText?: string
}>(), {
  value: undefined,
  label: '',
  depth: 0,
  emptyText: '暂无数据',
})

function parsed(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const source = value.trim()
  if (!source || (!source.startsWith('{') && !source.startsWith('['))) return value
  try { return JSON.parse(source) } catch { return value }
}

const resolved = computed(() => parsed(props.value))
const isRecord = computed(() => Boolean(resolved.value) && typeof resolved.value === 'object' && !Array.isArray(resolved.value))
const isList = computed(() => Array.isArray(resolved.value))
const entries = computed(() => isRecord.value ? Object.entries(resolved.value as Record<string, unknown>) : [])
const items = computed(() => isList.value ? resolved.value as unknown[] : [])

function primitive(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (value === true) return '是'
  if (value === false) return '否'
  return String(value)
}
</script>

<template>
  <div class="structured-viewer" :class="{ nested: depth > 0 }">
    <span v-if="label" class="viewer-label">{{ label }}</span>
    <dl v-if="isRecord && entries.length" class="record-view">
      <div v-for="([key, item]) in entries" :key="key" class="record-row">
        <dt>{{ key }}</dt>
        <dd><StructuredValueViewer :value="item" :depth="depth + 1" :empty-text="emptyText" /></dd>
      </div>
    </dl>
    <ol v-else-if="isList && items.length" class="list-view">
      <li v-for="(item, index) in items" :key="index">
        <span class="list-index">{{ index + 1 }}</span>
        <StructuredValueViewer :value="item" :depth="depth + 1" :empty-text="emptyText" />
      </li>
    </ol>
    <span v-else-if="isRecord || isList" class="viewer-empty">{{ emptyText }}</span>
    <span v-else class="primitive-view">{{ primitive(resolved) }}</span>
  </div>
</template>

<style scoped>
.structured-viewer { min-width: 0; color: var(--text-2); font-size: 11px; line-height: 1.55; }
.viewer-label { display: block; margin-bottom: 5px; color: var(--text-3); font-size: 10px; font-weight: 700; }
.record-view { display: grid; gap: 5px; margin: 0; }
.record-row { display: grid; grid-template-columns: minmax(90px, .7fr) minmax(0, 1.3fr); gap: 8px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); }
.record-row dt { overflow-wrap: anywhere; color: var(--text-3); font-weight: 700; }
.record-row dd { min-width: 0; margin: 0; }
.nested > .record-view .record-row { padding: 4px 0; border: 0; border-bottom: 1px dashed var(--border); border-radius: 0; background: transparent; }
.nested > .record-view .record-row:last-child { border-bottom: 0; }
.list-view { display: grid; gap: 5px; margin: 0; padding: 0; list-style: none; }
.list-view > li { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 6px; align-items: start; }
.list-index { display: grid; width: 20px; height: 20px; place-items: center; border-radius: 50%; color: var(--primary-600); background: var(--primary-soft); font-size: 9px; font-weight: 800; }
.primitive-view { white-space: pre-wrap; overflow-wrap: anywhere; }
.viewer-empty { color: var(--text-3); }
@media (max-width: 520px) {
  .record-row { grid-template-columns: 1fr; gap: 2px; }
}
</style>
