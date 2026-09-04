<template>
  <section class="publication-panel" aria-labelledby="agent-mcp-heading">
    <div class="publication-toolbar">
      <div>
        <h2 id="agent-mcp-heading">Agent 发布</h2>
        <p>将当前可对话的 Agent 作为远程 MCP 服务提供给第三方，调用时继承 Agent 的当前能力。</p>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreate">
        <el-icon aria-hidden="true"><Plus /></el-icon>新建发布
      </el-button>
    </div>

    <el-alert
      v-if="!canManage"
      class="readonly-notice"
      type="info"
      title="当前账户为只读：可查看发布状态，不能创建、轮换、测试或停用 Agent MCP。"
      show-icon
      :closable="false"
    />
    <el-alert
      v-else
      class="standard-mcp-notice"
      type="info"
      title="使用标准 MCP 工具发现与调用"
      description="第三方客户端通过 tools/list 自动发现 invoke_agent 的 message 和 conversation_id 参数，无需实现平台私有扩展。"
      show-icon
      :closable="false"
    />

    <div class="publication-grid" v-loading="loading" :aria-busy="loading">
      <article v-for="service in services" :key="service.id" class="card publication-card">
        <header class="publication-head">
          <div class="publication-icon"><el-icon :size="20"><Promotion /></el-icon></div>
          <div class="publication-title">
            <strong>{{ service.name }}</strong>
            <span>{{ service.agent_name }}</span>
          </div>
          <el-tag size="small" :type="statusType(service)" effect="light">{{ statusLabel(service) }}</el-tag>
        </header>

        <dl class="publication-facts">
          <div>
            <dt>业务场景</dt>
            <dd>{{ service.scenario_name || '未绑定场景' }}</dd>
          </div>
          <div>
            <dt>运行环境</dt>
            <dd>{{ service.runtime_environment }}</dd>
          </div>
          <div>
            <dt>凭证</dt>
            <dd class="mono secret-value">{{ service.key_prefix }}••••{{ service.token_hint }}</dd>
          </div>
          <div>
            <dt>到期时间</dt>
            <dd>{{ formatDate(service.expires_at) }}</dd>
          </div>
        </dl>

        <div class="endpoint-row">
          <el-icon aria-hidden="true"><Connection /></el-icon>
          <span class="mono" :title="service.endpoint_url">{{ service.endpoint_url }}</span>
        </div>

        <p v-if="service.stale" class="service-warning" role="status">Agent 配置已变化，请轮换凭证后重新分发配置。</p>
        <p v-else-if="service.missing?.length" class="service-warning" role="status">缺少：{{ service.missing.join('、') }}</p>

        <footer v-if="canManage" class="publication-actions">
          <el-button size="small" plain :loading="testingId === service.id" @click="testService(service)">
            <el-icon aria-hidden="true"><Link /></el-icon>测试
          </el-button>
          <el-button size="small" plain type="primary" :loading="rotatingId === service.id" @click="rotateToken(service)">
            <el-icon aria-hidden="true"><RefreshRight /></el-icon>轮换并复制
          </el-button>
          <el-switch
            :model-value="service.enabled"
            :aria-label="`${service.enabled ? '停用' : '启用'} ${service.name}`"
            @change="(value: boolean) => toggleService(service, value)"
          />
          <el-button size="small" text type="danger" @click="removeService(service)">
            <el-icon aria-hidden="true"><Delete /></el-icon>删除
          </el-button>
        </footer>
      </article>
    </div>

    <div v-if="!loading && !services.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><Promotion /></el-icon></div>
      <div>{{ canManage ? '暂无 Agent MCP 发布，创建后即可复制配置给第三方' : '暂无 Agent MCP 发布' }}</div>
      <el-button v-if="canManage" type="primary" size="small" @click="openCreate">
        <el-icon aria-hidden="true"><Plus /></el-icon>新建发布
      </el-button>
    </div>

    <el-dialog v-if="canManage" v-model="createDialog" title="新建 Agent MCP 发布" width="min(620px, 94vw)" @closed="resetCreate">
      <el-alert
        v-if="formError"
        class="dialog-error"
        type="error"
        :title="formError"
        :closable="false"
        show-icon
      />
      <el-form class="publication-form" label-width="108px" @submit.prevent>
        <el-form-item label="绑定 Agent" required>
          <el-select v-model="form.agent_id" class="full-width" filterable placeholder="选择可对话 Agent" @change="onAgentChange">
            <el-option
              v-for="candidate in candidates"
              :key="candidate.id"
              :value="candidate.id"
              :label="candidateLabel(candidate)"
              :disabled="!candidate.ready"
            >
              <div class="candidate-option">
                <span>{{ candidate.name }}</span>
                <el-tag size="small" :type="candidate.ready ? 'success' : 'warning'" effect="plain">
                  {{ candidate.ready ? '可发布' : '未就绪' }}
                </el-tag>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="服务名称" required>
          <el-input v-model.trim="form.name" maxlength="120" show-word-limit placeholder="默认使用 Agent 名称" />
        </el-form-item>
        <el-form-item label="凭证有效期">
          <el-select v-model="form.expires_in_days" class="full-width">
            <el-option label="90 天" :value="90" />
            <el-option label="180 天" :value="180" />
            <el-option label="365 天" :value="365" />
            <el-option label="730 天" :value="730" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert
        title="创建后会自动生成专属令牌。令牌只显示一次，请立即复制完整配置。"
        type="info"
        :closable="false"
        show-icon
      />
      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="createService">创建并生成配置</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="secretDialog"
      title="MCP 配置已生成"
      width="min(720px, 94vw)"
      :close-on-click-modal="false"
      @closed="clearCreatedService"
    >
      <el-alert
        title="此配置包含完整访问令牌，关闭后平台不会再次显示。丢失后请轮换凭证。"
        type="warning"
        :closable="false"
        show-icon
      />
      <label class="config-label" for="agent-mcp-config">第三方 MCP 连接配置</label>
      <el-input
        id="agent-mcp-config"
        :model-value="createdService?.config_json || ''"
        class="config-output"
        type="textarea"
        readonly
        :rows="11"
      />
      <template #footer>
        <el-button @click="secretDialog = false">关闭</el-button>
        <el-button type="primary" @click="copyConfig">
          <el-icon aria-hidden="true"><DocumentCopy /></el-icon>复制连接配置
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { AgentMCPCandidate, AgentMCPService, AgentMCPServiceCreated } from '@/types'

defineProps<{ canManage: boolean }>()

const services = ref<AgentMCPService[]>([])
const candidates = ref<AgentMCPCandidate[]>([])
const loading = ref(false)
const saving = ref(false)
const testingId = ref('')
const rotatingId = ref('')
const createDialog = ref(false)
const secretDialog = ref(false)
const createdService = ref<AgentMCPServiceCreated | null>(null)
const formError = ref('')
const form = reactive({ name: '', agent_id: '', expires_in_days: 365 })

async function load() {
  loading.value = true
  try {
    const [serviceRows, candidateRows] = await Promise.all([
      api.listAgentMCPServices(),
      api.listAgentMCPCandidates(),
    ])
    services.value = serviceRows
    candidates.value = candidateRows
  } catch (error: any) {
    ElMessage.error(error.message || 'Agent MCP 发布加载失败')
  } finally {
    loading.value = false
  }
}

function resetCreate() {
  form.name = ''
  form.agent_id = ''
  form.expires_in_days = 365
  formError.value = ''
}

function openCreate() {
  resetCreate()
  createDialog.value = true
}

function onAgentChange(agentId: string) {
  const candidate = candidates.value.find((item) => item.id === agentId)
  if (candidate && !form.name) form.name = candidate.name
}

function candidateLabel(candidate: AgentMCPCandidate) {
  const scope = candidate.scenario_name ? ` · ${candidate.scenario_name}` : ''
  return `${candidate.name}${scope}${candidate.ready ? '' : ' · 未就绪'}`
}

async function createService() {
  formError.value = ''
  if (!form.agent_id) formError.value = '请选择要发布的 Agent'
  else if (!form.name.trim()) formError.value = '请输入 MCP 服务名称'
  if (formError.value) return
  saving.value = true
  try {
    createdService.value = await api.createAgentMCPService({
      name: form.name.trim(),
      agent_id: form.agent_id,
      expires_in_days: form.expires_in_days,
    })
    createDialog.value = false
    secretDialog.value = true
    await load()
  } catch (error: any) {
    formError.value = error.message || '创建失败'
  } finally {
    saving.value = false
  }
}

async function testService(service: AgentMCPService) {
  testingId.value = service.id
  try {
    const result = await api.testAgentMCPService(service.id)
    ElMessage.success(result.message)
  } catch (error: any) {
    ElMessage.error(error.message || '发布测试失败')
  } finally {
    testingId.value = ''
  }
}

async function rotateToken(service: AgentMCPService) {
  try {
    await ElMessageBox.confirm(
      `轮换后「${service.name}」的旧配置将立即失效，是否继续？`,
      '轮换访问令牌',
      { type: 'warning', confirmButtonText: '轮换并生成配置', cancelButtonText: '取消' },
    )
    rotatingId.value = service.id
    createdService.value = await api.rotateAgentMCPToken(service.id)
    secretDialog.value = true
    await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '令牌轮换失败')
  } finally {
    rotatingId.value = ''
  }
}

async function toggleService(service: AgentMCPService, enabled: boolean) {
  try {
    const updated = await api.updateAgentMCPService(service.id, enabled)
    Object.assign(service, updated)
    ElMessage.success(enabled ? 'Agent MCP 已启用' : 'Agent MCP 已停用')
  } catch (error: any) {
    ElMessage.error(error.message || '状态更新失败')
  }
}

async function removeService(service: AgentMCPService) {
  try {
    await ElMessageBox.confirm(
      `删除「${service.name}」后，第三方配置将立即失效。`,
      '删除 Agent MCP',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await api.deleteAgentMCPService(service.id)
    ElMessage.success('Agent MCP 已删除')
    await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除失败')
  }
}

async function copyText(value: string, successMessage: string) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success(successMessage)
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = value
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
    ElMessage.success(successMessage)
  }
}

async function copyConfig() {
  await copyText(createdService.value?.config_json || '', 'MCP 连接配置已复制')
}

function clearCreatedService() {
  createdService.value = null
}

function statusLabel(service: AgentMCPService) {
  if (!service.enabled) return '已停用'
  if (service.stale) return '待重新发布'
  if (!service.ready) return '不可用'
  return '可调用'
}

function statusType(service: AgentMCPService) {
  if (!service.enabled) return 'info'
  if (service.stale || !service.ready) return 'warning'
  return 'success'
}

function formatDate(value?: string | null) {
  if (!value) return '长期有效'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleDateString('zh-CN')
}

onMounted(load)
</script>

<style scoped>
.publication-panel { min-width: 0; }
.publication-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.publication-toolbar h2 { margin: 0 0 4px; font-size: 16px; }
.publication-toolbar p { margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.55; }
.publication-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; min-height: 80px; }
.publication-card { display: flex; min-width: 0; flex-direction: column; }
.publication-head { display: flex; align-items: center; gap: 10px; }
.publication-icon { display: flex; width: 40px; height: 40px; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 8px; color: var(--primary); background: var(--primary-soft); }
.publication-title { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 2px; }
.publication-title strong, .publication-title span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.publication-title strong { font-size: 14px; }
.publication-title span { color: var(--text-3); font-size: 11px; }
.publication-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; margin: 16px 0 12px; }
.publication-facts div { min-width: 0; }
.publication-facts dt { margin-bottom: 3px; color: var(--text-3); font-size: 10px; }
.publication-facts dd { margin: 0; overflow: hidden; color: var(--text-2); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.secret-value { letter-spacing: 0; }
.endpoint-row { display: flex; min-width: 0; align-items: center; gap: 7px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); color: var(--text-3); }
.endpoint-row span { min-width: 0; overflow: hidden; font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.service-warning { margin: 10px 0 0; color: var(--warning); font-size: 11px; line-height: 1.5; }
.publication-actions { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: auto; padding-top: 14px; }
.publication-actions .el-switch { margin-left: auto; }
.readonly-notice { margin-bottom: 16px; }
.standard-mcp-notice { margin-bottom: 16px; }
.dialog-error { margin-bottom: 14px; }
.full-width { width: 100%; }
.candidate-option { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.config-label { display: block; margin: 16px 0 7px; color: var(--text-2); font-size: 12px; font-weight: 700; }
.config-output :deep(.el-textarea__inner) { font: 12px/1.65 'Cascadia Code', Consolas, monospace; }

@media (max-width: 1100px) {
  .publication-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 680px) {
  .publication-toolbar { flex-direction: column; }
  .publication-toolbar .el-button { width: 100%; }
  .publication-grid { grid-template-columns: 1fr; }
  .publication-actions .el-switch { margin-left: 0; }
  .publication-form :deep(.el-form-item) { display: block; }
  .publication-form :deep(.el-form-item__label) { width: auto !important; height: auto; margin-bottom: 6px; line-height: 1.4; }
  .publication-form :deep(.el-form-item__content) { margin-left: 0 !important; }
}
</style>
