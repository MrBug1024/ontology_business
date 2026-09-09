import type { AgentTurnEvent, AgentTurnRun } from '../types/index.ts'

/** A revision is acknowledged only after its text can be applied completely. */
export function applyAgentTurnEvent(run: AgentTurnRun, event: AgentTurnEvent): AgentTurnRun {
  if (event.revision <= run.revision) return run
  let result = event.data.result || run.result
  if (event.type === 'answer_reset') result = { ...result, answer: '' }
  if (event.type === 'answer_delta') {
    const answer = typeof result.answer === 'string' ? result.answer : ''
    if (typeof event.data.delta !== 'string' || event.data.offset !== answer.length) {
      throw new Error('回答进度不连续，正在恢复连接')
    }
    result = { ...result, answer: answer + event.data.delta }
  }
  const status = event.data.status || (
    event.type === 'answer_delta' || event.type === 'answer_reset' ? run.status : event.type
  )
  return { ...run, status, revision: event.revision, result, error: event.data.error || run.error }
}
