import type { DistillationTurn } from '../types/distillationConversation'

export function isWorking(turn: DistillationTurn | undefined | null): boolean { return turn?.status === 'queued' || turn?.status === 'running' }
export interface AssistantMessagePart {
  kind: 'thinking' | 'answer'
  content: string
  streaming: boolean
}
export function splitAssistantMessage(content: string): AssistantMessagePart[] {
  const parts: AssistantMessagePart[] = []
  const opening = /<think>/ig
  const closing = /(?:<\/think>|<\\think>)/ig
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = opening.exec(content)) !== null) {
    const before = content.slice(cursor, match.index)
    if (before.trim()) parts.push({ kind: 'answer', content: before, streaming: false })
    closing.lastIndex = match.index + match[0].length
    const end = closing.exec(content)
    if (end) {
      const thought = content.slice(match.index + match[0].length, end.index)
      if (thought.trim()) parts.push({ kind: 'thinking', content: thought, streaming: false })
      cursor = end.index + end[0].length
      opening.lastIndex = cursor
    } else {
      const thought = content.slice(match.index + match[0].length)
      if (thought.trim()) parts.push({ kind: 'thinking', content: thought, streaming: true })
      cursor = content.length
      break
    }
  }
  const remainder = content.slice(cursor)
  if (remainder.trim()) parts.push({ kind: 'answer', content: remainder, streaming: false })
  return parts
}
export function latestArtifactProposal(turns: DistillationTurn[], projectId: string, revision: number): DistillationTurn | undefined {
  return turns.filter(turn => turn.project_id === projectId && turn.base_revision === revision
    && (turn.status === 'succeeded' || isWorking(turn)) && turn.proposal && !turn.applied_revision)
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
