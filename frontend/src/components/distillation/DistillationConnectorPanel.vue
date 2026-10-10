<template>
  <section class="connector-panel" aria-label="本机调查连接器">
    <header>
      <strong>本机调查连接器</strong>
      <small>让 AI 调查工具驱动你电脑上的受控浏览器访问业务系统（适用于平台无法直达的内网系统）。连接器只暴露调查浏览器，不暴露文件或命令；令牌仅在服务端保存哈希。</small>
    </header>
    <p v-if="!eligible.length" class="discovery-muted">尚未配置启用浏览器调查的业务系统。</p>
    <article v-for="target in eligible" :key="target.key" class="connector-target">
      <div class="connector-target-head">
        <strong>{{ target.name }}</strong>
        <span :class="['connector-badge', sessionOf(target.key)?.status]">{{ statusLabel(sessionOf(target.key)?.status) }}</span>
      </div>
      <template v-if="command && command.target_key === target.key">
        <p class="discovery-muted">在成员电脑上执行以下命令（令牌只显示这一次，请在有效期内使用）：</p>
        <code class="connector-command">{{ command.command }}</code>
        <div class="distill-actions">
          <el-button size="small" @click="copyCommand">复制命令</el-button>
          <el-button size="small" tag="a" :href="command.script_url" download="investigation_connector.py">下载连接器脚本</el-button>
        </div>
        <p class="discovery-muted">本机需要 Python 3.10+，并先执行：<code>python -m pip install "playwright>=1.40,<2" "websockets>=12,<16" && python -m playwright install chromium</code>。浏览器窗口默认可见，可全程观看 AI 的调查操作。</p>
      </template>
      <template v-else>
        <p class="discovery-muted">{{ sessionHint(target.key) }}</p>
        <div class="distill-actions">
          <el-button size="small" :loading="busy === target.key" :disabled="disabled" @click="generate(target.key)">
            {{ sessionOf(target.key)?.status === 'connected' ? '重新生成命令（将断开当前连接）' : '生成连接命令' }}
          </el-button>
          <el-button v-if="sessionOf(target.key)?.status === 'connected'" size="small" text :disabled="disabled || !!busy" @click="disconnect(target.key)">断开</el-button>
        </div>
      </template>
    </article>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { connectorEligibleTargets, distillationConnectorApi, type DistillationConnectorCommand, type DistillationConnectorSession } from '@/api/distillationConnector'
import type { DistillationTargetSystem } from '@/types/businessDistillation'

const props = defineProps<{ projectId: string; targets: DistillationTargetSystem[]; disabled?: boolean }>()

const sessions = ref<DistillationConnectorSession[]>([])
const command = ref<DistillationConnectorCommand | null>(null)
const error = ref('')
const busy = ref('')
let controller: AbortController | undefined
let pollTimer: number | undefined

const eligible = computed(() => connectorEligibleTargets(props.targets))
const activeStatuses = new Set(['pending', 'connected'])

function sessionOf(targetKey: string): DistillationConnectorSession | undefined {
  return sessions.value.find(item => item.target_key === targetKey && activeStatuses.has(item.status))
}
function statusLabel(status?: string) {
  return ({ pending: '等待连接器', connected: '连接器已连接', revoked: '已断开', expired: '已过期' })[status || ''] || '未连接'
}
function sessionHint(targetKey: string) {
  const session = sessionOf(targetKey)
  if (!session) return '生成一次性命令，在能访问该系统的成员电脑上执行。'
  if (session.status === 'pending') return '命令已生成，等待成员电脑执行并回连平台…'
  const seen = session.last_seen_at ? ` · 最近活跃 ${new Date(session.last_seen_at).toLocaleString()}` : ''
  return `已连接${session.connector_platform ? `（${session.connector_platform}）` : ''}${seen}。调查工具将优先使用该电脑执行浏览器操作。`
}
async function load(showError = true) {
  controller?.abort()
  const current = new AbortController()
  controller = current
  try {
    const result = await distillationConnectorApi.list(props.projectId, current.signal)
    if (!current.signal.aborted) sessions.value = result.sessions
  }
  catch (caught: unknown) {
    if (!current.signal.aborted && showError) error.value = caught instanceof Error ? caught.message : '连接器状态加载失败'
  }
}
async function generate(targetKey: string) {
  if (props.disabled || busy.value) return
  busy.value = targetKey
  error.value = ''
  controller?.abort()
  const current = new AbortController()
  controller = current
  try {
    command.value = await distillationConnectorApi.create(props.projectId, targetKey, current.signal)
    await load(false)
  }
  catch (caught: unknown) {
    if (!current.signal.aborted) error.value = caught instanceof Error ? caught.message : '命令生成失败，请重试'
  }
  finally {
    if (!current.signal.aborted) busy.value = ''
  }
}
async function disconnect(targetKey: string) {
  const session = sessionOf(targetKey)
  if (!session || props.disabled || busy.value) return
  busy.value = targetKey
  error.value = ''
  controller?.abort()
  const current = new AbortController()
  controller = current
  try {
    await distillationConnectorApi.revoke(props.projectId, session.id, current.signal)
    if (!current.signal.aborted) { command.value = null; await load(false) }
  }
  catch (caught: unknown) {
    if (!current.signal.aborted) error.value = caught instanceof Error ? caught.message : '断开失败，请重试'
  }
  finally {
    if (!current.signal.aborted) busy.value = ''
  }
}
async function copyCommand() {
  if (!command.value) return
  try { await navigator.clipboard.writeText(command.value.command) }
  catch { error.value = '复制失败，请手动选择命令文本复制' }
}
function schedulePoll() {
  window.clearTimeout(pollTimer)
  const waiting = sessions.value.some(item => activeStatuses.has(item.status))
  if (!waiting || !props.projectId) return
  pollTimer = window.setTimeout(() => { void load(false).finally(schedulePoll) }, 3000)
}
watch(() => [props.projectId, props.targets], () => { command.value = null; void load().finally(schedulePoll) }, { immediate: true })
onBeforeUnmount(() => { controller?.abort(); window.clearTimeout(pollTimer) })
</script>

<style scoped>
.connector-panel { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
.connector-panel header small { display: block; margin-top: 4px; color: var(--text-secondary, #909399); }
.connector-target { margin: 12px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; }
.connector-target-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.connector-badge { font-size: 12px; padding: 2px 10px; border-radius: 999px; border: 1px solid var(--border); }
.connector-badge.connected { color: var(--el-color-success); border-color: currentColor; }
.connector-badge.pending { color: var(--el-color-warning); border-color: currentColor; }
.connector-command { display: block; margin: 8px 0; padding: 10px 12px; border-radius: 6px; background: var(--surface-muted, #f5f7fa); font-size: 12px; word-break: break-all; white-space: pre-wrap; }
</style>
