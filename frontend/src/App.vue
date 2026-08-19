<template>
  <el-container class="app-shell">
    <el-aside width="240px" class="sidebar">
      <div class="brand">
        <div class="brand-logo">
          <svg viewBox="0 0 32 32" width="22" height="22">
            <defs>
              <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#c4b5fd" />
                <stop offset="1" stop-color="#67e8f9" />
              </linearGradient>
            </defs>
            <circle cx="10" cy="11" r="3.4" fill="url(#lg)" />
            <circle cx="22" cy="11" r="3.4" fill="#fff" />
            <circle cx="16" cy="22" r="3.4" fill="url(#lg)" />
            <path d="M10 11 L22 11 M10 11 L16 22 M22 11 L16 22" stroke="url(#lg)" stroke-width="1.8" />
          </svg>
        </div>
        <div class="brand-text">
          <div class="brand-title">本体智能平台</div>
          <div class="brand-sub">ONTOLOGY · AI PLATFORM</div>
        </div>
      </div>

      <nav class="side-nav">
        <div class="nav-label">工作台</div>
        <el-menu :default-active="activeRoute" router class="side-menu" background-color="transparent" text-color="#a7a3c9" active-text-color="#ffffff">
          <el-menu-item index="/dashboard"><el-icon><Odometer /></el-icon><span>仪表盘</span></el-menu-item>
          <el-menu-item index="/scenarios"><el-icon><OfficeBuilding /></el-icon><span>业务场景</span></el-menu-item>
          <el-menu-item index="/data-sources"><el-icon><Coin /></el-icon><span>数据源</span></el-menu-item>
        </el-menu>

        <div class="nav-label">智能能力</div>
        <el-menu :default-active="activeRoute" router class="side-menu" background-color="transparent" text-color="#a7a3c9" active-text-color="#ffffff">
          <el-menu-item index="/agents"><el-icon><Cpu /></el-icon><span>Agent 管理</span></el-menu-item>
          <el-menu-item index="/skills"><el-icon><MagicStick /></el-icon><span>技能</span></el-menu-item>
          <el-menu-item index="/mcp"><el-icon><Connection /></el-icon><span>MCP 服务</span></el-menu-item>
          <el-menu-item index="/llm"><el-icon><ChatDotRound /></el-icon><span>LLM 配置</span></el-menu-item>
        </el-menu>
      </nav>

      <div class="side-footer">
        <div class="foot-badge">
          <span class="dot"></span>
          <span>平台运行中</span>
        </div>
      </div>
    </el-aside>
    <el-main class="main-area">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const activeRoute = computed(() => {
  const p = route.path
  if (p.startsWith('/scenarios')) return '/scenarios'
  if (p.startsWith('/agents')) return '/agents'
  return p
})
</script>

<style scoped>
.app-shell { height: 100vh; }
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
  background: radial-gradient(circle, rgba(124, 58, 237, 0.38), transparent 70%);
  pointer-events: none;
}
.sidebar::after {
  content: '';
  position: absolute;
  bottom: -120px; left: -80px;
  width: 240px; height: 240px;
  background: radial-gradient(circle, rgba(8, 145, 178, 0.22), transparent 70%);
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
  background: linear-gradient(135deg, #7c3aed, #0891b2);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 8px 20px rgba(124, 58, 237, 0.5);
}
.brand-title { color: #fff; font-weight: 800; font-size: 15px; letter-spacing: 0.2px; }
.brand-sub { color: #6f6a99; font-size: 9.5px; letter-spacing: 1.6px; margin-top: 2px; font-weight: 600; }

.side-nav {
  flex: 1;
  overflow-y: auto;
  position: relative;
  z-index: 1;
  padding-bottom: 8px;
}
.side-nav::-webkit-scrollbar { width: 0; }
.nav-label {
  color: #565187;
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
  height: 42px;
  line-height: 42px;
  font-weight: 500;
  transition: all var(--dur) var(--ease);
}
.side-menu :deep(.el-menu-item:hover) {
  background: rgba(124, 58, 237, 0.16);
  color: #e9e4ff;
}
.side-menu :deep(.el-menu-item.is-active) {
  background: var(--sidebar-active);
  box-shadow: 0 8px 20px rgba(124, 58, 237, 0.42);
  font-weight: 600;
}
.side-footer {
  margin-top: auto;
  padding: 14px 16px;
  border-top: 1px solid rgba(124, 58, 237, 0.18);
  position: relative;
  z-index: 1;
}
.foot-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #9d97c9;
  font-size: 11px;
  font-weight: 600;
  background: rgba(124, 58, 237, 0.12);
  border: 1px solid rgba(124, 58, 237, 0.24);
  border-radius: 10px;
  padding: 9px 12px;
}
.foot-badge .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #34d399;
  box-shadow: 0 0 8px #34d399;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.main-area {
  background: transparent;
  padding: 0;
  overflow-y: auto;
}

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
}
</style>
