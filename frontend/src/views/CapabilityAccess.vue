<template>
  <div class="page access-page">
    <header class="page-header">
      <div>
        <h1>发布与接入</h1>
        <p class="sub">按场景定义发布能力，并为外部 Agent 配置受控调用入口</p>
      </div>
      <el-button v-if="manifest" plain @click="downloadManifest">
        <el-icon><Download /></el-icon>导出清单
      </el-button>
    </header>

    <section class="access-context" aria-label="发布上下文">
      <label>
        <span>业务场景</span>
        <el-select v-model="scenarioId" filterable placeholder="选择场景" :loading="loadingScenarios">
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
      </label>
      <label>
        <span>环境</span>
        <el-radio-group v-model="environment" aria-label="发布环境">
          <el-radio-button value="dev">开发</el-radio-button>
          <el-radio-button value="staging">预发布</el-radio-button>
          <el-radio-button value="prod">生产</el-radio-button>
        </el-radio-group>
      </label>
    </section>

    <el-alert
      v-if="manifestError"
      ref="manifestErrorRef"
      class="manifest-error"
      type="warning"
      :title="manifestError"
      :closable="false"
      show-icon
      tabindex="-1"
    />

    <section v-if="manifest" class="deployment-band" aria-label="当前发布定义">
      <div>
        <span>定义来源</span>
        <strong>{{ manifest.deployment.definition_source === 'release' ? '已发布快照' : '开发中定义' }}</strong>
      </div>
      <div>
        <span>Release</span>
        <strong class="mono">{{ manifest.deployment.release_id ? shortId(manifest.deployment.release_id) : '未固定' }}</strong>
      </div>
      <div>
        <span>Definition hash</span>
        <strong class="mono" :title="manifest.deployment.definition_hash">{{ shortHash(manifest.deployment.definition_hash) }}</strong>
      </div>
      <div>
        <span>能力</span>
        <strong>{{ readyCount }} / {{ manifest.capabilities.length }} 就绪</strong>
      </div>
      <div class="deployment-actions">
        <el-tag :type="manifestReady ? 'success' : 'warning'" effect="light">
          {{ manifestReady ? '清单检查通过' : '存在发布阻塞' }}
        </el-tag>
        <el-button
          v-if="canWithdrawRelease"
          plain
          type="danger"
          :loading="withdrawingRelease"
          @click="withdrawCurrentRelease"
        >
          <el-icon><CircleClose /></el-icon>撤下发布
        </el-button>
      </div>
    </section>

    <el-tabs v-model="activeTab" class="access-tabs">
      <el-tab-pane label="协议适配器" name="adapters">
        <div v-loading="loadingManifest" class="adapter-grid">
          <article v-for="adapter in manifest?.adapters || []" :key="adapter.protocol" class="adapter-panel">
            <header>
              <span class="adapter-icon"><el-icon><component :is="adapter.protocol === 'rest' ? 'Link' : 'Connection'" /></el-icon></span>
              <div>
                <h2>{{ adapter.protocol === 'rest' ? 'REST API v2' : 'Capability MCP' }}</h2>
                <span>{{ manifest?.deployment.environment.toUpperCase() }} · {{ manifest?.deployment.definition_source === 'release' ? 'Release 固定' : 'Live definition' }}</span>
              </div>
              <el-tag size="small" :type="manifestReady ? 'success' : 'warning'">{{ manifestReady ? '可接入' : '需检查' }}</el-tag>
            </header>
            <div class="endpoint-row">
              <code>{{ absoluteUrl(adapter.endpoint) }}</code>
              <el-button text circle title="复制端点" aria-label="复制端点" @click="copyText(absoluteUrl(adapter.endpoint))">
                <el-icon><DocumentCopy /></el-icon>
              </el-button>
            </div>
            <dl>
              <div><dt>认证头</dt><dd class="mono">{{ adapter.authentication.header }}</dd></div>
              <div><dt>Scopes</dt><dd>{{ adapter.required_scopes.join(' · ') }}</dd></div>
              <div v-if="adapter.optional_scopes?.length"><dt>可选 Scopes</dt><dd>{{ adapter.optional_scopes.join(' · ') }}</dd></div>
              <div v-if="adapter.managed_input_upload"><dt>附件上传</dt><dd class="mono">{{ absoluteUrl(adapter.managed_input_upload) }}</dd></div>
              <div v-if="adapter.tools.length"><dt>Tools</dt><dd>{{ adapter.tools.join(' · ') }}</dd></div>
            </dl>
            <pre>{{ adapterSnippet(adapter.protocol) }}</pre>
            <el-button plain @click="copyText(adapterSnippet(adapter.protocol))">
              <el-icon><DocumentCopy /></el-icon>复制配置
            </el-button>
          </article>
          <el-empty v-if="!loadingManifest && !manifest" description="请选择可解析的场景与环境" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="集成密钥" name="keys">
        <section class="keys-section" v-loading="loadingKeys">
          <header class="section-toolbar">
            <div>
              <h2>Integration API keys</h2>
              <span>REST 与 MCP 可复用同一组 capabilities scopes</span>
            </div>
            <el-button v-if="canManage" type="primary" @click="openCreateKey">
              <el-icon><Plus /></el-icon>新建密钥
            </el-button>
          </header>
          <el-alert v-if="!canManage" type="info" title="当前账户无密钥管理权限" :closable="false" show-icon />
          <el-table v-else :data="keys" empty-text="暂无集成密钥">
            <el-table-column label="名称" min-width="170">
              <template #default="{ row }"><strong>{{ row.name }}</strong></template>
            </el-table-column>
            <el-table-column label="标识" min-width="150">
              <template #default="{ row }"><code>{{ row.key_prefix }}…{{ row.token_hint }}</code></template>
            </el-table-column>
            <el-table-column label="Scopes" min-width="250">
              <template #default="{ row }">
                <el-tag v-for="scope in row.scopes" :key="scope" size="small" effect="plain">{{ scope }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '有效' : '已撤销' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="到期" min-width="150">
              <template #default="{ row }">{{ formatDate(row.expires_at) }}</template>
            </el-table-column>
            <el-table-column label="最近使用" min-width="150">
              <template #default="{ row }">{{ formatDate(row.last_used_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="row.status === 'active'"
                  text
                  type="danger"
                  :aria-label="`撤销密钥 ${row.name}`"
                  @click="revokeKey(row)"
                ><el-icon><Delete /></el-icon>撤销</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Manifest 检查" name="manifest">
        <div v-loading="loadingManifest" class="manifest-tab">
          <section v-if="manifest" class="manifest-section">
          <div class="check-list" aria-label="清单安全检查">
            <div v-for="check in manifest.checks" :key="check.code" :class="{ failed: !check.passed }">
              <el-icon><component :is="check.passed ? 'CircleCheck' : 'WarningFilled'" /></el-icon>
              <span>{{ checkLabel(check.code) }}</span>
              <strong>{{ check.passed ? '通过' : check.count ? `${check.count} 项阻塞` : '未通过' }}</strong>
            </div>
          </div>
          <el-table :data="manifest.capabilities" empty-text="当前定义没有可发布能力">
            <el-table-column prop="name" label="能力" min-width="180" />
            <el-table-column prop="kind" label="类型" width="110" />
            <el-table-column label="输入端口" min-width="180">
              <template #default="{ row }">{{ row.data_ports.map((port: any) => port.key).join(' · ') || '无数据端口' }}</template>
            </el-table-column>
            <el-table-column label="副作用" width="100">
              <template #default="{ row }">{{ row.side_effect ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag :type="row.ready ? 'success' : 'warning'">{{ row.ready ? '就绪' : '阻塞' }}</el-tag></template>
            </el-table-column>
          </el-table>
          <div class="manifest-id"><span>Manifest ID</span><code>{{ manifest.manifest_id }}</code></div>
          </section>
          <el-empty v-else-if="!loadingManifest" description="当前环境尚无可检查的发布清单" />
        </div>
      </el-tab-pane>

    </el-tabs>

    <el-dialog v-model="createKeyVisible" title="新建集成密钥" width="min(560px, 94vw)" @closed="resetKeyForm">
      <div v-if="keyFormError" ref="keyErrorRef" class="form-error" role="alert" tabindex="-1">{{ keyFormError }}</div>
      <el-form label-position="top" @submit.prevent="createKey">
        <el-form-item label="名称" required>
          <el-input v-model="keyForm.name" maxlength="120" placeholder="如：客户工作台生产接入" />
        </el-form-item>
        <el-form-item label="Scopes" required>
          <el-checkbox-group v-model="keyForm.scopes">
            <el-checkbox value="capabilities:read">发现与回执</el-checkbox>
            <el-checkbox value="capabilities:invoke">调用能力</el-checkbox>
            <el-checkbox value="assets:write">上传临时附件</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="有效期">
          <el-input-number v-model="keyForm.expires_in_days" :min="1" :max="365" controls-position="right" />
          <span class="days-unit">天</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createKeyVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingKey" @click="createKey">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="secretVisible" title="集成密钥已创建" width="min(680px, 94vw)" :close-on-click-modal="false" @closed="clearSecret">
      <el-alert type="warning" title="密钥仅显示一次" :closable="false" show-icon />
      <div class="secret-row">
        <el-input :model-value="createdSecret" readonly class="mono" />
        <el-button type="primary" aria-label="复制新密钥" @click="copyText(createdSecret)"><el-icon><DocumentCopy /></el-icon>复制</el-button>
      </div>
      <pre v-if="manifest">{{ secretMcpConfig }}</pre>
      <template #footer><el-button type="primary" @click="secretVisible = false">我已妥善保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { capabilityAccessApi } from '@/api/capabilityAccess'
import { useAuthStore } from '@/stores/auth'
import type { Scenario } from '@/types'
import type {
  CapabilityAccessManifest,
  ExternalApiScope,
  IntegrationKey,
} from '@/types/capabilityAccess'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const scenarios = ref<Scenario[]>([])
const scenarioId = ref('')
const environment = ref<'dev' | 'staging' | 'prod'>('dev')
const manifest = ref<CapabilityAccessManifest | null>(null)
const manifestError = ref('')
const manifestErrorRef = ref()
const loadingScenarios = ref(false)
const loadingManifest = ref(false)
const activeTab = ref('adapters')
const keys = ref<IntegrationKey[]>([])
const loadingKeys = ref(false)
const createKeyVisible = ref(false)
const creatingKey = ref(false)
const keyFormError = ref('')
const keyErrorRef = ref<HTMLElement>()
const secretVisible = ref(false)
const createdSecret = ref('')
const withdrawingRelease = ref(false)
let manifestRequest = 0

const keyForm = reactive<{
  name: string
  scopes: ExternalApiScope[]
  expires_in_days: number
}>({
  name: '',
  scopes: ['capabilities:read', 'capabilities:invoke'],
  expires_in_days: 90,
})

const readyCount = computed(() => manifest.value?.capabilities.filter((item) => item.ready).length || 0)
const manifestReady = computed(() => Boolean(manifest.value?.checks.every((check) => check.passed)))
const canWithdrawRelease = computed(() => Boolean(
  canManage.value
  && manifest.value
  && environment.value !== 'dev'
  && manifest.value.deployment.definition_source === 'release'
  && manifest.value.release_history.some((item) => (
    item.environment === environment.value && item.status === 'released'
  )),
))
const secretMcpConfig = computed(() => {
  const adapter = manifest.value?.adapters.find((item) => item.protocol === 'mcp')
  if (!adapter) return ''
  return JSON.stringify({
    mcpServers: {
      'ontology-capabilities': {
        url: absoluteUrl(adapter.endpoint),
        headers: { Authorization: `Bearer ${createdSecret.value}` },
      },
    },
  }, null, 2)
})

function queryText(value: unknown) {
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}

async function loadScenarios() {
  loadingScenarios.value = true
  try {
    scenarios.value = await api.listScenarios()
    const requested = queryText(route.query.scenario_id)
    scenarioId.value = scenarios.value.some((item) => item.id === requested)
      ? requested
      : scenarios.value[0]?.id || ''
    const requestedEnvironment = queryText(route.query.environment)
    if (requestedEnvironment === 'dev' || requestedEnvironment === 'staging' || requestedEnvironment === 'prod') {
      environment.value = requestedEnvironment
    }
  } finally {
    loadingScenarios.value = false
  }
}

async function loadManifest() {
  const requestId = ++manifestRequest
  manifest.value = null
  manifestError.value = ''
  if (!scenarioId.value) return
  loadingManifest.value = true
  try {
    const loaded = await capabilityAccessApi.getManifest(scenarioId.value, environment.value)
    if (requestId === manifestRequest) manifest.value = loaded
  } catch (error: any) {
    if (requestId !== manifestRequest) return
    manifestError.value = error?.message || '接入清单解析失败'
    void nextTick(() => manifestErrorRef.value?.$el?.focus?.())
  } finally {
    if (requestId === manifestRequest) loadingManifest.value = false
  }
}

async function loadKeys() {
  if (!canManage.value) return
  loadingKeys.value = true
  try {
    keys.value = await capabilityAccessApi.listKeys()
  } catch (error: any) {
    ElMessage.error(error?.message || '集成密钥加载失败')
  } finally {
    loadingKeys.value = false
  }
}

function openCreateKey() {
  keyFormError.value = ''
  createKeyVisible.value = true
}

function resetKeyForm() {
  keyForm.name = ''
  keyForm.scopes = ['capabilities:read', 'capabilities:invoke']
  keyForm.expires_in_days = 90
  keyFormError.value = ''
}

async function createKey() {
  if (!keyForm.name.trim()) keyFormError.value = '请输入密钥名称'
  else if (!keyForm.scopes.length) keyFormError.value = '至少选择一个 scope'
  else keyFormError.value = ''
  if (keyFormError.value) {
    void nextTick(() => keyErrorRef.value?.focus())
    return
  }
  creatingKey.value = true
  try {
    const created = await capabilityAccessApi.createKey({
      name: keyForm.name.trim(),
      scopes: keyForm.scopes,
      expires_in_days: keyForm.expires_in_days,
    })
    createdSecret.value = created.token
    createKeyVisible.value = false
    secretVisible.value = true
    await loadKeys()
  } catch (error: any) {
    keyFormError.value = error?.message || '密钥创建失败'
    void nextTick(() => keyErrorRef.value?.focus())
  } finally {
    creatingKey.value = false
  }
}

async function revokeKey(key: IntegrationKey) {
  try {
    await ElMessageBox.confirm(`撤销“${key.name}”后，使用该密钥的 REST 与 MCP 调用会立即失败。`, '撤销集成密钥', {
      type: 'warning',
      confirmButtonText: '确认撤销',
    })
    await capabilityAccessApi.revokeKey(key.id)
    await loadKeys()
    ElMessage.success('密钥已撤销')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || '撤销失败')
  }
}

async function withdrawCurrentRelease() {
  if (!scenarioId.value || environment.value === 'dev' || !canWithdrawRelease.value) return
  const selectedEnvironment = environment.value
  try {
    const prompt = await ElMessageBox.prompt(
      `撤下 ${selectedEnvironment.toUpperCase()} 发布后，该环境将拒绝新的能力调用；Release、快照和历史回执仍会保留。`,
      '撤下当前环境发布',
      {
        type: 'warning',
        confirmButtonText: '确认撤下',
        cancelButtonText: '取消',
        inputPlaceholder: '填写撤下原因',
        inputValidator: (value: string) => value.trim() ? true : '撤下原因不能为空',
      },
    )
    withdrawingRelease.value = true
    const result = await capabilityAccessApi.withdrawRelease(
      scenarioId.value,
      selectedEnvironment,
      prompt.value.trim(),
    )
    ElMessage.success(result.changed ? '当前环境发布已撤下' : '当前环境发布此前已撤下')
    await loadManifest()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error?.message || '撤下发布失败')
    }
  } finally {
    withdrawingRelease.value = false
  }
}

function clearSecret() {
  createdSecret.value = ''
}

function absoluteUrl(value: string) {
  if (/^https?:\/\//i.test(value)) return value
  return new URL(value, window.location.origin).toString()
}

function adapterSnippet(protocol: 'rest' | 'mcp') {
  const adapter = manifest.value?.adapters.find((item) => item.protocol === protocol)
  if (!adapter) return ''
  if (protocol === 'rest') {
    return `curl -H "X-API-Key: <integration-key>" "${absoluteUrl(adapter.discovery || adapter.endpoint)}"`
  }
  return JSON.stringify({
    mcpServers: {
      'ontology-capabilities': {
        url: absoluteUrl(adapter.endpoint),
        headers: { Authorization: 'Bearer <integration-key>' },
      },
    },
  }, null, 2)
}

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.error('复制失败，请检查浏览器剪贴板权限')
  }
}

function downloadManifest() {
  if (!manifest.value) return
  const blob = new Blob([JSON.stringify(manifest.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `capability-manifest-${manifest.value.scenario.id}-${manifest.value.deployment.environment}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function checkLabel(code: string) {
  return ({
    definition_resolved: '运行定义可解析',
    release_pinned: '非开发环境已固定 Release',
    capabilities_ready: '能力就绪检查',
    runtime_bindings_excluded: '未包含运行数据绑定',
    credentials_excluded: '未包含凭据',
  } as Record<string, string>)[code] || code
}

function shortId(value: string) {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}
function shortHash(value: string) {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : '—'
}
function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

watch([scenarioId, environment], async () => {
  await router.replace({
    query: {
      ...route.query,
      scenario_id: scenarioId.value || undefined,
      environment: environment.value,
    },
  })
  void loadManifest()
})

onMounted(async () => {
  await Promise.all([loadScenarios(), loadKeys()])
  await loadManifest()
})
</script>

<style scoped>
.access-page { max-width: 1320px; margin: 0 auto; padding: 28px 30px 56px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.page-header h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
.page-header .sub { margin: 7px 0 0; color: var(--text-3); font-size: 13px; }
.access-context { display: flex; align-items: end; gap: 18px; margin-top: 24px; padding: 16px 0; border-block: 1px solid var(--border); }
.access-context label { display: flex; min-width: min(380px, 46vw); flex-direction: column; gap: 7px; color: var(--text-2); font-size: 12px; font-weight: 650; }
.access-context label + label { min-width: 0; }
.manifest-error { margin-top: 16px; }
.deployment-band { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)) auto; align-items: center; gap: 18px; margin-top: 18px; padding: 14px 16px; border-left: 3px solid var(--primary); background: var(--surface-2); }
.deployment-band > div { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.deployment-band > .deployment-actions { align-items: flex-end; flex-direction: row; justify-content: flex-end; }
.deployment-band span { color: var(--text-3); font-size: 10px; text-transform: uppercase; }
.deployment-band strong { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.access-tabs { margin-top: 20px; }
.adapter-grid { display: grid; min-height: 260px; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; padding-top: 8px; }
.adapter-panel { min-width: 0; padding: 18px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.adapter-panel > header { display: flex; align-items: center; gap: 10px; }
.adapter-panel header > div { min-width: 0; flex: 1; }
.adapter-panel h2, .section-toolbar h2, .legacy-section h2 { margin: 0; font-size: 15px; letter-spacing: 0; }
.adapter-panel header span, .section-toolbar span { color: var(--text-3); font-size: 11px; }
.adapter-icon { display: inline-flex; width: 34px; height: 34px; align-items: center; justify-content: center; border-radius: 6px; background: var(--primary-soft); color: var(--primary); }
.endpoint-row { display: flex; align-items: center; gap: 8px; margin: 16px 0 12px; padding: 9px 10px; border: 1px solid var(--border); background: var(--surface-2); }
.endpoint-row code { min-width: 0; flex: 1; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.adapter-panel dl { display: grid; gap: 8px; margin: 0 0 14px; }
.adapter-panel dl div { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 10px; }
.adapter-panel dt { color: var(--text-3); font-size: 11px; }
.adapter-panel dd { min-width: 0; margin: 0; overflow-wrap: anywhere; font-size: 11px; }
.adapter-panel pre, .secret-row + pre { max-height: 190px; overflow: auto; padding: 12px; border-radius: 6px; background: var(--code-bg, #101a24); color: var(--code-text, #d8e4ec); font-size: 11px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.keys-section, .manifest-section { padding-top: 8px; }
.manifest-tab { min-height: 260px; }
.section-toolbar, .legacy-section { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.section-toolbar > div { display: flex; flex-direction: column; gap: 4px; }
.keys-section :deep(.el-tag) { margin: 2px 4px 2px 0; }
.check-list { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 1px; margin-bottom: 18px; background: var(--border); }
.check-list > div { display: grid; min-height: 92px; place-items: center; gap: 4px; padding: 12px; background: var(--surface); color: var(--success, #16845b); text-align: center; }
.check-list > div.failed { color: var(--warning, #b36a00); }
.check-list span { color: var(--text-2); font-size: 11px; }
.check-list strong { font-size: 11px; }
.manifest-id { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); font-size: 11px; }
.manifest-id span { color: var(--text-3); }
.manifest-id code { overflow-wrap: anywhere; }
.legacy-section { margin-top: 8px; padding: 20px 0; border-block: 1px solid var(--border); }
.legacy-section p { margin: 6px 0 0; color: var(--text-3); font-size: 12px; }
.form-error { margin-bottom: 14px; padding: 10px 12px; border-left: 3px solid var(--danger, #d14343); background: var(--danger-soft, #fff1f1); color: var(--danger, #d14343); font-size: 12px; }
.days-unit { margin-left: 8px; color: var(--text-3); font-size: 12px; }
.secret-row { display: flex; gap: 8px; margin: 16px 0; }
.mono, code, pre { font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); }
@media (max-width: 900px) {
  .access-page { padding: 22px 18px 48px; }
  .deployment-band { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .deployment-band > .deployment-actions { align-items: flex-start; flex-direction: column; }
  .adapter-grid { grid-template-columns: 1fr; }
  .check-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .page-header, .access-context, .section-toolbar, .legacy-section { align-items: stretch; flex-direction: column; }
  .access-context label { min-width: 0; width: 100%; }
  .access-context :deep(.el-radio-group) { display: grid; grid-template-columns: repeat(3, 1fr); }
  .access-context :deep(.el-radio-button__inner) { width: 100%; }
  .deployment-band, .check-list { grid-template-columns: 1fr; }
  .secret-row { flex-direction: column; }
}
</style>
