import { http } from '@/api'
import type {
  CapabilityAccessManifest,
  ExternalApiScope,
  IntegrationKey,
  IntegrationKeyCreated,
} from '@/types/capabilityAccess'

export const capabilityAccessApi = {
  getManifest: (scenarioId: string, releaseId: string, signal?: AbortSignal) =>
    http.get<CapabilityAccessManifest>(
      `/developer/capability-access/${scenarioId}/manifest`,
      { params: { release_id: releaseId }, signal },
    ),
  listKeys: () => http.get<IntegrationKey[]>('/developer/api-keys'),
  createKey: (payload: {
    scenario_id: string
    name: string
    scopes: ExternalApiScope[]
    expires_in_days: number
  }) => http.post<IntegrationKeyCreated>('/developer/api-keys', payload),
  revokeKey: (keyId: string) => http.delete<IntegrationKey>(`/developer/api-keys/${keyId}`),
}
