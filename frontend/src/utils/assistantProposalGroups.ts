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
}

function normalizedCode(value: unknown) {
  const code = typeof value === 'string' ? value.trim() : ''
  return code || 'uncategorized'
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

/**
 * Keeps every compiler issue visible while reducing a large unresolved list to
 * stable, severity-aware disclosure groups. `blocking: false` is intentionally
 * the only non-blocking value so malformed data fails closed in the UI.
 */
export function groupScenarioModelIssues(value: unknown): ScenarioModelIssueGroup[] {
  if (!Array.isArray(value)) return []

  const groups = new Map<string, ScenarioModelIssueGroup>()
  value.forEach((rawIssue) => {
    const issueRecord: Record<string, unknown> = rawIssue && typeof rawIssue === 'object'
      ? rawIssue as Record<string, unknown>
      : { message: rawIssue }
    const code = normalizedCode(issueRecord.code)
    const blocking = issueRecord.blocking !== false
    const key = `${blocking ? 'blocking' : 'notice'}:${code.toLocaleLowerCase()}`
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
    if (existing) existing.issues.push(issue)
    else groups.set(key, { key, code, blocking, issues: [issue] })
  })

  return [...groups.values()].sort((left, right) => {
    if (left.blocking !== right.blocking) return left.blocking ? -1 : 1
    if (left.issues.length !== right.issues.length) return right.issues.length - left.issues.length
    return left.code.localeCompare(right.code, 'zh-CN')
  })
}

export function scenarioModelIssueLabel(code: string) {
  return ISSUE_LABELS[code.trim().toLocaleLowerCase()] || '其他预检问题'
}
