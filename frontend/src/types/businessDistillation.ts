export type AssertionStatus = 'fact' | 'inference' | 'hypothesis' | 'conflict'
export type DistillationDecision = 'undecided' | 'continue' | 'adjust' | 'stop'

export interface DistillationEvidence {
  key: string
  title: string
  kind: 'material' | 'observation' | 'system_export'
  role: 'input' | 'knowledge' | 'result' | 'process' | 'reference'
  data_source_id: string | null
  bucket_file_id: string | null
  summary: string
  coverage: string
  limitations: string
  investigation_source?: { turn_id: string; step_id: string; content_sha256: string } | null
  library_read?: { turn_id: string; step_id: string; identity_sha256: string } | null
  mcp_read?: { turn_id: string; step_id: string; identity_sha256: string } | null
  interview?: { turn_id: string; message_sha256: string } | null
}

export interface DistillationAssertion {
  key: string
  statement: string
  status: AssertionStatus
  evidence_refs: string[]
}

export interface ProcessNode {
  key: string
  name: string
  owner: string
  outcome: string
  evidence_refs: string[]
  trigger?: string
  inputs?: string
  rule?: string
  exceptions?: string
}

export interface ProcessEdge { source: string; target: string; label: string }
export interface ProcessGraph { nodes: ProcessNode[]; edges: ProcessEdge[] }
export interface ProcessImprovement {
  key: string
  existing_node_key: string
  decision: 'retain' | 'remove' | 'merge' | 'replace'
  rationale: string
  expected_benefit: string
}
export interface DistillationEntity { key: string; name: string; description: string; attributes: string[]; identity?: string; evidence_refs?: string[] }
export interface DistillationRelation {
  source: string
  target: string
  label: string
  cardinality: 'unconfirmed' | 'one_to_one' | 'one_to_many' | 'many_to_many'
  rationale?: string
  evidence_refs?: string[]
}
export interface DistillationLineage { source: string; target: string; transformation: string; evidence_refs: string[] }

export interface DistillationTargetSystem {
  key: string
  name: string
  base_url: string
  purpose: string
  allowed_paths: string[]
  notes: string
  browser?: { entry_path: string; login_paths: string[]; readonly_post_paths: string[] } | null
  access_mode: 'anonymous_readonly' | 'authorized_readonly'
  enabled: boolean
}

export interface DistillationDocument {
  beneficiary: string
  pain: string
  desired_outcome: string
  success_metric: string
  scope: string
  non_goals: string
  evidence: DistillationEvidence[]
  target_systems: DistillationTargetSystem[]
  assertions: DistillationAssertion[]
  as_is: ProcessGraph
  to_be: ProcessGraph
  improvements: ProcessImprovement[]
  entities: DistillationEntity[]
  relations: DistillationRelation[]
  lineage: DistillationLineage[]
  decision: DistillationDecision
  decision_reason: string
  open_questions: string[]
  historical_cases: HistoricalCase[]
}

export interface HistoricalCase {
  key: string
  title: string
  scope: string
  result_summary: string
  result_refs: string[]
  input_refs: string[]
  process_refs: string[]
  knowledge_refs: string[]
  association_basis: string
  steps: { node_key: string; input_summary: string; action: string; output_summary: string; evidence_refs: string[] }[]
  discrepancies: string
  limitations: string
}
export interface DistillationDraft { name: string; scenario_id: string | null; document: DistillationDocument }
export interface DistillationProject extends DistillationDraft {
  id: string
  revision: number
  created_at: string
  updated_at: string
  can_write: boolean
}
export interface DistillationProposal { base_revision: number; document: DistillationDocument; limitations: string[] }
export interface DistillationArtifact { key: string; filename: string; mime: string; sha256: string }
export interface DistillationPublication {
  id: string
  project_id: string
  project_revision: number
  data_source_id: string
  created_at: string
  artifacts: DistillationArtifact[]
}
