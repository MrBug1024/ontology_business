<template>
  <div>
    <p v-if="!document.historical_cases?.length" class="discovery-empty-note">暂无历史案例</p>
    <article v-for="item in document.historical_cases" :key="item.key" class="discovery-finding-card">
      <h3>{{ item.title }}</h3><p>覆盖范围：{{ item.scope || '待核对' }}</p>
      <p>历史结果：{{ item.result_summary || '待补充' }}</p><p>对应依据：{{ item.association_basis || '尚未建立输入与结果的对应关系' }}</p>
      <dl class="discovery-value-list"><div v-for="group in groups" :key="group.key"><dt>{{ group.label }}</dt><dd>{{ evidenceNames(item[group.key]) }}</dd></div></dl>
      <ol class="discovery-canvas-list"><li v-for="(step, index) in item.steps" :key="index"><strong>{{ nodeName(step.node_key) }}</strong><p>{{ step.input_summary || '待补输入' }} → {{ step.action || '待核对动作' }} → {{ step.output_summary || '待补产出' }}</p><small>{{ evidenceNames(step.evidence_refs) }}</small></li></ol>
      <p>差异：{{ item.discrepancies || '未记录；需要核对是否有反例' }}</p><p>限制：{{ item.limitations || '请补充时间、样本和缺失记录等限制' }}</p>
    </article>
  </div>
</template>
<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
const props = defineProps<{ document: DistillationDocument }>()
const groups = [{ key: 'result_refs', label: '结果依据' }, { key: 'input_refs', label: '对应输入' }, { key: 'process_refs', label: '过程记录' }, { key: 'knowledge_refs', label: '规则依据' }] as const
function evidenceNames(keys: string[]) { return keys.map(key => props.document.evidence.find(item => item.key === key)?.title || '待核对依据').join('、') || '尚无引用' }
function nodeName(key: string) { return props.document.as_is.nodes.find(item => item.key === key)?.name || '待核对环节' }
</script>
