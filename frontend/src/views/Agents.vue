<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>验证 Agent</h1>
        <div class="sub">供业务专家验证场景理解与能力边界；运行数据和附件按每次验证独立提供</div>
      </div>
      <div class="agent-header-actions">
        <el-select
          :model-value="scenarioScope"
          clearable
          filterable
          aria-label="按业务场景筛选 Agent"
          placeholder="全部业务场景"
          @change="changeScenarioScope"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建验证 Agent</el-button>
      </div>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="a in visibleAgents" :key="a.id">
        <article class="card agent-card">
          <div class="ag-head">
            <div class="ag-avatar"><el-icon :size="20"><Cpu /></el-icon></div>
            <div class="ag-title">
              <div class="ag-name">{{ a.name }}</div>
              <div class="muted">{{ a.scenario_name || '未绑定场景' }}</div>
            </div>
            <el-tag size="small" effect="plain" :type="agentReady(a) ? 'success' : 'warning'">{{ agentReady(a) ? '可验证' : '待补齐' }}</el-tag>
          </div>
          <div class="ag-desc">{{ a.description || '暂无描述' }}</div>
          <div class="ag-tags">
            <el-tag v-if="a.llm_name" size="small" type="primary" effect="light"><el-icon aria-hidden="true"><ChatDotRound /></el-icon>{{ a.llm_name }}</el-tag>
            <el-tag v-for="connection in a.runtime_connections || []" :key="connection.id || connection.name" size="small" type="info" effect="light">
              <el-icon aria-hidden="true"><Coin /></el-icon>{{ connection.name }}
            </el-tag>
            <span class="muted" v-if="!(a.llm_name || a.runtime_connections?.length)">暂无模型与业务数据库</span>
          </div>
          <div class="capability-line">
            <span>业务能力 {{ capabilityTotals(a).selected }} 项</span>
            <span class="capability-ready">可执行 {{ capabilityTotals(a).executable }}</span>
            <span v-if="capabilityTotals(a).blocked" class="capability-blocked">阻塞 {{ capabilityTotals(a).blocked }}</span>
            <el-tag v-if="a.capability_scope_legacy" size="small" type="warning" effect="plain">旧版待配置</el-tag>
          </div>
          <div class="agent-readiness" :class="{ ready: agentReady(a) }">
            <span
              v-for="axis in readinessAxes"
              :key="axis.key"
              :class="{ done: agentReadiness(a)[axis.key].ready }"
              :title="readinessAxisTitle(a, axis.key, axis.label)"
            >
              <el-icon><component :is="agentReadiness(a)[axis.key].ready ? 'CircleCheck' : 'Warning'" /></el-icon>{{ axis.label }}
            </span>
          </div>
          <div class="ag-actions">
            <el-button v-if="agentReady(a)" size="small" type="primary" @click="openAgentChat(a)"><el-icon><ChatDotRound /></el-icon> 开始验证</el-button>
            <el-button v-else size="small" plain type="warning" @click="continueSetup(a)"><el-icon><Position /></el-icon> 补齐验证配置</el-button>
            <el-button size="small" :type="agentReady(a) ? 'primary' : 'warning'" :text="agentReady(a)" :plain="!agentReady(a)" @click="openEdit(a)">
              <el-icon><Setting /></el-icon> 编辑
            </el-button>
            <el-button size="small" text type="danger" @click="remove(a)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </article>
      </el-col>
    </el-row>
    <div v-if="!loading && !visibleAgents.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><Cpu /></el-icon></div>
      <div>暂无验证 Agent，点击右上角开始创建</div>
      <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建验证 Agent</el-button>
    </div>

    <!-- 编辑对话框 -->
    <el-dialog v-model="dlg" :title="form.id ? '编辑验证 Agent' : '新建验证 Agent'" width="900px" top="4vh" class="agent-dialog">
      <el-form :model="form" label-width="100px" class="agent-form">
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
              <el-select v-model="form.scenario_id" clearable placeholder="绑定场景（注入本体上下文）" style="width:100%" @change="changeFormScenario">
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
            placeholder="可选。留空则使用平台默认提示词（包含场景语义与已授权能力，并按需使用验证资源）" />
        </el-form-item>

        <section class="runtime-connection-section" aria-labelledby="runtime-connection-heading">
          <div class="runtime-connection-heading">
            <div>
              <h3 id="runtime-connection-heading">业务数据库</h3>
              <p>可选。这里只配置 Agent 正式运行时可访问的数据库；“建模资料”中的连接不会出现在这里，也不会被运行时使用。</p>
            </div>
            <el-button size="small" :disabled="!form.scenario_id" @click="addRuntimeConnection">
              <el-icon><Plus /></el-icon>添加数据库
            </el-button>
          </div>
          <el-alert
            v-if="!form.scenario_id"
            type="info"
            :closable="false"
            title="选择业务场景后可以配置业务数据库"
          />
          <div v-else-if="form.runtime_connections?.length" class="runtime-connection-list">
            <article v-for="(connection, index) in form.runtime_connections" :key="connection.id || index" class="runtime-connection-card">
              <div class="runtime-connection-card-head">
                <strong>PostgreSQL 数据库 {{ index + 1 }}</strong>
                <el-tag v-if="connection.status === 'ok'" size="small" type="success">连接正常</el-tag>
                <el-tag v-else-if="connection.status === 'error'" size="small" type="danger">连接异常</el-tag>
                <el-button text type="danger" :aria-label="`移除数据库 ${index + 1}`" @click="removeRuntimeConnection(index)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
              <el-row :gutter="10">
                <el-col :span="8"><el-input v-model="connection.name" placeholder="连接名称" aria-label="业务数据库连接名称" /></el-col>
                <el-col :span="10"><el-input v-model="connection.config.host" placeholder="主机地址" aria-label="数据库主机地址" /></el-col>
                <el-col :span="6"><el-input-number v-model="connection.config.port" :min="1" :max="65535" controls-position="right" aria-label="数据库端口" style="width:100%" /></el-col>
              </el-row>
              <el-row :gutter="10" class="runtime-connection-fields">
                <el-col :span="8"><el-input v-model="connection.config.database" placeholder="数据库名" aria-label="数据库名" /></el-col>
                <el-col :span="8"><el-input v-model="connection.config.username" placeholder="用户名" aria-label="数据库用户名" /></el-col>
                <el-col :span="8"><el-input v-model="connection.config.password" type="password" show-password :placeholder="connection.id ? '留空保持原密码' : '密码'" aria-label="数据库密码" /></el-col>
              </el-row>
              <p v-if="connection.last_error" class="runtime-connection-error">{{ connection.last_error }}</p>
            </article>
          </div>
          <el-empty v-else description="未配置业务数据库；Agent 仍可处理对话文本和本轮上传文件" :image-size="54" />
        </section>

        <section class="capability-section" aria-labelledby="agent-capability-heading">
          <div class="capability-heading">
            <div>
              <h3 id="agent-capability-heading">业务能力</h3>
              <p>新 Agent 默认拥有当前场景的全部业务能力；只有需要限制职责边界时，才改为指定能力。</p>
            </div>
            <el-tag v-if="capabilityCatalog" size="small" effect="plain">{{ capabilityCatalog.environment }} 运行定义</el-tag>
          </div>
          <el-alert
            v-if="editingLegacyScope"
            type="warning"
            :closable="false"
            show-icon
            title="这是旧版 Agent，未配置的业务能力已安全停用；请选择职责所需能力后保存。"
            class="capability-alert"
          />
          <el-alert
            v-if="capabilityCatalogError"
            type="warning"
            :closable="false"
            show-icon
            :title="capabilityCatalogError"
            class="capability-alert"
          />
          <div v-loading="capabilityCatalogLoading" class="capability-grid">
            <article v-for="category in capabilityCategories" :key="category.key" class="capability-card">
              <div class="capability-card-head">
                <div>
                  <strong>{{ category.label }}</strong>
                  <span>{{ category.help }}</span>
                </div>
                <el-radio-group
                  :model-value="capabilityEntry(category.key).mode"
                  size="small"
                  :aria-label="`${category.label}授权模式`"
                  @change="changeCapabilityMode(category.key, $event)"
                >
                  <el-radio-button value="explicit">指定</el-radio-button>
                  <el-radio-button value="all">选择当前全部</el-radio-button>
                </el-radio-group>
              </div>
              <el-select
                v-if="capabilityEntry(category.key).mode === 'explicit'"
                :model-value="capabilityEntry(category.key).selected_ids"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                :disabled="!form.scenario_id || capabilityCatalogLoading"
                :placeholder="form.scenario_id ? `选择${category.label}` : '请先选择业务场景'"
                style="width:100%"
                @update:model-value="setCapabilityIds(category.key, $event)"
              >
                <el-option
                  v-for="item in capabilityOptions(category.key)"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                >
                  <div class="capability-option">
                    <span>{{ item.name }}</span>
                    <el-tag size="small" :type="item.executable ? 'success' : 'warning'" effect="plain">
                      {{ item.executable ? '可执行' : '阻塞' }}
                    </el-tag>
                  </div>
                </el-option>
              </el-select>
              <p v-else class="capability-all-hint">保存时会固定当前可见{{ category.label }}；以后新增需再次保存授权。</p>
              <div class="capability-stats" aria-live="polite">
                <span>已选择 {{ capabilityStats(category.key).selected }}</span>
                <span class="capability-ready">可执行 {{ capabilityStats(category.key).executable }}</span>
                <span v-if="capabilityStats(category.key).blocked" class="capability-blocked">阻塞 {{ capabilityStats(category.key).blocked }}</span>
              </div>
              <ul v-if="capabilityBlockedReasons(category.key).length" class="capability-reasons">
                <li v-for="reason in capabilityBlockedReasons(category.key)" :key="reason">{{ reason }}</li>
              </ul>
            </article>
          </div>
        </section>

        <el-alert
          :type="formAgentReady ? 'success' : 'warning'"
          :closable="false"
          show-icon
          class="setup-alert"
          :title="formAgentReady ? 'Agent 已具备开始能力验证的条件' : `尚缺：${formAgentMissing.join('、')}`"
        >
          <template #default>
            <span>{{ formAgentReady ? '发布与正式运行仍以各自就绪检查为准。' : '绑定资源是可选项，无固定数据的场景不会因此被阻塞。' }}</span>
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
import { computed, ref, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { allAgentCapabilityScope, cloneAgentCapabilityScope, emptyAgentCapabilityScope } from '@/utils/agentCapabilities'
import { normalizeAgentReadiness } from '@/utils/agentReadiness'
import { filterAgentsByScenario, scenarioIdFromQuery } from '@/utils/agentValidationView'
import type {
  Agent,
  AgentCapabilityCatalog,
  AgentCapabilityCategory,
  AgentCapabilityReadinessItem,
  AgentCapabilitySelection,
  AgentCapabilitySummary,
  AgentReadinessAxisKey,
  Scenario,
  LLMConfig,
  AgentRuntimeConnection,
} from '@/types'

const capabilityCategories: Array<{ key: AgentCapabilityCategory; label: string; help: string }> = [
  { key: 'functions', label: '函数', help: '确定性计算与转换' },
  { key: 'actions', label: '操作', help: '需要确认的业务变更' },
  { key: 'rules', label: '规则', help: '业务判断与约束' },
  { key: 'workflows', label: '工作流', help: '跨步骤任务编排' },
]
const readinessAxes: Array<{ key: AgentReadinessAxisKey; label: string }> = [
  { key: 'definition', label: '定义' },
  { key: 'validation', label: '验证' },
  { key: 'release', label: '发布' },
  { key: 'runtime', label: '运行' },
]

const agents = ref<Agent[]>([])
const scenarios = ref<Scenario[]>([])
const llms = ref<LLMConfig[]>([])
const route = useRoute()
const router = useRouter()
const scenarioScope = computed(() => scenarioIdFromQuery(route.query.scenario_id))
const visibleAgents = computed(() => filterAgentsByScenario(agents.value, scenarioScope.value))

const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<Agent>>({ data_source_ids: [], capability_scope: allAgentCapabilityScope() })
const capabilityCatalog = ref<AgentCapabilityCatalog | null>(null)
const capabilityCatalogLoading = ref(false)
const capabilityCatalogError = ref('')
const editingLegacyScope = ref(false)
const editingCapabilitySummary = ref<Partial<Record<AgentCapabilityCategory, AgentCapabilitySummary>>>({})
let viewDisposed = false
let loadRequest = 0
let capabilityCatalogRequest = 0

function capabilityEntry(category: AgentCapabilityCategory): AgentCapabilitySelection {
  if (!form.value.capability_scope) form.value.capability_scope = emptyAgentCapabilityScope()
  return form.value.capability_scope[category]
}

function changeCapabilityMode(category: AgentCapabilityCategory, value: unknown) {
  const entry = capabilityEntry(category)
  entry.mode = value === 'all' ? 'all' : 'explicit'
  entry.selected_ids = []
  editingLegacyScope.value = false
}

function setCapabilityIds(category: AgentCapabilityCategory, value: unknown) {
  const ids = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
  capabilityEntry(category).selected_ids = [...new Set(ids)]
  editingLegacyScope.value = false
}

function capabilityOptions(category: AgentCapabilityCategory): AgentCapabilityReadinessItem[] {
  const items = new Map<string, AgentCapabilityReadinessItem>()
  for (const item of capabilityCatalog.value?.categories[category] || []) items.set(item.id, item)
  for (const item of editingCapabilitySummary.value[category]?.items || []) {
    if (!items.has(item.id)) items.set(item.id, item)
  }
  return [...items.values()]
}

function selectedCapabilityItems(category: AgentCapabilityCategory): AgentCapabilityReadinessItem[] {
  const entry = capabilityEntry(category)
  if (entry.mode === 'all') return capabilityCatalog.value?.categories[category] || []
  const options = new Map(capabilityOptions(category).map((item) => [item.id, item]))
  return entry.selected_ids.map((id) => options.get(id) || {
    id,
    name: `已失效能力 ${id.slice(0, 8)}`,
    executable: false,
    blocked_reasons: ['所选能力已不在当前运行定义中或当前账号已无读取权限'],
  })
}

function capabilityStats(category: AgentCapabilityCategory) {
  const items = selectedCapabilityItems(category)
  const executable = items.filter((item) => item.executable).length
  return { selected: items.length, executable, blocked: items.length - executable }
}

function capabilityBlockedReasons(category: AgentCapabilityCategory): string[] {
  return selectedCapabilityItems(category)
    .flatMap((item) => item.executable
      ? []
      : (item.blocked_reasons?.length ? item.blocked_reasons : ['当前不可执行'])
        .map((reason) => `${item.name}：${reason}`))
    .filter((reason, index, values) => values.indexOf(reason) === index)
    .slice(0, 4)
}

function capabilityTotals(agent: Partial<Agent>) {
  const summaries = capabilityCategories.map((category) => agent.capability_summary?.[category.key])
  return summaries.reduce(
    (total, summary) => ({
      selected: total.selected + (summary?.selected_count || 0),
      executable: total.executable + (summary?.executable_count || 0),
      blocked: total.blocked + (summary?.blocked_count || 0),
    }),
    { selected: 0, executable: 0, blocked: 0 },
  )
}
function agentReadiness(agent: Partial<Agent>) {
  return normalizeAgentReadiness(agent)
}
function agentReady(agent: Partial<Agent>) {
  return agentReadiness(agent).validation.ready
}
function readinessAxisTitle(agent: Partial<Agent>, key: AgentReadinessAxisKey, label: string) {
  const axis = agentReadiness(agent)[key]
  if (axis.ready) return `${label}已就绪`
  return axis.missing.map((issue) => issue.label).join('；') || `${label}尚未就绪`
}
const formReadiness = computed(() => normalizeAgentReadiness({
  name: form.value.name || '',
  scenario_id: form.value.scenario_id,
  llm_config_id: form.value.llm_config_id,
  data_source_ids: form.value.data_source_ids || [],
}))
const formAgentMissing = computed(() => formReadiness.value.validation.missing.map((issue) => issue.label))
const formAgentReady = computed(() => formAgentMissing.value.length === 0)
async function loadCapabilityCatalog(scenarioId?: string | null) {
  const request = ++capabilityCatalogRequest
  capabilityCatalog.value = null
  capabilityCatalogError.value = ''
  if (!scenarioId) {
    capabilityCatalogLoading.value = false
    return
  }
  capabilityCatalogLoading.value = true
  try {
    const catalog = await api.getAgentCapabilityCatalog(scenarioId)
    if (!viewDisposed && request === capabilityCatalogRequest && form.value.scenario_id === scenarioId) {
      capabilityCatalog.value = catalog
    }
  } catch (e: any) {
    if (!viewDisposed && request === capabilityCatalogRequest && form.value.scenario_id === scenarioId) {
      capabilityCatalogError.value = `能力目录暂不可用：${e.message || '当前环境运行定义不可用'}`
    }
  } finally {
    if (!viewDisposed && request === capabilityCatalogRequest) capabilityCatalogLoading.value = false
  }
}

async function load() {
  const request = ++loadRequest
  loading.value = true
  try {
    const [ag, sc, ll] = await Promise.all([
      api.listAgents(), api.listScenarios(), api.listLLM(),
    ])
    if (viewDisposed || request !== loadRequest) return
    agents.value = ag
    scenarios.value = sc
    llms.value = ll
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
    runtime_connections: [],
    capability_scope: allAgentCapabilityScope(),
    runtime_binding_mode: 'capability_only',
  }
  editingLegacyScope.value = false
  editingCapabilitySummary.value = {}
  dlg.value = true
  void loadCapabilityCatalog(form.value.scenario_id)
}
function openEdit(a: Agent) {
  form.value = {
    ...a,
    data_source_ids: [],
    runtime_binding_mode: 'capability_only',
    runtime_connections: (a.runtime_connections || []).map((connection) => ({
      ...connection,
      config: { ...connection.config },
    })),
    capability_scope: cloneAgentCapabilityScope(a.capability_scope),
  }
  editingLegacyScope.value = Boolean(a.capability_scope_legacy)
  editingCapabilitySummary.value = a.capability_summary || {}
  dlg.value = true
  void loadCapabilityCatalog(form.value.scenario_id)
}

function changeFormScenario(value: string | undefined) {
  form.value.data_source_ids = []
  form.value.runtime_connections = []
  form.value.capability_scope = allAgentCapabilityScope()
  editingLegacyScope.value = false
  editingCapabilitySummary.value = {}
  void loadCapabilityCatalog(value)
}

function addRuntimeConnection() {
  if (!form.value.scenario_id) return ElMessage.info('请先选择业务场景')
  const connection: AgentRuntimeConnection = {
    name: `业务数据库 ${(form.value.runtime_connections?.length || 0) + 1}`,
    type: 'postgres',
    config: {
      host: '',
      port: undefined,
      database: '',
      username: '',
      password: '',
    },
  }
  form.value.runtime_connections = [...(form.value.runtime_connections || []), connection]
}

function removeRuntimeConnection(index: number) {
  form.value.runtime_connections = (form.value.runtime_connections || []).filter((_item, itemIndex) => itemIndex !== index)
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
  openEdit(agent)
}
async function save() {
  if (!form.value.name) return ElMessage.warning('请填写名称')
  const invalidConnection = (form.value.runtime_connections || []).find((connection) => (
    !connection.name.trim()
    || !String(connection.config.host || '').trim()
    || !String(connection.config.database || '').trim()
    || !String(connection.config.username || '').trim()
  ))
  if (invalidConnection) return ElMessage.warning('请补齐业务数据库的名称、主机、数据库名和用户名')
  saving.value = true
  try {
    if (form.value.id) await api.updateAgent(form.value.id, {
      ...form.value,
      data_source_ids: [],
      runtime_binding_mode: 'capability_only',
    })
    else await api.createAgent({
      ...form.value,
      data_source_ids: [],
      runtime_binding_mode: 'capability_only',
    })
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
async function changeScenarioScope(value: unknown) {
  const scenarioId = scenarioIdFromQuery(value)
  const query = { ...route.query }
  if (scenarioId) query.scenario_id = scenarioId
  else delete query.scenario_id
  await router.replace({ name: 'agents', query })
}
onMounted(() => {
  viewDisposed = false
  void load()
})
onBeforeUnmount(() => {
  viewDisposed = true
  loadRequest += 1
  capabilityCatalogRequest += 1
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
  display: -webkit-box; min-height: 38px; overflow: hidden; margin-bottom: 10px;
  -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  line-height: 1.5;
}
.runtime-connection-section { margin: 16px 0; padding: 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.runtime-connection-heading, .runtime-connection-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.runtime-connection-heading { align-items: flex-start; margin-bottom: 12px; }
.runtime-connection-heading h3 { margin: 0; color: var(--text-1); font-size: 15px; }
.runtime-connection-heading p { margin: 4px 0 0; color: var(--text-3); font-size: 11px; line-height: 1.55; }
.runtime-connection-list { display: grid; gap: 10px; }
.runtime-connection-card { padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.runtime-connection-card-head { margin-bottom: 10px; }
.runtime-connection-card-head strong { margin-right: auto; color: var(--text-1); font-size: 12px; }
.runtime-connection-fields { margin-top: 9px; }
.runtime-connection-error { margin: 8px 0 0; color: var(--danger); font-size: 11px; }
.ag-tags { min-height: 24px; margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
.capability-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: -2px 0 10px;
  color: var(--text-3);
  font-size: 11px;
}
.capability-ready { color: var(--success); }
.capability-blocked { color: var(--warning); }
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
.capability-section {
  margin: 4px 0 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.capability-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.capability-heading h3 { margin: 0 0 3px; color: var(--text-1); font-size: 15px; }
.capability-heading p { margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.55; }
.capability-alert { margin-bottom: 10px; }
.capability-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; min-height: 70px; }
.capability-card { min-width: 0; padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-1); }
.capability-card:last-child:nth-child(odd) { grid-column: 1 / -1; }
.capability-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
.capability-card-head > div:first-child { min-width: 0; }
.capability-card-head strong { display: block; color: var(--text-1); font-size: 13px; }
.capability-card-head span { display: block; margin-top: 2px; color: var(--text-3); font-size: 11px; }
.capability-option { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.capability-all-hint { margin: 0; color: var(--warning); font-size: 11px; line-height: 32px; }
.capability-stats { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; color: var(--text-3); font-size: 11px; }
.capability-reasons { margin: 7px 0 0; padding-left: 18px; color: var(--warning); font-size: 11px; line-height: 1.45; }
:deep(.agent-dialog .el-dialog__body) { max-height: calc(92vh - 132px); overflow-y: auto; overscroll-behavior: contain; }
.agent-header-actions { display: flex; align-items: center; gap: 8px; }
.agent-header-actions :deep(.el-select) { width: min(240px, 38vw); }
@media (max-width: 640px) {
  .agent-header-actions { width: 100%; align-items: stretch; }
  .agent-header-actions :deep(.el-select) { min-width: 0; flex: 1; width: auto; }
  .agent-form > .el-row { margin-right: 0 !important; margin-left: 0 !important; }
  .agent-form > .el-row > .el-col { max-width: 100%; flex: 0 0 100%; padding-right: 0 !important; padding-left: 0 !important; }
  .agent-form :deep(.el-form-item) { display: block; }
  .agent-form :deep(.el-form-item__label) { width: auto !important; height: auto; justify-content: flex-start; margin-bottom: 6px; padding: 0; line-height: 1.45; }
  .agent-form :deep(.el-form-item__content) { margin-left: 0 !important; }
  .capability-grid { grid-template-columns: 1fr; }
  .capability-card:last-child:nth-child(odd) { grid-column: auto; }
  .capability-card-head { align-items: stretch; flex-direction: column; }
  .capability-card-head :deep(.el-radio-group) { align-self: flex-start; }
}
</style>
