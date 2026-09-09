<template>
  <div class="chat-layout">
    <!-- 左侧：会话列表 -->
    <div class="chat-side">
      <div class="side-head">
        <el-button text @click="goBack" aria-label="返回验证 Agent 列表" title="返回验证 Agent 列表"><el-icon aria-hidden="true"><ArrowLeft /></el-icon></el-button>
        <div class="agent-title">
          <div class="agent-name">{{ agent?.name }}</div>
          <div class="muted">{{ agent?.scenario_name || '未绑定场景' }}</div>
        </div>
      </div>
      <div class="conv-list">
        <el-button class="new-conv-button" type="primary" :disabled="!agentValidationReady || conversationNavigationLocked" @click="newConv">
          <el-icon aria-hidden="true"><Plus /></el-icon> 新建验证会话
        </el-button>
        <div v-for="c in conversations" :key="c.id" class="conv-item" :class="{ active: curConv?.id === c.id }">
          <button class="conv-open" type="button" :disabled="conversationNavigationLocked" :aria-current="curConv?.id === c.id ? 'page' : undefined" :aria-label="`打开对话：${c.title || '新对话'}`" @click="openConv(c)">
            <el-icon aria-hidden="true"><ChatLineRound /></el-icon>
            <span class="conv-title">{{ c.title || '新对话' }}</span>
          </button>
          <span
            v-if="activeTurnCount(c.id)"
            class="conv-running"
            :aria-label="`${activeTurnCount(c.id)} 个任务处理中`"
            :title="`${activeTurnCount(c.id)} 个任务处理中`"
          >
            <el-icon class="is-loading" aria-hidden="true"><Loading /></el-icon>
            {{ activeTurnCount(c.id) }}
          </span>
          <button class="conv-del" type="button" :disabled="conversationNavigationLocked" :aria-label="`删除对话：${c.title || '新对话'}`" title="删除对话" @click.stop="delConv(c)"><el-icon aria-hidden="true"><Delete /></el-icon></button>
        </div>
        <el-empty v-if="!conversations.length" description="暂无对话" :image-size="50" />
      </div>
    </div>

    <!-- 右侧：对话区 -->
    <div class="chat-main">
      <el-alert
        v-if="agent && !agentValidationReady"
        class="validation-notice"
        type="warning"
        :closable="false"
        show-icon
        title="尚不可开始验证"
        :description="validationMissingText"
      />
      <div class="chat-messages" ref="msgRef">
        <div v-if="conversationLoading && !messages.length" class="empty-chat" role="status" aria-live="polite">
          <div class="empty-icon"><el-icon class="is-loading" :size="40" aria-hidden="true"><Loading /></el-icon></div>
          <div class="empty-title">正在加载对话</div>
        </div>
        <div v-else-if="!messages.length" class="empty-chat">
          <div class="empty-icon"><el-icon :size="40"><ChatDotRound /></el-icon></div>
          <div class="empty-title">{{ agentValidationReady ? `${agent?.name || 'Agent'} 可开始验证` : '等待验证配置' }}</div>
          <div v-if="!agentValidationReady" class="muted">{{ validationMissingText }}</div>
          <div v-if="agentValidationReady" class="suggestions">
            <button class="sug" type="button" v-for="q in suggestions" :key="q" @click="useSuggestion(q)">{{ q }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="m.id || i" class="msg-row" :class="m.role">
          <div class="msg-avatar">
            <el-icon><component :is="m.role === 'user' ? 'User' : 'Cpu'" /></el-icon>
          </div>
          <div class="msg-bubble">
            <PlainMessage v-if="m.content" :content="m.content" />
            <MessageInputAttachments v-if="m.role === 'user'" :snapshot="m.input_snapshot" />
            <!-- 工具调用卡片 -->
            <template v-for="(tc, ti) in m.tool_calls || []" :key="'tc' + ti">
              <AgentCapabilityReceipt
                v-if="capabilityInvocationId(tc) && agent?.id && m.id && receiptOwners.get(capabilityInvocationId(tc)) === m.id"
                :agent-id="agent.id" :message-id="m.id"
                :invocation-id="capabilityInvocationId(tc)"
                :scenario-id="agent.scenario_id || undefined" :streaming="m.streaming"
                @updating="followReceipt"
              />
            </template>
            <!-- 状态提示 -->
            <div v-if="m.status && (m.streaming || canRetryTurn(m))" class="status-line" role="status" aria-live="polite" aria-atomic="true">
              <el-icon v-if="m.streaming" class="is-loading" aria-hidden="true"><Loading /></el-icon>
              {{ m.status }}
            </div>
            <!-- 渠道消息以纯文本呈现。 -->
            <div v-if="canRetryTurn(m) || canCancelTurn(m)" class="turn-actions">
              <el-button v-if="canRetryTurn(m)" size="small" :loading="m.retrying" @click="retryTurn(m)">
                <el-icon aria-hidden="true"><RefreshRight /></el-icon>重试
              </el-button>
              <el-button v-if="canCancelTurn(m)" size="small" type="danger" plain :loading="m.cancelling" @click="stopTurn(m)">
                <el-icon aria-hidden="true"><CircleClose /></el-icon>取消
              </el-button>
            </div>
            <!-- 检索资料来源：由服务端按当前租户和 Agent 已绑定资料库过滤后返回。 -->
            <section v-if="m.citations?.length" class="citation-sources" :aria-labelledby="`citation-title-${i}`">
              <div class="citation-sources-head">
                <div>
                  <h4 :id="`citation-title-${i}`"><el-icon aria-hidden="true"><Document /></el-icon>资料来源 <span>{{ m.citations.length }}</span></h4>
                  <p role="status">本回答检索到 {{ m.citations.length }} 条可追溯资料。</p>
                </div>
              </div>
              <article v-for="citation in m.citations" :key="citation.chunk_id" class="citation-card">
                <div class="citation-card-head">
                  <span class="citation-id">{{ citation.citation_id }}</span>
                  <div class="citation-info">
                    <strong>{{ citation.filename }}</strong>
                    <small>{{ citation.data_source_name }} · 字符 {{ citation.char_start }}–{{ citation.char_end }} · 片段 {{ citationOrdinal(citation) }}</small>
                  </div>
                  <el-button
                    size="small"
                    text
                    type="primary"
                    :aria-label="`查看引用原文：${citation.filename}，字符 ${citation.char_start} 到 ${citation.char_end}`"
                    @click="previewCitation(citation)"
                  ><el-icon aria-hidden="true"><View /></el-icon>查看原文</el-button>
                </div>
                <p class="citation-excerpt">{{ citation.text }}</p>
              </article>
            </section>
            <!-- 附件卡片 -->
            <div class="attach-list" v-if="extractMessageAttachments(m).length">
              <div class="attach-card" v-for="a in extractMessageAttachments(m)" :key="a.id">
                <div class="attach-icon"><el-icon :size="22"><Document /></el-icon></div>
                <div class="attach-info">
                  <button class="attach-name" type="button" :aria-label="`预览附件：${a.filename}`" @click="preview(a)">{{ a.filename }}</button>
                  <div class="muted attach-sub">{{ artifactFormatLabel(a.format) }}<template v-if="a.size"> · {{ formatFileSize(a.size) }}</template></div>
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
      <el-dialog v-model="previewVisible" :title="citationPreview ? `${citationPreview.source === 'snapshot' ? '历史引用快照' : '引用原文'}：${previewFile.filename}` : previewFile.filename" width="720px" top="6vh" destroy-on-close>
        <div v-loading="previewLoading" class="preview-box">
          <template v-if="citationPreview">
            <p v-if="citationPreview.source === 'snapshot'" class="citation-range">
              历史引用快照：这是回答生成时保存的片段，不会按当前文件的旧偏移重新截取。
            </p>
            <p v-else class="citation-range">
              当前文件位置：字符 {{ citationPreview.charStart }}–{{ citationPreview.charEnd }}（原始快照不可用，文件内容可能已变更）。
            </p>
            <pre class="citation-original"><span>{{ citationPreview.prefix }}</span><mark>{{ citationPreview.highlighted }}</mark><span>{{ citationPreview.suffix }}</span></pre>
          </template>
          <SafeMarkdown v-else-if="previewText" :content="previewText" />
          <el-empty v-else-if="!previewLoading" description="暂无可预览内容" />
        </div>
        <template #footer>
          <el-button @click="previewVisible = false">关闭</el-button>
          <el-button type="primary" :disabled="!previewFile.id" @click="download(previewFile)">
            <el-icon><Download /></el-icon>{{ citationPreview?.source === 'snapshot' ? '下载当前文件' : '下载' }}
          </el-button>
        </template>
      </el-dialog>

      <div class="chat-input-area">
        <AgentInvocationComposer
          ref="composerRef"
          :agent-id="agent?.id || ''"
          :conversation-id="curConv?.id || ''"
          :disabled="!agentValidationReady || conversationLoading || currentTurnPending"
          :busy="currentTurnActive && !conversationLoading"
          :placeholder="agentValidationReady ? '描述业务需求，或上传本次处理所需的文件' : validationMissingText"
          :accepted-attachment-kinds="acceptedAttachmentKinds"
          @submit="send"
          @stop="stop"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onBeforeUnmount, onMounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentToolResultStatus } from '@/utils/agentToolResult'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type {
  Agent,
  AgentRuntimeCapability,
  ChatMessage,
  Conversation,
  RagCitation,
} from '@/types'
import AgentInvocationComposer from '@/components/AgentInvocationComposer.vue'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import PlainMessage from '@/components/PlainMessage.vue'
import MessageInputAttachments from '@/components/agent/MessageInputAttachments.vue'
import AgentCapabilityReceipt from '@/components/agent/AgentCapabilityReceipt.vue'
import { capabilityInvocationId } from '@/utils/agentCapabilityReceipt'
import { actionArtifactAttachment } from '@/utils/artifactAttachments'
import type { ArtifactAttachment } from '@/utils/artifactAttachments'
import { normalizeAgentReadiness } from '@/utils/agentReadiness'
import {
  useAgentDurableTurns,
  type AgentTurnViewMessage,
} from '@/composables/useAgentDurableTurns'

const route = useRoute()
const router = useRouter()
const agent = ref<Agent | null>(null)
const runtimeCapabilities = ref<AgentRuntimeCapability[]>([])
const conversations = ref<Conversation[]>([])
const curConv = ref<Conversation | null>(null)
type CitationPreview = {
  charStart: number
  charEnd: number
  prefix: string
  highlighted: string
  suffix: string
  source: 'snapshot' | 'current'
}

const messages = ref<AgentTurnViewMessage[]>([])
const receiptOwners = computed(() => {
  const owners = new Map<string, string>()
  for (const message of messages.value) {
    if (!message.id) continue
    for (const tool of message.tool_calls || []) {
      const invocationId = capabilityInvocationId(tool)
      if (invocationId) owners.set(invocationId, message.id)
    }
  }
  return owners
})
const conversationLoading = ref(false)
const composerRef = ref<InstanceType<typeof AgentInvocationComposer>>()
const msgRef = ref<HTMLElement>()
let viewDisposed = false
let agentLoadRequest = 0
let conversationListRequest = 0
let conversationLoadRequest = 0

const suggestions = [
  '请说明当前场景目标、输入输出与可用能力',
  '根据我接下来提供的需求给出专业分析和建议',
  '检查我接下来提供的材料说明并指出缺失信息',
  '预演一个已配置的业务操作并说明影响',
]

const validationReadiness = computed(() => agent.value
  ? normalizeAgentReadiness(agent.value).validation
  : { ready: false, missing: [] })
const agentValidationReady = computed(() => validationReadiness.value.ready)
const validationMissingText = computed(() => {
  const labels = validationReadiness.value.missing.map((issue) => issue.label)
  return labels.length ? `尚缺：${labels.join('、')}` : '服务端尚未确认验证就绪状态'
})
const acceptedAttachmentKinds = computed(() => [...new Set(
  runtimeCapabilities.value
    .filter((capability) => capability.readiness?.ready)
    .flatMap((capability) => capability.data_ports || [])
    .filter((port) => port.direction !== 'output' && port.allow_override !== false)
    .flatMap((port) => port.binding_kinds || [])
    .filter((kind) => kind === 'dataset_version' || kind === 'asset_version'),
)])

function queryValue(value: unknown) {
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function safeReturnPath(value: unknown) {
  const candidate = queryValue(value).trim()
  if (!candidate.startsWith('/') || candidate.startsWith('//') || candidate.includes('\\')) return ''
  try {
    const url = new URL(candidate, window.location.origin)
    return url.origin === window.location.origin ? `${url.pathname}${url.search}${url.hash}` : ''
  } catch {
    return ''
  }
}
function goBack() {
  const returnTo = safeReturnPath(route.query.return_to)
  if (returnTo) {
    void router.push(returnTo)
    return
  }
  const scenarioId = agent.value?.scenario_id || queryValue(route.query.scenario_id)
  void router.push({ name: 'agents', query: { scenario_id: scenarioId || undefined } })
}

// ── 附件：优先读取工具结果中的结构化 artifact，旧消息再回退到下载链接 ──
const ATTACH_RE = /\/api\/data-sources\/files\/([a-f0-9]{32})\/download/g
function extractAttachments(content: string): ArtifactAttachment[] {
  if (!content) return []
  const seen = new Set<string>()
  const out: ArtifactAttachment[] = []
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
      const nameMatch = before.match(/([^\s\[\]()（）]+\.(?:docx|xlsx|md|markdown|txt|csv|pdf))\s*\]\(\s*$/i)
      filename = nameMatch ? nameMatch[1] : `附件-${id.slice(0, 8)}`
    }
    out.push({ id, filename, format: filename.split('.').pop()?.toLowerCase(), url: `/api/data-sources/files/${id}/download` })
  }
  return out
}

function parsedToolResult(value: unknown): any {
  if (typeof value !== 'string') return value
  try { return JSON.parse(value) } catch { return value }
}

function extractMessageAttachments(message: AgentTurnViewMessage): ArtifactAttachment[] {
  const structured = (message.tool_calls || [])
    .map((tool: any) => actionArtifactAttachment(tool))
    .filter((item): item is ArtifactAttachment => Boolean(item))
  const legacy = extractAttachments(message.content || '')
  const unique = new Map<string, ArtifactAttachment>()
  for (const item of [...structured, ...legacy]) if (!unique.has(item.id)) unique.set(item.id, item)
  return [...unique.values()]
}

function artifactFormatLabel(format?: string) {
  return ({ docx: 'Word 文档', xlsx: 'Excel 工作簿', markdown: 'Markdown', md: 'Markdown' } as Record<string, string>)[String(format || '').toLowerCase()] || '业务附件'
}

function formatFileSize(size = 0) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

const previewVisible = ref(false)
const previewLoading = ref(false)
const previewText = ref('')
const previewFile = ref<ArtifactAttachment>({ id: '', filename: '', url: '' })
const citationPreview = ref<CitationPreview | null>(null)

async function preview(a: ArtifactAttachment) {
  previewFile.value = a
  previewVisible.value = true
  previewLoading.value = true
  previewText.value = ''
  citationPreview.value = null
  try {
    const r: any = await api.fileText(a.id)
    previewText.value = r.text || ''
  } catch (e: any) {
    previewText.value = `预览失败：${e.message}`
  } finally {
    previewLoading.value = false
  }
}

/** 后端偏移按 Python Unicode code point 计算；JS substring 需要 UTF-16 下标。 */
function codePointOffsetToUtf16(text: string, offset: number) {
  const safeOffset = Math.max(0, offset || 0)
  let codePoints = 0
  let utf16Offset = 0
  for (const character of text) {
    if (codePoints >= safeOffset) break
    utf16Offset += character.length
    codePoints += 1
  }
  return utf16Offset
}

function citationPreviewFor(text: string, citation: RagCitation): CitationPreview {
  const charStart = Math.max(0, citation.char_start || 0)
  const charEnd = Math.max(charStart, citation.char_end || charStart)
  const start = codePointOffsetToUtf16(text, charStart)
  const end = codePointOffsetToUtf16(text, charEnd)
  const context = 280
  const prefixStart = Math.max(0, start - context)
  const suffixEnd = Math.min(text.length, Math.max(end, start) + context)
  return {
    charStart,
    charEnd,
    prefix: `${prefixStart ? '…' : ''}${text.slice(prefixStart, start)}`,
    highlighted: text.slice(start, end) || citation.text || '（引用片段当前不可用）',
    suffix: `${text.slice(end, suffixEnd)}${suffixEnd < text.length ? '…' : ''}`,
    source: 'current',
  }
}

async function previewCitation(citation: RagCitation) {
  previewFile.value = {
    id: citation.file_id,
    filename: citation.filename,
    url: `/api/data-sources/files/${citation.file_id}/download`,
  }
  previewVisible.value = true
  previewLoading.value = false
  previewText.value = ''
  // Citations persist their answer-time excerpt. Prefer it over a fresh
  // offset lookup so a document reindex/update cannot silently display a
  // different passage under the historical citation label.
  if (citation.text) {
    citationPreview.value = {
      charStart: citation.char_start,
      charEnd: citation.char_end,
      prefix: '',
      highlighted: citation.text,
      suffix: '',
      source: 'snapshot',
    }
    return
  }
  citationPreview.value = null
  previewLoading.value = true
  try {
    const r: any = await api.fileText(citation.file_id)
    previewText.value = r.text || ''
    citationPreview.value = citationPreviewFor(previewText.value, citation)
  } catch (e: any) {
    previewText.value = `预览失败：${e.message}`
  } finally {
    previewLoading.value = false
  }
}

async function download(a: ArtifactAttachment) {
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

function citationsOf(value: unknown): RagCitation[] {
  if (!Array.isArray(value)) return []
  return value.filter((citation): citation is RagCitation => Boolean(
    citation
      && typeof citation === 'object'
      && typeof (citation as RagCitation).chunk_id === 'string'
      && typeof (citation as RagCitation).file_id === 'string'
      && typeof (citation as RagCitation).char_start === 'number'
      && typeof (citation as RagCitation).char_end === 'number',
  ))
}

function citationOrdinal(citation: RagCitation) {
  const ordinal = Number(citation.chunk_ordinal)
  return Number.isInteger(ordinal) && ordinal >= 0 ? ordinal + 1 : 1
}

function scrollBottom() {
  nextTick(() => {
    if (msgRef.value) msgRef.value.scrollTop = msgRef.value.scrollHeight
  })
}

function followReceipt() {
  const pane = msgRef.value
  if (pane && pane.scrollHeight - pane.scrollTop - pane.clientHeight < 160) scrollBottom()
}

const {
  conversationNavigationLocked,
  currentTurnPending,
  currentTurnActive,
  streaming,
  activeTurnCount,
  recoverActiveTurns,
  recoverConversationTurns,
  resetTurnScope,
  send,
  canRetryTurn,
  retryTurn,
  canCancelTurn,
  stopTurn,
  stop,
} = useAgentDurableTurns({
  agentId: () => agent.value?.id || '',
  currentConversation: curConv,
  messages,
  conversationLoading,
  validationReady: agentValidationReady,
  validationMissingText,
  invalidateConversationLoad: () => { conversationLoadRequest += 1 },
  invalidateConversationList: () => { conversationListRequest += 1 },
  refreshConversations: loadConvs,
  openConversation: openConv,
  clearComposerAfterAccepted: () => composerRef.value?.clearAfterAccepted(),
  normalizeCitations: citationsOf,
  scrollBottom,
})

async function loadAgent() {
  const requestedId = String(route.params.id || '')
  const requestId = ++agentLoadRequest
  const [loadedAgent, loadedCapabilities] = await Promise.all([
    api.getAgent(requestedId),
    api.getAgentRuntimeCapabilities(requestedId),
  ])
  if (viewDisposed || requestId !== agentLoadRequest || String(route.params.id || '') !== requestedId) return
  const agentScenarioId = loadedAgent.scenario_id || ''
  if (agentScenarioId && queryValue(route.query.scenario_id) !== agentScenarioId) {
    await router.replace({
      name: 'agent-chat',
      params: { id: loadedAgent.id || route.params.id },
      query: { ...route.query, scenario_id: agentScenarioId },
    })
    if (viewDisposed || requestId !== agentLoadRequest || String(route.params.id || '') !== requestedId) return
  }
  agent.value = loadedAgent
  runtimeCapabilities.value = loadedCapabilities
  void loadConvs(true)
}

function requestErrorMessage(error: any, fallback: string) {
  const detail = error?.detail ?? error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail.message === 'string' && detail.message.trim()) return detail.message
  return typeof error?.message === 'string' && error.message.trim() ? error.message : fallback
}

async function loadConvs(recoverActive = false): Promise<Conversation[]> {
  const currentAgentId = agent.value?.id
  const requestId = agentLoadRequest
  const listRequest = ++conversationListRequest
  const selectionRequest = conversationLoadRequest
  const selectedConversationId = curConv.value?.id || ''
  if (!currentAgentId) return []
  const loaded = await api.listConversations(currentAgentId)
  if (
    viewDisposed
    || requestId !== agentLoadRequest
    || agent.value?.id !== currentAgentId
  ) return loaded
  if (listRequest === conversationListRequest) conversations.value = loaded
  if (!recoverActive) return loaded
  try {
    const [activeRuns, recentRuns] = await Promise.all([
      api.listAgentTurns(currentAgentId, { activeOnly: true, limit: 100 }),
      api.listAgentTurns(currentAgentId, { limit: 100 }),
    ])
    if (
      viewDisposed
      || requestId !== agentLoadRequest
      || agent.value?.id !== currentAgentId
    ) return loaded
    recoverActiveTurns(activeRuns)
    if (
      selectionRequest !== conversationLoadRequest
      || (curConv.value?.id || '') !== selectedConversationId
    ) return loaded
    const recoveredRun = activeRuns[0] || recentRuns[0]
    const conversation = conversations.value.find((item) => item.id === recoveredRun?.conversation_id)
    if (conversation) void openConv(conversation)
  } catch {
    // Conversation history remains usable when turn recovery is unavailable.
  }
  return loaded
}

async function newConv() {
  if (conversationNavigationLocked.value) return
  conversationLoadRequest += 1
  conversationLoading.value = false
  curConv.value = null
  messages.value = []
}

function messageFromHistory(message: ChatMessage): AgentTurnViewMessage {
  const resultById = new Map((message.tool_results || []).map((result: any) => [result.id, result]))
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    input_snapshot: message.input_snapshot,
    citations: citationsOf(message.citations),
    tool_calls: (message.tool_calls || []).map((toolCall: any) => ({
      ...toolCall,
      args: toolCall.args ?? toolCall.arguments ?? {},
      result: resultById.get(toolCall.id)?.result,
      _open: false,
      status: agentToolResultStatus(resultById.get(toolCall.id)?.result),
    })),
  }
}

async function openConv(c: Conversation, clearMessages = true) {
  const request = ++conversationLoadRequest
  const requestedConversationId = c.id
  const requestedAgentId = agent.value?.id
  curConv.value = c
  if (clearMessages) messages.value = []
  conversationLoading.value = true
  try {
    const loadedMessages = await api.listMessages(requestedConversationId)
    if (
      viewDisposed
      || request !== conversationLoadRequest
      || curConv.value?.id !== requestedConversationId
      || agent.value?.id !== requestedAgentId
    ) return
    messages.value = loadedMessages.map(messageFromHistory)
    scrollBottom()

    try {
      const [recentConversationRuns, activeConversationRuns] = requestedAgentId
        ? await Promise.all([
          api.listAgentTurns(requestedAgentId, {
            conversationId: requestedConversationId,
            limit: 100,
          }),
          api.listAgentTurns(requestedAgentId, {
            conversationId: requestedConversationId,
            activeOnly: true,
            limit: 100,
          }),
        ])
        : [[], []]
      if (
        viewDisposed
        || request !== conversationLoadRequest
        || curConv.value?.id !== requestedConversationId
        || agent.value?.id !== requestedAgentId
      ) return
      recoverConversationTurns([...recentConversationRuns, ...activeConversationRuns])
      scrollBottom()
    } catch (error: unknown) {
      if (
        !viewDisposed
        && request === conversationLoadRequest
        && curConv.value?.id === requestedConversationId
        && agent.value?.id === requestedAgentId
      ) ElMessage.warning(requestErrorMessage(error, '对话已加载，但任务状态恢复失败'))
    }
  } catch (error: unknown) {
    if (
      !viewDisposed
      && request === conversationLoadRequest
      && curConv.value?.id === requestedConversationId
      && agent.value?.id === requestedAgentId
    ) ElMessage.error(requestErrorMessage(error, '对话记录加载失败'))
  } finally {
    if (
      !viewDisposed
      && request === conversationLoadRequest
      && curConv.value?.id === requestedConversationId
      && agent.value?.id === requestedAgentId
    ) conversationLoading.value = false
  }
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

function useSuggestion(text: string) {
  composerRef.value?.submitMessage(text)
}

onMounted(() => {
  document.getElementById('main-content')?.classList.add('agent-chat-active')
  void loadAgent()
})
watch(() => route.params.id, (nextId, previousId) => {
  if (!previousId || nextId === previousId) return
  resetTurnScope()
  conversationLoadRequest += 1
  agent.value = null
  runtimeCapabilities.value = []
  conversations.value = []
  curConv.value = null
  messages.value = []
  void loadAgent()
})
onBeforeUnmount(() => {
  viewDisposed = true
  agentLoadRequest += 1
  conversationLoadRequest += 1
  document.getElementById('main-content')?.classList.remove('agent-chat-active')
})
</script>

<style scoped>
.side-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
}
:global(.main-area.agent-chat-active) { display: flex; height: 100%; min-height: 0; flex-direction: column; overflow: hidden; }
:global(.main-area.agent-chat-active > .topbar), :global(.main-area.agent-chat-active > .flow-rail) { flex: 0 0 auto; }
:global(.main-area.agent-chat-active > .route-viewport) { flex: 1; min-height: 0; }
.chat-layout { height: 100%; min-height: 0; overflow: hidden; }
.chat-side, .chat-main { min-height: 0; overflow: hidden; }
.chat-messages { min-height: 0; overscroll-behavior: contain; }
.validation-notice { flex: 0 0 auto; margin: 12px 34px 0; }
.chat-layout button, .chat-layout :deep(.el-button) { touch-action: manipulation; }
.chat-layout :deep(.el-button) { min-height: 44px; }
.turn-actions { display: flex; margin-top: 10px; }
.side-head :deep(.el-button) { min-width: 44px; }
.agent-title { flex: 1; min-width: 0; }
.agent-name {
  font-weight: 700;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.new-conv-button { width: calc(100% - 20px); margin: 10px; }
.conv-item {
  display: flex;
  align-items: center;
  gap: 2px;
  min-height: 52px;
  padding: 4px 6px 4px 14px;
  font-size: 13px;
  color: var(--text-2);
  border-left: 2px solid transparent;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.conv-item:hover { background: var(--surface-2); }
.conv-item.active {
  background: var(--primary-soft);
  color: var(--primary-600);
  font-weight: 600;
  border-left-color: var(--primary);
}
.conv-open { display: flex; min-width: 0; min-height: 44px; flex: 1; align-items: center; gap: 8px; padding: 0; border: 0; background: transparent; color: inherit; cursor: pointer; font: inherit; text-align: left; }
.conv-open:focus-visible, .conv-del:focus-visible, .sug:focus-visible, .attach-name:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 2px; }
.conv-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-running { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 3px; color: var(--warning); font-size: 12px; font-weight: 600; }
.conv-del { opacity: .72; transition: opacity var(--dur), color var(--dur), background var(--dur); width: 44px; height: 44px; border: 0; border-radius: 9px; background: transparent; color: var(--text-3); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
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
.side-foot-label { flex-basis: 100%; }
.tool-card .head { min-height: 44px; }
.tool-structured-value { padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.agent-action-confirm { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 9px; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--warning) 38%, var(--border)); border-radius: 9px; background: var(--warning-soft); }
.agent-action-confirm > div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.agent-action-confirm strong { color: var(--text); font-size: 12px; }
.agent-action-confirm span { color: var(--text-2); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
.agent-action-confirm :deep(.el-button) { flex: 0 0 auto; }
.agent-confirm-outcome { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 9px; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--success) 34%, var(--border)); border-radius: 9px; background: var(--success-soft); }
.agent-confirm-outcome > div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.agent-confirm-outcome strong { color: var(--text); font-size: 12px; }
.agent-confirm-outcome span { color: var(--text-2); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
.agent-confirm-outcome :deep(.el-button) { flex: 0 0 auto; }
.citation-sources {
  margin-top: 12px;
  padding: 11px;
  border: 1px solid color-mix(in srgb, var(--primary) 25%, var(--border));
  border-radius: 11px;
  background: var(--surface-2);
}
.citation-sources-head { display: flex; align-items: flex-start; margin-bottom: 8px; }
.citation-sources-head h4 {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0;
  color: var(--text);
  font-size: 13px;
}
.citation-sources-head h4 span {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 5px;
  border-radius: 10px;
  background: var(--primary-soft);
  color: var(--primary-600);
  font-size: 11px;
}
.citation-sources-head p { margin: 3px 0 0; color: var(--text-3); font-size: 12px; }
.citation-sources article + article { margin-top: 8px; }
.citation-card {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
}
.citation-card-head { display: flex; align-items: flex-start; gap: 8px; }
.citation-id {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  min-width: 30px;
  min-height: 24px;
  padding: 0 5px;
  border-radius: 6px;
  background: var(--primary-soft);
  color: var(--primary-600);
  font-size: 12px;
  font-weight: 700;
}
.citation-info { flex: 1; min-width: 0; }
.citation-info strong, .citation-info small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.citation-info strong { color: var(--text); font-size: 13px; }
.citation-info small { margin-top: 2px; color: var(--text-3); font-size: 11px; }
.citation-excerpt {
  margin: 8px 0 0;
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.citation-range { margin: 0 0 8px; color: var(--text-3); font-size: 13px; }
.citation-original {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-2);
  background: var(--surface-2);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.citation-original mark { padding: 1px 2px; border-radius: 2px; color: inherit; background: var(--warning-soft); }
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
  display: flex;
  width: 100%;
  min-height: 44px;
  align-items: center;
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
  padding: 4px 2px;
}

@media (max-width: 720px) {
  .chat-layout { flex-direction: column; }
  .chat-side { width: 100%; height: clamp(128px, 34%, 220px); flex: 0 0 clamp(128px, 34%, 220px); }
  .conv-list { min-height: 0; }
  .chat-main { min-height: 0; height: auto; }
  .chat-messages { padding: 18px 14px; }
  .validation-notice { margin: 10px 14px 0; }
  .chat-input-area { padding: 12px 14px 16px; }
  .attach-card { align-items: flex-start; flex-wrap: wrap; }
  .attach-info { min-width: calc(100% - 52px); }
  .attach-actions { width: 100%; justify-content: flex-end; }
  .citation-card-head { flex-wrap: wrap; }
  .citation-info { min-width: calc(100% - 42px); }
  .agent-action-confirm, .agent-confirm-outcome { align-items: stretch; flex-direction: column; }
  .agent-action-confirm :deep(.el-button), .agent-confirm-outcome :deep(.el-button) { width: 100%; }
}
</style>
