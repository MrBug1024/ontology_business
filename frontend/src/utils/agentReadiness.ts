import type {
  Agent,
  AgentReadiness,
  AgentReadinessAxis,
  AgentReadinessAxisKey,
  AgentReadinessIssue,
} from '@/types'

const AXES: AgentReadinessAxisKey[] = ['definition', 'validation', 'release', 'runtime']

const FLAT_KEYS: Record<AgentReadinessAxisKey, string[]> = {
  definition: ['definition_valid', 'definition_ready'],
  validation: ['validation_ready'],
  release: ['release_ready'],
  runtime: ['runtime_ready'],
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null
}

function hasOwn(record: UnknownRecord, key: string) {
  return Object.prototype.hasOwnProperty.call(record, key)
}

function hasReadinessSignal(record: UnknownRecord) {
  return AXES.some((axis) => hasOwn(record, axis) || FLAT_KEYS[axis].some((key) => hasOwn(record, key)))
}

function readinessPayload(agent: Partial<Agent>): UnknownRecord | null {
  const nested = asRecord(agent.readiness)
  if (nested && hasReadinessSignal(nested)) return nested
  const flat = asRecord(agent)
  return flat && hasReadinessSignal(flat) ? flat : null
}

function normalizeIssue(value: unknown, axis: AgentReadinessAxisKey, index: number): AgentReadinessIssue | null {
  if (typeof value === 'string') {
    const label = value.trim()
    return label ? { code: `${axis}_issue_${index + 1}`, label } : null
  }
  const issue = asRecord(value)
  if (!issue) return null
  const code = typeof issue.code === 'string' ? issue.code.trim() : ''
  const labelSource = issue.label ?? issue.message ?? issue.reason ?? code
  const label = typeof labelSource === 'string' ? labelSource.trim() : ''
  if (!label) return null
  const normalized: AgentReadinessIssue = {
    code: code || `${axis}_issue_${index + 1}`,
    label,
  }
  if (typeof issue.target === 'string' && issue.target.trim()) normalized.target = issue.target.trim()
  if (typeof issue.blocking === 'boolean') normalized.blocking = issue.blocking
  return normalized
}

function issueList(value: unknown, axis: AgentReadinessAxisKey) {
  if (!Array.isArray(value)) return []
  const unique = new Map<string, AgentReadinessIssue>()
  value.forEach((item, index) => {
    const issue = normalizeIssue(item, axis, index)
    if (issue) unique.set(`${issue.code}:${issue.label}`, issue)
  })
  return [...unique.values()]
}

function serverAxis(payload: UnknownRecord, axis: AgentReadinessAxisKey): AgentReadinessAxis {
  const nestedAxis = asRecord(payload[axis])
  let ready: boolean | undefined
  if (typeof payload[axis] === 'boolean') ready = payload[axis] as boolean
  else if (nestedAxis && typeof nestedAxis.ready === 'boolean') ready = nestedAxis.ready

  if (ready === undefined) {
    for (const key of FLAT_KEYS[axis]) {
      if (typeof payload[key] === 'boolean') {
        ready = payload[key] as boolean
        break
      }
    }
  }

  const missingByAxis = asRecord(payload.missing)?.[axis]
  const issuesByAxis = asRecord(payload.issues)?.[axis]
  const flatMissing = payload[`${axis}_missing`] ?? payload[`${axis}_issues`]
  const missing = issueList(
    nestedAxis?.missing ?? nestedAxis?.issues ?? missingByAxis ?? issuesByAxis ?? flatMissing,
    axis,
  )
  return { ready: ready === true, missing }
}

function legacyIssue(code: string, label: string, target: string): AgentReadinessIssue {
  return { code, label, target, blocking: true }
}

function legacyReadiness(agent: Partial<Agent>): AgentReadiness {
  const hasScenario = Boolean(agent.scenario_id)
  const hasModel = Boolean(agent.llm_config_id)
  const definitionMissing = hasScenario
    ? []
    : [legacyIssue('scenario_required', '业务场景', 'agent-config:scenario')]
  const validationMissing = [...definitionMissing]
  if (!hasModel) validationMissing.push(legacyIssue('llm_required', '大模型', 'agent-config:llm'))

  return {
    source: 'legacy',
    definition: { ready: hasScenario, missing: definitionMissing },
    validation: { ready: hasScenario && hasModel, missing: validationMissing },
    release: {
      ready: false,
      missing: [legacyIssue('release_readiness_unknown', '等待服务端发布就绪检查', 'release-governance')],
    },
    runtime: {
      ready: false,
      missing: [legacyIssue('runtime_readiness_unknown', '等待服务端运行就绪检查', 'runtime-governance')],
    },
  }
}

/**
 * Prefer the server-owned four-axis contract. Older Agent responses fall back
 * to the minimum validation prerequisites and never infer a data requirement.
 */
export function normalizeAgentReadiness(agent: Partial<Agent>): AgentReadiness {
  const payload = readinessPayload(agent)
  if (!payload) return legacyReadiness(agent)
  return {
    source: payload.source === 'legacy' ? 'legacy' : 'server',
    definition: serverAxis(payload, 'definition'),
    validation: serverAxis(payload, 'validation'),
    release: serverAxis(payload, 'release'),
    runtime: serverAxis(payload, 'runtime'),
  }
}

export function withNormalizedAgentReadiness(agent: Agent): Agent {
  return { ...agent, readiness: normalizeAgentReadiness(agent) }
}
