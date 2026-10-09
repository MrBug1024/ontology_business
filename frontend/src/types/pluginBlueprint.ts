import type { PackageCapabilityKind, PluginHost } from './scenarioPackage'

export type CodingBlueprintCapabilityKind = PackageCapabilityKind | 'event'
export interface CodingBlueprintCapabilityRef {
  kind: CodingBlueprintCapabilityKind
  key: string
}
export interface CodingBlueprintProperty {
  key: string
  api_name: string
  name: string
  data_type: string
  description: string
  is_key: boolean
  is_required: boolean
  is_title: boolean
  is_enum: boolean
  enum_values: string[]
}
export interface CodingBlueprintObject {
  key: string
  api_name: string
  name: string
  description: string
  properties: CodingBlueprintProperty[]
}
export interface CodingBlueprintCapability extends CodingBlueprintCapabilityRef {
  name: string
  description: string
  selected: boolean
  available: boolean
  dependency: boolean
  invocation_supported: boolean
  invocation_authorized: boolean
  enabled: boolean
  ready: boolean
  semantic: {
    role: string
    runtime_kind: string | null
    trigger_type: 'manual' | 'scheduled' | 'event' | null
    node_counts: Array<{ kind: string; count: number }>
    requires_approval: boolean
    object_keys: string[]
    dependencies: CodingBlueprintCapabilityRef[]
    input_bindings: Array<{ path: string; object_key: string; many: boolean; partial: boolean }>
    output_node_keys: string[]
  }
}
export interface CodingScenarioBlueprint {
  version: 'scenario-capability-blueprint.v1'
  completeness: 'complete_authorized_projection'
  scenario: { id: string; name: string; description: string }
  deployment: { definition_source: 'release'; release_id: string; snapshot_id: string; definition_hash: string }
  stages: Array<{ key: string; label: string; contribution: string; boundary: string }>
  ontology: {
    objects: CodingBlueprintObject[]
    relations: Array<{ key: string; api_name: string; name: string; description: string; source_object_key: string; target_object_key: string; cardinality: '1:1' | '1:N' | 'N:1' | 'N:M' }>
  }
  capabilities: CodingBlueprintCapability[]
  coverage: {
    selected: CodingBlueprintCapabilityRef[]
    available: CodingBlueprintCapabilityRef[]
    dependencies: CodingBlueprintCapabilityRef[]
    unselected_available: CodingBlueprintCapabilityRef[]
  }
}
export interface CodingDeliveryProfile {
  version: 'scenario-plugin-delivery-profile.v1' | 'scenario-plugin-delivery-profile.v2'
  host: { key: PluginHost; label: string; scope: 'host_specific' }
  standards: Array<{ key: string; label: string; purpose: string; url: string }>
  components: Array<{ key: string; label: string; purpose: string; required: boolean; supported: boolean }>
  boundaries: Array<{ key: string; label: string; purpose: string }>
  platform_rules: Array<{ key: string; label: string; purpose: string }>
  protected_references: string[]
}
