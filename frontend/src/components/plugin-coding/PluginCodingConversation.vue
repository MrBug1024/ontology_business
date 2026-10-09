<template>
  <div class="conversation-history"><div ref="transcript" class="transcript" aria-label="公开编码对话" tabindex="0" @scroll="trackScroll">
    <div v-if="!workspace.turns.length" class="conversation-intro"><span class="agent-mark"><el-icon aria-hidden="true"><Cpu /></el-icon></span><div><h2>一起构建场景插件</h2><p>需求、修正和编码进度会保存在这个会话中。</p></div></div>
    <article v-for="turn in workspace.turns" :key="turn.id" class="coding-turn">
      <div class="user-message"><span>你 <time>{{ timeLabel(turn.created_at) }}</time></span><p>{{ turn.instruction }}</p></div>
      <div class="agent-message"><div class="agent-heading"><el-icon aria-hidden="true"><Cpu /></el-icon><b>编码助手</b><span class="turn-status">{{ turn.mode === 'discuss' ? '讨论 · ' : '' }}{{ statusLabel(turn.status, turn.mode) }}</span></div>
        <details v-if="turnReceipts(turn.id).length" class="resource-reads"><summary>已读取 {{ turnReceipts(turn.id).length }} 项工具资料</summary><div v-for="(receipt, index) in turnReceipts(turn.id)" :key="`${receipt.tool}:${index}`"><el-icon aria-hidden="true"><CircleCheck /></el-icon><span>{{ receipt.title }}</span><time :datetime="receipt.retrieved_at">{{ timeLabel(receipt.retrieved_at) }}</time></div></details>
        <div v-for="event in turnEvents(turn.id)" :key="event.sequence" class="public-step">
          <button v-if="event.path" type="button" class="file-change" @click="emit('inspect', event.path)"><el-icon aria-hidden="true"><Document /></el-icon><span>{{ event.path }}</span><el-icon aria-hidden="true"><ArrowRight /></el-icon></button>
          <SafeMarkdown v-if="event.message" :content="event.message" />
        </div>
        <p v-if="!turnEvents(turn.id).length" class="quiet">{{ ['queued', 'waiting_upload', 'running'].includes(turn.status) ? '正在等待公开编码进度…' : '本轮已结束。文件和校验结果可在主编辑区审阅。' }}</p>
        <div v-if="['failed', 'cancelled'].includes(turn.status)" class="turn-retry"><p>{{ turn.status === 'failed' ? '本轮未完成，可恢复原需求后再次发送。' : '本轮已停止，可恢复原需求继续。' }}</p><button type="button" title="将本轮需求放回输入框，发送后启动新轮次" @click="emit('retry', turn.instruction, turn.mode === 'discuss' ? 'discuss' : 'generate')">{{ turn.mode === 'discuss' ? '继续讨论' : '重试本轮' }}</button></div>
      </div>
    </article>
    <details v-if="unassigned.length" class="activity"><summary>工作台活动 · {{ unassigned.length }}</summary><p v-for="event in unassigned" :key="event.sequence">{{ event.message }} <button v-if="event.path" type="button" @click="emit('inspect', event.path)">{{ event.path }}</button></p></details>
    <div v-if="workspace.validation.length" class="conversation-result"><el-icon aria-hidden="true"><Warning /></el-icon><div><b>需要完善 {{ workspace.validation.length }} 项</b><p>在验证面板查看缺口，直接输入修正要求继续编码。</p></div><button type="button" @click="emit('checks')">查看</button></div>
    <div v-else-if="workspace.phase === 'ready_for_review'" class="conversation-result"><el-icon aria-hidden="true"><CircleCheck /></el-icon><div><b>候选文件已准备好</b><p>结构校验已通过，请审阅修改后交付。</p></div><button type="button" @click="emit('changes')">审阅</button></div>
  </div><button v-if="!following" class="jump-latest" type="button" @click="jumpToLatest"><el-icon aria-hidden="true"><ArrowDown /></el-icon>回到最新消息</button></div>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowDown, ArrowRight, CircleCheck, Cpu, Document, Warning } from '@element-plus/icons-vue'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import type { CodingWorkspace } from '@/types/pluginCoding'
const props = defineProps<{ workspace: CodingWorkspace }>()
const emit = defineEmits<{ inspect: [path: string]; checks: []; changes: []; retry: [instruction: string, mode: 'generate' | 'discuss'] }>()
const transcript = ref<HTMLElement | null>(null)
const following = ref(true)
const turnIds = computed(() => new Set(props.workspace.turns.map(turn => turn.id)))
const unassigned = computed(() => props.workspace.events.filter(event => !event.run_id || !turnIds.value.has(event.run_id)))
function turnEvents(id: string) { return props.workspace.events.filter(event => event.run_id === id) }
function turnReceipts(id: string) { return (props.workspace.resource_receipts || []).filter(receipt => receipt.run_id === id) }
function trackScroll() { const el = transcript.value; if (el) following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 90 }
function jumpToLatest() { const el = transcript.value; if (el) el.scrollTop = el.scrollHeight; following.value = true }
function timeLabel(value: string) { return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }
function statusLabel(value: string, mode: 'generate' | 'discuss' = 'generate') {
  if (mode === 'discuss' && ['queued', 'waiting_upload'].includes(value)) return '等待分析'
  if (mode === 'discuss' && value === 'running') return '正在分析'
  return ({ queued: '等待编码', waiting_upload: '等待编码', running: '编码中', succeeded: '本轮完成', failed: '本轮未完成', cancelled: '已停止' } as Record<string, string>)[value] || '状态未知'
}
watch(() => [props.workspace.events.length, props.workspace.turns.length, props.workspace.revision], async () => { await nextTick(); if (following.value) jumpToLatest() }, { immediate: true })
</script>
<style scoped>
.conversation-history { position: relative; flex: 1; min-height: 0; }
.transcript { height: 100%; min-height: 0; overflow: auto; padding: 18px 14px 54px; overscroll-behavior: contain; }
.jump-latest { position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; padding: 8px 12px; border: 1px solid var(--border-strong); border-radius: 22px; background: var(--surface); color: var(--text); font-size: 12px; white-space: nowrap; box-shadow: var(--shadow-sm); }
.conversation-intro { display: flex; gap: 10px; align-items: center; margin-bottom: 22px; }
.agent-mark { width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 11px; }
h2 { font-size: 14px; margin: 0 0 4px; font-weight: 600; }
p { margin: 6px 0; line-height: 1.75; overflow-wrap: anywhere; white-space: pre-wrap; }
.conversation-intro p, .quiet { color: var(--studio-muted); font-size: 12px; }
.coding-turn { margin-bottom: 24px; }
.user-message { padding: 10px 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; font-size: 12px; }
.user-message > span { font-size: 12px; font-weight: 600; }
time { font-weight: 400; color: var(--studio-muted); margin-left: 8px; }
.agent-heading { display: flex; align-items: center; gap: 7px; font-size: 12px; margin-bottom: 10px; }
.agent-heading .el-icon { font-size: 18px; }
.turn-status { color: var(--studio-muted); margin-left: auto; font-size: 11px; }
.turn-retry { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; font-size: 12px; }
.turn-retry p { flex: 1; min-width: 150px; margin: 0; color: var(--text-2); }
.turn-retry button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); color: var(--primary); }
.public-step { font-size: 12px; }
.resource-reads { padding: 10px 0; color: var(--text-2); font-size: 12px; }
.resource-reads > div { display: flex; gap: 7px; align-items: center; min-height: 32px; padding-top: 7px; }
.resource-reads > div > span { flex: 1; overflow-wrap: anywhere; }
.resource-reads .el-icon { flex-shrink: 0; color: var(--success); }
.resource-reads time { flex-shrink: 0; margin-left: 0; font-size: 11px; }
.file-change { display: flex; align-items: center; gap: 7px; width: 100%; margin: 9px 0 4px; padding: 8px 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); text-align: left; }
.file-change span { flex: 1; font: 12px ui-monospace, monospace; overflow-wrap: anywhere; }
.file-change:hover { border-color: var(--primary); }
.activity { color: var(--studio-muted); font-size: 12px; border-top: 1px solid var(--border); padding: 16px 0; }
summary { cursor: pointer; }
.activity button { border: 0; background: transparent; text-decoration: underline; }
.conversation-result { display: flex; align-items: flex-start; gap: 8px; padding: 12px 0; font-size: 12px; }
.conversation-result .el-icon { margin-top: 4px; font-size: 18px; }
.conversation-result div { flex: 1; }
.conversation-result p { color: var(--studio-muted); font-size: 12px; }
.conversation-result button { border: 1px solid var(--border); border-radius: 6px; background: var(--surface); padding: 5px 10px; }
button { color: inherit; font: inherit; cursor: pointer; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.transcript:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
</style>
