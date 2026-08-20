<script setup lang="ts">
import { ref, computed, reactive, watch, provide, onMounted, nextTick, markRaw } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  VueFlow,
  useVueFlow,
  MarkerType,
} from '@vue-flow/core'
import type { Node, Edge, Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import WFNode from './WFNode.vue'
import { api } from '@/api'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'

const props = defineProps<{
  modelValue: any
  scenarioId: string
  actions: any[]
  rules: any[]
  events: any[]
  llmConfigs: any[]
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: any): void
  (e: 'close'): void
  (e: 'save', w: any): void
}>()

// 注入场景资源给节点卡片（显示引用的操作/规则/事件名称）
const refs = reactive({ actions: props.actions, rules: props.rules, events: props.events })
watch(
  () => [props.actions, props.rules, props.events],
  () => {
    refs.actions = props.actions
    refs.rules = props.rules
    refs.events = props.events
  },
)
provide('wfRefs', refs)

// ── 节点类型注册（全部映射到 WFNode 组件）──
const _WFNode = markRaw(WFNode)
const nodeTypes: any = {
  start: _WFNode,
  end: _WFNode,
  action: _WFNode,
  rule: _WFNode,
  llm: _WFNode,
  event: _WFNode,
  http: _WFNode,
  script: _WFNode,
}

const PALETTE = [
  { type: 'action', label: '执行操作', icon: 'Operation', color: 'var(--graph-blue)', desc: '调用已定义操作（SQL/技能/MCP…）' },
  { type: 'rule', label: '规则判断', icon: 'SetUp', color: 'var(--warning)', desc: '评估规则，命中/未命中分支' },
  { type: 'llm', label: '大模型', icon: 'Cpu', color: 'var(--primary)', desc: 'LLM 分析/生成，支持变量引用' },
  { type: 'event', label: '发布事件', icon: 'Bell', color: 'var(--accent)', desc: '发布业务事件' },
  { type: 'http', label: 'HTTP 请求', icon: 'Link', color: 'var(--graph-teal)', desc: '调用外部 API' },
  { type: 'script', label: 'Python 脚本', icon: 'Document', color: 'var(--info)', desc: '自定义 Python 片段' },
  { type: 'end', label: '结束', icon: 'CircleCheck', color: 'var(--text-3)', desc: '流程结束' },
]

// ── 画布状态 ──
const nodes = ref<any[]>([])
const edges = ref<any[]>([])
const graph = ref<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] })
const selectedId = ref('')
const { screenToFlowCoordinate, fitView, addEdges, onConnect, onNodeClick, onEdgeClick, onPaneClick } = useVueFlow()

const selNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) || null)

// ── 数据转换 ──
function toFlowNode(n: any): Node {
  return {
    id: n.id,
    type: n.type,
    position: n.position || { x: 0, y: 0 },
    data: { ...(n.data || {}), name: n.name || (n.data || {}).name || '' },
  }
}
function toFlowEdge(e: any): Edge {
  const label = e.label === 'true' ? '命中' : e.label === 'false' ? '未命中' : e.label || ''
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.label || null,
    label: label || undefined,
    animated: !e.label,
    style: {
      stroke: e.label === 'true' ? 'var(--success)' : e.label === 'false' ? 'var(--danger)' : 'var(--graph-edge)',
      strokeWidth: 1.8,
    },
    labelStyle: { fill: 'var(--text-2)', fontSize: '10px', fontWeight: 600 },
    labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.95 },
    labelBgPadding: [6, 3] as [number, number],
    labelBgBorderRadius: 6,
    markerEnd: MarkerType.ArrowClosed,
  }
}
let loading = false
function syncToFlow() {
  loading = true
  nodes.value = graph.value.nodes.map(toFlowNode)
  edges.value = graph.value.edges.map(toFlowEdge)
  nextTick(() => {
    loading = false
    fitView({ padding: 0.25, duration: 250 })
  })
}
function syncFromFlow() {
  graph.value = {
    nodes: nodes.value.map((n) => ({
      id: n.id,
      type: n.type as string,
      name: (n.data as any).name || '',
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      data: { ...(n.data as any) },
    })),
    edges: edges.value.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.sourceHandle || '',
    })),
  }
  emitUpdate()
}

// ── 与父组件同步 ──
let lastEmitted: any = null
function emitUpdate() {
  const w = { ...props.modelValue, nodes: graph.value.nodes, edges: graph.value.edges }
  lastEmitted = w
  emit('update:modelValue', w)
}
function loadFromModel() {
  const w = props.modelValue || {}
  let ns: any[] = (w.nodes || []).map((n: any) => ({ ...n, data: { ...(n.data || {}) } }))
  let es: any[] = (w.edges || []).map((e: any) => ({ ...e }))
  // 旧版线性 steps → DAG（兼容）
  if (!ns.length && (w.steps || []).length) {
    const conv = stepsToNodes(w.steps)
    ns = conv.nodes
    es = conv.edges
  }
  graph.value = { nodes: ns, edges: es }
  selectedId.value = ''
  syncToFlow()
}
function stepsToNodes(steps: any[]) {
  const ns: any[] = [{ id: 'start', type: 'start', name: '开始', position: { x: 0, y: 0 }, data: {} }]
  const es: any[] = []
  let prev = 'start'
  steps.forEach((s: any, i: number) => {
    const id = `n${i + 1}`
    const base = { id, position: { x: 280, y: (i + 1) * 120 } }
    if (s.type === 'action') ns.push({ ...base, type: 'action', name: `操作 ${i + 1}`, data: { action_id: s.action_id, params: s.params || {} } })
    else if (s.type === 'rule') ns.push({ ...base, type: 'rule', name: `规则 ${i + 1}`, data: { rule_id: s.rule_id, record: s.record || {} } })
    else if (s.type === 'event') ns.push({ ...base, type: 'event', name: `事件 ${i + 1}`, data: { event_id: s.event_id, payload: s.payload || {} } })
    es.push({ id: `e${i + 1}`, source: prev, target: id, label: '' })
    prev = id
  })
  ns.push({ id: 'end', type: 'end', name: '结束', position: { x: 280, y: (steps.length + 1) * 120 }, data: {} })
  es.push({ id: `e${steps.length + 1}`, source: prev, target: 'end', label: '' })
  return { nodes: ns, edges: es }
}
watch(
  () => props.modelValue,
  (v) => {
    if (v === lastEmitted) return
    loadFromModel()
  },
)
watch([nodes, edges], () => {
  if (loading) return
  syncFromFlow()
}, { deep: true })
onMounted(loadFromModel)

// ── 连线 ──
onConnect((c: Connection) => {
  const label = c.sourceHandle || ''
  addEdges({
    ...c,
    id: 'e' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5),
    sourceHandle: label || null,
    label: label === 'true' ? '命中' : label === 'false' ? '未命中' : undefined,
    animated: !label,
    style: {
      stroke: label === 'true' ? 'var(--success)' : label === 'false' ? 'var(--danger)' : 'var(--graph-edge)',
      strokeWidth: 1.8,
    },
    labelStyle: { fill: 'var(--text-2)', fontSize: '10px', fontWeight: 600 },
    labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.95 },
    labelBgPadding: [6, 3] as [number, number],
    labelBgBorderRadius: 6,
    markerEnd: MarkerType.ArrowClosed,
  })
})
onNodeClick(({ node }) => {
  selectedId.value = node.id
})
onEdgeClick(() => {
  selectedId.value = ''
})
onPaneClick(() => {
  selectedId.value = ''
})

// ── 添加 / 删除节点 ──
function genId(type: string) {
  if (type === 'start') return 'start'
  if (type === 'end') return 'end'
  let i = nodes.value.length + 1
  let id = `n${i}`
  while (nodes.value.some((n) => n.id === id)) id = `n${++i}`
  return id
}
function defaultData(type: string) {
  switch (type) {
    case 'start': return { name: '开始' }
    case 'end': return { name: '结束', summary: '' }
    case 'action': return { name: '执行操作', action_id: '', params: {} }
    case 'rule': return { name: '规则判断', rule_id: '', record: {} }
    case 'llm': return { name: '大模型分析', prompt: '', system: '' }
    case 'event': return { name: '发布事件', event_id: '', payload: {} }
    case 'http': return { name: 'HTTP 请求', method: 'GET', url: '', body: {} }
    case 'script': return { name: 'Python 脚本', script: 'result = {"ok": True}' }
    default: return { name: type }
  }
}
function addNode(type: string, pos?: { x: number; y: number }) {
  const id = genId(type)
  const p = pos || { x: 60 + Math.random() * 240, y: 60 + Math.random() * 240 }
  nodes.value = [...nodes.value, { id, type, position: p, data: defaultData(type) }]
  selectedId.value = id
}
function deleteNode() {
  const id = selectedId.value
  if (!id) return
  nodes.value = nodes.value.filter((n) => n.id !== id)
  edges.value = edges.value.filter((e) => e.source !== id && e.target !== id)
  selectedId.value = ''
}

// ── 拖拽入画布 ──
function onDragStart(event: DragEvent, type: string) {
  if (event.dataTransfer) {
    event.dataTransfer.setData('application/wf-node', type)
    event.dataTransfer.effectAllowed = 'move'
  }
}
function onDragOver(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
}
function onDrop(event: DragEvent) {
  const type = event.dataTransfer?.getData('application/wf-node')
  if (!type) return
  const pos = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
  addNode(type, { x: pos.x - 90, y: pos.y - 24 })
}

// ── 自动布局（拓扑分层）──
function autoLayout() {
  const ns = graph.value.nodes
  const es = graph.value.edges
  if (!ns.length) return
  const preds: Record<string, string[]> = {}
  ns.forEach((n) => (preds[n.id] = []))
  es.forEach((e) => {
    if (preds[e.target]) preds[e.target].push(e.source)
  })
  const layer: Record<string, number> = {}
  ns.forEach((n) => (layer[n.id] = 0))
  for (let iter = 0; iter <= ns.length; iter++) {
    let changed = false
    for (const n of ns) {
      const pl = preds[n.id].reduce((m, p) => Math.max(m, layer[p] || 0), 0)
      if (pl > layer[n.id]) {
        layer[n.id] = pl
        changed = true
      }
    }
    if (!changed) break
  }
  const byLayer: Record<number, any[]> = {}
  ns.forEach((n) => {
    const l = layer[n.id]
    if (!byLayer[l]) byLayer[l] = []
    byLayer[l].push(n)
  })
  const GAP_X = 300
  const GAP_Y = 132
  const maxRow = Math.max(...Object.values(byLayer).map((a) => a.length))
  const centerY = ((maxRow - 1) * GAP_Y) / 2
  Object.entries(byLayer).forEach(([l, arr]) => {
    arr.forEach((n, i) => {
      n.position = { x: Number(l) * GAP_X, y: i * GAP_Y - centerY }
    })
  })
  syncToFlow()
}

// ── 校验 ──
function validate(): string[] {
  const errs: string[] = []
  const ns = graph.value.nodes
  const es = graph.value.edges
  const starts = ns.filter((n) => n.type === 'start')
  if (starts.length === 0) errs.push('缺少「开始」节点')
  if (starts.length > 1) errs.push('只能有一个「开始」节点')
  if (!ns.some((n) => n.type === 'end')) errs.push('缺少「结束」节点')
  for (const n of ns) {
    const d = n.data || {}
    const nm = n.name || n.id
    if (n.type === 'action' && !d.action_id) errs.push(`节点「${nm}」未选择操作`)
    if (n.type === 'rule' && !d.rule_id) errs.push(`节点「${nm}」未选择规则`)
    if (n.type === 'event' && !d.event_id) errs.push(`节点「${nm}」未选择事件`)
    if (n.type === 'llm' && !(d.prompt || '').trim()) errs.push(`节点「${nm}」未填写提示词`)
    if (n.type === 'http' && !(d.url || '').trim()) errs.push(`节点「${nm}」未填写 URL`)
  }
  for (const n of ns) {
    if (n.type !== 'rule') continue
    const nm = n.name || n.id
    if (!es.some((e) => e.source === n.id && e.label === 'true')) errs.push(`规则节点「${nm}」缺少「命中」分支连线`)
    if (!es.some((e) => e.source === n.id && e.label === 'false')) errs.push(`规则节点「${nm}」缺少「未命中」分支连线`)
  }
  if (starts.length === 1) {
    const out: Record<string, string[]> = {}
    ns.forEach((n) => (out[n.id] = []))
    es.forEach((e) => out[e.source]?.push(e.target))
    const seen = new Set<string>([starts[0].id])
    const q = [starts[0].id]
    while (q.length) {
      const cur = q.shift()!
      for (const t of out[cur] || []) if (!seen.has(t)) { seen.add(t); q.push(t) }
    }
    for (const n of ns) if (!seen.has(n.id)) errs.push(`节点「${n.name || n.id}」未与开始节点连通`)
  }
  return errs
}
async function validateNow() {
  const errs = validate()
  if (!errs.length) {
    ElMessage.success('校验通过，工作流结构完整')
    return
  }
  await ElMessageBox.alert(errs.map((e) => `· ${e}`).join('\n'), '发现以下问题', {
    type: 'warning',
    confirmButtonText: '知道了',
  })
}

// ── 保存 / 执行 ──
function save() {
  const errs = validate()
  if (errs.length) {
    ElMessageBox.alert(errs.map((e) => `· ${e}`).join('\n'), '保存前请修正以下问题', {
      type: 'warning',
      confirmButtonText: '知道了',
    })
    return
  }
  syncFromFlow()
  emit('save', { ...props.modelValue, nodes: graph.value.nodes, edges: graph.value.edges })
}

const executing = ref(false)
const execDlg = ref(false)
const execText = ref('')
async function doExecute() {
  if (!props.modelValue.id) {
    ElMessage.warning('请先保存工作流，再执行')
    return
  }
  executing.value = true
  try {
    const { value } = await ElMessageBox.prompt('输入工作流参数（JSON，可为空 {}）', '执行工作流', {
      inputValue: '{}',
      inputPattern: /\S/,
      confirmButtonText: '执行',
      cancelButtonText: '取消',
    })
    const params = JSON.parse(value || '{}')
    const res = await api.executeWorkflow(props.modelValue.id, params)
    execText.value = formatExec(res)
    execDlg.value = true
  } catch (e: any) {
    if (e !== 'cancel' && e?.message !== 'cancel') ElMessage.error(e?.message || '执行失败')
  } finally {
    executing.value = false
  }
}
function formatExec(res: any) {
  const lines: string[] = [`状态: ${res.status}    耗时: ${res.duration_ms}ms`, '─'.repeat(46)]
  for (const s of res.steps || []) {
    const ok = ['success', 'matched', 'published'].includes(s.status)
    const icon = ok ? '✓' : s.status === 'not_matched' ? '∅' : s.status === 'failed' ? '✗' : '·'
    lines.push(`${icon} [${s.type}] ${s.name || s.node || ''} → ${s.status}${s.error ? `  (${s.error})` : ''}`)
    if (s.result) lines.push(`    ${JSON.stringify(s.result).slice(0, 260)}`)
  }
  if (res.error) lines.push('', '错误: ' + res.error)
  return lines.join('\n')
}

// ── AI 生成 ──
const aiDlg = ref(false)
const aiDesc = ref('')
const aiLoading = ref(false)
async function runAiGenerate() {
  aiLoading.value = true
  try {
    const res = await api.generateWorkflow(props.scenarioId, aiDesc.value)
    graph.value = { nodes: res.nodes, edges: res.edges }
    const updated: any = { ...props.modelValue, nodes: res.nodes, edges: res.edges }
    if (!props.modelValue.name) {
      updated.name = res.name
      updated.description = res.description
    }
    // 记录 lastEmitted，避免 modelValue watcher 触发 loadFromModel 清空刚生成的图
    lastEmitted = updated
    emit('update:modelValue', updated)
    syncToFlow()
    aiDlg.value = false
    ElMessage.success('AI 已生成工作流草稿，请检查节点配置后保存')
  } catch (e: any) {
    ElMessage.error(e?.message || '生成失败')
  } finally {
    aiLoading.value = false
  }
}

// ── 右侧配置面板：JSON 字段 ──
const jsonText = ref('')
const jsonKey = computed(() => {
  switch (selNode.value?.type) {
    case 'action': return 'params'
    case 'rule': return 'record'
    case 'event': return 'payload'
    case 'http': return 'body'
    default: return ''
  }
})
watch(selectedId, () => {
  const n = selNode.value
  if (n && jsonKey.value) jsonText.value = JSON.stringify(n.data[jsonKey.value] ?? {}, null, 2)
})
function onJsonInput() {
  const n = selNode.value
  if (!n || !jsonKey.value) return
  try {
    n.data[jsonKey.value] = jsonText.value.trim() ? JSON.parse(jsonText.value) : {}
  } catch {
    /* 等待 JSON 合法后再应用 */
  }
}

const wf = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <div class="wfe">
    <!-- 顶栏：返回 + 名称/描述 -->
    <div class="wfe-topbar">
      <el-button size="small" text @click="emit('close')"><el-icon><ArrowLeft /></el-icon> 返回</el-button>
      <el-input v-model="wf.name" size="small" class="wfe-name" placeholder="工作流名称，如：数据检查与通知流程" />
      <el-input v-model="wf.description" size="small" class="wfe-desc" placeholder="描述（可选）" />
    </div>

    <div class="wfe-body">
      <!-- 左：节点库 -->
      <div class="wfe-palette">
        <div class="wfe-pal-title">节点库</div>
        <div
          class="wfe-pal-item"
          role="button"
          tabindex="0"
          :aria-label="`添加节点：${p.label}`"
          v-for="p in PALETTE"
          :key="p.type"
          draggable="true"
          @dragstart="onDragStart($event, p.type)"
          @click="addNode(p.type)"
          @keydown.enter.prevent="addNode(p.type)"
          @keydown.space.prevent="addNode(p.type)"
          :style="{ '--pc': p.color }"
        >
          <span class="wfe-pal-ico" aria-hidden="true"><el-icon :size="15"><component :is="p.icon" /></el-icon></span>
          <div class="wfe-pal-tt">
            <b>{{ p.label }}</b>
            <small>{{ p.desc }}</small>
          </div>
        </div>
        <div class="wfe-pal-tip">拖拽到画布，或点击添加</div>
      </div>

      <!-- 中：画布 -->
      <div class="wfe-canvas" @drop="onDrop" @dragover="onDragOver">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep' }"
          :connection-radius="40"
          :min-zoom="0.25"
          :max-zoom="2"
          fit-view-on-init
          class="wfe-flow"
        >
          <Background :gap="22" :size="1.5" color="#8ab9dc" />
          <Controls position="bottom-left" :show-interactions="false" />
        </VueFlow>

        <!-- 工具栏 -->
        <div class="wfe-toolbar">
          <el-button size="small" @click="autoLayout"><el-icon><Sort /></el-icon> 自动布局</el-button>
          <el-button size="small" @click="validateNow"><el-icon><CircleCheck /></el-icon> 校验</el-button>
          <el-button size="small" @click="aiDlg = true"><el-icon><MagicStick /></el-icon> AI 生成</el-button>
          <el-button size="small" type="primary" :loading="executing" @click="doExecute"><el-icon><VideoPlay /></el-icon> 执行</el-button>
          <el-button size="small" type="success" @click="save"><el-icon><Check /></el-icon> 保存</el-button>
        </div>

        <!-- 空状态 -->
        <div v-if="!nodes.length" class="wfe-empty">
          <el-icon :size="36"><Share /></el-icon>
          <p>从左侧拖入节点开始编排，或点击「AI 生成」</p>
        </div>
      </div>

      <!-- 右：节点配置面板 -->
      <div class="wfe-panel" v-if="selNode">
        <div class="wfe-panel-head">
          <b>{{ selNode.data.name || selNode.id }}</b>
          <el-button size="small" text @click="selectedId = ''" aria-label="关闭节点配置" title="关闭节点配置"><el-icon aria-hidden="true"><Close /></el-icon></el-button>
        </div>
        <div class="wfe-panel-body">
          <div class="wfe-field">
            <label>节点名称</label>
            <el-input v-model="selNode.data.name" size="small" />
          </div>

          <template v-if="selNode.type === 'action'">
            <div class="wfe-field">
              <label>选择操作</label>
              <el-select v-model="selNode.data.action_id" size="small" style="width:100%" placeholder="选择要执行的操作">
                <el-option v-for="a in actions" :key="a.id" :label="`${a.name}（${a.executor_type}）`" :value="a.id" />
              </el-select>
            </div>
            <div class="wfe-field">
              <label>参数（JSON，支持 {{ '{params.x}' }} 变量）</label>
              <el-input v-model="jsonText" type="textarea" :rows="6" class="mono" @input="onJsonInput" />
            </div>
          </template>

          <template v-if="selNode.type === 'rule'">
            <div class="wfe-field">
              <label>选择规则</label>
              <el-select v-model="selNode.data.rule_id" size="small" style="width:100%" placeholder="选择要评估的规则">
                <el-option v-for="r in rules" :key="r.id" :label="r.name" :value="r.id" />
              </el-select>
            </div>
            <div class="wfe-field">
              <label>评估记录（JSON，支持变量引用）</label>
              <el-input v-model="jsonText" type="textarea" :rows="6" class="mono" @input="onJsonInput" />
            </div>
            <div class="wfe-hint">命中 → 右侧「命中」出口；未命中 → 下方「未命中」出口</div>
          </template>

          <template v-if="selNode.type === 'llm'">
            <div class="wfe-field">
              <label>提示词（支持 {{ '{params.x}' }} / {{ '{n1.result}' }} 变量）</label>
          <el-input v-model="selNode.data.prompt" type="textarea" :rows="7" class="mono" placeholder="如：分析以下业务数据并给出结论：{{n1.result}}" />
            </div>
            <div class="wfe-field">
              <label>系统提示（可选）</label>
          <el-input v-model="selNode.data.system" type="textarea" :rows="2" placeholder="你是一个严谨的业务助手" />
            </div>
          </template>

          <template v-if="selNode.type === 'event'">
            <div class="wfe-field">
              <label>选择事件</label>
              <el-select v-model="selNode.data.event_id" size="small" style="width:100%" placeholder="选择要发布的事件">
                <el-option v-for="ev in events" :key="ev.id" :label="ev.name" :value="ev.id" />
              </el-select>
            </div>
            <div class="wfe-field">
              <label>事件负载（JSON，支持变量）</label>
              <el-input v-model="jsonText" type="textarea" :rows="4" class="mono" @input="onJsonInput" />
            </div>
          </template>

          <template v-if="selNode.type === 'http'">
            <div class="wfe-field">
              <label>请求方法</label>
              <el-select v-model="selNode.data.method" size="small" style="width:100%">
                <el-option v-for="m in ['GET', 'POST', 'PUT', 'DELETE']" :key="m" :label="m" :value="m" />
              </el-select>
            </div>
            <div class="wfe-field">
              <label>URL（支持变量）</label>
              <el-input v-model="selNode.data.url" size="small" class="mono" placeholder="https://api.example.com/v1/audit?org={params.org}" />
            </div>
            <div class="wfe-field">
              <label>请求体（JSON，POST/PUT 时生效）</label>
              <el-input v-model="jsonText" type="textarea" :rows="4" class="mono" @input="onJsonInput" />
            </div>
          </template>

          <template v-if="selNode.type === 'script'">
            <div class="wfe-field">
              <label>Python 代码（可用 params / ctx / json，结果赋给 result）</label>
              <el-input v-model="selNode.data.script" type="textarea" :rows="8" class="mono" />
            </div>
          </template>

          <template v-if="selNode.type === 'end'">
            <div class="wfe-field">
              <label>结束摘要（支持变量）</label>
          <el-input v-model="selNode.data.summary" type="textarea" :rows="3" placeholder="如：流程完成，共处理 {{n2.result.matched}} 条记录" />
            </div>
          </template>

          <el-button size="small" type="danger" plain class="wfe-del" @click="deleteNode">
            <el-icon><Delete /></el-icon> 删除节点
          </el-button>
        </div>
      </div>
    </div>

    <!-- AI 生成对话框 -->
    <el-dialog v-model="aiDlg" title="AI 生成工作流" width="560px" class="glass-dialog">
      <el-input
        v-model="aiDesc"
        type="textarea"
        :rows="4"
        placeholder="描述业务流程，AI 将自动编排节点与连线。例如：查询业务数据，判断是否命中规则，命中后生成结果，最后结束"
      />
      <template #footer>
        <el-button @click="aiDlg = false">取消</el-button>
        <el-button type="primary" :loading="aiLoading" @click="runAiGenerate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 执行结果对话框 -->
    <el-dialog v-model="execDlg" title="执行结果" width="680px" class="glass-dialog">
      <pre class="exec-result mono">{{ execText }}</pre>
      <template #footer>
        <el-button type="primary" @click="execDlg = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.wfe {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.wfe-topbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.wfe-name {
  width: 260px;
}
.wfe-desc {
  flex: 1;
  min-width: 120px;
}
.wfe-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 10px;
}

/* ── 节点库 ── */
.wfe-palette {
  width: 196px;
  flex-shrink: 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  overflow-y: auto;
}
.wfe-pal-title {
  font-size: 12px;
  font-weight: 800;
  color: var(--text-3);
  letter-spacing: 0.08em;
  padding: 2px 4px 4px;
}
.wfe-pal-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 9px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface);
  cursor: grab;
  font: inherit;
  color: inherit;
  text-align: left;
  transition: all var(--dur) var(--ease);
}
.wfe-pal-item:hover {
  border-color: var(--pc);
  box-shadow: var(--shadow-sm);
  transform: translateY(-1px);
}
.wfe-pal-item:active {
  cursor: grabbing;
}
.wfe-pal-item:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 2px; }
.wfe-pal-ico {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
  background: color-mix(in srgb, var(--pc) 12%, #fff);
  color: var(--pc);
}
.wfe-pal-tt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.wfe-pal-tt b {
  font-size: 12.5px;
  color: var(--text);
}
.wfe-pal-tt small {
  font-size: 10px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wfe-pal-tip {
  font-size: 10.5px;
  color: var(--text-3);
  text-align: center;
  padding: 4px 0 2px;
}

/* ── 画布 ── */
.wfe-canvas {
  position: relative;
  flex: 1;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  background:
    radial-gradient(ellipse at 30% 20%, color-mix(in srgb, var(--primary) 7%, transparent), transparent 55%),
    radial-gradient(ellipse at 75% 80%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 55%),
    var(--graph-bg-start);
}
.wfe-flow {
  width: 100%;
  height: 100%;
}
.wfe-toolbar {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 5;
  display: flex;
  gap: 6px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 6px;
  box-shadow: var(--shadow-sm);
}
.wfe-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-3);
  pointer-events: none;
}
.wfe-empty p {
  font-size: 13px;
}

/* ── 配置面板 ── */
.wfe-panel {
  width: 292px;
  flex-shrink: 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.wfe-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text);
}
.wfe-panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.wfe-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.wfe-field label {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--text-2);
}
.wfe-hint {
  font-size: 11px;
  color: var(--text-3);
  background: var(--grad-soft);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px 9px;
  line-height: 1.5;
}
.wfe-del {
  margin-top: auto;
}
.mono {
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 11.5px;
}
.exec-result {
  background: #1d2930;
  color: #e2e8f0;
  padding: 14px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.7;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 1100px) {
  .wfe-palette {
    width: 168px;
  }
  .wfe-panel {
    width: 250px;
  }
}
@media (max-width: 860px) {
  .wfe-body {
    flex-direction: column;
  }
  .wfe-palette {
    width: 100%;
    flex-direction: row;
    flex-wrap: wrap;
  }
  .wfe-pal-title,
  .wfe-pal-tip {
    display: none;
  }
  .wfe-panel {
    width: 100%;
    max-height: 300px;
  }
}
</style>
