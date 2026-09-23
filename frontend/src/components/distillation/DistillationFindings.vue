<template>
  <div class="discovery-findings" :class="{ 'is-graph-finding': tab !== 'value' }">
    <template v-if="tab === 'value'">
      <dl v-if="valueItems.length" class="discovery-value-list discovery-value-grid"><div v-for="item in valueItems" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl>
      <p v-else class="discovery-empty-note">暂无业务价值产物</p>
      <div v-if="document.decision !== 'undecided' || document.decision_reason" class="discovery-decision"><strong>{{ DECISION_LABELS[document.decision] }}</strong><p>{{ document.decision_reason }}</p></div>
    </template>
    <template v-else-if="tab === 'process'">
      <div v-if="document.as_is.nodes.length || document.to_be.nodes.length" class="discovery-segment" role="group" aria-label="选择流程"><button :aria-pressed="!targetFlow" :disabled="!document.as_is.nodes.length" @click="targetFlow = false">现状流程</button><button :aria-pressed="targetFlow" :disabled="!document.to_be.nodes.length" @click="targetFlow = true">目标流程</button></div>
      <DistillationGraph v-if="flow.nodes.length" variant="process" :title="targetFlow ? '目标流程' : '现状流程'" :nodes="processGraphNodes" :edges="flow.edges" />
      <p v-if="!flow.nodes.length" class="discovery-empty-note">暂无流程图谱</p>
    </template>
    <template v-else-if="tab === 'entities'">
      <DistillationGraph v-if="document.entities.length" variant="entity" title="业务对象关系" :nodes="entityGraphNodes" :edges="entityEdges" />
      <p v-if="!document.entities.length" class="discovery-empty-note">暂无 ER 图谱</p>
    </template>
    <template v-else-if="tab === 'lineage'">
      <DistillationGraph v-if="document.lineage.length" variant="lineage" title="数据血缘" :nodes="entityGraphNodes" :edges="lineageEdges" />
      <p v-if="!document.lineage.length" class="discovery-empty-note">暂无数据血缘</p>
    </template>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DistillationDocument } from '@/types/businessDistillation'
import { DECISION_LABELS } from '@/utils/businessDistillation'
import DistillationGraph from './DistillationGraph.vue'
const props = defineProps<{ document: DistillationDocument; tab: 'value' | 'entities' | 'lineage' | 'process' }>()
const selectedTargetFlow = ref(false)
const targetFlow = computed({
  get: () => !!props.document.to_be.nodes.length && (selectedTargetFlow.value || !props.document.as_is.nodes.length),
  set: (value: boolean) => { selectedTargetFlow.value = value },
})
const flow = computed(() => targetFlow.value ? props.document.to_be : props.document.as_is)
const valueItems = computed(() => [
  { label: '真正受益的人', value: props.document.beneficiary }, { label: '核心痛点', value: props.document.pain },
  { label: '最终结果', value: props.document.desired_outcome }, { label: '成功标准', value: props.document.success_metric },
  { label: '建设范围', value: props.document.scope }, { label: '范围之外', value: props.document.non_goals },
].filter(item => item.value.trim()))
const improvementLabels = { retain: '保留', remove: '删除', merge: '合并', replace: '替代' }
const entityGraphNodes = computed(() => props.document.entities.map(entity => ({
  ...entity,
  detail: entity.description,
  evidenceSummary: entity.evidence_refs?.length ? `依据：${evidenceNames(entity.evidence_refs)}` : '',
})))
const entityEdges = computed(() => props.document.relations.map(relation => ({ ...relation,
  uncertain: relation.cardinality === 'unconfirmed',
  label: relation.cardinality === 'unconfirmed' ? `基数待核对 · ${relation.label}` : relation.label,
  detail: relation.label,
  evidenceSummary: relation.evidence_refs?.length ? `依据：${evidenceNames(relation.evidence_refs)}` : '',
})))
const lineageEdges = computed(() => props.document.lineage.map(item => ({
  ...item,
  label: item.transformation,
  detail: item.transformation,
  evidenceSummary: item.evidence_refs.length ? `依据：${evidenceNames(item.evidence_refs)}` : '',
})))
const processGraphNodes = computed(() => flow.value.nodes.map(node => ({
  ...node,
  detail: node.owner,
  shape: /判断|决策|条件|分支|审批|校验/.test(node.name || '') ? 'decision' as const : 'process' as const,
  evidenceSummary: node.evidence_refs.length ? `依据：${evidenceNames(node.evidence_refs)}` : '',
  review: targetFlow.value
    ? props.document.improvements.filter(item => item.existing_node_key === node.key).map(item => `${improvementLabels[item.decision] || item.decision}：${item.rationale}${item.expected_benefit ? `；预期改善：${item.expected_benefit}` : ''}`).join('；')
    : '',
})))
function evidenceNames(keys: string[]) { return keys.map(key => props.document.evidence.find(item => item.key === key)?.title || '待核实依据').join('、') }
</script>
