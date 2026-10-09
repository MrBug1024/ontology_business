import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import type { CodingSettingsUpdate, CodingUpdate, CodingWorkspace } from '@/types/pluginCoding'

export function usePluginCodingWorkspace(workspaceId: Ref<string>, releaseId?: Ref<string>) {
  const workspace = ref<CodingWorkspace | null>(null)
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')
  let generation = 0
  let disposed = false
  let poll: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  const operations = new Set<AbortController>()

  async function load() {
    if (!workspaceId.value || disposed) return
    const id = workspaceId.value
    const current = ++generation
    controller?.abort()
    controller = new AbortController()
    const signal = controller.signal
    if (poll) clearTimeout(poll)
    loading.value = true
    try {
      const value = await pluginCodingApi.get(id, signal)
      if (disposed || current !== generation || id !== workspaceId.value) return
      if (releaseId?.value && value.release_id !== releaseId.value) throw new Error('此编码会话不属于当前业务发布，请选择对应会话')
      workspace.value = value
      error.value = ''
      if (value.run_status && ['waiting_upload', 'queued', 'running'].includes(value.run_status)) poll = setTimeout(load, 1500)
    } catch (caught: unknown) {
      if (!disposed && current === generation && !signal.aborted) error.value = caught instanceof Error ? caught.message : '编码工作台加载失败'
    } finally { if (!disposed && current === generation) loading.value = false }
  }

  async function mutate(payload: CodingUpdate | CodingSettingsUpdate, actionKind: 'revision' | 'settings') {
    if (!workspace.value || busy.value) return false
    const id = workspaceId.value
    const action = new AbortController()
    operations.add(action)
    busy.value = true
    error.value = ''
    generation += 1
    controller?.abort()
    if (poll) clearTimeout(poll)
    try {
      const value = actionKind === 'settings'
        ? await pluginCodingApi.settings(id, payload as CodingSettingsUpdate, action.signal)
        : await pluginCodingApi.revise(id, payload as CodingUpdate, action.signal)
      if (disposed || id !== workspaceId.value) return false
      workspace.value = value
      void load()
      return true
    } catch (caught: unknown) {
      if (!disposed && id === workspaceId.value && !action.signal.aborted) {
        error.value = caught instanceof Error ? caught.message : actionKind === 'settings' ? '设置未保存，选择已保留' : '修订未提交，草稿已保留'
        // Preserve the actionable error while recovering the newest file basis.
        const message = error.value
        await load()
        error.value = message
      }
      return false
    } finally { operations.delete(action); if (!disposed && id === workspaceId.value) busy.value = false }
  }
  const revise = (payload: CodingUpdate) => mutate(payload, 'revision')
  const settings = (payload: CodingSettingsUpdate) => mutate(payload, 'settings')

  watch(workspaceId, () => {
    generation += 1
    controller?.abort()
    for (const action of operations) action.abort()
    if (poll) clearTimeout(poll)
    workspace.value = null
    busy.value = false
    error.value = ''
    void load()
  }, { immediate: true })
  onBeforeUnmount(() => {
    disposed = true
    generation += 1
    if (poll) clearTimeout(poll)
    controller?.abort()
    for (const action of operations) action.abort()
  })
  return { workspace, loading, busy, error, load, revise, settings }
}
