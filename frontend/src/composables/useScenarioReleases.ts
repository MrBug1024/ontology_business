import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import type { ReleaseAction, ScenarioRelease } from '@/types/scenarioRelease'

export function useScenarioReleases(scenarioId: Ref<string>) {
  const releases = ref<ScenarioRelease[]>([])
  const loading = ref(false)
  const error = ref('')
  const offset = ref(0)
  const hasMore = ref(false)
  const busyId = ref('')
  let requestRevision = 0
  let controller: AbortController | undefined
  let disposed = false

  async function load() {
    const revision = ++requestRevision
    controller?.abort()
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const page = await scenarioReleasesApi.list(scenarioId.value, offset.value, controller.signal)
      if (disposed || revision !== requestRevision) return
      releases.value = page.items
      hasMore.value = page.has_more
    } catch (caught: unknown) {
      if (disposed || revision !== requestRevision || controller.signal.aborted) return
      error.value = caught instanceof Error ? caught.message : '发布列表加载失败'
    } finally {
      if (!disposed && revision === requestRevision) loading.value = false
    }
  }

  async function change(release: ScenarioRelease, action: ReleaseAction) {
    if (busyId.value) return null
    busyId.value = release.id
    error.value = ''
    try {
      const updated = await scenarioReleasesApi.change(release, action)
      if (disposed) return null
      await load()
      return updated
    } catch (caught: unknown) {
      if (!disposed) error.value = caught instanceof Error ? caught.message : '发布操作失败'
      return null
    } finally {
      if (!disposed) busyId.value = ''
    }
  }

  watch(scenarioId, () => {
    releases.value = []
    offset.value = 0
    void load()
  }, { immediate: true })
  onBeforeUnmount(() => {
    disposed = true
    requestRevision += 1
    controller?.abort()
  })

  return { releases, loading, error, offset, hasMore, busyId, load, change }
}
