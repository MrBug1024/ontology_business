<template>
  <div v-if="nodes.length" class="distill-graph" role="img" :aria-label="`${title}：${nodes.length} 个节点，${edges.length} 条关系；完整内容见下方文字说明`">
    <svg :viewBox="`0 0 800 ${height}`" xmlns="http://www.w3.org/2000/svg">
      <defs><marker :id="markerId" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
      <g v-for="(edge, index) in visibleEdges" :key="index">
        <path :d="edge.path" fill="none" stroke="currentColor" stroke-width="1.4" :stroke-dasharray="edge.uncertain ? '5 4' : undefined" :marker-end="`url(#${markerId})`" opacity=".6" />
        <text :x="edge.x" :y="edge.y" text-anchor="middle" font-size="11">{{ edge.label.slice(0, 12) }}</text>
      </g>
      <g v-for="node in positions" :key="node.key" :transform="`translate(${node.x},${node.y})`">
        <rect width="200" height="64" rx="8" fill="var(--surface)" stroke="var(--border-strong)" />
        <text x="100" y="27" text-anchor="middle" font-size="14" fill="var(--text)">{{ (node.name || '未命名').slice(0, 12) }}</text>
        <text x="100" y="48" text-anchor="middle" font-size="11" fill="var(--text-2)">{{ (node.detail || '').slice(0, 18) }}</text>
      </g>
    </svg>
  </div>
</template>
<script setup lang="ts">
import { computed, useId } from 'vue'
const props = defineProps<{ title: string; nodes: { key: string; name: string; detail?: string }[]; edges: { source: string; target: string; label: string; uncertain?: boolean }[] }>()
const markerId = `distill-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
const positions = computed(() => props.nodes.map((node, index) => ({ ...node, x: 30 + index % 3 * 270, y: 30 + Math.floor(index / 3) * 130 })))
const height = computed(() => Math.ceil(props.nodes.length / 3) * 130 + 15)
const visibleEdges = computed(() => props.edges.flatMap(edge => {
  const source = positions.value.find(node => node.key === edge.source)
  const target = positions.value.find(node => node.key === edge.target)
  if (!source || !target) return []
  const startX = source.x + 100, startY = source.y + 64
  const endX = target.x + 100, endY = target.y
  return [{ path: `M ${startX} ${startY} C ${startX} ${startY + 35}, ${endX} ${endY - 35}, ${endX} ${endY}`, x: (startX + endX) / 2, y: (startY + endY) / 2 - 5, label: edge.label, uncertain: edge.uncertain }]
}))
</script>
