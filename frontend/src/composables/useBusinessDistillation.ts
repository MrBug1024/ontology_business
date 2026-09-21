import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { businessDistillationApi as api } from '@/api/businessDistillation'
import { draftOf } from '@/utils/businessDistillation'
import type { DataSource, Scenario } from '@/types'
import type { DistillationArtifact, DistillationProject, DistillationProposal, DistillationPublication, DistillationScenarioState } from '@/types/businessDistillation'

export function useBusinessDistillation(projectId: Ref<string>, historyScope: Ref<string> = ref(''), lockScope = false) {
  const projects = ref<DistillationProject[]>([])
  const project = ref<DistillationProject | null>(null)
  const draft = ref(draftOf())
  const baseline = ref(draftOf())
  const scenarioRevision = ref<number | null>(null)
  const scenarios = ref<Scenario[]>([])
  const materials = ref<DataSource[]>([])
  const publications = ref<DistillationPublication[]>([])
  const proposal = ref<DistillationProposal | null>(null)
  const error = ref('')
  const notice = ref('')
  const loading = ref(false)
  const listing = ref(false)
  const busy = ref('')
  const offset = ref(0)
  const materialOffset = ref(0)
  const materialHasMore = ref(false)
  const materialLoading = ref(false)
  const hasMore = computed(() => projects.value.length === 50)
  function emptyDraft() { const value = draftOf(); value.scenario_id = historyScope.value && historyScope.value !== 'shared' ? historyScope.value : null; return value }
  const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(project.value ? draftOf(project.value) : baseline.value))
  let generation = 0
  let disposed = false
  let loadController: AbortController | undefined
  let listController: AbortController | undefined
  let actionController: AbortController | undefined
  let optionsController: AbortController | undefined
  let optionsGeneration = 0
  const materialPageSize = 50

  function errorMessage(caught: unknown): string {
    if (caught instanceof Error && 'status' in caught && caught.status === 409) {
      return `${caught.message || '当前操作存在冲突'}。当前草稿已保留；如版本已变化，请保留所需内容后重新加载并核对。`
    }
    if (caught instanceof Error && 'status' in caught && caught.status === 422 && 'detail' in caught && Array.isArray(caught.detail)) {
      const messages = caught.detail.flatMap((item: unknown) => item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string' ? [item.msg] : [])
      return messages.slice(0, 3).join('；') || '请检查必填内容、证据引用与流程连线后重试。'
    }
    return caught instanceof Error ? caught.message : '操作未完成，请重试。当前草稿已保留。'
  }

  async function list() {
    listController?.abort()
    const controller = new AbortController()
    listController = controller
    listing.value = true
    try {
      const rows = await api.list(offset.value, controller.signal, historyScope.value || 'shared')
      if (!disposed && !controller.signal.aborted) projects.value = rows
    } catch (caught: unknown) {
      if (!disposed && !controller.signal.aborted) error.value = errorMessage(caught)
    } finally {
      if (!disposed && !controller.signal.aborted) listing.value = false
    }
  }

  async function load() {
    const current = ++generation
    loadController?.abort()
    actionController?.abort()
    busy.value = ''
    const controller = new AbortController()
    loadController = controller
    error.value = ''
    notice.value = ''
    proposal.value = null
    project.value = null
    publications.value = []
    scenarioRevision.value = null
    draft.value = emptyDraft()
    baseline.value = draftOf()
    const scenarioId = historyScope.value && historyScope.value !== 'shared' ? historyScope.value : ''
    if (!projectId.value) {
      if (!scenarioId) { loading.value = false; return }
      loading.value = true
      try {
        const [state, versions] = await Promise.all([
          api.scenarioState(scenarioId, controller.signal),
          api.scenarioPublications(scenarioId, controller.signal),
        ])
        if (disposed || current !== generation) return
        draft.value = { name: '', scenario_id: scenarioId, document: state.document }
        scenarioRevision.value = state.revision
        baseline.value = draftOf({ id: '', name: '', scenario_id: scenarioId, revision: state.revision,
          document: state.document, created_at: state.updated_at, updated_at: state.updated_at, can_write: true })
        publications.value = versions
      } catch (caught: unknown) {
        if (!disposed && current === generation && !controller.signal.aborted) error.value = errorMessage(caught)
      } finally {
        if (!disposed && current === generation) loading.value = false
      }
      return
    }
    loading.value = true
    try {
      const row = await api.get(projectId.value, controller.signal)
      const versions = row.scenario_id
        ? await api.scenarioPublications(row.scenario_id, controller.signal)
        : await api.publications(row.id, controller.signal)
      if (disposed || current !== generation) return
      if (lockScope && (!historyScope.value || historyScope.value === 'shared' || row.scenario_id !== historyScope.value)) {
        throw new Error('该蒸馏会话不属于当前业务场景')
      }
      project.value = row
      draft.value = draftOf(row)
      scenarioRevision.value = null
      baseline.value = draftOf(row)
      publications.value = versions
    } catch (caught: unknown) {
      if (!disposed && current === generation && !controller.signal.aborted) error.value = errorMessage(caught)
    } finally {
      if (!disposed && current === generation) loading.value = false
    }
  }

  async function run<T>(label: string, action: (signal: AbortSignal) => Promise<T>): Promise<T | null> {
    if (busy.value || loading.value) return null
    busy.value = label
    error.value = ''
    notice.value = ''
    const current = generation
    const controller = new AbortController()
    actionController = controller
    try {
      const value = await action(controller.signal)
      return disposed || current !== generation || controller.signal.aborted ? null : value
    } catch (caught: unknown) {
      if (!disposed && current === generation && !controller.signal.aborted) error.value = errorMessage(caught)
      return null
    } finally {
      if (!disposed && current === generation && actionController === controller) busy.value = ''
    }
  }

  async function save() {
    if (!draft.value.name.trim()) { error.value = '请先填写蒸馏项目名称。'; return null }
    if (lockScope) draft.value.scenario_id = historyScope.value
    const payload = !project.value && scenarioRevision.value !== null
      ? { ...draft.value, expected_scenario_revision: scenarioRevision.value }
      : draft.value
    const row = await run('save', signal => project.value
      ? api.update(project.value.id, project.value.revision, draft.value, signal)
      : api.create(payload, signal))
    if (!row) return null
    project.value = row
    draft.value = draftOf(row)
    scenarioRevision.value = null
    proposal.value = null
    notice.value = '已保存，可继续分析或编辑。'
    void list()
    return row
  }

  async function analyze(instructions: string) {
    const row = project.value
    if (!row || dirty.value) { error.value = '请先保存当前草稿，再生成分析建议。'; return }
    const result = await run('analyze', signal => api.analyze(row.id, row.revision, instructions, signal))
    if (result) proposal.value = result
  }

  async function copyToScenario(scenarioId: string) {
    const row = project.value
    if (!row || dirty.value || !scenarioId) return null
    const copy = draftOf(row)
    copy.scenario_id = scenarioId
    const result = await run('copy', signal => api.create(copy, signal))
    if (!result) return null
    project.value = result
    draft.value = draftOf(result)
    publications.value = []
    void list()
    return result
  }

  function applyProposal() {
    if (!proposal.value || !project.value || dirty.value || proposal.value.base_revision !== project.value.revision) {
      error.value = '当前草稿或版本已变化，请重新保存并分析。'
      return
    }
    draft.value.document = JSON.parse(JSON.stringify(proposal.value.document)) as DistillationProposal['document']
    proposal.value = null
    notice.value = '建议已放入待保存草稿，请逐项核对，尤其是事实、证据与未决问题。'
  }

  async function publish() {
    const row = project.value
    if (!row || dirty.value) { error.value = '请保存并核对当前版本后再保存到资料库。'; return null }
    const result = await run('publish', signal => api.publish(row.id, row.revision, signal))
    if (result) {
      publications.value = [result, ...publications.value.filter(item => item.id !== result.id)]
      notice.value = '已保存到资料库。该版本保持不变，后续修改可发布新版本。'
    }
    return result
  }

  async function download(publication: DistillationPublication, artifact: DistillationArtifact) {
    const result = await run('download', signal => publication.project_id
      ? api.artifact(publication.project_id, publication.id, artifact.key, signal)
      : api.scenarioArtifact(publication.scenario_id || historyScope.value, publication.id, artifact.key, signal))
    if (!result) return
    const url = URL.createObjectURL(result)
    const link = document.createElement('a')
    link.href = url
    link.download = artifact.filename
    link.click()
    URL.revokeObjectURL(url)
  }

  async function removePublication(publication: DistillationPublication) {
    const result = await run('delete-publication', signal => api.deleteProduct(publication.id, signal))
    if (result === null) return false
    publications.value = publications.value.filter(item => item.id !== publication.id)
    notice.value = `业务蒸馏产物版本 ${publication.project_revision} 已删除，资料库投影同时移除。`
    return true
  }

  function cancelAnalysis() {
    if (busy.value !== 'analyze') return
    actionController?.abort()
    busy.value = ''
    notice.value = '已停止等待分析结果，当前草稿已保留。'
  }

  async function remove(projectId: string) {
    const result = await run('delete', signal => api.remove(projectId, signal))
    if (result !== null) void list()
    return result !== null
  }

  async function refreshOptions() {
    optionsController?.abort()
    const controller = new AbortController()
    optionsController = controller
    const current = ++optionsGeneration
    materialLoading.value = true
    const scenarioId = historyScope.value && historyScope.value !== 'shared' ? historyScope.value : undefined
    const results = await Promise.allSettled([
      // The embedded scene workspace already has a fixed scene context and
      // must not require workspace-level scenario-list permission just to
      // load its own materials.
      lockScope ? Promise.resolve(scenarios.value) : api.scenarios(controller.signal),
      api.materials(scenarioId, materialOffset.value, materialPageSize, controller.signal),
    ])
    if (disposed || controller.signal.aborted || current !== optionsGeneration) return
    const [scenarioResult, materialResult] = results
    if (scenarioResult.status === 'fulfilled') scenarios.value = scenarioResult.value
    if (materialResult.status === 'fulfilled') {
      materials.value = materialResult.value.items
      materialHasMore.value = materialResult.value.has_more
    }
    if (results.some(item => item.status === 'rejected')) error.value = '部分场景或资料库加载失败，请刷新资料选项重试。'
    materialLoading.value = false
  }

  function previousMaterialPage() {
    if (!materialLoading.value && materialOffset.value > 0) materialOffset.value = Math.max(0, materialOffset.value - materialPageSize)
  }

  function nextMaterialPage() {
    if (!materialLoading.value && materialHasMore.value) materialOffset.value += materialPageSize
  }

  watch(projectId, () => { void load() }, { immediate: true })
  watch(offset, () => { void list() }, { immediate: true })
  watch(historyScope, () => {
    projects.value = []
    materials.value = []
    materialHasMore.value = false
    offset.value = 0
    if (!projectId.value) { draft.value = emptyDraft(); void load() }
    if (materialOffset.value) materialOffset.value = 0
    else void refreshOptions()
    void list()
  }, { immediate: true })
  watch(materialOffset, () => { void refreshOptions() })
  onBeforeUnmount(() => {
    disposed = true
    generation += 1
    loadController?.abort()
    listController?.abort()
    actionController?.abort()
    optionsController?.abort()
  })
  return { projects, project, draft, baseline, scenarioRevision, scenarios, materials, publications, proposal, error, notice, loading, listing,
    busy, offset, hasMore, dirty, materialOffset, materialHasMore, materialLoading, materialPageSize,
    list, load, save, analyze, applyProposal, publish, download, removePublication, cancelAnalysis, refreshOptions, remove,
    previousMaterialPage, nextMaterialPage, copyToScenario }
}
