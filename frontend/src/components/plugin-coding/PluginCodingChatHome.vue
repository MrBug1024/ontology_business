<template>
  <div class="chat-home">
    <header class="chat-head">
      <div class="chat-title"><b>AI 编码</b><small v-if="scenario">{{ scenario.name }}</small><small v-else>在左侧选择业务场景</small></div>
      <el-button v-if="mode === 'new'" text size="small" :disabled="busy" @click="mode = 'history'"><el-icon aria-hidden="true"><ArrowLeft /></el-icon>会话列表</el-button>
      <el-button v-else type="primary" size="small" :disabled="!scenario || busy" aria-label="为当前插件源码新建编码会话" @click="emit('start-new')"><el-icon aria-hidden="true"><Plus /></el-icon>新建任务</el-button>
    </header>
    <div v-if="mode === 'new'" class="chat-composer" aria-label="新建插件编码对话"><slot /></div>
    <nav v-else class="session-list" aria-label="场景插件编码会话">
      <p v-if="!scenario" class="session-hint">点击左侧业务场景，查看该场景下的插件编码会话。</p>
      <template v-else>
        <p v-if="!sessions.length" class="session-hint">{{ explorer.isScenarioLoading(scenario.id) ? '正在读取编码会话…' : '此场景还没有编码会话。' }}<small v-if="!explorer.isScenarioLoading(scenario.id)">点击「新建任务」，描述插件服务谁、要完成什么业务。</small></p>
        <article v-for="session in sessions" :key="session.id" :class="['session-item', { active: isActive(session) }]">
          <button type="button" class="session-main" @click="emit('open-session', session)">
            <span class="session-title">{{ session.title }}</span>
            <small>{{ formatTime(session.created_at) }} · {{ hostLabel(session.host) }}<template v-if="session.frozen"> · 历史冻结</template><template v-else-if="session.active"> · 编码中</template></small>
          </button>
        </article>
        <p v-if="sessions.length" class="history-note">会话只用于拆分上下文，可随时新建，彼此共享同一份插件源码；插件版本只在定版时产生。</p>
      </template>
    </nav>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { ArrowLeft, Plus } from '@element-plus/icons-vue'
import type { PluginProjectExplorer } from '@/composables/usePluginProjectExplorer'
import type { Scenario } from '@/types'
import type { CodingSession } from '@/types/pluginCoding'

const props = defineProps<{ scenario: Scenario | null; sessions: CodingSession[]; explorer: PluginProjectExplorer; activeSessionId?: string; busy?: boolean; initialNew?: boolean }>()
const emit = defineEmits<{ 'start-new': []; 'open-session': [session: CodingSession] }>()
const mode = ref<'history' | 'new'>(props.initialNew ? 'new' : 'history')
function isActive(session: CodingSession) { return !session.frozen && session.id === props.activeSessionId }
function hostLabel(value: string) { return value === 'codex' ? 'OpenAI Codex' : 'Claude Code' }
function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  return sameDay ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}
function openNew() { mode.value = 'new' }
function closeNew() { mode.value = 'history' }
defineExpose({ openNew, closeNew })
</script>
<style scoped>
.chat-home { display: flex; flex-direction: column; min-height: 0; min-width: 0; flex: 1; }
.chat-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--border); flex: 0 0 auto; }
.chat-title { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.chat-title b { font-size: 12px; }
.chat-title small { color: var(--text-2); font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chat-composer { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: auto; }
.session-list { flex: 1; min-height: 0; overflow: auto; padding: 10px 10px 20px; display: flex; flex-direction: column; gap: 6px; }
.session-hint { padding: 14px 8px; font-size: 12px; color: var(--text-2); }
.session-hint small { display: block; margin-top: 6px; color: var(--text-3); line-height: 1.7; }
.session-item { border: 1px solid var(--border); border-radius: var(--radius-xs); background: var(--surface); overflow: hidden; }
.session-item.active { border-color: var(--primary); }
.session-main { display: flex; flex-direction: column; gap: 4px; width: 100%; padding: 10px 12px; border: 0; background: transparent; text-align: left; cursor: pointer; }
.session-main:hover { background: var(--surface-2); }
.session-item.active .session-main { background: var(--primary-soft); }
.session-title { font-size: 12px; color: var(--text); line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.session-main small { color: var(--text-2); font-size: 10px; }
.history-note { margin: 4px 6px 0; font-size: 10px; color: var(--text-3); line-height: 1.7; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
</style>
