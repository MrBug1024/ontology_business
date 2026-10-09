import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import type { CodingResourceCatalog, CodingResourceSelection } from '@/types/pluginCoding'

export const emptyCodingSelection = (): CodingResourceSelection => ({ llm_config_id: '', skill_ids: [], mcp_ids: [] })
export function usePluginCodingSettings(scenarioId: Ref<string>, selection: Ref<CodingResourceSelection>, persist?: (value: CodingResourceSelection) => Promise<boolean>) {
  const catalog = ref<CodingResourceCatalog | null>(null)
  const open = ref(false)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  let generation = 0
  let controller: AbortController | undefined
  const modelLabel = computed(() => catalog.value?.models.find(model => model.id === selection.value.llm_config_id)?.name || '')
  async function load() {
    controller?.abort()
    const current = ++generation
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const value = await pluginCodingApi.resources(scenarioId.value, controller.signal)
      if (current !== generation) return
      catalog.value = value
      if (!selection.value.llm_config_id && value.models[0]) selection.value = { ...selection.value, llm_config_id: value.models[0].id }
    } catch (caught: unknown) { if (current === generation && !controller.signal.aborted) error.value = caught instanceof Error ? caught.message : '编码资源读取失败，请重试' }
    finally { if (current === generation) loading.value = false }
  }
  async function save(value: CodingResourceSelection) {
    if (saving.value) return false
    saving.value = true
    error.value = ''
    const current = generation
    try {
      const accepted = persist ? await persist(value) : true
      if (current !== generation || !accepted) return false
      // Existing projects take their selection from the returned server workspace.
      if (!persist) selection.value = { llm_config_id: value.llm_config_id, skill_ids: [...value.skill_ids], mcp_ids: [...value.mcp_ids] }
      open.value = false
      return true
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '设置未保存，选择已保留'; return false }
    finally { if (current === generation) saving.value = false }
  }
  watch(scenarioId, () => { catalog.value = null; saving.value = false; void load() }, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  return { catalog, open, loading, saving, error, modelLabel, load, save }
}
