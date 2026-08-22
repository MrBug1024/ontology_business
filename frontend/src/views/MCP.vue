<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>MCP 服务</h1>
        <div class="sub">Model Context Protocol 工具服务，供已配置的操作和工作流连接外部工具</div>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建 MCP</el-button>
    </div>

    <el-alert
      v-if="!canManage"
      class="readonly-notice"
      type="info"
      title="当前账户为只读：可查看 MCP 配置，不能新建、修改、测试或查看远端工具。"
      show-icon
      :closable="false"
    />

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="m in mcps" :key="m.id">
        <div class="card mcp-card">
          <div class="mc-head">
            <div class="mc-icon"><el-icon :size="20"><Connection /></el-icon></div>
            <div class="mc-title">
              <div class="mc-name">{{ m.name }}</div>
              <el-tag size="small" type="info" effect="light">{{ transportLabel(m.transport) }}</el-tag>
            </div>
            <el-switch v-if="canManage" :model-value="!!m.enabled" @change="(v:any)=>toggle(m, v)" style="margin-left:auto" />
            <el-tag v-else size="small" :type="m.enabled ? 'success' : 'info'" effect="light">{{ m.enabled ? '已启用' : '已停用' }}</el-tag>
          </div>
          <div class="muted mono mc-cmd">
            {{ m.transport === 'stdio' ? `${m.command} ${(m.args||[]).join(' ')}` : m.url }}
          </div>
          <div v-if="canManage" class="mc-actions">
            <el-button size="small" plain @click="test(m)" :loading="m._testing"><el-icon><Link /></el-icon> 测试</el-button>
            <el-button size="small" type="primary" plain @click="showTools(m)"><el-icon><Tools /></el-icon> 工具</el-button>
            <el-button size="small" text type="primary" @click="openEdit(m)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(m)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </div>
      </el-col>
    </el-row>
    <div v-if="!loading && !mcps.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><Connection /></el-icon></div>
      <div>{{ canManage ? '暂无 MCP 服务，点击右上角「新建 MCP」接入工具服务' : '暂无可查看的 MCP 服务' }}</div>
      <el-button v-if="canManage" type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建 MCP</el-button>
    </div>

    <!-- 编辑对话框 -->
    <el-dialog v-if="canManage" v-model="dlg" :title="form.id ? '编辑 MCP' : '新建 MCP'" width="600px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：文件系统、天气服务" /></el-form-item>
        <el-form-item label="传输方式" required>
          <el-radio-group v-model="form.transport">
            <el-radio value="stdio">本地进程</el-radio>
            <el-radio value="sse">事件流</el-radio>
            <el-radio value="streamable_http">流式 HTTP</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.transport === 'stdio'">
          <el-form-item label="命令" required><el-input v-model="form.command" placeholder="如：npx 或 python" /></el-form-item>
          <el-form-item label="启动参数">
            <el-select v-model="form.args" multiple filterable allow-create default-first-option placeholder="输入一项后按回车添加" style="width:100%" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="URL" required><el-input v-model="form.url" placeholder="http://host:port/sse" /></el-form-item>
        </template>
        <el-form-item label="环境变量"><KeyValueEditor v-model="form.env" key-placeholder="变量名" value-placeholder="变量值" empty-text="没有额外环境变量" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 工具列表 -->
    <el-dialog v-if="canManage" v-model="toolsDlg" :title="'MCP 工具：' + (curMcp?.name || '')" width="640px" top="6vh">
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
import { computed, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { MCPConfig, MCPTool } from '@/types'
import KeyValueEditor from '@/components/KeyValueEditor.vue'

const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const mcps = ref<(MCPConfig & { _testing?: boolean })[]>([])
const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<MCPConfig>>({ transport: 'stdio', enabled: true })

const toolsDlg = ref(false)
const curMcp = ref<MCPConfig | null>(null)
const tools = ref<MCPTool[]>([])
const loadingTools = ref(false)

async function load() {
  loading.value = true
  try {
    mcps.value = await api.listMCP()
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function openCreate() {
  if (!canManage.value) return
  form.value = { name: '', transport: 'stdio', command: '', args: [], env: {}, enabled: true }
  dlg.value = true
}
function openEdit(m: MCPConfig) {
  if (!canManage.value) return
  form.value = { ...m, args: [...(m.args || [])], env: { ...(m.env || {}) } }
  dlg.value = true
}
function transportLabel(transport?: string) {
  return ({ stdio: '本地进程', sse: '事件流', streamable_http: '流式 HTTP' } as Record<string, string>)[transport || ''] || '外部工具'
}
async function save() {
  if (!canManage.value) return
  if (!form.value.name) return ElMessage.warning('请填写名称')
  const payload: any = { ...form.value }
  payload.args = form.value.transport === 'stdio' ? [...(form.value.args || [])] : []
  payload.env = { ...(form.value.env || {}) }
  saving.value = true
  try {
    if (form.value.id) await api.updateMCP(form.value.id, payload)
    else await api.createMCP(payload)
    ElMessage.success('已保存')
    dlg.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function toggle(m: MCPConfig, v: boolean) {
  if (!canManage.value) return
  try {
    await api.updateMCP(m.id!, { ...m, enabled: v })
    m.enabled = v
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function test(m: MCPConfig & { _testing?: boolean }) {
  if (!canManage.value) return
  m._testing = true
  try {
    const r: any = await api.testMCP(m.id!)
    ElMessage.success(r.message || '连接成功')
  } catch (e: any) {
    ElMessage.error('连接失败：' + e.message)
  } finally {
    m._testing = false
  }
}
async function showTools(m: MCPConfig) {
  if (!canManage.value) return
  curMcp.value = m
  tools.value = []
  toolsDlg.value = true
  loadingTools.value = true
  try {
    tools.value = await api.mcpTools(m.id!)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loadingTools.value = false
  }
}
async function remove(m: MCPConfig) {
  if (!canManage.value) return
  try {
    await ElMessageBox.confirm(`删除 MCP「${m.name}」？`, '确认', { type: 'warning' })
    await api.deleteMCP(m.id!)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}
onMounted(load)
</script>

<style scoped>
.mcp-card {
  margin-bottom: 16px;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.mcp-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.mc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.mc-icon {
  width: 40px; height: 40px;
  border-radius: 11px;
  background: var(--warning-soft);
  color: var(--warning);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.mc-title { flex: 1; min-width: 0; }
.mc-name {
  font-weight: 700; font-size: 15px; margin-bottom: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.mc-cmd {
  margin: 8px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mc-actions {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.readonly-notice { margin-bottom: 16px; }
</style>
