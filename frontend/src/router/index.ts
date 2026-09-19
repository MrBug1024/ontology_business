import { createRouter, createWebHistory, type LocationQuery } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const mainScrollPositions = new Map<string, number>()
const MAX_SAVED_SCROLL_POSITIONS = 100

function legacyPlatformSettingsRedirect(tab: 'llm' | 'mcp' | 'skills') {
  return (to: { query: LocationQuery }) => {
    const query = { ...to.query }
    query.platform_settings = tab
    delete query.manage
    return {
      name: 'data-sources',
      query,
    }
  }
}

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
    { path: '/members', name: 'members', component: () => import('@/views/OrganizationMembers.vue'), meta: { title: '成员与权限' } },
    { path: '/invitations', name: 'invitations', component: () => import('@/views/WorkspaceInvitations.vue'), meta: { title: '工作区邀请' } },
    { path: '/accounts', name: 'accounts', component: () => import('@/views/SystemAccounts.vue'), meta: { title: '账户管理' } },
    { path: '/scenarios', name: 'scenarios', component: () => import('@/views/Scenarios.vue'), meta: { title: '场景能力' } },
    { path: '/business-distillation/:id?', name: 'business-distillation', component: () => import('@/views/BusinessDistillation.vue'), meta: { title: '业务蒸馏' } },
    { path: '/scenarios/:id', name: 'scenario-detail', component: () => import('@/views/ScenarioDetail.vue'), meta: { title: '场景能力' } },
    { path: '/data-sources', name: 'data-sources', component: () => import('@/views/DataSources.vue'), meta: { title: '资料库' } },
    { path: '/templates', name: 'templates', redirect: (to) => ({ name: 'data-sources', query: { ...to.query, library_tab: 'templates' } }), meta: { title: '资料库' } },
    { path: '/agents', name: 'agents', component: () => import('@/views/Agents.vue'), meta: { title: '验证中心' } },
    { path: '/agents/:id/chat', name: 'agent-chat', component: () => import('@/views/AgentChat.vue'), meta: { title: '能力验证' } },
    { path: '/access', name: 'capability-access', component: () => import('@/views/CapabilityAccess.vue'), meta: { title: '发布与接入' } },
    { path: '/tasks', name: 'tasks', component: () => import('@/views/Tasks.vue'), meta: { title: '运行治理' } },
    { path: '/llm', name: 'llm', redirect: legacyPlatformSettingsRedirect('llm') },
    {
      path: '/mcp',
      name: 'mcp',
      redirect: (to) => {
        if (to.query.section === 'published') return { name: 'capability-access', query: { migrated_from: 'agent-publication' } }
        return legacyPlatformSettingsRedirect('mcp')(to)
      },
    },
    { path: '/skills', name: 'skills', redirect: legacyPlatformSettingsRedirect('skills') },
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
