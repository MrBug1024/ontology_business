import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import type { CodingWorkspace } from '@/types/pluginCoding'
import type { ScenarioRelease } from '@/types/scenarioRelease'

export function usePluginStudioContext(releaseId: Ref<string>) {
  const release = ref<ScenarioRelease | null>(null)
  const recent = ref<CodingWorkspace[]>([])
  const loading = ref(false)
  const error = ref('')
  let controller: AbortController | undefined
  let generation = 0
  async function load() {
    controller?.abort()
    const current = ++generation
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const results = await Promise.allSettled([scenarioReleasesApi.get(releaseId.value, controller.signal), pluginCodingApi.recent(releaseId.value, controller.signal)])
      if (current !== generation) return
      const context = results[0]
      const sessions = results[1]
      if (context.status === 'fulfilled') release.value = context.value
      if (sessions.status === 'fulfilled') recent.value = sessions.value
      for (const result of results) if (result.status === 'rejected') throw result.reason
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '发布与会话加载失败' }
    finally { if (current === generation) loading.value = false }
  }
  function updateRecent(value: CodingWorkspace) { recent.value = [value, ...recent.value.filter(item => item.id !== value.id)].slice(0, 5) }
  watch(releaseId, () => { release.value = null; recent.value = []; void load() }, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  return { release, recent, loading, error, load, updateRecent }
}
