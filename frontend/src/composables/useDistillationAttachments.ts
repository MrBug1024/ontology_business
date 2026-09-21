import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { distillationConversationApi as api } from '@/api/distillationConversation'
import type { DistillationAttachment, DistillationAttachmentDraft } from '@/types/distillationConversation'

export function useDistillationAttachments(projectId: Ref<string>) {
  const attachments = ref<DistillationAttachmentDraft[]>([]), loading = ref(false), error = ref('')
  const now = ref(Date.now())
  const expiryTimer = setInterval(() => { now.value = Date.now() }, 30_000)
  const files = new Map<string, File>(), controllers = new Set<AbortController>(), consumed = new Set<string>(), uncertain = new Set<string>()
  const busy = computed(() => loading.value || attachments.value.some(item => ['uploading', 'removing'].includes(item.status)))
  const readyIds = computed(() => attachments.value.flatMap(item => ['ready', 'bound'].includes(item.status) && item.attachment && ['ready', 'bound'].includes(item.attachment.status) && Date.parse(item.attachment.expires_at) > now.value ? [item.attachment.id] : []))
  const blocked = computed(() => busy.value || attachments.value.some(item => !['ready', 'bound'].includes(item.status) || !item.attachment || !['ready', 'bound'].includes(item.attachment.status) || Date.parse(item.attachment.expires_at) <= now.value))
  let generation = 0, listVersion = 0, disposed = false
  function control() { const value = new AbortController(); controllers.add(value); return value }
  function valid(epoch: number, controller: AbortController) { return !disposed && epoch === generation && !controller.signal.aborted }
  function message(caught: unknown) { return caught instanceof Error ? caught.message : '附件处理失败，请重试。' }
  function restored(item: DistillationAttachment): DistillationAttachmentDraft { return { key: item.request_id, filename: item.filename, byte_size: item.byte_size, status: item.status === 'bound' ? 'bound' : 'ready', progress: 100, error: '', attachment: item } }
  async function load() {
    if (!projectId.value) return
    const epoch = generation, version = ++listVersion, controller = control()
    loading.value = true
    try {
      const rows = await api.attachments(projectId.value, controller.signal)
      if (valid(epoch, controller) && version === listVersion) {
        const serverIds = new Set(rows.map(item => item.id)), serverRequests = new Set(rows.map(item => item.request_id))
        const retained = attachments.value.filter(item => (
          !item.attachment
          || ['uploading', 'removing'].includes(item.status)
          || serverIds.has(item.attachment.id)
          || serverRequests.has(item.key)
        ))
        for (const item of attachments.value) if (!retained.includes(item) && item.attachment) files.delete(item.key)
        attachments.value = retained
        for (const item of rows) {
          if (consumed.has(item.id)) continue
          const existing = attachments.value.find(row => row.key === item.request_id || row.attachment?.id === item.id)
          if (!existing) attachments.value.push(restored(item))
          else if (existing.status !== 'removing') Object.assign(existing, restored(item))
          files.delete(item.request_id)
          uncertain.delete(item.request_id)
        }
        error.value = ''
      }
    } catch (caught: unknown) { if (valid(epoch, controller) && version === listVersion) error.value = message(caught) }
    finally { controllers.delete(controller); if (valid(epoch, controller) && version === listVersion) loading.value = false }
  }
  async function upload(item: DistillationAttachmentDraft) {
    const file = files.get(item.key)
    if (!file || !projectId.value) return
    const epoch = generation, controller = control()
    item.status = 'uploading'; item.error = ''; item.progress = 0
    try {
      const result = await api.upload(projectId.value, item.key, file, controller.signal, value => { if (valid(epoch, controller)) item.progress = value })
      if (valid(epoch, controller)) {
        attachments.value = attachments.value.filter(row => row.key === item.key || row.attachment?.id !== result.id)
        item.attachment = result; item.status = 'ready'; item.progress = 100
        files.delete(item.key)
        uncertain.delete(item.key)
      }
    } catch (caught: unknown) {
      if (valid(epoch, controller)) {
        item.status = 'failed'; item.error = message(caught)
        const rejected = caught instanceof Error && 'status' in caught && [400, 403, 404, 413, 415, 422].includes(Number(caught.status))
        if (rejected) uncertain.delete(item.key); else uncertain.add(item.key)
      }
    }
    finally { controllers.delete(controller) }
  }
  async function add(selected: File[]) {
    const epoch = generation
    error.value = ''
    const composerCount = attachments.value.filter(item => item.status !== 'bound').length
    if (composerCount + selected.length > 5) { error.value = '每轮最多添加 5 份临时附件，请先移除部分附件。'; return }
    for (const file of selected) {
      if (disposed || epoch !== generation) break
      const item: DistillationAttachmentDraft = { key: crypto.randomUUID(), filename: file.name, byte_size: file.size, status: 'failed', progress: 0, error: '', attachment: null }
      attachments.value.push(item)
      files.set(item.key, file)
      const state = attachments.value[attachments.value.length - 1]
      if (!file.size || file.size > 10 * 1024 * 1024) { state.error = '请选择非空且不超过 10 MB 的文件。'; continue }
      await upload(state)
    }
  }
  async function removeSubmitted(id: string) {
    const epoch = generation, controller = control()
    try {
      await api.removeAttachment(projectId.value, id, controller.signal)
      if (valid(epoch, controller)) {
        consumed.add(id)
        const removed = attachments.value.filter(item => item.attachment?.id === id)
        for (const item of removed) files.delete(item.key)
        attachments.value = attachments.value.filter(item => item.attachment?.id !== id)
        error.value = ''
        return true
      }
    }
    catch (caught: unknown) { if (valid(epoch, controller)) error.value = message(caught) }
    finally { controllers.delete(controller) }
    return false
  }
  async function remove(key: string) {
    const item = attachments.value.find(row => row.key === key)
    if (!item || ['uploading', 'removing'].includes(item.status)) return
    if (!item.attachment && uncertain.has(key)) { await upload(item); if (!item.attachment) return }
    if (!item.attachment) { attachments.value = attachments.value.filter(row => row.key !== key); files.delete(key); return }
    const epoch = generation, controller = control()
    const previousStatus = item.status
    item.status = 'removing'
    try {
      await api.removeAttachment(projectId.value, item.attachment.id, controller.signal)
      if (valid(epoch, controller)) { consumed.add(item.attachment.id); attachments.value = attachments.value.filter(row => row.key !== key); files.delete(key) }
    } catch (caught: unknown) { if (valid(epoch, controller)) { item.status = previousStatus; item.error = message(caught) } }
    finally { controllers.delete(controller) }
  }
  function retry(key: string) { const item = attachments.value.find(row => row.key === key); if (item?.status === 'failed') return upload(item) }
  function sent(ids: string[]) {
    ids.forEach(id => {
      consumed.add(id)
      const item = attachments.value.find(row => row.attachment?.id === id)
      if (item?.attachment) {
        item.status = 'bound'
        item.attachment = { ...item.attachment, status: 'bound' }
        files.delete(item.key)
      }
    })
    for (const key of files.keys()) if (!attachments.value.some(item => item.key === key)) files.delete(key)
  }
  watch(projectId, () => { generation += 1; for (const controller of controllers) controller.abort(); controllers.clear(); files.clear(); consumed.clear(); uncertain.clear(); attachments.value = []; loading.value = false; error.value = ''; void load() }, { immediate: true })
  onBeforeUnmount(() => { disposed = true; generation += 1; clearInterval(expiryTimer); for (const controller of controllers) controller.abort(); files.clear() })
  return { attachments, loading, error, busy, blocked, readyIds, load, add, remove, retry, sent, removeSubmitted }
}
