<template>
  <section class="candidate-review" aria-labelledby="candidate-review-heading" :aria-busy="loading || operationBusy">
    <header class="review-header">
      <div>
        <h2 id="candidate-review-heading">候选定义评审</h2>
        <p>候选保持停用；正式化资格、校验结论和阻塞原因均来自服务端治理结果。</p>
      </div>
      <el-button
        plain
        :loading="loading"
        :disabled="operationBusy"
        aria-label="刷新候选治理结果"
        @click="emit('refresh', false)"
      >
        <el-icon aria-hidden="true"><Refresh /></el-icon>
        刷新
      </el-button>
    </header>

    <dl class="review-metrics" aria-label="正式定义与候选定义统计">
      <div>
        <dt>正式定义</dt>
        <dd>{{ formalCount }}</dd>
      </div>
      <div>
        <dt>候选定义</dt>
        <dd>{{ summaryCount('candidate_count') }}</dd>
      </div>
      <div>
        <dt>可晋级</dt>
        <dd>{{ summaryCount('promotion_eligible_count') }}</dd>
      </div>
      <div>
        <dt>阻塞</dt>
        <dd>{{ summaryCount('promotion_blocked_count') }}</dd>
      </div>
    </dl>

    <div
      v-if="failure"
      ref="errorSummary"
      class="error-summary"
      role="alert"
      tabindex="-1"
      aria-labelledby="candidate-error-heading"
    >
      <div class="error-summary-head">
        <el-icon aria-hidden="true"><WarningFilled /></el-icon>
        <div>
          <h3 id="candidate-error-heading">本次操作未完成</h3>
          <p>{{ failure.message }}</p>
        </div>
      </div>
      <ul v-if="failure.blockers.length" class="error-blockers">
        <li v-for="(blocker, index) in failure.blockers" :key="`${blocker.code || 'blocker'}:${index}`">
          <button
            v-if="firstFailureDraftId(blocker)"
            type="button"
            @click="focusCandidate(firstFailureDraftId(blocker))"
          >
            {{ blocker.message }}
          </button>
          <span v-else>{{ blocker.message }}</span>
          <small v-if="candidateBlockerLocation(blocker)">字段：{{ candidateBlockerLocation(blocker) }}</small>
          <small v-if="blocker.resolution_hint">{{ blocker.resolution_hint }}</small>
        </li>
      </ul>
      <div v-if="failureTargetCandidates.length" class="failure-targets" aria-label="受影响候选">
        <span>受影响候选</span>
        <button
          v-for="candidate in failureTargetCandidates"
          :key="candidate.id"
          type="button"
          @click="focusCandidate(candidate.id)"
        >
          {{ candidate.title || candidate.resource_key }}
        </button>
      </div>
      <el-button size="small" plain @click="clearFailure">关闭错误摘要</el-button>
    </div>

    <div v-if="loadError" class="load-error" role="alert">
      <span>{{ loadError }}</span>
      <el-button size="small" plain @click="emit('refresh', false)">重新加载</el-button>
    </div>

    <p class="sr-status" aria-live="polite" aria-atomic="true">{{ operationNotice }}</p>

    <div class="review-toolbar">
      <div class="review-filters" aria-label="候选定义筛选">
        <el-input v-model="query" clearable placeholder="搜索名称或资源键" aria-label="搜索候选名称或资源键">
          <template #prefix><el-icon aria-hidden="true"><Search /></el-icon></template>
        </el-input>
        <el-select v-model="kindFilter" aria-label="按资源类型筛选">
          <el-option label="全部类型" value="" />
          <el-option v-for="kind in candidateKinds" :key="kind" :label="scenarioDraftKindLabel(kind)" :value="kind" />
        </el-select>
        <el-select v-model="originFilter" aria-label="按候选来源筛选">
          <el-option label="全部来源" value="" />
          <el-option v-for="origin in candidateOrigins" :key="origin" :label="candidateOriginLabel(origin)" :value="origin" />
        </el-select>
      </div>
      <div class="batch-actions">
        <el-button
          plain
          :loading="batchRevalidating"
          :disabled="!canWrite || !revalidationCandidates.length || revalidationCandidates.length > 200 || (operationBusy && !batchRevalidating)"
          @click="revalidateAll"
        >
          <el-icon aria-hidden="true"><CircleCheck /></el-icon>
          一键确定性校验（{{ revalidationCandidates.length }}）
        </el-button>
        <el-checkbox
          :model-value="allVisibleEligibleSelected"
          :indeterminate="someVisibleEligibleSelected"
          :disabled="!canWrite || !visibleEligibleCandidates.length || operationBusy"
          @change="toggleVisibleEligible"
        >
          选择当前可晋级项
        </el-checkbox>
        <el-button
          type="primary"
          :loading="batchPromoting"
          :disabled="!canWrite || !selectedCount || (operationBusy && !batchPromoting)"
          @click="promoteSelected"
        >
          <el-icon aria-hidden="true"><Top /></el-icon>
          原子批量晋级（{{ selectedCount }}）
        </el-button>
      </div>
    </div>

    <div class="result-context" role="status" aria-live="polite" aria-atomic="true">
      当前显示 {{ visibleCandidates.length }} 项；已选择 {{ selectedCount }} 项。
    </div>

    <div v-loading="loading" class="candidate-list" data-testid="candidate-review-list">
      <article
        v-for="candidate in visibleCandidates"
        :id="candidateDomId(candidate.id)"
        :key="candidate.id"
        :ref="(element) => setCandidateRef(candidate.id, element)"
        class="candidate-item"
        :class="{ 'is-failure-target': failureDraftIds.has(candidate.id) }"
        tabindex="-1"
        :aria-labelledby="`${candidateDomId(candidate.id)}-title`"
      >
        <div class="candidate-select">
          <el-checkbox
            :model-value="selectedIds.has(candidate.id)"
            :disabled="!canWrite || candidate.promotion_eligible !== true || operationBusy"
            :aria-label="`${selectedIds.has(candidate.id) ? '取消选择' : '选择'}候选 ${candidate.title || candidate.resource_key}`"
            @change="(value: boolean | string | number) => toggleCandidate(candidate, Boolean(value))"
          />
        </div>

        <div class="candidate-body">
          <header class="candidate-heading">
            <div class="candidate-title-wrap">
              <el-tag size="small" effect="plain">{{ scenarioDraftKindLabel(candidate.resource_kind) }}</el-tag>
              <h3 :id="`${candidateDomId(candidate.id)}-title`">{{ candidate.title || candidate.resource_key }}</h3>
              <code>r{{ candidate.revision }}</code>
            </div>
            <el-tag
              size="small"
              :type="candidate.promotion_eligible ? 'success' : 'warning'"
              effect="plain"
            >
              {{ candidate.promotion_eligible ? '可晋级' : '不可晋级' }}
            </el-tag>
          </header>

          <dl class="candidate-statuses">
            <div>
              <dt>来源</dt>
              <dd>{{ candidateOriginLabel(candidate.source_origin) }}</dd>
            </div>
            <div>
              <dt>生命周期</dt>
              <dd>{{ candidateLifecycleLabel(candidate.lifecycle_status) }}</dd>
            </div>
            <div>
              <dt>验证结论</dt>
              <dd>{{ candidateValidationLabel(candidate.validation_status) }}</dd>
            </div>
            <div>
              <dt>激活状态</dt>
              <dd>{{ candidateActivationLabel(candidate.activation_status) }}</dd>
            </div>
          </dl>

          <div v-if="candidate.promotion_blockers.length" class="candidate-blockers">
            <strong>晋级阻塞（{{ candidate.promotion_blockers.length }}）</strong>
            <ul>
              <li v-for="(blocker, index) in candidate.promotion_blockers" :key="`${blocker.code || 'blocker'}:${index}`">
                <span>{{ blocker.message }}</span>
                <small v-if="candidateBlockerLocation(blocker)">字段：{{ candidateBlockerLocation(blocker) }}</small>
                <small v-if="blocker.resolution_hint">{{ blocker.resolution_hint }}</small>
              </li>
            </ul>
          </div>
          <div v-else-if="candidate.promotion_eligible" class="candidate-ready">
            <el-icon aria-hidden="true"><CircleCheck /></el-icon>
            服务端预检通过，可正式化为当前场景定义。
          </div>

          <details class="candidate-provenance">
            <summary>来源与版本依据</summary>
            <dl>
              <div><dt>资源键</dt><dd><code>{{ candidate.resource_key }}</code></dd></div>
              <div><dt>生成通道</dt><dd>{{ candidate.materialization_source || '未记录' }}</dd></div>
              <div><dt>质量指纹</dt><dd><code>{{ candidate.quality_fingerprint || '待校验' }}</code></dd></div>
              <div><dt>来源引用</dt><dd>{{ candidate.source_refs?.length ? candidate.source_refs.join('、') : '无' }}</dd></div>
            </dl>
          </details>
        </div>

        <div v-if="canWrite" class="candidate-actions">
          <el-button
            plain
            :loading="revalidatingIds.has(candidate.id)"
            :disabled="operationBusy && !revalidatingIds.has(candidate.id)"
            @click="revalidate(candidate)"
          >
            <el-icon aria-hidden="true"><Refresh /></el-icon>
            重新校验
          </el-button>
          <el-button
            type="primary"
            plain
            :loading="promotingIds.has(candidate.id)"
            :disabled="candidate.promotion_eligible !== true || (operationBusy && !promotingIds.has(candidate.id))"
            @click="promoteOne(candidate)"
          >
            <el-icon aria-hidden="true"><Top /></el-icon>
            逐项晋级
          </el-button>
        </div>
      </article>

      <el-empty
        v-if="!loading && !visibleCandidates.length"
        :image-size="64"
        :description="candidates.length ? '没有匹配当前筛选条件的候选定义' : '当前没有待评审的候选定义'"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { ElMessageBox } from 'element-plus'
import { CircleCheck, Refresh, Search, Top, WarningFilled } from '@element-plus/icons-vue'
import { api } from '@/api'
import type {
  ScenarioModelCandidateBlocker,
  ScenarioModelCandidateOrigin,
  ScenarioModelCandidateSummary,
  ScenarioModelDraftResource,
} from '@/types'
import {
  candidateActivationLabel,
  candidateApiFailure,
  candidateBlockerLocation,
  candidateFailureDraftIds,
  candidateLifecycleLabel,
  candidateOriginLabel,
  candidatePromotionRequest,
  candidateValidationLabel,
  type CandidateApiFailure,
} from '@/utils/candidateGovernance'
import { scenarioDraftKindLabel } from '@/utils/scenarioModelDrafts'

const props = withDefaults(defineProps<{
  scenarioId: string
  candidates: ScenarioModelDraftResource[]
  summary?: ScenarioModelCandidateSummary
  formalCount: number
  loading?: boolean
  loadError?: string
  canWrite?: boolean
}>(), {
  summary: () => ({}),
  loading: false,
  loadError: '',
  canWrite: false,
})

const emit = defineEmits<{
  refresh: [definitionChanged: boolean]
}>()

const query = ref('')
const kindFilter = ref('')
const originFilter = ref<ScenarioModelCandidateOrigin | ''>('')
const selectedIds = ref(new Set<string>())
const revalidatingIds = ref(new Set<string>())
const promotingIds = ref(new Set<string>())
const batchRevalidating = ref(false)
const batchPromoting = ref(false)
const failure = ref<CandidateApiFailure | null>(null)
const failureDraftIds = ref(new Set<string>())
const errorSummary = ref<HTMLElement | null>(null)
const candidateRefs = new Map<string, HTMLElement>()
const operationNotice = ref('')

const candidateKinds = computed(() => [...new Set(props.candidates.map((item) => item.resource_kind))].sort())
const candidateOrigins = computed<ScenarioModelCandidateOrigin[]>(() => (
  [...new Set(props.candidates.map((item) => item.source_origin))].sort() as ScenarioModelCandidateOrigin[]
))
const visibleCandidates = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase()
  return props.candidates.filter((item) => (
    (!kindFilter.value || item.resource_kind === kindFilter.value)
    && (!originFilter.value || item.source_origin === originFilter.value)
    && (!needle || `${item.title || ''} ${item.resource_key}`.toLocaleLowerCase().includes(needle))
  ))
})
const visibleEligibleCandidates = computed(() => visibleCandidates.value.filter((item) => item.promotion_eligible === true))
const revalidationCandidates = computed(() => props.candidates.filter((item) => (
  !['formalized', 'resolved', 'superseded'].includes(item.lifecycle_status)
)))
const selectedCount = computed(() => selectedIds.value.size)
const allVisibleEligibleSelected = computed(() => (
  visibleEligibleCandidates.value.length > 0
  && visibleEligibleCandidates.value.every((item) => selectedIds.value.has(item.id))
))
const someVisibleEligibleSelected = computed(() => (
  !allVisibleEligibleSelected.value
  && visibleEligibleCandidates.value.some((item) => selectedIds.value.has(item.id))
))
const failureTargetCandidates = computed(() => props.candidates.filter((item) => failureDraftIds.value.has(item.id)))
const operationBusy = computed(() => (
  props.loading || batchRevalidating.value || batchPromoting.value || revalidatingIds.value.size > 0 || promotingIds.value.size > 0
))

watch(() => props.candidates.map((item) => `${item.id}:${item.revision}:${item.promotion_eligible}`).join('|'), () => {
  const eligibleIds = new Set(props.candidates.filter((item) => item.promotion_eligible === true).map((item) => item.id))
  selectedIds.value = new Set([...selectedIds.value].filter((id) => eligibleIds.has(id)))
})

function summaryCount(key: keyof ScenarioModelCandidateSummary): string | number {
  const value = Number(props.summary?.[key])
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : '—'
}

function candidateDomId(id: string): string {
  return `candidate-${id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

function setCandidateRef(id: string, value: Element | ComponentPublicInstance | null) {
  const element = value instanceof HTMLElement
    ? value
    : value && '$el' in value && value.$el instanceof HTMLElement ? value.$el : null
  if (element) candidateRefs.set(id, element)
  else candidateRefs.delete(id)
}

function toggleCandidate(candidate: ScenarioModelDraftResource, selected: boolean) {
  if (candidate.promotion_eligible !== true) return
  const next = new Set(selectedIds.value)
  if (selected) next.add(candidate.id)
  else next.delete(candidate.id)
  selectedIds.value = next
}

function toggleVisibleEligible(selected: boolean | string | number) {
  const next = new Set(selectedIds.value)
  for (const candidate of visibleEligibleCandidates.value) {
    if (Boolean(selected)) next.add(candidate.id)
    else next.delete(candidate.id)
  }
  selectedIds.value = next
}

function firstFailureDraftId(blocker: ScenarioModelCandidateBlocker): string {
  return (blocker.draft_ids || []).find((id) => candidateRefs.has(id)) || ''
}

async function focusCandidate(id: string) {
  await nextTick()
  const target = candidateRefs.get(id)
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
  target?.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' })
  target?.focus({ preventScroll: true })
}

function clearFailure() {
  failure.value = null
  failureDraftIds.value = new Set()
}

async function showFailure(error: unknown, fallbackDraftIds: string[] = []) {
  const parsed = candidateApiFailure(error)
  failure.value = parsed
  const targets = candidateFailureDraftIds(parsed)
  failureDraftIds.value = new Set(targets.length ? targets : fallbackDraftIds)
  await nextTick()
  errorSummary.value?.focus()
}

function setBusyId(target: typeof revalidatingIds, id: string, busy: boolean) {
  const next = new Set(target.value)
  if (busy) next.add(id)
  else next.delete(id)
  target.value = next
}

async function revalidate(candidate: ScenarioModelDraftResource) {
  clearFailure()
  operationNotice.value = ''
  setBusyId(revalidatingIds, candidate.id, true)
  try {
    const updated = await api.revalidateScenarioModelCandidate(props.scenarioId, candidate.id, {
      expected_revision: candidate.revision,
    })
    operationNotice.value = updated.promotion_eligible
      ? `${candidate.title || candidate.resource_key} 重新校验通过，可晋级。`
      : `${candidate.title || candidate.resource_key} 重新校验完成，仍有晋级阻塞。`
    emit('refresh', false)
    if (!updated.promotion_eligible) await focusCandidate(candidate.id)
  } catch (error) {
    await showFailure(error, [candidate.id])
  } finally {
    setBusyId(revalidatingIds, candidate.id, false)
  }
}

async function revalidateAll() {
  const candidates = revalidationCandidates.value
  if (!candidates.length || candidates.length > 200) return
  clearFailure()
  operationNotice.value = ''
  batchRevalidating.value = true
  try {
    const result = await api.revalidateScenarioModelCandidates(props.scenarioId, {
      items: candidates.map((candidate) => ({
        draft_id: candidate.id,
        expected_revision: candidate.revision,
      })),
    })
    operationNotice.value = result.eligible_count
      ? `已确定性校验 ${result.revalidated_count} 项：${result.eligible_count} 项可直接晋级，${result.blocked_count} 项仍需补全。`
      : `已确定性校验 ${result.revalidated_count} 项；当前 ${result.blocked_count} 项都有可定位的正式化阻塞。`
    emit('refresh', false)
  } catch (error) {
    await showFailure(error, candidates.map((candidate) => candidate.id))
  } finally {
    batchRevalidating.value = false
  }
}

async function promoteOne(candidate: ScenarioModelDraftResource) {
  if (candidate.promotion_eligible !== true) return
  clearFailure()
  operationNotice.value = ''
  setBusyId(promotingIds, candidate.id, true)
  try {
    const result = await api.promoteScenarioModelCandidate(props.scenarioId, candidate.id, {
      expected_revision: candidate.revision,
    })
    if (result.atomic !== true) throw new Error('服务端未确认原子正式化结果，请重新加载核对。')
    const next = new Set(selectedIds.value)
    next.delete(candidate.id)
    selectedIds.value = next
    operationNotice.value = `${candidate.title || candidate.resource_key} 已正式化；可激活的正式定义不会自动激活。`
    emit('refresh', true)
  } catch (error) {
    await showFailure(error, [candidate.id])
  } finally {
    setBusyId(promotingIds, candidate.id, false)
  }
}

async function promoteSelected() {
  if (!selectedIds.value.size) return
  let request
  try {
    request = candidatePromotionRequest(props.candidates, selectedIds.value)
  } catch (error) {
    await showFailure(error)
    return
  }
  try {
    await ElMessageBox.confirm(
      `将 ${request.items.length} 个候选作为一个原子批次正式化；任一项失败时不会写入任何一项。`,
      '确认原子批量晋级',
      {
        confirmButtonText: '全部晋级',
        cancelButtonText: '取消',
        type: 'warning',
        autofocus: true,
      },
    )
  } catch {
    return
  }

  clearFailure()
  operationNotice.value = ''
  batchPromoting.value = true
  try {
    const result = await api.promoteScenarioModelCandidates(props.scenarioId, request)
    if (result.atomic !== true) throw new Error('服务端未确认原子批次结果，请重新加载核对。')
    selectedIds.value = new Set()
    operationNotice.value = `${result.promoted.length} 个候选已原子正式化；可激活的正式定义不会自动激活。`
    emit('refresh', true)
  } catch (error) {
    await showFailure(error, request.items.map((item) => item.draft_id))
  } finally {
    batchPromoting.value = false
  }
}
</script>

<style scoped>
.candidate-review {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  gap: 16px;
  overflow: auto;
  padding: 2px 2px 20px;
}

.review-header,
.review-toolbar,
.batch-actions,
.candidate-heading,
.candidate-title-wrap,
.candidate-actions {
  display: flex;
  align-items: center;
}

.review-header {
  justify-content: space-between;
  gap: 16px;
}

.review-header h2,
.candidate-heading h3,
.error-summary h3 {
  margin: 0;
  color: var(--text);
  letter-spacing: 0;
}

.review-header h2 {
  font-size: 18px;
}

.review-header p,
.error-summary p {
  margin: 4px 0 0;
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.55;
}

.review-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
  border-block: 1px solid var(--border);
}

.review-metrics > div {
  padding: 12px 16px;
  border-inline-end: 1px solid var(--border);
}

.review-metrics > div:last-child {
  border-inline-end: 0;
}

.review-metrics dt,
.candidate-statuses dt,
.candidate-provenance dt {
  color: var(--text-2);
  font-size: 12px;
}

.review-metrics dd {
  margin: 4px 0 0;
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.error-summary,
.load-error {
  border: 1px solid color-mix(in srgb, var(--danger) 42%, var(--border));
  border-radius: 6px;
  background: var(--danger-soft);
  color: var(--text);
}

.error-summary {
  padding: 14px 16px;
  outline: none;
}

.error-summary:focus-visible {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 28%, transparent);
}

.error-summary-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.error-summary-head > .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--danger);
  font-size: 20px;
}

.error-summary h3 {
  font-size: 15px;
}

.error-blockers,
.candidate-blockers ul {
  margin: 12px 0;
  padding-inline-start: 22px;
}

.error-blockers li,
.candidate-blockers li {
  margin: 6px 0;
  line-height: 1.45;
}

.error-blockers button {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--danger);
  cursor: pointer;
  font: inherit;
  text-align: start;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.failure-targets {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 10px 0 12px;
  flex-wrap: wrap;
  color: var(--text-2);
  font-size: 12px;
}

.failure-targets button {
  min-height: 28px;
  padding: 3px 8px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border));
  border-radius: 4px;
  background: var(--surface);
  color: var(--danger);
  cursor: pointer;
}

.error-blockers small,
.candidate-blockers small {
  display: block;
  margin-top: 2px;
  color: var(--text-2);
}

.load-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
}

.sr-status {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.review-toolbar {
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.review-filters {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 150px 150px;
  gap: 8px;
  min-width: min(100%, 540px);
}

.batch-actions {
  gap: 12px;
  flex-wrap: wrap;
}

.result-context {
  color: var(--text-2);
  font-size: 12px;
}

.candidate-list {
  display: flex;
  min-height: 140px;
  flex-direction: column;
  gap: 10px;
}

.candidate-item {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  outline: none;
}

.candidate-item:focus-visible {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 24%, transparent);
}

.candidate-item.is-failure-target {
  border-color: color-mix(in srgb, var(--danger) 55%, var(--border));
}

.candidate-select {
  padding-top: 2px;
}

.candidate-body {
  min-width: 0;
}

.candidate-heading {
  justify-content: space-between;
  gap: 12px;
}

.candidate-title-wrap {
  min-width: 0;
  gap: 8px;
  flex-wrap: wrap;
}

.candidate-heading h3 {
  max-width: 100%;
  overflow-wrap: anywhere;
  font-size: 15px;
}

.candidate-title-wrap code,
.candidate-provenance code {
  color: var(--text-2);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.candidate-statuses {
  display: grid;
  grid-template-columns: repeat(4, minmax(100px, 1fr));
  gap: 8px;
  margin: 14px 0 0;
}

.candidate-statuses > div {
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface-2);
}

.candidate-statuses dd,
.candidate-provenance dd {
  margin: 3px 0 0;
  color: var(--text);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.candidate-blockers,
.candidate-ready {
  margin-top: 12px;
  border-radius: 4px;
  font-size: 13px;
}

.candidate-blockers {
  padding: 10px 12px;
  background: var(--warning-soft);
  color: var(--text);
}

.candidate-blockers ul {
  margin-bottom: 0;
}

.candidate-ready {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 11px;
  background: var(--success-soft);
  color: var(--success);
}

.candidate-provenance {
  margin-top: 10px;
  color: var(--text-2);
  font-size: 12px;
}

.candidate-provenance summary {
  width: fit-content;
  min-height: 28px;
  cursor: pointer;
  line-height: 28px;
}

.candidate-provenance dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 8px 0 0;
  padding: 10px;
  border-inline-start: 2px solid var(--border-strong);
}

.candidate-actions {
  align-self: start;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
  max-width: 230px;
}

@media (max-width: 900px) {
  .candidate-item {
    grid-template-columns: 28px minmax(0, 1fr);
  }

  .candidate-actions {
    grid-column: 2;
    justify-content: flex-start;
    max-width: none;
  }
}

@media (max-width: 720px) {
  .review-header,
  .review-toolbar,
  .load-error {
    align-items: stretch;
    flex-direction: column;
  }

  .review-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .review-metrics > div:nth-child(2) {
    border-inline-end: 0;
  }

  .review-metrics > div:nth-child(-n + 2) {
    border-block-end: 1px solid var(--border);
  }

  .review-filters {
    grid-template-columns: 1fr;
    width: 100%;
    min-width: 0;
  }

  .batch-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .candidate-statuses,
  .candidate-provenance dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 420px) {
  .candidate-item {
    grid-template-columns: 24px minmax(0, 1fr);
    padding: 12px;
  }

  .candidate-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .candidate-statuses,
  .candidate-provenance dl {
    grid-template-columns: 1fr;
  }

  .candidate-actions {
    display: grid;
    grid-template-columns: 1fr;
  }

  .candidate-actions :deep(.el-button) {
    min-height: 40px;
    margin-inline-start: 0;
  }
}

</style>
