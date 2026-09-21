export const SCENARIO_STAGES = [
  'distillation',
  'materials',
  'ontology',
  'instances',
  'mappings',
  'functions',
  'actions',
  'rules',
  'events',
  'workflows',
  'capability-inputs',
  'candidates',
] as const

export type ScenarioStage = typeof SCENARIO_STAGES[number]

const SCENARIO_STAGE_NAMES = new Set<string>(SCENARIO_STAGES)

export function normalizeScenarioStage(value: unknown): ScenarioStage {
  const candidate = Array.isArray(value) ? value[0] : value
  return typeof candidate === 'string' && SCENARIO_STAGE_NAMES.has(candidate)
    ? candidate as ScenarioStage
    : 'distillation'
}
