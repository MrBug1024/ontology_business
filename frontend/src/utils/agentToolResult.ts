export type AgentToolStatus = 'running' | 'done' | 'error'

export function agentToolResultStatus(raw: unknown): AgentToolStatus {
  if (raw === undefined) return 'running'
  let result: unknown = raw
  if (typeof result === 'string') {
    try {
      result = JSON.parse(result) as unknown
    } catch {
      return 'done'
    }
  }
  if (!result || typeof result !== 'object' || Array.isArray(result)) return 'done'
  const document = result as Record<string, unknown>
  if (document.error || document.success === false) return 'error'
  return ['failed', 'rejected', 'timed_out', 'cancelled'].includes(String(document.status || ''))
    ? 'error'
    : 'done'
}
