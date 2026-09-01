type ScenarioScopedAgent = {
  scenario_id?: string | null
}

export function scenarioIdFromQuery(value: unknown): string {
  const first = Array.isArray(value) ? value[0] : value
  return typeof first === 'string' ? first : ''
}

export function filterAgentsByScenario<T extends ScenarioScopedAgent>(agents: T[], scenarioId: string): T[] {
  return scenarioId ? agents.filter((agent) => agent.scenario_id === scenarioId) : agents
}
