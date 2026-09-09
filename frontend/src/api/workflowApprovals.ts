import { http } from '@/api'
import type { ChannelReplyRequest, ChannelReplyResult } from '@/types/channelDelivery'

export const workflowApprovals = {
  reply: (id: string, payload: ChannelReplyRequest, signal?: AbortSignal) =>
    http.post<ChannelReplyResult>(`/tasks/approvals/${encodeURIComponent(id)}/reply`, payload, { signal }),
}
