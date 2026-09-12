import { http } from '@/api'
import type { AgentCapabilityReceipt } from '@/types/agentCapabilityReceipt'
import { managedFileDownloadUrl } from '@/utils/managedFileUrls'

export const agentCapabilityReceiptApi = {
  artifactDownloadUrl: (fileId: string, agentId?: string) => managedFileDownloadUrl(fileId, agentId),
  get: (agentId: string, invocationId: string, messageId: string, signal?: AbortSignal) =>
    http.get<AgentCapabilityReceipt>(`/agents/${agentId}/capability-invocations/${invocationId}`, {
      params: { message_id: messageId }, signal,
    }),
  confirm: (agentId: string, invocationId: string, messageId: string) =>
    http.post<AgentCapabilityReceipt>(`/agents/${agentId}/capability-invocations/${invocationId}/confirm`, {
      message_id: messageId, confirmed: true,
    }),
}
