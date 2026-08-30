import type {
  ScenarioModelDraftIssue,
  ScenarioModelDraftListResponse,
  ScenarioModelDraftResource,
} from '@/types'
import { normalizeCandidateBlockers } from './candidateGovernance.ts'

export const scenarioDraftStages = [
  'ontology',
  'instances',
  'mappings',
  'functions',
  'actions',
  'rules',
  'events',
  'workflows',
  'candidates',
] as const

export type ScenarioDraftStage = typeof scenarioDraftStages[number]

const stageByKind: Record<string, ScenarioDraftStage> = {
  entity: 'ontology',
  property: 'ontology',
  relation: 'ontology',
  instance: 'instances',
  mapping: 'mappings',
  data_mapping: 'mappings',
  conceptual_mapping: 'mappings',
  relation_mapping: 'mappings',
  function: 'functions',
  action: 'actions',
  rule: 'rules',
  event: 'events',
  workflow: 'workflows',
  capability_port: 'candidates',
}

const kindLabels: Record<string, string> = {
  entity: '对象类型',
  property: '属性',
  relation: '关系类型',
  instance: '对象实例',
  mapping: '对象映射',
  data_mapping: '对象映射',
  conceptual_mapping: '逻辑映射（待绑定）',
  relation_mapping: '关系映射',
  function: '函数',
  action: '操作',
  rule: '规则',
  event: '事件',
  workflow: '工作流',
  capability_port: '能力端口',
}

const stageLabels: Record<ScenarioDraftStage, string> = {
  ontology: '本体模型',
  instances: '对象实例',
  mappings: '数据映射',
  functions: '函数',
  actions: '操作',
  rules: '规则',
  events: '事件',
  workflows: '工作流',
  candidates: '候选定义',
}

function clean(value: unknown) {
  return typeof value === 'string' ? value.trim() : ''
}

export function draftRefToken(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value).trim()
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const reference = value as Record<string, unknown>
  return draftRefToken(reference.id || reference.key || reference.resource_key || reference.ref || reference.name)
}

function record(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, any>
    : {}
}

function issue(value: unknown): ScenarioModelDraftIssue | null {
  if (typeof value === 'string' && value.trim()) return { message: value.trim() }
  const item = record(value)
  const code = clean(item.code)
  const message = code === 'draft_requires_revalidation'
    ? '工作草稿已修改，需要重新校验后才能进入正式模型。'
    : clean(item.message || item.detail || item.reason)
  if (!message) return null
  return {
    code: code || undefined,
    message,
    field: clean(item.field) || undefined,
    path: clean(item.path) || undefined,
    blocking: item.blocking === true,
    resolution_hint: code === 'draft_requires_revalidation'
      ? '重新执行场景模型校验；在校验通过前草稿继续保持停用。'
      : clean(item.resolution_hint || item.suggestion) || undefined,
    source_refs: Array.isArray(item.source_refs) ? item.source_refs.map(clean).filter(Boolean) : undefined,
  }
}

export function scenarioDraftStage(kind: unknown): ScenarioDraftStage | '' {
  return stageByKind[clean(kind)] || ''
}

export function scenarioDraftKindLabel(kind: unknown) {
  const normalized = clean(kind)
  return kindLabels[normalized] || normalized || '建模资源'
}

export function scenarioDraftStageLabel(stage: ScenarioDraftStage) {
  return stageLabels[stage]
}

export function scenarioDraftIssueCount(item: ScenarioModelDraftResource) {
  const explicit = Number(item.issues_count)
  return Number.isFinite(explicit)
    ? Math.max(Math.trunc(explicit), item.validation_issues.length, 0)
    : item.validation_issues.length
}

export function scenarioDraftBlockingIssueCount(item: ScenarioModelDraftResource) {
  const explicit = Number(item.blocking_issue_count)
  const counted = item.validation_issues.filter((entry) => entry.blocking).length
  return Number.isFinite(explicit) ? Math.max(Math.trunc(explicit), counted, 0) : counted
}

export function scenarioDraftIsOpen(item: ScenarioModelDraftResource) {
  return !['applied', 'resolved', 'superseded', 'discarded', 'promoted'].includes(clean(item.draft_status))
}

export function normalizeScenarioModelDrafts(
  response: ScenarioModelDraftListResponse | ScenarioModelDraftResource[] | null | undefined,
): ScenarioModelDraftResource[] {
  const rawItems = Array.isArray(response)
    ? response
    : Array.isArray(response?.items)
      ? response.items
      : Array.isArray(response?.drafts) ? response.drafts : []

  return rawItems.flatMap((raw, index) => {
    const item = record(raw)
    const payload = record(item.payload || item.draft_payload || item.resource_payload)
    const resourceKind = clean(item.resource_kind || item.kind || item.resource)
    const resourceKey = clean(item.resource_key || item.change_key || item.key)
    const id = clean(item.id || item.draft_id) || `${resourceKind || 'resource'}:${resourceKey || index}`
    if (!resourceKind || !scenarioDraftStage(resourceKind)) return []
    const validationIssues = (Array.isArray(item.validation_issues)
      ? item.validation_issues
      : Array.isArray(item.issues) ? item.issues : [])
      .map(issue)
      .filter((entry): entry is ScenarioModelDraftIssue => Boolean(entry))
    const title = clean(item.title || item.display_name || payload.name || payload.title || resourceKey)
    return [{
      id,
      revision: Math.max(Math.trunc(Number(item.revision) || 0), 0),
      scenario_id: clean(item.scenario_id) || undefined,
      proposal_id: clean(item.proposal_id),
      task_id: clean(item.task_id),
      resource_kind: resourceKind,
      resource_key: resourceKey,
      title: title || scenarioDraftKindLabel(resourceKind),
      payload,
      validation_issues: validationIssues,
      issues_count: Math.max(Number(item.issues_count) || 0, validationIssues.length),
      blocking_issue_count: Math.max(
        Number(item.blocking_issue_count) || 0,
        validationIssues.filter((entry) => entry.blocking).length,
      ),
      draft_status: clean(item.draft_status || item.status) || 'needs_revision',
      source: clean(item.source || item.materialization_source) || 'unknown',
      materialization_source: clean(item.materialization_source || item.source),
      source_origin: ['assistant', 'manual', 'imported'].includes(clean(item.source_origin))
        ? item.source_origin
        : 'unknown',
      validation_status: ['valid', 'invalid'].includes(clean(item.validation_status))
        ? item.validation_status
        : 'not_validated',
      lifecycle_status: ['candidate', 'deferred', 'formalized', 'resolved', 'superseded'].includes(clean(item.lifecycle_status))
        ? item.lifecycle_status
        : 'candidate',
      promotion_eligible: item.promotion_eligible === true,
      promotion_blockers: normalizeCandidateBlockers(item.promotion_blockers),
      activation_status: ['active', 'not_applicable'].includes(clean(item.activation_status))
        ? item.activation_status
        : 'inactive',
      quality_fingerprint: clean(item.quality_fingerprint),
      source_thread_id: clean(item.source_thread_id || item.thread_id) || null,
      source_message_id: clean(item.source_message_id) || null,
      compilation_job_id: clean(item.compilation_job_id) || null,
      source_refs: Array.isArray(item.source_refs) ? item.source_refs.map(clean).filter(Boolean) : [],
      resolved_resource_id: clean(item.resolved_resource_id) || undefined,
      // Staging resources are never runnable, even if a malformed response says otherwise.
      enabled: false,
      publishable: false,
      created_at: clean(item.created_at) || undefined,
      updated_at: clean(item.updated_at) || undefined,
    } satisfies ScenarioModelDraftResource]
  })
}
