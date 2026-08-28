<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>MCP 服务</h1>
        <div class="sub">{{ activeSection === 'connected' ? '接入外部工具服务，供操作和工作流调用' : '把已验证 Agent 发布为第三方可直接调用的 MCP 服务' }}</div>
      </div>
      <div v-if="canManage && activeSection === 'connected'" class="page-actions">
        <el-button plain @click="openImport"><el-icon><DocumentAdd /></el-icon> 导入 mcpServers 配置</el-button>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建 MCP</el-button>
      </div>
    </div>

    <el-tabs v-model="activeSection" class="mcp-sections">
      <el-tab-pane label="接入的 MCP" name="connected" />
      <el-tab-pane label="Agent 发布" name="published" />
    </el-tabs>

    <section v-if="activeSection === 'connected'">

    <el-alert
      v-if="!canManage"
      class="readonly-notice"
      type="info"
      title="当前账户为只读：可查看 MCP 配置，不能新建、修改、测试或查看远端工具。"
      show-icon
      :closable="false"
    />

    <el-row :gutter="16" v-loading="loading">
      <el-col v-for="m in mcps" :key="m.id" :xs="24" :sm="12" :lg="8">
        <div class="card mcp-card">
          <div class="mc-head">
            <div class="mc-icon"><el-icon :size="20"><Connection /></el-icon></div>
            <div class="mc-title">
              <div class="mc-name">{{ m.name }}</div>
              <el-tag size="small" type="info" effect="light">{{ transportLabel(m.transport) }}</el-tag>
            </div>
            <el-switch v-if="canManage" :model-value="!!m.enabled" aria-label="启用 MCP" @change="(v:any)=>toggle(m, v)" />
            <el-tag v-else size="small" :type="m.enabled ? 'success' : 'info'" effect="light">{{ m.enabled ? '已启用' : '已停用' }}</el-tag>
          </div>
          <div class="muted mono mc-cmd" :title="displayEndpoint(m)">{{ displayEndpoint(m) }}</div>
          <div v-if="m.transport !== 'stdio' && Object.keys(m.headers || {}).length" class="credential-summary">
            <el-icon aria-hidden="true"><Lock /></el-icon>
            已配置 {{ Object.keys(m.headers || {}).length }} 个请求头
          </div>
          <div v-if="canManage" class="mc-actions">
            <el-button size="small" plain :loading="m._testing" @click="test(m)"><el-icon><Link /></el-icon> 测试</el-button>
            <el-button size="small" type="primary" plain @click="showTools(m)"><el-icon><Tools /></el-icon> 工具</el-button>
            <el-button size="small" text type="primary" @click="openEdit(m)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(m)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </div>
      </el-col>
    </el-row>
    <div v-if="!loading && !mcps.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><Connection /></el-icon></div>
      <div>{{ canManage ? '暂无 MCP 服务，可新建或导入常见客户端的 mcpServers 配置' : '暂无可查看的 MCP 服务' }}</div>
      <div v-if="canManage" class="empty-actions">
        <el-button plain size="small" @click="openImport"><el-icon><DocumentAdd /></el-icon> 导入 mcpServers 配置</el-button>
        <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建 MCP</el-button>
      </div>
    </div>
    </section>

    <AgentMCPPublications v-else :can-manage="canManage" />

    <el-dialog v-if="canManage" v-model="dlg" :title="form.id ? '编辑 MCP' : '新建 MCP'" width="min(720px, 94vw)" @closed="clearFormErrors">
      <div v-if="formError" ref="formErrorRef" class="form-error-summary" role="alert" tabindex="-1">
        <el-icon aria-hidden="true"><WarningFilled /></el-icon>
        <div><strong>请先修正配置</strong><span>{{ formError }}</span></div>
      </div>
      <el-form :model="form" label-width="96px" @submit.prevent="save">
        <el-form-item label="名称" required :error="formErrors.name">
          <el-input v-model="form.name" placeholder="如：文档检索、天气服务" @blur="validateForm" />
        </el-form-item>
        <el-form-item label="传输方式" required :error="formErrors.transport">
          <el-radio-group v-model="form.transport" @change="onTransportChange">
            <el-radio value="stdio">本地进程（需运维开启）</el-radio>
            <el-radio value="sse">SSE（兼容）</el-radio>
            <el-radio value="streamable_http">HTTP（推荐）</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.transport === 'stdio'">
          <el-form-item label="命令" required :error="formErrors.command">
            <el-input v-model="form.command" placeholder="如：npx 或 python" @blur="validateForm" />
          </el-form-item>
          <el-form-item label="启动参数">
            <el-select v-model="form.args" multiple filterable allow-create default-first-option placeholder="输入一项后按回车添加" class="full-width" />
          </el-form-item>
          <el-form-item label="环境变量" :error="formErrors.env">
            <KeyValueEditor
              v-model="form.env"
              string-only
              flat-keys
              mask-values
              key-placeholder="变量名"
              value-placeholder="变量值（已配置的值留空可保持）"
              empty-text="没有额外环境变量"
            />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="URL" required :error="formErrors.url">
            <el-input v-model="form.url" placeholder="https://example.com/mcp" @blur="validateForm" />
          </el-form-item>
          <el-form-item label="请求头" :error="formErrors.headers">
            <div class="field-stack">
              <KeyValueEditor
                v-model="form.headers"
                string-only
                flat-keys
                mask-values
                key-placeholder="请求头名称"
                value-placeholder="请求头值，如 Bearer Token"
                empty-text="没有额外请求头"
              />
              <span class="field-help">请求头值不会回显；编辑时留空表示保持原值，删除整行表示移除该请求头。</span>
            </div>
          </el-form-item>
        </template>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-if="canManage" v-model="importDlg" title="导入 mcpServers 配置" width="min(780px, 94vw)" @closed="resetImport">
      <div class="import-intro">
        <div>
          <strong>兼容 mcpServers 配置</strong>
          <span>支持一次导入多个 stdio、SSE 或 type=http 的 Streamable HTTP 服务。</span>
        </div>
        <el-button size="small" text type="primary" @click="insertImportExample">填入示例</el-button>
      </div>
      <label class="json-label" for="mcp-standard-json">配置 JSON</label>
      <el-input
        id="mcp-standard-json"
        v-model="importText"
        type="textarea"
        :rows="12"
        resize="vertical"
        spellcheck="false"
        class="json-input"
        placeholder='{"mcpServers":{"service":{"type":"http","url":"https://example.com/mcp","headers":{"Authorization":"Bearer ..."}}}}'
        @input="clearImportPreview"
      />
      <div v-if="importError" ref="importErrorRef" class="form-error-summary import-error" role="alert" tabindex="-1">
        <el-icon aria-hidden="true"><WarningFilled /></el-icon>
        <div><strong>配置未通过校验</strong><span>{{ importError }}</span></div>
      </div>
      <section v-if="importResult" class="import-preview" aria-label="标准配置校验结果">
        <header>
          <div><strong>校验通过</strong><span>将处理 {{ importResult.items.length }} 个服务；密钥值不会出现在预览中。</span></div>
          <el-tag type="success" effect="plain">可导入</el-tag>
        </header>
        <div class="import-service-list">
          <article v-for="item in importResult.items" :key="item.name" class="import-service">
            <div class="import-service-main">
              <strong>{{ item.name }}</strong>
              <span class="mono">{{ item.endpoint }}</span>
            </div>
            <div class="import-service-meta">
              <el-tag size="small" effect="plain">{{ transportLabel(item.transport) }}</el-tag>
              <el-tag v-if="item.header_keys.length" size="small" type="warning" effect="plain">请求头 {{ item.header_keys.join('、') }}</el-tag>
              <el-tag v-if="item.env_keys.length" size="small" type="info" effect="plain">环境变量 {{ item.env_keys.length }}</el-tag>
              <el-tag v-if="item.action !== 'create'" size="small" :type="item.action === 'replace' ? 'warning' : 'info'" effect="plain">{{ item.action === 'replace' ? '将替换' : '将跳过' }}</el-tag>
            </div>
          </article>
        </div>
      </section>
      <div class="import-policy">
        <label for="mcp-conflict-policy">遇到同名服务</label>
        <el-select id="mcp-conflict-policy" v-model="conflictPolicy" @change="clearImportPreview">
          <el-option label="停止导入（推荐）" value="error" />
          <el-option label="跳过已存在服务" value="skip" />
          <el-option label="替换已存在服务" value="replace" />
        </el-select>
      </div>
      <el-alert title="导入只保存配置，不会自动连接或调用远端工具；保存后请逐条测试。" type="info" :closable="false" show-icon />
      <template #footer>
        <el-button @click="importDlg = false">取消</el-button>
        <el-button :loading="validatingImport" @click="validateImport">校验配置</el-button>
        <el-button type="primary" :loading="importing" :disabled="!importResult" @click="performImport">确认导入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-if="canManage" v-model="toolsDlg" :title="'MCP 工具：' + (curMcp?.name || '')" width="min(680px, 94vw)" top="6vh">
      <el-table :data="tools" size="small" v-loading="loadingTools">
        <el-table-column prop="name" label="工具名" min-width="160">
          <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="240" show-overflow-tooltip />
      </el-table>
      <el-empty v-if="!loadingTools && !tools.length" description="未获取到工具" :image-size="60" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { MCPConfig, MCPImportResult, MCPTool } from '@/types'
import KeyValueEditor from '@/components/KeyValueEditor.vue'
import AgentMCPPublications from '@/components/AgentMCPPublications.vue'
import { parseStandardMCPConfig } from '@/utils/mcpConfig'

type MCPForm = {
  id?: string
  name: string
  transport: 'stdio' | 'sse' | 'streamable_http'
  command: string
  args: string[]
  url: string
  env: Record<string, string>
  headers: Record<string, string>
  enabled: boolean
}

const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const activeSection = ref<'connected' | 'published'>('connected')
const mcps = ref<(MCPConfig & { _testing?: boolean })[]>([])
const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<MCPForm>(emptyForm())
const formErrors = reactive({ name: '', transport: '', command: '', url: '', env: '', headers: '' })
const formError = ref('')
const formErrorRef = ref<HTMLElement>()

const importDlg = ref(false)
const importText = ref('')
const importError = ref('')
const importErrorRef = ref<HTMLElement>()
const importResult = ref<MCPImportResult | null>(null)
const conflictPolicy = ref<'error' | 'skip' | 'replace'>('error')
const validatingImport = ref(false)
const importing = ref(false)

const toolsDlg = ref(false)
const curMcp = ref<MCPConfig | null>(null)
const tools = ref<MCPTool[]>([])
const loadingTools = ref(false)

function emptyForm(): MCPForm {
  return { name: '', transport: 'streamable_http', command: '', args: [], url: '', env: {}, headers: {}, enabled: true }
}

function configPayload(source: Partial<MCPForm | MCPConfig>, enabled = source.enabled !== false) {
  const transport = source.transport === 'sse' || source.transport === 'streamable_http' ? source.transport : 'stdio'
  return {
    name: String(source.name || '').trim(),
    transport,
    command: transport === 'stdio' ? String(source.command || '').trim() : '',
    args: transport === 'stdio' ? (source.args || []).map(String) : [],
    url: transport === 'stdio' ? '' : String(source.url || '').trim(),
    env: transport === 'stdio' ? Object.fromEntries(Object.entries(source.env || {}).map(([key, value]) => [key, String(value)])) : {},
    headers: transport === 'stdio' ? {} : Object.fromEntries(Object.entries(source.headers || {}).map(([key, value]) => [key, String(value)])),
    enabled,
  }
}

async function load() {
  loading.value = true
  try {
    mcps.value = await api.listMCP()
  } catch (error: any) {
    ElMessage.error('加载失败：' + error.message)
  } finally {
    loading.value = false
  }
}

function clearFormErrors() {
  Object.keys(formErrors).forEach((key) => { formErrors[key as keyof typeof formErrors] = '' })
  formError.value = ''
}

function openCreate() {
  if (!canManage.value) return
  clearFormErrors()
  form.value = emptyForm()
  dlg.value = true
}

function openEdit(m: MCPConfig) {
  if (!canManage.value) return
  clearFormErrors()
  form.value = {
    id: m.id,
    name: m.name,
    transport: m.transport === 'sse' ? 'sse' : m.transport === 'stdio' ? 'stdio' : 'streamable_http',
    command: m.command || '',
    args: [...(m.args || [])],
    url: m.url || '',
    env: { ...(m.env || {}) },
    headers: { ...(m.headers || {}) },
    enabled: m.enabled !== false,
  }
  dlg.value = true
}

function onTransportChange() {
  clearFormErrors()
  if (form.value.transport === 'stdio') {
    form.value.url = ''
    form.value.headers = {}
  } else {
    form.value.command = ''
    form.value.args = []
    form.value.env = {}
  }
}

function transportLabel(transport?: string) {
  return ({ stdio: '本地进程', sse: 'SSE（兼容）', streamable_http: 'Streamable HTTP', http: 'Streamable HTTP' } as Record<string, string>)[transport || ''] || '外部工具'
}

function displayEndpoint(m: MCPConfig) {
  if (m.transport === 'stdio') return `${m.command || ''} ${(m.args || []).join(' ')}`.trim()
  try {
    const url = new URL(m.url || '')
    return `${url.origin}${url.pathname}`
  } catch {
    return m.url || '未配置 URL'
  }
}

function validateForm() {
  clearFormErrors()
  const value = configPayload(form.value)
  if (!value.name) formErrors.name = '请输入 MCP 服务名称'
  if (value.transport === 'stdio' && !value.command) formErrors.command = '本地进程必须填写启动命令'
  if (value.transport !== 'stdio') {
    if (!value.url) formErrors.url = '远程 MCP 必须填写 URL'
    else {
      try {
        const parsed = new URL(value.url)
        if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) throw new Error()
        if (parsed.username || parsed.password) formErrors.url = 'URL 不能包含用户凭据，请使用请求头'
      } catch {
        formErrors.url = '请输入完整的 HTTP 或 HTTPS 地址'
      }
    }
  }
  const first = Object.values(formErrors).find(Boolean) || ''
  formError.value = first
  return !first
}

async function save() {
  if (!canManage.value) return
  if (!validateForm()) {
    await nextTick()
    formErrorRef.value?.focus()
    return
  }
  const payload = configPayload(form.value)
  saving.value = true
  try {
    if (form.value.id) await api.updateMCP(form.value.id, payload)
    else await api.createMCP(payload)
    ElMessage.success('MCP 配置已保存')
    dlg.value = false
    await load()
  } catch (error: any) {
    const message = error.message || '保存失败'
    if (/headers|请求头/i.test(message)) formErrors.headers = message
    if (/\benv\b|环境变量/i.test(message)) formErrors.env = message
    if (/\burl\b|地址|目标主机/i.test(message)) formErrors.url = message
    formError.value = message
    await nextTick()
    formErrorRef.value?.focus()
  } finally {
    saving.value = false
  }
}

async function toggle(m: MCPConfig, enabled: boolean) {
  if (!canManage.value || !m.id) return
  try {
    await api.updateMCP(m.id, configPayload(m, enabled))
    m.enabled = enabled
  } catch (error: any) {
    ElMessage.error(error.message || '启用状态更新失败')
  }
}

async function test(m: MCPConfig & { _testing?: boolean }) {
  if (!canManage.value || !m.id) return
  m._testing = true
  try {
    const result: any = await api.testMCP(m.id)
    if (result?.ok === false) throw new Error(result.message || '连接失败')
    ElMessage.success(result?.message || '连接成功')
  } catch (error: any) {
    ElMessage.error('连接失败：' + (error.message || '请求失败'))
  } finally {
    m._testing = false
  }
}

async function showTools(m: MCPConfig) {
  if (!canManage.value || !m.id) return
  curMcp.value = m
  tools.value = []
  toolsDlg.value = true
  loadingTools.value = true
  try {
    tools.value = await api.mcpTools(m.id)
  } catch (error: any) {
    ElMessage.error(error.message || '获取工具失败')
  } finally {
    loadingTools.value = false
  }
}

async function remove(m: MCPConfig) {
  if (!canManage.value || !m.id) return
  try {
    await ElMessageBox.confirm(`删除 MCP「${m.name}」？`, '确认', { type: 'warning' })
    await api.deleteMCP(m.id)
    ElMessage.success('已删除')
    await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || '删除失败')
  }
}

function openImport() {
  if (!canManage.value) return
  resetImport()
  importDlg.value = true
}

function resetImport() {
  importText.value = ''
  importError.value = ''
  importResult.value = null
  conflictPolicy.value = 'error'
}

function clearImportPreview() {
  importError.value = ''
  importResult.value = null
}

function insertImportExample() {
  importText.value = JSON.stringify({
    mcpServers: {
      'example-service': {
        type: 'http',
        url: 'https://example.com/mcp',
        headers: { Authorization: 'Bearer <your-token>' },
      },
    },
  }, null, 2)
  clearImportPreview()
}

async function showImportError(error: unknown) {
  importResult.value = null
  importError.value = error instanceof Error ? error.message : '配置校验失败'
  await nextTick()
  importErrorRef.value?.focus()
}

async function validateImport() {
  validatingImport.value = true
  try {
    const parsed = parseStandardMCPConfig(importText.value)
    importResult.value = await api.importMCP(parsed.payload, { dryRun: true, conflictPolicy: conflictPolicy.value })
    importError.value = ''
  } catch (error) {
    await showImportError(error)
  } finally {
    validatingImport.value = false
  }
}

async function performImport() {
  if (!importResult.value || importing.value) return
  try {
    if (conflictPolicy.value === 'replace' && importResult.value.items.some((item) => item.action === 'replace')) {
      await ElMessageBox.confirm('替换会覆盖同名服务的地址、请求头和启用状态，是否继续？', '确认替换 MCP', {
        type: 'warning', confirmButtonText: '确认替换', cancelButtonText: '取消',
      })
    }
    importing.value = true
    const parsed = parseStandardMCPConfig(importText.value)
    const result = await api.importMCP(parsed.payload, { conflictPolicy: conflictPolicy.value })
    ElMessage.success(`导入完成：新建 ${result.created}，替换 ${result.replaced}，跳过 ${result.skipped}`)
    importDlg.value = false
    await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') await showImportError(error)
  } finally {
    importing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page-actions, .empty-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mcp-sections { min-width: 0; }
.mcp-card { margin-bottom: 16px; transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease); }
.mcp-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-md); border-color: var(--border-strong); }
.mc-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.mc-icon { width: 40px; height: 40px; border-radius: 11px; background: var(--warning-soft); color: var(--warning); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.mc-title { flex: 1; min-width: 0; }
.mc-name { margin-bottom: 3px; overflow: hidden; font-size: 15px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.mc-cmd { margin: 8px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.credential-summary { display: flex; align-items: center; gap: 5px; margin: 7px 0; color: var(--text-3); font-size: 11px; }
.mc-actions { display: flex; gap: 4px; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--border); }
.readonly-notice { margin-bottom: 16px; }
.full-width { width: 100%; }
.field-stack { display: grid; gap: 7px; width: 100%; }
.field-help { color: var(--text-3); font-size: 11px; line-height: 1.5; }
.form-error-summary { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 14px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border)); border-radius: 9px; color: var(--danger); background: var(--danger-soft); outline: none; }
.form-error-summary > div { display: flex; flex-direction: column; gap: 2px; }
.form-error-summary strong { font-size: 12px; }
.form-error-summary span { font-size: 11px; line-height: 1.5; }
.import-intro, .import-preview > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.import-intro { margin-bottom: 14px; }
.import-intro > div, .import-preview > header > div { display: flex; flex-direction: column; gap: 4px; }
.import-intro strong, .import-preview strong { color: var(--text); font-size: 13px; }
.import-intro span, .import-preview header span { color: var(--text-3); font-size: 11px; line-height: 1.5; }
.json-label { display: block; margin-bottom: 6px; color: var(--text-2); font-size: 12px; font-weight: 700; }
.json-input :deep(.el-textarea__inner) { font: 12px/1.65 'Cascadia Code', Consolas, monospace; }
.import-error { margin-top: 12px; margin-bottom: 0; }
.import-preview { display: grid; gap: 10px; margin-top: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }
.import-service-list { display: grid; gap: 7px; }
.import-service { display: grid; gap: 7px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.import-service-main { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; }
.import-service-main span { min-width: 0; overflow: hidden; color: var(--text-3); font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.import-service-meta { display: flex; gap: 5px; flex-wrap: wrap; }
.import-policy { display: grid; grid-template-columns: 110px minmax(0, 260px); align-items: center; gap: 10px; margin: 14px 0; }
.import-policy label { color: var(--text-2); font-size: 12px; font-weight: 700; }

@media (max-width: 680px) {
  .page-actions { width: 100%; }
  .page-actions .el-button { flex: 1; margin-left: 0; }
  .import-service-main { align-items: flex-start; flex-direction: column; }
  .import-policy { grid-template-columns: 1fr; }
}
</style>
