<template>
  <section>
    <div class="distill-section-head"><h3>{{ title }}</h3><el-button @click="addNode">添加节点</el-button></div>
    <DistillationGraph variant="process" :title="title" :nodes="graph.nodes.map(node => ({ ...node, detail: node.owner, shape: /判断|决策|条件|分支|审批|校验/.test(node.name || '') ? 'decision' : 'process' }))" :edges="graph.edges" />
    <el-empty v-if="!graph.nodes.length" description="按业务结果倒推必要节点，也可以先生成分析建议后核对。" />
    <article v-for="(node, index) in graph.nodes" :key="node.key" class="distill-item">
      <div class="distill-section-head"><h4>节点 {{ index + 1 }}</h4><el-button text type="danger" :aria-label="`移除节点 ${node.name || index + 1}`" @click="remove(node.key)">移除</el-button></div>
      <div class="distill-fields">
        <el-form-item label="业务步骤"><el-input v-model="node.name" maxlength="200" /></el-form-item>
        <el-form-item label="责任角色 / 系统"><el-input v-model="node.owner" maxlength="200" /></el-form-item>
        <el-form-item label="产出与完成证据"><el-input v-model="node.outcome" type="textarea" :rows="2" maxlength="4000" /></el-form-item>
        <el-form-item label="依据"><el-select v-model="node.evidence_refs" multiple><el-option v-for="item in evidence" :key="item.key" :label="item.title || '未命名证据'" :value="item.key" /></el-select></el-form-item>
      </div>
    </article>
    <div class="distill-section-head"><h4>流转与分支</h4><el-button :disabled="!graph.nodes.length" @click="graph.edges.push({ source: '', target: '', label: '' })">添加连线</el-button></div>
    <div v-for="(edge, index) in graph.edges" :key="index" class="distill-link-row">
      <el-form-item :label="`连线 ${index + 1} 起点`"><el-select v-model="edge.source"><el-option v-for="node in graph.nodes" :key="node.key" :label="node.name || '未命名节点'" :value="node.key" /></el-select></el-form-item>
      <el-form-item label="终点"><el-select v-model="edge.target"><el-option v-for="node in graph.nodes" :key="node.key" :label="node.name || '未命名节点'" :value="node.key" /></el-select></el-form-item>
      <el-form-item label="流转条件"><el-input v-model="edge.label" maxlength="200" placeholder="通过、退回、补充资料等" /></el-form-item>
      <el-button text type="danger" :aria-label="`移除连线 ${index + 1}`" @click="graph.edges.splice(index, 1)">移除</el-button>
    </div>
  </section>
</template>
<script setup lang="ts">
import type { DistillationEvidence, ProcessGraph } from '@/types/businessDistillation'
import { removeProcessNode } from '@/utils/businessDistillation'
import { createClientRequestId } from '@/utils/clientRequestId'
import DistillationGraph from './DistillationGraph.vue'
const graph = defineModel<ProcessGraph>({ required: true })
defineProps<{ title: string; evidence: DistillationEvidence[] }>()
const emit = defineEmits<{ remove: [key: string] }>()
function addNode() { graph.value.nodes.push({ key: `node_${createClientRequestId().replace(/-/g, '').slice(0, 16)}`, name: '', owner: '', outcome: '', evidence_refs: [] }) }
function remove(key: string) { graph.value = removeProcessNode(graph.value, key); emit('remove', key) }
</script>
