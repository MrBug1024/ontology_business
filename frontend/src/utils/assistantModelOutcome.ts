import type { AssistantDecisionGate, AssistantModelNextAction, AssistantModelTask } from '@/types'

export function modelTaskNeedsCorrection(
  task: Pick<AssistantModelTask, 'status'> | undefined,
  nextAction: Pick<AssistantModelNextAction, 'can_apply'> | null,
): boolean {
  return task?.status === 'blocked' && nextAction?.can_apply !== true
}

const SERVICE_FAILURE_CODES = new Set([
  'LLM_NOT_CONFIGURED', 'COMPILER_PROVIDER_REQUEST_FAILED', 'COMPILER_PROVIDER_UNAVAILABLE',
  'COMPILER_EXECUTION_INTERRUPTED',
])

export function compilationServiceFailure(unresolved: unknown): string | null {
  if (!Array.isArray(unresolved)) return null
  for (const value of unresolved) {
    if (!value || typeof value !== 'object') continue
    const issue = value as Record<string, unknown>
    if (typeof issue.code !== 'string' || !SERVICE_FAILURE_CODES.has(issue.code.toUpperCase())) continue
    return typeof issue.message === 'string' && issue.message.trim()
      ? issue.message.trim() : '模型服务未能完成本轮建设。'
  }
  return null
}

export function compilationFailureSummary(unresolved: unknown): string | null {
  const failure = compilationServiceFailure(unresolved)
  return failure ? `${failure}\n\n本轮未生成可用定义。原始资料和已有具体草稿已保留。\n\n请恢复模型服务后在当前对话重试；这不是需要补充业务资料的问题。` : null
}

export function decisionGatePresentation(gate: AssistantDecisionGate | null): {
  title: string; label: string; type: 'success' | 'warning' | 'info'
} {
  if (gate?.reason_codes?.includes('COMPILATION_UNAVAILABLE')) {
    return { title: '模型服务未完成建设', label: '恢复后重试', type: 'warning' }
  }
  if (gate?.reason_codes?.includes('CANDIDATE_VALIDATION_FAILED')) {
    return { title: '顾问生成的定义仍需修正', label: '修正定义', type: 'warning' }
  }
  if (gate?.reason_codes?.includes('SCENARIO_CONTEXT_CHANGED')) {
    return { title: '场景定义已变化', label: '重新校验候选', type: 'warning' }
  }
  if (gate?.mode === 'clarify') {
    return { title: '发现影响业务含义的关键歧义', label: '先对齐问题', type: 'warning' }
  }
  if (gate?.mode === 'candidate_review') {
    return { title: '发现副作用或外部事实风险', label: '人工审核', type: 'info' }
  }
  return { title: '证据覆盖和确定性校验已通过', label: '可直接建设', type: 'success' }
}
