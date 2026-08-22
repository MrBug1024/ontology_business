<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>仪表盘</h1>
        <div class="sub">{{ selectedScenario ? `${selectedScenario.name} · 场景总览` : '业务场景本体智能平台 · 总览' }}</div>
      </div>
      <div class="dashboard-actions">
        <el-select
          v-if="scenarios.length"
          :model-value="activeScenarioId"
          class="scenario-select"
          filterable
          aria-label="切换仪表盘业务场景"
          placeholder="选择业务场景"
          @change="selectScenario"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button @click="load" :loading="loading">
          <el-icon v-if="!loading"><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="16">
      <el-col :xs="12" :sm="12" :md="6" v-for="s in stats" :key="s.label">
        <div class="stat-card" role="link" tabindex="0" :aria-label="`查看${s.label}`" @click="s.to && $router.push(s.to)" @keydown.enter.prevent="s.to && $router.push(s.to)" @keydown.space.prevent="s.to && $router.push(s.to)">
          <div class="stat-icon" :style="{ background: s.bg, color: s.fg }" aria-hidden="true">
            <el-icon :size="22"><component :is="s.icon" /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-num">
              <span v-if="loading" class="skeleton-num"></span>
              <template v-else>{{ s.value }}</template>
            </div>
            <div class="stat-label">{{ s.label }}</div>
          </div>
          <el-icon class="stat-arrow"><ArrowRight /></el-icon>
        </div>
      </el-col>
    </el-row>

    <section class="journey-card card mt-4" aria-labelledby="journey-title">
      <div class="journey-head">
        <div>
          <span class="journey-eyebrow">RECOMMENDED NEXT STEP</span>
          <h3 id="journey-title">{{ recommendedStep.title }}</h3>
          <p>{{ recommendedStep.description }}</p>
        </div>
        <el-button type="primary" @click="$router.push(recommendedStep.to)">
          {{ recommendedStep.action }} <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        </el-button>
      </div>
      <ol class="journey-track" aria-label="平台建设流程">
        <li
          v-for="(step, index) in journeySteps"
          :key="step.key"
          :class="{ complete: step.complete, current: step.key === recommendedStep.key }"
        >
          <button type="button" @click="$router.push(step.to)" :aria-current="step.key === recommendedStep.key ? 'step' : undefined">
            <span class="journey-index" aria-hidden="true"><el-icon v-if="step.complete"><Check /></el-icon><template v-else>{{ index + 1 }}</template></span>
            <span><b>{{ step.title }}</b><small>{{ step.summary }}</small></span>
          </button>
        </li>
      </ol>
    </section>

    <el-row :gutter="16" class="mt-4">
      <el-col :xs="24" :md="14">
        <div class="card">
          <div class="card-title">
            <el-icon><OfficeBuilding /></el-icon> 业务场景
            <el-button class="card-more" size="small" text type="primary" @click="$router.push(scopedTarget('scenarios'))">全部</el-button>
          </div>
          <el-table v-loading="loading" :data="scenarios" size="small">
            <el-table-column prop="name" label="名称" min-width="140">
              <template #default="{ row }">
                <button class="table-link cell-main" type="button" @click="$router.push(scenarioDetailTarget(row.id))">{{ row.name }}</button>
                <div class="cell-sub">{{ row.industry || '通用' }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="entity_count" label="实体" width="70" align="center" />
            <el-table-column prop="relation_count" label="关系" width="70" align="center" />
            <el-table-column prop="data_source_count" label="数据源" width="80" align="center" />
            <el-table-column label="" width="50" align="center">
              <template #default>
                <el-icon class="row-arrow"><ArrowRight /></el-icon>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!loading && !scenarios.length" class="empty-wrap">
            <div class="empty-icon"><el-icon :size="28"><OfficeBuilding /></el-icon></div>
            <div>暂无业务场景</div>
            <el-button type="primary" size="small" @click="$router.push(scopedTarget('scenarios'))">去创建</el-button>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :md="10">
        <div class="card">
          <div class="card-title">
            <el-icon><Cpu /></el-icon> 当前场景 Agent
            <el-button class="card-more" size="small" text type="primary" @click="$router.push(scopedTarget('agents'))">全部</el-button>
          </div>
          <el-table v-loading="loading" :data="agents" size="small">
            <el-table-column prop="name" label="名称" min-width="120">
              <template #default="{ row }">
                <button class="table-link cell-main" type="button" @click="$router.push(agentChatTarget(row.id))">{{ row.name }}</button>
                <div class="cell-sub">{{ row.scenario_name || '未绑定场景' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="已绑定资源" min-width="180">
              <template #default="{ row }">
                <div class="cap-tags">
                  <el-tooltip
                    v-for="tag in agentCapabilityTags(row)"
                    :key="tag.key"
                    :content="tag.detail"
                    placement="top"
                    :show-after="300"
                  >
                    <el-tag
                      class="cap-tag"
                      size="small"
                      :type="tag.type"
                      effect="light"
                      :aria-label="tag.detail"
                    ><span class="cap-tag-label">{{ tag.label }}</span></el-tag>
                  </el-tooltip>
                  <span class="muted" v-if="!agentCapabilityTags(row).length">未绑定模型或资源</span>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!loading && !agents.length" class="empty-wrap">
            <div class="empty-icon"><el-icon :size="28"><Cpu /></el-icon></div>
            <div>{{ activeScenarioId ? '当前场景暂无 Agent' : '暂无 Agent' }}</div>
            <el-button type="primary" size="small" @click="$router.push(scopedTarget('agents'))">去创建</el-button>
          </div>
        </div>
      </el-col>
    </el-row>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { RouteLocationRaw } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type {
  ActionExecutionLog,
  Agent,
  DataSource,
  ReleaseRecord,
  Scenario,
  ScenarioDetail,
  WorkflowRun,
} from '@/types'

const route = useRoute()
const router = useRouter()
const scenarios = ref<Scenario[]>([])
const activeScenarioId = ref('')
const scenarioDetail = ref<ScenarioDetail | null>(null)
const agents = ref<Agent[]>([])
const dataSources = ref<DataSource[]>([])
const tasks = ref<WorkflowRun[]>([])
const releaseRecords = ref<ReleaseRecord[]>([])
const executionLogs = ref<ActionExecutionLog[]>([])
const loading = ref(false)
let loadRequest = 0

const selectedScenario = computed(() => scenarios.value.find((scenario) => scenario.id === activeScenarioId.value) || null)

type AgentCapabilityTag = {
  key: string
  label: string
  detail: string
  type: 'primary' | 'success' | 'warning' | 'info'
}

function boundNames(names?: string[]) {
  return (names || []).map(name => name.trim()).filter(Boolean)
}

function summarizedBinding(
  key: string,
  label: string,
  names: string[] | undefined,
  type: AgentCapabilityTag['type'],
): AgentCapabilityTag | null {
  const bound = boundNames(names)
  if (!bound.length) return null
  const remaining = bound.length - 1
  return {
    key,
    label: `${label}·${bound[0]}${remaining ? ` +${remaining}` : ''}`,
    detail: `已绑定${label}：${bound.join('、')}`,
    type,
  }
}

/**
 * The dashboard is an inventory of bindings, not an authorization or action
 * surface.  In particular, skill/MCP bindings are labelled as configurations:
 * actual external operations still go through the permissioned Action/
 * Workflow path and any required approval.
 */
function agentCapabilityTags(agent: Agent): AgentCapabilityTag[] {
  const tags: AgentCapabilityTag[] = []
  const llmName = agent.llm_name?.trim()
  if (llmName) {
    tags.push({
      key: 'llm',
      label: `模型·${llmName}`,
      detail: `已绑定模型：${llmName}`,
      type: 'primary',
    })
  }

  const dataSource = summarizedBinding('data-source', '数据/资料源', agent.data_source_names, 'info')
  if (dataSource) tags.push(dataSource)

  const skills = summarizedBinding('skills', '技能配置', agent.skill_names, 'success')
  if (skills) tags.push(skills)

  const mcps = summarizedBinding('mcps', 'MCP 配置', agent.mcp_names, 'warning')
  if (mcps) tags.push(mcps)
  return tags
}

function routeScenarioId() {
  const value = route.query.scenario_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}

function scenarioQuery(id = activeScenarioId.value) {
  return id ? { scenario_id: id } : {}
}

function scopedTarget(name: string): RouteLocationRaw {
  return { name, query: scenarioQuery() }
}

function scenarioDetailTarget(id?: string | null): RouteLocationRaw {
  return id
    ? { name: 'scenario-detail', params: { id }, query: scenarioQuery(id) }
    : scopedTarget('scenarios')
}

function agentChatTarget(id?: string | null): RouteLocationRaw {
  return id
    ? { name: 'agent-chat', params: { id }, query: { ...scenarioQuery(), return_to: route.fullPath } }
    : scopedTarget('agents')
}

const readyAgents = computed(() => agents.value.filter((agent) => (
  agent.scenario_id === activeScenarioId.value && Boolean(agent.llm_config_id)
)))
const verifiedMappingCount = computed(() => scenarioDetail.value?.mappings.filter((mapping) => ['ready', 'ok'].includes(mapping.status || '')).length || 0)
const verifiedDataSourceCount = computed(() => dataSources.value.filter((source) => source.status === 'ok').length)
const hasVerifiedData = computed(() => verifiedMappingCount.value > 0 || verifiedDataSourceCount.value > 0)
const hasExecutableDefinition = computed(() => Boolean(
  scenarioDetail.value?.actions.some((action) => action.enabled !== false)
  || scenarioDetail.value?.workflows.some((workflow) => workflow.status === 'active' && workflow.enabled !== false),
))
const activeReleaseEnvironments = computed(() => new Set(
  releaseRecords.value.filter((record) => record.status === 'released').map((record) => record.environment),
))
const hasOperationalEvidence = computed(() => (
  tasks.value.some((task) => task.status === 'succeeded')
  || executionLogs.value.some((log) => ['success', 'succeeded'].includes(log.status))
))
const attentionTaskCount = computed(() => tasks.value.filter((task) => (
  ['awaiting_approval', 'retry_waiting', 'failed', 'timed_out'].includes(task.status)
)).length)

const stats = computed(() => [
  { label: '本体实体', value: scenarioDetail.value?.entities.length || 0, icon: 'OfficeBuilding', bg: 'var(--primary-soft)', fg: 'var(--primary-600)', to: scenarioDetailTarget(activeScenarioId.value) },
  { label: '可用 Agent', value: readyAgents.value.length, icon: 'Cpu', bg: 'var(--success-soft)', fg: 'var(--success)', to: scopedTarget('agents') },
  { label: '场景数据源', value: dataSources.value.length, icon: 'Coin', bg: 'var(--warning-soft)', fg: 'var(--warning)', to: scopedTarget('data-sources') },
  { label: '待审批 / 异常', value: attentionTaskCount.value, icon: 'WarningFilled', bg: 'var(--danger-soft)', fg: 'var(--danger)', to: scopedTarget('tasks') },
])

const journeySteps = computed(() => {
  const hasScenario = Boolean(activeScenarioId.value && selectedScenario.value)
  const entityCount = scenarioDetail.value?.entities.length || 0
  const relationCount = scenarioDetail.value?.relations.length || 0
  const hasOntology = hasScenario && entityCount > 0
  const hasReadyAgent = readyAgents.value.length > 0
  const hasReleased = activeReleaseEnvironments.value.size > 0
  return [
    {
      key: 'model', title: '定义业务场景', summary: hasOntology ? `${entityCount} 个实体 · ${relationCount} 类关系` : '描述目标并建立对象模型',
      description: hasScenario ? '继续完善对象、关系、规则与业务边界，让后续数据和动作都有明确语义。' : '先用一段业务描述创建场景，平台会引导你生成本体草稿并确认差异。',
      action: hasScenario ? '继续完善本体' : '创建第一个场景', to: hasScenario ? scenarioDetailTarget(activeScenarioId.value) : scopedTarget('scenarios'), complete: hasOntology,
    },
    {
      key: 'data', title: '接入并验证数据', summary: hasVerifiedData.value
        ? `${verifiedDataSourceCount.value} 个数据源已验证 · ${verifiedMappingCount.value} 个映射就绪`
        : dataSources.value.length ? `${dataSources.value.length} 个数据源待验证` : '连接数据并测试映射',
      description: '接入数据库或文档资料，先预览字段和来源，再测试映射与检索结果。',
      action: '进入数据接入', to: scopedTarget('data-sources'), complete: hasVerifiedData.value,
    },
    {
      key: 'agent', title: '编排 Agent 与动作', summary: hasReadyAgent && hasExecutableDefinition.value
        ? `${readyAgents.value.length} 个 Agent 与可执行编排已就绪`
        : hasReadyAgent ? 'Agent 已绑定，继续配置动作或流程' : '绑定场景、模型与受控工具',
      description: '将模型、数据、技能和受权限治理的 Action 组合成可运行的业务能力。',
      action: '配置智能能力', to: scopedTarget('agents'), complete: hasReadyAgent && hasExecutableDefinition.value,
    },
    {
      key: 'release', title: '校验并发布', summary: hasReleased ? `${activeReleaseEnvironments.value.size} 个环境已有有效发布` : '评审差异与环境就绪度',
      description: '执行发布前校验，检查差异、连接器与审批，再将冻结定义推广到目标环境。',
      action: '开始发布校验', to: scopedTarget('releases'), complete: hasReleased,
    },
    {
      key: 'operate', title: '运营、审批与复盘', summary: hasOperationalEvidence.value ? '已有成功运行与审计证据' : '处理任务、异常与审计证据',
      description: '从任务中心处理审批和异常，并沿血缘查看每次决策使用的数据、权限与执行结果。',
      action: '进入任务中心', to: scopedTarget('tasks'), complete: hasOperationalEvidence.value,
    },
  ]
})
const recommendedStep = computed(() => journeySteps.value.find((step) => !step.complete) || journeySteps.value[journeySteps.value.length - 1])

function resetScenarioEvidence() {
  scenarioDetail.value = null
  agents.value = []
  dataSources.value = []
  tasks.value = []
  releaseRecords.value = []
  executionLogs.value = []
}

async function selectScenario(value: string | number | boolean) {
  const id = String(value || '')
  if (!id || id === routeScenarioId()) return
  localStorage.setItem('ontology-active-scenario', id)
  await router.replace({ name: 'dashboard', query: { ...route.query, scenario_id: id } })
  return
}

async function load() {
  const request = ++loadRequest
  loading.value = true
  try {
    const scenarioRows = await api.listScenarios()
    if (request !== loadRequest) return
    scenarios.value = scenarioRows

    const requested = routeScenarioId()
    const stored = localStorage.getItem('ontology-active-scenario') || ''
    const nextScenarioId = scenarioRows.some((scenario) => scenario.id === requested)
      ? requested
      : scenarioRows.some((scenario) => scenario.id === stored)
        ? stored
        : scenarioRows[0]?.id || ''

    if (nextScenarioId !== activeScenarioId.value) resetScenarioEvidence()
    activeScenarioId.value = nextScenarioId
    if (nextScenarioId) localStorage.setItem('ontology-active-scenario', nextScenarioId)
    else localStorage.removeItem('ontology-active-scenario')

    if (requested !== nextScenarioId) {
      const query = { ...route.query }
      if (nextScenarioId) query.scenario_id = nextScenarioId
      else delete query.scenario_id
      await router.replace({ name: 'dashboard', query })
      return
    }
    if (!nextScenarioId) {
      resetScenarioEvidence()
      return
    }

    const [detailResult, agentResult, sourceResult, taskResult, recordResult, logResult] = await Promise.allSettled([
      api.getScenario(nextScenarioId),
      api.listAgents(),
      api.listDataSources(nextScenarioId),
      api.listTasks({ scenario_id: nextScenarioId, limit: 100 }),
      api.listReleaseRecords(nextScenarioId),
      api.listExecutionLogs(nextScenarioId, 50),
    ])
    if (request !== loadRequest || routeScenarioId() !== nextScenarioId) return
    if (detailResult.status === 'rejected') throw detailResult.reason
    scenarioDetail.value = detailResult.value
    agents.value = agentResult.status === 'fulfilled'
      ? agentResult.value.filter((agent) => agent.scenario_id === nextScenarioId)
      : []
    dataSources.value = sourceResult.status === 'fulfilled'
      ? sourceResult.value.filter((source) => source.scenario_id === nextScenarioId)
      : []
    tasks.value = taskResult.status === 'fulfilled'
      ? taskResult.value.filter((task) => task.scenario_id === nextScenarioId)
      : []
    releaseRecords.value = recordResult.status === 'fulfilled'
      ? recordResult.value.filter((record) => record.scenario_id === nextScenarioId)
      : []
    executionLogs.value = logResult.status === 'fulfilled'
      ? logResult.value.filter((log) => log.scenario_id === nextScenarioId)
      : []
    if ([agentResult, sourceResult, taskResult, recordResult, logResult].some((result) => result.status === 'rejected')) {
      ElMessage.warning('当前场景的部分治理证据暂不可读取；仪表盘仅展示已获授权且成功加载的数据。')
    }
  } catch (e: any) {
    if (request === loadRequest) ElMessage.error('加载失败：' + e.message)
  } finally {
    if (request === loadRequest) loading.value = false
  }
}

onMounted(() => { void load() })
onBeforeUnmount(() => {
  loadRequest += 1
})
</script>

<style scoped>
.mt-4 { margin-top: 16px; }
.dashboard-actions { display: flex; align-items: center; gap: 10px; }
.scenario-select { width: min(260px, 40vw); }

@media (max-width: 620px) {
  .page-header { align-items: stretch; flex-direction: column; }
  .dashboard-actions { width: 100%; }
  .scenario-select { flex: 1; width: auto; min-width: 0; }
  .dashboard-actions :deep(.el-button) { min-height: 44px; }
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  cursor: pointer;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 18px 20px;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
  position: relative;
  overflow: hidden;
}
.stat-card::before {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--grad-soft);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.stat-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.stat-card:hover::before { opacity: 1; }
.stat-card > * { position: relative; }

.stat-icon {
  width: 50px; height: 50px;
  border-radius: 13px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.stat-body { flex: 1; min-width: 0; }
.stat-num {
  font-size: 28px;
  font-weight: 800;
  line-height: 1.1;
  letter-spacing: -0.5px;
  color: var(--text);
}
.skeleton-num {
  display: inline-block;
  width: 44px; height: 26px;
  border-radius: 6px;
  background: linear-gradient(90deg, var(--surface-3) 25%, var(--border) 50%, var(--surface-3) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
}
@keyframes shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
.stat-label { color: var(--text-2); font-size: 13px; font-weight: 600; margin-top: 2px; white-space: nowrap; }
.stat-arrow {
  color: var(--text-3);
  transition: transform var(--dur) var(--ease), color var(--dur);
}
.stat-card:hover .stat-arrow {
  transform: translateX(3px);
  color: var(--primary);
}
.stat-card:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }

@media (max-width: 768px) {
  .stat-card { padding: 14px 14px; gap: 10px; }
  .stat-icon { width: 40px; height: 40px; border-radius: 11px; }
  .stat-num { font-size: 22px; }
  .stat-label { font-size: 12px; }
  .stat-arrow { display: none; }
}

.card-more {
  margin-left: auto;
}
.cell-main { font-weight: 700; color: var(--text); }
.table-link { display: block; max-width: 100%; padding: 4px 0; overflow: hidden; border: 0; background: transparent; font: inherit; text-align: left; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.table-link:hover { color: var(--primary-600); text-decoration: underline; text-underline-offset: 3px; }
.cell-sub { font-size: 12px; color: var(--text-3); margin-top: 1px; }
.row-arrow { color: var(--text-3); }
.cap-tags { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.cap-tag { max-width: 100%; min-width: 0; }
.cap-tag-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.journey-card { overflow: hidden; padding: 0; }
.journey-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px 22px; background: linear-gradient(135deg, color-mix(in srgb, var(--primary-soft) 78%, var(--surface)), var(--surface)); }
.journey-head > div { min-width: 0; }
.journey-eyebrow { color: var(--primary-600); font-size: 10px; font-weight: 800; letter-spacing: .11em; }
.journey-head h3 { margin: 4px 0 3px; color: var(--text); font-size: 19px; letter-spacing: -.02em; }
.journey-head p { max-width: 760px; margin: 0; color: var(--text-2); font-size: 12.5px; line-height: 1.55; }
.journey-track { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin: 0; padding: 0; border-top: 1px solid var(--border); list-style: none; }
.journey-track li { position: relative; min-width: 0; }
.journey-track li:not(:last-child)::after { position: absolute; top: 27px; right: -5px; z-index: 1; width: 10px; height: 1px; background: var(--border-strong); content: ''; }
.journey-track button { display: flex; width: 100%; min-height: 72px; align-items: center; gap: 8px; padding: 12px; border: 0; background: var(--surface); color: var(--text-2); text-align: left; cursor: pointer; transition: background var(--dur) var(--ease); }
.journey-track button:hover { background: var(--surface-2); }
.journey-index { display: inline-flex; flex: 0 0 27px; width: 27px; height: 27px; align-items: center; justify-content: center; border: 1px solid var(--border-strong); border-radius: 50%; background: var(--surface); color: var(--text-3); font-size: 10px; font-weight: 800; }
.journey-track button > span:last-child { display: flex; min-width: 0; flex-direction: column; }
.journey-track b { overflow: hidden; color: var(--text); font-size: 11.5px; text-overflow: ellipsis; white-space: nowrap; }
.journey-track small { margin-top: 2px; overflow: hidden; color: var(--text-3); font-size: 9.5px; text-overflow: ellipsis; white-space: nowrap; }
.journey-track li.complete .journey-index { border-color: color-mix(in srgb, var(--success) 45%, var(--border)); background: var(--success-soft); color: var(--success); }
.journey-track li.current button { background: var(--primary-soft); }
.journey-track li.current .journey-index { border-color: var(--primary); background: var(--primary); color: #fff; }
@media (max-width: 900px) {
  .journey-track { grid-template-columns: 1fr; }
  .journey-track li:not(:last-child)::after { top: auto; right: auto; bottom: -5px; left: 25px; width: 1px; height: 10px; }
  .journey-track button { min-height: 60px; }
}
@media (max-width: 620px) {
  .journey-head { align-items: stretch; flex-direction: column; }
  .journey-head :deep(.el-button) { min-height: 44px; }
}
</style>
