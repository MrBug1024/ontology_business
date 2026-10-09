import { http } from '@/api'
import type { ScenarioPackageBuild, PackageEvidence } from '@/types/scenarioPackage'
export const scenarioPackagesApi = {
  evidence: (releaseId: string, signal: AbortSignal) =>
    http.get<PackageEvidence[]>(`/scenario-releases/${releaseId}/plugin-evidence`, { signal }),
  build: (releaseId: string, payload: ScenarioPackageBuild, signal: AbortSignal) =>
    http.post<Blob>(`/scenario-releases/${releaseId}/plugin`, payload, { responseType: 'blob', signal }),
}
