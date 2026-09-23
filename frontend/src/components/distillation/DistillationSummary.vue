<template>
  <section class="distill-summary">
    <dl class="distill-summary-grid">
      <div><dt>服务对象</dt><dd>{{ document.beneficiary || '待明确' }}</dd></div>
      <div><dt>核心痛点</dt><dd>{{ document.pain || '待明确' }}</dd></div>
      <div><dt>最终结果</dt><dd>{{ document.desired_outcome || '待明确' }}</dd></div>
      <div><dt>成功标准</dt><dd>{{ document.success_metric || '待明确' }}</dd></div>
      <div><dt>建设范围</dt><dd>{{ document.scope || '待明确' }}</dd></div>
      <div><dt>不做什么</dt><dd>{{ document.non_goals || '待明确' }}</dd></div>
    </dl>
    <p>证据 {{ document.evidence.length }} 项 · 断言 {{ document.assertions.length }} 项 · 业务对象 {{ document.entities.length }} 个</p>
    <details v-if="document.assertions.length"><summary>查看事实与推断</summary><ul><li v-for="item in document.assertions" :key="item.key"><strong>{{ ASSERTION_LABELS[item.status] }}：</strong>{{ item.statement }}</li></ul></details>
    <DistillationGraph variant="process" title="建议目标流程" :nodes="document.to_be.nodes.map(item => ({ ...item, detail: item.owner }))" :edges="document.to_be.edges" />
    <details><summary>查看完整建议内容</summary>
      <h4>证据与限制</h4><ul><li v-for="item in document.evidence" :key="item.key">{{ item.title }}：{{ item.summary }}<p>覆盖：{{ item.coverage || '未说明' }}；限制：{{ item.limitations || '未说明' }}</p></li></ul>
      <h4>现状流程</h4><ul><li v-for="item in document.as_is.nodes" :key="item.key">{{ item.name }} · {{ item.owner }} · {{ item.outcome }}</li></ul>
      <h4>目标流程</h4><ul><li v-for="item in document.to_be.nodes" :key="item.key">{{ item.name }} · {{ item.owner }} · {{ item.outcome }}</li></ul>
      <h4>流程改进</h4><ul><li v-for="item in document.improvements" :key="item.key">{{ improvementLabels[item.decision] }}：{{ item.rationale }}；{{ item.expected_benefit }}</li></ul>
      <h4>业务对象</h4><ul><li v-for="item in document.entities" :key="item.key">{{ item.name }}：{{ item.description }}；属性：{{ item.attributes.join('、') }}</li></ul>
      <h4>关系</h4><ul><li v-for="(item, index) in document.relations" :key="index">{{ entityName(item.source) }} → {{ entityName(item.target) }}：{{ item.label }}</li></ul>
      <h4>数据血缘</h4><ul><li v-for="(item, index) in document.lineage" :key="index">{{ entityName(item.source) }} → {{ entityName(item.target) }}：{{ item.transformation }}</li></ul>
    </details>
    <p><strong>建设判断：</strong>{{ DECISION_LABELS[document.decision] }}。{{ document.decision_reason }}</p>
    <ul v-if="document.open_questions.length"><li v-for="(question, index) in document.open_questions" :key="index">{{ question }}</li></ul>
  </section>
</template>
<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
import { ASSERTION_LABELS, DECISION_LABELS } from '@/utils/businessDistillation'
import DistillationGraph from './DistillationGraph.vue'
const props = defineProps<{ document: DistillationDocument }>()
const improvementLabels = { retain: '保留', remove: '删除', merge: '合并', replace: '替代' }
function entityName(key: string) { return props.document.entities.find(item => item.key === key)?.name || '未命名对象' }
</script>
