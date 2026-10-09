import type { Agent, AgentRuntimeCapability } from '@/types'

export function forwardedValidationRelease(value: unknown, targetScenario: string, sourceScenario: string): string | undefined {
  return sourceScenario && sourceScenario === targetScenario && typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
    ? value : undefined
}

interface TargetApi {
  getAgent(id: string, releaseId?: string, signal?: AbortSignal): Promise<Agent>
  getAgentRuntimeCapabilities(id: string, releaseId?: string, signal?: AbortSignal): Promise<AgentRuntimeCapability[]>
}

export type AgentValidationTarget =
  | { agent: Agent; capabilities: AgentRuntimeCapability[]; error: '' }
  | { agent: null; capabilities: []; error: string }

export function createAgentValidationLoader(api: TargetApi) {
  let controller: AbortController | undefined
  let generation = 0
  let disposed = false

  async function load(id: string, releaseId: string): Promise<AgentValidationTarget | null> {
    if (disposed) return null
    controller?.abort()
    controller = new AbortController()
    const signal = controller.signal
    const current = ++generation
    const [agent, capabilities] = await Promise.allSettled([
      api.getAgent(id, releaseId || undefined, signal),
      api.getAgentRuntimeCapabilities(id, releaseId || undefined, signal),
    ])
    if (disposed || current !== generation || signal.aborted) return null
    const failure = agent.status === 'rejected' ? agent.reason
      : capabilities.status === 'rejected' ? capabilities.reason : null
    if (agent.status !== 'fulfilled' || capabilities.status !== 'fulfilled') {
      const candidate = failure as { message?: unknown } | null
      return { agent: null, capabilities: [], error: typeof candidate?.message === 'string'
        ? candidate.message : '验证目标加载失败，请重新选择或重试' }
    }
    return { agent: agent.value, capabilities: capabilities.value, error: '' }
  }

  function dispose() {
    disposed = true
    generation += 1
    controller?.abort()
  }
  return { load, dispose }
}
