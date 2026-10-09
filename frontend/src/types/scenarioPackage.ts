export type PackageCapabilityKind = 'function' | 'action' | 'rule' | 'workflow'
export type PluginHost = 'claude_code' | 'codex'
export type AcceptanceRole = 'success' | 'boundary' | 'failure'
export type AcceptanceStatus = 'succeeded' | 'failed' | 'rejected'
export interface PackageCapability { kind: PackageCapabilityKind; key: string }
export interface PackageAcceptanceCase extends PackageCapability {
  role: AcceptanceRole
  invocation_id: string
  expected_status: AcceptanceStatus
}
export interface ScenarioPackageBuild {
  expected_revision: number
  target: PluginHost
  capabilities: PackageCapability[]
  acceptance_cases: PackageAcceptanceCase[]
  confirmed_business_acceptance: boolean
}
export interface PackageEvidence extends PackageCapability {
  invocation_id: string
  status: AcceptanceStatus
  created_at: string
  workflow_status: string | null
  eligible: boolean
}
