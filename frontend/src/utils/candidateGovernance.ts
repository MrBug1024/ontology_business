import type {
  ScenarioModelCandidateActivationStatus,
  ScenarioModelCandidateBatchPromotionRequest,
  ScenarioModelCandidateBlocker,
  ScenarioModelCandidateLifecycleStatus,
  ScenarioModelCandidateOrigin,
  ScenarioModelCandidateValidationStatus,
  ScenarioModelDraftResource,
} from '@/types'

function record(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, any>
    : {}
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function stringList(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const values = value.map((item) => clean(item)).filter(Boolean)
  return values.length ? [...new Set(values)] : undefined
}

export function normalizeCandidateBlocker(value: unknown): ScenarioModelCandidateBlocker | null {
  if (typeof value === 'string' && value.trim()) return { message: value.trim(), blocking: true }
  const item = record(value)
  const message = clean(item.message || item.detail || item.reason)
  if (!message) return null
  return {
    code: clean(item.code) || undefined,
    message,
    field: clean(item.field) || undefined,
    path: clean(item.path) || undefined,
    field_path: stringList(item.field_path),
    blocking: item.blocking !== false,
    resolution_hint: clean(item.resolution_hint || item.suggestion) || undefined,
    source_refs: stringList(item.source_refs),
    draft_ids: stringList(item.draft_ids),
    resource_keys: stringList(item.resource_keys),
  }
}

export function normalizeCandidateBlockers(value: unknown): ScenarioModelCandidateBlocker[] {
  return (Array.isArray(value) ? value : [])
    .map(normalizeCandidateBlocker)
    .filter((item): item is ScenarioModelCandidateBlocker => Boolean(item))
}

export interface CandidateApiFailure {
  code: string
  message: string
  blockers: ScenarioModelCandidateBlocker[]
}

function parsedMessage(value: unknown): unknown {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

export function candidateApiFailure(error: unknown): CandidateApiFailure {
  const source = record(error)
  const detailValue = source.detail ?? parsedMessage(source.message)
  const detail = record(detailValue)
  const message = clean(detail.message)
    || (typeof detailValue === 'string' ? clean(detailValue) : '')
    || clean(source.message)
    || '候选治理请求失败，请重新加载后重试。'
  return {
    code: clean(detail.code) || 'candidate_request_failed',
    message,
    blockers: normalizeCandidateBlockers(detail.blockers),
  }
}

export function candidateFailureDraftIds(failure: CandidateApiFailure): string[] {
  return [...new Set(failure.blockers.flatMap((item) => item.draft_ids || []))]
}

export function candidatePromotionRequest(
  candidates: ScenarioModelDraftResource[],
  selectedIds: Iterable<string>,
): ScenarioModelCandidateBatchPromotionRequest {
  const byId = new Map(candidates.map((item) => [item.id, item]))
  const uniqueIds = [...new Set(selectedIds)]
  const items = uniqueIds.map((draftId) => {
    const candidate = byId.get(draftId)
    if (!candidate) throw new Error(`候选 ${draftId} 已不在当前列表中，请重新加载。`)
    return { draft_id: candidate.id, expected_revision: candidate.revision }
  })
  if (!items.length) throw new Error('请至少选择一个候选定义。')
  return { items }
}

export function candidateOriginLabel(value: ScenarioModelCandidateOrigin): string {
  return ({ assistant: '智能顾问', manual: '人工创建', imported: '外部导入', unknown: '来源未知' })[value]
}

export function candidateValidationLabel(value: ScenarioModelCandidateValidationStatus): string {
  return ({ not_validated: '待校验', valid: '校验通过', invalid: '校验未通过' })[value]
}

export function candidateLifecycleLabel(value: ScenarioModelCandidateLifecycleStatus): string {
  return ({ candidate: '候选', deferred: '已暂缓', formalized: '已正式化', resolved: '已关联正式定义', superseded: '已被替代' })[value]
}

export function candidateActivationLabel(value: ScenarioModelCandidateActivationStatus): string {
  return ({ inactive: '未激活', active: '已激活', not_applicable: '无需激活' })[value]
}

export function candidateBlockerLocation(item: ScenarioModelCandidateBlocker): string {
  if (item.field_path?.length) return item.field_path.join(' › ')
  return clean(item.path || item.field)
}
