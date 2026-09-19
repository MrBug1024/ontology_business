import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { assistantResourcesApi } from '@/api/assistantResources'
import type { ModelingAdvisorResourceOptions, ModelingAdvisorResourceSelection } from '@/types/assistantResources'

const emptyOptions = (): ModelingAdvisorResourceOptions => ({ models: [], skills: [], mcps: [] })
export const emptyModelingAdvisorSelection = (): ModelingAdvisorResourceSelection => ({ llm_config_id: null, skill_ids: [], mcp_ids: [] })

export function useModelingAdvisorResources(scopeKey: Ref<string>, selection: Ref<ModelingAdvisorResourceSelection>) {
  const options = ref(emptyOptions()), loading = ref(false), loaded = ref(false), error = ref('')
  let controller: AbortController | undefined
  const unavailable = computed(() => loaded.value && (
    !!selection.value.llm_config_id && !options.value.models.some(item => item.id === selection.value.llm_config_id)
    || selection.value.skill_ids.some(id => !options.value.skills.some(item => item.id === id))
    || selection.value.mcp_ids.some(id => !options.value.mcps.some(item => item.id === id))
  ))
  function cancelLoad() {
    controller?.abort()
    controller = undefined
    loading.value = false
  }
  async function load() {
    cancelLoad()
    const current = new AbortController()
    controller = current
    loading.value = true
    error.value = ''
    try {
      const result = await assistantResourcesApi.list(current.signal)
      if (controller === current && !current.signal.aborted) { options.value = result; loaded.value = true }
    } catch (caught: unknown) {
      if (controller === current && !current.signal.aborted) error.value = caught instanceof Error ? caught.message : '配置加载失败，请重试。'
    } finally {
      if (controller === current) { loading.value = false; controller = undefined }
    }
  }
  watch(scopeKey, () => {
    cancelLoad()
    options.value = emptyOptions()
    loaded.value = false
    error.value = ''
    selection.value = emptyModelingAdvisorSelection()
  }, { flush: 'sync' })
  onBeforeUnmount(cancelLoad)
  return { options, loading, loaded, error, unavailable, load, cancelLoad }
}
