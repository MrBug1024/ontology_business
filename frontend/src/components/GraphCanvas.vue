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
          <path d="M0,0 L8,3.5 L0,7 Z" fill="var(--graph-edge)" />
        </marker>
        <marker id="gc-arrow-sel" markerWidth="9" markerHeight="9" refX="8" refY="3.5" orient="auto">
          <path d="M0,0 L8,3.5 L0,7 Z" fill="var(--primary)" />
        </marker>
        <linearGradient id="gc-node-bar" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stop-color="var(--graph-teal)" />
          <stop offset="100%" stop-color="var(--graph-blue)" />
        </linearGradient>
        <radialGradient id="gc-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgba(44,190,194,0.22)" />
          <stop offset="100%" stop-color="rgba(44,190,194,0)" />
        </radialGradient>
      </defs>

      <g :transform="`translate(${view.x},${view.y}) scale(${view.k})`">
        <!-- 边 -->
        <g v-for="e in edgeViews" :key="e.id">
          <path
            :d="e.path"
            fill="none"
            :stroke="e.selected ? 'var(--primary)' : 'var(--border-strong)'"
            :stroke-width="e.selected ? 2.6 : 1.6"
            :stroke-dasharray="e.dashed ? '6 5' : 'none'"
            :marker-end="e.selected ? 'url(#gc-arrow-sel)' : 'url(#gc-arrow)'"
            opacity="0.9"
          />
          <!-- 加宽命中区 -->
          <path :d="e.path" fill="none" stroke="transparent" stroke-width="14" class="edge-hit" @mousedown.stop @click.stop="onEdgeClick(e)" />
          <g v-if="e.label" class="edge-label" @mousedown.stop @click.stop="onEdgeClick(e)">
            <rect :x="e.mx - e.labelW / 2" :y="e.my - 10" :width="e.labelW" height="18" rx="9"
              :fill="e.selected ? 'var(--primary)' : 'var(--surface)'" />
            <text :x="e.mx" :y="e.my + 3.5" text-anchor="middle" font-size="10.5"
              :fill="e.selected ? '#fff' : 'var(--primary-600)'" font-weight="600">{{ e.label }}</text>
          </g>
        </g>

        <!-- 连线拖拽中的临时线 -->
        <line v-if="linking" :x1="linking.x1" :y1="linking.y1" :x2="linking.x2" :y2="linking.y2"
          stroke="var(--accent)" stroke-width="2" stroke-dasharray="5 4" />

        <!-- 节点 -->
        <g v-for="n in nodes" :key="n.id" :transform="`translate(${n.x},${n.y})`"
          class="gnode" :class="{ selected: selectedId === n.id }"
          @mousedown.stop="onNodeMouseDown($event, n)">
          <!-- 光晕 -->
          <circle v-if="mode === 'instance'" :r="(n.size || 16) + 12" fill="url(#gc-glow)" />
          <!-- 实体节点：圆角矩形 -->
          <template v-if="mode === 'schema'">
            <rect :x="-n.w / 2" :y="-n.h / 2" :width="n.w" :height="n.h" rx="16"
              :fill="'var(--surface)'" :stroke="n.color" :stroke-width="selectedId === n.id ? 2.4 : 1.3" />
            <rect :x="-n.w / 2" :y="-n.h / 2" :width="n.w" height="6" rx="3" fill="url(#gc-node-bar)" />
            <circle :cx="-n.w / 2 + 18" cy="-4" r="5" :fill="n.color" />
            <text :x="-n.w / 2 + 32" y="0" text-anchor="start" font-size="13" font-weight="700" fill="var(--text)">{{ shortLabel(n.label, 17) }}</text>
            <text :x="-n.w / 2 + 16" y="20" text-anchor="start" font-size="10" fill="var(--text-3)">
              {{ n.meta?.abstract ? '抽象 · ' : '' }}{{ n.meta?.count ?? 0 }} 属性
            </text>
          </template>
          <!-- 实例节点：信息卡片，标签不再悬浮在圆形下方互相覆盖 -->
          <template v-else>
            <rect :x="-n.w / 2" :y="-n.h / 2" :width="n.w" :height="n.h" rx="15"
              :fill="'var(--surface)'" :stroke="n.color" :stroke-width="selectedId === n.id ? 2.4 : 1.2" />
            <circle :cx="-n.w / 2 + 18" cy="0" r="7" :fill="n.color" fill-opacity="0.18" :stroke="n.color" />
            <circle :cx="-n.w / 2 + 18" cy="0" r="3" :fill="n.color" />
            <text :x="-n.w / 2 + 34" y="-2" text-anchor="start" font-size="11.5" font-weight="700" fill="var(--text)">{{ shortLabel(n.label, 18) }}</text>
            <text :x="-n.w / 2 + 34" y="15" text-anchor="start" font-size="9.5" fill="var(--text-3)">{{ shortLabel(n.meta?.entity_name || '实例节点', 14) }}</text>
          </template>
          <!-- 连线手柄（schema 模式，hover 显示） -->
          <circle v-if="mode === 'schema'" :cx="n.w / 2" cy="0" r="7" class="link-handle"
            :fill="'var(--accent)'" @mousedown.stop="onLinkStart($event, n)" />
        </g>
      </g>
    </svg>

    <!-- 空状态 -->
    <div v-if="!nodes.length" class="graph-empty">
      <el-icon :size="34"><Share /></el-icon>
      <div>{{ emptyText }}</div>
    </div>

    <div class="graph-caption">
      <span class="caption-mark"></span>
      <span>{{ mode === 'schema' ? '本体结构' : '实例关系' }}</span>
      <i>{{ nodes.length }} 个节点</i>
    </div>

    <!-- 工具栏 -->
    <div class="graph-tools">
      <button title="放大" aria-label="放大画布" @click="zoomBy(1.2)"><el-icon aria-hidden="true"><ZoomIn /></el-icon></button>
      <button title="缩小" aria-label="缩小画布" @click="zoomBy(0.8)"><el-icon aria-hidden="true"><ZoomOut /></el-icon></button>
      <button title="适应画布" aria-label="适应画布" @click="fitView"><el-icon aria-hidden="true"><FullScreen /></el-icon></button>
      <button title="重新布局" aria-label="重新布局" @click="relayout"><el-icon aria-hidden="true"><Refresh /></el-icon></button>
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
  const layout = forceLayout(props.data, {
    width: W(),
    height: H(),
    nodePadding: props.mode === 'schema' ? 44 : 30,
    nodeSize: (n) => ({ width: nodeWidth(n), height: nodeHeight(n) }),
  })
  nodes.value = layout.nodes.map((n) => ({ ...n, w: nodeWidth(n), h: nodeHeight(n) }))
  fitView()
}

function nodeWidth(n: GraphNode) {
  if (props.mode === 'instance') return Math.max(144, Math.min(228, (n.label || '').length * 11 + 68))
  return Math.max(158, Math.min(236, (n.label || '').length * 14 + 78))
}

function nodeHeight(_n: GraphNode) {
  return props.mode === 'instance' ? 52 : 72
}

function shortLabel(value: unknown, max: number) {
  const text = String(value || '')
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function fitView() {
  if (!nodes.value.length) {
    view.x = 0; view.y = 0; view.k = 1
    return
  }
  const minX = Math.min(...nodes.value.map((n) => n.x - n.w / 2)) - 56
  const maxX = Math.max(...nodes.value.map((n) => n.x + n.w / 2)) + 56
  const minY = Math.min(...nodes.value.map((n) => n.y - n.h / 2)) - 56
  const maxY = Math.max(...nodes.value.map((n) => n.y + n.h / 2)) + 56
  const w = maxX - minX
  const h = maxY - minY
  const k = Math.min(1.55, Math.min(W() / w, H() / h))
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
    const hit = Math.abs(n.x - g.x) <= n.w / 2 + 8 && Math.abs(n.y - g.y) <= n.h / 2 + 8
    const d = Math.hypot(n.x - g.x, n.y - g.y)
    if (hit && d < bestD) {
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
      // 从矩形节点边缘出发，避免连线穿透卡片。
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.hypot(dx, dy) || 1
      const sourceScale = Math.min(a.w / 2 / Math.max(Math.abs(dx / dist), 0.0001), a.h / 2 / Math.max(Math.abs(dy / dist), 0.0001))
      const targetScale = Math.min(b.w / 2 / Math.max(Math.abs(dx / dist), 0.0001), b.h / 2 / Math.max(Math.abs(dy / dist), 0.0001))
      const x1 = a.x + (dx / dist) * sourceScale
      const y1 = a.y + (dy / dist) * sourceScale
      const x2 = b.x - (dx / dist) * (targetScale + 6)
      const y2 = b.y - (dy / dist) * (targetScale + 6)
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
  border-radius: 20px;
  overflow: hidden;
  background:
    radial-gradient(circle at 12% 12%, rgba(62, 180, 217, .14), transparent 34%),
    radial-gradient(circle at 86% 78%, rgba(47, 194, 177, .11), transparent 34%),
    linear-gradient(145deg, var(--graph-bg-start), var(--graph-bg-end));
  border: 1px solid var(--graph-border);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.88), var(--shadow-sm);
}
.graph-svg {
  display: block;
  cursor: grab;
  touch-action: none;
  background-image: radial-gradient(circle, rgba(75, 151, 210, .16) 1px, transparent 1px);
  background-size: 24px 24px;
}
.graph-svg:active {
  cursor: grabbing;
}
.gnode {
  cursor: pointer;
}
.gnode.selected rect,
.gnode.selected circle {
  filter: drop-shadow(0 7px 12px rgba(38, 147, 196, .24));
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
  stroke: var(--graph-border);
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
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  border: 1px solid var(--graph-border);
  border-radius: 14px;
  padding: 6px;
  box-shadow: 0 8px 24px rgba(44, 113, 164, .12);
  backdrop-filter: blur(12px);
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
  color: var(--graph-blue-deep);
  transition: background var(--dur) var(--ease);
}
.graph-tools button:hover {
  background: var(--graph-soft);
}
.graph-caption {
  position: absolute;
  left: 16px;
  top: 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  color: var(--graph-blue-deep);
  background: color-mix(in srgb, var(--surface) 84%, transparent);
  border: 1px solid var(--graph-border);
  border-radius: 12px;
  box-shadow: 0 6px 18px rgba(44, 113, 164, .08);
  backdrop-filter: blur(10px);
  font-size: 11px;
  font-weight: 750;
  letter-spacing: .04em;
  pointer-events: none;
}
.graph-caption i {
  color: var(--text-3);
  font-size: 10px;
  font-style: normal;
  font-weight: 550;
}
.caption-mark {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--graph-teal);
  box-shadow: 0 0 0 4px rgba(44, 190, 194, .12);
}
.graph-legend {
  position: absolute;
  left: 14px;
  bottom: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  border: 1px solid var(--graph-border);
  border-radius: 12px;
  padding: 8px 12px;
  max-width: 70%;
  box-shadow: 0 6px 18px rgba(44, 113, 164, .08);
  backdrop-filter: blur(10px);
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
@media (max-width: 768px) {
  .graph-tools button { width: 44px; height: 44px; }
}
</style>
