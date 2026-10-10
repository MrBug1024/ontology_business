import { computed, onBeforeUnmount, reactive, ref, watch, type Ref } from 'vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import { createClientRequestId } from '@/utils/clientRequestId'
import type { CodingContext, CodingResourceCatalog, CodingResourceSelection, CodingWorkspace } from '@/types/pluginCoding'
import type { ScenarioRelease } from '@/types/scenarioRelease'
import type { PluginHost } from '@/types/scenarioPackage'

export function usePluginTaskStart(release: Ref<ScenarioRelease>, initialSettings?: CodingResourceSelection) {
  const context = ref<CodingContext | null>(null)
  const catalog = ref<CodingResourceCatalog | null>(null)
  const selected = reactive<Record<string, boolean>>({})
  const instruction = ref('')
  const resourceSelection = ref<CodingResourceSelection>({ llm_config_id: initialSettings?.llm_config_id || '', skill_ids: [...(initialSettings?.skill_ids || [])], mcp_ids: [...(initialSettings?.mcp_ids || [])] })
  const models = computed(() => catalog.value?.models || [])
  const modelId = computed({ get: () => resourceSelection.value.llm_config_id, set: (value: string) => { resourceSelection.value = { ...resourceSelection.value, llm_config_id: value } } })
  const target = ref<PluginHost>('claude_code')
  const loading = ref(false)
  const building = ref(false)
  const error = ref('')
  let generation = 0
  let controller: AbortController | undefined
  let admissionBody = ''
  let admissionId = ''
  const capabilities = computed(() => context.value?.capabilities || [])
  const selectedCapabilities = computed(() => capabilities.value.filter(item => selected[`${item.kind}:${item.key}`]).map(({ kind, key }) => ({ kind, key })))
  const resourcesValid = computed(() => Boolean(resourceSelection.value.skill_ids.length <= 10 && resourceSelection.value.mcp_ids.length <= 10 && catalog.value?.models.some(model => model.id === modelId.value && (!resourceSelection.value.mcp_ids.length || model.supports_tools)) && resourceSelection.value.skill_ids.every(id => catalog.value?.skills.some(item => item.id === id)) && resourceSelection.value.mcp_ids.every(id => catalog.value?.mcps.some(item => item.id === id))))
  const canStart = computed(() => !loading.value && !building.value && release.value.enabled && selectedCapabilities.value.length > 0 && selectedCapabilities.value.length <= 20 && Boolean(instruction.value.trim()) && resourcesValid.value)
  async function load() {
    controller?.abort()
    const current = ++generation
    controller = new AbortController()
    loading.value = true
    building.value = false
    error.value = ''
    const previousReleaseId = context.value?.deployment.release_id
    const previousSelection = { ...selected }
    context.value = null
    for (const key of Object.keys(selected)) delete selected[key]
    try {
      const results = await Promise.allSettled([pluginCodingApi.context(release.value.id, controller.signal, target.value), pluginCodingApi.resources(release.value.scenario_id, controller.signal)])
      if (current !== generation) return
      const contract = results[0]
      const configurations = results[1]
      if (contract.status === 'fulfilled') {
        if (contract.value.deployment.release_id !== release.value.id || contract.value.scenario.id !== release.value.scenario_id) throw new Error('返回的能力契约不属于当前场景版本，请重新读取')
        if (contract.value.delivery_profile && contract.value.delivery_profile.host.key !== target.value) throw new Error('返回的插件交付规范不属于所选宿主，请重新读取')
        context.value = contract.value
        contract.value.capabilities.forEach((item, index) => {
          const key = `${item.kind}:${item.key}`
          selected[key] = previousReleaseId === release.value.id ? previousSelection[key] === true : index < 20
        })
      }
      if (configurations.status === 'fulfilled') {
        catalog.value = configurations.value
        if (!modelId.value) modelId.value = configurations.value.models[0]?.id || ''
      }
      for (const result of results) if (result.status === 'rejected') throw result.reason
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '场景能力加载失败' }
    finally { if (current === generation) loading.value = false }
  }
  async function start(): Promise<CodingWorkspace | null> {
    if (!canStart.value) return null
    const current = generation
    building.value = true
    error.value = ''
    const payload = { expected_revision: release.value.revision, target: target.value, capabilities: selectedCapabilities.value, ...resourceSelection.value, instruction: instruction.value }
    const body = JSON.stringify(payload)
    if (body !== admissionBody) { admissionBody = body; admissionId = createClientRequestId() }
    controller?.abort()
    controller = new AbortController()
    try {
      const value = await pluginCodingApi.start(release.value.id, { ...payload, request_id: admissionId }, controller.signal)
      return current === generation ? value : null
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '编码任务创建失败；需求已保留，可重试'; return null }
    finally { if (current === generation) building.value = false }
  }
  function applyFixedCapabilities(fixed: Array<{ kind: string; key: string }>) {
    // An existing project fixes the capability scope; the form reflects it.
    for (const key of Object.keys(selected)) delete selected[key]
    for (const item of fixed) selected[`${item.kind}:${item.key}`] = true
  }
  watch([() => release.value.id, target], load, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  return { context, capabilities, catalog, models, resourceSelection, selected, instruction, modelId, target, loading, building, error, canStart, load, start, applyFixedCapabilities }
}
