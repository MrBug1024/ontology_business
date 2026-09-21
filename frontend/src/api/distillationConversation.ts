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
  resources: (signal: AbortSignal, scenarioId?: string) => http.get<DistillationResourceOptions>('/business-distillation/resources', {
    params: { scenario_id: scenarioId || undefined },
    signal,
  }),
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
  stream: (
    projectId: string,
    turnId: string,
    onTurn: (turn: DistillationTurn) => void,
    onDone: () => void,
    onError: (error: Error) => void,
  ) => {
    const control = new AbortController()
    void (async () => {
      try {
        const response = await fetch(`/api${turnPath(projectId, turnId)}/events`, {
          headers: { Accept: 'text/event-stream' },
          credentials: 'include',
          cache: 'no-store',
          signal: control.signal,
        })
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let eventName = ''
        let receivedDone = false
        while (!control.signal.aborted) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const rawLine of lines) {
            const line = rawLine.trim()
            if (line.startsWith('event:')) {
              eventName = line.slice(6).trim()
              continue
            }
            if (!line.startsWith('data:')) continue
            const data = line.slice(5).trim()
            if (data === '[DONE]') {
              receivedDone = true
              onDone()
              return
            }
            if (eventName === 'error') {
              throw new Error('业务蒸馏对话流不可用或权限已变化')
            }
            eventName = ''
            try {
              onTurn(JSON.parse(data) as DistillationTurn)
            } catch {
              throw new Error('业务蒸馏对话流格式异常')
            }
          }
        }
        if (!control.signal.aborted && !receivedDone) onError(new Error('业务蒸馏对话流意外结束'))
      } catch (error: unknown) {
        if (!control.signal.aborted) onError(error instanceof Error ? error : new Error('业务蒸馏对话流中断'))
      }
    })()
    return control
  },
  cancel: (projectId: string, turnId: string, signal: AbortSignal) => http.post<DistillationTurn>(`${turnPath(projectId, turnId)}/cancel`, {}, { signal }),
  apply: (projectId: string, turnId: string, revision: number, signal: AbortSignal) =>
    http.post<DistillationProject>(`${turnPath(projectId, turnId)}/apply`, { expected_revision: revision }, { signal }),
}
