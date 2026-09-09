import { computed, onBeforeUnmount, reactive, ref, type ComputedRef, type Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, streamAgentTurn } from '@/api'
import { applyAgentTurnEvent } from '@/utils/agentTurnProgress'
import type {
  AgentChatRequest,
  AgentTurnEvent,
  AgentTurnRun,
  AgentTurnStatus,
  ChatMessage,
  Conversation,
  RagCitation,
} from '@/types'

export type AgentTurnViewMessage = ChatMessage & {
  streaming?: boolean
  status?: string
  turnId?: string
  turnRevision?: number
  turnStatus?: AgentTurnStatus
  retrying?: boolean
  cancelling?: boolean
  retried?: boolean
  retryIdempotencyKey?: string
}

export interface AgentDurableTurnOptions {
  agentId: () => string
  currentConversation: Ref<Conversation | null>
  messages: Ref<AgentTurnViewMessage[]>
  conversationLoading: Ref<boolean>
  validationReady: Readonly<Ref<boolean>>
  validationMissingText: Readonly<Ref<string>>
  invalidateConversationLoad: () => void
  invalidateConversationList: () => void
  refreshConversations: () => Promise<Conversation[]>
  openConversation: (conversation: Conversation, clearMessages?: boolean) => Promise<void>
  clearComposerAfterAccepted: () => void
  normalizeCitations: (value: unknown) => RagCitation[]
  scrollBottom: () => void
  followProgress: () => void
}

export interface AgentDurableTurns {
  conversationNavigationLocked: ComputedRef<boolean>
  currentTurnPending: ComputedRef<boolean>
  currentTurnActive: ComputedRef<boolean>
  streaming: ComputedRef<boolean>
  activeTurnCount: (conversationId: string) => number
  recoverActiveTurns: (runs: AgentTurnRun[]) => void
  recoverConversationTurns: (runs: AgentTurnRun[]) => void
  resetTurnScope: () => void
  send: (payload: AgentChatRequest) => Promise<void>
  canRetryTurn: (message: AgentTurnViewMessage) => boolean
  retryTurn: (message: AgentTurnViewMessage) => Promise<void>
  canCancelTurn: (message: AgentTurnViewMessage) => boolean
  stopTurn: (message: AgentTurnViewMessage) => Promise<void>
  stop: () => Promise<void>
}

const TURN_STATUS_LABELS: Record<AgentTurnStatus, string> = {
  accepted: '需求已接收',
  preparing_inputs: '正在准备输入资料',
  validating_contracts: '正在校验输入契约',
  planning: '正在规划处理步骤',
  invoking_tools: '正在调用受治理能力',
  responding: '正在整理结果',
  cancel_requested: '正在取消',
  succeeded: '对话已完成',
  failed: '处理失败',
  cancelled: '已取消',
  indeterminate: '执行结果待核对',
}

const TERMINAL_TURN_STATUSES = new Set<AgentTurnStatus>([
  'succeeded',
  'failed',
  'cancelled',
  'indeterminate',
])

function isTerminalTurn(status: AgentTurnStatus) {
  return TERMINAL_TURN_STATUSES.has(status)
}

function streamErrorContent(content: string, error: unknown) {
  const separator = content ? '\n\n' : ''
  return `${content}${separator}[错误] ${String(error)}`
}

function requestErrorMessage(error: unknown, fallback: string) {
  const candidate = error as {
    detail?: unknown
    message?: unknown
    response?: { data?: { detail?: unknown } }
  }
  const detail = candidate?.detail ?? candidate?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (
    detail
    && typeof detail === 'object'
    && 'message' in detail
    && typeof detail.message === 'string'
    && detail.message.trim()
  ) return detail.message
  return typeof candidate?.message === 'string' && candidate.message.trim()
    ? candidate.message
    : fallback
}

function invocationMessage(payload: AgentChatRequest) {
  if (payload.message.trim()) return payload.message.trim()
  if (payload.attachments?.length) return `已上传 ${payload.attachments.length} 个文件，请根据文件内容完成业务需求。`
  return ''
}

export function useAgentDurableTurns(options: AgentDurableTurnOptions): AgentDurableTurns {
  const pendingTurnRequests = ref(new Map<number, string>())
  const pendingComposerRequests = ref(new Set<number>())
  const activeTurns = ref(new Map<string, AgentTurnRun>())
  const turnControllers = new Map<string, AbortController>()
  const turnSubscriptionVersions = new Map<string, number>()
  let disposed = false
  let turnScopeVersion = 0
  let nextTurnSubscriptionVersion = 0
  let nextPendingTurnRequest = 0

  const conversationNavigationLocked = computed(() => pendingComposerRequests.value.size > 0)
  const currentTurnPending = computed(() => {
    const conversationId = options.currentConversation.value?.id || ''
    return [...pendingTurnRequests.value.values()].some((value) => value === conversationId)
  })
  const currentTurnActive = computed(() => {
    const conversationId = options.currentConversation.value?.id || ''
    if (!conversationId) return false
    return [...activeTurns.value.values()].some((run) => run.conversation_id === conversationId)
  })
  const streaming = computed(() => currentTurnPending.value || currentTurnActive.value)

  function activeTurnCount(conversationId: string) {
    let count = 0
    for (const run of activeTurns.value.values()) {
      if (run.conversation_id === conversationId) count += 1
    }
    return count
  }

  function isTurnScopeActive(scope: number, agentId?: string) {
    return Boolean(
      !disposed
      && scope === turnScopeVersion
      && (!agentId || options.agentId() === agentId),
    )
  }

  function findTurnMessage(runId: string, assistantMessageId?: string | null) {
    return options.messages.value.find((message) => (
      message.turnId === runId
      || Boolean(assistantMessageId && message.id === assistantMessageId)
    ))
  }

  function applyTurnResult(result: Record<string, unknown>, assistant: AgentTurnViewMessage) {
    if (typeof result.answer === 'string') assistant.content = result.answer
    if (Array.isArray(result.citations)) assistant.citations = options.normalizeCitations(result.citations)
  }

  function applyTurnRun(run: AgentTurnRun, assistant: AgentTurnViewMessage) {
    if (
      assistant.turnId === run.id
      && assistant.turnRevision !== undefined
      && run.revision < assistant.turnRevision
    ) return
    assistant.id = run.assistant_message_id || assistant.id
    assistant.turnId = run.id
    assistant.turnRevision = run.revision
    assistant.turnStatus = run.status
    assistant.streaming = !isTerminalTurn(run.status)
    assistant.status = TURN_STATUS_LABELS[run.status]
    applyTurnResult(run.result || {}, assistant)
    const inputSnapshot = run.result?.input_snapshot
    if (inputSnapshot && typeof inputSnapshot === 'object' && !Array.isArray(inputSnapshot)) {
      const userMessage = options.messages.value.find(message => message.id === run.user_message_id)
      if (userMessage) userMessage.input_snapshot = inputSnapshot as Record<string, unknown>
    }
    if (run.status === 'failed' && !assistant.content) {
      assistant.content = streamErrorContent('', run.error?.message || '后台处理失败')
    } else if (run.status === 'cancelled') {
      assistant.content = '该任务已取消。'
    } else if (run.status === 'indeterminate' && !assistant.content) {
      assistant.content = '外部执行结果暂时无法确认，请核对执行记录后再决定是否重试。'
    }
  }

  function isTurnSubscriptionCurrent(runId: string, subscription: number, scope: number) {
    return Boolean(
      isTurnScopeActive(scope)
      && turnSubscriptionVersions.get(runId) === subscription,
    )
  }

  function releaseTurnController(runId: string, subscription: number) {
    if (turnSubscriptionVersions.get(runId) !== subscription) return
    turnControllers.get(runId)?.abort()
    turnControllers.delete(runId)
  }

  function handleTurnEvent(
    runId: string,
    event: AgentTurnEvent,
    subscription: number,
    scope: number,
  ) {
    if (!isTurnSubscriptionCurrent(runId, subscription, scope)) return
    const currentTurn = activeTurns.value.get(runId)
    if (!currentTurn || event.revision <= currentTurn.revision) return
    const nextRun = applyAgentTurnEvent(currentTurn, event)
    const nextStatus = nextRun.status
    if (isTerminalTurn(nextStatus)) activeTurns.value.delete(runId)
    else activeTurns.value.set(runId, nextRun)
    const assistant = findTurnMessage(runId, nextRun.assistant_message_id)
    if (assistant) {
      applyTurnRun(nextRun, assistant)
      if (typeof event.data.label === 'string' && event.data.label.trim()) {
        assistant.status = event.data.label
      }
      options.followProgress()
    }
    if (isTerminalTurn(nextStatus)) void finishTurn(runId, subscription, scope)
  }

  function handleTurnStreamError(
    runId: string,
    error: Error,
    subscription: number,
    scope: number,
  ) {
    if (!isTurnSubscriptionCurrent(runId, subscription, scope)) return
    releaseTurnController(runId, subscription)
    turnSubscriptionVersions.delete(runId)
    const run = activeTurns.value.get(runId)
    activeTurns.value.delete(runId)
    const assistant = findTurnMessage(runId, run?.assistant_message_id)
    if (assistant) {
      assistant.streaming = false
      assistant.status = '进度连接已中断，重新打开该对话可恢复'
    }
    ElMessage.error(`进度连接失败：${error.message}`)
  }

  async function refreshTurnHistory(run: AgentTurnRun, scope: number) {
    await options.refreshConversations()
    if (!isTurnScopeActive(scope)) return
    const conversation = options.currentConversation.value
    if (conversation && conversation.id === run.conversation_id) {
      await options.openConversation(conversation, false)
    }
  }

  async function finishTurn(runId: string, subscription: number, scope: number) {
    if (!isTurnSubscriptionCurrent(runId, subscription, scope)) return
    releaseTurnController(runId, subscription)
    let run: AgentTurnRun
    try {
      run = await api.getAgentTurn(runId)
    } catch (error: unknown) {
      if (!isTurnSubscriptionCurrent(runId, subscription, scope)) return
      const activeRun = activeTurns.value.get(runId)
      activeTurns.value.delete(runId)
      turnSubscriptionVersions.delete(runId)
      const assistant = findTurnMessage(runId, activeRun?.assistant_message_id)
      if (assistant?.streaming) {
        assistant.streaming = false
        assistant.status = '状态刷新失败，重新打开该对话可恢复'
      }
      ElMessage.error(requestErrorMessage(error, 'Agent Turn 状态刷新失败'))
      return
    }
    if (!isTurnSubscriptionCurrent(runId, subscription, scope)) return
    const assistant = findTurnMessage(runId, run.assistant_message_id)
    if (assistant) applyTurnRun(run, assistant)
    if (!isTerminalTurn(run.status)) {
      bindTurn(run, assistant)
      return
    }
    activeTurns.value.delete(runId)
    turnSubscriptionVersions.delete(runId)
    if (run.status === 'failed') ElMessage.error(run.error?.message || '后台处理失败')
    if (run.status === 'indeterminate') ElMessage.warning('执行结果待核对，请勿直接重复提交')

    try {
      await refreshTurnHistory(run, scope)
    } catch {
      if (isTurnScopeActive(scope)) {
        ElMessage.warning('任务状态已更新，但会话记录刷新失败，请重新打开该对话')
      }
    }
  }

  function bindTurn(run: AgentTurnRun, assistant?: AgentTurnViewMessage) {
    const trackedRun = activeTurns.value.get(run.id)
    const currentRun = trackedRun && trackedRun.revision > run.revision ? trackedRun : run
    const currentAssistant = assistant || findTurnMessage(run.id, currentRun.assistant_message_id)
    if (currentAssistant) applyTurnRun(currentRun, currentAssistant)
    if (isTerminalTurn(currentRun.status)) {
      activeTurns.value.delete(run.id)
      const existingSubscription = turnSubscriptionVersions.get(run.id)
      if (existingSubscription !== undefined) releaseTurnController(run.id, existingSubscription)
      turnSubscriptionVersions.delete(run.id)
      return
    }
    activeTurns.value.set(run.id, currentRun)
    if (turnControllers.has(run.id)) return
    const subscription = ++nextTurnSubscriptionVersion
    const scope = turnScopeVersion
    turnSubscriptionVersions.set(run.id, subscription)
    turnControllers.set(run.id, streamAgentTurn(
      run.id,
      (event) => {
        handleTurnEvent(run.id, event, subscription, scope)
      },
      () => { void finishTurn(run.id, subscription, scope) },
      (error) => {
        handleTurnStreamError(run.id, error, subscription, scope)
      },
      (state) => {
        if (!isTurnSubscriptionCurrent(run.id, subscription, scope)) return
        const latestRun = activeTurns.value.get(run.id)
        const latestAssistant = findTurnMessage(run.id, latestRun?.assistant_message_id)
        if (!latestRun || !latestAssistant) return
        latestAssistant.status = state === 'reconnecting'
          ? `${TURN_STATUS_LABELS[latestRun.status]}，正在恢复连接`
          : TURN_STATUS_LABELS[latestRun.status]
      },
      currentRun.revision,
    ))
  }

  function recoverActiveTurns(runs: AgentTurnRun[]) {
    for (const run of runs) bindTurn(run)
  }

  function recoverConversationTurns(runs: AgentTurnRun[]) {
    const runsById = new Map<string, AgentTurnRun>()
    for (const run of runs) {
      const previous = runsById.get(run.id)
      if (!previous || run.revision >= previous.revision) runsById.set(run.id, run)
    }
    const latestRuns = [...runsById.values()]
    for (const run of latestRuns) {
      const assistant = options.messages.value.find((message) => message.id === run.assistant_message_id)
      if (assistant) applyTurnRun(run, assistant)
    }
    for (const run of latestRuns) {
      if (!run.parent_run_id) continue
      const parentMessage = options.messages.value.find((message) => message.turnId === run.parent_run_id)
      if (parentMessage) parentMessage.retried = true
    }
    for (const run of latestRuns) bindTurn(run)
  }

  function resetTurnScope() {
    turnScopeVersion += 1
    options.invalidateConversationList()
    for (const controller of turnControllers.values()) controller.abort()
    turnControllers.clear()
    turnSubscriptionVersions.clear()
    activeTurns.value.clear()
    pendingTurnRequests.value.clear()
    pendingComposerRequests.value.clear()
    options.conversationLoading.value = false
    for (const message of options.messages.value) message.streaming = false
  }

  async function send(payload: AgentChatRequest) {
    if (!options.validationReady.value) {
      ElMessage.warning(options.validationMissingText.value)
      return
    }
    if (options.conversationLoading.value || streaming.value) return
    const currentAgentId = options.agentId()
    if (!currentAgentId) return
    const scope = turnScopeVersion
    const targetConversationId = options.currentConversation.value?.id || undefined
    const pendingRequest = ++nextPendingTurnRequest
    pendingTurnRequests.value.set(pendingRequest, targetConversationId || '')
    pendingComposerRequests.value.add(pendingRequest)
    options.invalidateConversationLoad()
    const messageText = invocationMessage(payload)
    const requestKey = payload.idempotency_key || `turn-${Date.now()}`
    const userMessage = reactive<AgentTurnViewMessage>({
      id: `pending-user-${requestKey}`,
      role: 'user',
      content: messageText,
    })
    const assistant = reactive<AgentTurnViewMessage>({
      id: `pending-assistant-${requestKey}`,
      role: 'assistant',
      content: '',
      tool_calls: [],
      streaming: true,
      status: '正在发送需求',
    })
    options.messages.value.push(userMessage, assistant)
    options.scrollBottom()
    try {
      const run = await api.createAgentTurn(currentAgentId, {
        ...payload,
        conversation_id: targetConversationId,
      })
      if (!isTurnScopeActive(scope, currentAgentId)) return
      const placeholdersVisible = options.messages.value.includes(userMessage)
        && options.messages.value.includes(assistant)
      if (placeholdersVisible) {
        options.clearComposerAfterAccepted()
        userMessage.id = run.user_message_id || userMessage.id
        assistant.id = run.assistant_message_id || assistant.id
      }
      if (placeholdersVisible && run.conversation_id && !targetConversationId) {
        options.currentConversation.value = {
          id: run.conversation_id,
          agent_id: currentAgentId,
          title: messageText.slice(0, 50) || '新对话',
        }
      }
      bindTurn(run, placeholdersVisible ? assistant : undefined)
      void options.refreshConversations()
    } catch (error: unknown) {
      if (isTurnScopeActive(scope, currentAgentId)) {
        options.messages.value = options.messages.value.filter((message) => (
          message !== userMessage && message !== assistant
        ))
        ElMessage.error(requestErrorMessage(error, '需求发送失败，草稿已保留'))
      }
    } finally {
      pendingTurnRequests.value.delete(pendingRequest)
      pendingComposerRequests.value.delete(pendingRequest)
    }
  }

  function canRetryTurn(message: AgentTurnViewMessage) {
    return Boolean(
      message.turnId
      && message.turnRevision !== undefined
      && message.turnStatus
      && !message.retried
      && ['failed', 'cancelled', 'indeterminate'].includes(message.turnStatus),
    )
  }

  function retryIdempotencyKey(message: AgentTurnViewMessage) {
    if (message.retryIdempotencyKey) return message.retryIdempotencyKey
    const random = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    message.retryIdempotencyKey = `retry-${random}`
    return message.retryIdempotencyKey
  }

  async function retryTurn(message: AgentTurnViewMessage) {
    if (
      message.retrying
      || !canRetryTurn(message)
      || !message.turnId
      || message.turnRevision === undefined
    ) return
    const currentAgentId = options.agentId()
    if (!currentAgentId) return
    const scope = turnScopeVersion
    const turnId = message.turnId
    const turnRevision = message.turnRevision
    if (message.turnStatus === 'indeterminate') {
      try {
        await ElMessageBox.confirm(
          '该执行的外部结果尚未确认。请先核对执行记录；仅在确认可以再次执行后继续重试。',
          '确认重新执行',
          { type: 'warning', confirmButtonText: '已核对，继续重试' },
        )
      } catch {
        return
      }
      if (!isTurnScopeActive(scope, currentAgentId)) return
    }
    const previousStatus = message.status
    message.status = '正在重新排队'
    message.retrying = true
    const pendingRequest = ++nextPendingTurnRequest
    pendingTurnRequests.value.set(pendingRequest, options.currentConversation.value?.id || '')
    let run: AgentTurnRun
    try {
      run = await api.retryAgentTurn(turnId, turnRevision, retryIdempotencyKey(message))
    } catch (error: unknown) {
      if (isTurnScopeActive(scope, currentAgentId)) {
        const currentMessage = findTurnMessage(turnId) || message
        currentMessage.status = previousStatus
        currentMessage.retrying = false
        ElMessage.error(requestErrorMessage(error, '重试提交失败'))
      }
      return
    } finally {
      pendingTurnRequests.value.delete(pendingRequest)
    }
    if (!isTurnScopeActive(scope, currentAgentId)) return
    message.retrying = false
    message.retried = true
    bindTurn(run)
    try {
      await refreshTurnHistory(run, scope)
      if (!isTurnScopeActive(scope, currentAgentId)) return
      const conversation = options.currentConversation.value
      if (!conversation || conversation.id !== run.conversation_id) {
        ElMessage.info('任务已重新排队，可从对应会话查看进度')
      }
    } catch {
      if (isTurnScopeActive(scope, currentAgentId)) {
        ElMessage.warning('任务已重新排队，但会话列表刷新失败')
      }
    }
  }

  function canCancelTurn(message: AgentTurnViewMessage) {
    return Boolean(
      message.turnId
      && message.turnStatus
      && activeTurns.value.has(message.turnId)
      && !isTerminalTurn(message.turnStatus),
    )
  }

  async function stopTurn(message: AgentTurnViewMessage) {
    if (!message.turnId || message.cancelling) return
    const run = activeTurns.value.get(message.turnId)
    const currentAgentId = options.agentId()
    if (!run || !currentAgentId || isTerminalTurn(run.status)) return
    const scope = turnScopeVersion
    message.cancelling = true
    message.status = '正在请求取消'
    try {
      const updated = await api.cancelAgentTurn(run.id, run.revision)
      if (!isTurnScopeActive(scope, currentAgentId)) return
      bindTurn(updated, findTurnMessage(run.id, updated.assistant_message_id))
    } catch (error: unknown) {
      if (!isTurnScopeActive(scope, currentAgentId)) return
      let latestError = error
      try {
        const current = await api.getAgentTurn(run.id)
        if (!isTurnScopeActive(scope, currentAgentId)) return
        bindTurn(current, findTurnMessage(run.id, current.assistant_message_id))
        const status = Number((error as { status?: number })?.status)
        if (!isTerminalTurn(current.status) && status === 409) {
          try {
            const updated = await api.cancelAgentTurn(current.id, current.revision)
            if (!isTurnScopeActive(scope, currentAgentId)) return
            bindTurn(updated, findTurnMessage(current.id, updated.assistant_message_id))
            return
          } catch (retryError: unknown) {
            latestError = retryError
          }
        } else if (current.status === 'cancel_requested' || isTerminalTurn(current.status)) {
          return
        }
      } catch {
        if (!isTurnScopeActive(scope, currentAgentId)) return
        const assistant = findTurnMessage(run.id, run.assistant_message_id)
        if (assistant) assistant.status = TURN_STATUS_LABELS[run.status]
      }
      ElMessage.warning(requestErrorMessage(latestError, '取消状态已变化，请稍后重试'))
    } finally {
      if (isTurnScopeActive(scope, currentAgentId)) {
        const assistant = findTurnMessage(run.id, run.assistant_message_id)
        if (assistant) assistant.cancelling = false
      }
    }
  }

  async function stop() {
    const assistant = [...options.messages.value].reverse().find(canCancelTurn)
    if (assistant) {
      await stopTurn(assistant)
      return
    }
    if (activeTurns.value.size) ElMessage.info('请打开正在处理的会话后逐一取消任务')
  }

  onBeforeUnmount(() => {
    disposed = true
    resetTurnScope()
  })

  return {
    conversationNavigationLocked,
    currentTurnPending,
    currentTurnActive,
    streaming,
    activeTurnCount,
    recoverActiveTurns,
    recoverConversationTurns,
    resetTurnScope,
    send,
    canRetryTurn,
    retryTurn,
    canCancelTurn,
    stopTurn,
    stop,
  }
}
