import { http } from '@/api'
import type { LLMConfig, MCPConfig, Skill } from '@/types'
import type { ModelingAdvisorResourceOptions } from '@/types/assistantResources'

export const assistantResourcesApi = {
  async list(signal: AbortSignal): Promise<ModelingAdvisorResourceOptions> {
    const [models, skills, mcps] = await Promise.all([
      http.get<LLMConfig[]>('/llm-configs', { signal }),
      http.get<Skill[]>('/skills', { signal }),
      http.get<MCPConfig[]>('/mcp', { signal }),
    ])
    // The selector only retains display metadata; connection settings remain in platform settings.
    return {
      models: models.flatMap(item => item.id && item.enabled !== false && (!item.capabilities?.length || item.capabilities.includes('chat'))
        ? [{ id: item.id, name: item.name, description: item.model }] : []),
      skills: skills.filter(item => item.id && item.enabled && item.source === 'builtin')
        .map(item => ({ id: item.id, name: item.name, description: item.description })),
      mcps: mcps.flatMap(item => item.id && item.enabled !== false && ['sse', 'streamable_http', 'http'].includes(item.transport)
        ? [{ id: item.id, name: item.name, description: '查看工具目录与输入契约' }] : []),
    }
  },
}
