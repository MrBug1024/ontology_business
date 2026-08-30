<template>
  <router-view v-if="route.meta.public" />
  <el-container v-else class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>

    <button
      v-if="sidebarOpen"
      class="sidebar-scrim"
      type="button"
      aria-label="关闭导航"
      @click="sidebarOpen = false"
    />

    <el-aside class="sidebar" :class="{ open: sidebarOpen }" width="224px">
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
          <div class="brand-title">本体业务平台</div>
          <div class="brand-sub">ONTOLOGY · AGENT</div>
        </div>
        <el-button class="sidebar-close" text circle aria-label="关闭导航" @click="sidebarOpen = false">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>

      <nav class="side-nav" aria-label="主导航">
        <div class="nav-label">能力生命周期</div>
        <el-menu
          :default-active="activeRoute"
          router
          class="side-menu"
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--sidebar-title)"
          @select="sidebarOpen = false"
        >
          <el-menu-item index="/scenarios"><el-icon aria-hidden="true"><OfficeBuilding /></el-icon><span>场景能力</span></el-menu-item>
          <el-menu-item index="/data-sources"><el-icon aria-hidden="true"><Coin /></el-icon><span>建模资料</span></el-menu-item>
          <el-menu-item index="/agents"><el-icon aria-hidden="true"><Cpu /></el-icon><span>验证中心</span></el-menu-item>
          <el-menu-item index="/access"><el-icon aria-hidden="true"><Connection /></el-icon><span>发布与接入</span></el-menu-item>
        </el-menu>

        <div class="nav-label">运行控制</div>
        <el-menu
          :default-active="activeRoute"
          router
          class="side-menu"
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--sidebar-title)"
          @select="sidebarOpen = false"
        >
          <el-menu-item index="/tasks"><el-icon aria-hidden="true"><List /></el-icon><span>运行治理</span></el-menu-item>
        </el-menu>

        <div class="nav-label">平台管理</div>
        <el-menu
          :default-active="activeRoute"
          router
          class="side-menu"
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--sidebar-title)"
          @select="sidebarOpen = false"
        >
          <el-sub-menu index="settings">
            <template #title><el-icon aria-hidden="true"><Setting /></el-icon><span>平台配置</span></template>
            <el-menu-item index="/templates">产物模板</el-menu-item>
            <el-menu-item index="/llm">大模型</el-menu-item>
            <el-menu-item index="/mcp">外部工具</el-menu-item>
            <el-menu-item index="/skills">本地技能</el-menu-item>
          </el-sub-menu>
        </el-menu>
      </nav>

      <div class="side-footer">
        <span class="status-dot" aria-hidden="true" />
        <span><b>工作区已连接</b><small>业务工作区</small></span>
      </div>
    </el-aside>

    <el-main
      id="main-content"
      class="main-area"
      :class="{ 'assistant-launcher-safe': assistantSafeArea, 'navigation-open': sidebarOpen }"
      tabindex="-1"
    >
      <header class="topbar" role="banner">
        <div class="topbar-leading">
          <el-button class="menu-button" text circle aria-label="打开导航" @click="sidebarOpen = true"><el-icon><Menu /></el-icon></el-button>
          <div class="crumb" aria-label="当前位置"><span>业务工作区</span><el-icon aria-hidden="true"><ArrowRight /></el-icon><strong>{{ pageTitle }}</strong></div>
        </div>
        <div class="top-actions">
          <el-button
            class="theme-button"
            text
            circle
            :title="theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            :aria-pressed="theme === 'dark'"
            @click="toggleTheme"
          ><el-icon :size="17" aria-hidden="true"><component :is="theme === 'light' ? 'Moon' : 'Sunny'" /></el-icon></el-button>
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
      <div class="route-viewport">
        <router-view />
      </div>
      <GlobalAssistant :context="assistantContext" :hide-launcher="sidebarOpen" />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import GlobalAssistant from '@/components/GlobalAssistant.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sidebarOpen = ref(false)
const theme = ref<'light' | 'dark'>((localStorage.getItem('ontology-theme') as 'light' | 'dark') || 'light')

const activeRoute = computed(() => {
  if (route.path.startsWith('/scenarios')) return '/scenarios'
  if (route.path.startsWith('/agents')) return '/agents'
  if (route.path.startsWith('/access')) return '/access'
  return route.path
})
const pageTitle = computed(() => String(route.meta.title || '业务场景'))
const assistantSafeArea = computed(() => !route.path.match(/^\/agents\/[^/]+\/chat(?:\/|$)/)
  && !route.path.match(/^\/scenarios\/[^/]+(?:\/|$)/))
const assistantContext = computed(() => {
  const queryScenario = Array.isArray(route.query.scenario_id)
    ? String(route.query.scenario_id[0] || '')
    : typeof route.query.scenario_id === 'string' ? route.query.scenario_id : ''
  return {
    page: pageTitle.value,
    path: route.fullPath,
    scenario_id: route.path.startsWith('/scenarios/') && typeof route.params.id === 'string'
      ? route.params.id
      : queryScenario,
  }
})
const initials = computed(() => (auth.user?.display_name || auth.user?.email || 'U').slice(0, 1).toUpperCase())

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

watch(() => route.fullPath, () => { sidebarOpen.value = false })
onMounted(() => {
  applyTheme()
  window.addEventListener('ontology-theme-change', syncTheme)
  auth.initialize()
})
onBeforeUnmount(() => window.removeEventListener('ontology-theme-change', syncTheme))
</script>

<style scoped>
.app-shell { height: 100dvh; min-height: 0; overflow: hidden; }
.skip-link { position: fixed; top: 8px; left: 8px; z-index: 2000; transform: translateY(-160%); padding: 9px 12px; border-radius: 9px; background: var(--primary); color: #fff; font-size: 12px; font-weight: 700; transition: transform var(--dur) var(--ease); }
.skip-link:focus { transform: translateY(0); }
.sidebar { position: relative; z-index: 30; display: flex; height: 100%; min-height: 0; flex-direction: column; overflow: hidden; background: var(--sidebar-bg); border-right: 1px solid var(--sidebar-border); }
.sidebar::before { position: absolute; top: -150px; right: -100px; width: 290px; height: 290px; background: radial-gradient(circle, var(--sidebar-glow), transparent 70%); content: ''; pointer-events: none; }
.brand { position: relative; z-index: 1; display: flex; align-items: center; gap: 11px; padding: 20px 17px 17px; }
.brand-logo { display: flex; flex: 0 0 40px; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 12px; background: var(--brand-bg); box-shadow: var(--shadow-sm); }
.brand-title { color: var(--sidebar-title); font-size: 14px; font-weight: 760; letter-spacing: .2px; }
.brand-sub { margin-top: 2px; color: var(--sidebar-muted); font-size: 9px; font-weight: 650; letter-spacing: 1.5px; }
.sidebar-close { display: none; margin-left: auto; color: var(--sidebar-text); }
.side-nav { position: relative; z-index: 1; flex: 1; overflow-y: auto; padding-bottom: 10px; }
.side-nav::-webkit-scrollbar { width: 0; }
.nav-label { padding: 16px 21px 6px; color: var(--sidebar-muted); font-size: 10px; font-weight: 720; letter-spacing: 1.25px; }
.side-menu { border-right: 0; }
.side-menu :deep(.el-menu-item), .side-menu :deep(.el-sub-menu__title) { width: calc(100% - 20px); min-height: 44px; height: 44px; margin: 3px 10px; border-radius: 10px; font-weight: 560; line-height: 44px; transition: color var(--dur) var(--ease), background var(--dur) var(--ease); }
.side-menu :deep(.el-menu-item:hover), .side-menu :deep(.el-sub-menu__title:hover) { background: var(--sidebar-hover); color: var(--sidebar-title); }
.side-menu :deep(.el-menu-item.is-active) { background: var(--sidebar-active); color: var(--sidebar-title); font-weight: 680; box-shadow: var(--shadow-sm); }
.side-menu :deep(.el-sub-menu .el-menu-item) { min-width: 0; padding-left: 48px !important; font-size: 12px; }
.side-footer { position: relative; z-index: 1; display: flex; align-items: center; gap: 9px; margin-top: auto; padding: 14px 17px; border-top: 1px solid var(--sidebar-border); color: var(--sidebar-muted); }
.side-footer > span:last-child { display: flex; flex-direction: column; gap: 2px; }
.side-footer b { color: var(--sidebar-text); font-size: 10.5px; font-weight: 680; }
.side-footer small { font-size: 9px; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: #34d399; box-shadow: 0 0 0 4px rgba(52, 211, 153, .12); }
.main-area { min-width: 0; height: 100%; min-height: 0; padding: 0; overflow-x: hidden; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; background: transparent; }
.main-area.navigation-open { overflow: hidden; }
.route-viewport { position: relative; min-width: 0; }
.main-area.assistant-launcher-safe > .route-viewport { padding-bottom: max(96px, env(safe-area-inset-bottom)); }
.topbar { position: sticky; top: 0; z-index: 20; display: flex; height: 64px; flex: 0 0 auto; align-items: center; justify-content: space-between; padding: 0 26px; border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 94%, transparent); backdrop-filter: blur(12px); }
.topbar-leading, .crumb, .top-actions, .user-trigger { display: flex; align-items: center; }
.topbar-leading { min-width: 0; gap: 8px; }
.crumb { min-width: 0; gap: 7px; color: var(--text-3); font-size: 12px; }
.crumb strong { overflow: hidden; color: var(--text); font-size: 12.5px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.crumb .el-icon { flex: 0 0 auto; font-size: 11px; }
.top-actions { gap: 10px; }
.theme-button, .menu-button { min-width: 44px; min-height: 44px; color: var(--text-2); }
.menu-button { display: none; }
.user-trigger { min-height: 44px; gap: 8px; padding: 4px 0 4px 5px; border: 0; background: transparent; color: var(--text); cursor: pointer; font: inherit; }
.user-avatar { display: inline-flex; width: 30px; height: 30px; align-items: center; justify-content: center; border-radius: 9px; background: var(--primary-soft); color: var(--primary); font-size: 12px; font-weight: 760; }
.user-name { max-width: 150px; overflow: hidden; font-size: 12px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-scrim { display: none; }
@media (max-width: 900px) {
  .main-area { scrollbar-gutter: auto; }
  .sidebar { position: fixed; inset: 0 auto 0 0; width: min(84vw, 280px) !important; visibility: hidden; pointer-events: none; transform: translateX(-105%); transition: transform var(--dur) var(--ease), visibility 0s linear var(--dur); }
  .sidebar.open { visibility: visible; pointer-events: auto; transform: translateX(0); transition-delay: 0s; }
  .sidebar-scrim { position: fixed; inset: 0; z-index: 29; display: block; border: 0; background: rgba(8, 19, 28, .45); }
  .sidebar-close, .menu-button { display: inline-flex; }
  .topbar { height: 60px; padding: 0 12px; }
  .crumb > span, .crumb > .el-icon, .user-name { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar, .skip-link, .side-menu :deep(.el-menu-item), .side-menu :deep(.el-sub-menu__title) { transition-duration: .01ms !important; }
}
</style>
