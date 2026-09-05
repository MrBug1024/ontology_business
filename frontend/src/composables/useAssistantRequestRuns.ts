import { onBeforeUnmount, reactive, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { AssistantMessage, AssistantRequestRun } from '@/types'

export interface AssistantRequestRunOptions {
  messages: Ref<AssistantMessage[]>
  threadId: Readonly<Ref<string>>
  reloadThread: (threadId: string) => Promise<void>
}

export interface AssistantRequestRuns {
  assistantRequestStates: Record<string, AssistantRequestRun['status']>
  assistantRequestRunId: (message: AssistantMessage) => string
  assistantRequestStatus: (message: AssistantMessage) => string
  applyAssistantRequestRun: (run: AssistantRequestRun, target?: AssistantMessage) => void
  recoverAssistantRequest: (
    runId: string,
    expectedThreadId: string,
    target?: AssistantMessage,
  ) => void
  retryAssistantRequest: (message: AssistantMessage) => Promise<void>
  cancelAssistantRequest: (message: AssistantMessage) => Promise<void>
  resetAssistantRequestRuns: () => void
}

const TERMINAL_REQUEST_STATUSES = new Set<AssistantRequestRun['status']>([
  'succeeded',
  'failed',
  'cancelled',
])

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback
}

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : Boolean(error && typeof error === 'object' && 'name' in error && error.name === 'AbortError')
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  if (signal.aborted) return Promise.resolve(false)
  return new Promise<boolean>((resolve) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', cancel)
      resolve(true)
    }, milliseconds)
    const cancel = () => {
      window.clearTimeout(timer)
      resolve(false)
    }
    signal.addEventListener('abort', cancel, { once: true })
  })
}

export function useAssistantRequestRuns(options: AssistantRequestRunOptions): AssistantRequestRuns {
  const assistantRequestPolls = new Map<string, AbortController>()
  const assistantRequestStates = reactive<Record<string, AssistantRequestRun['status']>>({})
  let disposed = false
  let requestScope = 0

  function scopeIsCurrent(scope: number) {
    return !disposed && scope === requestScope
  }

  function pollIsCurrent(runId: string, controller: AbortController, scope: number) {
    return scopeIsCurrent(scope) && assistantRequestPolls.get(runId) === controller
  }

  function messageIsCurrent(message: AssistantMessage, scope: number) {
    return scopeIsCurrent(scope) && options.messages.value.includes(message)
  }

  function assistantRequestRunId(message: AssistantMessage) {
    return String(message.context?.assistant_request_run_id || '')
  }

  function assistantRequestStatus(message: AssistantMessage) {
    return String(message.context?.assistant_request_status || '')
  }

  function applyAssistantRequestRun(run: AssistantRequestRun, target?: AssistantMessage) {
    assistantRequestStates[run.id] = run.status
    const message = target || options.messages.value.find((item) => (
      item.role === 'assistant' && assistantRequestRunId(item) === run.id
    ))
    if (!message) return
    message.id = run.assistant_message_id
    message.context = {
      ...(message.context || {}),
      assistant_request_run_id: run.id,
      assistant_request_status: run.status,
      assistant_request_revision: run.revision,
      error_code: run.error?.code || '',
    }
    if (run.status === 'waiting_upload' || run.status === 'queued') {
      message.content = '消息已接收；正在等待后台完成附件上传与解析。'
    } else if (run.status === 'running') {
      message.content = '附件准备完成；正在根据本次需求规划处理步骤。'
    } else if (run.status === 'failed') {
      message.content = run.error?.message || '这次助手请求未完成，请显式重试。'
    } else if (run.status === 'cancelled') {
      message.content = '本次助手请求已取消。'
    }
  }

  function recoverAssistantRequest(
    runId: string,
    expectedThreadId: string,
    target?: AssistantMessage,
  ) {
    if (!runId || assistantRequestPolls.has(runId) || disposed) return
    const controller = new AbortController()
    const scope = requestScope
    assistantRequestPolls.set(runId, controller)
    void (async () => {
      try {
        let consecutiveFailures = 0
        while (pollIsCurrent(runId, controller, scope)) {
          let run: AssistantRequestRun
          try {
            run = await api.getAssistantRequestRun(runId, controller.signal)
            if (!pollIsCurrent(runId, controller, scope)) break
            consecutiveFailures = 0
          } catch (error: unknown) {
            if (isAbortError(error) || !pollIsCurrent(runId, controller, scope)) break
            consecutiveFailures += 1
            if (consecutiveFailures >= 4) throw error
            if (!await abortableDelay(
              Math.min(600 * (2 ** consecutiveFailures), 4000),
              controller.signal,
            )) break
            continue
          }
          applyAssistantRequestRun(run, target)
          if (TERMINAL_REQUEST_STATUSES.has(run.status)) {
            if (run.status === 'failed') {
              ElMessage.error(run.error?.message || '助手请求未完成，请显式重试。')
            }
            if (options.threadId.value === expectedThreadId || options.threadId.value === run.thread_id) {
              await options.reloadThread(run.thread_id)
            }
            break
          }
          if (!await abortableDelay(900, controller.signal)) break
        }
      } catch (error: unknown) {
        if (pollIsCurrent(runId, controller, scope)) {
          ElMessage.warning(errorMessage(error, '后台请求状态暂时不可用；重新打开会话后会继续恢复。'))
        }
      } finally {
        if (pollIsCurrent(runId, controller, scope)) {
          assistantRequestPolls.delete(runId)
          delete assistantRequestStates[runId]
        }
      }
    })()
  }

  async function retryAssistantRequest(message: AssistantMessage) {
    const runId = assistantRequestRunId(message)
    if (!runId) return
    const scope = requestScope
    try {
      let revision = Number(message.context?.assistant_request_revision || 0)
      if (revision < 1) {
        const current = await api.getAssistantRequestRun(runId)
        if (!messageIsCurrent(message, scope)) return
        applyAssistantRequestRun(current, message)
        revision = current.revision
      }
      const run = await api.retryAssistantRequestRun(runId, revision)
      if (!messageIsCurrent(message, scope)) return
      applyAssistantRequestRun(run, message)
      recoverAssistantRequest(run.id, run.thread_id, message)
    } catch (error: unknown) {
      if (messageIsCurrent(message, scope)) {
        ElMessage.error(errorMessage(error, '无法重试本次助手请求'))
      }
    }
  }

  async function cancelAssistantRequest(message: AssistantMessage) {
    const runId = assistantRequestRunId(message)
    if (!runId) return
    const scope = requestScope
    try {
      let revision = Number(message.context?.assistant_request_revision || 0)
      if (revision < 1) {
        const current = await api.getAssistantRequestRun(runId)
        if (!messageIsCurrent(message, scope)) return
        applyAssistantRequestRun(current, message)
        revision = current.revision
      }
      const run = await api.cancelAssistantRequestRun(runId, revision)
      if (!messageIsCurrent(message, scope)) return
      applyAssistantRequestRun(run, message)
    } catch (error: unknown) {
      if (messageIsCurrent(message, scope)) {
        ElMessage.warning(errorMessage(error, '请求已经开始，无法在当前阶段取消'))
      }
    }
  }

  function resetAssistantRequestRuns() {
    requestScope += 1
    assistantRequestPolls.forEach((controller) => controller.abort())
    assistantRequestPolls.clear()
    Object.keys(assistantRequestStates).forEach((key) => delete assistantRequestStates[key])
  }

  onBeforeUnmount(() => {
    disposed = true
    resetAssistantRequestRuns()
  })

  return {
    assistantRequestStates,
    assistantRequestRunId,
    assistantRequestStatus,
    applyAssistantRequestRun,
    recoverAssistantRequest,
    retryAssistantRequest,
    cancelAssistantRequest,
    resetAssistantRequestRuns,
  }
}
