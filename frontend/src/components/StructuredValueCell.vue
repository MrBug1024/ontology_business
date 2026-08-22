<script setup lang="ts">
import { computed } from 'vue'
import StructuredValueViewer from '@/components/StructuredValueViewer.vue'

const props = defineProps<{ value?: unknown }>()
const structured = computed(() => Boolean(props.value) && typeof props.value === 'object')
const summary = computed(() => {
  if (Array.isArray(props.value)) return `列表（${props.value.length} 项）`
  if (structured.value) return `对象（${Object.keys(props.value as Record<string, unknown>).length} 个字段）`
  if (props.value === null || props.value === undefined || props.value === '') return '—'
  if (props.value === true) return '是'
  if (props.value === false) return '否'
  return String(props.value)
})
</script>

<template>
  <el-popover v-if="structured" trigger="click" placement="top-start" :width="360">
    <StructuredValueViewer :value="value" />
    <template #reference><el-button class="structured-cell-button" text type="primary">{{ summary }}</el-button></template>
  </el-popover>
  <span v-else class="primitive-cell">{{ summary }}</span>
</template>

<style scoped>
.structured-cell-button { height: auto; min-height: 28px; padding: 2px 0; white-space: normal; text-align: left; }
.primitive-cell { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
