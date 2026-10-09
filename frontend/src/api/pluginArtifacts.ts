import { http } from '@/api'
import type { PluginArtifact, PluginArtifactPage } from '@/types/pluginArtifact'
export const pluginArtifactsApi = {
  list: (scenarioId: string, offset: number, signal: AbortSignal) =>
    http.get<PluginArtifactPage>('/plugin-artifacts', { params: { scenario_id: scenarioId || undefined, offset, limit: 50 }, signal }),
  get: (id: string, signal: AbortSignal) => http.get<PluginArtifact>(`/plugin-artifacts/${id}`, { signal }),
  download: (value: PluginArtifact, format: 'plugin' | 'marketplace', signal: AbortSignal) =>
    http.post<Blob>(`/plugin-artifacts/${value.id}/download`, { artifact_hash: value.artifact_hash, format }, { responseType: 'blob', signal }),
}
