import type { PluginHost } from './scenarioPackage'

export interface PluginInstallation {
  host: PluginHost
  scope: 'local_test' | 'public'
  marketplace_name: string
  package_name: string
  plugin_version: string
  marketplace_url: string
  marketplace_sha256: string
  installer_url: string
  installer_sha256: string
  powershell_command: string
  bash_command: string
  usage_command: string
  requirements: string[]
  configuration_notes: string[]
}
export interface PluginPublication {
  publication_id: string
  artifact_id: string
  revision: number
  status: 'unpublished' | 'published' | 'withdrawn'
  published_at: string | null
  configuration_ready: boolean
  available: boolean
  unavailable_reason: string
  installation: PluginInstallation | null
}
export interface PluginPublicationUpdate {
  artifact_hash: string
  expected_revision: number
  action: 'publish' | 'withdraw'
  confirmed_publication: boolean
}
