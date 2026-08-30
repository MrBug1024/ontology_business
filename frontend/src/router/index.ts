import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

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
    { path: '/', redirect: '/scenarios' },
    { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { title: '登录', public: true } },
    { path: '/scenarios', name: 'scenarios', component: () => import('@/views/Scenarios.vue'), meta: { title: '场景能力' } },
    { path: '/scenarios/:id', name: 'scenario-detail', component: () => import('@/views/ScenarioDetail.vue'), meta: { title: '场景能力' } },
    { path: '/data-sources', name: 'data-sources', component: () => import('@/views/DataSources.vue'), meta: { title: '建模资料' } },
    { path: '/templates', name: 'templates', component: () => import('@/views/Templates.vue'), meta: { title: '模板中心' } },
    { path: '/agents', name: 'agents', component: () => import('@/views/Agents.vue'), meta: { title: '验证中心' } },
    { path: '/agents/:id/chat', name: 'agent-chat', component: () => import('@/views/AgentChat.vue'), meta: { title: '能力验证' } },
    { path: '/access', name: 'capability-access', component: () => import('@/views/CapabilityAccess.vue'), meta: { title: '发布与接入' } },
    { path: '/tasks', name: 'tasks', component: () => import('@/views/Tasks.vue'), meta: { title: '运行治理' } },
    { path: '/llm', name: 'llm', component: () => import('@/views/LLMConfigs.vue'), meta: { title: '大模型配置' } },
    {
      path: '/mcp',
      name: 'mcp',
      component: () => import('@/views/MCP.vue'),
      meta: { title: '外部工具' },
      beforeEnter: (to) => to.query.section === 'published'
        ? { name: 'capability-access', query: { migrated_from: 'agent-publication' } }
        : true,
    },
    { path: '/skills', name: 'skills', component: () => import('@/views/Skills.vue'), meta: { title: '本地技能' } },
    { path: '/:pathMatch(.*)*', redirect: '/scenarios' },
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
  const auth = useAuthStore()
  try {
    // The app-level auth store validates the session once. Repeating /me on
    // every menu click makes navigation wait on an unrelated network round
    // trip; API 401 responses still redirect through the axios interceptor.
    if (!auth.initialized) await auth.initialize()
    return auth.user ? true : { name: 'login', query: { redirect: to.fullPath } }
  } catch {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
})

export default router
