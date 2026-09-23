<template>
  <section v-if="nodes.length" class="distill-graph" :class="`is-${variant}`" role="region" :aria-label="`${title}：${nodes.length} 个节点，${edges.length} 条关系；点击节点或连线查看文字说明`">
    <header class="distill-graph-head">
      <div>
        <span class="distill-graph-kicker">{{ variantLabels[variant] }}</span>
        <strong>{{ title }}</strong>
      </div>
      <small>{{ nodes.length }} 节点 · {{ edges.length }} 连接</small>
    </header>

    <div class="distill-graph-canvas" @click="clearSelection">
      <svg
        :width="layoutWidth"
        :height="height"
        :viewBox="`0 0 ${layoutWidth} ${height}`"
        preserveAspectRatio="xMidYMin meet"
        xmlns="http://www.w3.org/2000/svg"
        tabindex="0"
        @keydown.escape="clearSelection"
      >
        <defs>
          <marker :id="markerId" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
          </marker>
          <marker :id="selectedMarkerId" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--primary)" />
          </marker>
        </defs>

        <g v-for="edge in edgeViews" :key="`edge-${edge.index}`" class="distill-edge" :class="{ 'is-selected': isEdgeSelected(edge.index) }" @click.stop="selectEdge(edge.index)">
          <path
            :d="edge.path"
            fill="none"
            :stroke="isEdgeSelected(edge.index) ? 'var(--primary)' : 'var(--distill-edge)'"
            :stroke-width="isEdgeSelected(edge.index) ? 2.8 : 1.7"
            :stroke-dasharray="edge.uncertain ? '5 4' : undefined"
            :marker-end="`url(#${isEdgeSelected(edge.index) ? selectedMarkerId : markerId})`"
            :opacity="isEdgeSelected(edge.index) ? 1 : 0.78"
          />
          <path :d="edge.path" fill="none" stroke="transparent" stroke-width="18" class="distill-edge-hit" />
          <g v-if="edge.label" class="distill-edge-label">
            <rect :x="edge.labelX - edge.labelWidth / 2" :y="edge.labelY - 11" :width="edge.labelWidth" height="20" rx="10" />
            <text :x="edge.labelX" :y="edge.labelY + 4" text-anchor="middle">{{ truncate(edge.label, 24) }}</text>
          </g>
        </g>

        <g
          v-for="node in positions"
          :key="node.key"
          class="distill-node"
          :class="{ 'is-selected': isNodeSelected(node.key) }"
          :transform="`translate(${node.x},${node.y})`"
          role="button"
          tabindex="0"
          :aria-label="`查看${node.name || '未命名节点'}详情`"
          @click.stop="selectNode(node.key)"
          @keydown.enter.prevent="selectNode(node.key)"
          @keydown.space.prevent="selectNode(node.key)"
        >
          <template v-if="variant === 'process' && node.shape === 'decision'">
            <polygon :points="diamondPoints(node)" class="distill-node-surface" />
            <text class="distill-node-title decision-title" x="0" y="-4" text-anchor="middle">{{ truncate(node.name || '未命名节点', 14) }}</text>
            <text class="distill-node-subtitle" x="0" y="17" text-anchor="middle">{{ truncate(node.owner || '判断节点', 15) }}</text>
          </template>
          <template v-else>
            <rect :x="-node.w / 2" :y="-node.h / 2" :width="node.w" :height="node.h" rx="12" class="distill-node-surface" />
            <rect :x="-node.w / 2" :y="-node.h / 2" :width="node.w" height="6" rx="3" class="distill-node-accent" />
            <circle :cx="-node.w / 2 + 20" cy="-node.h / 2 + 24" r="6" class="distill-node-dot" />
            <text :x="-node.w / 2 + 36" :y="-node.h / 2 + 29" class="distill-node-title" text-anchor="start">{{ truncate(node.name || '未命名节点', variant === 'entity' ? 18 : 20) }}</text>
            <text :x="-node.w / 2 + 16" :y="-node.h / 2 + 52" class="distill-node-subtitle" text-anchor="start">{{ truncate(nodeSummary(node), 28) }}</text>
            <text v-if="variant === 'entity' && node.attributes?.length" :x="-node.w / 2 + 16" :y="-node.h / 2 + 73" class="distill-node-meta" text-anchor="start">{{ truncate(node.attributes.slice(0, 3).join(' · '), 30) }}</text>
            <text v-else-if="variant === 'process' && node.outcome" :x="-node.w / 2 + 16" :y="-node.h / 2 + 73" class="distill-node-meta" text-anchor="start">{{ truncate(node.outcome, 30) }}</text>
          </template>
        </g>
      </svg>

      <aside v-if="selectedItem" class="distill-graph-inspector" role="dialog" aria-label="图谱详情" @click.stop>
        <button type="button" class="distill-inspector-close" aria-label="关闭图谱详情" title="关闭" @click="clearSelection">×</button>
        <template v-if="selectedItem.kind === 'node' && selectedNode">
          <span class="distill-inspector-kicker">{{ variantLabels[variant] }}节点</span>
          <h3>{{ selectedNode.name || '未命名节点' }}</h3>
          <p v-if="selectedNode.description || selectedNode.detail" class="distill-inspector-description">{{ selectedNode.description || selectedNode.detail }}</p>
          <dl class="distill-inspector-facts">
            <div v-if="selectedNode.owner"><dt>责任角色 / 系统</dt><dd>{{ selectedNode.owner }}</dd></div>
            <div v-if="selectedNode.trigger"><dt>触发</dt><dd>{{ selectedNode.trigger }}</dd></div>
            <div v-if="selectedNode.inputs"><dt>输入</dt><dd>{{ selectedNode.inputs }}</dd></div>
            <div v-if="selectedNode.outcome"><dt>产出与结果</dt><dd>{{ selectedNode.outcome }}</dd></div>
            <div v-if="selectedNode.rule"><dt>规则</dt><dd>{{ selectedNode.rule }}</dd></div>
            <div v-if="selectedNode.exceptions"><dt>例外</dt><dd>{{ selectedNode.exceptions }}</dd></div>
            <div v-if="selectedNode.identity"><dt>身份规则</dt><dd>{{ selectedNode.identity }}</dd></div>
            <div v-if="selectedNode.attributes?.length"><dt>业务属性</dt><dd class="distill-inspector-tags"><span v-for="attribute in selectedNode.attributes" :key="attribute">{{ attribute }}</span></dd></div>
            <div v-if="selectedNode.evidenceSummary"><dt>依据</dt><dd>{{ selectedNode.evidenceSummary }}</dd></div>
            <div v-if="selectedNode.review"><dt>改进依据</dt><dd>{{ selectedNode.review }}</dd></div>
          </dl>
        </template>
        <template v-else-if="selectedEdge">
          <span class="distill-inspector-kicker">{{ variantLabels[variant] }}连线</span>
          <h3>{{ selectedEdge.label || '未命名关系' }}</h3>
          <p class="distill-inspector-route">{{ selectedEdgeSourceName }} <span>→</span> {{ selectedEdgeTargetName }}</p>
          <dl class="distill-inspector-facts">
            <div v-if="selectedEdge.detail || selectedEdge.transformation"><dt>{{ variant === 'lineage' ? '转换规则' : '关系说明' }}</dt><dd>{{ selectedEdge.detail || selectedEdge.transformation }}</dd></div>
            <div v-if="selectedEdge.cardinality"><dt>数量关系</dt><dd>{{ cardinalityLabel(selectedEdge.cardinality) }}</dd></div>
            <div v-if="selectedEdge.rationale"><dt>判断依据</dt><dd>{{ selectedEdge.rationale }}</dd></div>
            <div v-if="selectedEdge.evidenceSummary"><dt>依据</dt><dd>{{ selectedEdge.evidenceSummary }}</dd></div>
          </dl>
        </template>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, useId } from 'vue'

type GraphVariant = 'entity' | 'process' | 'lineage'
type GraphNode = {
  key: string
  name?: string
  detail?: string
  description?: string
  owner?: string
  outcome?: string
  trigger?: string
  inputs?: string
  rule?: string
  exceptions?: string
  identity?: string
  attributes?: string[]
  review?: string
  evidenceSummary?: string
  shape?: 'process' | 'decision'
}
type GraphEdge = {
  source: string
  target: string
  label?: string
  detail?: string
  transformation?: string
  cardinality?: string
  rationale?: string
  uncertain?: boolean
  evidenceSummary?: string
}
type PositionedNode = GraphNode & { x: number; y: number; w: number; h: number }

const props = withDefaults(defineProps<{ title: string; nodes: GraphNode[]; edges: GraphEdge[]; variant?: GraphVariant }>(), { variant: 'process' })
const variantLabels: Record<GraphVariant, string> = { entity: 'ER 关系', process: '业务流程', lineage: '数据血缘' }
const layoutWidth = 1060
const markerId = `distill-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
const selectedMarkerId = `${markerId}-selected`
const selectedItem = ref<{ kind: 'node'; key: string } | { kind: 'edge'; index: number } | null>(null)

const nodeWidth = computed(() => props.variant === 'process' ? 240 : 248)
const nodeHeight = computed(() => props.variant === 'process' ? 94 : 102)

function calculateRanks() {
  const ranks = new Map(props.nodes.map(node => [node.key, 0]))
  const incoming = new Map<string, string[]>()
  for (const edge of props.edges) incoming.set(edge.target, [...(incoming.get(edge.target) || []), edge.source])
  function rankOf(key: string, visiting = new Set<string>()): number {
    if (visiting.has(key)) return 0
    const parents = incoming.get(key) || []
    if (!parents.length) return 0
    const next = new Set(visiting).add(key)
    return Math.max(...parents.map(parent => rankOf(parent, next) + 1))
  }
  for (const node of props.nodes) ranks.set(node.key, rankOf(node.key))
  return ranks
}

const positions = computed<PositionedNode[]>(() => {
  const ranks = calculateRanks()
  const groups = new Map<number, GraphNode[]>()
  for (const node of props.nodes) {
    const rank = ranks.get(node.key) || 0
    groups.set(rank, [...(groups.get(rank) || []), node])
  }
  const maxRank = Math.max(0, ...groups.keys())
  const gapX = 42
  const gapY = props.variant === 'process' ? 66 : 72
  const result: PositionedNode[] = []
  for (let rank = 0; rank <= maxRank; rank += 1) {
    const group = groups.get(rank) || []
    const rowWidth = group.length * nodeWidth.value + Math.max(0, group.length - 1) * gapX
    const startX = (layoutWidth - rowWidth) / 2 + nodeWidth.value / 2
    group.forEach((node, index) => result.push({
      ...node,
      x: startX + index * (nodeWidth.value + gapX),
      y: 62 + rank * (nodeHeight.value + gapY),
      w: nodeWidth.value,
      h: nodeHeight.value,
    }))
  }
  return result
})

const height = computed(() => Math.max(430, Math.max(0, ...positions.value.map(node => node.y + node.h / 2 + 64))))
const nodeByKey = computed(() => new Map(positions.value.map(node => [node.key, node])))
const edgeViews = computed(() => props.edges.flatMap((edge, index) => {
  const source = nodeByKey.value.get(edge.source)
  const target = nodeByKey.value.get(edge.target)
  if (!source || !target) return []
  const points = connectionPoints(source, target)
  const horizontal = Math.abs(target.x - source.x) > Math.abs(target.y - source.y)
  const bend = Math.max(34, Math.min(92, (horizontal ? Math.abs(target.x - source.x) : Math.abs(target.y - source.y)) * 0.42))
  const path = horizontal
    ? `M ${points.x1} ${points.y1} C ${points.x1 + Math.sign(target.x - source.x) * bend} ${points.y1}, ${points.x2 - Math.sign(target.x - source.x) * bend} ${points.y2}, ${points.x2} ${points.y2}`
    : `M ${points.x1} ${points.y1} C ${points.x1} ${points.y1 + Math.sign(target.y - source.y) * bend}, ${points.x2} ${points.y2 - Math.sign(target.y - source.y) * bend}, ${points.x2} ${points.y2}`
  return [{
    index,
    path,
    label: edge.label || edge.cardinality || '',
    labelX: (points.x1 + points.x2) / 2 + (horizontal ? 0 : 12),
    labelY: (points.y1 + points.y2) / 2 + (horizontal ? -12 : 0),
    labelWidth: Math.max(72, Math.min(250, (edge.label || edge.cardinality || '').length * 11 + 24)),
    uncertain: edge.uncertain,
  }]
}))

const selectedNode = computed(() => selectedItem.value?.kind === 'node' ? nodeByKey.value.get(selectedItem.value.key) : undefined)
const selectedEdge = computed(() => selectedItem.value?.kind === 'edge' ? props.edges[selectedItem.value.index] : undefined)
const selectedEdgeSourceName = computed(() => selectedEdge.value ? props.nodes.find(node => node.key === selectedEdge.value?.source)?.name || '待明确对象' : '')
const selectedEdgeTargetName = computed(() => selectedEdge.value ? props.nodes.find(node => node.key === selectedEdge.value?.target)?.name || '待明确对象' : '')

function connectionPoints(source: PositionedNode, target: PositionedNode) {
  const dx = target.x - source.x
  const dy = target.y - source.y
  if (Math.abs(dx) > Math.abs(dy)) {
    const direction = Math.sign(dx) || 1
    return { x1: source.x + direction * source.w / 2, y1: source.y, x2: target.x - direction * target.w / 2, y2: target.y }
  }
  const direction = Math.sign(dy) || 1
  return { x1: source.x, y1: source.y + direction * source.h / 2, x2: target.x, y2: target.y - direction * target.h / 2 }
}

function diamondPoints(node: PositionedNode) {
  const halfWidth = node.w / 2
  const halfHeight = node.h / 2
  return `0,${-halfHeight} ${halfWidth},0 0,${halfHeight} ${-halfWidth},0`
}
function nodeSummary(node: GraphNode) {
  if (props.variant === 'entity') return `${node.attributes?.length || 0} 个业务属性`
  if (props.variant === 'lineage') return node.detail || node.description || '业务对象'
  return node.owner || node.detail || '流程节点'
}
function truncate(value: string, max: number) { return value.length > max ? `${value.slice(0, max - 1)}…` : value }
function cardinalityLabel(value: string) {
  return ({ unconfirmed: '基数待核对', one_to_one: '一对一', one_to_many: '一对多', many_to_many: '多对多' } as Record<string, string>)[value] || value
}
function isNodeSelected(key: string) { return selectedItem.value?.kind === 'node' && selectedItem.value.key === key }
function isEdgeSelected(index: number) { return selectedItem.value?.kind === 'edge' && selectedItem.value.index === index }
function selectNode(key: string) { selectedItem.value = { kind: 'node', key } }
function selectEdge(index: number) { selectedItem.value = { kind: 'edge', index } }
function clearSelection() { selectedItem.value = null }
</script>

<style scoped>
.distill-graph {
  --distill-edge: #8c9ca3;
  --distill-node-fill: #ffffff;
  --distill-node-border: #b9c9cf;
  --distill-node-accent: #6b8d99;
  position: relative;
  display: flex;
  min-height: 360px;
  flex-direction: column;
  overflow: hidden;
  margin: 20px 0;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-2);
  box-shadow: var(--shadow-xs);
}
.distill-graph.is-entity { --distill-node-accent: #6e8d86; }
.distill-graph.is-lineage { --distill-node-accent: #8a7b67; }
.distill-graph-head { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 14px; border-bottom: 1px solid var(--border); background: var(--surface); }
.distill-graph-head > div { display: flex; min-width: 0; align-items: baseline; gap: 9px; }
.distill-graph-head strong { overflow: hidden; color: var(--text); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.distill-graph-head small { color: var(--text-3); font-size: 11px; white-space: nowrap; }
.distill-graph-kicker, .distill-inspector-kicker { color: var(--primary-600); font-size: 10px; font-weight: 750; letter-spacing: .08em; }
.distill-graph-canvas { position: relative; flex: 1; min-height: 0; overflow: auto; }
.distill-graph-canvas > svg { display: block; width: 100%; min-width: 0; margin: 0 auto; color: var(--distill-edge); outline: none; }
.distill-edge, .distill-node { color: var(--distill-edge); }
.distill-edge { cursor: pointer; }
.distill-edge-hit { cursor: pointer; }
.distill-edge-label { pointer-events: none; }
.distill-edge-label rect { fill: var(--surface); stroke: var(--border); }
.distill-edge.is-selected .distill-edge-label rect { fill: var(--primary-soft); stroke: var(--primary); }
.distill-edge-label text { fill: var(--text-2); font-size: 11px; font-weight: 650; }
.distill-edge.is-selected .distill-edge-label text { fill: var(--primary-600); }
.distill-node { cursor: pointer; outline: none; }
.distill-node-surface { fill: var(--distill-node-fill); stroke: var(--distill-node-border); stroke-width: 1.5; }
.distill-node-accent { fill: var(--distill-node-accent); }
.distill-node-dot { fill: var(--distill-node-accent); }
.distill-node-title { fill: var(--text); font-size: 14px; font-weight: 700; }
.distill-node-subtitle { fill: var(--text-2); font-size: 11px; }
.distill-node-meta { fill: var(--text-3); font-size: 10px; }
.distill-node.is-selected .distill-node-surface { stroke: var(--primary); stroke-width: 2.6; filter: drop-shadow(0 4px 8px rgba(54, 95, 108, .18)); }
.distill-node:focus-visible .distill-node-surface { stroke: var(--primary); stroke-width: 2.6; }
.distill-graph-inspector { position: absolute; top: 12px; right: 12px; width: min(320px, calc(100% - 24px)); max-height: calc(100% - 24px); overflow-y: auto; padding: 16px 17px; border: 1px solid var(--border-strong); border-radius: 12px; background: color-mix(in srgb, var(--surface) 96%, transparent); box-shadow: var(--shadow-md); backdrop-filter: blur(10px); }
.distill-inspector-close { position: absolute; top: 8px; right: 9px; width: 30px; height: 30px; border: 0; border-radius: 8px; background: transparent; color: var(--text-3); font-size: 22px; line-height: 1; cursor: pointer; }
.distill-inspector-close:hover { background: var(--surface-2); color: var(--text); }
.distill-graph-inspector h3 { margin: 7px 34px 7px 0; color: var(--text); font-size: 17px; }
.distill-inspector-description, .distill-inspector-route { margin: 0 0 12px; color: var(--text-2); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.distill-inspector-route span { padding: 0 5px; color: var(--primary); }
.distill-inspector-facts { display: grid; gap: 9px; margin: 0; }
.distill-inspector-facts > div { padding-top: 9px; border-top: 1px solid var(--border); }
.distill-inspector-facts dt { color: var(--text-3); font-size: 10px; }
.distill-inspector-facts dd { margin: 3px 0 0; color: var(--text); font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.distill-inspector-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.distill-inspector-tags span { padding: 3px 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--surface-2); color: var(--text-2); font-size: 11px; }
@media (max-width: 640px) {
  .distill-graph-head { align-items: flex-start; flex-direction: column; gap: 3px; }
  .distill-graph-head small { white-space: normal; }
  .distill-graph-inspector { top: 8px; right: 8px; max-height: calc(100% - 16px); width: calc(100% - 16px); }
}
</style>
