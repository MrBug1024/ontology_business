import { http } from '@/api'
import type {
  CapabilityAccessManifest,
  ExternalApiScope,
  IntegrationKey,
  IntegrationKeyCreated,
  ScenarioReleaseWithdrawal,
} from '@/types/capabilityAccess'

export const capabilityAccessApi = {
  getManifest: (scenarioId: string, environment: 'dev' | 'staging' | 'prod') =>
    http.get<CapabilityAccessManifest>(
      `/developer/capability-access/${scenarioId}/manifest`,
      { params: { environment } },
    ),
  listKeys: () => http.get<IntegrationKey[]>('/developer/api-keys'),
  createKey: (payload: {
    name: string
    scopes: ExternalApiScope[]
    expires_in_days: number
  }) => http.post<IntegrationKeyCreated>('/developer/api-keys', payload),
  revokeKey: (keyId: string) => http.delete<IntegrationKey>(`/developer/api-keys/${keyId}`),
  withdrawRelease: (
    scenarioId: string,
    environment: 'staging' | 'prod',
    reason: string,
  ) => http.post<ScenarioReleaseWithdrawal>(
    `/scenarios/${scenarioId}/releases/${environment}/withdraw`,
    { confirmed: true, reason },
  ),
}
