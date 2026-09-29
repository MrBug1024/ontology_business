<script setup lang="ts">
import { computed } from 'vue'
import type { DatasetSchema, Entity, ScenarioModelDraftResource } from '@/types'
import { semanticMappingCandidateRows } from '@/utils/semanticMappingCandidates'

const props = defineProps<{ candidates: ScenarioModelDraftResource[]; entities: Entity[]; schemas: DatasetSchema[] }>()
const emit = defineEmits<{ review: [] }>()
const rows = computed(() => semanticMappingCandidateRows(props.candidates, props.entities, props.schemas))
</script>

<template>
  <section v-if="rows.length" class="mapping-candidates" aria-label="顾问语义映射候选">
    <header><h3>待审阅语义映射</h3><el-button @click="emit('review')">前往候选评审</el-button></header>
    <p>核对对象与资料字段对应后，在候选评审中重新校验并晋级。补齐操作保留已有字段对应。</p>
    <article v-for="row in rows" :key="row.id">
      <strong>{{ row.entity }} ← {{ row.source }}</strong>
      <span>{{ row.operation }} · {{ row.fields.length }} 项字段 · {{ row.eligible ? '服务端预检通过' : '需修正或重新校验' }}</span>
      <details><summary>检查字段对应</summary>
        <ul><li v-for="(field, index) in row.fields" :key="index">{{ field.property }} ← {{ field.source }}{{ field.required ? '（必填）' : '' }}</li></ul>
      </details>
    </article>
    <p v-if="candidates.length > 25">此处显示前 25 项，全部候选请到候选评审查看。</p>
  </section>
</template>

<style scoped>
.mapping-candidates { display: grid; gap: 10px; }
.mapping-candidates header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.mapping-candidates h3, .mapping-candidates p { margin: 0; }
.mapping-candidates p, .mapping-candidates span { color: var(--text-2); font-size: 12px; }
.mapping-candidates article { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; }
.mapping-candidates summary { cursor: pointer; }
.mapping-candidates li { overflow-wrap: anywhere; }
</style>
