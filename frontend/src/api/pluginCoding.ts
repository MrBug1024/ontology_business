import { http } from '@/api'
import type { CodingContext, CodingCreate, CodingDraftCreate, CodingExport, CodingProject, CodingProjectSummary, CodingResourceCatalog, CodingSession, CodingSettingsUpdate, CodingUpdate, CodingWorkspace } from '@/types/pluginCoding'
import type { PluginHost, ScenarioPackageBuild } from '@/types/scenarioPackage'
import type { PluginArtifact } from '@/types/pluginArtifact'
export const pluginCodingApi = {
  resources: (scenarioId: string, signal: AbortSignal) =>
    http.get<CodingResourceCatalog>('/plugin-coding/resources', { params: { scenario_id: scenarioId || undefined }, signal }),
  settings: (id: string, payload: CodingSettingsUpdate, signal: AbortSignal) =>
    http.post<CodingWorkspace>(`/plugin-workspaces/${id}/settings`, payload, { signal }),
  projects: (scenarioId: string, signal: AbortSignal) =>
    http.get<CodingProjectSummary[]>('/plugin-projects', { params: { scenario_id: scenarioId }, signal }),
  sessions: (scenarioId: string, signal: AbortSignal) =>
    http.get<CodingSession[]>('/plugin-workspaces', { params: { scenario_id: scenarioId }, signal }),
  context: (releaseId: string, signal: AbortSignal, target: PluginHost = 'claude_code') => http.get<CodingContext>(`/scenario-releases/${releaseId}/plugin-context`, { params: { target }, signal }),
  start: (releaseId: string, payload: CodingDraftCreate, signal: AbortSignal) => http.post<CodingWorkspace>(`/scenario-releases/${releaseId}/plugin-drafts`, payload, { signal }),
  create: (releaseId: string, payload: CodingCreate, signal: AbortSignal) =>
    http.post<CodingWorkspace>(`/scenario-releases/${releaseId}/plugin-workspaces`, payload, { signal }),
  get: (id: string, signal: AbortSignal, sessionId?: string) => http.get<CodingWorkspace>(`/plugin-workspaces/${id}`, { params: { session_id: sessionId || undefined }, signal }),
  project: (id: string, signal: AbortSignal) => http.get<CodingProject>(`/plugin-workspaces/${id}/files`, { signal }),
  revise: (id: string, payload: CodingUpdate, signal: AbortSignal) =>
    http.post<CodingWorkspace>(`/plugin-workspaces/${id}/revisions`, payload, { signal }),
  export: (id: string, payload: CodingExport, signal: AbortSignal) =>
    http.post<Blob>(`/plugin-workspaces/${id}/artifact`, payload, { responseType: 'blob', signal }),
  review: (id: string, payload: Omit<CodingExport, 'format'> & { acceptance?: ScenarioPackageBuild }, signal: AbortSignal) =>
    http.post<PluginArtifact>(`/plugin-workspaces/${id}/review`, payload, { signal }),
}
