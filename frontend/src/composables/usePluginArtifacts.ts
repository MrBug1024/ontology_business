import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pluginArtifactsApi } from '@/api/pluginArtifacts'
import type { PluginArtifact } from '@/types/pluginArtifact'

export function usePluginArtifacts(scenarioId: Ref<string>, artifactId: Ref<string>) {
  const items = ref<PluginArtifact[]>([])
  const selected = ref<PluginArtifact | null>(null)
  const loading = ref(false)
  const error = ref('')
  const offset = ref(0)
  const hasMore = ref(false)
  let generation = 0
  let controller: AbortController | undefined
  async function load() {
    controller?.abort()
    controller = new AbortController()
    const current = ++generation
    const selectedId = artifactId.value
    const scope = scenarioId.value
    loading.value = true
    selected.value = null
    error.value = ''
    try {
      const page = await pluginArtifactsApi.list(scope, offset.value, controller.signal)
      if (current !== generation) return
      items.value = page.items
      hasMore.value = page.has_more
      if (selectedId) {
        const value = page.items.find(item => item.id === selectedId) || await pluginArtifactsApi.get(selectedId, controller.signal)
        if (current !== generation) return
        if (scope && value.scenario_id !== scope) throw new Error('此插件版本不属于当前业务场景，请重新选择')
        selected.value = value
      }
    } catch (caught: unknown) {
      if (current === generation) error.value = caught instanceof Error ? caught.message : '插件版本加载失败'
    } finally { if (current === generation) loading.value = false }
  }
  watch(scenarioId, () => { offset.value = 0; items.value = [] })
  watch([scenarioId, artifactId, offset], () => { void load() }, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  return { items, selected, loading, error, offset, hasMore, load }
}
