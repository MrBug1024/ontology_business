import { http } from '@/api'
import type { PluginArtifact, PluginArtifactPage } from '@/types/pluginArtifact'
export const pluginArtifactsApi = {
  // The publishing center manages the full snapshot lifecycle, so retired
  // versions stay visible for deletion; deleted ones leave every list.
  list: (scenarioId: string, offset: number, signal: AbortSignal) =>
    http.get<PluginArtifactPage>('/plugin-artifacts', { params: { scenario_id: scenarioId || undefined, offset, limit: 50, include_retired: true }, signal }),
  get: (id: string, signal: AbortSignal) => http.get<PluginArtifact>(`/plugin-artifacts/${id}`, { signal }),
  download: (value: PluginArtifact, format: 'plugin' | 'marketplace', signal: AbortSignal) =>
    http.post<Blob>(`/plugin-artifacts/${value.id}/download`, { artifact_hash: value.artifact_hash, format }, { responseType: 'blob', signal }),
  retire: (value: PluginArtifact, signal: AbortSignal) =>
    http.post<PluginArtifact>(`/plugin-artifacts/${value.id}/retire`, { artifact_hash: value.artifact_hash }, { signal }),
  remove: (value: PluginArtifact, signal: AbortSignal) =>
    http.post<null>(`/plugin-artifacts/${value.id}/delete`, { artifact_hash: value.artifact_hash }, { signal }),
}
