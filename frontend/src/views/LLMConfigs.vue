<template>
  <main class="model-page" aria-labelledby="model-title">
    <header class="model-header">
      <div>
        <span class="eyebrow">MODEL OPERATIONS</span>
        <h1 id="model-title">模型与路由</h1>
        <p>配置模型能力、路由优先级和预算；每次调用的延迟、Token 与成本均可审计。</p>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建模型配置</el-button>
    </header>

    <el-alert
      v-if="!canManage"
      class="model-alert"
      type="info"
      title="当前账户为只读：可查看模型、路由、使用情况与评测，不能修改配置或测试连接。"
      show-icon
      :closable="false"
    />

    <section class="route-grid" aria-labelledby="route-title" aria-live="polite">
      <div class="route-title">
        <span class="eyebrow">CAPABILITY ROUTING</span>
        <h3 id="route-title">当前路由</h3>
      </div>
      <button v-for="item in capabilityRoutes" :key="item.capability" type="button" class="route-card" @click="selectCapability(item.capability)">
        <span class="route-icon"><el-icon><component :is="capabilityIcon(item.capability)" /></el-icon></span>
        <span class="route-copy"><small>{{ capabilityLabel(item.capability) }}</small><b>{{ item.config?.name || '未配置' }}</b><em>{{ item.config ? `优先级 ${item.config.routing_priority ?? 100}` : '需要启用可用模型' }}</em></span>
      </button>
    </section>

    <el-alert v-if="error" class="model-alert" type="error" :title="error" show-icon :closable="false" role="alert" />

    <section v-loading="loading" class="config-grid" aria-label="模型配置列表">
      <article v-for="config in llms" :key="config.id" class="model-card card" :class="{ disabled: config.enabled === false }">
        <header class="model-card-head">
          <span class="model-icon"><el-icon><Cpu /></el-icon></span>
          <div class="model-name"><strong>{{ config.name }}</strong><span class="mono">{{ config.model || '未填写模型标识' }}</span></div>
          <div class="card-tags">
            <el-tag v-if="config.is_default" size="small" type="success" effect="light">默认</el-tag>
            <el-tag size="small" :type="config.enabled === false ? 'info' : 'primary'" effect="plain">{{ config.enabled === false ? '已停用' : '已启用' }}</el-tag>
          </div>
        </header>
        <div class="capability-tags" :aria-label="`${config.name} 的模型能力`">
          <el-tag v-for="capability in config.capabilities || []" :key="capability" size="small" effect="plain">{{ capabilityLabel(capability) }}</el-tag>
        </div>
        <dl class="model-meta">
          <div><dt>接入</dt><dd>{{ config.provider || 'openai_compatible' }}</dd></div>
          <div><dt>路由优先级</dt><dd>{{ config.routing_priority ?? 100 }}</dd></div>
          <div><dt>温度 / Token</dt><dd>{{ config.temperature }} / {{ config.max_tokens }}</dd></div>
          <div><dt>输入 / 输出单价</dt><dd>{{ costRate(config.input_cost_per_million) }} / {{ costRate(config.output_cost_per_million) }}</dd></div>
          <div class="wide"><dt>预算</dt><dd>{{ budgetText(config) }}</dd></div>
        </dl>
        <footer class="model-actions">
          <el-button size="small" plain @click="openOperations(config)"><el-icon><DataAnalysis /></el-icon> 使用与评测</el-button>
          <el-button v-if="canManage" size="small" plain :loading="config._testing" @click="test(config)"><el-icon><Link /></el-icon> 测试</el-button>
          <el-button v-if="canManage" size="small" text type="primary" @click="openEdit(config)"><el-icon><Edit /></el-icon> 编辑</el-button>
          <el-button v-if="canManage" size="small" text type="danger" @click="remove(config)"><el-icon><Delete /></el-icon> 删除</el-button>
        </footer>
      </article>
      <div v-if="!loading && !llms.length" class="empty-state card">
        <el-icon :size="30"><Cpu /></el-icon>
        <strong>尚未配置可用模型</strong>
        <span>新建后选择能力和优先级，Agent 与工作流即可按路由使用它。</span>
        <el-button v-if="canManage" type="primary" size="small" @click="openCreate">新建模型配置</el-button>
      </div>
    </section>

    <el-dialog v-if="canManage" v-model="dialogVisible" :title="form.id ? '编辑模型配置' : '新建模型配置'" width="min(720px, calc(100vw - 28px))" destroy-on-close>
      <el-form :model="form" label-position="top" class="model-form">
        <div class="form-grid">
          <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：业务对话主模型" /></el-form-item>
          <el-form-item label="Provider"><el-select v-model="form.provider" style="width:100%"><el-option v-for="provider in providers" :key="provider" :label="provider" :value="provider" /></el-select></el-form-item>
          <el-form-item label="Base URL" required><el-input v-model="form.base_url" class="mono" placeholder="https://api.example.com/v1" /></el-form-item>
          <el-form-item label="模型标识" required><el-input v-model="form.model" class="mono" placeholder="model-name" /></el-form-item>
          <el-form-item class="form-full" :label="form.id ? 'API Key（留空保留原值）' : 'API Key'" :required="!form.id"><el-input v-model="form.api_key" type="password" show-password class="mono" placeholder="密钥只写入服务端，页面不会回显" /></el-form-item>
        </div>
        <section class="form-section" aria-labelledby="capability-title">
          <div><span class="eyebrow">CAPABILITIES</span><h3 id="capability-title">能力与路由</h3></div>
          <el-checkbox-group v-model="form.capabilities" class="capability-checks" aria-label="模型能力">
            <el-checkbox v-for="capability in capabilities" :key="capability" :label="capability">{{ capabilityLabel(capability) }}</el-checkbox>
          </el-checkbox-group>
          <div class="form-grid compact">
            <el-form-item label="路由优先级（数字越小越优先）"><el-input-number v-model="form.routing_priority" :min="0" :max="10000" :step="10" style="width:100%" /></el-form-item>
            <el-form-item label="启用状态"><el-switch v-model="form.enabled" active-text="已启用" inactive-text="已停用" /></el-form-item>
            <el-form-item label="设为默认（需具备对话能力）"><el-switch v-model="form.is_default" /></el-form-item>
          </div>
        </section>
        <section class="form-section" aria-labelledby="runtime-title">
          <div><span class="eyebrow">RUNTIME & COST</span><h3 id="runtime-title">运行参数与成本</h3></div>
          <div class="form-grid compact">
            <el-form-item label="温度"><el-slider v-model="form.temperature" :min="0" :max="2" :step="0.1" show-input /></el-form-item>
            <el-form-item label="最大 Token"><el-input-number v-model="form.max_tokens" :min="1" :max="131072" :step="256" style="width:100%" /></el-form-item>
            <el-form-item label="输入单价 / 百万 Token"><el-input-number v-model="form.input_cost_per_million" :min="0" :precision="6" :step="0.01" style="width:100%" /></el-form-item>
            <el-form-item label="输出单价 / 百万 Token"><el-input-number v-model="form.output_cost_per_million" :min="0" :precision="6" :step="0.01" style="width:100%" /></el-form-item>
            <el-form-item label="预算上限（0 表示不限制）"><el-input-number v-model="form.budget_limit" :min="0" :precision="4" :step="1" style="width:100%" /></el-form-item>
            <el-form-item label="币种"><el-input v-model="form.cost_currency" maxlength="12" class="mono" /></el-form-item>
          </div>
        </section>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
    </el-dialog>

    <el-drawer v-model="operationsVisible" :title="`${activeConfig?.name || '模型'} · 使用与评测`" size="min(880px, 94vw)" destroy-on-close>
      <div v-loading="operationsLoading" class="operations-drawer">
        <el-alert v-if="operationsError" type="error" :title="operationsError" show-icon :closable="false" role="alert" />
        <template v-else-if="activeConfig">
          <section class="usage-grid" aria-label="模型使用概览">
            <div><span>调用次数</span><b>{{ usage?.invocation_count || 0 }}</b></div>
            <div><span>成功率</span><b>{{ successRate }}</b></div>
            <div><span>平均延迟</span><b>{{ Math.round(usage?.average_latency_ms || 0) }} ms</b></div>
            <div><span>已估成本</span><b>{{ money(usage?.estimated_cost || 0, usage?.currency || activeConfig.cost_currency) }}</b></div>
          </section>
          <section class="budget-strip">
            <div><strong>预算使用</strong><span>{{ budgetUsageText }}</span></div>
            <el-progress :percentage="budgetPercentage" :status="budgetPercentage >= 100 ? 'exception' : budgetPercentage >= 80 ? 'warning' : 'success'" :stroke-width="8" />
          </section>
          <el-tabs v-model="operationsTab">
            <el-tab-pane label="调用追踪" name="traces">
              <el-table :data="traces" size="small" empty-text="尚无调用追踪">
                <el-table-column prop="created_at" label="时间" min-width="145"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
                <el-table-column prop="capability" label="能力" width="90"><template #default="{ row }"><el-tag size="small">{{ capabilityLabel(row.capability) }}</el-tag></template></el-table-column>
                <el-table-column prop="status" label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.status === 'succeeded' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">{{ traceStatus(row.status) }}</el-tag></template></el-table-column>
                <el-table-column prop="latency_ms" label="延迟" width="88"><template #default="{ row }">{{ row.latency_ms }} ms</template></el-table-column>
                <el-table-column prop="total_tokens" label="Token" width="88" />
                <el-table-column prop="estimated_cost" label="成本" min-width="100"><template #default="{ row }">{{ money(row.estimated_cost, row.currency) }}</template></el-table-column>
              </el-table>
            </el-tab-pane>
            <el-tab-pane label="基础评测" name="evaluations">
              <div class="evaluation-actions"><span>记录人工或外部评测的非敏感摘要。</span><el-button v-if="canManage" size="small" type="primary" @click="evaluationDialog = true"><el-icon><Plus /></el-icon> 记录评测</el-button></div>
              <el-table :data="evaluations" size="small" empty-text="尚无评测记录">
                <el-table-column prop="name" label="评测" min-width="160" />
                <el-table-column prop="capability" label="能力" width="92"><template #default="{ row }">{{ capabilityLabel(row.capability) }}</template></el-table-column>
                <el-table-column prop="score" label="得分" width="88"><template #default="{ row }">{{ Number(row.score || 0).toFixed(2) }}</template></el-table-column>
                <el-table-column prop="passed" label="结果" width="80"><template #default="{ row }"><el-tag size="small" :type="row.passed ? 'success' : 'danger'">{{ row.passed ? '通过' : '未通过' }}</el-tag></template></el-table-column>
                <el-table-column prop="created_at" label="时间" min-width="140"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
              </el-table>
              <p v-if="evaluationSummary" class="evaluation-summary">累计 {{ evaluationSummary.total }} 次评测，平均得分 {{ Number(evaluationSummary.average_score || 0).toFixed(2) }}，平均延迟 {{ Math.round(evaluationSummary.average_latency_ms || 0) }} ms。</p>
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-if="canManage" v-model="evaluationDialog" title="记录基础评测" width="min(520px, calc(100vw - 28px))" destroy-on-close>
      <el-form :model="evaluationForm" label-position="top">
        <el-form-item label="评测名称" required><el-input v-model="evaluationForm.name" /></el-form-item>
        <div class="form-grid compact"><el-form-item label="能力"><el-select v-model="evaluationForm.capability" style="width:100%"><el-option v-for="capability in capabilities" :key="capability" :label="capabilityLabel(capability)" :value="capability" /></el-select></el-form-item><el-form-item label="通过"><el-switch v-model="evaluationForm.passed" /></el-form-item><el-form-item label="得分（0–1）"><el-input-number v-model="evaluationForm.score" :min="0" :max="1" :step="0.01" style="width:100%" /></el-form-item><el-form-item label="延迟（ms）"><el-input-number v-model="evaluationForm.latency_ms" :min="0" style="width:100%" /></el-form-item></div>
        <el-form-item label="非敏感说明"><el-input v-model="evaluationForm.notes" type="textarea" :rows="3" maxlength="2000" show-word-limit /></el-form-item>
      </el-form>
      <template #footer><el-button @click="evaluationDialog = false">取消</el-button><el-button type="primary" :loading="evaluationSaving" @click="saveEvaluation">保存评测</el-button></template>
    </el-dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { LLMConfig, LLMEvaluation, LLMEvaluationSummary, LLMTrace, LLMUsageSummary } from '@/types'

type ConfigCard = LLMConfig & { _testing?: boolean }
type Capability = 'chat' | 'embedding' | 'vision' | 'tool'
const capabilities: Capability[] = ['chat', 'embedding', 'vision', 'tool']
const providers = ['openai', 'deepseek', 'dashscope', 'ollama', 'vllm', 'openai_compatible']
const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const llms = ref<ConfigCard[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const dialogVisible = ref(false)
const form = ref<Partial<LLMConfig>>(newForm())
const capabilityRoutes = ref<Array<{ capability: Capability; config: LLMConfig | null }>>(capabilities.map((capability) => ({ capability, config: null })))
const operationsVisible = ref(false)
const operationsLoading = ref(false)
const operationsError = ref('')
const operationsTab = ref('traces')
const activeConfig = ref<LLMConfig | null>(null)
const usage = ref<LLMUsageSummary | null>(null)
const traces = ref<LLMTrace[]>([])
const evaluations = ref<LLMEvaluation[]>([])
const evaluationSummary = ref<LLMEvaluationSummary | null>(null)
const evaluationDialog = ref(false)
const evaluationSaving = ref(false)
const evaluationForm = ref<Partial<LLMEvaluation>>(newEvaluation())

function newForm(): Partial<LLMConfig> {
  return { provider: 'openai_compatible', base_url: '', api_key: '', model: '', temperature: 0.2, max_tokens: 4096, is_default: false, capabilities: ['chat', 'tool'], enabled: true, routing_priority: 100, input_cost_per_million: 0, output_cost_per_million: 0, budget_limit: 0, cost_currency: 'USD' }
}
function newEvaluation(): Partial<LLMEvaluation> { return { name: '基础评测', capability: 'chat', passed: true, score: 0, latency_ms: 0, input_tokens: 0, output_tokens: 0, estimated_cost: 0, notes: '' } }
function capabilityLabel(value: string) { return ({ chat: '对话', embedding: '向量', vision: '视觉', tool: '工具' } as Record<string, string>)[value] || value }
function capabilityIcon(value: string) { return ({ chat: 'ChatDotRound', embedding: 'Connection', vision: 'View', tool: 'Tools' } as Record<string, string>)[value] || 'Cpu' }
function costRate(value?: number) { return `${Number(value || 0).toFixed(4)}` }
function money(value?: number, currency = 'USD') { return `${currency || 'USD'} ${Number(value || 0).toFixed(4)}` }
function budgetText(config: LLMConfig) { return Number(config.budget_limit || 0) > 0 ? `${money(config.budget_limit, config.cost_currency)} 上限` : '未设置上限' }
function formatDate(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—' }
function traceStatus(value: string) { return ({ succeeded: '成功', failed: '失败', cancelled: '取消' } as Record<string, string>)[value] || value }
const successRate = computed(() => { const count = usage.value?.invocation_count || 0; return count ? `${Math.round(((usage.value?.succeeded_count || 0) / count) * 100)}%` : '—' })
const budgetPercentage = computed(() => { const limit = usage.value?.budget_limit || 0; return limit > 0 ? Math.min(100, Math.round(((usage.value?.estimated_cost || 0) / limit) * 100)) : 0 })
const budgetUsageText = computed(() => { if (!usage.value?.budget_limit) return '未设置预算上限'; return `${money(usage.value.estimated_cost, usage.value.currency)} / ${money(usage.value.budget_limit, usage.value.currency)}${usage.value.budget_remaining == null ? '' : `，剩余 ${money(usage.value.budget_remaining, usage.value.currency)}`}` })

async function loadRoutes() {
  capabilityRoutes.value = await Promise.all(capabilities.map(async (capability) => {
    try { const route = await api.resolveLLM(capability); return { capability, config: route.selected } } catch { return { capability, config: null } }
  }))
}
async function load() {
  loading.value = true; error.value = ''
  try { llms.value = await api.listLLM(); await loadRoutes() } catch (cause: any) { error.value = cause?.message || '模型配置加载失败' } finally { loading.value = false }
}
function openCreate() { if (canManage.value) { form.value = newForm(); dialogVisible.value = true } }
function openEdit(config: LLMConfig) { if (canManage.value) { form.value = { ...newForm(), ...config, api_key: '' }; dialogVisible.value = true } }
function selectCapability(capability: Capability) { const config = capabilityRoutes.value.find((item) => item.capability === capability)?.config; if (config) openOperations(config) }
async function save() {
  if (!canManage.value) return
  if (!form.value.name || !form.value.base_url || !form.value.model) return ElMessage.warning('请填写名称、Base URL 和模型标识')
  if (!form.value.id && !form.value.api_key) return ElMessage.warning('新建模型配置时请填写 API Key')
  if (!form.value.capabilities?.length) return ElMessage.warning('至少选择一种模型能力')
  saving.value = true
  try { if (form.value.id) await api.updateLLM(form.value.id, form.value); else await api.createLLM(form.value); ElMessage.success('模型配置已保存'); dialogVisible.value = false; await load() } catch (cause: any) { ElMessage.error(cause?.message || '保存失败') } finally { saving.value = false }
}
async function test(config: ConfigCard) { if (!canManage.value) return; config._testing = true; try { const result: any = await api.testLLM(config.id!); ElMessage.success(result.message || '连接成功'); await load() } catch (cause: any) { ElMessage.error(`连接失败：${cause?.message || '未知错误'}`) } finally { config._testing = false } }
async function remove(config: LLMConfig) { if (!canManage.value) return; try { await ElMessageBox.confirm(`删除模型配置「${config.name}」？其历史 trace 和评测也将一并删除。`, '确认删除', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }); await api.deleteLLM(config.id!); ElMessage.success('已删除'); await load() } catch (cause: any) { if (cause !== 'cancel' && cause !== 'close') ElMessage.error(cause?.message || '删除失败') } }
async function openOperations(config: LLMConfig) {
  activeConfig.value = config; operationsVisible.value = true; operationsLoading.value = true; operationsError.value = ''; operationsTab.value = 'traces'
  try { const [nextUsage, nextTraces, nextEvaluations, nextEvaluationSummary] = await Promise.all([api.getLLMUsageSummary(config.id!), api.listLLMTraces(config.id!), api.listLLMEvaluations(config.id!), api.getLLMEvaluationSummary(config.id!)]); usage.value = nextUsage; traces.value = nextTraces; evaluations.value = nextEvaluations; evaluationSummary.value = nextEvaluationSummary } catch (cause: any) { operationsError.value = cause?.message || '使用与评测数据加载失败' } finally { operationsLoading.value = false }
}
async function saveEvaluation() {
  if (!canManage.value) return
  if (!activeConfig.value?.id || !evaluationForm.value.name) return ElMessage.warning('请填写评测名称')
  evaluationSaving.value = true
  try { await api.createLLMEvaluation(activeConfig.value.id, evaluationForm.value); ElMessage.success('评测已记录'); evaluationDialog.value = false; evaluationForm.value = newEvaluation(); await openOperations(activeConfig.value) } catch (cause: any) { ElMessage.error(cause?.message || '评测保存失败') } finally { evaluationSaving.value = false }
}
onMounted(load)
</script>

<style scoped>
.model-page { min-height: 100%; padding: 24px 28px 34px; }
.model-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 18px; }
.eyebrow { color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .15em; }
.model-header h1 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.model-header p { margin: 0; color: var(--text-2); font-size: 13px; }
.route-grid { display: grid; grid-template-columns: minmax(160px, .8fr) repeat(4, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px; }
.route-title, .route-card { min-height: 90px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); box-shadow: var(--shadow-xs); }
.route-title { display: flex; flex-direction: column; justify-content: center; padding: 14px; }.route-title h3 { margin: 5px 0 0; color: var(--text); font-size: 16px; }
.route-card { display: flex; align-items: center; gap: 10px; padding: 12px; color: var(--text); cursor: pointer; text-align: left; transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }.route-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }.route-card:focus-visible, .model-actions :deep(button:focus-visible) { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }
.route-icon, .model-icon { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 11px; background: var(--primary-soft); color: var(--primary); }.route-icon { width: 34px; height: 34px; }.route-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }.route-copy small, .route-copy em { overflow: hidden; color: var(--text-3); font-size: 10px; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }.route-copy b { overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.model-alert { margin-bottom: 12px; }
.config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr)); gap: 14px; }.card { border: 1px solid var(--border); border-radius: 15px; background: var(--surface); box-shadow: var(--shadow-xs); }.model-card { display: flex; flex-direction: column; min-height: 292px; padding: 16px; transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }.model-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }.model-card.disabled { opacity: .72; }.model-card-head { display: flex; align-items: flex-start; gap: 10px; }.model-icon { width: 40px; height: 40px; }.model-name { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 3px; }.model-name strong { overflow: hidden; color: var(--text); font-size: 15px; text-overflow: ellipsis; white-space: nowrap; }.model-name span { overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.card-tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }.capability-tags { display: flex; min-height: 27px; flex-wrap: wrap; gap: 5px; margin: 14px 0 10px; }.model-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin: 0; padding: 12px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }.model-meta div { min-width: 0; }.model-meta .wide { grid-column: 1 / -1; }.model-meta dt { color: var(--text-3); font-size: 10px; }.model-meta dd { margin: 3px 0 0; overflow: hidden; color: var(--text-2); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.model-actions { display: flex; flex-wrap: wrap; gap: 2px; margin-top: auto; padding-top: 10px; }
.empty-state { grid-column: 1 / -1; min-height: 220px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 9px; padding: 24px; color: var(--text-3); text-align: center; }.empty-state strong { color: var(--text-2); font-size: 14px; }.empty-state span { max-width: 440px; font-size: 12px; line-height: 1.55; }
.model-form :deep(.el-form-item) { margin-bottom: 15px; }.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }.form-full { grid-column: 1 / -1; }.form-section { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }.form-section h3 { margin: 4px 0 11px; color: var(--text); font-size: 15px; }.capability-checks { display: flex; flex-wrap: wrap; gap: 10px 16px; margin-bottom: 16px; }.compact { align-items: end; }.compact :deep(.el-form-item) { margin-bottom: 8px; }
.operations-drawer { min-height: 240px; }.usage-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin: 0 0 14px; }.usage-grid div { min-width: 0; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }.usage-grid span { display: block; color: var(--text-3); font-size: 10px; }.usage-grid b { display: block; margin-top: 4px; overflow: hidden; color: var(--text); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }.budget-strip { margin-bottom: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 11px; }.budget-strip div { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 9px; }.budget-strip strong { color: var(--text-2); font-size: 12px; }.budget-strip span { color: var(--text-3); font-size: 11px; text-align: right; }.evaluation-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; color: var(--text-3); font-size: 12px; }.evaluation-summary { margin: 10px 0 0; color: var(--text-3); font-size: 12px; }
@media (max-width: 1180px) { .route-grid { grid-template-columns: repeat(4, 1fr); }.route-title { grid-column: 1 / -1; min-height: auto; }.route-title br { display: none; } }
@media (max-width: 800px) { .model-page { padding: 18px 14px 24px; }.model-header { flex-direction: column; }.route-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.usage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 500px) { .route-grid, .form-grid, .usage-grid { grid-template-columns: 1fr; }.form-full { grid-column: auto; }.model-meta { grid-template-columns: 1fr; }.model-meta .wide { grid-column: auto; }.evaluation-actions { align-items: flex-start; flex-direction: column; } }
@media (prefers-reduced-motion: reduce) { .route-card, .model-card { transition: none; }.route-card:hover, .model-card:hover { transform: none; } }
</style>
