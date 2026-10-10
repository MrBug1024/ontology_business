import type { PluginHost } from './scenarioPackage'

export interface PluginArtifact {
  id: string
  workspace_id: string
  scenario_id: string
  scenario_name: string
  release_id: string
  release_name: string
  package_name: string
  plugin_version: string
  host: PluginHost
  host_label: string
  artifact_hash: string
  created_at: string
  available: boolean
  unavailable_reason: string
  retired: boolean
  retired_at: string | null
}
export interface PluginArtifactPage {
  items: PluginArtifact[]
  offset: number
  limit: number
  has_more: boolean
}
