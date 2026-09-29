<script setup lang="ts">
import { computed } from 'vue'
import SchemaFieldBuilder from '@/components/SchemaFieldBuilder.vue'
import { workflowResultRows, workflowResultSchema } from '@/utils/workflowResult'

const props = defineProps<{ output?: unknown; triggerConfig?: unknown; nodeId: string }>()
const emit = defineEmits<{ (event: 'update:output', value: string): void }>()
const rows = computed(() => workflowResultRows(props.output))
const schema = computed(() => workflowResultSchema(props.triggerConfig, props.nodeId))
</script>

<template>
  <section class="result-fields" aria-label="工作流业务输出">
    <strong>业务结果</strong>
    <el-input v-if="typeof output === 'string'" :model-value="output" type="textarea" :rows="3"
      aria-label="业务结果引用" @update:model-value="emit('update:output', $event)" />
    <dl v-else-if="rows.length">
      <template v-for="row in rows" :key="row.path">
        <dt>{{ row.path }}</dt><dd>{{ row.value }}</dd>
      </template>
    </dl>
    <p v-else>尚未配置业务结果，可通过智能业务顾问补全。</p>
    <p v-if="typeof output !== 'string' && rows.length">展示前 64 项；结构化结果可通过智能业务顾问调整。</p>
    <template v-if="Object.keys(schema).length">
      <strong>结果字段要求</strong>
      <SchemaFieldBuilder :model-value="schema" readonly />
      <p>执行结果必须通过这些字段要求的服务端校验。</p>
    </template>
  </section>
</template>

<style scoped>
.result-fields { display: grid; gap: 8px; margin-bottom: 14px; min-width: 0; }
.result-fields strong { font-size: 12px; }
.result-fields p, .result-fields dl { margin: 0; font-size: 11px; color: var(--text-3); }
.result-fields dt { font-weight: 600; }
.result-fields dd { margin: 3px 0 8px; color: var(--text-2); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
