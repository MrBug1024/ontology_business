<template>
  <main class="lineage-page" aria-labelledby="lineage-title">
    <header class="lineage-header">
      <div>
        <span class="eyebrow">TRACEABILITY</span>
        <h1 id="lineage-title">端到端血缘</h1>
        <p>从数据源、本体对象和 AI 回答，一直追到 Action 与外部执行结果。</p>
      </div>
      <div class="header-actions">
        <el-select v-model="scenarioId" class="scenario-select" aria-label="选择业务场景" placeholder="选择业务场景" @change="loadGraph">
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button :loading="loading" @click="loadGraph"><el-icon><Refresh /></el-icon> 刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      class="lineage-alert"
      type="error"
      :title="error"
      show-icon
      :closable="false"
      role="alert"
    />
    <el-alert
      v-if="graph?.truncated"
      class="lineage-alert"
      type="warning"
      title="血缘图已按安全上限截断；请在场景内缩小资料、对象或运行记录范围后再次查看。"
      show-icon
      :closable="false"
    />

    <template v-if="graph">
      <section class="summary-grid" aria-label="血缘概览" aria-live="polite">
        <button class="summary-card summary-card--source" type="button" @click="activeKind = 'data_source'">
          <span class="summary-icon"><el-icon><Coin /></el-icon></span>
          <span><b>{{ graph.summary.data_sources }}</b><small>数据源</small></span>
        </button>
        <button class="summary-card summary-card--object" type="button" @click="activeKind = 'object'">
          <span class="summary-icon"><el-icon><Box /></el-icon></span>
          <span><b>{{ graph.summary.objects }}</b><small>本体对象</small></span>
        </button>
        <button class="summary-card summary-card--ai" type="button" @click="activeKind = 'ai_answer'">
          <span class="summary-icon"><el-icon><Cpu /></el-icon></span>
          <span><b>{{ graph.summary.ai_answers }}</b><small>AI 回答</small></span>
        </button>
        <button class="summary-card summary-card--action" type="button" @click="activeKind = 'action_execution'">
          <span class="summary-icon"><el-icon><Connection /></el-icon></span>
          <span><b>{{ graph.summary.action_executions }}</b><small>Action 执行</small></span>
        </button>
      </section>

      <section class="lineage-toolbar" aria-label="血缘筛选">
        <el-select v-model="activeKind" clearable placeholder="全部节点类型" aria-label="按节点类型筛选">
          <el-option v-for="option in kindOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-input v-model="query" clearable placeholder="搜索节点、来源或执行状态" aria-label="搜索血缘节点" />
        <span class="result-count">显示 {{ visibleEdges.length }} / {{ graph.edges.length }} 条关系</span>
      </section>

      <section class="lineage-layout">
        <section class="edge-panel card" aria-labelledby="edge-title">
          <div class="section-head">
            <div>
              <span class="eyebrow">LINEAGE PATHS</span>
              <h3 id="edge-title">可追溯链路</h3>
            </div>
            <p>选择任一链路查看安全元数据；不会显示原始业务参数或外部响应正文。</p>
          </div>

          <div v-if="!visibleEdges.length" class="empty-state">
            <el-icon :size="28"><Share /></el-icon>
            <strong>当前筛选下没有血缘关系</strong>
            <span>可清除筛选，或先运行映射、检索、Agent 对话或工作流。</span>
          </div>
          <ol v-else class="edge-list" aria-label="血缘关系列表">
            <li v-for="edge in visibleEdges" :key="edge.id">
              <button class="edge-row" type="button" :class="{ active: selectedEdge?.id === edge.id }" @click="selectEdge(edge)">
                <span class="node-chip" :class="`kind-${sourceNode(edge)?.kind || 'unknown'}`">{{ sourceNode(edge)?.label || edge.source }}</span>
                <span class="edge-arrow" aria-hidden="true"><el-icon><Right /></el-icon></span>
                <span class="edge-label">{{ edge.label || edge.kind }}</span>
                <span class="edge-arrow" aria-hidden="true"><el-icon><Right /></el-icon></span>
                <span class="node-chip" :class="`kind-${targetNode(edge)?.kind || 'unknown'}`">{{ targetNode(edge)?.label || edge.target }}</span>
              </button>
            </li>
          </ol>
        </section>

        <aside class="detail-panel card" aria-labelledby="detail-title">
          <div class="section-head">
            <div>
              <span class="eyebrow">INSPECT</span>
              <h3 id="detail-title">链路详情</h3>
            </div>
          </div>
          <div v-if="selectedEdge" class="detail-content">
            <div class="detail-path">
              <strong>{{ sourceNode(selectedEdge)?.label || selectedEdge.source }}</strong>
              <span>{{ selectedEdge.label || selectedEdge.kind }}</span>
              <strong>{{ targetNode(selectedEdge)?.label || selectedEdge.target }}</strong>
            </div>
            <dl>
              <div><dt>关系</dt><dd>{{ selectedEdge.kind }}</dd></div>
              <div><dt>来源类型</dt><dd>{{ kindLabel(sourceNode(selectedEdge)?.kind || '') }}</dd></div>
              <div><dt>目标类型</dt><dd>{{ kindLabel(targetNode(selectedEdge)?.kind || '') }}</dd></div>
              <template v-for="item in safeMeta(selectedEdge)" :key="item.key">
                <div><dt>{{ item.key }}</dt><dd>{{ item.value }}</dd></div>
              </template>
            </dl>
          </div>
          <div v-else class="detail-empty">
            <el-icon :size="28"><View /></el-icon>
            <p>从左侧选择一条关系以查看它的审计信息。</p>
          </div>
        </aside>
      </section>
    </template>

    <div v-else-if="!loading" class="empty-state card">
      <el-icon :size="30"><Share /></el-icon>
      <strong>选择业务场景以查看血缘</strong>
      <span>系统会在数据映射、文档引用、Agent 和 Action 运行后自动形成链路。</span>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { LineageEdge, LineageGraph, LineageNode, Scenario } from '@/types'

const route = useRoute()
const router = useRouter()
const scenarios = ref<Scenario[]>([])
const scenarioId = ref(typeof route.query.scenario_id === 'string' ? route.query.scenario_id : '')
const graph = ref<LineageGraph | null>(null)
const loading = ref(false)
const error = ref('')
const activeKind = ref('')
const query = ref('')
const selectedEdge = ref<LineageEdge | null>(null)

const KIND_LABELS: Record<string, string> = {
  data_source: '数据源', mapping: '数据映射', object: '本体对象', document: '业务文档', document_chunk: '文档片段',
  ai_answer: 'AI 回答', action: 'Action', workflow: '工作流', action_execution: 'Action 执行', external_result: '外部结果', workflow_run: '工作流运行',
}
const kindOptions = Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label }))
const nodeMap = computed(() => new Map((graph.value?.nodes || []).map((node) => [node.id, node])))
const normalizedQuery = computed(() => query.value.trim().toLocaleLowerCase())
const visibleEdges = computed(() => (graph.value?.edges || []).filter((edge) => {
  const source = sourceNode(edge)
  const target = targetNode(edge)
  const matchesKind = !activeKind.value || source?.kind === activeKind.value || target?.kind === activeKind.value
  if (!matchesKind) return false
  const haystack = [source?.label, target?.label, edge.kind, edge.label].filter(Boolean).join(' ').toLocaleLowerCase()
  return !normalizedQuery.value || haystack.includes(normalizedQuery.value)
}))

function sourceNode(edge: LineageEdge): LineageNode | undefined { return nodeMap.value.get(edge.source) }
function targetNode(edge: LineageEdge): LineageNode | undefined { return nodeMap.value.get(edge.target) }
function kindLabel(kind: string) { return KIND_LABELS[kind] || kind || '未知' }
function selectEdge(edge: LineageEdge) { selectedEdge.value = edge }
function safeMeta(edge: LineageEdge) {
  return Object.entries(edge.meta || {})
    .filter(([key, value]) => !/content|input|result|token|secret|key/i.test(key) && value != null && value !== '')
    .slice(0, 8)
    .map(([key, value]) => ({ key, value: typeof value === 'object' ? JSON.stringify(value) : String(value) }))
}

async function loadScenarios() {
  try {
    scenarios.value = await api.listScenarios()
    // 深链接可能指向已经删除、无权访问或来自另一环境的场景。先收敛到当前
    // 可见列表，避免选择框显示一个场景却持续请求一个失效 ID。
    if (!scenarios.value.some((scenario) => scenario.id === scenarioId.value)) {
      scenarioId.value = scenarios.value[0]?.id || ''
      if (scenarioId.value) {
        await router.replace({ name: 'lineage', query: { scenario_id: scenarioId.value } })
      } else {
        await router.replace({ name: 'lineage' })
      }
    }
    if (scenarioId.value) await loadGraph()
  } catch (cause: any) {
    error.value = cause?.message || '业务场景加载失败'
  }
}
async function loadGraph() {
  if (!scenarioId.value) return
  loading.value = true
  error.value = ''
  try {
    graph.value = await api.getScenarioLineage(scenarioId.value)
    selectedEdge.value = graph.value.edges[0] || null
    await router.replace({ name: 'lineage', query: { scenario_id: scenarioId.value } })
  } catch (cause: any) {
    graph.value = null
    error.value = cause?.message || '血缘图加载失败'
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

watch(activeKind, () => {
  if (selectedEdge.value && !visibleEdges.value.some((edge) => edge.id === selectedEdge.value?.id)) selectedEdge.value = visibleEdges.value[0] || null
})
onMounted(loadScenarios)
</script>

<style scoped>
.lineage-page { min-height: 100%; padding: 24px 28px 34px; }
.lineage-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 18px; }
.eyebrow { color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .15em; }
.lineage-header h1 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.lineage-header p { margin: 0; color: var(--text-2); font-size: 13px; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.scenario-select { width: min(280px, 48vw); }
.lineage-alert { margin-bottom: 12px; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.summary-card { min-height: 82px; display: flex; align-items: center; gap: 11px; padding: 13px; border: 1px solid var(--border); border-radius: 14px; color: var(--text); background: var(--surface); text-align: left; cursor: pointer; box-shadow: var(--shadow-xs); transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.summary-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.summary-card:focus-visible, .edge-row:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }
.summary-icon { width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center; border-radius: 11px; font-size: 18px; }
.summary-card span:last-child { display: flex; flex-direction: column; gap: 2px; }
.summary-card b { font-size: 21px; line-height: 1.1; }
.summary-card small { color: var(--text-2); font-size: 11px; font-weight: 650; }
.summary-card--source .summary-icon { background: var(--primary-soft); color: var(--primary); }
.summary-card--object .summary-icon { background: var(--success-soft); color: var(--success); }
.summary-card--ai .summary-icon { background: var(--info-soft); color: var(--info); }
.summary-card--action .summary-icon { background: var(--warning-soft); color: var(--warning); }
.lineage-toolbar { display: grid; grid-template-columns: minmax(160px, 220px) minmax(200px, 1fr) auto; align-items: center; gap: 10px; margin-bottom: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.result-count { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.lineage-layout { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(280px, .72fr); align-items: start; gap: 14px; }
.card { border: 1px solid var(--border); border-radius: 15px; background: var(--surface); box-shadow: var(--shadow-xs); }
.edge-panel, .detail-panel { min-height: 420px; padding: 17px; }
.section-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 14px; }
.section-head h3 { margin: 4px 0 0; color: var(--text); font-size: 16px; }
.section-head p { max-width: 380px; margin: 2px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.55; }
.edge-list { display: flex; flex-direction: column; gap: 7px; margin: 0; padding: 0; list-style: none; }
.edge-row { width: 100%; min-height: 48px; display: grid; grid-template-columns: minmax(90px, 1fr) 18px minmax(66px, .6fr) 18px minmax(90px, 1fr); align-items: center; gap: 5px; padding: 7px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); color: var(--text-2); cursor: pointer; text-align: left; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease), transform var(--dur) var(--ease); }
.edge-row:hover, .edge-row.active { border-color: color-mix(in srgb, var(--primary) 38%, var(--border)); background: var(--primary-soft); }
.edge-row:hover { transform: translateX(2px); }
.node-chip { min-width: 0; overflow: hidden; color: var(--text); font-size: 12px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.edge-arrow { display: inline-flex; justify-content: center; color: var(--text-3); font-size: 13px; }
.edge-label { min-width: 0; overflow: hidden; color: var(--text-3); font-size: 11px; text-align: center; text-overflow: ellipsis; white-space: nowrap; }
.kind-ai_answer, .kind-document_chunk { color: var(--info); }
.kind-action_execution, .kind-external_result { color: var(--warning); }
.kind-data_source, .kind-mapping { color: var(--primary); }
.kind-object { color: var(--success); }
.detail-content { display: flex; flex-direction: column; gap: 16px; }
.detail-path { display: flex; flex-direction: column; gap: 5px; padding: 12px; border-radius: 11px; background: var(--surface-2); }
.detail-path strong { color: var(--text); font-size: 13px; overflow-wrap: anywhere; }
.detail-path span { color: var(--primary); font-size: 12px; font-weight: 650; }
.detail-content dl { display: grid; gap: 8px; margin: 0; }
.detail-content dl div { padding: 9px 10px; border: 1px solid var(--border); border-radius: 9px; }
.detail-content dt { color: var(--text-3); font-size: 10px; }
.detail-content dd { margin: 3px 0 0; color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
.empty-state, .detail-empty { min-height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 9px; padding: 24px; color: var(--text-3); text-align: center; }
.empty-state strong { color: var(--text-2); font-size: 14px; }
.empty-state span, .detail-empty p { margin: 0; font-size: 12px; line-height: 1.55; }
@media (max-width: 1050px) { .lineage-layout { grid-template-columns: 1fr; } .detail-panel { min-height: auto; } }
@media (max-width: 760px) { .lineage-page { padding: 18px 14px 24px; } .lineage-header { flex-direction: column; } .header-actions { width: 100%; } .scenario-select { flex: 1; max-width: none; } .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .lineage-toolbar { grid-template-columns: 1fr; } .result-count { white-space: normal; } .section-head { flex-direction: column; gap: 5px; } }
@media (max-width: 460px) { .summary-grid { grid-template-columns: 1fr; } .edge-row { grid-template-columns: minmax(0, 1fr) 16px; } .edge-row .edge-label { grid-column: 1 / -1; text-align: left; padding-left: 4px; } .edge-row .node-chip:last-child { grid-column: 1 / 2; } .edge-row .edge-arrow:nth-of-type(2) { display: none; } }
@media (prefers-reduced-motion: reduce) { .summary-card, .edge-row { transition: none; } .summary-card:hover, .edge-row:hover { transform: none; } }
</style>
