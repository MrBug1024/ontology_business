import { createRouter, createWebHistory } from 'vue-router'
import { api } from '@/api'

const mainScrollPositions = new Map<string, number>()
const MAX_SAVED_SCROLL_POSITIONS = 100

function saveMainScrollPosition(fullPath: string) {
  const main = document.getElementById('main-content')
  if (!main) return
  // Refresh insertion order so the cap evicts the least recently visited path.
  mainScrollPositions.delete(fullPath)
  mainScrollPositions.set(fullPath, main.scrollTop)
  if (mainScrollPositions.size <= MAX_SAVED_SCROLL_POSITIONS) return
  const oldestPath = mainScrollPositions.keys().next().value
  if (oldestPath) mainScrollPositions.delete(oldestPath)
}

const router = createRouter({
  history: createWebHistory(),
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    if (to.path !== from.path || to.name !== from.name) return { top: 0 }
    return false
  },
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { title: '登录', public: true } },
    { path: '/dashboard', name: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '仪表盘' } },
    { path: '/tasks', name: 'tasks', component: () => import('@/views/Tasks.vue'), meta: { title: '任务中心' } },
    { path: '/incidents', name: 'incidents', component: () => import('@/views/Incidents.vue'), meta: { title: '事件中心' } },
    { path: '/lineage', name: 'lineage', component: () => import('@/views/Lineage.vue'), meta: { title: '端到端血缘' } },
    { path: '/releases', name: 'releases', component: () => import('@/views/Releases.vue'), meta: { title: '发布治理' } },
    { path: '/connectors', name: 'connectors', component: () => import('@/views/Connectors.vue'), meta: { title: '连接器与环境' } },
    { path: '/advanced-assets', name: 'advanced-assets', component: () => import('@/views/AdvancedAssets.vue'), meta: { title: '高级数据与模型' } },
    { path: '/scenarios', name: 'scenarios', component: () => import('@/views/Scenarios.vue'), meta: { title: '业务场景' } },
    { path: '/scenarios/:id', name: 'scenario-detail', component: () => import('@/views/ScenarioDetail.vue'), meta: { title: '场景详情' } },
    { path: '/data-sources', name: 'data-sources', component: () => import('@/views/DataSources.vue'), meta: { title: '数据源' } },
    { path: '/permissions', name: 'permissions', component: () => import('@/views/Permissions.vue'), meta: { title: '权限与成员' } },
    { path: '/agents', name: 'agents', component: () => import('@/views/Agents.vue'), meta: { title: 'Agent 管理' } },
    { path: '/agents/:id/chat', name: 'agent-chat', component: () => import('@/views/AgentChat.vue'), meta: { title: 'AI 对话' } },
    { path: '/skills', name: 'skills', component: () => import('@/views/Skills.vue'), meta: { title: '技能' } },
    { path: '/mcp', name: 'mcp', component: () => import('@/views/MCP.vue'), meta: { title: 'MCP 服务' } },
    { path: '/llm', name: 'llm', component: () => import('@/views/LLMConfigs.vue'), meta: { title: 'LLM 配置' } },
  ],
})

router.afterEach((to, from, failure) => {
  if (failure) return
  if (from.matched.length) saveMainScrollPosition(from.fullPath)
  document.title = `${to.meta.title || ''} · 业务场景本体智能平台`
  if (to.path === from.path && to.name === from.name) return
  const targetFullPath = to.fullPath
  const targetScrollTop = mainScrollPositions.get(targetFullPath) ?? 0
  requestAnimationFrame(() => {
    if (router.currentRoute.value.fullPath !== targetFullPath) return
    const main = document.getElementById('main-content')
    if (!main) return
    main.scrollTop = targetScrollTop
    main.focus({ preventScroll: true })
  })
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    await api.me()
    return true
  } catch {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
})

export default router
