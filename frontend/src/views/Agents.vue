<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>Agent 管理</h1>
        <div class="sub">绑定业务场景、LLM、技能、MCP 与数据源，构建智能体</div>
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
            <el-tag size="small" effect="plain" :type="agentReady(a) ? 'success' : 'warning'">{{ agentReady(a) ? '可测试' : '待配置' }}</el-tag>
          </div>
          <div class="ag-desc">{{ a.description || '暂无描述' }}</div>
          <div class="ag-tags">
            <el-tag v-if="a.llm_name" size="small" type="primary" effect="light"><el-icon aria-hidden="true"><ChatDotRound /></el-icon>{{ a.llm_name }}</el-tag>
            <el-tag v-for="n in a.skill_names || []" :key="n" size="small" type="success" effect="light"><el-icon aria-hidden="true"><MagicStick /></el-icon>{{ n }}</el-tag>
            <el-tag v-for="n in a.mcp_names || []" :key="n" size="small" type="warning" effect="light"><el-icon aria-hidden="true"><Connection /></el-icon>{{ n }}</el-tag>
            <el-tag v-for="n in a.data_source_names || []" :key="n" size="small" type="info" effect="light"><el-icon aria-hidden="true"><Coin /></el-icon>{{ n }}</el-tag>
            <span class="muted" v-if="!(a.llm_name || a.skill_names?.length || a.mcp_names?.length || a.data_source_names?.length)">未配置能力</span>
          </div>
          <div class="agent-readiness" :class="{ ready: agentReady(a) }">
            <span><el-icon><component :is="a.scenario_id ? 'CircleCheck' : 'Warning'" /></el-icon>场景</span>
            <span><el-icon><component :is="a.llm_config_id ? 'CircleCheck' : 'Warning'" /></el-icon>模型</span>
            <span><el-icon><component :is="a.data_source_ids?.length ? 'CircleCheck' : 'InfoFilled'" /></el-icon>数据</span>
          </div>
          <div class="ag-actions">
            <el-button v-if="agentReady(a)" size="small" type="primary" @click="openAgentChat(a)"><el-icon><ChatDotRound /></el-icon> 测试对话</el-button>
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
            <el-form-item label="LLM 配置">
              <el-select v-model="form.llm_config_id" clearable placeholder="选择大模型" style="width:100%">
                <el-option v-for="l in llms" :key="l.id" :label="`${l.name}（${l.model}）`" :value="l.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="系统提示词">
          <el-input v-model="form.system_prompt" type="textarea" :rows="3"
            placeholder="可选。留空则使用平台默认提示词（含本体摘要、数据源、技能、MCP 说明）" />
        </el-form-item>

        <el-form-item label="技能">
          <el-select v-model="form.skill_ids" multiple placeholder="安装技能（如 OCR 解析、数据分析）" style="width:100%">
            <el-option v-for="s in skills" :key="s.id" :label="s.name" :value="s.id" :disabled="!s.enabled">
              <span>{{ s.name }}</span>
              <span class="muted" style="float:right">{{ s.description?.slice(0, 20) }}</span>
            </el-option>
          </el-select>
        </el-form-item>

        <el-form-item label="MCP 服务">
          <el-select v-model="form.mcp_ids" multiple placeholder="安装 MCP 工具服务" style="width:100%">
            <el-option v-for="m in mcps" :key="m.id" :label="m.name" :value="m.id" :disabled="!m.enabled" />
          </el-select>
        </el-form-item>

        <el-form-item label="数据源">
          <el-select v-model="form.data_source_ids" multiple placeholder="绑定数据源（数据库 / 文件桶）" style="width:100%">
            <el-option v-for="d in availableDataSources" :key="d.id" :label="d.name" :value="d.id">
              <span>{{ d.name }}</span>
              <span class="muted" style="float:right">{{ d.type }}</span>
            </el-option>
          </el-select>
        </el-form-item>
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
import type { Agent, Scenario, LLMConfig, Skill, MCPConfig, DataSource } from '@/types'

const agents = ref<Agent[]>([])
const scenarios = ref<Scenario[]>([])
const llms = ref<LLMConfig[]>([])
const skills = ref<Skill[]>([])
const mcps = ref<MCPConfig[]>([])
const dataSources = ref<DataSource[]>([])
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
const form = ref<Partial<Agent>>({ skill_ids: [], mcp_ids: [], data_source_ids: [] })
let viewDisposed = false
let loadRequest = 0
const availableDataSources = computed(() => {
  const scenarioId = form.value.scenario_id
  return dataSources.value.filter((source) => !scenarioId || !source.scenario_id || source.scenario_id === scenarioId)
})
function agentReady(agent: Agent) {
  return Boolean(agent.scenario_id && agent.llm_config_id)
}

async function load() {
  const request = ++loadRequest
  const scope = scenarioScope.value
  loading.value = true
  try {
    const [ag, sc, ll, sk, mc, ds] = await Promise.all([
      api.listAgents(), api.listScenarios(), api.listLLM(),
      api.listSkills(), api.listMCP(), api.listDataSources(),
    ])
    if (viewDisposed || request !== loadRequest || scope !== scenarioScope.value) return
    agents.value = scope ? ag.filter((agent) => agent.scenario_id === scope) : ag
    scenarios.value = sc
    llms.value = ll
    skills.value = sk
    mcps.value = mc
    dataSources.value = ds
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
    skill_ids: [],
    mcp_ids: [],
    data_source_ids: [],
  }
  dlg.value = true
}
function openEdit(a: Agent) {
  form.value = { ...a, skill_ids: [...(a.skill_ids || [])], mcp_ids: [...(a.mcp_ids || [])], data_source_ids: [...(a.data_source_ids || [])] }
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
.agent-readiness.ready span:first-child .el-icon,
.agent-readiness.ready span:nth-child(2) .el-icon { color: var(--success); }
.agent-header-actions { display: flex; align-items: center; gap: 8px; }
.agent-header-actions :deep(.el-select) { width: min(240px, 38vw); }
@media (max-width: 640px) {
  .agent-header-actions { width: 100%; align-items: stretch; }
  .agent-header-actions :deep(.el-select) { min-width: 0; flex: 1; width: auto; }
}
</style>
