export const OPEN_SCENARIO_MODELING_ADVISOR_EVENT = 'open-scenario-modeling-advisor'

export interface OpenScenarioModelingAdvisorDetail { scenario_id: string; prompt?: string }

export function openScenarioModelingAdvisor(detail: OpenScenarioModelingAdvisorDetail) {
  window.dispatchEvent(new CustomEvent<OpenScenarioModelingAdvisorDetail>(OPEN_SCENARIO_MODELING_ADVISOR_EVENT, { detail }))
}
