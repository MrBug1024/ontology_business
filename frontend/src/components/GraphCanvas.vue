<template>
  <div class="graph-wrap" ref="wrapRef">
    <svg
      class="graph-svg"
      :width="wrapW"
      :height="wrapH"
      @mousedown="onBgMouseDown"
      @wheel.prevent="onWheel"
    >
      <defs>
        <marker id="gc-arrow" markerWidth="9" markerHeight="9" refX="8" refY="3.5" orient="auto">
          <path d="M0,0 L8,3.5 L0,7 Z" fill="#a78bfa" />
        </marker>
        <marker id="gc-arrow-sel" markerWidth="9" markerHeight="9" refX="8" refY="3.5" orient="auto">
          <path d="M0,0 L8,3.5 L0,7 Z" fill="#7c3aed" />
        </marker>
        <radialGradient id="gc-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgba(124,58,237,0.22)" />
          <stop offset="100%" stop-color="rgba(124,58,237,0)" />
        </radialGradient>
      </defs>

      <g :transform="`translate(${view.x},${view.y}) scale(${view.k})`">
        <!-- 边 -->
        <g v-for="e in edgeViews" :key="e.id">
          <path
            :d="e.path"
            fill="none"
            :stroke="e.selected ? '#7c3aed' : '#c4b5fd'"
            :stroke-width="e.selected ? 2.6 : 1.6"
            :stroke-dasharray="e.dashed ? '6 5' : 'none'"
            :marker-end="e.selected ? 'url(#gc-arrow-sel)' : 'url(#gc-arrow)'"
            opacity="0.9"
          />
          <!-- 加宽命中区 -->
          <path :d="e.path" fill="none" stroke="transparent" stroke-width="14" class="edge-hit" @mousedown.stop @click.stop="onEdgeClick(e)" />
          <g v-if="e.label" class="edge-label" @mousedown.stop @click.stop="onEdgeClick(e)">
            <rect :x="e.mx - e.labelW / 2" :y="e.my - 10" :width="e.labelW" height="18" rx="9"
              :fill="e.selected ? '#7c3aed' : 'rgba(255,255,255,0.95)'" />
            <text :x="e.mx" :y="e.my + 3.5" text-anchor="middle" font-size="10.5"
              :fill="e.selected ? '#fff' : '#6d28d9'" font-weight="600">{{ e.label }}</text>
          </g>
        </g>

        <!-- 连线拖拽中的临时线 -->
        <line v-if="linking" :x1="linking.x1" :y1="linking.y1" :x2="linking.x2" :y2="linking.y2"
          stroke="#0891b2" stroke-width="2" stroke-dasharray="5 4" />

        <!-- 节点 -->
        <g v-for="n in nodes" :key="n.id" :transform="`translate(${n.x},${n.y})`"
          class="gnode" :class="{ selected: selectedId === n.id }"
          @mousedown.stop="onNodeMouseDown($event, n)">
          <!-- 光晕 -->
          <circle v-if="mode === 'instance'" :r="(n.size || 16) + 12" fill="url(#gc-glow)" />
          <!-- 实体节点：圆角矩形 -->
          <template v-if="mode === 'schema'">
            <rect :x="-n.w / 2" :y="-26" :width="n.w" height="52" rx="14"
              :fill="'rgba(255,255,255,0.96)'" :stroke="n.color" :stroke-width="selectedId === n.id ? 2.4 : 1.4" />
            <rect :x="-n.w / 2" :y="-26" :width="n.w" height="7" rx="3.5" :fill="n.color" />
            <text :y="-2" text-anchor="middle" font-size="13" font-weight="700" fill="#1e1b4b">{{ n.label }}</text>
            <text :y="14" text-anchor="middle" font-size="10" fill="#94919f">
              {{ n.meta?.abstract ? '抽象 · ' : '' }}{{ n.meta?.count ?? 0 }} 属性
            </text>
          </template>
          <!-- 实例节点：圆形 -->
          <template v-else>
            <circle :r="n.size || 16" :fill="n.color" fill-opacity="0.16" :stroke="n.color" :stroke-width="selectedId === n.id ? 3 : 2" />
            <circle :r="Math.max(4, (n.size || 16) * 0.42)" :fill="n.color" />
            <text :y="(n.size || 16) + 14" text-anchor="middle" font-size="11" font-weight="600" fill="#5b5878">{{ n.label }}</text>
          </template>
          <!-- 连线手柄（schema 模式，hover 显示） -->
          <circle v-if="mode === 'schema'" :cx="n.w / 2" cy="0" r="7" class="link-handle"
            :fill="'#0891b2'" @mousedown.stop="onLinkStart($event, n)" />
        </g>
      </g>
    </svg>

    <!-- 空状态 -->
    <div v-if="!nodes.length" class="graph-empty">
      <el-icon :size="34"><Share /></el-icon>
      <div>{{ emptyText }}</div>
    </div>

    <!-- 工具栏 -->
    <div class="graph-tools">
      <button title="放大" @click="zoomBy(1.2)"><el-icon><ZoomIn /></el-icon></button>
      <button title="缩小" @click="zoomBy(0.8)"><el-icon><ZoomOut /></el-icon></button>
      <button title="适应画布" @click="fitView"><el-icon><FullScreen /></el-icon></button>
      <button title="重新布局" @click="relayout"><el-icon><Refresh /></el-icon></button>
    </div>

    <!-- 图例 -->
    <div v-if="legend.length" class="graph-legend">
      <div v-for="l in legend" :key="l.label" class="legend-item">
        <span class="legend-dot" :style="{ background: l.color }"></span>{{ l.label }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import type { GraphData, GraphNode } from '@/types'
import { forceLayout, edgePath, type LayoutNode } from '@/utils/graphLayout'

const props = withDefaults(
  defineProps<{
    data: GraphData
    mode?: 'schema' | 'instance'
    height?: number
    selectedId?: string | null
    emptyText?: string
    legend?: { label: string; color: string }[]
  }>(),
  { mode: 'schema', height: 560, selectedId: null, emptyText: '暂无数据', legend: () => [] },
)

const emit = defineEmits<{
  (e: 'select', node: LayoutNode): void
  (e: 'node-dblclick', node: LayoutNode): void
  (e: 'edge-click', edge: any): void
  (e: 'canvas-click'): void
  (e: 'add-relation', sourceId: string, targetId: string): void
}>()

const wrapRef = ref<HTMLElement>()
const wrapW = ref(1000)
const wrapH = ref(560)
const nodes = ref<LayoutNode[]>([])
const view = reactive({ x: 0, y: 0, k: 1 })

let drag: { node: LayoutNode; ox: number; oy: number; moved: boolean; sx: number; sy: number } | null = null
let pan: { sx: number; sy: number; ox: number; oy: number; moved: boolean } | null = null
const linking = ref<{ sourceId: string; x1: number; y1: number; x2: number; y2: number } | null>(null)

// 有效画布尺寸：优先取容器实测值（自适应），否则回退到 prop
function W() {
  return wrapW.value || 1000
}
function H() {
  return wrapH.value || props.height
}

function toGraph(clientX: number, clientY: number) {
  const rect = wrapRef.value!.getBoundingClientRect()
  return {
    x: (clientX - rect.left - view.x) / view.k,
    y: (clientY - rect.top - view.y) / view.k,
  }
}

function relayout() {
  const layout = forceLayout(props.data, { width: W(), height: H() })
  nodes.value = layout.nodes.map((n) => ({ ...n, w: nodeWidth(n) }))
  fitView()
}

function nodeWidth(n: LayoutNode) {
  if (props.mode === 'instance') return 0
  return Math.max(128, (n.label || '').length * 14 + 44)
}

function fitView() {
  if (!nodes.value.length) {
    view.x = 0; view.y = 0; view.k = 1
    return
  }
  const xs = nodes.value.map((n) => n.x)
  const ys = nodes.value.map((n) => n.y)
  const minX = Math.min(...xs) - 60
  const maxX = Math.max(...xs) + 60
  const minY = Math.min(...ys) - 60
  const maxY = Math.max(...ys) + 60
  const w = maxX - minX
  const h = maxY - minY
  const k = Math.min(1.6, Math.min(W() / w, H() / h))
  view.k = k
  view.x = W() / 2 - ((minX + maxX) / 2) * k
  view.y = H() / 2 - ((minY + maxY) / 2) * k
}

function zoomBy(factor: number) {
  const cx = W() / 2
  const cy = H() / 2
  const k2 = Math.max(0.3, Math.min(3, view.k * factor))
  view.x = cx - (cx - view.x) * (k2 / view.k)
  view.y = cy - (cy - view.y) * (k2 / view.k)
  view.k = k2
}

function onWheel(e: WheelEvent) {
  const rect = wrapRef.value!.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  const factor = e.deltaY < 0 ? 1.12 : 0.89
  const k2 = Math.max(0.3, Math.min(3, view.k * factor))
  view.x = mx - (mx - view.x) * (k2 / view.k)
  view.y = my - (my - view.y) * (k2 / view.k)
  view.k = k2
}

// ── 节点拖拽 ──
function onNodeMouseDown(e: MouseEvent, n: LayoutNode) {
  e.stopPropagation()
  const g = toGraph(e.clientX, e.clientY)
  drag = { node: n, ox: g.x - n.x, oy: g.y - n.y, moved: false, sx: e.clientX, sy: e.clientY }
  n.fx = n.x
  n.fy = n.y
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

// ── 连线 ──
function onLinkStart(e: MouseEvent, n: LayoutNode) {
  e.stopPropagation()
  const g = toGraph(e.clientX, e.clientY)
  linking.value = { sourceId: n.id, x1: n.x, y1: n.y, x2: g.x, y2: g.y }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onLinkUp)
}
function onLinkUp(e: MouseEvent) {
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onLinkUp)
  const src = linking.value
  const target = nodeAt(e.clientX, e.clientY)
  if (src && target && target.id !== src.sourceId) {
    emit('add-relation', src.sourceId, target.id)
  }
  linking.value = null
}
function nodeAt(clientX: number, clientY: number): LayoutNode | null {
  const g = toGraph(clientX, clientY)
  let best: LayoutNode | null = null
  let bestD = Infinity
  for (const n of nodes.value) {
    const r = props.mode === 'instance' ? (n.size || 16) : Math.max(n.w / 2, 26)
    const d = Math.hypot(n.x - g.x, n.y - g.y)
    if (d < r + 8 && d < bestD) {
      best = n
      bestD = d
    }
  }
  return best
}

// ── 平移 ──
function onBgMouseDown(e: MouseEvent) {
  pan = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y, moved: false }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

function onMove(e: MouseEvent) {
  if (drag) {
    const g = toGraph(e.clientX, e.clientY)
    drag.node.x = g.x - drag.ox
    drag.node.y = g.y - drag.oy
    drag.node.fx = drag.node.x
    drag.node.fy = drag.node.y
    if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 4) drag.moved = true
  } else if (pan) {
    view.x = pan.ox + (e.clientX - pan.sx)
    view.y = pan.oy + (e.clientY - pan.sy)
    if (Math.hypot(e.clientX - pan.sx, e.clientY - pan.sy) > 4) pan.moved = true
  } else if (linking.value) {
    const g = toGraph(e.clientX, e.clientY)
    linking.value.x2 = g.x
    linking.value.y2 = g.y
  }
}

function onUp(e: MouseEvent) {
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  if (drag) {
    drag.node.fx = null
    drag.node.fy = null
    if (!drag.moved) emit('select', drag.node)
    drag = null
  } else if (pan) {
    if (!pan.moved) emit('canvas-click')
    pan = null
  }
}

function onEdgeClick(e: any) {
  emit('edge-click', e)
}

// ── 边视图 ──
const edgeViews = computed(() => {
  const byId = new Map(nodes.value.map((n) => [n.id, n]))
  return props.data.edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => {
      const a = byId.get(e.source)!
      const b = byId.get(e.target)!
      // 从节点边缘出发
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.hypot(dx, dy) || 1
      const ra = props.mode === 'instance' ? (a.size || 16) : Math.max(a.w / 2, 26)
      const rb = props.mode === 'instance' ? (b.size || 16) : Math.max(b.w / 2, 26)
      const x1 = a.x + (dx / dist) * ra
      const y1 = a.y + (dy / dist) * ra
      const x2 = b.x - (dx / dist) * (rb + 6)
      const y2 = b.y - (dy / dist) * (rb + 6)
      const { path, mx, my } = edgePath(x1, y1, x2, y2, 0.16)
      const label = e.label || ''
      return {
        id: e.id,
        path,
        mx,
        my,
        label,
        labelW: label.length * 11 + 16,
        dashed: e.relation_type === 'N:M' || e.relation_type === 'many_to_many' || e.type === 'N:M',
        selected: false,
      }
    })
})

let ro: ResizeObserver | null = null
function resize() {
  if (!wrapRef.value) return
  const w = wrapRef.value.clientWidth
  const h = wrapRef.value.clientHeight
  if (w && Math.abs(w - wrapW.value) > 1) wrapW.value = w
  if (h && Math.abs(h - wrapH.value) > 1) wrapH.value = h
}

watch(
  () => [props.data, props.mode],
  () => {
    relayout()
  },
  { immediate: true },
)

onMounted(() => {
  resize()
  window.addEventListener('resize', resize)
  if (typeof ResizeObserver !== 'undefined' && wrapRef.value) {
    ro = new ResizeObserver(() => {
      const before = { w: wrapW.value, h: wrapH.value }
      resize()
      // 尺寸变化后重新适配视图（保留节点相对位置，避免随机重排抖动）
      if (Math.abs(wrapW.value - before.w) > 2 || Math.abs(wrapH.value - before.h) > 2) {
        fitView()
      }
    })
    ro.observe(wrapRef.value)
  }
  relayout()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  ro?.disconnect()
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  window.removeEventListener('mouseup', onLinkUp)
})
</script>

<style scoped>
.graph-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  border-radius: var(--radius);
  overflow: hidden;
  background:
    radial-gradient(circle at 20% 20%, rgba(124, 58, 237, 0.06), transparent 40%),
    radial-gradient(circle at 80% 70%, rgba(8, 145, 178, 0.06), transparent 40%),
    linear-gradient(180deg, #fbfaff, #f5f3fc);
  border: 1px solid var(--border);
}
.graph-svg {
  display: block;
  cursor: grab;
  touch-action: none;
}
.graph-svg:active {
  cursor: grabbing;
}
.gnode {
  cursor: pointer;
}
.gnode.selected rect,
.gnode.selected circle {
  filter: drop-shadow(0 0 10px rgba(124, 58, 237, 0.5));
}
.link-handle {
  opacity: 0;
  transition: opacity 0.15s;
  cursor: crosshair;
}
.gnode:hover .link-handle {
  opacity: 1;
}
.edge-hit {
  cursor: pointer;
}
.edge-label rect {
  stroke: var(--border);
}
.graph-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-3);
  font-size: 13px;
  pointer-events: none;
}
.graph-tools {
  position: absolute;
  right: 14px;
  top: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 6px;
  box-shadow: var(--shadow-sm);
}
.graph-tools button {
  width: 32px;
  height: 32px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--primary-600);
  transition: background var(--dur) var(--ease);
}
.graph-tools button:hover {
  background: var(--grad-soft);
}
.graph-legend {
  position: absolute;
  left: 14px;
  bottom: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 8px 12px;
  max-width: 70%;
  box-shadow: var(--shadow-xs);
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-2);
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
</style>
