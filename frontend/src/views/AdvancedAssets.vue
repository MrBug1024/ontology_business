<template>
  <main class="advanced-page page" aria-labelledby="advanced-title">
    <header class="page-header advanced-header">
      <div>
        <span class="eyebrow">P2 ADVANCED RUNTIME</span>
        <h2 id="advanced-title">高级数据与模型</h2>
        <p class="sub">把空间、时序、媒体、实时流和可治理模型接入本体场景；每条记录、每次运行和每条反馈都可追溯。</p>
      </div>
      <div class="header-actions">
        <el-select v-model="selectedScenarioId" aria-label="选择业务场景" placeholder="选择业务场景" class="scenario-select" @change="loadAssets">
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button v-if="canManage && selectedScenarioId" type="primary" @click="openCreate">新建资产</el-button>
      </div>
    </header>

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" role="alert" />

    <section v-loading="loading" class="asset-layout" aria-live="polite">
      <div class="asset-list-panel">
        <div class="section-head">
          <div><span class="eyebrow">CATALOG</span><h3>资产目录</h3></div>
          <el-select v-model="kindFilter" aria-label="按类型筛选" clearable placeholder="全部类型" size="small" @change="loadAssets">
            <el-option v-for="item in kindOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </div>
        <div v-if="assets.length" class="asset-grid">
          <button v-for="asset in assets" :key="asset.id" type="button" class="asset-card" :class="{ active: selectedAsset?.id === asset.id }" @click="selectAsset(asset)">
            <span class="asset-icon" aria-hidden="true"><el-icon><component :is="assetIcon(asset.kind)" /></el-icon></span>
            <span class="asset-copy"><strong>{{ asset.name }}</strong><small>{{ kindLabel(asset.kind) }} · v{{ asset.version || 1 }}</small><em>{{ asset.description || '暂无说明' }}</em></span>
            <el-tag size="small" :type="asset.status === 'ready' ? 'success' : asset.status === 'disabled' ? 'info' : 'warning'" effect="plain">{{ statusLabel(asset.status) }}</el-tag>
          </button>
        </div>
        <el-empty v-else description="当前场景还没有高级资产" />
      </div>

      <section v-if="selectedAsset" class="asset-detail card" aria-labelledby="asset-detail-title">
        <header class="detail-head">
          <div><span class="eyebrow">{{ kindLabel(selectedAsset.kind) }}</span><h3 id="asset-detail-title">{{ selectedAsset.name }}</h3><p>{{ selectedAsset.description || '暂无资产说明' }}</p></div>
          <div class="detail-actions"><el-button size="small" :loading="detailLoading" @click="refreshDetail">刷新</el-button><el-button v-if="canManage" size="small" type="danger" plain @click="removeAsset">删除</el-button></div>
        </header>
        <div class="metric-grid" aria-label="资产统计">
          <div><span>记录</span><b>{{ summary?.record_count || 0 }}</b></div>
          <div><span>运行</span><b>{{ summary?.run_count || 0 }}</b></div>
          <div><span>反馈</span><b>{{ summary?.feedback_count || 0 }}</b></div>
          <div><span>版本</span><b>v{{ selectedAsset.version || 1 }}</b></div>
        </div>

        <el-tabs v-model="activeTab" class="detail-tabs">
          <el-tab-pane label="数据记录" name="records">
            <div class="tab-toolbar"><span class="muted">实时资产支持按 sequence 增量刷新；空间资产支持 bbox 查询。</span><div><el-button v-if="canManage && selectedAsset.kind === 'media'" size="small" @click="mediaInput?.click()">上传媒体</el-button><input v-if="selectedAsset.kind === 'media'" ref="mediaInput" class="visually-hidden" type="file" @change="uploadMedia" /><el-button v-if="canManage && selectedAsset.kind !== 'media'" size="small" type="primary" @click="recordDialog = true">写入记录</el-button></div></div>
            <el-table :data="records" size="small" max-height="330" empty-text="尚无记录">
              <el-table-column prop="sequence" label="#" width="58" />
              <el-table-column prop="event_type" label="事件" min-width="120" />
              <el-table-column prop="event_time" label="时间" min-width="150"><template #default="{ row }">{{ formatDate(row.event_time || row.created_at) }}</template></el-table-column>
              <el-table-column label="内容" min-width="190"><template #default="{ row }"><a v-if="row.content_type" class="media-link" :href="api.advancedMediaUrl(selectedAsset!.id, row.id)" target="_blank" rel="noreferrer">打开媒体</a><code v-else>{{ compactJson(row.payload) }}</code></template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="运行记录" name="runs">
            <div class="tab-toolbar"><span class="muted">运行器只接受服务端 allowlist 内的确定性算子，不执行任意代码。</span><el-button v-if="canManage && ['ml_model', 'simulation', 'optimization', 'timeseries'].includes(selectedAsset.kind)" size="small" type="primary" @click="runDialog = true">运行</el-button></div>
            <el-table :data="runs" size="small" max-height="330" empty-text="尚无运行记录">
              <el-table-column prop="run_type" label="类型" width="100" /><el-table-column prop="status" label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.status === 'succeeded' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">{{ runStatus(row.status) }}</el-tag></template></el-table-column><el-table-column prop="created_at" label="时间" min-width="150"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column><el-table-column label="结果" min-width="220"><template #default="{ row }"><code>{{ compactJson(row.output_payload || { error: row.error }) }}</code></template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane v-if="selectedAsset.kind === 'ml_model'" label="模型反馈" name="feedback">
            <div class="tab-toolbar"><span class="muted">将人工复核结果绑定到预测运行，形成可审计的反馈闭环。</span><el-button v-if="canManage" size="small" type="primary" @click="feedbackDialog = true">记录反馈</el-button></div>
            <el-table :data="feedback" size="small" max-height="330" empty-text="尚无反馈">
              <el-table-column prop="label" label="标签" min-width="130" /><el-table-column prop="score" label="得分" width="90"><template #default="{ row }">{{ row.score == null ? '—' : Number(row.score).toFixed(2) }}</template></el-table-column><el-table-column prop="run_id" label="运行 ID" min-width="150"><template #default="{ row }"><code>{{ row.run_id || '—' }}</code></template></el-table-column><el-table-column prop="created_at" label="时间" min-width="150"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </section>
      <section v-else class="asset-detail card empty-detail"><el-icon :size="34"><DataAnalysis /></el-icon><strong>选择一个资产查看运行面板</strong><span>资产记录、运行结果和模型反馈会在这里集中呈现。</span></section>
    </section>

    <el-dialog v-model="createDialog" title="新建高级资产" width="min(620px, calc(100vw - 28px))" destroy-on-close>
      <el-form :model="assetForm" label-position="top"><div class="form-grid"><el-form-item label="名称" required><el-input v-model="assetForm.name" /></el-form-item><el-form-item label="类型" required><el-select v-model="assetForm.kind" style="width:100%"><el-option v-for="item in kindOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item><el-form-item class="form-full" label="说明"><el-input v-model="assetForm.description" type="textarea" :rows="2" maxlength="8000" show-word-limit /></el-form-item></div><el-alert type="info" :closable="false" show-icon title="运行配置使用 JSON 数据描述；不允许保存密码、Token 或任意代码。" /><el-form-item label="运行配置 JSON"><el-input v-model="assetForm.configText" type="textarea" :rows="6" class="mono" /></el-form-item></el-form>
      <template #footer><el-button @click="createDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="createAsset">创建资产</el-button></template>
    </el-dialog>

    <el-dialog v-model="recordDialog" title="写入数据记录" width="min(600px, calc(100vw - 28px))"><el-form label-position="top"><el-form-item label="记录 JSON"><el-input v-model="recordText" type="textarea" :rows="9" class="mono" /></el-form-item></el-form><template #footer><el-button @click="recordDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="createRecord">写入</el-button></template></el-dialog>
    <el-dialog v-model="runDialog" title="运行资产算子" width="min(600px, calc(100vw - 28px))"><el-form label-position="top"><el-form-item label="运行类型"><el-select v-model="runType" style="width:100%"><el-option v-for="item in runOptionsForAsset(selectedAsset?.kind || '')" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item><el-form-item label="参数 JSON"><el-input v-model="runText" type="textarea" :rows="9" class="mono" /></el-form-item><el-form-item label="幂等键（可选）"><el-input v-model="idempotencyKey" placeholder="同一资产版本重复提交时复用结果" /></el-form-item></el-form><template #footer><el-button @click="runDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="runAsset">执行</el-button></template></el-dialog>
    <el-dialog v-model="feedbackDialog" title="记录模型反馈" width="min(560px, calc(100vw - 28px))"><el-form :model="feedbackForm" label-position="top"><el-form-item label="关联运行 ID"><el-input v-model="feedbackForm.run_id" class="mono" /></el-form-item><div class="form-grid compact"><el-form-item label="标签"><el-input v-model="feedbackForm.label" /></el-form-item><el-form-item label="得分（0–1）"><el-input-number v-model="feedbackForm.score" :min="0" :max="1" :step="0.05" style="width:100%" /></el-form-item></div><el-form-item label="说明"><el-input v-model="feedbackForm.notes" type="textarea" :rows="3" /></el-form-item></el-form><template #footer><el-button @click="feedbackDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="createFeedback">保存反馈</el-button></template></el-dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { DataAnalysis, Location, VideoCamera, Connection, TrendCharts, Cpu, SetUp } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { AdvancedAsset, AdvancedAssetSummary, AdvancedFeedback, AdvancedRecord, AdvancedRun, Scenario } from '@/types'

const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const scenarios = ref<Scenario[]>([])
const selectedScenarioId = ref('')
const assets = ref<AdvancedAsset[]>([])
const selectedAsset = ref<AdvancedAsset | null>(null)
const summary = ref<AdvancedAssetSummary | null>(null)
const records = ref<AdvancedRecord[]>([])
const runs = ref<AdvancedRun[]>([])
const feedback = ref<AdvancedFeedback[]>([])
const loading = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const error = ref('')
const kindFilter = ref('')
const activeTab = ref('records')
const createDialog = ref(false)
const recordDialog = ref(false)
const runDialog = ref(false)
const feedbackDialog = ref(false)
const mediaInput = ref<HTMLInputElement | null>(null)
const recordText = ref('{"event_type":"event.created","payload":{}}')
const runText = ref('{"features":{}}')
const runType = ref('predict')
const idempotencyKey = ref('')
const assetForm = ref({ name: '', kind: 'timeseries', description: '', configText: '{}' })
const feedbackForm = ref<{ run_id: string; label: string; score?: number; notes: string }>({ run_id: '', label: '', score: undefined, notes: '' })
let pollTimer: number | undefined

const kindOptions = [
  { value: 'geospatial', label: '地理空间' }, { value: 'timeseries', label: '时序数据' }, { value: 'media', label: '媒体' },
  { value: 'realtime', label: '实时流' }, { value: 'ml_model', label: '机器学习模型' }, { value: 'simulation', label: '仿真' }, { value: 'optimization', label: '优化' },
]
function kindLabel(kind: string) { return kindOptions.find((item) => item.value === kind)?.label || kind }
function statusLabel(status: string) { return ({ ready: '可用', draft: '草稿', disabled: '停用' } as Record<string, string>)[status] || status }
function runStatus(status: string) { return ({ succeeded: '成功', failed: '失败', running: '运行中' } as Record<string, string>)[status] || status }
function assetIcon(kind: string) { return ({ geospatial: Location, timeseries: TrendCharts, media: VideoCamera, realtime: Connection, ml_model: Cpu, simulation: SetUp, optimization: DataAnalysis } as Record<string, any>)[kind] || DataAnalysis }
function runOptionsForAsset(kind: string) { return kind === 'timeseries' ? [{ value: 'aggregate', label: '时序聚合' }] : kind === 'simulation' ? [{ value: 'simulate', label: '仿真' }] : kind === 'optimization' ? [{ value: 'optimize', label: '优化' }] : [{ value: 'predict', label: '预测' }] }
function formatDate(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—' }
function compactJson(value: any) { const text = JSON.stringify(value || {}); return text.length > 130 ? `${text.slice(0, 127)}…` : text }
function clearPoll() { if (pollTimer) { window.clearInterval(pollTimer); pollTimer = undefined } }

async function loadAssets() {
  if (!selectedScenarioId.value) return
  loading.value = true; error.value = ''
  try { assets.value = await api.listAdvancedAssets(selectedScenarioId.value, kindFilter.value || undefined); if (selectedAsset.value && !assets.value.find((item) => item.id === selectedAsset.value?.id)) selectedAsset.value = null; } catch (cause: any) { error.value = cause?.message || '高级资产加载失败' } finally { loading.value = false }
}
async function selectAsset(asset: AdvancedAsset) { selectedAsset.value = asset; activeTab.value = 'records'; runType.value = asset.kind === 'timeseries' ? 'aggregate' : asset.kind === 'simulation' ? 'simulate' : asset.kind === 'optimization' ? 'optimize' : 'predict'; runText.value = asset.kind === 'timeseries' ? '{"values":[],"aggregation":"avg"}' : asset.kind === 'simulation' ? '{"state":{}}' : asset.kind === 'optimization' ? '{"candidates":[]}' : '{"features":{}}'; await refreshDetail(); clearPoll(); if (asset.kind === 'realtime') pollTimer = window.setInterval(() => refreshDetail(true), 5000) }
async function refreshDetail(silent = false) {
  if (!selectedAsset.value) return
  detailLoading.value = !silent
  try { const [s, page, r, f] = await Promise.all([api.getAdvancedAssetSummary(selectedAsset.value.id), api.listAdvancedRecords(selectedAsset.value.id, { limit: 100 }), api.listAdvancedRuns(selectedAsset.value.id), selectedAsset.value.kind === 'ml_model' ? api.listAdvancedFeedback(selectedAsset.value.id) : Promise.resolve([])]); summary.value = s; records.value = page.items; runs.value = r; feedback.value = f as AdvancedFeedback[] } catch (cause: any) { if (!silent) ElMessage.error(cause?.message || '资产详情加载失败') } finally { detailLoading.value = false }
}
function openCreate() { assetForm.value = { name: '', kind: 'timeseries', description: '', configText: '{}' }; createDialog.value = true }
async function createAsset() {
  if (!selectedScenarioId.value || !assetForm.value.name.trim()) return ElMessage.warning('请填写资产名称')
  saving.value = true
  try { let config: Record<string, any>; try { config = JSON.parse(assetForm.value.configText || '{}') } catch { return ElMessage.warning('运行配置必须是有效 JSON') }; await api.createAdvancedAsset(selectedScenarioId.value, { name: assetForm.value.name.trim(), kind: assetForm.value.kind as any, description: assetForm.value.description, status: 'ready', schema: {}, config }); createDialog.value = false; ElMessage.success('资产已创建'); await loadAssets() } catch (cause: any) { ElMessage.error(cause?.message || '创建失败') } finally { saving.value = false }
}
async function createRecord() { if (!selectedAsset.value) return; saving.value = true; try { await api.createAdvancedRecord(selectedAsset.value.id, JSON.parse(recordText.value)); recordDialog.value = false; ElMessage.success('记录已写入'); await refreshDetail() } catch (cause: any) { ElMessage.error(cause?.message || '记录写入失败，请检查 JSON') } finally { saving.value = false } }
async function runAsset() { if (!selectedAsset.value) return; saving.value = true; try { const run = await api.runAdvancedAsset(selectedAsset.value.id, runType.value, { params: JSON.parse(runText.value), idempotency_key: idempotencyKey.value || undefined }); runDialog.value = false; ElMessage[run.status === 'succeeded' ? 'success' : 'warning'](run.status === 'succeeded' ? '运行完成' : run.error || '运行失败'); await refreshDetail() } catch (cause: any) { ElMessage.error(cause?.message || '运行失败，请检查参数 JSON') } finally { saving.value = false } }
async function uploadMedia(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file || !selectedAsset.value) return; saving.value = true; try { await api.uploadAdvancedMedia(selectedAsset.value.id, file); ElMessage.success('媒体已上传'); await refreshDetail() } catch (cause: any) { ElMessage.error(cause?.message || '媒体上传失败') } finally { saving.value = false; (event.target as HTMLInputElement).value = '' } }
async function createFeedback() { if (!selectedAsset.value) return; saving.value = true; try { await api.createAdvancedFeedback(selectedAsset.value.id, feedbackForm.value); feedbackDialog.value = false; ElMessage.success('反馈已记录'); await refreshDetail() } catch (cause: any) { ElMessage.error(cause?.message || '反馈记录失败') } finally { saving.value = false } }
async function removeAsset() { if (!selectedAsset.value) return; try { await ElMessageBox.confirm(`删除资产「${selectedAsset.value.name}」及其运行记录？`, '确认删除', { type: 'warning' }); await api.deleteAdvancedAsset(selectedAsset.value.id); ElMessage.success('资产已删除'); selectedAsset.value = null; await loadAssets() } catch (cause: any) { if (cause !== 'cancel' && cause !== 'close') ElMessage.error(cause?.message || '删除失败') } }
onMounted(async () => { try { scenarios.value = await api.listScenarios(); selectedScenarioId.value = scenarios.value[0]?.id || ''; await loadAssets() } catch (cause: any) { error.value = cause?.message || '场景加载失败' } })
onBeforeUnmount(clearPoll)
</script>

<style scoped>
.advanced-page { max-width: 1640px; }
.advanced-header { align-items: center; }
.header-actions, .detail-actions, .tab-toolbar { display: flex; align-items: center; gap: 10px; }
.scenario-select { width: 230px; }
.asset-layout { display: grid; grid-template-columns: minmax(320px, .78fr) minmax(0, 1.55fr); gap: 18px; align-items: start; }
.asset-list-panel { min-width: 0; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.section-head h3, .detail-head h3 { margin: 2px 0 0; font-size: 18px; }
.asset-grid { display: grid; gap: 9px; }
.asset-card { width: 100%; min-height: 82px; display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; align-items: center; gap: 10px; text-align: left; border: 1px solid var(--border); border-radius: 13px; padding: 13px; background: var(--surface); color: var(--text); cursor: pointer; transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease), transform var(--dur) var(--ease); }
.asset-card:hover, .asset-card.active { border-color: var(--primary); box-shadow: var(--shadow); transform: translateY(-1px); }
.asset-icon { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 11px; color: var(--primary-600); background: var(--primary-soft); font-size: 18px; }
.asset-copy { min-width: 0; display: grid; gap: 2px; }
.asset-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-copy small { color: var(--primary-600); font-size: 11px; font-weight: 700; }
.asset-copy em { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-3); font-size: 11px; font-style: normal; }
.asset-detail { min-width: 0; min-height: 460px; }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.detail-head p { margin: 5px 0 0; color: var(--text-2); font-size: 13px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 20px 0 16px; }
.metric-grid > div { border: 1px solid var(--border); background: var(--surface-2); border-radius: 11px; padding: 11px 13px; display: grid; gap: 4px; }
.metric-grid span { color: var(--text-3); font-size: 11px; }
.metric-grid b { font-size: 20px; font-weight: 800; }
.detail-tabs { min-width: 0; }
.tab-toolbar { justify-content: space-between; margin: 0 0 10px; gap: 12px; }
.tab-toolbar .muted { max-width: 70%; }
.media-link { color: var(--primary-600); font-weight: 700; }
code, .mono { font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; font-size: 12px; }
.empty-detail { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 9px; color: var(--text-2); text-align: center; }
.empty-detail .el-icon { color: var(--primary); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.form-full { grid-column: 1 / -1; }
.visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 1080px) { .asset-layout { grid-template-columns: 1fr; } .asset-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .advanced-page { padding: 18px 14px 28px; } .advanced-header, .detail-head { align-items: flex-start; flex-direction: column; } .header-actions, .scenario-select { width: 100%; } .asset-grid { grid-template-columns: 1fr; } .metric-grid { grid-template-columns: repeat(2, 1fr); } .tab-toolbar { align-items: flex-start; flex-direction: column; } .tab-toolbar .muted { max-width: none; } .form-grid { grid-template-columns: 1fr; } .form-full { grid-column: auto; } }
@media (prefers-reduced-motion: reduce) { .asset-card { transition: none; } .asset-card:hover, .asset-card.active { transform: none; } }
</style>
