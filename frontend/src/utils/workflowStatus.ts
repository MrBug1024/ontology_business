export type WorkflowStatus = 'draft' | 'active' | 'disabled'

/** Only an explicit user selection changes execution eligibility. */
export function workflowStatusSelection(value: unknown): { status: WorkflowStatus; enabled: boolean } | null {
  if (value !== 'draft' && value !== 'active' && value !== 'disabled') return null
  return { status: value, enabled: value === 'active' }
}
