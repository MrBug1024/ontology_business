import type { DistillationTurn } from '../types/distillationConversation'

export function isWorking(turn: DistillationTurn): boolean { return turn.status === 'queued' || turn.status === 'running' }
export function latestArtifactProposal(turns: DistillationTurn[], projectId: string, revision: number): DistillationTurn | undefined {
  return turns.filter(turn => turn.project_id === projectId && turn.base_revision === revision && turn.status === 'succeeded' && turn.proposal && !turn.applied_revision)
    .sort((left, right) => right.turn_number - left.turn_number)[0]
}
export function mergeTurns(current: DistillationTurn[], received: DistillationTurn[]): DistillationTurn[] {
  const turns = new Map(current.map(turn => [turn.id, turn]))
  for (const turn of received) {
    const existing = turns.get(turn.id)
    if (!existing || turn.updated_at >= existing.updated_at) turns.set(turn.id, { ...turn, applied_revision: turn.applied_revision ?? existing?.applied_revision ?? null })
  }
  return [...turns.values()].sort((left, right) => left.turn_number - right.turn_number)
}
export function conversationTitle(message: string): string {
  return message.trim().split(/[\n。！？]/, 1)[0]?.slice(0, 36) || '新的业务探索'
}
export function composeClarificationAnswer(input: string, answer: string, previousAnswer?: string): string {
  if (previousAnswer) {
    const index = input.indexOf(previousAnswer)
    const before = index >= 0 ? input.slice(0, index) : ''
    const after = index >= 0 ? input.slice(index + previousAnswer.length) : ''
    if (index >= 0 && (!before || before.endsWith('\n\n')) && (!after || after.startsWith('\n\n'))) {
      return before + answer + after
    }
  }
  return input.trim() ? `${input}\n\n${answer}` : answer
}
export const TURN_STATUS_LABELS = { queued: '等待开始', running: '正在查证与思考', waiting: '等待你的补充', succeeded: '已回复', cancelled: '已停止', failed: '本轮未完成' }
