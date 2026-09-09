import { http } from '@/api'
import type { AgentCapabilityReceipt } from '@/types/agentCapabilityReceipt'

export const agentCapabilityReceiptApi = {
  artifactDownloadUrl: (fileId: string) => `/api/data-sources/files/${fileId}/download`,
  get: (agentId: string, invocationId: string, messageId: string, signal?: AbortSignal) =>
    http.get<AgentCapabilityReceipt>(`/agents/${agentId}/capability-invocations/${invocationId}`, {
      params: { message_id: messageId }, signal,
    }),
  confirm: (agentId: string, invocationId: string, messageId: string) =>
    http.post<AgentCapabilityReceipt>(`/agents/${agentId}/capability-invocations/${invocationId}/confirm`, {
      message_id: messageId, confirmed: true,
    }),
}
