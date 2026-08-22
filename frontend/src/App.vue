<template>
  <router-view v-if="route.meta.public" />
  <div v-else class="app-shell" :class="{ 'has-flow': showFlowRail }">
    <a class="skip-link" href="#main-content">跳到主要内容</a>

    <aside id="primary-navigation" class="sidebar" :class="{ 'is-open': mobileNavOpen }" aria-label="平台导航">
      <div class="brand">
        <div class="brand-logo" aria-hidden="true">
          <svg viewBox="0 0 32 32" width="22" height="22">
            <defs>
              <linearGradient id="brand-gradient" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#2cbeb0" />
                <stop offset="1" stop-color="#438be5" />
              </linearGradient>
            </defs>
            <circle cx="10" cy="11" r="3.4" fill="url(#brand-gradient)" />
            <circle cx="22" cy="11" r="3.4" fill="#438be5" />
            <circle cx="16" cy="22" r="3.4" fill="url(#brand-gradient)" />
            <path d="M10 11 L22 11 M10 11 L16 22 M22 11 L16 22" stroke="url(#brand-gradient)" stroke-width="1.8" />
          </svg>
        </div>
        <div class="brand-text">
          <div class="brand-title">本体智能平台</div>
          <div class="brand-sub">ONTOLOGY · OPERATIONS</div>
        </div>
        <button
          ref="mobileNavCloseButton"
          class="sidebar-close"
          type="button"
          aria-label="关闭导航"
          @click="closeMobileNav(true)"
        >
          <el-icon aria-hidden="true"><Close /></el-icon>
        </button>
      </div>

      <div class="mode-switch" role="group" aria-label="工作模式">
        <button
          v-for="mode in workModes"
          :key="mode.value"
          type="button"
          :aria-pressed="workMode === mode.value"
          :class="{ active: workMode === mode.value }"
          @click="setWorkMode(mode.value)"
        >
          <el-icon aria-hidden="true"><component :is="mode.icon" /></el-icon>
          <span>{{ mode.label }}</span>
        </button>
      </div>

      <nav class="side-nav" aria-label="主导航">
        <template v-if="workMode === 'operator'">
          <div class="nav-label">日常运营</div>
          <RouterLink
            v-for="item in operatorPrimaryNav"
            :key="item.key"
            :to="item.to"
            class="nav-item"
            :class="{ active: isNavActive(item.matches) }"
            @click="mobileNavOpen = false"
          >
            <el-icon aria-hidden="true"><component :is="item.icon" /></el-icon>
            <span class="nav-copy"><b>{{ item.label }}</b><small>{{ item.description }}</small></span>
            <span v-if="item.badge" class="nav-badge">{{ item.badge }}</span>
          </RouterLink>

          <div class="nav-label">分析与协作</div>
          <RouterLink
            v-for="item in operatorInsightNav"
            :key="item.key"
            :to="item.to"
            class="nav-item nav-item--compact"
            :class="{ active: isNavActive(item.matches) }"
            @click="mobileNavOpen = false"
          >
            <el-icon aria-hidden="true"><component :is="item.icon" /></el-icon>
            <span class="nav-copy"><b>{{ item.label }}</b></span>
          </RouterLink>
        </template>

        <template v-else>
          <div class="nav-label nav-label--flow">建设流水线 <span>按顺序推进</span></div>
          <RouterLink
            v-for="(item, index) in builderPrimaryNav"
            :key="item.key"
            :to="item.to"
            class="nav-item nav-item--step"
            :class="{ active: isNavActive(item.matches) }"
            @click="mobileNavOpen = false"
          >
            <span class="step-number" aria-hidden="true">{{ index + 1 }}</span>
            <span class="nav-copy"><b>{{ item.label }}</b><small>{{ item.description }}</small></span>
            <el-icon class="nav-chevron" aria-hidden="true"><ArrowRight /></el-icon>
          </RouterLink>

          <div class="nav-label">运行验证</div>
          <RouterLink
            v-for="item in builderValidationNav"
            :key="item.key"
            :to="item.to"
            class="nav-item nav-item--compact"
            :class="{ active: isNavActive(item.matches) }"
            @click="mobileNavOpen = false"
          >
            <el-icon aria-hidden="true"><component :is="item.icon" /></el-icon>
            <span class="nav-copy"><b>{{ item.label }}</b></span>
          </RouterLink>
        </template>

        <template v-if="managementNav.length">
          <div class="nav-label nav-label--button">
            <button type="button" :aria-expanded="managementOpen" aria-controls="management-navigation" @click="managementOpen = !managementOpen">
              <span>治理与系统</span>
              <el-icon aria-hidden="true" :class="{ rotated: managementOpen }"><ArrowDown /></el-icon>
            </button>
          </div>
          <div v-show="managementOpen" id="management-navigation" class="management-nav">
            <RouterLink
              v-for="item in managementNav"
              :key="item.key"
              :to="item.to"
              class="nav-item nav-item--compact"
              :class="{ active: isNavActive(item.matches) }"
              @click="mobileNavOpen = false"
            >
              <el-icon aria-hidden="true"><component :is="item.icon" /></el-icon>
              <span class="nav-copy"><b>{{ item.label }}</b></span>
            </RouterLink>
          </div>
        </template>
      </nav>

      <div class="side-footer">
        <span class="status-dot" aria-hidden="true"></span>
        <span><b>工作区已连接</b><small>{{ workModeLabel }}模式</small></span>
      </div>
    </aside>

    <button
      v-if="mobileNavOpen"
      class="nav-scrim"
      type="button"
      aria-label="关闭导航"
      @click="closeMobileNav(true)"
    ></button>

    <main id="main-content" class="main-area" tabindex="-1">
      <header class="topbar" role="banner">
        <div class="topbar-context">
          <button
            ref="mobileMenuButton"
            class="menu-button"
            type="button"
            :aria-expanded="mobileNavOpen"
            aria-controls="primary-navigation"
            aria-label="打开导航"
            @click="openMobileNav"
          >
            <el-icon aria-hidden="true"><Menu /></el-icon>
          </button>
          <div class="crumb" aria-label="当前位置">
            <span>{{ workModeLabel }}工作台</span>
            <el-icon aria-hidden="true"><ArrowRight /></el-icon>
            <strong>{{ pageTitle }}</strong>
          </div>
          <el-select
            v-if="supportsScenarioContext"
            class="context-switcher"
            :model-value="routeScenarioId()"
            :clearable="!scenarioContextRequired"
            filterable
            aria-label="切换当前业务场景"
            placeholder="全部业务场景"
            @change="changeScenarioContext"
          >
            <template #prefix><el-icon aria-hidden="true"><OfficeBuilding /></el-icon></template>
            <el-option v-for="scenario in scenarioOptions" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
          </el-select>
          <span v-else class="context-chip context-chip--global">
            <el-icon aria-hidden="true"><Grid /></el-icon>
            {{ globalContextLabel }}
          </span>
        </div>
        <div class="top-actions">
          <el-button
            class="theme-button"
            text
            circle
            @click="toggleTheme"
            :title="theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            :aria-pressed="theme === 'dark'"
          >
            <el-icon :size="17" aria-hidden="true"><component :is="theme === 'light' ? 'Moon' : 'Sunny'" /></el-icon>
          </el-button>
          <el-dropdown trigger="click" @command="onUserCommand">
            <button class="user-trigger" type="button" aria-haspopup="menu" :aria-label="`打开用户菜单，当前用户 ${auth.user?.display_name || auth.user?.email || ''}`">
              <span class="user-avatar" aria-hidden="true">{{ initials }}</span>
              <span class="user-name">{{ auth.user?.display_name || auth.user?.email }}</span>
              <el-icon aria-hidden="true"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>{{ auth.user?.email }}</el-dropdown-item>
                <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <div v-if="safeReturnTo" class="return-strip" role="navigation" aria-label="返回来源">
        <button type="button" @click="router.push(safeReturnTo)">
          <el-icon aria-hidden="true"><ArrowLeft /></el-icon>
          返回上一工作区
        </button>
        <span>当前页面保留了来源上下文，完成操作后可原路返回。</span>
      </div>

      <nav v-if="showFlowRail" class="flow-rail" aria-label="建设流程">
        <div class="flow-stage-list">
          <template v-for="(stage, index) in flowStages" :key="stage.key">
            <RouterLink
              :to="stage.to"
              class="flow-stage"
              :class="{ active: index === currentFlowIndex }"
              :aria-current="index === currentFlowIndex ? 'step' : undefined"
            >
              <span>{{ index + 1 }}</span>
              <b>{{ stage.shortLabel }}</b>
            </RouterLink>
            <span v-if="index < flowStages.length - 1" class="flow-connector" aria-hidden="true"></span>
          </template>
        </div>
        <RouterLink v-if="nextFlowStage" :to="nextFlowStage.to" class="flow-next">
          <span><small>下一步</small><b>{{ nextFlowStage.label }}</b></span>
          <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        </RouterLink>
      </nav>

      <div class="route-viewport">
        <router-view :key="`${route.path}|${routeScenarioId()}`" />
      </div>
      <GlobalAssistant :context="assistantContext" />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { Scenario } from '@/types'
import GlobalAssistant from '@/components/GlobalAssistant.vue'

type WorkMode = 'operator' | 'builder'
type NavTarget = string | { name: string; params?: Record<string, string>; query?: Record<string, string> }
type NavItem = {
  key: string
  label: string
  description?: string
  icon: string
  to: NavTarget
  matches: string[]
  badge?: string
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const storedTheme = localStorage.getItem('ontology-theme')
const storedWorkMode = localStorage.getItem('ontology-work-mode')
const theme = ref<'light' | 'dark'>(storedTheme === 'dark' ? 'dark' : 'light')
const workMode = ref<WorkMode>(storedWorkMode === 'builder' ? 'builder' : 'operator')
const mobileNavOpen = ref(false)
const mobileMenuButton = ref<HTMLButtonElement | null>(null)
const mobileNavCloseButton = ref<HTMLButtonElement | null>(null)
const managementOpen = ref(false)
const scenarioOptions = ref<Scenario[]>([])
const currentScenarioId = ref(localStorage.getItem('ontology-active-scenario') || '')

const openMobileNav = async () => {
  mobileNavOpen.value = true
  await nextTick()
  mobileNavCloseButton.value?.focus()
}

const closeMobileNav = async (restoreFocus = false) => {
  mobileNavOpen.value = false
  if (restoreFocus) {
    await nextTick()
    mobileMenuButton.value?.focus()
  }
}

const handleGlobalKeydown = (event: KeyboardEvent) => {
  if (event.key !== 'Escape' || !mobileNavOpen.value) return
  event.preventDefault()
  void closeMobileNav(true)
}

const workModes = [
  { value: 'operator' as const, label: '运营', icon: 'Monitor' },
  { value: 'builder' as const, label: '构建', icon: 'Tools' },
]

const workModeLabel = computed(() => workMode.value === 'builder' ? '构建' : '运营')
const pageTitle = computed(() => String(route.meta.title || '工作台'))

function scenarioTarget(name: string): NavTarget {
  return currentScenarioId.value
    ? { name, query: { scenario_id: currentScenarioId.value } }
    : { name }
}

const operatorScenarioTarget = computed<NavTarget>(() => currentScenarioId.value
  ? { name: 'scenario-detail', params: { id: currentScenarioId.value }, query: { stage: 'flow' } }
  : { name: 'scenarios' },
)
const builderScenarioTarget = computed<NavTarget>(() => currentScenarioId.value
  ? { name: 'scenario-detail', params: { id: currentScenarioId.value }, query: { stage: 'ontology' } }
  : { name: 'scenarios' },
)

const operatorPrimaryNav = computed<NavItem[]>(() => [
  { key: 'dashboard', label: '今日总览', description: '状态、风险与下一项工作', icon: 'Odometer', to: '/dashboard', matches: ['/dashboard'] },
  { key: 'workspace', label: '业务对象工作台', description: '查询对象、关系、来源与动作', icon: 'OfficeBuilding', to: operatorScenarioTarget.value, matches: ['/scenarios'] },
  { key: 'tasks', label: '待办与审批', description: '优先处理需要人工决定的任务', icon: 'List', to: scenarioTarget('tasks'), matches: ['/tasks'], badge: '待办' },
  { key: 'incidents', label: '异常处置', description: '定位失败、恢复并形成闭环', icon: 'Bell', to: scenarioTarget('incidents'), matches: ['/incidents'] },
])

const operatorInsightNav = computed<NavItem[]>(() => [
  { key: 'agents', label: 'AI 协作空间', icon: 'ChatDotRound', to: scenarioTarget('agents'), matches: ['/agents'] },
  { key: 'lineage', label: '决策与数据血缘', icon: 'Share', to: scenarioTarget('lineage'), matches: ['/lineage'] },
])

const builderPrimaryNav = computed<NavItem[]>(() => [
  { key: 'model', label: '定义场景与本体', description: '目标、对象、关系和业务规则', icon: 'OfficeBuilding', to: builderScenarioTarget.value, matches: ['/scenarios'] },
  { key: 'data', label: '接入并映射数据', description: '连接、预览、转换和质量验证', icon: 'Coin', to: scenarioTarget('data-sources'), matches: ['/data-sources', '/connectors'] },
  { key: 'agent', label: '编排智能与动作', description: 'Agent、模型、工具和工作流', icon: 'Cpu', to: scenarioTarget('agents'), matches: ['/agents', '/skills', '/mcp', '/llm'] },
  { key: 'release', label: '校验评审并发布', description: '差异、审批、环境和回滚', icon: 'SetUp', to: scenarioTarget('releases'), matches: ['/releases'] },
])

const builderValidationNav = computed<NavItem[]>(() => [
  { key: 'tasks', label: '任务与审批验证', icon: 'List', to: scenarioTarget('tasks'), matches: ['/tasks'] },
  { key: 'lineage', label: '端到端血缘验证', icon: 'Share', to: scenarioTarget('lineage'), matches: ['/lineage'] },
])

const managementNav = computed<NavItem[]>(() => {
  if (workMode.value !== 'builder' && !auth.user?.can_manage) return []
  const items: NavItem[] = [
    { key: 'connectors', label: '连接器与环境', icon: 'Connection', to: scenarioTarget('connectors'), matches: ['/connectors'] },
    { key: 'assets', label: '高级数据与模型', icon: 'DataAnalysis', to: scenarioTarget('advanced-assets'), matches: ['/advanced-assets'] },
  ]
  if (auth.user?.can_manage) {
    items.push(
      { key: 'llm', label: '模型路由', icon: 'ChatDotRound', to: '/llm', matches: ['/llm'] },
      { key: 'skills', label: '技能目录', icon: 'MagicStick', to: '/skills', matches: ['/skills'] },
      { key: 'mcp', label: 'MCP 服务', icon: 'Connection', to: '/mcp', matches: ['/mcp'] },
      { key: 'permissions', label: '权限与成员', icon: 'Lock', to: '/permissions', matches: ['/permissions'] },
    )
  }
  return items
})

const flowStages = computed(() => [
  { key: 'model', label: '定义场景与本体', shortLabel: '业务定义', to: builderScenarioTarget.value, matches: ['/scenarios'] },
  { key: 'data', label: '接入并映射数据', shortLabel: '数据映射', to: scenarioTarget('data-sources'), matches: ['/data-sources', '/connectors'] },
  { key: 'agent', label: '编排智能与动作', shortLabel: '智能编排', to: scenarioTarget('agents'), matches: ['/agents', '/skills', '/mcp', '/llm'] },
  { key: 'release', label: '校验评审并发布', shortLabel: '验证发布', to: scenarioTarget('releases'), matches: ['/releases'] },
  { key: 'operate', label: '运营、审批与复盘', shortLabel: '运营复盘', to: scenarioTarget('tasks'), matches: ['/tasks', '/incidents', '/lineage'] },
])

const currentFlowIndex = computed(() => flowStages.value.findIndex((stage) => stage.matches.some((prefix) => route.path.startsWith(prefix))))
const showFlowRail = computed(() => workMode.value === 'builder' && currentFlowIndex.value >= 0)
const nextFlowStage = computed(() => currentFlowIndex.value >= 0 && currentFlowIndex.value < flowStages.value.length - 1
  ? flowStages.value[currentFlowIndex.value + 1]
  : null,
)
const scenarioAwareRouteNames = new Set([
  'scenario-detail', 'data-sources', 'agents', 'agent-chat', 'tasks', 'incidents', 'lineage',
  'releases', 'connectors', 'advanced-assets',
])
const scenarioRequiredRouteNames = new Set([
  'scenario-detail', 'incidents', 'lineage', 'releases', 'connectors', 'advanced-assets',
])
const supportsScenarioContext = computed(() => scenarioAwareRouteNames.has(String(route.name || '')))
const scenarioContextRequired = computed(() => scenarioRequiredRouteNames.has(String(route.name || '')))
const globalContextLabel = computed(() => ['llm', 'skills', 'mcp', 'permissions'].includes(String(route.name || '')) ? '全局资源' : '当前工作区')
const safeReturnTo = computed(() => {
  const raw = route.query.return_to
  const value = Array.isArray(raw) ? String(raw[0] || '') : typeof raw === 'string' ? raw : ''
  if (!value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return ''
  try {
    const target = new URL(value, window.location.origin)
    return target.origin === window.location.origin ? `${target.pathname}${target.search}${target.hash}` : ''
  } catch {
    return ''
  }
})

const assistantContext = computed(() => ({
  page: pageTitle.value,
  path: route.fullPath,
  scenario_id: routeScenarioId(),
  work_mode: workMode.value,
}))
const initials = computed(() => (auth.user?.display_name || auth.user?.email || 'U').slice(0, 1).toUpperCase())

function isNavActive(matches: string[]) {
  return matches.some((prefix) => route.path.startsWith(prefix))
}
function setWorkMode(value: WorkMode) {
  workMode.value = value
  localStorage.setItem('ontology-work-mode', value)
}
function applyTheme() {
  document.documentElement.dataset.theme = theme.value
  localStorage.setItem('ontology-theme', theme.value)
}
function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  applyTheme()
}
function onUserCommand(command: string) {
  if (command === 'logout') auth.logout().then(() => router.replace('/login'))
}
function syncTheme(event: Event) {
  const value = (event as CustomEvent<'light' | 'dark'>).detail
  if (value === 'light' || value === 'dark') theme.value = value
}
function routeScenarioId() {
  if (route.name === 'scenario-detail' && typeof route.params.id === 'string') return route.params.id
  const value = route.query.scenario_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
async function changeScenarioContext(value: string) {
  if (value) {
    currentScenarioId.value = value
    localStorage.setItem('ontology-active-scenario', value)
  } else {
    currentScenarioId.value = ''
    localStorage.removeItem('ontology-active-scenario')
  }
  if (route.name === 'scenario-detail') {
    if (value) {
      await router.push({ name: 'scenario-detail', params: { id: value }, query: { ...route.query, stage: String(route.query.stage || (workMode.value === 'builder' ? 'ontology' : 'flow')) } })
    } else {
      await router.push({ name: 'scenarios' })
    }
    return
  }
  const query = { ...route.query }
  if (value) query.scenario_id = value
  else delete query.scenario_id
  await router.push({ name: String(route.name), params: route.params, query })
}
async function loadScenarioOptions() {
  try {
    scenarioOptions.value = await api.listScenarios()
    if (scenarioContextRequired.value && !routeScenarioId() && scenarioOptions.value.length) {
      const candidate = scenarioOptions.value.some((scenario) => scenario.id === currentScenarioId.value)
        ? currentScenarioId.value
        : scenarioOptions.value[0].id
      currentScenarioId.value = candidate
      localStorage.setItem('ontology-active-scenario', candidate)
      await router.replace({ name: String(route.name), params: route.params, query: { ...route.query, scenario_id: candidate } })
    }
  } catch {
    scenarioOptions.value = []
  }
}
function syncRouteContext() {
  const id = routeScenarioId()
  if (id) {
    currentScenarioId.value = id
    localStorage.setItem('ontology-active-scenario', id)
    if (!scenarioOptions.value.some((scenario) => scenario.id === id)) void loadScenarioOptions()
  } else if (supportsScenarioContext.value && !scenarioContextRequired.value) {
    currentScenarioId.value = ''
    localStorage.removeItem('ontology-active-scenario')
  }
  mobileNavOpen.value = false
  managementOpen.value = managementNav.value.some((item) => isNavActive(item.matches)) || managementOpen.value
}

watch(() => route.fullPath, syncRouteContext, { immediate: true })
onMounted(() => {
  applyTheme()
  window.addEventListener('ontology-theme-change', syncTheme)
  window.addEventListener('keydown', handleGlobalKeydown)
  void auth.initialize().then(loadScenarioOptions)
})
onBeforeUnmount(() => {
  window.removeEventListener('ontology-theme-change', syncTheme)
  window.removeEventListener('keydown', handleGlobalKeydown)
})
</script>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: 252px minmax(0, 1fr);
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
}
.skip-link {
  position: fixed;
  top: 8px;
  left: 8px;
  z-index: 1000;
  transform: translateY(-150%);
  padding: 10px 13px;
  border-radius: 9px;
  background: var(--primary);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  transition: transform var(--dur) var(--ease);
}
.skip-link:focus { transform: translateY(0); }
.sidebar {
  position: relative;
  z-index: 50;
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border-right: 1px solid color-mix(in srgb, var(--sidebar-title) 8%, transparent);
  background: var(--sidebar-bg);
  color: var(--sidebar-text);
}
.sidebar::before {
  position: absolute;
  top: -150px;
  right: -100px;
  width: 300px;
  height: 300px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--sidebar-glow), transparent 70%);
  content: '';
  pointer-events: none;
}
.brand {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 11px;
  min-height: 70px;
  padding: 15px 16px 11px;
}
.brand-logo {
  display: flex;
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  background: var(--brand-bg);
  box-shadow: var(--shadow-sm);
}
.brand-text { min-width: 0; }
.brand-title { color: var(--sidebar-title); font-size: 15px; font-weight: 760; letter-spacing: .1px; }
.brand-sub { margin-top: 2px; color: var(--sidebar-muted); font-size: 8.5px; font-weight: 650; letter-spacing: 1.35px; }
.sidebar-close { display: none; }
.mode-switch {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  margin: 4px 12px 10px;
  padding: 4px;
  border: 1px solid var(--sidebar-border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--sidebar-bg) 82%, #000);
}
.mode-switch button {
  display: inline-flex;
  min-height: 38px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--sidebar-muted);
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: color var(--dur) var(--ease), background var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.mode-switch button.active {
  background: var(--sidebar-active);
  color: var(--sidebar-title);
  box-shadow: 0 1px 3px rgba(0, 0, 0, .18);
}
.side-nav {
  position: relative;
  z-index: 1;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 0 9px 10px;
  scrollbar-gutter: stable;
}
.nav-label {
  padding: 13px 10px 6px;
  color: var(--sidebar-muted);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 1.25px;
}
.nav-label--flow { display: flex; align-items: center; justify-content: space-between; }
.nav-label--flow span { font-size: 9px; font-weight: 600; letter-spacing: 0; opacity: .8; }
.nav-label--button { padding: 9px 0 4px; }
.nav-label--button button {
  display: flex;
  width: 100%;
  min-height: 34px;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: 750;
  letter-spacing: 1.25px;
  cursor: pointer;
}
.nav-label--button .el-icon { transition: transform var(--dur) var(--ease); }
.nav-label--button .el-icon.rotated { transform: rotate(180deg); }
.nav-item {
  display: flex;
  min-height: 52px;
  align-items: center;
  gap: 10px;
  margin: 3px 0;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 11px;
  color: var(--sidebar-text);
  text-decoration: none;
  transition: color var(--dur) var(--ease), background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.nav-item:hover { background: var(--sidebar-hover); color: var(--sidebar-title); }
.nav-item.active {
  border-color: var(--sidebar-border);
  background: var(--sidebar-active);
  color: var(--sidebar-title);
  box-shadow: var(--shadow-xs);
}
.nav-item > .el-icon { flex: 0 0 18px; font-size: 18px; }
.nav-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 1px; }
.nav-copy b { overflow: hidden; font-size: 12.5px; font-weight: 680; text-overflow: ellipsis; white-space: nowrap; }
.nav-copy small { overflow: hidden; color: var(--sidebar-muted); font-size: 9.5px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.nav-item--compact { min-height: 42px; padding-block: 6px; }
.nav-item--compact .nav-copy b { font-size: 12px; }
.nav-item--step { min-height: 56px; gap: 9px; }
.step-number {
  display: inline-flex;
  flex: 0 0 25px;
  width: 25px;
  height: 25px;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--sidebar-border);
  border-radius: 8px;
  background: var(--sidebar-hover);
  color: var(--sidebar-muted);
  font-size: 10px;
  font-weight: 800;
}
.nav-item--step.active .step-number { border-color: transparent; background: var(--primary); color: #fff; }
.nav-chevron { color: var(--sidebar-muted); font-size: 13px !important; }
.nav-badge {
  flex: 0 0 auto;
  padding: 2px 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--warning) 24%, transparent);
  color: #f8d39b;
  font-size: 9px;
  font-weight: 700;
}
.management-nav { padding-bottom: 4px; }
.side-footer {
  position: relative;
  z-index: 1;
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 9px;
  min-height: 58px;
  padding: 10px 17px;
  border-top: 1px solid var(--sidebar-border);
  background: color-mix(in srgb, var(--sidebar-bg) 92%, #000);
  color: var(--sidebar-muted);
}
.side-footer > span:last-child { display: flex; flex-direction: column; }
.side-footer b { color: var(--sidebar-text); font-size: 10.5px; }
.side-footer small { margin-top: 1px; font-size: 9px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #34d399; box-shadow: 0 0 0 4px rgba(52, 211, 153, .11); }
.nav-scrim { display: none; }
.main-area {
  position: relative;
  height: 100%;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  background: var(--bg-grad);
  scrollbar-gutter: stable;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 40;
  display: flex;
  height: 64px;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--surface) 94%, transparent);
  backdrop-filter: blur(12px);
}
.topbar-context { display: flex; min-width: 0; align-items: center; gap: 11px; }
.menu-button { display: none; }
.crumb { display: flex; min-width: 0; align-items: center; gap: 7px; color: var(--text-3); font-size: 11px; white-space: nowrap; }
.crumb .el-icon { flex: 0 0 auto; font-size: 11px; }
.crumb strong { overflow: hidden; color: var(--text); font-size: 12.5px; font-weight: 700; text-overflow: ellipsis; }
.context-chip,
.context-switcher {
  display: inline-flex;
  min-width: 0;
  max-width: 220px;
  min-height: 30px;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  padding: 4px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 10.5px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.context-switcher { width: min(220px, 28vw); padding: 0; border: 0; background: transparent; overflow: visible; }
.context-switcher :deep(.el-select__wrapper) { min-height: 34px; border-radius: 999px; background: var(--surface-2); box-shadow: 0 0 0 1px var(--border) inset; }
.context-chip--global { color: var(--text-3); }
.context-chip .el-icon { flex: 0 0 auto; color: var(--primary); }
.top-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 9px; }
.theme-button { min-width: 44px; min-height: 44px; color: var(--text-2); }
.theme-button:hover { background: var(--primary-soft); color: var(--primary); }
.user-trigger {
  display: inline-flex;
  min-height: 44px;
  align-items: center;
  gap: 8px;
  padding: 4px 0 4px 5px;
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}
.user-avatar { display: inline-flex; width: 30px; height: 30px; align-items: center; justify-content: center; border-radius: 9px; background: var(--primary-soft); color: var(--primary); font-size: 12px; font-weight: 750; }
.user-name { max-width: 140px; overflow: hidden; font-size: 12px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.user-trigger > .el-icon { color: var(--text-3); font-size: 12px; }
.flow-rail {
  position: sticky;
  top: 64px;
  z-index: 30;
  display: flex;
  min-height: 58px;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 8px 24px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--surface) 96%, transparent);
  box-shadow: 0 4px 14px rgba(33, 52, 64, .04);
  backdrop-filter: blur(12px);
}
.return-strip { display: flex; min-height: 42px; align-items: center; gap: 12px; padding: 6px 24px; border-bottom: 1px solid color-mix(in srgb, var(--primary) 20%, var(--border)); background: var(--primary-soft); color: var(--text-2); font-size: 11px; }
.return-strip button { display: inline-flex; min-height: 32px; align-items: center; gap: 6px; padding: 4px 8px; border: 0; border-radius: 8px; background: transparent; color: var(--primary-600); font: inherit; font-weight: 700; cursor: pointer; }
.return-strip button:hover { background: color-mix(in srgb, var(--surface) 62%, transparent); }
.flow-stage-list { display: flex; min-width: 0; flex: 1; align-items: center; }
.flow-stage { display: inline-flex; flex: 0 1 auto; min-height: 38px; align-items: center; gap: 7px; padding: 5px 7px; border-radius: 9px; color: var(--text-3); text-decoration: none; transition: color var(--dur) var(--ease), background var(--dur) var(--ease); }
.flow-stage:hover { background: var(--surface-2); color: var(--text); }
.flow-stage > span { display: inline-flex; flex: 0 0 23px; width: 23px; height: 23px; align-items: center; justify-content: center; border: 1px solid var(--border-strong); border-radius: 50%; background: var(--surface); font-size: 9px; font-weight: 800; }
.flow-stage b { font-size: 10.5px; font-weight: 700; white-space: nowrap; }
.flow-stage.active { background: var(--primary-soft); color: var(--primary-600); }
.flow-stage.active > span { border-color: var(--primary); background: var(--primary); color: #fff; }
.flow-connector { flex: 1 1 22px; min-width: 8px; max-width: 36px; height: 1px; background: var(--border-strong); }
.flow-next { display: inline-flex; flex: 0 0 auto; min-height: 40px; align-items: center; gap: 9px; padding: 5px 10px 5px 12px; border: 1px solid color-mix(in srgb, var(--primary) 30%, var(--border)); border-radius: 10px; background: var(--surface); color: var(--primary-600); text-decoration: none; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease); }
.flow-next:hover { border-color: var(--primary); background: var(--primary-soft); }
.flow-next span { display: flex; flex-direction: column; }
.flow-next small { color: var(--text-3); font-size: 8.5px; }
.flow-next b { font-size: 10.5px; }
.route-viewport { min-width: 0; }

@media (max-width: 1180px) {
  .app-shell { grid-template-columns: 228px minmax(0, 1fr); }
  .flow-stage b { display: none; }
  .flow-connector { min-width: 6px; }
}
@media (max-width: 900px) {
  .app-shell { display: block; }
  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    width: min(320px, calc(100vw - 48px));
    transform: translateX(-105%);
    visibility: hidden;
    pointer-events: none;
    box-shadow: var(--shadow-lg);
    transition: transform .24s var(--ease), visibility 0s linear .24s;
  }
  .sidebar.is-open {
    transform: translateX(0);
    visibility: visible;
    pointer-events: auto;
    transition-delay: 0s;
  }
  .mode-switch button,
  .nav-label--button button,
  .flow-stage,
  .flow-next,
  .return-strip button { min-height: 44px; }
  .sidebar-close, .menu-button {
    display: inline-flex;
    min-width: 44px;
    min-height: 44px;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }
  .sidebar-close { margin-left: auto; color: var(--sidebar-text); }
  .menu-button { margin-left: -8px; color: var(--text-2); }
  .nav-scrim { position: fixed; inset: 0; z-index: 45; display: block; border: 0; background: rgba(15, 23, 42, .42); backdrop-filter: blur(2px); }
  .topbar { padding: 0 14px; }
  .crumb > span, .crumb > .el-icon, .context-chip, .context-switcher { display: none; }
  .flow-rail { padding-inline: 14px; }
  .return-strip { padding-inline: 14px; }
  .flow-stage-list { flex: 0 0 auto; }
  .flow-stage, .flow-connector { display: none; }
  .flow-stage.active { display: inline-flex; }
  .flow-stage.active b { display: inline; }
  .flow-next { margin-left: auto; }
}
@media (max-width: 560px) {
  .topbar { height: 60px; }
  .flow-rail { top: 60px; min-height: 56px; }
  .user-name, .user-trigger > .el-icon { display: none; }
  .top-actions { gap: 2px; }
  .flow-next small { display: none; }
  .return-strip span { display: none; }
  .flow-next b { max-width: 108px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar, .skip-link, .nav-item, .flow-stage, .flow-next { transition-duration: .01ms !important; }
}
</style>
