export type ExternalApiScope =
  | 'scenarios:read'
  | 'objects:read'
  | 'capabilities:read'
  | 'capabilities:invoke'
  | 'assets:write'

export interface IntegrationKey {
  id: string
  tenant_id: string
  user_id: string
  issued_by_user_id?: string | null
  revoked_by_user_id?: string | null
  name: string
  key_prefix: string
  token_hint: string
  scopes: ExternalApiScope[]
  status: 'active' | 'revoked'
  expires_at?: string | null
  last_used_at?: string | null
  revoked_at?: string | null
  created_at: string
}

export interface IntegrationKeyCreated extends IntegrationKey {
  token: string
}

export interface CapabilityAccessManifest {
  manifest_version: 'capability-access-manifest/v1'
  manifest_id: string
  scenario: { id: string; name: string }
  deployment: {
    environment: 'dev' | 'staging' | 'prod'
    definition_source: 'live' | 'release'
    release_id?: string | null
    snapshot_id?: string | null
    definition_hash: string
  }
  capabilities: Array<{
    kind: 'function' | 'action' | 'rule' | 'workflow' | 'query' | 'provider'
    key: string
    name: string
    input_schema_hash: string
    output_schema_hash: string
    side_effect: boolean
    requires_confirmation: boolean
    idempotency_required: boolean
    ready: boolean
    blocking_codes: string[]
    data_ports: Array<{
      key: string
      name: string
      direction: 'input' | 'output'
      role: string
      media_kind: string
      schema_hash: string
      required: boolean
      cardinality: string
      binding_policy: string
    }>
  }>
  adapters: Array<{
    protocol: 'rest' | 'mcp'
    endpoint: string
    discovery?: string | null
    invocation?: string | null
    receipt?: string | null
    managed_input_upload?: string | null
    authentication: Record<string, string>
    required_scopes: ExternalApiScope[]
    optional_scopes?: ExternalApiScope[]
    tools: string[]
  }>
  release_history: Array<{
    id: string
    snapshot_id: string
    environment: 'dev' | 'staging' | 'prod'
    status: string
    created_at: string
  }>
  checks: Array<{ code: string; passed: boolean; count?: number | null }>
}

export interface ScenarioReleaseWithdrawal {
  scenario_id: string
  environment: 'staging' | 'prod'
  withdrawn_release_ids: string[]
  changed: boolean
  withdrawn_at?: string | null
  withdrawn_by_user_id?: string | null
  reason: string
}
