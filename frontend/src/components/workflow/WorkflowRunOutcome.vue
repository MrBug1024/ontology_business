<script setup lang="ts">
import { computed } from 'vue'
import StructuredValueViewer from '@/components/StructuredValueViewer.vue'
import { workflowBusinessOutputs } from '@/utils/workflowResult'

const props = defineProps<{ status: string; result: unknown }>()
const outputs = computed(() => workflowBusinessOutputs(props.status, props.result))
</script>

<template>
  <section v-if="outputs.length" class="run-outcome" aria-label="工作流业务结果">
    <h4>业务结果</h4>
    <div v-for="(output, index) in outputs" :key="index" class="run-output">
      <h5 v-if="outputs.length > 1">{{ output.name }}</h5>
      <StructuredValueViewer :value="output.value" empty-text="本次返回空结果" />
    </div>
  </section>
</template>

<style scoped>
.run-outcome { margin: 0 0 18px; padding: 14px; border: 1px solid var(--border); border-radius: 12px; }
.run-outcome h4, .run-outcome h5 { margin: 0 0 10px; }
.run-output + .run-output { margin-top: 12px; }
</style>
