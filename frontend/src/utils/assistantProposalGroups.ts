export interface ScenarioModelIssue {
  code: string
  message: string
  blocking: boolean
  sourceRefs: string[]
  resolutionHint?: string
}

export interface ScenarioModelIssueGroup {
  key: string
  code: string
  blocking: boolean
  count: number
  blockingCount: number
  affectedCount: number
  message: string
  resolutionHint?: string
  issues: ScenarioModelIssue[]
}

const ISSUE_LABELS: Record<string, string> = {
  uncategorized: '未分类预检问题',
  unknown_rule_field: '规则字段未定义',
  missing_reference: '引用对象不存在',
  invalid_modeled_coverage: '建模覆盖证据不足',
  invalid_rule_condition: '规则条件不受支持',
  invalid_entity: '对象类型定义无效',
  chunk_resource_conflict: '分段资源冲突',
  inconsistent_source_coverage: '来源覆盖不一致',
  missing_primary_key: '缺少主键',
  existing_property_conflict: '现有属性冲突',
  invalid_combined_entity: '组合对象定义无效',
  invalid_workflow: '工作流定义无效',
  invalid_relation_constraints: '关系约束格式不正确',
  invalid_property_constraints: '属性约束格式不正确',
  invalid_relation_constraint_endpoints: '关系公理不适用于当前端点',
  relation_axiom_requires_relation_constraint: '本体公理建模位置不正确',
  unsupported_class_axiom: '类本体公理暂未支持',
  invalid_workflow_trigger: '工作流触发配置不完整',
  missing_workflow_resource_refs: '工作流节点缺少业务资源',
  multiple_primary_keys: '主键不唯一',
  multiple_title_properties: '标题属性不唯一',
  document_reported_issue: '文档识别待确认',
  mapping_deferred_no_data_source: '数据映射等待数据源',
  missing_data_source: '数据映射等待数据源',
  prerequisite_draft_only: '前置任务仅保留草稿',
  invalid_task_plan: '建模任务计划标识异常',
  invalid_task_dependency: '建模任务依赖异常',
  invalid_task_state: '建模任务状态异常',
  data_source_dependency: '数据源尚未接入或绑定',
}

const DATA_SOURCE_CODES = new Set([
  'missing_data_source',
  'mapping_deferred_no_data_source',
  'data_source_not_configured',
  'data_source_unavailable',
  'missing_mapping_table',
  'uninspected_relation_mapping_table',
  'missing_relation_mapping_table',
  'data_source_dependency',
])

function normalizedCode(value: unknown) {
  const code = typeof value === 'string' ? value.trim() : ''
  return code || 'uncategorized'
}

function effectiveCode(issue: Record<string, unknown>) {
  const code = normalizedCode(issue.code).toLocaleLowerCase()
  const reported = normalizedCode(issue.reported_code).toLocaleLowerCase()
  const selected = code === 'document_reported_issue' && reported !== 'uncategorized'
    ? reported
    : code
  const message = normalizedMessage(issue.message).toLocaleLowerCase()
  if (DATA_SOURCE_CODES.has(selected) || (
    selected === 'missing_reference'
    && ['数据源', '物理表', '数据表', 'data source'].some((token) => message.includes(token))
  )) return 'data_source_dependency'
  return selected
}

function normalizedMessage(value: unknown) {
  if (typeof value === 'string' && value.trim()) return value.trim()
  return '未提供问题说明'
}

function normalizedSourceRefs(value: unknown) {
  if (!Array.isArray(value)) return []
  return [...new Set(value
    .filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    .map((item) => item.trim()))]
}

/** Collapse repeated resource-level failures into one user-facing root cause. */
export function groupScenarioModelIssues(value: unknown): ScenarioModelIssueGroup[] {
  if (!Array.isArray(value)) return []

  const groups = new Map<string, ScenarioModelIssueGroup>()
  value.forEach((rawIssue) => {
    const issueRecord: Record<string, unknown> = rawIssue && typeof rawIssue === 'object'
      ? rawIssue as Record<string, unknown>
      : { message: rawIssue }
    const code = effectiveCode(issueRecord)
    const explicitCount = Number(issueRecord.count)
    const count = Number.isFinite(explicitCount) && explicitCount > 0 ? Math.trunc(explicitCount) : 1
    const explicitBlockingCount = Number(issueRecord.blocking_count)
    const blockingCount = Number.isFinite(explicitBlockingCount) && explicitBlockingCount >= 0
      ? Math.min(Math.trunc(explicitBlockingCount), count)
      : issueRecord.blocking !== false ? count : 0
    const blocking = blockingCount > 0
    const key = code
    const issue: ScenarioModelIssue = {
      code,
      message: normalizedMessage(issueRecord.message),
      blocking,
      sourceRefs: normalizedSourceRefs(issueRecord.source_refs),
      resolutionHint: typeof issueRecord.resolution_hint === 'string'
        ? issueRecord.resolution_hint.trim()
        : '',
    }
    const existing = groups.get(key)
    if (existing) {
      existing.count += count
      existing.blockingCount += blockingCount
      existing.blocking = existing.blockingCount > 0
      existing.affectedCount += Math.max(Number(issueRecord.affected_count) || count, 0)
      if (!existing.resolutionHint && issue.resolutionHint) existing.resolutionHint = issue.resolutionHint
    } else {
      groups.set(key, {
        key,
        code,
        blocking,
        count,
        blockingCount,
        affectedCount: Math.max(Number(issueRecord.affected_count) || count, 0),
        message: code === 'data_source_dependency'
          ? '数据源、物理表或字段尚未接入或绑定。'
          : issue.message,
        resolutionHint: code === 'data_source_dependency'
          ? '接入并检查数据源后，把现有逻辑映射绑定到真实表和字段；无需重建其他草稿。'
          : issue.resolutionHint,
        // One representative is enough for diagnostics; the count preserves
        // scale without rendering dozens of near-identical resource rows.
        issues: [issue],
      })
    }
  })

  return [...groups.values()].sort((left, right) => {
    if (left.blocking !== right.blocking) return left.blocking ? -1 : 1
    if (left.count !== right.count) return right.count - left.count
    return left.code.localeCompare(right.code, 'zh-CN')
  })
}

export function scenarioModelIssueLabel(code: string) {
  return ISSUE_LABELS[code.trim().toLocaleLowerCase()] || '其他预检问题'
}
