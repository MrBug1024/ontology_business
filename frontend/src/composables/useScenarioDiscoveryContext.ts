import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { scenarioDiscoveryContextApi } from '@/api/scenarioDiscoveryContext'
import { SCENARIO_DISCOVERY_CONTEXT_CHANGED_EVENT, type ScenarioDiscoveryContextChangedDetail } from '@/utils/scenarioAdvisorEvents'
import type { ScenarioDiscoveryContext } from '@/types/scenarioDiscoveryContext'

export function useScenarioDiscoveryContext(scenarioId: Ref<string>) {
  const context = ref<ScenarioDiscoveryContext | null>(null)
  const loading = ref(false)
  const error = ref('')
  const unauthorized = ref(false)
  let generation = 0
  let disposed = false
  let controller: AbortController | undefined

  async function load() {
    const id = scenarioId.value
    const current = ++generation
    controller?.abort()
    context.value = null
    error.value = ''
    unauthorized.value = false
    loading.value = Boolean(id)
    if (!id) return
    const request = new AbortController()
    controller = request
    try {
      const value = await scenarioDiscoveryContextApi.get(id, request.signal)
      if (disposed || current !== generation || request.signal.aborted) return
      if (value.scenario.id !== id) throw new Error('返回的业务认知不属于当前场景，请重新读取。')
      context.value = value
    } catch (caught: unknown) {
      if (disposed || current !== generation || request.signal.aborted) return
      const status = caught instanceof Error && 'status' in caught ? caught.status : undefined
      unauthorized.value = status === 401 || status === 403 || status === 404
      error.value = unauthorized.value ? '当前业务认知不可访问，请核对场景权限。' : caught instanceof Error ? caught.message : '场景业务认知读取失败，请重试。'
    } finally {
      if (!disposed && current === generation) loading.value = false
    }
  }

  function onChanged(event: Event) {
    const detail = (event as CustomEvent<ScenarioDiscoveryContextChangedDetail>).detail
    if (detail?.scenario_id === scenarioId.value) void load()
  }

  watch(scenarioId, load, { immediate: true })
  onMounted(() => window.addEventListener(SCENARIO_DISCOVERY_CONTEXT_CHANGED_EVENT, onChanged))
  onBeforeUnmount(() => {
    disposed = true
    generation++
    controller?.abort()
    window.removeEventListener(SCENARIO_DISCOVERY_CONTEXT_CHANGED_EVENT, onChanged)
  })
  return { context, loading, error, unauthorized, load }
}
