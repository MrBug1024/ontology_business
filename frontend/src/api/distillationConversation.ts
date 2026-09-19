import { http } from '@/api'
import type { DistillationProject } from '@/types/businessDistillation'
import type {
  DistillationAttachment,
  DistillationConversationPage,
  DistillationResourceOptions,
  DistillationResourceSelection,
  DistillationTurn,
} from '@/types/distillationConversation'

const path = (projectId: string) => `/business-distillation/${encodeURIComponent(projectId)}/conversation`
const turnPath = (projectId: string, turnId: string) => `${path(projectId)}/turns/${encodeURIComponent(turnId)}`

function selectionPayload(selection: DistillationResourceSelection = {}): Required<DistillationResourceSelection> {
  return {
    llm_config_id: selection.llm_config_id || null,
    skill_ids: [...new Set(selection.skill_ids || [])].sort(),
    mcp_ids: [...new Set(selection.mcp_ids || [])].sort(),
    investigation_tool_keys: selection.investigation_tool_keys == null ? null : [...new Set(selection.investigation_tool_keys)].sort(),
  }
}

export const distillationConversationApi = {
  list: (projectId: string, beforeTurn: number | undefined, signal: AbortSignal) =>
    http.get<DistillationConversationPage>(path(projectId), { params: { limit: 20, before_turn_number: beforeTurn }, signal }),
  resources: (signal: AbortSignal) => http.get<DistillationResourceOptions>('/business-distillation/resources', { signal }),
  send: (
    projectId: string,
    requestId: string,
    message: string,
    revision: number,
    signal: AbortSignal,
    attachmentIds: string[] = [],
    resourceSelection: DistillationResourceSelection = {},
  ) => http.post<DistillationTurn>(
    `${path(projectId)}/turns`,
    {
      request_id: requestId,
      message,
      expected_revision: revision,
      attachment_ids: attachmentIds,
      resource_selection: selectionPayload(resourceSelection),
    },
    { signal },
  ),
  attachments: (projectId: string, signal: AbortSignal) => http.get<DistillationAttachment[]>(`${path(projectId)}/attachments`, { signal }),
  upload: (projectId: string, requestId: string, file: File, signal: AbortSignal, progress: (value: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    form.append('request_id', requestId)
    return http.post<DistillationAttachment>(`${path(projectId)}/attachments`, form, { signal, timeout: 180_000, headers: { 'Content-Type': 'multipart/form-data' }, onUploadProgress: (event: { loaded: number; total?: number }) => progress(Math.min(99, Math.round(event.loaded / (event.total || file.size || 1) * 100))) })
  },
  removeAttachment: (projectId: string, attachmentId: string, signal: AbortSignal) => http.delete<void>(`${path(projectId)}/attachments/${encodeURIComponent(attachmentId)}`, { signal }),
  get: (projectId: string, turnId: string, signal: AbortSignal) => http.get<DistillationTurn>(turnPath(projectId, turnId), { signal }),
  cancel: (projectId: string, turnId: string, signal: AbortSignal) => http.post<DistillationTurn>(`${turnPath(projectId, turnId)}/cancel`, {}, { signal }),
  apply: (projectId: string, turnId: string, revision: number, signal: AbortSignal) =>
    http.post<DistillationProject>(`${turnPath(projectId, turnId)}/apply`, { expected_revision: revision }, { signal }),
}
