import type { DistillationDecision } from './businessDistillation'

export interface DiscoveryProcessNode {
  key: string
  name: string
  owner: string
  outcome: string
  trigger: string
  inputs: string
  rule: string
  exceptions: string
}

export interface DiscoveryProcess {
  nodes: DiscoveryProcessNode[]
  total_nodes: number
  has_more: boolean
}

export interface ScenarioDiscoveryContext {
  version: 'scenario-discovery-context.v1'
  scenario: { id: string; name: string; description: string }
  revision: number | null
  business: {
    beneficiary: string
    pain: string
    desired_outcome: string
    success_metric: string
    scope: string
    non_goals: string
    decision: DistillationDecision
    decision_reason: string
    open_questions: string[]
  }
  handoff: {
    status: 'missing' | 'current' | 'stale'
    publication_id: string | null
    publication_revision: number | null
  }
  construction: { can_continue: boolean; reason: string }
  processes: { as_is: DiscoveryProcess; to_be: DiscoveryProcess }
  historical_cases: {
    items: Array<{ key: string; title: string; result_summary: string; limitations: string }>
    total_count: number
    has_more: boolean
  }
  materials: {
    sources: Array<{ data_source_id: string; name: string; type: string; scope: 'scenario' | 'shared'; resource_scope: 'modeling'; content_read: false }>
    has_more: boolean
    next_offset: number | null
  }
  boundaries: string[]
}
