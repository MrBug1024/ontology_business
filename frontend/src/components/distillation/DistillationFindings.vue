<template>
  <div class="discovery-findings">
    <template v-if="tab === 'value'">
      <dl v-if="valueItems.length" class="discovery-value-list discovery-value-grid"><div v-for="item in valueItems" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl>
      <p v-else class="discovery-empty-note">暂无业务价值产物</p>
      <div v-if="document.decision !== 'undecided' || document.decision_reason" class="discovery-decision"><strong>{{ DECISION_LABELS[document.decision] }}</strong><p>{{ document.decision_reason }}</p></div>
    </template>
    <template v-else-if="tab === 'process'">
      <div v-if="document.as_is.nodes.length || document.to_be.nodes.length" class="discovery-segment" role="group" aria-label="选择流程"><button :aria-pressed="!targetFlow" :disabled="!document.as_is.nodes.length" @click="targetFlow = false">现状流程</button><button :aria-pressed="targetFlow" :disabled="!document.to_be.nodes.length" @click="targetFlow = true">目标流程</button></div>
      <DistillationGraph v-if="flow.nodes.length" :title="targetFlow ? '目标流程' : '现状流程'" :nodes="flow.nodes.map(node => ({ ...node, detail: node.owner }))" :edges="flow.edges" />
      <ol v-if="flow.nodes.length" class="discovery-canvas-list"><li v-for="node in flow.nodes" :key="node.key"><strong>{{ node.name }}</strong><p v-if="node.owner">责任人：{{ node.owner }}</p><p v-if="node.trigger">触发：{{ node.trigger }}</p><p v-if="node.inputs">输入：{{ node.inputs }}</p><p v-if="node.outcome">结果：{{ node.outcome }}</p><p v-if="node.rule">规则：{{ node.rule }}</p><p v-if="node.exceptions">例外：{{ node.exceptions }}</p><small v-if="node.evidence_refs.length">依据：{{ evidenceNames(node.evidence_refs) }}</small></li></ol>
      <ul v-if="flow.edges.length" class="discovery-canvas-list"><li v-for="(edge, index) in flow.edges" :key="index">{{ nodeName(edge.source) }} → {{ nodeName(edge.target) }}<span v-if="edge.label"> · {{ edge.label }}</span></li></ul>
      <p v-if="!flow.nodes.length" class="discovery-empty-note">暂无流程图谱</p>
      <template v-if="targetFlow && document.improvements.length"><h3>改进依据</h3><article v-for="item in document.improvements" :key="item.key" class="discovery-finding-card"><strong>{{ improvementLabels[item.decision] }} · {{ existingNodeName(item.existing_node_key) }}</strong><p>{{ item.rationale }}</p><small v-if="item.expected_benefit">预期改善：{{ item.expected_benefit }}</small></article></template>
    </template>
    <template v-else-if="tab === 'entities'">
      <DistillationGraph v-if="document.entities.length" title="业务对象关系" :nodes="document.entities" :edges="entityEdges" />
      <dl class="discovery-value-list discovery-value-grid"><div v-for="entity in document.entities" :key="entity.key"><dt>{{ entity.name }}</dt><dd>{{ entity.description }}<p v-if="entity.identity">身份规则：{{ entity.identity }}</p><small v-if="entity.evidence_refs?.length">依据：{{ evidenceNames(entity.evidence_refs) }}</small><small v-if="entity.attributes.length">属性：{{ entity.attributes.join('、') }}</small></dd></div></dl>
      <ul class="discovery-canvas-list"><li v-for="(relation, index) in document.relations" :key="index">{{ entityName(relation.source) }} → {{ entityName(relation.target) }} · {{ relation.label }}<p>{{ cardinalityLabels[relation.cardinality] }}</p><p v-if="relation.rationale">基数依据：{{ relation.rationale }}</p><small v-if="relation.evidence_refs?.length">证据：{{ evidenceNames(relation.evidence_refs) }}</small></li></ul>
      <p v-if="!document.entities.length" class="discovery-empty-note">暂无 ER 图谱</p>
    </template>
    <template v-else-if="tab === 'lineage'">
      <DistillationGraph v-if="document.lineage.length" title="数据血缘" :nodes="document.entities" :edges="document.lineage.map(item => ({ ...item, label: item.transformation }))" />
      <ol class="discovery-canvas-list"><li v-for="(item, index) in document.lineage" :key="index"><strong>{{ entityName(item.source) }} → {{ entityName(item.target) }}</strong><p>{{ item.transformation }}</p><small v-if="item.evidence_refs.length">依据：{{ evidenceNames(item.evidence_refs) }}</small></li></ol>
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
const cardinalityLabels = { unconfirmed: '基数待核对', one_to_one: '一对一', one_to_many: '一对多', many_to_many: '多对多' }
const entityEdges = computed(() => props.document.relations.map(relation => ({ ...relation,
  uncertain: relation.cardinality === 'unconfirmed',
  label: relation.cardinality === 'unconfirmed' ? `基数待核对 · ${relation.label}` : relation.label,
})))
function entityName(key: string) { return props.document.entities.find(item => item.key === key)?.name || '待明确对象' }
function nodeName(key: string) { return flow.value.nodes.find(item => item.key === key)?.name || '待明确环节' }
function existingNodeName(key: string) { return props.document.as_is.nodes.find(item => item.key === key)?.name || '当前流程' }
function evidenceNames(keys: string[]) { return keys.map(key => props.document.evidence.find(item => item.key === key)?.title || '待核实依据').join('、') }
</script>
