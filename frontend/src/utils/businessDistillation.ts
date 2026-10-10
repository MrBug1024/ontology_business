import type { DistillationDocument, DistillationDraft, DistillationProject } from '@/types/businessDistillation'

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

function withoutMaterialReferences(draft: DistillationDraft): DistillationDraft {
  const copy = JSON.parse(JSON.stringify(draft)) as DistillationDraft
  const materialKeys = new Set(copy.document.evidence.filter(item => item.kind === 'material').map(item => item.key))
  copy.document.evidence = copy.document.evidence.filter(item => !materialKeys.has(item.key))
  const strip = (items: { evidence_refs?: string[] }[]) => items.forEach(item => {
    if (item.evidence_refs) item.evidence_refs = item.evidence_refs.filter(ref => !materialKeys.has(ref))
  })
  strip([...copy.document.assertions, ...copy.document.as_is.nodes, ...copy.document.to_be.nodes,
    ...copy.document.entities, ...copy.document.relations, ...copy.document.lineage])
  for (const item of copy.document.historical_cases) {
    item.result_refs = item.result_refs.filter(ref => !materialKeys.has(ref))
    item.input_refs = item.input_refs.filter(ref => !materialKeys.has(ref))
    item.process_refs = item.process_refs.filter(ref => !materialKeys.has(ref))
    item.knowledge_refs = item.knowledge_refs.filter(ref => !materialKeys.has(ref))
    strip(item.steps)
  }
  return copy
}

export function isMaterialReferenceOnlyChange(current: DistillationDraft, baseline: DistillationDraft): boolean {
  if (current.name !== baseline.name || current.scenario_id !== baseline.scenario_id) return false
  return JSON.stringify(withoutMaterialReferences(current).document) === JSON.stringify(withoutMaterialReferences(baseline).document)
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

