import { createRouter, createWebHistory } from 'vue-router'
import { api } from '@/api'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { title: '登录', public: true } },
    { path: '/dashboard', name: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '仪表盘' } },
    { path: '/tasks', name: 'tasks', component: () => import('@/views/Tasks.vue'), meta: { title: '任务中心' } },
    { path: '/lineage', name: 'lineage', component: () => import('@/views/Lineage.vue'), meta: { title: '端到端血缘' } },
    { path: '/scenarios', name: 'scenarios', component: () => import('@/views/Scenarios.vue'), meta: { title: '业务场景' } },
    { path: '/scenarios/:id', name: 'scenario-detail', component: () => import('@/views/ScenarioDetail.vue'), meta: { title: '场景详情' } },
    { path: '/data-sources', name: 'data-sources', component: () => import('@/views/DataSources.vue'), meta: { title: '数据源' } },
    { path: '/agents', name: 'agents', component: () => import('@/views/Agents.vue'), meta: { title: 'Agent 管理' } },
    { path: '/agents/:id/chat', name: 'agent-chat', component: () => import('@/views/AgentChat.vue'), meta: { title: 'AI 对话' } },
    { path: '/skills', name: 'skills', component: () => import('@/views/Skills.vue'), meta: { title: '技能' } },
    { path: '/mcp', name: 'mcp', component: () => import('@/views/MCP.vue'), meta: { title: 'MCP 服务' } },
    { path: '/llm', name: 'llm', component: () => import('@/views/LLMConfigs.vue'), meta: { title: 'LLM 配置' } },
  ],
})

router.afterEach((to) => {
  document.title = `${to.meta.title || ''} · 业务场景本体智能平台`
  requestAnimationFrame(() => document.getElementById('main-content')?.focus())
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
