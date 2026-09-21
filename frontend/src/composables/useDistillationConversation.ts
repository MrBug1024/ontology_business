import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { distillationConversationApi as api } from '@/api/distillationConversation'
import { isWorking, mergeTurns } from '@/utils/distillationConversation'
import type { DistillationResourceSelection, DistillationTurn } from '@/types/distillationConversation'

function stableResourceSelection(selection: DistillationResourceSelection = {}) {
  return {
    llm_config_id: selection.llm_config_id || null,
    skill_ids: [...new Set(selection.skill_ids || [])].sort(),
    mcp_ids: [...new Set(selection.mcp_ids || [])].sort(),
    investigation_tool_keys: selection.investigation_tool_keys == null ? null : [...new Set(selection.investigation_tool_keys)].sort(),
  }
}

export function useDistillationConversation(projectId: Ref<string>, draftKey: Ref<string> = projectId) {
  const turns = ref<DistillationTurn[]>([]), input = ref(''), error = ref('')
  const loading = ref(false), sending = ref(false), applying = ref(''), cancelling = ref(false), hasMore = ref(false)
  const active = computed(() => turns.value.find(isWorking))
  const lastTurn = computed(() => turns.value[turns.value.length - 1])
  const drafts = new Map<string, string>()
  let generation = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let pending: { project: string; message: string; revision: number; attachments: string; resources: string; id: string } | undefined
  const controllers = new Set<AbortController>()

  function controller() { const value = new AbortController(); controllers.add(value); return value }
  function message(caught: unknown) { return caught instanceof Error ? caught.message : '对话操作未完成，请重试。你的输入已保留。' }
  function current(epoch: number, control: AbortController) { return !disposed && epoch === generation && !control.signal.aborted }
  function receive(turn: DistillationTurn) { turns.value = mergeTurns(turns.value, [turn]) }
  function schedulePoll() {
    clearTimeout(timer)
    if (active.value && !disposed) timer = setTimeout(() => { void poll() }, 1200)
  }
  async function poll() {
    const turn = active.value
    if (!turn) return
    const epoch = generation, control = controller()
    try {
      const result = await api.get(turn.project_id, turn.id, control.signal)
      if (current(epoch, control)) { receive(result); error.value = ''; schedulePoll() }
    } catch (caught: unknown) {
      if (current(epoch, control)) error.value = `${message(caught)} 已保存的对话可重新连接继续查看。`
    } finally { controllers.delete(control) }
  }
  async function load(older = false) {
    if (!projectId.value || loading.value) return
    const epoch = generation, id = projectId.value, control = controller()
    loading.value = true
    try {
      const page = await api.list(id, older ? turns.value[0]?.turn_number : undefined, control.signal)
      if (!current(epoch, control)) return
      turns.value = mergeTurns(turns.value, page.turns)
      hasMore.value = page.has_more
      error.value = ''
      schedulePoll()
    } catch (caught: unknown) { if (current(epoch, control)) error.value = message(caught) }
    finally { controllers.delete(control); if (current(epoch, control)) loading.value = false }
  }
  async function send(text: string, revision: number, attachmentIds: string[] = [], resourceSelection: DistillationResourceSelection = {}) {
    if (!projectId.value || sending.value || active.value || !text.trim()) return false
    const epoch = generation, id = projectId.value, control = controller()
    const attachments = JSON.stringify(attachmentIds)
    const selectedResources = stableResourceSelection(resourceSelection)
    const resources = JSON.stringify(selectedResources)
    const request = pending?.project === id && pending.message === text && pending.revision === revision && pending.attachments === attachments && pending.resources === resources
      ? pending : { project: id, message: text, revision, attachments, resources, id: crypto.randomUUID() }
    pending = request
    sending.value = true
    error.value = ''
    try {
      const result = await api.send(id, request.id, text, revision, control.signal, attachmentIds, selectedResources)
      if (!current(epoch, control)) return false
      receive(result)
      pending = undefined
      if (input.value === text) input.value = ''
      schedulePoll()
      return true
    } catch (caught: unknown) { if (current(epoch, control)) error.value = message(caught); return false }
    finally { controllers.delete(control); if (current(epoch, control)) sending.value = false }
  }
  async function cancel() {
    const turn = active.value
    if (!turn || cancelling.value) return
    const epoch = generation, control = controller()
    cancelling.value = true
    try {
      const result = await api.cancel(turn.project_id, turn.id, control.signal)
      if (current(epoch, control)) { receive(result); error.value = ''; schedulePoll() }
    } catch (caught: unknown) { if (current(epoch, control)) error.value = message(caught) }
    finally { controllers.delete(control); if (current(epoch, control)) cancelling.value = false }
  }
  async function apply(turn: DistillationTurn, revision: number) {
    if (applying.value || active.value) return null
    const epoch = generation, control = controller()
    applying.value = turn.id
    try {
      const result = await api.apply(turn.project_id, turn.id, revision, control.signal)
      if (!current(epoch, control)) return null
      receive({ ...turn, applied_revision: result.revision })
      error.value = ''
      return result
    } catch (caught: unknown) { if (current(epoch, control)) error.value = message(caught); return null }
    finally { controllers.delete(control); if (current(epoch, control)) applying.value = '' }
  }
  watch([draftKey, projectId], ([key], previous) => {
    const previousKey = previous?.[0]
    if (previousKey !== undefined) drafts.set(previousKey, input.value)
    generation += 1
    clearTimeout(timer)
    for (const control of controllers) control.abort()
    controllers.clear()
    turns.value = []; input.value = drafts.get(key) || ''; error.value = ''; loading.value = false
    sending.value = false; applying.value = ''; cancelling.value = false; hasMore.value = false
    void load()
  }, { immediate: true })
  onBeforeUnmount(() => { disposed = true; generation += 1; clearTimeout(timer); for (const control of controllers) control.abort() })
  return { turns, input, error, loading, sending, applying, cancelling, hasMore, active, lastTurn, load, send, cancel, apply }
}
