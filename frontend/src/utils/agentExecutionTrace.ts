import type { AgentTurnEvent } from '../types/index.ts'
import type { AgentExecutionStep } from '../types/agentExecutionTrace.ts'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function resultDocument(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try { return record(JSON.parse(value) as unknown) } catch { return {} }
  }
  return record(value)
}

const statusLabels: Record<string, string> = {
  running: '执行中', returned: '已返回', succeeded: '执行成功', failed: '执行失败',
  pending: '等待处理', queued: '已排队', awaiting_confirmation: '等待确认',
  awaiting_approval: '等待审批', indeterminate: '结果待核对', cancelled: '已取消',
  rejected: '已拒绝', timed_out: '已超时', unknown: '未记录结果',
}
const toolLabels: Record<string, string> = {
  list_available_capabilities: '查询可用能力',
  list_capabilities: '查询可用能力', describe_capability: '查看能力契约',
  invoke_capability: '调用业务能力', get_invocation: '查询执行结果',
}
const phaseLabels: Record<string, string> = {
  accepted: '接收请求', preparing_inputs: '准备输入资料', validating_contracts: '校验输入契约',
  planning: '规划处理步骤', cancel_requested: '请求取消', succeeded: '回答完成',
  failed: '对话处理失败', cancelled: '对话已取消', indeterminate: '对话结果待核对',
}

export function executionStatusLabel(status: string): string { return statusLabels[status] || '状态待核对' }
export function executionToolLabel(name: string): string { return toolLabels[name] || name }
export function executionNeedsAttention(status: string): boolean {
  return ['failed', 'rejected', 'timed_out', 'indeterminate', 'unknown'].includes(status)
}

export function executionSteps(events: AgentTurnEvent[], history: unknown[], active: boolean): AgentExecutionStep[] {
  const steps = new Map<string, AgentExecutionStep>()
  const currentCalls = new Map<string, string>()
  for (const event of events) {
    const tool = event.data.tool_step
    if (!tool) continue
    const key = tool.phase === 'started' ? `event-${event.revision}` : currentCalls.get(tool.call_id) || `event-${event.revision}`
    const previous = steps.get(key)
    steps.set(key, {
      ...previous, step_key: key, call_id: tool.call_id, name: tool.name, status: tool.status,
      ...(tool.phase === 'started' ? { started_at: event.created_at } : { finished_at: event.created_at }),
      ...(tool.invocation_id ? { invocation_id: tool.invocation_id } : {}),
      ...(tool.error_code ? { error_code: tool.error_code } : {}),
    })
    if (tool.phase === 'started') currentCalls.set(tool.call_id, key)
    else currentCalls.delete(tool.call_id)
  }
  const matched = new Set<string>()
  for (const [index, value] of history.entries()) {
    const call = record(value)
    const id = typeof call.id === 'string' ? call.id : `historic-${index}`
    const previous = [...steps.values()].find(step => step.call_id === id && !matched.has(step.step_key))
    const key = previous?.step_key || `historic-${index}`
    matched.add(key)
    // A recorded result belongs to this occurrence, even when a model reuses an ID.
    if (previous?.finished_at) continue
    const result = resultDocument(call.result)
    const error = record(result.error)
    const reportedStatus = typeof result.status === 'string' ? result.status : ''
    const failed = result.error || result.ok === false || result.success === false
    const status = failed && !['indeterminate', 'cancelled', 'rejected', 'timed_out', 'awaiting_confirmation', 'awaiting_approval'].includes(reportedStatus) ? 'failed'
      : reportedStatus ? reportedStatus
        : call.result !== undefined ? 'returned' : previous?.status || (active ? 'running' : 'unknown')
    steps.set(key, {
      ...previous, step_key: key, call_id: id, name: typeof call.name === 'string' ? call.name : '工具调用', status,
      ...(typeof result.invocation_id === 'string' && /^[a-f0-9]{32}$/i.test(result.invocation_id)
        ? { invocation_id: result.invocation_id } : {}),
      ...(typeof error.code === 'string' && /^[A-Za-z0-9_.:-]{1,80}$/.test(error.code)
        ? { error_code: error.code } : {}),
    })
  }
  return [...steps.values()].map(step => !active && step.status === 'running' && !step.finished_at
    ? { ...step, status: 'unknown' } : step)
}

export function executionPhases(events: AgentTurnEvent[]) {
  return events.filter(event => phaseLabels[event.type]).map(event => ({
    revision: event.revision, label: phaseLabels[event.type], created_at: event.created_at,
    error_code: event.data.error?.code,
  }))
}

/** Only public assistant text preceding a tool call; never private model reasoning. */
export function executionAnalysis(events: AgentTurnEvent[]) {
  let text = ''
  let offset = 0
  const notes: { revision: number; text: string }[] = []
  for (const event of events) {
    if (event.type === 'answer_reset') { text = ''; offset = 0 }
    if (event.type === 'answer_delta') {
      if (event.data.offset === offset && typeof event.data.delta === 'string') {
        text = (text + event.data.delta).slice(0, 16_000)
        offset += event.data.delta.length
      }
    }
    if (event.type === 'invoking_tools' && event.data.tool_step?.phase !== 'finished' && text.trim()) {
      notes.push({ revision: event.revision, text })
      text = ''
    }
  }
  return notes
}
