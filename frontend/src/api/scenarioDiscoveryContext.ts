import { http } from '@/api'
import type { ScenarioDiscoveryContext } from '@/types/scenarioDiscoveryContext'

export const scenarioDiscoveryContextApi = {
  get: (scenarioId: string, signal: AbortSignal) =>
    http.get<ScenarioDiscoveryContext>(`/business-distillation/scenario/${encodeURIComponent(scenarioId)}/context`, { signal }),
}
