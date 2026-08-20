<template>
  <div class="chat-layout">
    <!-- 左侧：会话列表 -->
    <div class="chat-side">
      <div class="side-head">
        <el-button text @click="$router.push('/agents')" aria-label="返回 Agent 列表" title="返回 Agent 列表"><el-icon aria-hidden="true"><ArrowLeft /></el-icon></el-button>
        <div class="agent-title">
          <div class="agent-name">{{ agent?.name }}</div>
          <div class="muted">{{ agent?.scenario_name || '未绑定场景' }}</div>
        </div>
      </div>
      <div class="conv-list">
        <el-button type="primary" size="small" style="width:100%;margin:10px" @click="newConv">
          <el-icon><Plus /></el-icon> 新对话
        </el-button>
        <div v-for="c in conversations" :key="c.id" class="conv-item" :class="{ active: curConv?.id === c.id }" role="button" tabindex="0" :aria-current="curConv?.id === c.id ? 'true' : undefined" :aria-label="`打开对话：${c.title || '新对话'}`" @click="openConv(c)" @keydown.enter.prevent="openConv(c)" @keydown.space.prevent="openConv(c)">
          <el-icon aria-hidden="true"><ChatLineRound /></el-icon>
          <span class="conv-title">{{ c.title || '新对话' }}</span>
          <button class="conv-del" type="button" :aria-label="`删除对话：${c.title || '新对话'}`" title="删除对话" @click.stop="delConv(c)"><el-icon aria-hidden="true"><Delete /></el-icon></button>
        </div>
        <el-empty v-if="!conversations.length" description="暂无对话" :image-size="50" />
      </div>
      <div class="side-foot" v-if="agent">
        <div class="muted">已配置能力</div>
        <el-tag v-for="n in agent.skill_names || []" :key="n" size="small" type="success" effect="plain" style="margin:2px"><el-icon aria-hidden="true"><MagicStick /></el-icon>{{ n }}</el-tag>
        <el-tag v-for="n in agent.mcp_names || []" :key="n" size="small" type="warning" effect="plain" style="margin:2px"><el-icon aria-hidden="true"><Connection /></el-icon>{{ n }}</el-tag>
        <el-tag v-for="n in agent.data_source_names || []" :key="n" size="small" type="info" effect="plain" style="margin:2px"><el-icon aria-hidden="true"><Coin /></el-icon>{{ n }}</el-tag>
      </div>
    </div>

    <!-- 右侧：对话区 -->
    <div class="chat-main">
      <div class="chat-messages" ref="msgRef">
        <div v-if="!messages.length" class="empty-chat">
          <div class="empty-icon"><el-icon :size="40"><ChatDotRound /></el-icon></div>
          <div class="empty-title">{{ agent?.name }} 已就绪</div>
          <div class="muted">基于「{{ agent?.scenario_name || '通用' }}」场景本体，可查询数据、检索文档、调用技能</div>
          <div class="suggestions">
            <button class="sug" type="button" v-for="q in suggestions" :key="q" @click="send(q)">{{ q }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
          <div class="msg-avatar">
            <el-icon><component :is="m.role === 'user' ? 'User' : 'Cpu'" /></el-icon>
          </div>
          <div class="msg-bubble">
            <!-- 工具调用卡片 -->
            <template v-for="(tc, ti) in m.tool_calls || []" :key="'tc' + ti">
              <div class="tool-card" :class="{ open: tc._open }">
                <button class="head" type="button" :aria-expanded="tc._open" @click="tc._open = !tc._open">
                  <el-icon aria-hidden="true"><component :is="tc.status === 'error' ? 'CircleClose' : tc.status === 'done' ? 'CircleCheck' : 'Loading'" /></el-icon>
                  <span>{{ tc.name }}</span>
                  <el-tag v-if="tc.status === 'done'" size="small" type="success">完成</el-tag>
                  <el-tag v-else-if="tc.status === 'error'" size="small" type="danger">失败</el-tag>
                  <el-tag v-else size="small" type="warning">执行中</el-tag>
                  <el-icon style="margin-left:auto" aria-hidden="true"><component :is="tc._open ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
                </button>
                <div class="body">
                  <div class="muted" style="margin-bottom:4px">参数</div>
                  <pre class="code" style="max-height:160px">{{ JSON.stringify(tc.args, null, 2) }}</pre>
                  <div v-if="tc.result !== undefined" class="muted" style="margin:8px 0 4px">结果</div>
                  <pre v-if="tc.result !== undefined" class="code" style="max-height:200px">{{ formatResult(tc.result) }}</pre>
                </div>
              </div>
            </template>
            <!-- 状态提示 -->
            <div v-if="m.status" class="status-line"><el-icon class="is-loading"><Loading /></el-icon> {{ m.status }}</div>
            <!-- 正文（markdown） -->
            <div class="md-body" v-if="m.content" v-html="renderMd(m.content)"></div>
            <!-- 附件卡片 -->
            <div class="attach-list" v-if="extractAttachments(m.content).length">
              <div class="attach-card" v-for="a in extractAttachments(m.content)" :key="a.id">
                <div class="attach-icon"><el-icon :size="22"><Document /></el-icon></div>
                <div class="attach-info">
                  <button class="attach-name" type="button" :aria-label="`预览附件：${a.filename}`" @click="preview(a)">{{ a.filename }}</button>
                  <div class="muted attach-sub">点击预览</div>
                </div>
                <div class="attach-actions">
                  <el-button size="small" text type="primary" @click="preview(a)"><el-icon><View /></el-icon> 预览</el-button>
                  <el-button size="small" text type="primary" @click="download(a)"><el-icon><Download /></el-icon> 下载</el-button>
                </div>
              </div>
            </div>
            <span v-if="m.streaming" class="cursor">▍</span>
          </div>
        </div>
      </div>

      <!-- 附件预览弹窗 -->
      <el-dialog v-model="previewVisible" :title="previewFile.filename" width="720px" top="6vh" destroy-on-close>
        <div v-loading="previewLoading" class="preview-box">
          <div class="md-body" v-if="previewText" v-html="renderMd(previewText)"></div>
          <el-empty v-else-if="!previewLoading" description="暂无可预览内容" />
        </div>
        <template #footer>
          <el-button @click="previewVisible = false">关闭</el-button>
          <el-button type="primary" :disabled="!previewFile.id" @click="download(previewFile)">
            <el-icon><Download /></el-icon> 下载
          </el-button>
        </template>
      </el-dialog>

      <div class="chat-input-area">
        <el-input v-model="input" type="textarea" :rows="2" resize="none"
          placeholder="输入业务问题，例如查询数据、检索文档或执行已配置的业务操作"
          @keydown.enter.exact.prevent="send()" />
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
          <span class="muted">Enter 发送 · Shift+Enter 换行</span>
          <el-button v-if="streaming" type="danger" plain @click="stop">
            <el-icon><VideoPause /></el-icon> 停止
          </el-button>
          <el-button v-else type="primary" :disabled="!input.trim()" @click="send()">
            <el-icon><Promotion /></el-icon> 发送
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import { api, streamChat } from '@/api'
import type { Agent, Conversation } from '@/types'

const route = useRoute()
const agent = ref<Agent | null>(null)
const conversations = ref<Conversation[]>([])
const curConv = ref<Conversation | null>(null)
const messages = ref<any[]>([])
const input = ref('')
const streaming = ref(false)
const msgRef = ref<HTMLElement>()
let ctrl: AbortController | null = null

const suggestions = [
  '请介绍当前业务场景和可用能力',
  '查询当前场景中可用的业务对象',
  '根据已配置规则检查一组业务数据',
  '检索业务文档并给出要点总结',
]

function renderMd(s: string) {
  return marked.parse(s, { breaks: true }) as string
}

// ── 附件：从消息内容中提取 save_deliverable 生成的下载链接 ──
const ATTACH_RE = /\/api\/data-sources\/files\/([a-f0-9]{32})\/download/g
function extractAttachments(content: string): { id: string; filename: string; url: string }[] {
  if (!content) return []
  const seen = new Set<string>()
  const out: { id: string; filename: string; url: string }[] = []
  let m: RegExpExecArray | null
  ATTACH_RE.lastIndex = 0
  while ((m = ATTACH_RE.exec(content))) {
    const id = m[1]
    if (seen.has(id)) continue
    seen.add(id)
    // 从 Markdown 链接文本或 URL 上下文推断文件名
    const linkMatch = content.slice(Math.max(0, m.index - 120), m.index).match(/\[([^\]]+)\]\(\s*$/)
    let filename = linkMatch ? linkMatch[1].replace(/^[📎📄\s]+/, '') : ''
    if (!filename) {
      const before = content.slice(0, m.index)
      const nameMatch = before.match(/([^\s\[\]()（）]+\.md|([^\s\[\]()（）]+\.txt))\s*\]\(\s*$/)
      filename = nameMatch ? nameMatch[1] : `附件-${id.slice(0, 8)}`
    }
    out.push({ id, filename, url: `/api/data-sources/files/${id}/download` })
  }
  return out
}

const previewVisible = ref(false)
const previewLoading = ref(false)
const previewText = ref('')
const previewFile = ref<{ id: string; filename: string; url: string }>({ id: '', filename: '', url: '' })

async function preview(a: { id: string; filename: string; url: string }) {
  previewFile.value = a
  previewVisible.value = true
  previewLoading.value = true
  previewText.value = ''
  try {
    const r: any = await api.fileText(a.id)
    previewText.value = r.text || ''
  } catch (e: any) {
    previewText.value = `预览失败：${e.message}`
  } finally {
    previewLoading.value = false
  }
}

async function download(a: { id: string; filename: string; url: string }) {
  try {
    const resp = await fetch(a.url)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const objUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objUrl
    link.download = a.filename || `附件-${a.id.slice(0, 8)}`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(objUrl)
    ElMessage.success('开始下载')
  } catch (e: any) {
    ElMessage.error('下载失败：' + e.message)
  }
}

function formatResult(r: any) {
  if (typeof r === 'string') return r.length > 2000 ? r.slice(0, 2000) + '…' : r
  return JSON.stringify(r, null, 2)?.slice(0, 2000)
}
function scrollBottom() {
  nextTick(() => {
    if (msgRef.value) msgRef.value.scrollTop = msgRef.value.scrollHeight
  })
}

async function loadAgent() {
  agent.value = await api.getAgent(route.params.id as string)
  loadConvs()
}
async function loadConvs() {
  if (!agent.value) return
  conversations.value = await api.listConversations(agent.value.id!)
}
async function newConv() {
  curConv.value = null
  messages.value = []
}
async function openConv(c: Conversation) {
  curConv.value = c
  messages.value = []
  const msgs: any[] = await api.listMessages(c.id)
  for (const m of msgs) {
    messages.value.push({
      role: m.role,
      content: m.content,
      tool_calls: (m.tool_calls || []).map((t: any) => ({ ...t, _open: false, status: 'done' })),
    })
  }
  scrollBottom()
}
async function delConv(c: Conversation) {
  try {
    await ElMessageBox.confirm('删除该对话？', '确认', { type: 'warning' })
    await api.deleteConversation(c.id)
    if (curConv.value?.id === c.id) newConv()
    await loadConvs()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}

function send(text?: string) {
  const msg = (text ?? input.value).trim()
  if (!msg || streaming.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: msg })
  messages.value.push({ role: 'assistant', content: '', tool_calls: [], streaming: true, status: '正在思考…' })
  const ai = messages.value[messages.value.length - 1]
  streaming.value = true
  const isNewConv = !curConv.value
  scrollBottom()

  ctrl = streamChat(
    agent.value!.id!,
    { message: msg, conversation_id: curConv.value?.id },
    (ev) => handleEvent(ev, ai),
    () => finish(ai, isNewConv),
    (e) => {
      ai.status = ''
      ai.streaming = false
      streaming.value = false
      ElMessage.error('对话出错：' + e.message)
    },
  )
}

function handleEvent(ev: { type: string; data: any }, ai: any) {
  switch (ev.type) {
    case 'status':
      ai.status = ev.data
      break
    case 'tool_call':
      ai.status = ''
      ai.tool_calls.push({ name: ev.data.name, args: ev.data.arguments, status: 'running', _open: true })
      break
    case 'tool_result': {
      const tc = [...ai.tool_calls].reverse().find((t: any) => t.status === 'running')
      if (tc) {
        tc.status = 'done'
        tc.result = ev.data.result
      }
      break
    }
    case 'token':
      ai.status = ''
      ai.content += ev.data
      break
    case 'done':
      break
    case 'error':
      ai.status = ''
      ai.content += `\n\n[错误] ${ev.data}`
      break
  }
  scrollBottom()
}

async function finish(ai: any, isNewConv = false) {
  ai.streaming = false
  ai.status = ''
  streaming.value = false
  ctrl = null
  await loadConvs()
  if (isNewConv && conversations.value.length) {
    curConv.value = conversations.value[0]
  }
  scrollBottom()
}

function stop() {
  ctrl?.abort()
  streaming.value = false
}

onMounted(loadAgent)
</script>

<style scoped>
.side-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
}
.chat-layout { height: calc(100dvh - 68px); min-height: 0; overflow: hidden; }
.agent-title { flex: 1; min-width: 0; }
.agent-name {
  font-weight: 700;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-list { flex: 1; overflow-y: auto; }
.conv-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-2);
  border-left: 2px solid transparent;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.conv-item:hover { background: var(--surface-2); }
.conv-item:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: -2px; }
.conv-item.active {
  background: var(--primary-soft);
  color: var(--primary-600);
  font-weight: 600;
  border-left-color: var(--primary);
}
.conv-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-del { opacity: 0; transition: opacity var(--dur); width: 28px; height: 28px; border: 0; border-radius: 7px; background: transparent; color: var(--text-3); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
.conv-item:hover .conv-del { opacity: 1; }
.conv-del:hover, .conv-del:focus-visible { opacity: 1; color: var(--danger); background: var(--danger-soft); }
.side-foot {
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  max-height: 140px;
  overflow-y: auto;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-content: flex-start;
}
.empty-chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--text-2);
  padding: 24px;
}
.empty-icon {
  width: 76px; height: 76px;
  border-radius: 20px;
  background: var(--grad-soft);
  color: var(--primary-600);
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 16px;
  box-shadow: var(--shadow-sm);
}
.empty-title { font-size: 18px; font-weight: 700; color: var(--text); margin-bottom: 6px; }
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 20px;
  max-width: 560px;
}
.sug {
  font: inherit;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
  min-height: 44px;
  line-height: 1.4;
  transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease), background var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.sug:hover {
  border-color: var(--primary);
  color: var(--primary-600);
  background: var(--primary-soft);
  transform: translateY(-1px);
}
.cursor {
  animation: blink 1s step-end infinite;
  color: var(--primary);
}
@keyframes blink { 50% { opacity: 0; } }
.status-line {
  color: var(--text-3);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.attach-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}
.attach-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.attach-card:hover {
  border-color: var(--primary);
  box-shadow: var(--shadow-sm);
}
.attach-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--primary-soft);
  color: var(--primary-600);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.attach-info {
  flex: 1;
  min-width: 0;
}
.attach-name {
  border: 0;
  padding: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.attach-name:hover {
  color: var(--primary-600);
}
.attach-sub {
  font-size: 12px;
}
.attach-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.preview-box {
  max-height: 68vh;
  overflow-y: auto;
  padding: 4px 2px;
}

@media (max-width: 720px) {
  .chat-layout { height: auto; min-height: calc(100dvh - 68px); flex-direction: column; }
  .chat-side { width: 100%; height: 220px; flex: 0 0 220px; }
  .conv-list { min-height: 0; }
  .chat-main { min-height: 440px; height: calc(100dvh - 288px); }
  .chat-messages { padding: 18px 14px; }
  .chat-input-area { padding: 12px 14px 16px; }
  .attach-card { align-items: flex-start; flex-wrap: wrap; }
  .attach-info { min-width: calc(100% - 52px); }
  .attach-actions { width: 100%; justify-content: flex-end; }
}
</style>
