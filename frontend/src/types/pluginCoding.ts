import type { PackageCapabilityKind, PluginHost, ScenarioPackageBuild } from '@/types/scenarioPackage'
import type { CodingDeliveryProfile, CodingScenarioBlueprint } from './pluginBlueprint'
export interface CodingResourceSelection {
  llm_config_id: string
  skill_ids: string[]
  mcp_ids: string[]
}
export interface CodingResourceCatalog {
  models: Array<{ id: string; name: string; model: string; supports_tools: boolean }>
  skills: Array<{ id: string; name: string; description: string; version: string; mode: 'instructions' }>
  mcps: Array<{ id: string; name: string; transport: 'sse' | 'streamable_http'; mode: 'read_only_resources' }>
  base_tools: Array<{ key: string; title: string; description: string }>
}
export interface CodingResourceReceipt {
  tool: string
  title: string
  read_only: true
  run_id: string
  content_sha256: string | null
  retrieved_at: string
}
export interface CodingSettingsUpdate extends CodingResourceSelection {
  expected_revision: number
  request_id: string
}
export interface CodingCapability {
  kind: PackageCapabilityKind
  key: string
  name: string
  description: string
  definition_hash: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  side_effect: boolean
  requires_confirmation: boolean
  idempotency_required: boolean
  data_ports: Array<{ key?: string; name?: string; required?: boolean }>
  readiness: { ready: boolean; issues: Array<{ code: string; message: string; blocking: boolean }> }
}
export interface CodingContext {
  scenario: { id: string; name: string; description: string }
  deployment: { definition_source: string; release_id: string; snapshot_id: string; definition_hash: string }
  capabilities: CodingCapability[]
  scenario_blueprint: CodingScenarioBlueprint
  delivery_profile: CodingDeliveryProfile
}
export interface CodingTask { id: string; release_id: string; scenario_id: string; title: string; plugin_version: string; host: PluginHost; phase: string; created_at: string }
export interface CodingFile { path: string; content: string; previous: string; editable: boolean }
export interface CodingWorkspace {
  id: string
  release_id: string
  revision: number
  phase: 'draft' | 'generating' | 'ready_for_review' | 'validation_failed' | 'released'
  source_phase?: CodingWorkspace['phase']
  plugin_version: string
  host: PluginHost
  files_hash: string
  files: CodingFile[]
  events: { sequence: number; kind: string; message: string; path: string; run_id?: string | null }[]
  turns: { id: string; instruction: string; status: string; created_at: string; mode?: 'generate' | 'discuss' }[]
  validation: string[]
  active_run_id: string | null
  run_status: string | null
  exported_count: number
  capabilities: CodingCapability[]
  business_acceptance_required: boolean
  resource_selection: CodingResourceSelection
  resource_receipts: CodingResourceReceipt[]
  scenario_blueprint?: CodingScenarioBlueprint | null
  delivery_profile?: CodingDeliveryProfile | null
}
export interface CodingCreate extends ScenarioPackageBuild {
  request_id: string
  llm_config_id: string
  instruction: string
  plugin_version: string
  skill_ids?: string[]
  mcp_ids?: string[]
}
export type CodingDraftCreate = Omit<CodingCreate, 'acceptance_cases' | 'confirmed_business_acceptance'>
export interface CodingUpdate {
  expected_revision: number
  request_id: string
  action: 'generate' | 'save' | 'discuss' | 'stop'
  base_files_hash: string
  instruction: string
  files: { path: string; content: string }[]
  plugin_version?: string
}
export interface CodingExport {
  expected_revision: number
  files_hash: string
  format: 'plugin' | 'marketplace'
  confirmed_code_review: true
}
