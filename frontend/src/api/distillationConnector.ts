import { http } from '@/api'
import type { DistillationTargetSystem } from '@/types/businessDistillation'

const root = '/business-distillation'
const projectPath = (id: string) => `${root}/${encodeURIComponent(id)}`

export type DistillationConnectorStatus = 'pending' | 'connected' | 'revoked' | 'expired'

export interface DistillationConnectorSession {
  id: string
  target_key: string
  status: DistillationConnectorStatus
  token_prefix: string
  connector_platform: string
  created_at: string
  expires_at: string
  connected_at: string | null
  last_seen_at: string | null
  revoked_reason: string
}

export interface DistillationConnectorCommand extends DistillationConnectorSession {
  command: string
  script_url: string
}

export const distillationConnectorApi = {
  list: (projectId: string, signal: AbortSignal) =>
    http.get<{ sessions: DistillationConnectorSession[] }>(`${projectPath(projectId)}/connector-sessions`, { signal }),
  create: (projectId: string, targetKey: string, signal: AbortSignal) =>
    http.post<DistillationConnectorCommand>(`${projectPath(projectId)}/connector-sessions`, { target_key: targetKey }, { signal }),
  revoke: (projectId: string, sessionId: string, signal: AbortSignal) =>
    http.delete<DistillationConnectorSession>(`${projectPath(projectId)}/connector-sessions/${encodeURIComponent(sessionId)}`, { signal }),
  stepScreenshotUrl: (projectId: string, turnId: string, stepId: string) =>
    // Direct <img src>: unlike the http client (baseURL '/api'), this URL must
    // carry the API prefix itself.
    `/api${projectPath(projectId)}/conversation/turns/${encodeURIComponent(turnId)}/steps/${encodeURIComponent(stepId)}/screenshot`,
}

export function connectorEligibleTargets(targets: DistillationTargetSystem[]): DistillationTargetSystem[] {
  return targets.filter(target => target.enabled && target.browser)
}
