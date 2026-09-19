import type { DistillationDocument } from '@/types/businessDistillation'

export const HANDOFF_CHOICES = [
  { value: 'continue', label: '继续建设', reason: '当前结论与依据足够，可以据此继续场景建设。' },
  { value: 'adjust', label: '调整方向后建设', reason: '方向值得保留，需要依据当前结论调整建设方案。' },
  { value: 'stop', label: '暂缓建设', reason: '当前价值或依据仍不足，先保留调查结论并暂缓建设。' },
] as const

export function handoffDecision(decision: DistillationDocument['decision'], reason: string) {
  const selected = HANDOFF_CHOICES.find(item => item.value === decision)
  if (!selected) throw new Error('请先明确阶段结论的下一步')
  return { decision: selected.value, decision_reason: reason.trim() || selected.reason }
}

export function handoffReasonAfterChoice(previous: DistillationDocument['decision'], next: DistillationDocument['decision'], reason: string): string {
  const previousChoice = HANDOFF_CHOICES.find(item => item.value === previous)
  const nextChoice = HANDOFF_CHOICES.find(item => item.value === next)
  return previousChoice && nextChoice && reason.trim() === previousChoice.reason ? nextChoice.reason : reason
}
