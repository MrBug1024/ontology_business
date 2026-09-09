import { onBeforeUnmount, ref, watch } from 'vue'
import { agentExecutionTraceApi } from '@/api/agentExecutionTrace'
import type { AgentTurnEvent } from '@/types'

export function useAgentExecutionTrace(source: () => { runId?: string; revision?: number; open: boolean }) {
  const events = ref<AgentTurnEvent[]>([])
  const loading = ref(false)
  const error = ref('')
  const hasMore = ref(false)
  let generation = 0
  let cursor = 0
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined

  function stop() {
    generation++
    controller?.abort()
    controller = undefined
    if (timer) clearTimeout(timer)
    timer = undefined
    loading.value = false
  }

  async function load() {
    const runId = source().runId
    if (!runId || loading.value) return
    const current = generation
    const request = new AbortController()
    controller = request
    loading.value = true
    error.value = ''
    try {
      // Catch up in bounded pages, then yield control for explicit continuation.
      for (let page = 0; page < 5; page++) {
        const result = await agentExecutionTraceApi.list(runId, cursor, request.signal)
        if (current !== generation || request.signal.aborted) return
        const fresh = result.filter(event => event.revision > cursor)
        events.value.push(...fresh)
        if (fresh.length) cursor = fresh[fresh.length - 1].revision
        hasMore.value = result.length === 200
        if (!hasMore.value || !fresh.length) break
      }
    } catch {
      if (current === generation && !request.signal.aborted) error.value = '过程记录读取失败，请重试'
    } finally {
      if (current === generation) {
        loading.value = false
        if (source().open && !error.value && !hasMore.value && (source().revision || 0) > cursor) schedule()
      }
    }
  }

  function schedule() {
    if (timer) return
    timer = setTimeout(() => {
      timer = undefined
      void load()
    }, 1000)
  }

  watch(() => source().runId, () => {
    stop()
    events.value = []
    cursor = 0
    error.value = ''
    hasMore.value = false
    if (source().open) void load()
  }, { immediate: true })
  watch(() => source().open, (open) => {
    if (open) void load()
    else stop()
  })
  watch(() => source().revision, () => {
    if (!source().open || timer || error.value) return
    schedule()
  })
  onBeforeUnmount(stop)
  return { events, loading, error, hasMore, load }
}
