<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>Agent 管理</h2>
        <div class="sub">绑定业务场景、LLM、技能、MCP 与数据源，构建智能体</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建 Agent</el-button>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="a in agents" :key="a.id">
        <div class="card agent-card" role="button" tabindex="0" :aria-label="`打开 Agent 对话：${a.name}`" @click="$router.push('/agents/' + a.id + '/chat')" @keydown.enter.prevent="$router.push('/agents/' + a.id + '/chat')" @keydown.space.prevent="$router.push('/agents/' + a.id + '/chat')">
          <div class="ag-head">
            <div class="ag-avatar"><el-icon :size="20"><Cpu /></el-icon></div>
            <div class="ag-title">
              <div class="ag-name">{{ a.name }}</div>
              <div class="muted">{{ a.scenario_name || '未绑定场景' }}</div>
            </div>
          </div>
          <div class="ag-desc">{{ a.description || '暂无描述' }}</div>
          <div class="ag-tags">
            <el-tag v-if="a.llm_name" size="small" type="primary" effect="light"><el-icon aria-hidden="true"><ChatDotRound /></el-icon>{{ a.llm_name }}</el-tag>
            <el-tag v-for="n in a.skill_names || []" :key="n" size="small" type="success" effect="light"><el-icon aria-hidden="true"><MagicStick /></el-icon>{{ n }}</el-tag>
            <el-tag v-for="n in a.mcp_names || []" :key="n" size="small" type="warning" effect="light"><el-icon aria-hidden="true"><Connection /></el-icon>{{ n }}</el-tag>
            <el-tag v-for="n in a.data_source_names || []" :key="n" size="small" type="info" effect="light"><el-icon aria-hidden="true"><Coin /></el-icon>{{ n }}</el-tag>
            <span class="muted" v-if="!(a.llm_name || a.skill_names?.length || a.mcp_names?.length || a.data_source_names?.length)">未配置能力</span>
          </div>
          <div class="ag-actions" @click.stop>
            <el-button size="small" type="primary" @click="$router.push('/agents/' + a.id + '/chat')"><el-icon><ChatDotRound /></el-icon> 对话</el-button>
            <el-button size="small" text type="primary" @click="openEdit(a)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(a)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </div>
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
            <el-option v-for="d in dataSources" :key="d.id" :label="d.name" :value="d.id">
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
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { Agent, Scenario, LLMConfig, Skill, MCPConfig, DataSource } from '@/types'

const agents = ref<Agent[]>([])
const scenarios = ref<Scenario[]>([])
const llms = ref<LLMConfig[]>([])
const skills = ref<Skill[]>([])
const mcps = ref<MCPConfig[]>([])
const dataSources = ref<DataSource[]>([])

const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<Agent>>({ skill_ids: [], mcp_ids: [], data_source_ids: [] })

async function load() {
  loading.value = true
  try {
    const [ag, sc, ll, sk, mc, ds] = await Promise.all([
      api.listAgents(), api.listScenarios(), api.listLLM(),
      api.listSkills(), api.listMCP(), api.listDataSources(),
    ])
    agents.value = ag
    scenarios.value = sc
    llms.value = ll
    skills.value = sk
    mcps.value = mc
    dataSources.value = ds
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function openCreate() {
  form.value = { name: '', description: '', skill_ids: [], mcp_ids: [], data_source_ids: [] }
  dlg.value = true
}
function openEdit(a: Agent) {
  form.value = { ...a, skill_ids: [...(a.skill_ids || [])], mcp_ids: [...(a.mcp_ids || [])], data_source_ids: [...(a.data_source_ids || [])] }
  dlg.value = true
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
onMounted(load)
</script>

<style scoped>
.agent-card {
  cursor: pointer;
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
</style>
