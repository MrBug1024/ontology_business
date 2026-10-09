import { http } from '@/api'
import type { PluginPublication, PluginPublicationUpdate } from '@/types/pluginPublication'
export const pluginPublicationsApi = {
  get: (artifactId: string, signal: AbortSignal) => http.get<PluginPublication>(`/plugin-artifacts/${artifactId}/publication`, { signal }),
  update: (artifactId: string, value: PluginPublicationUpdate, signal: AbortSignal) => http.post<PluginPublication>(`/plugin-artifacts/${artifactId}/publication`, value, { signal }),
}
