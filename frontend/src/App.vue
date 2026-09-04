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
        <div class="nav-label">核心流程</div>
        <el-menu
          :default-active="activeRoute"
          router
          class="side-menu"
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--sidebar-title)"
          @select="sidebarOpen = false"
        >
          <el-menu-item index="/scenarios"><el-icon aria-hidden="true"><OfficeBuilding /></el-icon><span>业务场景</span></el-menu-item>
          <el-menu-item index="/data-sources"><el-icon aria-hidden="true"><Coin /></el-icon><span>数据源</span></el-menu-item>
          <el-menu-item index="/templates"><el-icon aria-hidden="true"><Files /></el-icon><span>模板中心</span></el-menu-item>
          <el-menu-item index="/agents"><el-icon aria-hidden="true"><Cpu /></el-icon><span>Agent</span></el-menu-item>
        </el-menu>

        <div class="nav-label">运行</div>
        <el-menu
          :default-active="activeRoute"
          router
          class="side-menu"
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--sidebar-title)"
          @select="sidebarOpen = false"
        >
          <el-menu-item index="/tasks"><el-icon aria-hidden="true"><List /></el-icon><span>任务中心</span></el-menu-item>
        </el-menu>

        <div class="nav-label">系统设置</div>
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
            <template #title><el-icon aria-hidden="true"><Setting /></el-icon><span>能力配置</span></template>
            <el-menu-item index="/llm">大模型</el-menu-item>
            <el-menu-item index="/mcp">外部工具</el-menu-item>
            <el-menu-item index="/skills">本地技能</el-menu-item>
          </el-sub-menu>
          <el-menu-item v-if="auth.user?.can_manage" index="/members"><el-icon aria-hidden="true"><UserFilled /></el-icon><span>成员与权限</span></el-menu-item>
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
          <div class="crumb" aria-label="当前位置"><span>{{ activeWorkspaceName }}</span><el-icon aria-hidden="true"><ArrowRight /></el-icon><strong>{{ pageTitle }}</strong></div>
        </div>
        <div class="top-actions">
          <el-dropdown
            v-if="workspaces.length > 1"
            trigger="click"
            :disabled="Boolean(switchingWorkspaceId)"
            @command="switchWorkspace(String($event))"
          >
            <button
              class="workspace-trigger"
              type="button"
              aria-haspopup="menu"
              :aria-label="`切换工作区，当前为 ${activeWorkspaceName}`"
            >
              <el-icon aria-hidden="true"><OfficeBuilding /></el-icon>
              <span class="workspace-copy"><small>工作区</small><b>{{ activeWorkspaceName }}</b></span>
              <el-icon class="workspace-chevron" aria-hidden="true"><component :is="switchingWorkspaceId ? 'Loading' : 'ArrowDown'" /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu class="workspace-menu">
                <el-dropdown-item
                  v-for="workspace in workspaces"
                  :key="workspace.organization_id"
                  :command="workspace.organization_id"
                  :disabled="workspace.is_active || switchingWorkspaceId === workspace.organization_id"
                >
                  <div class="workspace-option">
                    <span><b>{{ workspace.name || '未命名工作区' }}</b><small>{{ workspace.role_name }}</small></span>
                    <el-icon v-if="workspace.is_active" aria-label="当前工作区"><Check /></el-icon>
                  </div>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-popover
            v-model:visible="invitationPopoverVisible"
            placement="bottom-end"
            :width="420"
            trigger="click"
            popper-class="workspace-invitation-popper"
            @show="loadInvitations"
          >
            <template #reference>
              <el-badge :value="pendingInvitationCount" :hidden="pendingInvitationCount === 0" class="invitation-badge">
                <el-button
                  class="invitation-button"
                  text
                  circle
                  :aria-label="pendingInvitationCount ? `查看 ${pendingInvitationCount} 个工作区邀请` : '查看工作区邀请'"
                  title="工作区邀请"
                ><el-icon aria-hidden="true"><Bell /></el-icon></el-button>
              </el-badge>
            </template>
            <section class="invitation-popover" aria-label="工作区邀请">
              <div class="invitation-popover-heading">
                <div><strong>工作区邀请</strong><span>有效邀请可直接加入，不会影响你的原工作区数据</span></div>
                <el-button text circle :loading="invitationsLoading" aria-label="刷新工作区邀请" title="刷新" @click="loadInvitations"><el-icon aria-hidden="true"><Refresh /></el-icon></el-button>
              </div>
              <div v-if="invitationsLoading && !invitations.length" class="invitation-empty" aria-live="polite">正在加载邀请…</div>
              <div v-else-if="invitationError" class="invitation-error" role="alert">{{ invitationError }}</div>
              <div v-else-if="!invitations.length" class="invitation-empty">当前没有待处理的工作区邀请</div>
              <div v-else class="invitation-list">
                <article v-for="invitation in invitations" :key="invitation.id" class="invitation-item">
                  <div class="invitation-item-copy">
                    <strong>{{ invitation.organization_name || '未命名工作区' }}</strong>
                    <span>{{ invitation.inviter_name }} 邀请你担任{{ invitation.role_name }}</span>
                    <small>有效至 {{ formatInvitationExpiry(invitation.expires_at) }}</small>
                  </div>
                  <div class="invitation-item-actions">
                    <el-button text :disabled="invitationActionId === invitation.id" @click="declineInvitation(invitation)">拒绝</el-button>
                    <el-button type="primary" size="small" :loading="invitationActionId === invitation.id" @click="acceptInvitation(invitation)">同意</el-button>
                  </div>
                </article>
              </div>
            </section>
          </el-popover>

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
                <el-dropdown-item v-if="auth.user?.can_manage" command="members"><el-icon aria-hidden="true"><UserFilled /></el-icon>成员与权限</el-dropdown-item>
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
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { api } from '@/api'
import GlobalAssistant from '@/components/GlobalAssistant.vue'
import type { OrganizationInvitationInboxItem } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sidebarOpen = ref(false)
const theme = ref<'light' | 'dark'>((localStorage.getItem('ontology-theme') as 'light' | 'dark') || 'light')
const invitations = ref<OrganizationInvitationInboxItem[]>([])
const invitationsLoading = ref(false)
const invitationError = ref('')
const invitationActionId = ref<string | null>(null)
const invitationPopoverVisible = ref(false)
const switchingWorkspaceId = ref<string | null>(null)

const activeRoute = computed(() => {
  if (route.path.startsWith('/scenarios')) return '/scenarios'
  if (route.path.startsWith('/agents')) return '/agents'
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
const workspaces = computed(() => auth.user?.workspaces || [])
const activeWorkspaceName = computed(() => auth.user?.active_workspace?.name || '业务工作区')
const pendingInvitationCount = computed(() => auth.user?.pending_invitation_count ?? invitations.value.length)

function applyTheme() {
  document.documentElement.dataset.theme = theme.value
  localStorage.setItem('ontology-theme', theme.value)
}
function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  applyTheme()
}
function onUserCommand(command: string) {
  if (command === 'members') router.push('/members')
  if (command === 'logout') auth.logout().then(() => router.replace('/login'))
}

function detail(error: unknown, fallback: string) {
  const responseDetail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof responseDetail === 'string' && responseDetail) return responseDetail
  return error instanceof Error && error.message ? error.message : fallback
}

function formatInvitationExpiry(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '24 小时内' : date.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

async function loadInvitations() {
  if (!auth.user || invitationsLoading.value) return
  invitationsLoading.value = true
  invitationError.value = ''
  try {
    invitations.value = await api.listMyOrganizationInvitations()
  } catch (error) {
    invitationError.value = detail(error, '工作区邀请加载失败')
  } finally {
    invitationsLoading.value = false
  }
}

async function acceptInvitation(invitation: OrganizationInvitationInboxItem) {
  invitationActionId.value = invitation.id
  invitationError.value = ''
  try {
    const user = await api.acceptMyOrganizationInvitation(invitation.id)
    auth.setUser(user)
    ElMessage.success(`已加入「${invitation.organization_name || '工作区'}」`)
    window.location.assign('/scenarios')
  } catch (error) {
    invitationError.value = detail(error, '接受邀请失败，请稍后重试')
  } finally {
    invitationActionId.value = null
  }
}

async function declineInvitation(invitation: OrganizationInvitationInboxItem) {
  invitationActionId.value = invitation.id
  invitationError.value = ''
  try {
    await api.declineMyOrganizationInvitation(invitation.id)
    invitations.value = invitations.value.filter((item) => item.id !== invitation.id)
    await auth.refresh()
    ElMessage.success('已拒绝工作区邀请')
  } catch (error) {
    invitationError.value = detail(error, '拒绝邀请失败，请稍后重试')
  } finally {
    invitationActionId.value = null
  }
}

async function switchWorkspace(organizationId: string) {
  if (!organizationId || switchingWorkspaceId.value) return
  switchingWorkspaceId.value = organizationId
  try {
    const user = await api.switchOrganizationWorkspace(organizationId)
    auth.setUser(user)
    window.location.assign('/scenarios')
  } catch (error) {
    ElMessage.error(detail(error, '工作区切换失败'))
  } finally {
    switchingWorkspaceId.value = null
  }
}
function syncTheme(event: Event) {
  const value = (event as CustomEvent<'light' | 'dark'>).detail
  if (value === 'light' || value === 'dark') theme.value = value
}

watch(() => route.fullPath, () => { sidebarOpen.value = false })
watch(() => auth.user?.pending_invitation_count, (count) => {
  if (count) loadInvitations()
}, { immediate: true })
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
.topbar-leading, .crumb, .top-actions, .user-trigger, .workspace-trigger { display: flex; align-items: center; }
.topbar-leading { min-width: 0; gap: 8px; }
.crumb { min-width: 0; gap: 7px; color: var(--text-3); font-size: 12px; }
.crumb strong { overflow: hidden; color: var(--text); font-size: 12.5px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.crumb .el-icon { flex: 0 0 auto; font-size: 11px; }
.top-actions { gap: 10px; }
.theme-button, .menu-button, .invitation-button { min-width: 44px; min-height: 44px; color: var(--text-2); }
.menu-button { display: none; }
.workspace-trigger { min-width: 0; max-width: 230px; height: 44px; gap: 8px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer; font: inherit; text-align: left; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease); }
.workspace-trigger:hover { border-color: var(--border-strong); background: var(--primary-soft); }
.workspace-trigger > .el-icon:first-child { flex: 0 0 auto; color: var(--primary); font-size: 16px; }
.workspace-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 1px; }
.workspace-copy small { overflow: hidden; color: var(--text-3); font-size: 9px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.workspace-copy b { overflow: hidden; color: var(--text); font-size: 11px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.workspace-chevron { flex: 0 0 auto; color: var(--text-3); font-size: 12px; }
.invitation-badge :deep(.el-badge__content) { top: 8px; right: 8px; border: 2px solid var(--surface); }
.workspace-option { display: flex; width: 240px; min-width: 0; align-items: center; justify-content: space-between; gap: 14px; }
.workspace-option > span { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.workspace-option b, .workspace-option small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-option b { color: var(--text); font-size: 12px; }
.workspace-option small { color: var(--text-3); font-size: 11px; }
.workspace-option > .el-icon { color: var(--primary); }
.invitation-popover { display: flex; max-height: min(440px, calc(100dvh - 112px)); flex-direction: column; overflow: auto; }
.invitation-popover-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 2px 2px 12px; border-bottom: 1px solid var(--border); }
.invitation-popover-heading > div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.invitation-popover-heading strong { color: var(--text); font-size: 14px; }
.invitation-popover-heading span { color: var(--text-3); font-size: 11px; line-height: 1.45; }
.invitation-empty, .invitation-error { padding: 26px 8px 14px; color: var(--text-3); font-size: 12px; text-align: center; }
.invitation-error { color: var(--danger); }
.invitation-list { display: flex; flex-direction: column; }
.invitation-item { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; padding: 14px 2px; border-bottom: 1px solid var(--border); }
.invitation-item:last-child { border-bottom: 0; }
.invitation-item-copy { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.invitation-item-copy strong, .invitation-item-copy span, .invitation-item-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.invitation-item-copy strong { color: var(--text); font-size: 12px; }
.invitation-item-copy span { color: var(--text-2); font-size: 11px; }
.invitation-item-copy small { color: var(--text-3); font-size: 10.5px; }
.invitation-item-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 3px; }
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
  .crumb > span, .crumb > .el-icon, .user-name, .workspace-copy { display: none; }
  .workspace-trigger { width: 44px; min-width: 44px; justify-content: center; padding: 0; }
  .workspace-chevron { display: none; }
  .workspace-trigger > .el-icon:first-child { font-size: 18px; }
  .workspace-option { width: min(220px, calc(100vw - 72px)); }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar, .skip-link, .side-menu :deep(.el-menu-item), .side-menu :deep(.el-sub-menu__title) { transition-duration: .01ms !important; }
}
</style>
