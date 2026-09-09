import { http } from '@/api'
import type { AgentTurnEvent } from '@/types'

export const agentExecutionTraceApi = {
  list: (runId: string, afterRevision: number, signal: AbortSignal) =>
    http.get<AgentTurnEvent[]>(`/agent-turns/${runId}/event-log`, {
      params: { after_revision: afterRevision }, signal,
    }),
}
