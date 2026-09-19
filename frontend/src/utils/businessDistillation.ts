import type { DistillationDocument, DistillationDraft, DistillationProject, ProcessGraph } from '@/types/businessDistillation'

export const DECISION_LABELS = { undecided: '待核对', continue: '继续建设', adjust: '调整方向', stop: '暂缓建设' }
export const ASSERTION_LABELS = { fact: '有据事实', inference: '推断', hypothesis: '待验证假设', conflict: '冲突' }

export function emptyDistillationDocument(): DistillationDocument {
  return {
    beneficiary: '', pain: '', desired_outcome: '', success_metric: '', scope: '', non_goals: '',
    evidence: [], target_systems: [], assertions: [], as_is: { nodes: [], edges: [] }, to_be: { nodes: [], edges: [] },
    improvements: [], entities: [], relations: [], lineage: [], decision: 'undecided', decision_reason: '', open_questions: [], historical_cases: [],
  }
}

export function draftOf(project?: DistillationProject): DistillationDraft {
  return project
    ? JSON.parse(JSON.stringify({ name: project.name, scenario_id: project.scenario_id, document: project.document })) as DistillationDraft
    : { name: '', scenario_id: null, document: emptyDistillationDocument() }
}

export function linesOf(value: string): string[] {
  return value.split('\n').map(line => line.trim()).filter(Boolean)
}

export function removeProcessNode(graph: ProcessGraph, key: string): ProcessGraph {
  return { nodes: graph.nodes.filter(node => node.key !== key), edges: graph.edges.filter(edge => edge.source !== key && edge.target !== key) }
}

export function removeEvidence(document: DistillationDocument, key: string): void {
  document.evidence = document.evidence.filter(item => item.key !== key)
  for (const item of [...document.assertions, ...document.as_is.nodes, ...document.to_be.nodes, ...document.lineage, ...document.entities, ...document.relations]) {
    if (item.evidence_refs) item.evidence_refs = item.evidence_refs.filter(ref => ref !== key)
    // Removing proof must never leave an unsupported statement classified as fact.
    if ('status' in item && item.status === 'fact' && !item.evidence_refs.length) item.status = 'hypothesis'
  }
  for (const item of document.historical_cases ?? []) {
    for (const field of ['result_refs', 'input_refs', 'process_refs', 'knowledge_refs'] as const) item[field] = item[field].filter(ref => ref !== key)
    for (const step of item.steps) step.evidence_refs = step.evidence_refs.filter(ref => ref !== key)
  }
}

export function reviewQuestions(document: DistillationDocument): string[] {
  const checks: [boolean, string][] = [
    [!document.beneficiary.trim(), '明确真正受益的人，以及谁负责为结果验收。'],
    [!document.pain.trim(), '说明真实矛盾和损失，而不只是列出需要的功能。'],
    [!document.desired_outcome.trim(), '描述问题得到解决时的最终结果。'],
    [!document.success_metric.trim(), '给出可以观察或核验的成功标准。'],
    [!document.evidence.length, '补充业务案例或访谈依据；目前仍是待验证的构想。'],
    [document.assertions.some(item => item.status === 'conflict'), '存在冲突证据，需要核对适用范围和例外。'],
    [!document.to_be.nodes.length, '补充达成最终结果的目标流程与责任人。'],
    [document.decision === 'undecided', '人工决定继续、调整还是暂缓建设，并说明原因。'],
    [!document.decision_reason.trim(), '写明建设决策的理由和仍需验证的条件。'],
  ]
  return checks.filter(([applies]) => applies).map(([, question]) => question)
}
