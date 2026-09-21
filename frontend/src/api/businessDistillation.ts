import { http } from '@/api'
import type { BucketFile, DataSourceCatalog, Scenario } from '@/types'
import type { DistillationDraft, DistillationProject, DistillationProposal, DistillationPublication, DistillationScenarioState, DistillationTargetSystem } from '@/types/businessDistillation'

const root = '/business-distillation'
const projectPath = (id: string) => `${root}/${encodeURIComponent(id)}`

export const businessDistillationApi = {
  list: (offset: number, signal: AbortSignal, scope = '') =>
    http.get<DistillationProject[]>(root, { params: { offset, limit: 50, scenario_id: scope && scope !== 'shared' ? scope : undefined, shared_only: scope === 'shared' ? true : undefined }, signal }),
  get: (id: string, signal: AbortSignal) => http.get<DistillationProject>(projectPath(id), { signal }),
  scenarioState: (scenarioId: string, signal: AbortSignal) =>
    http.get<DistillationScenarioState>(`${root}/scenario/${encodeURIComponent(scenarioId)}/state`, { signal }),
  systemAccess: (id: string, signal: AbortSignal) => http.get<SystemAccessStatus[]>(`${projectPath(id)}/system-access`, { signal }),
  configureSystem: (id: string, payload: { expected_revision: number; target: DistillationTargetSystem; credentials: Omit<SystemAccessInput, 'expected_revision'> | null }, signal: AbortSignal) =>
    http.put<DistillationProject>(`${projectPath(id)}/business-system`, payload, { signal }),
  authorizeSystem: (id: string, targetKey: string, payload: SystemAccessInput, signal: AbortSignal) =>
    http.post<DistillationProject>(`${projectPath(id)}/targets/${encodeURIComponent(targetKey)}/authorize`, payload, { signal }),
  revokeSystem: (id: string, targetKey: string, expectedRevision: number, signal: AbortSignal) =>
    http.post<DistillationProject>(`${projectPath(id)}/targets/${encodeURIComponent(targetKey)}/revoke`, { expected_revision: expectedRevision }, { signal }),
  create: (draft: DistillationDraft, signal: AbortSignal) => http.post<DistillationProject>(root, draft, { signal }),
  remove: (id: string, signal: AbortSignal) => http.delete<void>(projectPath(id), { signal }),
  update: (id: string, expectedRevision: number, draft: DistillationDraft, signal: AbortSignal) =>
    http.put<DistillationProject>(projectPath(id), { ...draft, expected_revision: expectedRevision }, { signal }),
  analyze: (id: string, expectedRevision: number, instructions: string, signal: AbortSignal) =>
    http.post<DistillationProposal>(`${projectPath(id)}/analyze`, { expected_revision: expectedRevision, instructions }, { signal, timeout: 180_000 }),
  publish: (id: string, expectedRevision: number, signal: AbortSignal) =>
    http.post<DistillationPublication>(`${projectPath(id)}/publish`, { expected_revision: expectedRevision }, { signal }),
  publications: (id: string, signal: AbortSignal) =>
    http.get<DistillationPublication[]>(`${projectPath(id)}/publications`, { signal }),
  scenarioPublications: (scenarioId: string, signal: AbortSignal) =>
    http.get<DistillationPublication[]>(`${root}/scenario/${encodeURIComponent(scenarioId)}/publications`, { signal }),
  publication: (id: string, publicationId: string, signal: AbortSignal) =>
    http.get<DistillationPublication>(`${projectPath(id)}/publications/${encodeURIComponent(publicationId)}`, { signal }),
  publicationById: (publicationId: string, signal: AbortSignal) =>
    http.get<DistillationPublication>(`${root}/publications/${encodeURIComponent(publicationId)}`, { signal }),
  artifact: (id: string, publicationId: string, key: string, signal: AbortSignal) =>
    http.get<Blob>(`${projectPath(id)}/publications/${encodeURIComponent(publicationId)}/artifacts/${encodeURIComponent(key)}`, { signal, responseType: 'blob' }),
  artifactByPublicationId: (publicationId: string, key: string, signal: AbortSignal) =>
    http.get<Blob>(`${root}/publications/${encodeURIComponent(publicationId)}/artifacts/${encodeURIComponent(key)}`, { signal, responseType: 'blob' }),
  deletePublication: (id: string, publicationId: string, signal: AbortSignal) =>
    http.delete<void>(`${projectPath(id)}/publications/${encodeURIComponent(publicationId)}`, { signal }),
  deleteProduct: (publicationId: string, signal: AbortSignal) =>
    http.delete<void>(`${root}/publications/${encodeURIComponent(publicationId)}`, { signal }),
  scenarioArtifact: (scenarioId: string, publicationId: string, key: string, signal: AbortSignal) =>
    http.get<Blob>(`${root}/scenario/${encodeURIComponent(scenarioId)}/publications/${encodeURIComponent(publicationId)}/artifacts/${encodeURIComponent(key)}`, { signal, responseType: 'blob' }),
  scenarios: (signal: AbortSignal) => http.get<Scenario[]>('/scenarios', { signal }),
  materials: (scenarioId: string | undefined, offset: number, limit: number, signal: AbortSignal) =>
    http.get<DataSourceCatalog>('/data-sources/catalog', {
      params: { scenario_id: scenarioId, offset, limit },
      signal,
    }),
  files: (sourceId: string, signal: AbortSignal) => http.get<BucketFile[]>(`/data-sources/${encodeURIComponent(sourceId)}/files`, { signal }),
}

export interface SystemAccessStatus {
  target_key: string
  status: 'active' | 'expired' | 'revoked' | 'scope_changed' | 'missing'
  expires_at: string | null
  authorization_basis: string
}
export interface SystemAccessInput {
  expected_revision: number
  auth_type: 'basic' | 'bearer' | 'browser'
  username: string
  secret: string
  expires_at: string
  authorization_basis: string
  authorized_readonly: true
}
