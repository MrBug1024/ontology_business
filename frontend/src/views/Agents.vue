<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>Agent 管理</h1>
        <div class="sub">绑定业务场景、大模型与业务数据，完成就绪检查后进入对话</div>
      </div>
      <div class="agent-header-actions">
        <el-select
          v-model="scenarioScope"
          clearable
          filterable
          aria-label="按业务场景筛选 Agent"
          placeholder="全部业务场景"
          @change="changeScenarioScope"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建 Agent</el-button>
      </div>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="a in agents" :key="a.id">
        <article class="card agent-card">
          <div class="ag-head">
            <div class="ag-avatar"><el-icon :size="20"><Cpu /></el-icon></div>
            <div class="ag-title">
              <div class="ag-name">{{ a.name }}</div>
              <div class="muted">{{ a.scenario_name || '未绑定场景' }}</div>
            </div>
            <el-tag size="small" effect="plain" :type="agentReady(a) ? 'success' : 'warning'">{{ agentReady(a) ? '可对话' : '待配置' }}</el-tag>
          </div>
          <div class="ag-desc">{{ a.description || '暂无描述' }}</div>
          <div class="ag-tags">
            <el-tag v-if="a.llm_name" size="small" type="primary" effect="light"><el-icon aria-hidden="true"><ChatDotRound /></el-icon>{{ a.llm_name }}</el-tag>
            <el-tag v-for="n in a.data_source_names || []" :key="n" size="small" type="info" effect="light"><el-icon aria-hidden="true"><Coin /></el-icon>{{ n }}</el-tag>
            <span class="muted" v-if="!(a.llm_name || a.data_source_names?.length)">未配置模型与数据</span>
          </div>
          <div class="agent-readiness" :class="{ ready: agentReady(a) }">
            <span :class="{ done: agentReadiness(a).ontology }"><el-icon><component :is="agentReadiness(a).ontology ? 'CircleCheck' : 'Warning'" /></el-icon>本体</span>
            <span :class="{ done: agentReadiness(a).source }"><el-icon><component :is="agentReadiness(a).source ? 'CircleCheck' : 'Warning'" /></el-icon>数据源</span>
            <span :class="{ done: agentReadiness(a).mapping }"><el-icon><component :is="agentReadiness(a).mapping ? 'CircleCheck' : 'Warning'" /></el-icon>映射</span>
            <span :class="{ done: agentReadiness(a).model }"><el-icon><component :is="agentReadiness(a).model ? 'CircleCheck' : 'Warning'" /></el-icon>模型</span>
            <span :class="{ done: agentReadiness(a).dataBinding }"><el-icon><component :is="agentReadiness(a).dataBinding ? 'CircleCheck' : 'Warning'" /></el-icon>业务数据</span>
          </div>
          <div class="ag-actions">
            <el-button v-if="agentReady(a)" size="small" type="primary" @click="openAgentChat(a)"><el-icon><ChatDotRound /></el-icon> 进入对话</el-button>
            <el-button v-else size="small" plain type="warning" @click="continueSetup(a)"><el-icon><Position /></el-icon> 继续建设</el-button>
            <el-button size="small" :type="agentReady(a) ? 'primary' : 'warning'" :text="agentReady(a)" :plain="!agentReady(a)" @click="openEdit(a)">
              <el-icon><Setting /></el-icon> {{ agentReady(a) ? '配置' : '补齐配置' }}
            </el-button>
            <el-button size="small" text type="danger" @click="remove(a)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </article>
      </el-col>
    </el-row>
    <div v-if="!loading && !agents.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><Cpu /></el-icon></div>
      <div>暂无 Agent，点击右上角「新建 Agent」开始</div>
      <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建 Agent</el-button>
    </div>

    <!-- 编辑对话框 -->
    <el-dialog v-model="dlg" :title="form.id ? '编辑 Agent' : '新建 Agent'" width="760px" top="5vh">
      <el-form :model="form" label-width="100px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：业务分析助手" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="描述"><el-input v-model="form.description" placeholder="Agent 用途说明" /></el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="业务场景">
              <el-select v-model="form.scenario_id" clearable placeholder="绑定场景（注入本体上下文）" style="width:100%">
                <el-option v-for="s in scenarios" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="大模型">
              <el-select v-model="form.llm_config_id" clearable placeholder="选择大模型" style="width:100%">
                <el-option v-for="l in llms" :key="l.id" :label="`${l.name}（${l.model}）`" :value="l.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="系统提示词">
          <el-input v-model="form.system_prompt" type="textarea" :rows="3"
            placeholder="可选。留空则使用平台默认提示词（包含本体、映射数据和场景能力摘要）" />
        </el-form-item>

        <el-form-item label="可用数据">
          <el-select v-model="form.data_source_ids" multiple placeholder="选择映射数据及需要检索的文档资料库" style="width:100%">
            <el-option v-for="d in availableDataSources" :key="d.id" :label="d.name" :value="d.id">
              <span>{{ d.name }}</span>
              <span class="muted" style="float:right">{{ sourceBindingHint(d) }}</span>
            </el-option>
          </el-select>
        </el-form-item>

        <el-alert
          v-if="form.scenario_id"
          :type="formAgentReady ? 'success' : 'warning'"
          :closable="false"
          show-icon
          class="setup-alert"
          :title="formAgentReady ? 'Agent 已具备进入对话的条件' : `尚缺：${formAgentMissing.join('、')}`"
        >
          <template #default>
            <span v-if="!formAgentReady">可以先保存草稿；补齐以上项目后才能进入对话。</span>
          </template>
        </el-alert>
      </el-form>
      <template #footer>
        <el-button @click="dlg=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { Agent, Scenario, ScenarioDetail, LLMConfig, DataSource } from '@/types'

const agents = ref<Agent[]>([])
const scenarios = ref<Scenario[]>([])
const llms = ref<LLMConfig[]>([])
const dataSources = ref<DataSource[]>([])
const scenarioDetails = ref<Record<string, ScenarioDetail>>({})
const route = useRoute()
const router = useRouter()
const queryScenarioId = () => {
  const value = route.query.scenario_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
const scenarioScope = ref(queryScenarioId())

const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<Agent>>({ data_source_ids: [] })
let viewDisposed = false
let loadRequest = 0
const formMappedSourceIds = computed(() => new Set(
  (form.value.scenario_id ? scenarioDetails.value[form.value.scenario_id]?.mappings : [])
    ?.map((mapping) => mapping.data_source_id) || [],
))
const availableDataSources = computed(() => {
  const scenarioId = form.value.scenario_id
  return dataSources.value.filter((source) => {
    const belongsToScenario = !scenarioId || !source.scenario_id || source.scenario_id === scenarioId
    const hasMapping = Boolean(source.id && formMappedSourceIds.value.has(source.id))
    return belongsToScenario && (hasMapping || source.type === 'file_bucket')
  })
})
function sourceBindingHint(source: DataSource) {
  return source.id && formMappedSourceIds.value.has(source.id) ? '已映射' : '文档检索'
}
function scenarioSetup(scenarioId?: string | null) {
  const detail = scenarioId ? scenarioDetails.value[scenarioId] : undefined
  const ontology = Boolean(detail?.entities?.length)
  const source = Boolean(detail?.data_sources?.length || detail?.mappings?.some((mapping) => mapping.data_source_id))
  const mapping = Boolean(detail?.mappings?.length)
  return { ontology, source, mapping }
}
function agentReadiness(agent: Partial<Agent>) {
  const setup = scenarioSetup(agent.scenario_id)
  const mappedSourceIds = new Set(
    (agent.scenario_id ? scenarioDetails.value[agent.scenario_id]?.mappings : [])
      ?.map((mapping) => mapping.data_source_id) || [],
  )
  const dataBinding = Boolean(agent.data_source_ids?.some((id) => mappedSourceIds.has(id)))
  const model = Boolean(agent.llm_config_id)
  return { ...setup, model, dataBinding, ready: setup.ontology && setup.source && setup.mapping && model && dataBinding }
}
function agentReady(agent: Partial<Agent>) {
  return agentReadiness(agent).ready
}
const formScenarioMissing = computed(() => {
  const setup = scenarioSetup(form.value.scenario_id)
  const missing: string[] = []
  if (!setup.ontology) missing.push('对象类型')
  if (!setup.source) missing.push('数据源')
  if (!setup.mapping) missing.push('数据映射')
  return missing
})
const formAgentMissing = computed(() => {
  const missing = [...formScenarioMissing.value]
  if (!form.value.llm_config_id) missing.push('大模型')
  if (!(form.value.data_source_ids || []).some((id) => formMappedSourceIds.value.has(id))) missing.push('映射数据')
  return missing
})
const formAgentReady = computed(() => formAgentMissing.value.length === 0)
async function ensureScenarioDetail(scenarioId?: string | null) {
  if (!scenarioId || scenarioDetails.value[scenarioId]) return
  try {
    const detail = await api.getScenario(scenarioId)
    if (!viewDisposed) scenarioDetails.value = { ...scenarioDetails.value, [detail.id]: detail }
  } catch {
    // 场景详情的访问错误由就绪提示体现，不阻断 Agent 草稿编辑。
  }
}

async function load() {
  const request = ++loadRequest
  const scope = scenarioScope.value
  loading.value = true
  try {
    const [ag, sc, ll, ds] = await Promise.all([
      api.listAgents(), api.listScenarios(), api.listLLM(), api.listDataSources(),
    ])
    const detailIds = [...new Set(ag.map((agent) => agent.scenario_id).filter((id): id is string => Boolean(id)))]
    const details = await Promise.allSettled(detailIds.map((id) => api.getScenario(id)))
    if (viewDisposed || request !== loadRequest || scope !== scenarioScope.value) return
    agents.value = scope ? ag.filter((agent) => agent.scenario_id === scope) : ag
    scenarios.value = sc
    llms.value = ll
    dataSources.value = ds
    scenarioDetails.value = Object.fromEntries(
      details.flatMap((result) => result.status === 'fulfilled' ? [[result.value.id, result.value] as const] : []),
    )
  } catch (e: any) {
    if (!viewDisposed && request === loadRequest) ElMessage.error('加载失败：' + e.message)
  } finally {
    if (!viewDisposed && request === loadRequest) loading.value = false
  }
}
function openCreate() {
  form.value = {
    name: '',
    description: '',
    scenario_id: scenarioScope.value || undefined,
    data_source_ids: [],
  }
  dlg.value = true
}
function openEdit(a: Agent) {
  form.value = { ...a, data_source_ids: [...(a.data_source_ids || [])] }
  dlg.value = true
}
function openAgentChat(agent: Agent) {
  if (!agent.id) return
  void router.push({
    name: 'agent-chat',
    params: { id: agent.id },
    query: {
      scenario_id: agent.scenario_id || scenarioScope.value || undefined,
      return_to: route.fullPath,
    },
  })
}
function continueSetup(agent: Agent) {
  const setup = scenarioSetup(agent.scenario_id)
  if (!agent.scenario_id) return openEdit(agent)
  if (!setup.ontology) {
    void router.push({ name: 'scenario-detail', params: { id: agent.scenario_id }, query: { stage: 'ontology', return_to: route.fullPath } })
    return
  }
  if (!setup.source) {
    void router.push({ name: 'data-sources', query: { scenario_id: agent.scenario_id, return_to: route.fullPath } })
    return
  }
  if (!setup.mapping) {
    void router.push({ name: 'scenario-detail', params: { id: agent.scenario_id }, query: { stage: 'mappings', return_to: route.fullPath } })
    return
  }
  openEdit(agent)
}
async function save() {
  if (!form.value.name) return ElMessage.warning('请填写名称')
  saving.value = true
  try {
    if (form.value.id) await api.updateAgent(form.value.id, form.value)
    else await api.createAgent(form.value)
    ElMessage.success('已保存')
    dlg.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function remove(a: Agent) {
  try {
    await ElMessageBox.confirm(`删除 Agent「${a.name}」？`, '确认', { type: 'warning' })
    await api.deleteAgent(a.id!)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}
async function changeScenarioScope(value: string) {
  const query = { ...route.query }
  if (value) query.scenario_id = value
  else delete query.scenario_id
  await router.replace({ name: 'agents', query })
  return
}
watch(() => form.value.scenario_id, () => {
  void ensureScenarioDetail(form.value.scenario_id)
  const allowedIds = new Set(availableDataSources.value.map((source) => source.id))
  form.value.data_source_ids = (form.value.data_source_ids || []).filter((id) => allowedIds.has(id))
})
onMounted(() => {
  viewDisposed = false
  void load()
})
onBeforeUnmount(() => {
  viewDisposed = true
  loadRequest += 1
})
</script>

<style scoped>
.agent-card {
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
  margin-bottom: 16px;
  position: relative;
  overflow: hidden;
}
.agent-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--grad);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.agent-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.agent-card:hover::before { opacity: 1; }
.agent-card:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }
.ag-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.ag-avatar {
  width: 42px; height: 42px;
  border-radius: 12px;
  background: var(--grad);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  box-shadow: var(--shadow-primary);
}
.ag-title { flex: 1; min-width: 0; }
.ag-name {
  font-size: 15px; font-weight: 700;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ag-desc {
  color: var(--text-2); font-size: 13px;
  height: 38px; overflow: hidden; margin-bottom: 10px;
  line-height: 1.5;
}
.ag-tags { min-height: 24px; margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
.ag-actions {
  display: flex; gap: 4px;
  border-top: 1px solid var(--border); padding-top: 8px;
}
.agent-readiness { display: flex; flex-wrap: wrap; gap: 7px; margin: 2px 0 10px; }
.agent-readiness span { display: inline-flex; align-items: center; gap: 4px; color: var(--text-3); font-size: 10.5px; }
.agent-readiness .el-icon { color: var(--warning); }
.agent-readiness span.done { color: var(--text-2); }
.agent-readiness span.done .el-icon { color: var(--success); }
.setup-alert { margin-top: 4px; }
.agent-header-actions { display: flex; align-items: center; gap: 8px; }
.agent-header-actions :deep(.el-select) { width: min(240px, 38vw); }
@media (max-width: 640px) {
  .agent-header-actions { width: 100%; align-items: stretch; }
  .agent-header-actions :deep(.el-select) { min-width: 0; flex: 1; width: auto; }
}
</style>
