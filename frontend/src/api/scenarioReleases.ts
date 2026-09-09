import { http } from '@/api'
import type { ReleaseAction, ScenarioRelease, ScenarioReleasePage } from '@/types/scenarioRelease'

export const scenarioReleasesApi = {
  list: (scenarioId: string, offset: number, signal: AbortSignal) =>
    http.get<ScenarioReleasePage>('/scenario-releases', {
      params: { scenario_id: scenarioId || undefined, offset, limit: 50 }, signal,
    }),
  create: (payload: { scenario_id: string; name: string; notes: string; confirmed: true }) =>
    http.post<ScenarioRelease>('/scenario-releases', payload),
  change: (release: ScenarioRelease, action: ReleaseAction) =>
    http.patch<ScenarioRelease>(`/scenario-releases/${release.id}`, {
      action, expected_revision: release.revision,
    }),
}
