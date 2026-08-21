<template>
  <router-view v-if="route.meta.public" />
  <el-container v-else class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <div class="brand-logo">
          <svg viewBox="0 0 32 32" width="22" height="22">
            <defs>
              <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#2cbeb0" />
              <stop offset="1" stop-color="#438be5" />
              </linearGradient>
            </defs>
            <circle cx="10" cy="11" r="3.4" fill="url(#lg)" />
            <circle cx="22" cy="11" r="3.4" fill="#438be5" />
            <circle cx="16" cy="22" r="3.4" fill="url(#lg)" />
            <path d="M10 11 L22 11 M10 11 L16 22 M22 11 L16 22" stroke="url(#lg)" stroke-width="1.8" />
          </svg>
        </div>
        <div class="brand-text">
          <div class="brand-title">本体智能平台</div>
          <div class="brand-sub">ONTOLOGY · AI PLATFORM</div>
        </div>
      </div>

      <nav class="side-nav" aria-label="主导航">
        <div class="nav-label">工作台</div>
        <el-menu :default-active="activeRoute" router class="side-menu" background-color="transparent" text-color="var(--sidebar-text)" active-text-color="var(--sidebar-title)">
          <el-menu-item index="/dashboard" title="仪表盘"><el-icon aria-hidden="true"><Odometer /></el-icon><span>仪表盘</span></el-menu-item>
          <el-menu-item index="/tasks" title="任务中心"><el-icon aria-hidden="true"><List /></el-icon><span>任务中心</span></el-menu-item>
          <el-menu-item index="/incidents" title="事件中心"><el-icon aria-hidden="true"><Bell /></el-icon><span>事件中心</span></el-menu-item>
          <el-menu-item index="/lineage" title="端到端血缘"><el-icon aria-hidden="true"><Share /></el-icon><span>端到端血缘</span></el-menu-item>
          <el-menu-item index="/releases" title="发布治理"><el-icon aria-hidden="true"><SetUp /></el-icon><span>发布治理</span></el-menu-item>
          <el-menu-item index="/connectors" title="连接器与环境"><el-icon aria-hidden="true"><Connection /></el-icon><span>连接器与环境</span></el-menu-item>
          <el-menu-item index="/scenarios" title="业务场景"><el-icon aria-hidden="true"><OfficeBuilding /></el-icon><span>业务场景</span></el-menu-item>
          <el-menu-item index="/data-sources" title="数据源"><el-icon aria-hidden="true"><Coin /></el-icon><span>数据源</span></el-menu-item>
        </el-menu>

        <div class="nav-label">智能能力</div>
        <el-menu :default-active="activeRoute" router class="side-menu" background-color="transparent" text-color="var(--sidebar-text)" active-text-color="var(--sidebar-title)">
          <el-menu-item index="/agents" title="Agent 管理"><el-icon aria-hidden="true"><Cpu /></el-icon><span>Agent 管理</span></el-menu-item>
          <el-menu-item index="/skills" title="技能"><el-icon aria-hidden="true"><MagicStick /></el-icon><span>技能</span></el-menu-item>
          <el-menu-item index="/mcp" title="MCP 服务"><el-icon aria-hidden="true"><Connection /></el-icon><span>MCP 服务</span></el-menu-item>
          <el-menu-item index="/llm" title="LLM 配置"><el-icon aria-hidden="true"><ChatDotRound /></el-icon><span>LLM 配置</span></el-menu-item>
        </el-menu>

        <div class="nav-label">组织治理</div>
        <el-menu :default-active="activeRoute" router class="side-menu" background-color="transparent" text-color="var(--sidebar-text)" active-text-color="var(--sidebar-title)">
          <el-menu-item index="/permissions" title="权限与成员"><el-icon aria-hidden="true"><Lock /></el-icon><span>权限与成员</span></el-menu-item>
        </el-menu>
      </nav>

      <div class="side-footer">
        <div class="foot-badge">
          <span class="dot" aria-hidden="true"></span>
          <span>工作区已连接</span>
        </div>
      </div>
    </el-aside>
    <el-main id="main-content" class="main-area" tabindex="-1">
      <header class="topbar" role="banner">
        <div class="crumb"><span>工作台</span><el-icon><ArrowRight /></el-icon><strong>{{ pageTitle }}</strong></div>
        <div class="top-actions">
          <el-button class="theme-button" text circle @click="toggleTheme" :title="theme === 'light' ? '切换深色主题' : '切换浅色主题'" :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'" :aria-pressed="theme === 'dark'"><el-icon :size="17" aria-hidden="true"><component :is="theme === 'light' ? 'Moon' : 'Sunny'" /></el-icon></el-button>
          <el-dropdown trigger="click" @command="onUserCommand">
            <button class="user-trigger" type="button" aria-haspopup="menu" :aria-label="`打开用户菜单，当前用户 ${auth.user?.display_name || auth.user?.email || ''}`"><span class="user-avatar" aria-hidden="true">{{ initials }}</span><span class="user-name">{{ auth.user?.display_name || auth.user?.email }}</span><el-icon aria-hidden="true"><ArrowDown /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu><el-dropdown-item disabled>{{ auth.user?.email }}</el-dropdown-item><el-dropdown-item divided command="logout">退出登录</el-dropdown-item></el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>
      <router-view />
      <GlobalAssistant :context="assistantContext" />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import GlobalAssistant from '@/components/GlobalAssistant.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = ref<'light' | 'dark'>((localStorage.getItem('ontology-theme') as 'light' | 'dark') || 'light')
const activeRoute = computed(() => {
  const p = route.path
  if (p.startsWith('/scenarios')) return '/scenarios'
  if (p.startsWith('/agents')) return '/agents'
  return p
})
const pageTitle = computed(() => String(route.meta.title || '工作台'))
const assistantContext = computed(() => ({
  page: pageTitle.value,
  path: route.fullPath,
  scenario_id: route.path.startsWith('/scenarios/') && typeof route.params.id === 'string' ? route.params.id : '',
}))
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
onMounted(() => {
  applyTheme()
  window.addEventListener('ontology-theme-change', syncTheme)
  auth.initialize()
})
onBeforeUnmount(() => window.removeEventListener('ontology-theme-change', syncTheme))
</script>

<style scoped>
.app-shell { min-height: 100dvh; }
.skip-link {
  position: fixed;
  top: 8px;
  left: 8px;
  z-index: 1000;
  transform: translateY(-150%);
  padding: 9px 12px;
  border-radius: 9px;
  background: var(--primary);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  transition: transform var(--dur) var(--ease);
}
.skip-link:focus { transform: translateY(0); }
.sidebar {
  background: var(--sidebar-bg);
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}
.sidebar::before {
  content: '';
  position: absolute;
  top: -140px; right: -90px;
  width: 280px; height: 280px;
  background: radial-gradient(circle, var(--sidebar-glow), transparent 70%);
  pointer-events: none;
}
.sidebar::after {
  content: '';
  position: absolute;
  bottom: -120px; left: -80px;
  width: 240px; height: 240px;
  background: radial-gradient(circle, var(--sidebar-glow-2), transparent 70%);
  pointer-events: none;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 22px 18px 18px;
  position: relative;
  z-index: 1;
}
.brand-logo {
  width: 40px; height: 40px;
  border-radius: 12px;
  background: var(--brand-bg);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  box-shadow: var(--shadow-sm);
}
.brand-title { color: var(--sidebar-title); font-weight: 760; font-size: 15px; letter-spacing: 0.2px; }
.brand-sub { color: var(--sidebar-muted); font-size: 9.5px; letter-spacing: 1.6px; margin-top: 2px; font-weight: 600; }

.side-nav {
  flex: 1;
  overflow-y: auto;
  position: relative;
  z-index: 1;
  padding-bottom: 8px;
}
.side-nav::-webkit-scrollbar { width: 0; }
.nav-label {
  color: var(--sidebar-muted);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1.4px;
  padding: 16px 22px 6px;
  text-transform: uppercase;
}
.side-menu {
  border-right: none;
  flex: 0 0 auto;
}
.side-menu :deep(.el-menu-item) {
  border-radius: 11px;
  margin: 3px 10px;
  width: calc(100% - 20px);
  min-height: 44px;
  height: 44px;
  line-height: 44px;
  font-weight: 500;
  transition: all var(--dur) var(--ease);
}
.side-menu :deep(.el-menu-item:hover) {
  background: var(--sidebar-hover);
  color: var(--sidebar-title);
}
.side-menu :deep(.el-menu-item.is-active) {
  background: var(--sidebar-active);
  box-shadow: var(--shadow-sm);
  font-weight: 600;
}
.side-footer {
  margin-top: auto;
  padding: 14px 16px;
  border-top: 1px solid var(--sidebar-border);
  position: relative;
  z-index: 1;
}
.foot-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--sidebar-muted);
  font-size: 11px;
  font-weight: 600;
  background: var(--sidebar-hover);
  border: 1px solid var(--sidebar-border);
  border-radius: 10px;
  padding: 9px 12px;
}
.foot-badge .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #34d399;
  box-shadow: 0 0 0 4px rgba(100, 175, 151, .12);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.main-area {
  background: transparent;
  padding: 0;
  overflow-y: auto;
  min-width: 0;
}
.topbar { height: 68px; display: flex; align-items: center; justify-content: space-between; padding: 0 28px; border-bottom: 1px solid var(--border); background: var(--surface); position: sticky; top: 0; z-index: 10; }
.crumb { display: flex; align-items: center; gap: 8px; color: var(--text-3); font-size: 12px; }
.crumb .el-icon { font-size: 12px; }
.crumb strong { color: var(--text); font-weight: 650; font-size: 13px; }
.top-actions { display: flex; align-items: center; gap: 12px; }
.theme-button { color: var(--text-2); min-width: 44px; min-height: 44px; }
.theme-button:hover { color: var(--primary); background: var(--primary-soft); }
.user-trigger { display: inline-flex; align-items: center; gap: 8px; border: 0; background: transparent; color: var(--text); cursor: pointer; min-height: 44px; padding: 4px 0 4px 6px; font: inherit; }
.user-avatar { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border-radius: 9px; background: var(--primary-soft); color: var(--primary); font-size: 12px; font-weight: 750; }
.user-name { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 650; }
.user-trigger > .el-icon { color: var(--text-3); font-size: 12px; }

@media (max-width: 1024px) {
  .app-shell :deep(.el-aside) { width: 200px !important; }
}
@media (max-width: 768px) {
  .app-shell :deep(.el-aside) { width: 64px !important; }
  .brand { justify-content: center; padding: 18px 8px 14px; }
  .brand-text, .nav-label, .foot-badge span:last-child { display: none; }
  .side-menu :deep(.el-menu-item) {
    margin: 3px 8px;
    width: calc(100% - 16px);
    justify-content: center;
  }
  .side-menu :deep(.el-menu-item span) { display: none; }
  .side-menu :deep(.el-menu-item .el-icon) { margin: 0; }
  .foot-badge { justify-content: center; padding: 9px 6px; }
  .topbar { padding: 0 12px; }
  .user-name, .crumb span, .crumb .el-icon { display: none; }
}
</style>
