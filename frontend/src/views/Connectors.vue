<template>
  <main class="page connector-page" aria-labelledby="connector-page-title">
    <header class="page-header connector-header">
      <div>
        <span class="eyebrow">CONNECTOR GOVERNANCE</span>
        <h1 id="connector-page-title">连接器与环境</h1>
        <p class="sub">把已配置的数据源、MCP 服务和 LLM 部署安全地绑定到场景环境；凭据始终留在原配置中。</p>
      </div>
      <div class="header-actions">
        <span class="runtime-context" data-testid="connector-runtime-environment" title="由服务端部署配置决定，不能在页面中切换">
          本实例运行于 <b>{{ environmentLabel(runtimeEnvironment) }}</b>
        </span>
        <el-select
          v-model="scenarioId"
          aria-label="选择需要管理连接器的业务场景"
          class="scenario-select"
          :disabled="loading || !scenarios.length"
          placeholder="选择业务场景"
          data-testid="connector-scenario-select"
          @change="changeScenario"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button :loading="loading" :disabled="!scenarioId" data-testid="connector-refresh" @click="loadConnections">
          <el-icon aria-hidden="true"><Refresh /></el-icon>刷新
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      class="connector-alert"
      type="error"
      :title="error"
      show-icon
      closable
      role="alert"
      @close="error = ''"
    />
    <p v-if="feedback" class="connector-feedback" role="status" aria-live="polite">
      <el-icon aria-hidden="true"><CircleCheckFilled /></el-icon>{{ feedback }}
    </p>

    <section v-if="!loading && !scenarios.length" class="card empty-card" aria-labelledby="connector-empty-title">
      <el-icon aria-hidden="true" :size="30"><Connection /></el-icon>
      <h3 id="connector-empty-title">暂无可管理的业务场景</h3>
      <p>先创建一个场景并配置数据源、MCP 或 LLM，才能建立环境绑定。</p>
    </section>

    <template v-else-if="scenarioId">
      <section class="environment-card card" aria-labelledby="environment-title">
        <div>
          <span class="eyebrow">TARGET ENVIRONMENT</span>
          <h3 id="environment-title">选择目标环境</h3>
          <p>同一个外部引用可在开发、预发布和生产环境绑定到不同的实际连接器；这里管理目标绑定，不会切换当前实例的运行环境。</p>
        </div>
        <el-radio-group v-model="environment" class="environment-switch" aria-label="目标环境" @change="changeEnvironment">
          <el-radio-button v-for="item in environments" :key="item.id" :value="item.id">{{ item.label }}</el-radio-button>
        </el-radio-group>
      </section>

      <el-alert
        v-if="pendingRequirement"
        class="requirement-alert"
        type="warning"
        :closable="false"
        show-icon
        data-testid="connector-requirement-alert"
      >
        <template #title>资源包预检要求处理外部绑定</template>
        <p>“{{ pendingRequirement.label }}”需要在 {{ environmentLabel(environment) }} 选择一个同租户、范围匹配的 {{ kindLabel(pendingRequirement.kind) }}，并完成显式健康检查。</p>
      </el-alert>

      <section class="binding-layout">
        <article class="card binding-form-card" aria-labelledby="binding-form-title">
          <header class="section-head">
            <div>
              <span class="eyebrow">BIND + VERIFY</span>
              <h3 id="binding-form-title">创建或更新环境绑定</h3>
            </div>
            <el-tag type="info" effect="plain">{{ environmentLabel(environment) }}</el-tag>
          </header>
          <p class="form-intro">保存后可立即健康检查。检查可能连接外部服务，因此不会在发布时自动执行。</p>
          <el-form label-position="top" @submit.prevent="saveBinding">
            <el-form-item label="外部引用键" required>
              <el-input v-model.trim="bindingForm.binding_key" maxlength="180" show-word-limit placeholder="例如 data_source:orders:sqlite" data-testid="connector-binding-key" />
              <p class="field-help">由资源包预检带入时请保持不变；它不包含运行时 ID 或凭据。</p>
            </el-form-item>
            <el-form-item label="连接器类型" required>
              <el-select v-model="bindingForm.kind" placeholder="选择连接器类型" data-testid="connector-kind-select" @change="bindingForm.connector_id = ''">
                <el-option v-for="kind in connectorKinds" :key="kind.id" :label="kind.label" :value="kind.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标连接器" required>
              <el-select
                v-model="bindingForm.connector_id"
                placeholder="选择已有的同租户连接器"
                :disabled="!candidateConnectors.length"
                data-testid="connector-target-select"
              >
                <el-option
                  v-for="connector in candidateConnectors"
                  :key="connector.id"
                  :label="`${connector.name} · ${connector.adapter_type || '未声明适配器'}`"
                  :value="connector.id"
                >
                  <div class="connector-option">
                    <span>{{ connector.name }}</span>
                    <small>{{ connector.adapter_type || '未声明适配器' }} · {{ healthLabel(connector.health) }}</small>
                  </div>
                </el-option>
              </el-select>
              <p v-if="!candidateConnectors.length" class="field-help field-help--warning">当前场景没有可绑定的 {{ kindLabel(bindingForm.kind) }}。请先到来源配置页创建一个连接器。</p>
            </el-form-item>
            <el-form-item label="用途说明">
              <el-input v-model.trim="bindingForm.reference_label" maxlength="300" placeholder="例如：客户资源包的开发环境数据源" />
            </el-form-item>
            <el-checkbox v-model="bindingForm.check" class="verify-check">保存后立即运行健康检查</el-checkbox>
            <p class="field-help">结果只记录健康状态和已去敏的诊断摘要，不会回显密码、Token、请求头或 API Key。</p>
            <div class="form-actions">
              <el-button type="primary" native-type="submit" :loading="saving" :disabled="!canSaveBinding" data-testid="connector-save-binding">
                <el-icon aria-hidden="true"><Connection /></el-icon>{{ bindingForm.check ? '验证并保存绑定' : '保存绑定' }}
              </el-button>
              <el-button text type="primary" @click="resetBindingForm">清空表单</el-button>
            </div>
          </el-form>
        </article>

        <aside class="card guidance-card" aria-labelledby="guidance-title">
          <span class="eyebrow">SAFE DELIVERY</span>
          <h3 id="guidance-title">发布前会再次检查</h3>
          <ol>
            <li><b>选择目标</b><span>只能选择当前租户且作用域适配该场景的连接器。</span></li>
            <li><b>显式验证</b><span>健康检查由你主动发起，避免发布操作意外产生外部调用或模型费用。</span></li>
            <li><b>按环境重检</b><span>合入和发布会再次验证绑定、启用状态、配置签名和健康结果。</span></li>
          </ol>
          <p><el-icon aria-hidden="true"><Lock /></el-icon> 环境绑定只保存逻辑引用和审计状态，不保存凭据。</p>
        </aside>
      </section>

      <section class="card bindings-card" aria-labelledby="bindings-title" v-loading="loadingBindings">
        <header class="section-head">
          <div>
            <span class="eyebrow">ACTIVE BINDINGS</span>
            <h3 id="bindings-title">{{ environmentLabel(environment) }}环境绑定</h3>
          </div>
          <span class="section-note">{{ bindings.length }} 项 · 发布门禁由服务端执行</span>
        </header>
        <el-empty v-if="!bindings.length && !loadingBindings" description="尚未建立环境绑定；可通过左侧表单开始。" :image-size="58" />
        <el-table v-else :data="bindings" class="binding-table" data-testid="connector-bindings-table">
          <el-table-column label="外部引用" min-width="210">
            <template #default="{ row }">
              <strong>{{ row.reference_label || row.binding_key }}</strong>
              <small class="mono">{{ row.binding_key }}</small>
            </template>
          </el-table-column>
          <el-table-column label="目标连接器" min-width="185">
            <template #default="{ row }">
              <span>{{ row.name }}</span>
              <small>{{ kindLabel(row.kind) }} · {{ row.adapter_type || '未声明适配器' }}</small>
            </template>
          </el-table-column>
          <el-table-column label="健康状态" min-width="175">
            <template #default="{ row }">
              <el-tag :type="healthTag(row)" effect="light">{{ row.ready ? '可发布' : healthLabel(row.health) }}</el-tag>
              <small v-if="row.blocking_reason" class="binding-reason">{{ row.blocking_reason }}</small>
              <small v-else-if="row.checked_at">已检查：{{ formatDate(row.checked_at) }}</small>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button text type="primary" :loading="checkingBindingId === row.binding_id" @click="checkBinding(row)">
                <el-icon aria-hidden="true"><Refresh /></el-icon>重新验证
              </el-button>
              <el-button text @click="openSource(row)"><el-icon aria-hidden="true"><ArrowRight /></el-icon>来源配置</el-button>
              <el-popconfirm title="删除后依赖该引用的发布将被服务端阻断，确定删除？" confirm-button-text="删除绑定" cancel-button-text="取消" @confirm="deleteBinding(row)">
                <template #reference><el-button text type="danger" :loading="deletingBindingId === row.binding_id">删除</el-button></template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="card catalog-card" aria-labelledby="catalog-title" v-loading="loadingCatalog">
        <header class="section-head catalog-head">
          <div>
            <span class="eyebrow">REGISTERED TARGETS</span>
            <h3 id="catalog-title">可用连接器目录</h3>
            <p>目录由现有数据源、MCP、LLM 配置归一化而来；不会复制配置或显示密钥。</p>
          </div>
          <el-select v-model="catalogKind" size="small" aria-label="按连接器类型筛选目录">
            <el-option label="全部类型" value="all" />
            <el-option v-for="kind in connectorKinds" :key="kind.id" :label="kind.label" :value="kind.id" />
          </el-select>
        </header>
        <el-empty v-if="!visibleCatalog.length && !loadingCatalog" description="该场景暂无可用连接器；请先创建数据源、MCP 或 LLM 配置。" :image-size="58" />
        <div v-else class="catalog-grid">
          <article v-for="connector in visibleCatalog" :key="`${connector.kind}-${connector.id}`" class="catalog-item">
            <div class="catalog-icon" :class="`catalog-icon--${connector.kind}`" aria-hidden="true"><el-icon><component :is="kindIcon(connector.kind)" /></el-icon></div>
            <div class="catalog-copy">
              <div class="catalog-title"><strong>{{ connector.name }}</strong><el-tag size="small" effect="plain">{{ kindLabel(connector.kind) }}</el-tag></div>
              <p>{{ connector.adapter_type || '未声明适配器' }} · {{ connector.capabilities.join(' / ') || '未声明能力' }}</p>
              <div class="catalog-state"><span :class="`state-dot state-dot--${connector.health}`" aria-hidden="true"></span>{{ healthLabel(connector.health) }} · {{ secretStateLabel(connector.secret_state) }}</div>
            </div>
            <el-button text type="primary" :aria-label="`查看 ${connector.name} 的来源配置`" @click="openSource(connector)">查看配置</el-button>
          </article>
        </div>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { ConnectorBinding, ConnectorCatalogItem, ConnectorKind, Scenario } from '@/types'

type Environment = 'dev' | 'staging' | 'prod'

const route = useRoute()
const router = useRouter()
const scenarios = ref<Scenario[]>([])
const scenarioId = ref('')
const environment = ref<Environment>('dev')
const runtimeEnvironment = ref<Environment>('dev')
const catalog = ref<ConnectorCatalogItem[]>([])
const bindings = ref<ConnectorBinding[]>([])
const loading = ref(false)
const loadingCatalog = ref(false)
const loadingBindings = ref(false)
const saving = ref(false)
const checkingBindingId = ref('')
const deletingBindingId = ref('')
const error = ref('')
const feedback = ref('')
const catalogKind = ref<'all' | ConnectorKind>('all')
const pendingRequirement = ref<{ key: string; kind: ConnectorKind; label: string } | null>(null)
let routeSyncReady = false
let viewDisposed = false
let bootstrapRequest = 0
let connectionRequest = 0
const bindingForm = ref<{ binding_key: string; kind: ConnectorKind; connector_id: string; reference_label: string; check: boolean }>({
  binding_key: '', kind: 'data_source', connector_id: '', reference_label: '', check: true,
})

const environments: Array<{ id: Environment; label: string }> = [
  { id: 'dev', label: '开发 dev' }, { id: 'staging', label: '预发布 staging' }, { id: 'prod', label: '生产 prod' },
]
const connectorKinds: Array<{ id: ConnectorKind; label: string }> = [
  { id: 'data_source', label: '数据源' }, { id: 'mcp', label: 'MCP 服务' }, { id: 'llm', label: 'LLM 部署' },
]
const candidateConnectors = computed(() => catalog.value.filter((item) => item.kind === bindingForm.value.kind && item.enabled))
const visibleCatalog = computed(() => catalogKind.value === 'all' ? catalog.value : catalog.value.filter((item) => item.kind === catalogKind.value))
const canSaveBinding = computed(() => Boolean(
  scenarioId.value && bindingForm.value.binding_key.trim() && bindingForm.value.connector_id && !saving.value,
))

function queryValue(key: string) {
  const value = route.query[key]
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function isEnvironment(value: string): value is Environment { return value === 'dev' || value === 'staging' || value === 'prod' }
function isKind(value: string): value is ConnectorKind { return value === 'data_source' || value === 'mcp' || value === 'llm' }
function kindLabel(kind: string) { return connectorKinds.find((item) => item.id === kind)?.label || kind }
function environmentLabel(value: Environment) { return environments.find((item) => item.id === value)?.label || value }
function healthLabel(value?: string) { return ({ healthy: '健康', unhealthy: '异常', unknown: '待验证' } as Record<string, string>)[value || 'unknown'] || '待验证' }
function secretStateLabel(value?: string) { return ({ configured: '凭据已配置', missing: '缺少凭据', not_required: '无需凭据' } as Record<string, string>)[value || 'not_required'] || '凭据状态未知' }
function healthTag(binding: ConnectorBinding): 'success' | 'danger' | 'warning' | 'info' {
  if (binding.ready || binding.health === 'healthy') return 'success'
  if (binding.health === 'unhealthy') return 'danger'
  return 'warning'
}
function kindIcon(kind: ConnectorKind) { return ({ data_source: 'Coin', mcp: 'Connection', llm: 'ChatDotRound' } as Record<ConnectorKind, string>)[kind] }
function formatDate(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function toErrorMessage(cause: unknown, fallback: string) { return cause instanceof Error ? cause.message : fallback }

function hydrateRequirement() {
  pendingRequirement.value = null
  const requestedEnvironment = queryValue('environment')
  const requestedKey = queryValue('binding_key')
  const requestedKind = queryValue('kind')
  const requestedLabel = queryValue('reference_label')
  if (isEnvironment(requestedEnvironment)) environment.value = requestedEnvironment
  if (requestedKey && isKind(requestedKind)) {
    pendingRequirement.value = { key: requestedKey, kind: requestedKind, label: requestedLabel || requestedKey }
    bindingForm.value = {
      binding_key: requestedKey,
      kind: requestedKind,
      connector_id: '',
      reference_label: requestedLabel || '',
      check: true,
    }
  }
}

function requirementKey() {
  const requirement = pendingRequirement.value
  return requirement ? `${requirement.key}|${requirement.kind}|${requirement.label}` : ''
}

async function syncRouteContext() {
  if (!routeSyncReady) return
  const requestedScenario = queryValue('scenario_id')
  const nextScenario = scenarios.value.some((item) => item.id === requestedScenario)
    ? requestedScenario
    : scenarios.value[0]?.id || ''
  const requestedEnvironment = queryValue('environment')
  const nextEnvironment: Environment = isEnvironment(requestedEnvironment) ? requestedEnvironment : 'dev'
  const scenarioChanged = nextScenario !== scenarioId.value
  const environmentChanged = nextEnvironment !== environment.value
  const previousRequirement = requirementKey()
  scenarioId.value = nextScenario
  environment.value = nextEnvironment
  hydrateRequirement()
  const requirementChanged = previousRequirement !== requirementKey()
  if ((scenarioChanged || requirementChanged) && !pendingRequirement.value) resetBindingForm()
  feedback.value = ''
  if (scenarioChanged) return
  else if (environmentChanged) await loadBindings()
}

async function bootstrap() {
  const request = ++bootstrapRequest
  loading.value = true
  error.value = ''
  try {
    const [availableScenarios, runtime] = await Promise.all([
      api.listReleaseScenarios(),
      api.getRuntimeEnvironment(),
    ])
    if (viewDisposed || request !== bootstrapRequest) return
    scenarios.value = availableScenarios
    if (isEnvironment(runtime.environment)) runtimeEnvironment.value = runtime.environment
    const requestedScenario = queryValue('scenario_id')
    scenarioId.value = scenarios.value.some((item) => item.id === requestedScenario)
      ? requestedScenario
      : scenarios.value[0]?.id || ''
    if (scenarioId.value && requestedScenario !== scenarioId.value) {
      await router.replace({ query: { ...route.query, scenario_id: scenarioId.value } })
      return
    }
    hydrateRequirement()
    if (scenarioId.value) await loadConnections()
  } catch (cause) {
    if (!viewDisposed && request === bootstrapRequest) error.value = toErrorMessage(cause, '加载连接器场景失败')
  } finally {
    if (!viewDisposed && request === bootstrapRequest) loading.value = false
  }
}
async function loadConnections() {
  const targetScenario = scenarioId.value
  const targetEnvironment = environment.value
  if (!targetScenario) return
  const request = ++connectionRequest
  loadingCatalog.value = true
  loadingBindings.value = true
  error.value = ''
  try {
    const [targets, bound] = await Promise.all([
      api.listConnectors(targetScenario),
      api.listConnectorBindings(targetScenario, targetEnvironment),
    ])
    if (
      viewDisposed
      || request !== connectionRequest
      || targetScenario !== scenarioId.value
      || targetEnvironment !== environment.value
    ) return
    catalog.value = targets
    bindings.value = bound
  } catch (cause) {
    if (!viewDisposed && request === connectionRequest) error.value = toErrorMessage(cause, '加载连接器目录或环境绑定失败')
  } finally {
    if (!viewDisposed && request === connectionRequest) {
      loadingCatalog.value = false
      loadingBindings.value = false
    }
  }
}
async function loadBindings() {
  const targetScenario = scenarioId.value
  const targetEnvironment = environment.value
  if (!targetScenario) return
  const request = ++connectionRequest
  loadingBindings.value = true
  error.value = ''
  try {
    const bound = await api.listConnectorBindings(targetScenario, targetEnvironment)
    if (
      viewDisposed
      || request !== connectionRequest
      || targetScenario !== scenarioId.value
      || targetEnvironment !== environment.value
    ) return
    bindings.value = bound
  } catch (cause) {
    if (!viewDisposed && request === connectionRequest) error.value = toErrorMessage(cause, '加载环境绑定失败')
  } finally {
    if (!viewDisposed && request === connectionRequest) loadingBindings.value = false
  }
}
async function changeScenario() {
  const query = { ...route.query, scenario_id: scenarioId.value }
  await router.replace({ query })
  return
}
async function changeEnvironment() {
  await router.replace({ query: { ...route.query, environment: environment.value } })
  await loadBindings()
}
function resetBindingForm() {
  bindingForm.value = {
    binding_key: pendingRequirement.value?.key || '',
    kind: pendingRequirement.value?.kind || 'data_source',
    connector_id: '',
    reference_label: pendingRequirement.value?.label || '',
    check: true,
  }
}
async function saveBinding() {
  if (!canSaveBinding.value) return
  saving.value = true
  error.value = ''
  try {
    const saved = await api.saveConnectorBinding(scenarioId.value, {
      environment: environment.value,
      binding_key: bindingForm.value.binding_key.trim(),
      kind: bindingForm.value.kind,
      connector_id: bindingForm.value.connector_id,
      reference_label: bindingForm.value.reference_label.trim() || undefined,
      check: bindingForm.value.check,
    })
    feedback.value = saved.ready
      ? `已保存并验证「${saved.reference_label || saved.binding_key}」，该环境绑定可用于受治理导入和发布。`
      : `已保存绑定，但尚未通过健康检查：${saved.blocking_reason || '请修复后重新验证。'}`
    if (pendingRequirement.value?.key === saved.binding_key && pendingRequirement.value.kind === saved.kind) pendingRequirement.value = null
    await loadConnections()
  } catch (cause) {
    error.value = toErrorMessage(cause, '保存环境绑定失败')
  } finally {
    saving.value = false
  }
}
async function checkBinding(binding: ConnectorBinding) {
  checkingBindingId.value = binding.binding_id
  error.value = ''
  try {
    const checked = await api.checkConnectorBinding(scenarioId.value, binding.binding_id)
    feedback.value = checked.ready
      ? `「${checked.reference_label || checked.binding_key}」已通过健康检查。`
      : `健康检查未通过：${checked.blocking_reason || '请检查来源配置后重试。'}`
    await loadConnections()
  } catch (cause) {
    error.value = toErrorMessage(cause, '健康检查失败')
  } finally {
    checkingBindingId.value = ''
  }
}
async function deleteBinding(binding: ConnectorBinding) {
  deletingBindingId.value = binding.binding_id
  error.value = ''
  try {
    await api.deleteConnectorBinding(scenarioId.value, binding.binding_id)
    feedback.value = `已删除「${binding.reference_label || binding.binding_key}」；依赖它的发布会在服务端明确被阻断。`
    await loadConnections()
  } catch (cause) {
    error.value = toErrorMessage(cause, '删除环境绑定失败')
  } finally {
    deletingBindingId.value = ''
  }
}
function openSource(connector: { id?: string; connector_id?: string; kind: ConnectorKind }) {
  const name = connector.kind === 'data_source' ? 'data-sources' : connector.kind === 'mcp' ? 'mcp' : 'llm'
  const selectedKey = connector.kind === 'data_source' ? 'source_id' : 'connector_id'
  const connectorId = connector.id || connector.connector_id || ''
  void router.push({
    name,
    query: {
      scenario_id: scenarioId.value,
      [selectedKey]: connectorId,
      environment: environment.value,
      return_to: route.fullPath,
    },
  })
}

watch(() => route.fullPath, () => { void syncRouteContext() })
onMounted(async () => {
  viewDisposed = false
  await bootstrap()
  if (viewDisposed) return
  routeSyncReady = true
})
onBeforeUnmount(() => {
  viewDisposed = true
  routeSyncReady = false
  bootstrapRequest += 1
  connectionRequest += 1
})
</script>

<style scoped>
.connector-page { max-width: 1440px; margin: 0 auto; padding-bottom: 34px; }
.connector-header, .header-actions, .section-head, .catalog-head, .catalog-title, .catalog-state, .form-actions { display: flex; align-items: center; }
.connector-header { justify-content: space-between; gap: 20px; }
.connector-header h1 { margin: 4px 0 7px; font-size: 24px; }
.connector-header .sub { max-width: 720px; }
.header-actions { gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.runtime-context { padding: 6px 9px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-2); color: var(--text-2); font-size: 12px; white-space: nowrap; }
.runtime-context b { color: var(--primary); font-weight: 700; }
.scenario-select { width: min(300px, 74vw); }
.connector-alert, .requirement-alert { margin: 0 0 14px; }
.connector-feedback { display: flex; align-items: center; gap: 7px; color: var(--success, #15803d); margin: 0 0 14px; font-size: 13px; }
.empty-card { min-height: 220px; display: grid; place-content: center; text-align: center; gap: 8px; color: var(--text-2); }
.empty-card h3 { color: var(--text); margin: 4px 0; }
.empty-card p { margin: 0; max-width: 420px; }
.environment-card { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.environment-card h3, .guidance-card h3, .section-head h3 { margin: 4px 0 5px; }
.environment-card p, .form-intro, .catalog-head p { margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.55; }
.environment-switch { flex-shrink: 0; }
.binding-layout { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, .75fr); gap: 16px; margin-bottom: 16px; }
.binding-form-card, .guidance-card, .bindings-card, .catalog-card, .environment-card { border: 1px solid var(--border); }
.section-head { justify-content: space-between; gap: 14px; margin-bottom: 16px; }
.section-head h3 { font-size: 18px; }
.form-intro { margin-bottom: 16px; }
.field-help { margin: 6px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.45; }
.field-help--warning { color: var(--warning, #a16207); }
.verify-check { margin: 2px 0 0; }
.form-actions { gap: 8px; margin-top: 18px; }
.guidance-card { background: linear-gradient(145deg, var(--surface, #fff), var(--surface-2)); }
.guidance-card ol { display: grid; gap: 14px; padding: 0; margin: 18px 0; list-style: none; counter-reset: governance; }
.guidance-card li { display: grid; grid-template-columns: 28px 1fr; column-gap: 10px; align-items: start; }
.guidance-card li::before { counter-increment: governance; content: counter(governance); display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary-soft); color: var(--primary); font-size: 12px; font-weight: 750; }
.guidance-card li b { grid-column: 2; color: var(--text); font-size: 13px; }
.guidance-card li span { grid-column: 2; color: var(--text-2); font-size: 12px; line-height: 1.55; margin-top: 2px; }
.guidance-card > p { display: flex; align-items: flex-start; gap: 7px; padding-top: 14px; border-top: 1px solid var(--border); color: var(--text-2); font-size: 12px; line-height: 1.55; }
.bindings-card, .catalog-card { margin-bottom: 16px; overflow: hidden; }
.section-note { color: var(--text-3); font-size: 12px; }
.binding-table :deep(.el-table__cell) { vertical-align: top; }
.binding-table strong, .binding-table small { display: block; }
.binding-table small { margin-top: 3px; color: var(--text-3); font-size: 11px; line-height: 1.35; overflow-wrap: anywhere; }
.binding-table .binding-reason { color: var(--warning, #a16207); max-width: 280px; }
.catalog-head { align-items: flex-start; }
.catalog-head > div { min-width: 0; }
.catalog-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
.catalog-item { display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 13px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); }
.catalog-icon { display: grid; place-items: center; width: 38px; height: 38px; border-radius: 11px; color: var(--primary); background: var(--primary-soft); }
.catalog-icon--mcp { color: #0f766e; background: color-mix(in srgb, #14b8a6 14%, var(--surface)); }
.catalog-icon--llm { color: #7c3aed; background: color-mix(in srgb, #8b5cf6 14%, var(--surface)); }
.catalog-copy { min-width: 0; }
.catalog-title { gap: 7px; min-width: 0; }
.catalog-title strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.catalog-copy p { margin: 3px 0 5px; color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
.catalog-state { gap: 5px; color: var(--text-3); font-size: 11px; }
.state-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); }
.state-dot--healthy { background: var(--success, #16a34a); }
.state-dot--unhealthy { background: var(--danger, #dc2626); }
.connector-option { display: flex; justify-content: space-between; gap: 12px; }
.connector-option small { color: var(--text-3); }
@media (max-width: 900px) { .binding-layout { grid-template-columns: 1fr; } .environment-card { align-items: flex-start; flex-direction: column; } }
@media (max-width: 640px) { .connector-header { align-items: flex-start; flex-direction: column; } .header-actions { justify-content: flex-start; width: 100%; } .scenario-select { width: 100%; } .environment-switch { width: 100%; display: flex; } .environment-switch :deep(.el-radio-button) { flex: 1; } .environment-switch :deep(.el-radio-button__inner) { width: 100%; padding-inline: 7px; } .catalog-item { grid-template-columns: 40px minmax(0, 1fr); } .catalog-item > .el-button { grid-column: 2; justify-self: start; padding-left: 0; } .binding-table :deep(.el-table__fixed-right) { display: none; } }
@media (prefers-reduced-motion: reduce) { .connector-page *, .connector-page *::before { transition-duration: .01ms !important; animation-duration: .01ms !important; } }
</style>
