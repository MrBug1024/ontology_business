<template>
  <button
    v-if="showLauncher"
    class="assistant-launcher"
    type="button"
    aria-label="打开全局 AI 助手"
    title="打开全局 AI 助手"
    @click="openAssistant"
  >
    <span class="assistant-launcher-icon" aria-hidden="true"><el-icon><MagicStick /></el-icon></span>
    <span>AI 助手</span>
    <span class="assistant-live-dot" aria-hidden="true"></span>
  </button>

  <el-drawer
    v-model="visible"
    direction="rtl"
    size="min(480px, 100vw)"
    :with-header="false"
    append-to-body
    class="assistant-drawer"
    @opened="scrollBottom"
  >
    <div class="assistant-shell">
      <header class="assistant-head">
        <div class="assistant-title-wrap">
          <div class="assistant-avatar" aria-hidden="true"><el-icon :size="19"><MagicStick /></el-icon></div>
          <div>
            <div class="assistant-title">全局 AI 助手 <el-tag size="small" type="success" effect="plain">上下文感知</el-tag></div>
            <div class="assistant-subtitle">协助建模、映射、编排与解释</div>
          </div>
        </div>
        <el-button text circle aria-label="关闭 AI 助手" title="关闭" @click="visible = false">
          <el-icon aria-hidden="true"><Close /></el-icon>
        </el-button>
      </header>

      <div class="assistant-context" aria-label="当前上下文">
        <el-tag size="small" effect="plain"><el-icon aria-hidden="true"><Location /></el-icon>{{ context.page || '工作台' }}</el-tag>
        <el-tag v-if="context.scenario_id" size="small" type="info" effect="plain">当前场景</el-tag>
        <el-tag v-if="selection.label" size="small" type="warning" effect="plain">已选：{{ selection.label }}</el-tag>
        <span class="context-hint">助手只会使用当前可见且有权限的上下文</span>
      </div>

      <div class="assistant-session-bar">
        <div class="session-current" :title="currentThread?.title || '尚未开始新会话'">
          <span class="session-label">当前会话</span>
          <strong>{{ currentThread?.title || '尚未开始新会话' }}</strong>
        </div>
        <div class="session-actions">
          <el-button size="small" plain :type="historyVisible ? 'primary' : 'default'" @click="toggleHistory">
            <el-icon aria-hidden="true"><Clock /></el-icon>{{ historyVisible ? '返回对话' : '会话历史' }}<span v-if="threads.length" class="thread-count">{{ threads.length }}</span>
          </el-button>
          <el-button size="small" type="primary" plain aria-label="新建助手会话" title="新建会话" @click="createNewThread">
            <el-icon aria-hidden="true"><Plus /></el-icon>新建
          </el-button>
        </div>
      </div>

      <section v-if="historyVisible" class="assistant-history" aria-label="当前上下文的会话历史">
        <div class="history-head">
          <div>
            <h3>会话历史</h3>
            <p>仅显示「{{ context.page }}」下的会话，不会混入其他页面或场景。</p>
          </div>
          <el-button text type="primary" @click="createNewThread"><el-icon aria-hidden="true"><Plus /></el-icon>新建会话</el-button>
        </div>
        <div v-if="threadsLoading" class="history-state" role="status"><el-icon class="is-loading"><Loading /></el-icon>正在加载当前上下文的会话…</div>
        <div v-else-if="!threads.length" class="history-state">当前页面还没有历史会话，点击“新建会话”开始。</div>
        <div v-else class="thread-list">
          <div v-for="thread in threads" :key="thread.id" class="thread-item" :class="{ active: thread.id === threadId }">
            <button
              type="button"
              class="thread-select"
              :aria-current="thread.id === threadId ? 'true' : undefined"
              :title="`继续会话：${thread.title}`"
              @click="selectThread(thread)"
            >
              <span class="thread-dot" aria-hidden="true"></span>
              <span class="thread-copy"><strong>{{ thread.title || '新的助手任务' }}</strong><small>{{ formatThreadTime(thread.updated_at || thread.created_at) }}</small></span>
            </button>
            <el-button class="thread-delete" text circle aria-label="删除会话" title="删除会话" @click="deleteThread(thread)">
              <el-icon aria-hidden="true"><Delete /></el-icon>
            </el-button>
          </div>
        </div>
      </section>

      <main v-else ref="messageRef" class="assistant-messages">
        <div v-if="!messages.length" class="assistant-empty">
          <div class="empty-mark" aria-hidden="true"><el-icon :size="28"><ChatDotRound /></el-icon></div>
          <h3>从业务问题开始</h3>
          <p>我会先理解当前页面和场景，再给出可检查的方案。所有修改都会先生成草稿。</p>
          <div class="assistant-suggestions">
            <button v-for="item in starterSuggestions" :key="item" type="button" class="suggestion-chip" @click="send(item)">{{ item }}</button>
          </div>
        </div>

        <article v-for="(message, index) in messages" :key="message.id || index" class="assistant-message" :class="message.role">
          <div v-if="message.role === 'assistant'" class="message-avatar assistant-message-avatar" aria-hidden="true"><el-icon><MagicStick /></el-icon></div>
          <div class="message-content">
            <div v-if="message.role === 'user'" class="message-label">你</div>
            <div v-else class="message-label">平台 AI 助手</div>
            <div v-if="message.role === 'assistant' && message.thinking?.length" class="thinking-summary">
              <button type="button" class="thinking-toggle" :aria-expanded="isThinkingExpanded(message, index)" @click="toggleThinking(message, index)">
                <span class="thinking-toggle-main"><el-icon aria-hidden="true"><Cpu /></el-icon><span>{{ thinkingSummary(message) }}</span></span>
                <span v-if="message.streaming" class="thinking-live" role="status" aria-live="polite">处理中</span>
                <el-icon class="thinking-chevron" :class="{ rotated: isThinkingExpanded(message, index) }" aria-hidden="true"><ArrowDown /></el-icon>
              </button>
              <div v-show="isThinkingExpanded(message, index)" class="thinking-body">
                <div v-for="step in message.thinking" :key="step.id" class="thinking-step" :class="`is-${step.status || 'done'}`">
                  <span class="thinking-step-dot" aria-hidden="true"></span>
                  <div><strong>{{ step.title }}</strong><span>{{ step.detail }}</span></div>
                </div>
                <div class="thinking-note">这里展示的是可审计的处理摘要，不是模型的原始隐藏思考内容。</div>
              </div>
            </div>
            <div class="message-bubble" :class="{ user: message.role === 'user' }">
              <SafeMarkdown v-if="message.role === 'assistant'" :content="message.content" />
              <span v-if="message.role === 'assistant' && message.streaming" class="stream-cursor" aria-hidden="true">▍</span>
              <div v-else-if="message.role !== 'assistant'" class="user-content">{{ message.content }}</div>
            </div>

            <div v-if="proposalOf(message)" class="proposal-card">
              <div class="proposal-head">
                <div>
                  <div class="proposal-title"><el-icon aria-hidden="true"><DocumentChecked /></el-icon>{{ proposalOf(message)?.title }}</div>
                  <div class="proposal-summary">{{ proposalOf(message)?.summary }}</div>
                </div>
                <el-tag size="small" :type="proposalOf(message)?.status === 'applied' ? 'success' : 'warning'" effect="plain">
                  {{ proposalOf(message)?.status === 'applied' ? '已应用' : '待确认' }}
                </el-tag>
              </div>
              <div class="proposal-preview">
                <template v-if="proposalOf(message)?.kind === 'ontology'">
                  <span>实体 {{ proposalOf(message)?.payload?.entities?.length || 0 }}</span>
                  <span>关系 {{ proposalOf(message)?.payload?.relations?.length || 0 }}</span>
                </template>
                <template v-else>
                  <span>节点 {{ proposalOf(message)?.payload?.nodes?.length || 0 }}</span>
                  <span>连线 {{ proposalOf(message)?.payload?.edges?.length || 0 }}</span>
                </template>
                <span v-if="proposalOf(message)?.changes?.length">差异 {{ proposalOf(message)?.changes?.length }}</span>
                <button type="button" class="preview-toggle" @click="toggleProposal(index)">{{ expandedProposal[index] ? '收起详情' : '查看详情' }}</button>
              </div>
              <div v-if="expandedProposal[index] && proposalOf(message)?.changes?.length" class="proposal-changes" aria-label="Change Set 差异">
                <div v-for="(change, changeIndex) in proposalOf(message)?.changes" :key="`${change.resource}-${change.name}-${changeIndex}`" class="proposal-change">
                  <el-tag size="small" effect="plain" :type="proposalOperationType(change.operation)">{{ proposalOperationLabel(change.operation) }}</el-tag>
                  <div class="proposal-change-copy">
                    <strong>{{ proposalResourceLabel(change.resource) }} · {{ change.name }}</strong>
                    <span>{{ change.summary }}</span>
                  </div>
                </div>
              </div>
              <pre v-if="expandedProposal[index]" class="proposal-code">{{ proposalText(proposalOf(message)) }}</pre>
              <div class="proposal-actions">
                <el-button size="small" type="primary" :loading="applyingIndex === index" :disabled="!context.scenario_id || proposalOf(message)?.status === 'applied' || !proposalOf(message)?.proposal_id" @click="applyProposal(message, index)">
                  <el-icon aria-hidden="true"><Check /></el-icon>{{ proposalOf(message)?.status === 'applied' ? '已应用到场景草稿' : '确认并应用变更' }}
                </el-button>
                <span v-if="!context.scenario_id" class="proposal-hint">请先打开业务场景</span>
                <span v-else-if="!proposalOf(message)?.proposal_id" class="proposal-hint">此草稿缺少安全标识，请重新生成</span>
              </div>
            </div>

            <div v-if="sourcesOf(message).length" class="message-sources">
              <span class="sources-label">参考资料</span>
              <el-tag v-for="source in sourcesOf(message)" :key="source.id || source.filename" size="small" effect="plain" type="info">
                <el-icon aria-hidden="true"><Paperclip /></el-icon>{{ source.filename }}
              </el-tag>
            </div>

            <div v-if="message.questions?.length" class="question-list">
              <div v-for="question in message.questions" :key="question.id" class="question-card">
                <b>{{ question.title }}</b>
                <span>{{ question.message }}</span>
                <el-button size="small" text type="primary" @click="send(question.title)">继续说明</el-button>
              </div>
            </div>
          </div>
        </article>

        <div v-if="loading && !hasStreamingAssistant" class="assistant-thinking" role="status">
          <div class="message-avatar assistant-message-avatar" aria-hidden="true"><el-icon class="is-loading"><Loading /></el-icon></div>
          <div><span class="thinking-title">正在理解当前上下文</span><span class="thinking-dots" aria-hidden="true">···</span></div>
        </div>
      </main>

      <footer class="assistant-composer">
        <div v-if="attachments.length" class="attachment-strip" aria-label="待发送附件">
          <div v-for="item in attachments" :key="item.id" class="attachment-chip">
            <el-icon aria-hidden="true"><Document /></el-icon>
            <span>{{ item.filename }}</span>
            <el-tag v-if="item.status === 'parsed'" size="small" type="success">已解析</el-tag>
            <el-tag v-else-if="item.status === 'error'" size="small" type="danger">失败</el-tag>
            <button type="button" :aria-label="`移除附件 ${item.filename}`" title="移除附件" @click="removeAttachment(item)"><el-icon aria-hidden="true"><Close /></el-icon></button>
          </div>
        </div>
        <div class="composer-tools">
          <label class="tool-button" title="添加临时附件">
            <el-icon aria-hidden="true"><Paperclip /></el-icon><span>添加附件</span>
            <input ref="fileInput" type="file" multiple accept=".pdf,.docx,.xlsx,.xls,.pptx,.md,.txt,.csv,.json,.png,.jpg,.jpeg" @change="onFilesPicked" />
          </label>
          <el-button size="small" :type="mode === 'draft' ? 'primary' : 'default'" plain @click="mode = mode === 'draft' ? 'ask' : 'draft'">
            <el-icon aria-hidden="true"><MagicStick /></el-icon>{{ mode === 'draft' ? '草稿模式' : '生成草稿' }}
          </el-button>
          <span class="composer-hint">{{ mode === 'draft' ? '将优先生成可检查的本体或流程草稿' : 'Enter 发送 · Shift+Enter 换行' }}</span>
        </div>
        <div class="composer-input-row">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="12000"
            show-word-limit
            :placeholder="mode === 'draft' ? '描述要建模或编排的业务，例如：根据附件建立供应商管理本体…' : '描述你正在处理的业务问题…'"
            @keydown.enter.exact.prevent="send()"
          />
          <el-button class="send-button" type="primary" :loading="loading" :disabled="!input.trim()" aria-label="发送消息" title="发送" @click="send()">
            <el-icon aria-hidden="true"><Promotion /></el-icon>
          </el-button>
        </div>
      </footer>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, streamAssistantChat } from '@/api'
import type { AssistantAttachment, AssistantMessage, AssistantProposal, AssistantThread, AssistantThought } from '@/types'
import SafeMarkdown from '@/components/SafeMarkdown.vue'

interface AssistantContext {
  page?: string
  path?: string
  scenario_id?: string
}

const props = defineProps<{ context: AssistantContext }>()
const visible = ref(false)
const loading = ref(false)
const input = ref('')
const mode = ref<'ask' | 'draft'>('ask')
const messages = ref<AssistantMessage[]>([])
const attachments = ref<AssistantAttachment[]>([])
const threadId = ref('')
const threads = ref<AssistantThread[]>([])
const historyVisible = ref(false)
const threadsLoading = ref(false)
const messageRef = ref<HTMLElement>()
const fileInput = ref<HTMLInputElement>()
const applyingIndex = ref<number | null>(null)
const expandedProposal = reactive<Record<number, boolean>>({})
const expandedThinking = reactive<Record<string, boolean>>({})
const selection = reactive<{ label: string; kind: string; id: string }>({ label: '', kind: '', id: '' })

const context = computed(() => ({
  page: props.context.page || '工作台',
  path: props.context.path || '',
  scenario_id: props.context.scenario_id || '',
}))
const assistantScopeKey = computed(() => `${context.value.scenario_id || 'global'}|${(context.value.path || '/').split('?', 1)[0] || '/'}`)
const storageKey = computed(() => `ontology-assistant-thread:${encodeURIComponent(assistantScopeKey.value)}`)
const currentThread = computed(() => threads.value.find((thread) => thread.id === threadId.value))
const hasStreamingAssistant = computed(() => messages.value.some((message) => message.role === 'assistant' && message.streaming))
// The Agent chat owns the bottom-right composer controls. A persistent global
// launcher must not cover its primary send action or keyboard focus target.
const showLauncher = computed(() => !/^\/agents\/[^/]+\/chat(?:\/|$|\?)/.test(context.value.path))
const starterSuggestions = computed(() => context.value.scenario_id
  ? ['解释当前业务场景', '根据当前资料生成本体草稿', '把当前业务流程编排为工作流']
  : ['这个平台可以帮我做什么？', '如何开始建立一个业务本体？', '我应该先准备哪些业务资料？'])

function proposalOf(message: AssistantMessage): AssistantProposal | null {
  const proposal = message.proposal as AssistantProposal | undefined
  return proposal && proposal.kind && proposal.payload ? proposal : null
}

function proposalText(proposal: AssistantProposal | null) {
  if (!proposal) return ''
  return JSON.stringify(proposal.payload, null, 2)
}

function proposalOperationLabel(operation: string) {
  return ({ add: '新增', update: '修改', delete: '删除', skip: '跳过' } as Record<string, string>)[operation] || operation
}

function proposalOperationType(operation: string) {
  return ({ add: 'success', update: 'warning', delete: 'danger', skip: 'info' } as Record<string, string>)[operation] || 'info'
}

function proposalResourceLabel(resource: string) {
  return ({ entity: '实体', relation: '关系', workflow: '工作流', workflow_node: '工作流节点', workflow_edge: '工作流连线' } as Record<string, string>)[resource] || resource
}

function sourcesOf(message: AssistantMessage) {
  return message.sources?.length
    ? message.sources
    : (Array.isArray(message.attachments) ? message.attachments : []) as { id?: string; filename: string; status?: string }[]
}

function toggleProposal(index: number) {
  expandedProposal[index] = !expandedProposal[index]
}

function messageKey(message: AssistantMessage, index: number) {
  return message.id || `message-${index}`
}

function isThinkingExpanded(message: AssistantMessage, index: number) {
  return expandedThinking[messageKey(message, index)] ?? Boolean(message.streaming)
}

function toggleThinking(message: AssistantMessage, index: number) {
  const key = messageKey(message, index)
  expandedThinking[key] = !isThinkingExpanded(message, index)
}

function thinkingSummary(message: AssistantMessage) {
  const steps = message.thinking || []
  const running = steps.find((step) => step.status === 'running')
  if (running) return running.title
  const error = steps.find((step) => step.status === 'error')
  if (error) return error.title
  return message.streaming ? '正在处理当前请求' : `已完成 ${steps.length} 个处理步骤`
}

function upsertThinking(message: AssistantMessage, step: AssistantThought, index: number) {
  const thinking = (message.thinking || []) as AssistantThought[]
  const existing = thinking.find((item) => item.id === step.id)
  if (existing) Object.assign(existing, step)
  else thinking.push(step)
  message.thinking = thinking
  const key = messageKey(message, index)
  if (!(key in expandedThinking)) expandedThinking[key] = true
}

function scrollBottom() {
  nextTick(() => {
    if (messageRef.value) messageRef.value.scrollTop = messageRef.value.scrollHeight
  })
}

function apiContext() {
  return {
    scenario_id: context.value.scenario_id || undefined,
    page: context.value.page,
    path: context.value.path,
  }
}

function formatThreadTime(value?: string) {
  if (!value) return '刚刚'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚'
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function welcomeMessage(): AssistantMessage {
  return {
    role: 'assistant',
    content: context.value.scenario_id
      ? `我已进入「${context.value.page}」工作区。你可以让我解释当前本体、根据附件生成模型，或把业务描述编排成工作流。`
      : '我可以协助你设计业务场景、本体模型、数据映射和工作流。打开具体场景后，我还能直接携带当前页面上下文。',
  }
}

async function loadThread(id: string, closeHistory = true) {
  try {
    messages.value = await api.listAssistantMessages(id, apiContext())
    threadId.value = id
    localStorage.setItem(storageKey.value, id)
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
    if (closeHistory) historyVisible.value = false
    scrollBottom()
  } catch (error: any) {
    localStorage.removeItem(storageKey.value)
    threadId.value = ''
    messages.value = []
    ElMessage.error(error.message || '无法加载会话')
  }
}

async function loadThreads() {
  threadsLoading.value = true
  try {
    threads.value = await api.listAssistantThreads(apiContext())
  } catch (error: any) {
    threads.value = []
    ElMessage.error(error.message || '无法加载会话历史')
  } finally {
    threadsLoading.value = false
  }
}

async function loadContext() {
  await loadThreads()
  const saved = localStorage.getItem(storageKey.value) || ''
  const candidate = threads.value.find((thread) => thread.id === saved) || threads.value[0]
  if (candidate) {
    await loadThread(candidate.id, false)
  } else {
    threadId.value = ''
    messages.value = []
  }
  if (!messages.value.length) messages.value = [welcomeMessage()]
  scrollBottom()
}

async function openAssistant() {
  visible.value = true
  await loadContext()
}

async function toggleHistory() {
  historyVisible.value = !historyVisible.value
  if (historyVisible.value) await loadThreads()
}

async function createNewThread() {
  if (loading.value || threadsLoading.value) return
  try {
    const thread = await api.createAssistantThread(apiContext())
    threads.value = [thread, ...threads.value.filter((item) => item.id !== thread.id)]
    threadId.value = thread.id
    localStorage.setItem(storageKey.value, thread.id)
    messages.value = [welcomeMessage()]
    attachments.value = []
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
    historyVisible.value = false
    scrollBottom()
  } catch (error: any) {
    ElMessage.error(error.message || '新建会话失败')
  }
}

async function selectThread(thread: AssistantThread) {
  if (loading.value || thread.id === threadId.value) {
    historyVisible.value = false
    scrollBottom()
    return
  }
  await loadThread(thread.id)
  if (!messages.value.length) messages.value = [welcomeMessage()]
}

async function deleteThread(thread: AssistantThread) {
  try {
    await ElMessageBox.confirm(`确定删除“${thread.title || '新的助手任务'}”吗？删除后无法恢复。`, '删除会话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await api.deleteAssistantThread(thread.id, apiContext())
    const wasCurrent = thread.id === threadId.value
    threads.value = threads.value.filter((item) => item.id !== thread.id)
    if (wasCurrent) {
      localStorage.removeItem(storageKey.value)
      threadId.value = ''
      messages.value = threads.value[0] ? [] : [welcomeMessage()]
      if (threads.value[0]) await loadThread(threads.value[0].id, false)
      if (!messages.value.length) messages.value = [welcomeMessage()]
    }
    ElMessage.success('会话已删除')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除会话失败')
  }
}

async function onFilesPicked(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || [])
  for (const file of files) {
    try {
      const uploaded = await api.uploadAssistantAttachment(file)
      attachments.value.push(uploaded)
      if (uploaded.status === 'error') ElMessage.warning(`${uploaded.filename}：${uploaded.error || '解析失败'}`)
    } catch (error: any) {
      ElMessage.error(`${file.name} 上传失败：${error.message || '请求失败'}`)
    }
  }
  target.value = ''
}

async function removeAttachment(item: AssistantAttachment) {
  attachments.value = attachments.value.filter((x) => x.id !== item.id)
  try { await api.deleteAssistantAttachment(item.id) } catch { /* 仅移除当前上下文即可 */ }
}

let streamController: AbortController | null = null

function syncThread(threadIdValue: string, title: string) {
  const existingThread = threads.value.find((thread) => thread.id === threadIdValue)
  if (existingThread) {
    existingThread.updated_at = new Date().toISOString()
    return
  }
  threads.value.unshift({
    id: threadIdValue,
    scenario_id: context.value.scenario_id || null,
    scope_key: assistantScopeKey.value,
    title: title.slice(0, 80),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })
}

function handleAssistantEvent(event: { type: string; data: any }, ai: AssistantMessage, index: number) {
  switch (event.type) {
    case 'progress':
      upsertThinking(ai, event.data as AssistantThought, index)
      break
    case 'token':
      ai.content += String(event.data || '')
      break
    case 'proposal':
      ai.proposal = event.data || {}
      break
    case 'meta': {
      const data = event.data || {}
      if (data.thread_id) {
        threadId.value = data.thread_id
        localStorage.setItem(storageKey.value, data.thread_id)
        const currentUserMessage = [...messages.value].reverse().find((message) => message.role === 'user' && message.content)
        syncThread(data.thread_id, currentUserMessage?.content || '新的助手任务')
      }
      ai.proposal = data.proposal || ai.proposal || {}
      ai.questions = data.questions || []
      ai.sources = data.sources || []
      if (Array.isArray(data.thinking)) ai.thinking = data.thinking
      break
    }
    case 'error':
      ai.content += `${ai.content ? '\n\n' : ''}这次请求没有完成：${String(event.data || '未知错误')}`
      upsertThinking(ai, { id: 'error', title: '处理未完成', detail: '助手遇到问题，请检查配置后重试。', status: 'error' }, index)
      break
  }
  scrollBottom()
}

function finishStream(ai: AssistantMessage) {
  ai.streaming = false
  loading.value = false
  streamController = null
  scrollBottom()
}

function send(text?: string) {
  const content = (text ?? input.value).trim()
  if (!content || loading.value) return
  if (messages.value.length === 1 && messages.value[0].role === 'assistant' && !messages.value[0].id) messages.value = []
  const currentAttachments = [...attachments.value]
  messages.value.push({ role: 'user', content, attachments: currentAttachments })
  messages.value.push({ role: 'assistant', content: '', thinking: [], streaming: true })
  // 从响应式数组中重新取出消息，避免直接修改未被 Vue 代理的原始对象。
  const aiIndex = messages.value.length - 1
  const ai = messages.value[aiIndex]
  input.value = ''
  loading.value = true
  scrollBottom()
  streamController = streamAssistantChat(
    {
      message: content,
      thread_id: threadId.value || undefined,
      scenario_id: context.value.scenario_id || undefined,
      page: context.value.page,
      path: context.value.path,
      selection: selection.id ? { ...selection } : {},
      attachment_ids: currentAttachments.map((item) => item.id),
      mode: mode.value,
    },
    (event) => {
      handleAssistantEvent(event, ai, aiIndex)
      if (event.type === 'meta') attachments.value = []
    },
    () => finishStream(ai),
    (error) => {
      ai.content += `${ai.content ? '\n\n' : ''}这次请求没有完成：${error.message || '请求失败'}`
      ai.streaming = false
      loading.value = false
      streamController = null
      ElMessage.error(error.message || '助手请求失败')
      scrollBottom()
    },
  )
}

async function applyProposal(message: AssistantMessage, index: number) {
  const proposal = proposalOf(message)
  if (!proposal || proposal.status === 'applied' || !context.value.scenario_id || !threadId.value || !proposal.proposal_id || applyingIndex.value !== null) return
  try {
    await ElMessageBox.confirm(
      `将把 ${proposal.changes?.filter((change) => change.operation !== 'skip').length || 0} 项变更写入当前场景草稿。草稿状态的工作流不会立即执行。`,
      '确认应用 Change Set',
      {
        type: 'warning',
        confirmButtonText: '确认应用',
        cancelButtonText: '取消',
        distinguishCancelAndClose: true,
      },
    )
  } catch {
    return
  }
  applyingIndex.value = index
  try {
    const result = await api.applyAssistantProposal({
      kind: proposal.kind,
      scenario_id: context.value.scenario_id,
      thread_id: threadId.value,
      proposal_id: proposal.proposal_id,
      confirm: true,
    })
    message.content += '\n\n变更已应用到当前场景草稿。'
    message.proposal = { ...proposal, status: 'applied', apply_result: result?.data || {} }
    window.dispatchEvent(new CustomEvent('assistant-proposal-applied', { detail: { scenario_id: context.value.scenario_id, kind: proposal.kind } }))
    ElMessage.success(result?.status === 'replayed' ? '该变更已应用过，已恢复应用结果' : '变更已应用到场景草稿')
  } catch (error: any) {
    ElMessage.error(error.message || '应用变更失败')
  } finally {
    applyingIndex.value = null
  }
}

function onSelection(event: Event) {
  const detail = (event as CustomEvent<{ label?: string; kind?: string; id?: string }>).detail || {}
  selection.label = detail.label || ''
  selection.kind = detail.kind || ''
  selection.id = detail.id || ''
}

watch(() => storageKey.value, async () => {
  messages.value = []
  threads.value = []
  threadId.value = ''
  historyVisible.value = false
  attachments.value = []
  Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
  if (visible.value) {
    await loadContext()
  }
})
watch(showLauncher, (show) => {
  if (!show) visible.value = false
})

onMounted(() => window.addEventListener('ontology-selection-change', onSelection))
onBeforeUnmount(() => {
  streamController?.abort()
  window.removeEventListener('ontology-selection-change', onSelection)
})
</script>

<style scoped>
.assistant-launcher {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 50;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 46px;
  padding: 6px 14px 6px 8px;
  border: 1px solid color-mix(in srgb, var(--primary) 32%, var(--border));
  border-radius: 24px;
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  color: var(--text);
  box-shadow: var(--shadow-md);
  backdrop-filter: blur(14px);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  font-weight: 750;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.assistant-launcher:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.assistant-launcher:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 38%, transparent); outline-offset: 3px; }
.assistant-launcher-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  color: #fff;
  background: var(--grad);
}
.assistant-live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

.assistant-shell { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--bg); }
.assistant-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 18px 13px; border-bottom: 1px solid var(--border); background: var(--surface); }
.assistant-title-wrap { display: flex; align-items: center; gap: 10px; min-width: 0; }
.assistant-avatar, .message-avatar { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; color: #fff; background: var(--grad); box-shadow: var(--shadow-sm); }
.assistant-avatar { width: 38px; height: 38px; border-radius: 12px; }
.assistant-title { display: flex; align-items: center; gap: 7px; font-size: 15px; font-weight: 800; color: var(--text); }
.assistant-subtitle { margin-top: 3px; color: var(--text-3); font-size: 11px; }
.assistant-context { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 10px 18px; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.context-hint { color: var(--text-3); font-size: 11px; margin-left: auto; }
.assistant-session-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 18px; border-bottom: 1px solid var(--border); background: var(--surface); }
.session-current { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.session-current strong { overflow: hidden; color: var(--text); font-size: 12px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.session-label { color: var(--text-3); font-size: 10px; }
.session-actions { display: flex; align-items: center; flex: 0 0 auto; gap: 6px; }
.session-actions :deep(.el-button) { min-height: 32px; }
.thread-count { display: inline-flex; align-items: center; justify-content: center; min-width: 17px; height: 17px; margin-left: 4px; padding: 0 4px; border-radius: 9px; color: var(--primary-600); background: var(--primary-soft); font-size: 10px; }
.assistant-history { flex: 1; min-height: 0; overflow: auto; padding: 18px; background: var(--bg); }
.history-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.history-head h3 { margin: 0 0 5px; color: var(--text); font-size: 15px; }
.history-head p { max-width: 280px; margin: 0; color: var(--text-3); font-size: 11px; line-height: 1.6; }
.history-state { display: flex; align-items: center; justify-content: center; min-height: 150px; color: var(--text-3); font-size: 12px; text-align: center; }
.thread-list { display: flex; flex-direction: column; gap: 7px; }
.thread-item { display: flex; align-items: stretch; gap: 3px; min-height: 58px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease; }
.thread-item:hover, .thread-item.active { border-color: color-mix(in srgb, var(--primary) 48%, var(--border)); background: var(--primary-soft); box-shadow: var(--shadow-sm); }
.thread-select { display: flex; align-items: center; flex: 1; gap: 10px; min-width: 0; min-height: 56px; padding: 8px 10px; border: 0; color: var(--text); background: transparent; cursor: pointer; text-align: left; }
.thread-select:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 35%, transparent); outline-offset: -3px; border-radius: 9px; }
.thread-dot { width: 7px; height: 7px; flex: 0 0 auto; border-radius: 50%; background: var(--text-3); }
.thread-item.active .thread-dot { background: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }
.thread-copy { display: flex; flex-direction: column; min-width: 0; gap: 4px; }
.thread-copy strong { overflow: hidden; font-size: 12px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.thread-copy small { color: var(--text-3); font-size: 10px; }
.thread-delete { align-self: center; width: 36px; height: 36px; margin-right: 5px; color: var(--text-3); }
.thread-delete:hover, .thread-delete:focus-visible { color: var(--danger); background: var(--danger-soft); }
.assistant-messages { flex: 1; min-height: 0; overflow-y: auto; padding: 18px; }
.assistant-empty { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 22px; color: var(--text-2); }
.empty-mark { display: flex; align-items: center; justify-content: center; width: 64px; height: 64px; margin-bottom: 13px; border-radius: 19px; color: var(--primary-600); background: var(--grad-soft); }
.assistant-empty h3 { margin: 0 0 6px; color: var(--text); font-size: 17px; }
.assistant-empty p { max-width: 310px; margin: 0; font-size: 12px; line-height: 1.7; }
.assistant-suggestions { display: flex; flex-direction: column; gap: 8px; width: min(100%, 320px); margin-top: 20px; }
.suggestion-chip { min-height: 42px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 10px; color: var(--text-2); background: var(--surface); cursor: pointer; font: inherit; text-align: left; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease), color var(--dur) var(--ease); }
.suggestion-chip:hover, .suggestion-chip:focus-visible { border-color: var(--primary); color: var(--primary-600); background: var(--primary-soft); outline: none; }
.assistant-message { display: flex; gap: 9px; margin-bottom: 16px; }
.assistant-message.user { justify-content: flex-end; }
.assistant-message-avatar { width: 30px; height: 30px; border-radius: 10px; margin-top: 21px; }
.message-content { max-width: 88%; min-width: 0; }
.assistant-message.user .message-content { max-width: 82%; }
.message-label { margin: 0 4px 5px; color: var(--text-3); font-size: 10.5px; font-weight: 700; }
.assistant-message.user .message-label { text-align: right; }
.thinking-summary { margin-bottom: 7px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--primary) 22%, var(--border)); border-radius: 10px; background: var(--surface-2); }
.thinking-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; min-height: 38px; padding: 8px 10px; border: 0; color: var(--text-2); background: transparent; cursor: pointer; font: inherit; font-size: 11px; text-align: left; }
.thinking-toggle:hover, .thinking-toggle:focus-visible { color: var(--primary-600); background: var(--primary-soft); outline: none; }
.thinking-toggle-main { display: inline-flex; align-items: center; min-width: 0; gap: 6px; }
.thinking-toggle-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thinking-live { flex: 0 0 auto; color: var(--primary-600); font-size: 10px; }
.thinking-chevron { flex: 0 0 auto; color: var(--text-3); transition: transform 160ms ease; }
.thinking-chevron.rotated { transform: rotate(180deg); }
.thinking-body { padding: 2px 10px 10px 28px; border-top: 1px solid var(--border); }
.thinking-step { position: relative; display: flex; gap: 8px; padding: 8px 0 0; color: var(--text-2); font-size: 11px; line-height: 1.45; }
.thinking-step-dot { width: 7px; height: 7px; flex: 0 0 auto; margin-top: 5px; border-radius: 50%; background: var(--text-3); }
.thinking-step.is-running .thinking-step-dot { background: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }
.thinking-step.is-error .thinking-step-dot { background: var(--danger); }
.thinking-step div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.thinking-step strong { color: var(--text); font-weight: 700; }
.thinking-step span:not(.thinking-step-dot) { color: var(--text-3); }
.thinking-note { margin-top: 9px; color: var(--text-3); font-size: 10px; line-height: 1.5; }
.message-bubble { padding: 10px 12px; border: 1px solid var(--border); border-radius: 13px 13px 13px 4px; background: var(--surface); box-shadow: var(--shadow-xs); }
.message-bubble.user { border-color: var(--border-strong); border-radius: 13px 13px 4px 13px; color: var(--primary-600); background: var(--primary-soft); white-space: pre-wrap; }
.user-content { line-height: 1.65; font-size: 13px; }
.stream-cursor { display: inline-block; margin-left: 2px; color: var(--primary); animation: stream-cursor-blink 900ms steps(2, jump-none) infinite; }
@keyframes stream-cursor-blink { 50% { opacity: 0; } }
.proposal-card { margin-top: 9px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--warning) 38%, var(--border)); border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-xs); }
.proposal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 11px 12px 8px; background: var(--warning-soft); }
.proposal-title { display: flex; align-items: center; gap: 6px; color: var(--text); font-size: 12.5px; font-weight: 800; }
.proposal-summary { margin-top: 4px; color: var(--text-2); font-size: 11.5px; line-height: 1.5; }
.proposal-preview { display: flex; align-items: center; gap: 10px; padding: 9px 12px; color: var(--text-2); font-size: 11.5px; }
.preview-toggle { margin-left: auto; padding: 0; border: 0; color: var(--primary-600); background: transparent; cursor: pointer; font: inherit; }
.proposal-changes { display: flex; flex-direction: column; gap: 7px; max-height: 190px; padding: 0 12px 10px; overflow: auto; }
.proposal-change { display: flex; align-items: flex-start; gap: 7px; padding: 7px 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.proposal-change-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.proposal-change-copy strong { color: var(--text); font-size: 11px; font-weight: 750; line-height: 1.4; }
.proposal-change-copy span { color: var(--text-3); font-size: 10.5px; line-height: 1.45; }
.proposal-code { max-height: 180px; margin: 0 12px 10px; padding: 10px; overflow: auto; border-radius: 8px; color: #e2e8f0; background: #1d2930; font-size: 10px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.proposal-actions { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-top: 1px solid var(--border); }
.proposal-hint { color: var(--text-3); font-size: 10.5px; }
.message-sources { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 7px; }
.sources-label { color: var(--text-3); font-size: 10.5px; }
.question-list { display: flex; flex-direction: column; gap: 7px; margin-top: 8px; }
.question-card { display: flex; flex-direction: column; gap: 4px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 9px; color: var(--text-2); background: var(--surface-2); font-size: 11.5px; line-height: 1.5; }
.question-card b { color: var(--text); }
.question-card .el-button { align-self: flex-start; padding-left: 0; }
.assistant-thinking { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; color: var(--text-3); font-size: 11.5px; }
.assistant-thinking .message-avatar { width: 30px; height: 30px; border-radius: 10px; }
.thinking-title { color: var(--text-2); }
.thinking-dots { display: inline-block; width: 18px; overflow: hidden; animation: dots 1.2s steps(3, end) infinite; }
@keyframes dots { 0%, 20% { width: 0; } 60% { width: 9px; } 100% { width: 18px; } }
.assistant-composer { padding: 10px 14px 14px; border-top: 1px solid var(--border); background: var(--surface); }
.attachment-strip { display: flex; flex-direction: column; gap: 5px; max-height: 90px; overflow: auto; margin-bottom: 8px; }
.attachment-chip { display: flex; align-items: center; gap: 5px; min-width: 0; padding: 5px 7px; border: 1px solid var(--border); border-radius: 7px; color: var(--text-2); background: var(--surface-2); font-size: 11px; }
.attachment-chip span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attachment-chip button { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 5px; color: var(--text-3); background: transparent; cursor: pointer; }
.attachment-chip button:hover, .attachment-chip button:focus-visible { color: var(--danger); background: var(--danger-soft); outline: none; }
.composer-tools { display: flex; align-items: center; gap: 7px; min-height: 30px; margin-bottom: 7px; }
.tool-button { display: inline-flex; align-items: center; gap: 4px; min-height: 30px; padding: 0 8px; border: 1px solid var(--border); border-radius: 7px; color: var(--text-2); background: var(--surface); cursor: pointer; font-size: 11.5px; }
.tool-button:hover, .tool-button:focus-within { border-color: var(--primary); color: var(--primary-600); background: var(--primary-soft); }
.tool-button input { display: none; }
.composer-hint { flex: 1; min-width: 0; overflow: hidden; color: var(--text-3); font-size: 10.5px; text-align: right; text-overflow: ellipsis; white-space: nowrap; }
.composer-input-row { display: flex; align-items: flex-end; gap: 8px; }
.composer-input-row :deep(.el-textarea__inner) { min-height: 74px !important; padding-right: 12px; }
.send-button { width: 42px; height: 42px; padding: 0; flex: 0 0 auto; }

@media (prefers-reduced-motion: reduce) {
  .thinking-chevron, .stream-cursor { transition: none; animation: none; }
}

@media (max-width: 560px) {
  .assistant-launcher { right: 14px; bottom: 14px; }
  .context-hint { width: 100%; margin-left: 0; }
  .assistant-session-bar { align-items: flex-start; flex-direction: column; }
  .session-actions { width: 100%; }
  .session-actions :deep(.el-button) { flex: 1; }
  .assistant-messages { padding: 14px; }
  .assistant-history { padding: 14px; }
  .assistant-composer { padding: 9px 10px 12px; }
  .composer-hint { display: none; }
}
</style>
