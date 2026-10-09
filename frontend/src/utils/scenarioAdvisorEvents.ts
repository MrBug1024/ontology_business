export const OPEN_SCENARIO_MODELING_ADVISOR_EVENT = 'open-scenario-modeling-advisor'
export const SCENARIO_DISCOVERY_CONTEXT_CHANGED_EVENT = 'scenario-discovery-context-changed'

export interface OpenScenarioModelingAdvisorDetail { scenario_id: string; prompt?: string }
export interface ScenarioDiscoveryContextChangedDetail { scenario_id: string }

export function openScenarioModelingAdvisor(detail: OpenScenarioModelingAdvisorDetail) {
  window.dispatchEvent(new CustomEvent<OpenScenarioModelingAdvisorDetail>(OPEN_SCENARIO_MODELING_ADVISOR_EVENT, { detail }))
}

export function notifyScenarioDiscoveryContextChanged(scenarioId: string) {
  if (scenarioId) window.dispatchEvent(new CustomEvent<ScenarioDiscoveryContextChangedDetail>(SCENARIO_DISCOVERY_CONTEXT_CHANGED_EVENT, { detail: { scenario_id: scenarioId } }))
}
