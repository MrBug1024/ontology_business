import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { pluginCodingApi } from '@/api/pluginCoding'
import type { CodingFile, CodingProject, CodingProjectSummary, CodingSession } from '@/types/pluginCoding'

export interface ProjectFileSelection { projectId: string; path: string }

/**
 * Explorer state for the single-page plugin IDE. The first tree level is the
 * business scenario; expanding it shows the scenario's plugin source files
 * directly — the one durable source tree per host. Coding sessions live only in
 * the chat pane: they are context-bounded conversations on the shared source
 * and never imply plugin versions. The page, never the explorer, owns
 * navigation; server data stays authoritative.
 */
export function usePluginProjectExplorer() {
  const projectsByScenario = ref<Record<string, CodingProjectSummary[]>>({})
  const fileTrees = ref<Record<string, CodingProject>>({})
  const loading = ref(new Set<string>())
  const errors = ref<Record<string, string>>({})
  const selection = ref<ProjectFileSelection | null>(null)
  const sessionsByScenario = ref<Record<string, CodingSession[]>>({})
  const expandedScenarios = ref(new Set<string>())
  const loadingScenarios = ref(new Set<string>())
  const scenarioErrors = ref<Record<string, string>>({})
  const controllers = new Map<string, AbortController>()
  const generations = new Map<string, number>()

  function isLoading(projectId: string) { return loading.value.has(projectId) }
  function projectOf(projectId: string) { return fileTrees.value[projectId] }
  function projectsFor(scenarioId: string) { return projectsByScenario.value[scenarioId] || [] }
  function sessionsFor(scenarioId: string) { return sessionsByScenario.value[scenarioId] || [] }
  function isScenarioExpanded(scenarioId: string) { return expandedScenarios.value.has(scenarioId) }
  function isScenarioLoading(scenarioId: string) { return loadingScenarios.value.has(scenarioId) }

  async function loadProjectFiles(projectId: string) {
    const current = (generations.get(projectId) || 0) + 1
    generations.set(projectId, current)
    controllers.get(projectId)?.abort()
    const controller = new AbortController()
    controllers.set(projectId, controller)
    const nextLoading = new Set(loading.value)
    nextLoading.add(projectId)
    loading.value = nextLoading
    errors.value = { ...errors.value, [projectId]: '' }
    try {
      const project = await pluginCodingApi.project(projectId, controller.signal)
      if (generations.get(projectId) !== current) return
      fileTrees.value = { ...fileTrees.value, [projectId]: project }
    } catch (caught: unknown) {
      if (generations.get(projectId) !== current) return
      errors.value = { ...errors.value, [projectId]: caught instanceof Error ? caught.message : '插件源码加载失败' }
    } finally {
      if (generations.get(projectId) === current) {
        const clearing = new Set(loading.value)
        clearing.delete(projectId)
        loading.value = clearing
      }
    }
  }

  function ensureFiles(projectId: string) {
    if (!fileTrees.value[projectId] && !isLoading(projectId)) void loadProjectFiles(projectId)
  }

  function selectFile(projectId: string, path: string) {
    ensureFiles(projectId)
    selection.value = { projectId, path }
  }

  function clearSelection() { selection.value = null }
  function reloadProject(projectId: string) { void loadProjectFiles(projectId) }

  async function loadScenario(scenarioId: string) {
    const key = `scenario:${scenarioId}`
    const current = (generations.get(key) || 0) + 1
    generations.set(key, current)
    controllers.get(key)?.abort()
    const controller = new AbortController()
    controllers.set(key, controller)
    const nextLoading = new Set(loadingScenarios.value)
    nextLoading.add(scenarioId)
    loadingScenarios.value = nextLoading
    scenarioErrors.value = { ...scenarioErrors.value, [scenarioId]: '' }
    try {
      const [projects, sessions] = await Promise.all([
        pluginCodingApi.projects(scenarioId, controller.signal),
        pluginCodingApi.sessions(scenarioId, controller.signal),
      ])
      if (generations.get(key) !== current) return
      projectsByScenario.value = { ...projectsByScenario.value, [scenarioId]: projects }
      sessionsByScenario.value = { ...sessionsByScenario.value, [scenarioId]: sessions }
      // Expanding a scenario shows its source directly, so the tree loads now.
      for (const project of projects) ensureFiles(project.id)
    } catch (caught: unknown) {
      if (generations.get(key) !== current) return
      scenarioErrors.value = { ...scenarioErrors.value, [scenarioId]: caught instanceof Error ? caught.message : '插件源码加载失败' }
    } finally {
      if (generations.get(key) === current) {
        const clearing = new Set(loadingScenarios.value)
        clearing.delete(scenarioId)
        loadingScenarios.value = clearing
      }
    }
  }

  function expandScenario(scenarioId: string) {
    // Only one scenario's source tree stays open: expanding another collapses
    // the previous one and aborts its pending reads.
    const previous = [...expandedScenarios.value].find(id => id !== scenarioId)
    if (previous) collapseScenario(previous)
    const next = new Set(expandedScenarios.value)
    next.add(scenarioId)
    expandedScenarios.value = next
    if (!projectsByScenario.value[scenarioId] && !isScenarioLoading(scenarioId)) void loadScenario(scenarioId)
    else for (const project of projectsFor(scenarioId)) ensureFiles(project.id)
  }

  function collapseScenario(scenarioId: string) {
    const next = new Set(expandedScenarios.value)
    next.delete(scenarioId)
    expandedScenarios.value = next
    controllers.get(`scenario:${scenarioId}`)?.abort()
    generations.set(`scenario:${scenarioId}`, (generations.get(`scenario:${scenarioId}`) || 0) + 1)
    for (const project of projectsFor(scenarioId)) {
      controllers.get(project.id)?.abort()
      generations.set(project.id, (generations.get(project.id) || 0) + 1)
    }
    if (projectsFor(scenarioId).some(project => selection.value?.projectId === project.id)) selection.value = null
  }

  function toggleScenario(scenarioId: string) { if (isScenarioExpanded(scenarioId)) collapseScenario(scenarioId); else expandScenario(scenarioId) }
  function reloadScenario(scenarioId: string) { void loadScenario(scenarioId) }

  const selectedFile = computed<CodingFile | null>(() => {
    const current = selection.value
    if (!current) return null
    return fileTrees.value[current.projectId]?.files.find(file => file.path === current.path) || null
  })
  const selectedProject = computed<CodingProjectSummary | null>(() => {
    const current = selection.value
    if (!current) return null
    for (const projects of Object.values(projectsByScenario.value)) {
      const found = projects.find(project => project.id === current.projectId)
      if (found) return found
    }
    return null
  })

  function projectSummary(projectId: string): CodingProjectSummary | null {
    for (const projects of Object.values(projectsByScenario.value)) {
      const found = projects.find(project => project.id === projectId)
      if (found) return found
    }
    return null
  }

  onBeforeUnmount(() => {
    for (const controller of controllers.values()) controller.abort()
    controllers.clear()
    generations.clear()
  })

  return reactive({ projectsByScenario, fileTrees, loading, errors, selection, selectedFile, selectedProject,
    sessionsByScenario, expandedScenarios, loadingScenarios, scenarioErrors,
    isLoading, projectOf, projectSummary, projectsFor, sessionsFor, selectFile, clearSelection, reloadProject,
    isScenarioExpanded, isScenarioLoading, expandScenario, collapseScenario, toggleScenario, reloadScenario })
}

export type PluginProjectExplorer = ReturnType<typeof usePluginProjectExplorer>
