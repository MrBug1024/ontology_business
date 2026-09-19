<template>
  <section>
    <h2>还原现状，再设计更好的路径</h2>
    <p class="distill-hint">保留真实发生的流程及其缺口。目标流程应说明如何改善结果，合规、风险控制和必要人工环节也要有责任人。</p>
    <el-tabs v-model="tab">
      <el-tab-pane label="历史实际流程" name="as_is"><DistillationProcessEditor v-model="document.as_is" title="现状流程" :evidence="document.evidence" @remove="removeImprovement" /></el-tab-pane>
      <el-tab-pane label="建议目标流程" name="to_be"><DistillationProcessEditor v-model="document.to_be" title="目标流程" :evidence="document.evidence" /></el-tab-pane>
      <el-tab-pane label="流程价值审查" name="improvements">
        <div class="distill-section-head"><h3>每一步为什么值得保留</h3><el-button :disabled="!document.as_is.nodes.length" @click="addImprovement">添加审查</el-button></div>
        <p class="distill-hint">检查重复采集、仅为报表留痕、无后续处置及无人承担结果的步骤。删减前也需核对法规和审计要求。</p>
        <article v-for="(item, index) in document.improvements" :key="item.key" class="distill-item">
          <div class="distill-fields">
            <el-form-item label="现状节点"><el-select v-model="item.existing_node_key"><el-option v-for="node in document.as_is.nodes" :key="node.key" :label="node.name || '未命名节点'" :value="node.key" /></el-select></el-form-item>
            <el-form-item label="处理建议"><el-select v-model="item.decision"><el-option label="保留" value="retain" /><el-option label="删除" value="remove" /><el-option label="合并" value="merge" /><el-option label="替代" value="replace" /></el-select></el-form-item>
            <el-form-item label="业务理由与约束"><el-input v-model="item.rationale" type="textarea" :rows="3" maxlength="4000" /></el-form-item>
            <el-form-item label="预期改善与验证办法"><el-input v-model="item.expected_benefit" type="textarea" :rows="3" maxlength="4000" /></el-form-item>
          </div>
          <el-button text type="danger" :aria-label="`移除审查 ${index + 1}`" @click="document.improvements.splice(index, 1)">移除审查</el-button>
        </article>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { DistillationDocument } from '@/types/businessDistillation'
import DistillationProcessEditor from './DistillationProcessEditor.vue'
const document = defineModel<DistillationDocument>({ required: true })
const tab = ref('as_is')
function addImprovement() { document.value.improvements.push({ key: `review_${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`, existing_node_key: '', decision: 'retain', rationale: '', expected_benefit: '' }) }
function removeImprovement(key: string) { document.value.improvements = document.value.improvements.filter(item => item.existing_node_key !== key) }
</script>
