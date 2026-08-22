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
                <template v-if="proposalOf(message)?.kind === 'scenario'">
                  <span>业务场景 1</span>
                  <span>状态 {{ scenarioStatusLabel(proposalOf(message)?.payload?.status) }}</span>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'ontology'">
                  <span>对象类型 {{ proposalOf(message)?.payload?.entities?.length || 0 }}</span>
                  <span>关系类型 {{ proposalOf(message)?.payload?.relations?.length || 0 }}</span>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'mapping'">
                  <span>目标 {{ proposalOf(message)?.payload?.entity_name || '对象类型' }}</span>
                  <span>字段 {{ Object.keys(proposalOf(message)?.payload?.column_map || {}).length }}</span>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'workflow'">
                  <span>节点 {{ proposalOf(message)?.payload?.nodes?.length || 0 }}</span>
                  <span>连线 {{ proposalOf(message)?.payload?.edges?.length || 0 }}</span>
                </template>
                <span v-if="proposalOf(message)?.changes?.length">差异 {{ proposalOf(message)?.changes?.length }}</span>
                <button type="button" class="preview-toggle" @click="toggleProposal(index)">{{ expandedProposal[index] ? '收起详情' : '查看详情' }}</button>
              </div>
              <div v-if="expandedProposal[index] && proposalOf(message)?.changes?.length" class="proposal-changes" aria-label="变更清单差异">
                <div v-for="(change, changeIndex) in proposalOf(message)?.changes" :key="`${change.resource}-${change.name}-${changeIndex}`" class="proposal-change">
                  <el-tag size="small" effect="plain" :type="proposalOperationType(change.operation)">{{ proposalOperationLabel(change.operation) }}</el-tag>
                  <div class="proposal-change-copy">
                    <strong>{{ proposalResourceLabel(change.resource) }} · {{ change.name }}</strong>
                    <span>{{ change.summary }}</span>
                  </div>
                </div>
              </div>
              <div v-if="expandedProposal[index]" class="proposal-detail" aria-label="草稿结构化内容">
                <template v-if="proposalOf(message)?.kind === 'scenario'">
                  <dl class="proposal-summary-grid">
                    <div><dt>场景名称</dt><dd>{{ proposalOf(message)?.payload?.name || '未命名场景' }}</dd></div>
                    <div><dt>行业领域</dt><dd>{{ proposalOf(message)?.payload?.industry || '未指定' }}</dd></div>
                    <div><dt>命名空间</dt><dd>{{ proposalOf(message)?.payload?.namespace || 'default' }}</dd></div>
                  </dl>
                  <p v-if="proposalOf(message)?.payload?.description" class="proposal-description">{{ proposalOf(message)?.payload?.description }}</p>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'ontology'">
                  <section class="proposal-section">
                    <h4>对象类型</h4>
                    <article v-for="entity in proposalOf(message)?.payload?.entities || []" :key="entity.name" class="ontology-preview-card">
                      <div><strong>{{ entity.name || '未命名对象类型' }}</strong><span>{{ entity.description || '暂无说明' }}</span></div>
                      <div class="ontology-property-list">
                        <span v-for="property in entity.properties || []" :key="property.name">
                          <b>{{ property.name }}</b>{{ propertyTypeLabel(property.data_type) }}<em v-if="property.is_key">主键</em><em v-if="property.is_required">必填</em>
                        </span>
                        <small v-if="!(entity.properties || []).length">暂未定义属性</small>
                      </div>
                    </article>
                  </section>
                  <section class="proposal-section">
                    <h4>关系类型</h4>
                    <div v-for="relation in proposalOf(message)?.payload?.relations || []" :key="`${relation.name}-${relation.source}-${relation.target}`" class="relation-preview-row">
                      <strong>{{ relation.name || '未命名关系' }}</strong><span>{{ relation.source || '?' }} → {{ relation.target || '?' }}</span><el-tag size="small" effect="plain">{{ relation.relation_type || '1:N' }}</el-tag>
                    </div>
                    <small v-if="!(proposalOf(message)?.payload?.relations || []).length" class="proposal-empty">暂未识别关系类型，请在应用前确认业务文档是否描述了对象间关系。</small>
                  </section>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'mapping'">
                  <dl class="proposal-summary-grid">
                    <div><dt>目标对象类型</dt><dd>{{ proposalOf(message)?.payload?.entity_name || proposalOf(message)?.payload?.entity_id || '未选择' }}</dd></div>
                    <div><dt>数据源</dt><dd>{{ proposalOf(message)?.payload?.data_source_name || proposalOf(message)?.payload?.data_source_id || '未选择' }}</dd></div>
                    <div><dt>表 / 文件结构</dt><dd>{{ proposalOf(message)?.payload?.table_name || '未选择' }}</dd></div>
                  </dl>
                  <div class="mapping-preview-list">
                    <div v-for="(sourceColumn, propertyName) in proposalOf(message)?.payload?.column_map || {}" :key="String(propertyName)"><b>{{ propertyName }}</b><span>←</span><strong>{{ sourceColumn }}</strong></div>
                  </div>
                </template>
                <template v-else-if="proposalOf(message)?.kind === 'workflow'">
                  <section class="proposal-section">
                    <h4>{{ proposalOf(message)?.payload?.name || '工作流草稿' }}</h4>
                    <div class="workflow-preview-list">
                      <div v-for="(node, nodeIndex) in proposalOf(message)?.payload?.nodes || []" :key="node.id || nodeIndex"><span>{{ nodeIndex + 1 }}</span><b>{{ node.name || node.label || node.id || '未命名节点' }}</b><el-tag size="small" effect="plain">{{ workflowNodeTypeLabel(node.type) }}</el-tag></div>
                    </div>
                  </section>
                </template>
              </div>
              <div class="proposal-actions">
                <el-button size="small" type="primary" :loading="applyingIndex === index" :disabled="!proposalCanApply(proposalOf(message)) || proposalOf(message)?.status === 'applied' || !proposalOf(message)?.proposal_id" @click="applyProposal(message, index)">
                  <el-icon aria-hidden="true"><Check /></el-icon>{{ proposalApplyLabel(proposalOf(message)) }}
                </el-button>
                <span v-if="proposalApplyHint(proposalOf(message))" class="proposal-hint">{{ proposalApplyHint(proposalOf(message)) }}</span>
              </div>
            </div>

            <div v-if="sourcesOf(message).length" class="message-sources" aria-label="回答引用">
              <span class="sources-label">回答依据</span>
              <button
                v-for="source in sourcesOf(message)"
                :key="source.id || source.filename"
                type="button"
                class="source-card"
                :class="{ 'is-clickable': source.file_id }"
                :disabled="!source.file_id"
                :title="source.file_id ? `查看引用原文：${source.filename}` : '本次对话的临时附件'"
                @click="openSource(source)"
              >
                <span class="source-mark">{{ source.citation_id || (source.kind === 'rag' ? '引用' : '附件') }}</span>
                <span class="source-copy"><strong>{{ source.filename }}</strong><small>{{ source.data_source_name || (source.file_id ? '正式资料库' : '临时上下文') }}</small></span>
                <el-icon v-if="source.file_id" aria-hidden="true"><ArrowRight /></el-icon>
              </button>
            </div>

            <section v-if="hasAssistantEvidence(message)" class="answer-evidence" aria-label="回答的规则、工具与不确定项">
              <header>
                <span><el-icon aria-hidden="true"><DocumentChecked /></el-icon>回答证据</span>
                <el-tag size="small" effect="plain" :type="confidenceType(message.evidence?.confidence)">置信度 {{ confidencePercent(message.evidence?.confidence) }}</el-tag>
              </header>
              <div class="evidence-meta-grid">
                <div v-if="message.evidence?.rules_used?.length"><b>使用规则</b><span>{{ message.evidence.rules_used.map((item) => item.result || item.name).join('；') }}</span></div>
                <div v-if="message.evidence?.tools_called?.length"><b>调用工具</b><span>{{ message.evidence.tools_called.map((item) => `${item.name}${item.purpose ? ` · ${item.purpose}` : ''}`).join('；') }}</span></div>
              </div>
              <div v-if="message.evidence?.uncertainties?.length" class="evidence-uncertainties"><b>仍需确认</b><ul><li v-for="item in message.evidence.uncertainties" :key="item">{{ item }}</li></ul></div>
            </section>

            <section v-if="hasActionPreview(message)" class="assistant-action-preview" aria-label="操作影响与预演结果">
              <header>
                <div><span class="eyebrow">安全预演</span><b>{{ message.action_preview?.target?.name || '待选择操作' }}</b></div>
                <el-tag size="small" effect="plain" :type="message.action_preview?.requires_approval ? 'warning' : 'info'">{{ message.action_preview?.requires_approval ? '需要确认或审批' : '仍需显式提交' }}</el-tag>
              </header>
              <dl>
                <div><dt>执行方式</dt><dd>{{ actionExecutorLabel(message.action_preview?.impact?.executor_type) }}</dd></div>
                <div><dt>权限检查</dt><dd>{{ assistantPermissionLabel(message.action_preview?.permission) }}</dd></div>
                <div><dt>副作用</dt><dd>{{ message.action_preview?.impact?.side_effects_skipped === true ? '已跳过，仅预演' : '未创建预演' }}</dd></div>
              </dl>
              <p v-if="message.action_preview?.impact?.postcondition">影响：{{ message.action_preview.impact.postcondition }}</p>
              <KeyValueEditor v-if="Object.keys(message.action_preview?.parameters || {}).length" :model-value="message.action_preview?.parameters" readonly empty-text="暂无执行参数" class="action-preview-params" />
              <div class="assistant-action-next">
                <span>{{ message.action_preview?.execution_boundary || '真实执行必须进入已配置的操作并重新确认。' }}</span>
                <el-button v-if="message.action_preview?.target?.id && context.scenario_id" size="small" type="primary" plain @click="continueGovernedAction(message.action_preview)">
                  {{ message.action_preview?.preview?.log_id ? '进入操作确认' : '填写参数并预演' }}
                </el-button>
              </div>
            </section>

            <div v-if="message.questions?.length" class="question-list">
              <div v-for="question in message.questions" :key="question.id" class="question-card">
                <b>{{ question.title }}</b>
                <span>{{ question.message }}</span>
                <div v-if="question.options?.length" class="question-options">
                  <button v-for="option in question.options" :key="option.value || option.label" type="button" @click="answerQuestion(question, option, message)">
                    <span><strong>{{ option.label }}</strong><em v-if="option.recommended">推荐</em></span>
                    <small>{{ option.impact }}</small>
                  </button>
                </div>
                <el-button v-else size="small" text type="primary" @click="answerQuestion(question)">补充信息</el-button>
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
          <div class="temporary-context-note"><el-icon aria-hidden="true"><Lock /></el-icon>临时上下文仅随下一条消息发送，不会自动进入正式数据源或对象映射。</div>
        </div>
        <div class="composer-tools">
          <label class="tool-button" :class="{ disabled: uploadingFiles > 0 }" title="添加临时附件">
            <el-icon v-if="uploadingFiles" class="is-loading" aria-hidden="true"><Loading /></el-icon>
            <el-icon v-else aria-hidden="true"><Paperclip /></el-icon><span>{{ uploadingFiles ? `正在解析 ${uploadingFiles} 个文件` : '添加附件' }}</span>
            <input ref="fileInput" type="file" multiple :disabled="uploadingFiles > 0" accept=".pdf,.docx,.xlsx,.xls,.pptx,.md,.txt,.csv,.json,.png,.jpg,.jpeg" @change="onFilesPicked" />
          </label>
          <div class="assistant-mode-switch" role="group" aria-label="助手工作模式">
            <button
              v-for="option in modeOptions"
              :key="option.value"
              type="button"
              :class="{ active: mode === option.value }"
              :aria-pressed="mode === option.value"
              :title="option.hint"
              @click="mode = option.value"
            >{{ option.label }}</button>
          </div>
        </div>
        <div class="composer-mode-hint" role="status"><b>{{ activeModeOption.label }}</b><span>{{ activeModeOption.hint }}</span></div>
        <div class="composer-input-row">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="12000"
            show-word-limit
            :placeholder="activeModeOption.placeholder"
            @keydown.enter.exact.prevent="send()"
          />
          <el-button class="send-button" type="primary" :loading="loading" :disabled="!input.trim() || uploadingFiles > 0" aria-label="发送消息" title="发送" @click="send()">
            <el-icon aria-hidden="true"><Promotion /></el-icon>
          </el-button>
        </div>
      </footer>
    </div>
  </el-drawer>

  <el-dialog v-model="sourcePreviewVisible" title="引用原文" width="min(720px, 94vw)" append-to-body>
    <div v-loading="sourcePreviewLoading" class="source-preview">
      <div v-if="sourcePreview" class="source-preview-meta">
        <el-tag v-if="sourcePreview.citation_id" type="info" effect="plain">{{ sourcePreview.citation_id }}</el-tag>
        <div><strong>{{ sourcePreview.filename }}</strong><span>{{ sourcePreview.data_source_name || '正式资料库' }}</span></div>
      </div>
      <el-alert title="以下内容按当前账号权限重新读取；历史引用失效或权限收回后将无法显示。" type="info" :closable="false" show-icon />
      <pre class="source-preview-text">{{ sourcePreviewText || '暂无可显示的引用片段' }}</pre>
    </div>
    <template #footer><el-button type="primary" @click="sourcePreviewVisible = false">关闭</el-button></template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, streamAssistantChat } from '@/api'
import type { AssistantActionPreview, AssistantAttachment, AssistantMessage, AssistantProposal, AssistantQuestion, AssistantSource, AssistantThread, AssistantThought } from '@/types'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import KeyValueEditor from '@/components/KeyValueEditor.vue'

interface AssistantContext {
  page?: string
  path?: string
  scenario_id?: string
}

const props = withDefaults(defineProps<{ context: AssistantContext; hideLauncher?: boolean }>(), {
  hideLauncher: false,
})
const router = useRouter()
type AssistantMode = 'explain' | 'draft' | 'apply' | 'execute'
const visible = ref(false)
const loading = ref(false)
const input = ref('')
const mode = ref<AssistantMode>('explain')
const modeOptions: Array<{ value: AssistantMode; label: string; hint: string; placeholder: string }> = [
  { value: 'explain', label: '只解释', hint: '只读取和说明，不生成或写入变更。', placeholder: '询问当前业务事实、规则、来源或不确定项…' },
  { value: 'draft', label: '生成草稿', hint: '生成可检查的变更清单，确认前不写入。', placeholder: '描述要创建的场景、本体、映射或工作流草稿…' },
  { value: 'apply', label: '应用修改', hint: '定位待应用草稿；写入仍需在变更清单中二次确认。', placeholder: '说明要应用哪份草稿或需要核对的变更范围…' },
  { value: 'execute', label: '执行操作', hint: '分析影响并引导预演/审批；不会从聊天直接触发副作用。', placeholder: '描述要执行的操作、参数和期望结果，以便检查权限与影响…' },
]
const activeModeOption = computed(() => modeOptions.find((option) => option.value === mode.value) || modeOptions[0])
const messages = ref<AssistantMessage[]>([])
const attachments = ref<AssistantAttachment[]>([])
const threadId = ref('')
const threads = ref<AssistantThread[]>([])
const historyVisible = ref(false)
const threadsLoading = ref(false)
const messageRef = ref<HTMLElement>()
const fileInput = ref<HTMLInputElement>()
const uploadingFiles = ref(0)
const applyingIndex = ref<number | null>(null)
const sourcePreviewVisible = ref(false)
const sourcePreviewLoading = ref(false)
const sourcePreview = ref<AssistantSource | null>(null)
const sourcePreviewText = ref('')
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
const showLauncher = computed(() => {
  if (props.hideLauncher) return false
  const path = context.value.path
  // 场景建模页已有唯一的“AI 建模助手”入口；不再叠加浮动按钮遮挡编辑面板。
  return !/^\/agents\/[^/]+\/chat(?:\/|$|\?)/.test(path)
    && !/^\/scenarios\/[^/?]+(?:\/|$|\?)/.test(path)
})
const starterSuggestions = computed(() => context.value.scenario_id
  ? ['解释当前业务场景', '根据当前资料生成本体草稿', '根据现有对象类型和数据源生成映射草稿', '把当前业务流程编排为工作流']
  : ['根据我的业务描述生成场景草稿', '这个平台可以帮我做什么？', '我应该先准备哪些业务资料？'])

function proposalOf(message: AssistantMessage): AssistantProposal | null {
  const proposal = message.proposal as AssistantProposal | undefined
  return proposal && proposal.kind && proposal.payload ? proposal : null
}

function proposalCanApply(proposal: AssistantProposal | null) {
  if (!proposal) return false
  return proposal.kind === 'scenario' ? !context.value.scenario_id : Boolean(context.value.scenario_id)
}

function proposalApplyLabel(proposal: AssistantProposal | null) {
  if (!proposal) return '确认并应用变更'
  if (proposal.status === 'applied') return proposal.kind === 'scenario' ? '场景已创建' : '变更已应用'
  return ({ scenario: '确认并创建场景', mapping: '确认并保存映射', ontology: '确认并应用本体', workflow: '确认并保存流程' } as Record<string, string>)[proposal.kind] || '确认并应用变更'
}

function proposalApplyHint(proposal: AssistantProposal | null) {
  if (!proposal?.proposal_id) return '此草稿缺少安全标识，请重新生成'
  if (proposal.kind === 'scenario' && context.value.scenario_id) return '场景草稿只能在全局工作区创建'
  if (proposal.kind !== 'scenario' && !context.value.scenario_id) return '请先打开业务场景'
  if (proposal.kind === 'mapping') return '保存后仍需预览、测试并刷新对象'
  return ''
}

function proposalOperationLabel(operation: string) {
  return ({ add: '新增', update: '修改', delete: '删除', skip: '跳过' } as Record<string, string>)[operation] || operation
}

function proposalOperationType(operation: string) {
  return ({ add: 'success', update: 'warning', delete: 'danger', skip: 'info' } as Record<string, string>)[operation] || 'info'
}

function proposalResourceLabel(resource: string) {
  return ({
    scenario: '业务场景', entity: '对象类型', property: '属性', relation: '关系类型', mapping: '数据映射', mapping_field: '映射字段', data_mapping: '数据映射',
    action: '操作', rule: '规则', workflow: '工作流', workflow_node: '工作流节点', workflow_edge: '工作流连线',
  } as Record<string, string>)[resource] || resource
}

function scenarioStatusLabel(status?: string) {
  return ({ draft: '草稿', active: '已启用', archived: '已归档' } as Record<string, string>)[status || 'draft'] || '草稿'
}

function propertyTypeLabel(type?: string) {
  return ({ string: '文本', integer: '整数', number: '数值', boolean: '是/否', date: '日期', datetime: '日期时间', object: '对象', array: '列表', uuid: '唯一标识' } as Record<string, string>)[type || 'string'] || '文本'
}

function workflowNodeTypeLabel(type?: string) {
  return ({ start: '开始', end: '结束', action: '执行操作', rule: '规则判断', event: '业务事件', condition: '条件分支', llm: '模型处理', parallel: '并行处理', loop: '循环处理', delay: '等待' } as Record<string, string>)[type || ''] || '业务节点'
}

function actionExecutorLabel(type?: string) {
  return ({ sql: '数据库查询', skill: '本地技能', mcp: '外部工具', http: 'HTTPS 接口', script: '受控脚本' } as Record<string, string>)[type || ''] || '尚未确定'
}

function sourcesOf(message: AssistantMessage): AssistantSource[] {
  return message.sources?.length
    ? message.sources
    : (Array.isArray(message.attachments) ? message.attachments : []) as AssistantSource[]
}

function hasAssistantEvidence(message: AssistantMessage) {
  const evidence = message.evidence
  return Boolean(evidence && (evidence.rules_used?.length || evidence.tools_called?.length || evidence.uncertainties?.length || evidence.confidence > 0))
}
function confidencePercent(value?: number) { return `${Math.round(Math.max(0, Math.min(Number(value || 0), 1)) * 100)}%` }
function confidenceType(value?: number): 'success' | 'warning' | 'danger' {
  const score = Number(value || 0)
  return score >= .8 ? 'success' : score >= .6 ? 'warning' : 'danger'
}
function hasActionPreview(message: AssistantMessage) { return Boolean(message.action_preview && Object.keys(message.action_preview).length) }
function assistantPermissionLabel(permission?: Record<string, unknown>) {
  if (!permission || !Object.keys(permission).length) return '尚未检查'
  const allowed = permission.allowed ?? permission.decision ?? permission.result
  if (allowed === true || allowed === 'allow' || allowed === 'allowed') return '允许预演'
  if (allowed === false || allowed === 'deny' || allowed === 'denied') return '未获允许'
  return String(allowed ?? '已检查')
}
async function continueGovernedAction(preview?: AssistantActionPreview) {
  const actionId = preview?.target?.id
  if (!actionId || !context.value.scenario_id) return
  visible.value = false
  await router.push({
    name: 'scenario-detail',
    params: { id: context.value.scenario_id },
    query: { stage: 'actions', action_id: actionId, return_to: context.value.path || undefined },
    state: { assistant_action_preview: JSON.parse(JSON.stringify(preview || {})) },
  })
  window.dispatchEvent(new CustomEvent('open-governed-action', { detail: { action_id: actionId, preview } }))
}

async function openSource(source: AssistantSource) {
  if (!source.file_id) return
  sourcePreview.value = source
  sourcePreviewText.value = ''
  sourcePreviewVisible.value = true
  sourcePreviewLoading.value = true
  try {
    const result = await api.fileText(source.file_id)
    const text = result.text || ''
    const hasRange = Number.isFinite(source.char_start) && Number.isFinite(source.char_end)
    if (!hasRange) {
      sourcePreviewText.value = text.slice(0, 5000)
      return
    }
    const start = Math.max(Number(source.char_start || 0), 0)
    const end = Math.max(Number(source.char_end || start), start)
    const contextStart = Math.max(start - 240, 0)
    const contextEnd = Math.min(end + 240, text.length)
    sourcePreviewText.value = `${contextStart > 0 ? '…' : ''}${text.slice(contextStart, contextEnd)}${contextEnd < text.length ? '…' : ''}`
  } catch (error: any) {
    sourcePreviewText.value = ''
    ElMessage.error(error.message || '引用原文读取失败，资料可能已变更或权限已收回')
  } finally {
    sourcePreviewLoading.value = false
  }
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

async function uploadTemporaryFiles(files: File[]) {
  for (const file of files) {
    uploadingFiles.value += 1
    try {
      const uploaded = await api.uploadAssistantAttachment(file)
      attachments.value.push(uploaded)
      if (uploaded.status === 'error') ElMessage.warning(`${uploaded.filename}：${uploaded.error || '解析失败'}`)
    } catch (error: any) {
      ElMessage.error(`${file.name} 上传失败：${error.message || '请求失败'}`)
    } finally {
      uploadingFiles.value = Math.max(uploadingFiles.value - 1, 0)
    }
  }
}

async function onFilesPicked(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || [])
  await uploadTemporaryFiles(files)
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
    case 'action_preview':
      ai.action_preview = event.data || undefined
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
      ai.evidence = data.evidence || undefined
      ai.action_preview = data.action_preview || undefined
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
  if (!content || loading.value || uploadingFiles.value > 0) return
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

async function answerQuestion(
  question: AssistantQuestion,
  option?: NonNullable<AssistantQuestion['options']>[number],
  sourceMessage?: AssistantMessage,
) {
  if (!option) {
    input.value = `${question.title}：`
    return
  }
  if (option.value === 'open_scenario') {
    visible.value = false
    await router.push({ name: 'scenarios' })
    return
  }
  if (option.value === 'draft_scenario') {
    mode.value = 'draft'
    input.value = '请根据以下业务目标创建业务场景草稿：\n'
    return
  }
  if (['provide_params', 'inspect_schema'].includes(String(option.value || '')) && sourceMessage?.action_preview?.target?.id) {
    await continueGovernedAction(sourceMessage.action_preview)
    return
  }
  if (option.value === 'configure_action' && context.value.scenario_id) {
    visible.value = false
    await router.push({
      name: 'scenario-detail',
      params: { id: context.value.scenario_id },
      query: { stage: 'actions', return_to: context.value.path || undefined },
    })
    return
  }
  const prompt = option.prompt?.trim() || [
    question.title,
    `我的选择：${option.label}${option.value ? `（${option.value}）` : ''}`,
    `已了解影响：${option.impact}`,
    '请按这个选择继续，并明确后续仍需我确认的变更或操作。',
  ].join('\n')
  send(prompt)
}

async function applyProposal(message: AssistantMessage, index: number) {
  const proposal = proposalOf(message)
  if (!proposal || proposal.status === 'applied' || !proposalCanApply(proposal) || !threadId.value || !proposal.proposal_id || applyingIndex.value !== null) return
  const effectiveChanges = proposal.changes?.filter((change) => change.operation !== 'skip').length || 0
  const confirmation = proposal.kind === 'scenario'
    ? `将根据这份草稿创建业务场景“${proposal.payload?.name || '未命名场景'}”。附件仍只属于助手临时上下文，不会成为正式数据源。`
    : proposal.kind === 'mapping'
      ? `将把 ${effectiveChanges} 项映射差异保存到当前场景。保存不会导入数据，之后仍需预览、测试并刷新对象。`
      : `将把 ${effectiveChanges} 项变更写入当前场景草稿。草稿状态的工作流不会立即执行。`
  try {
    await ElMessageBox.confirm(
      confirmation,
      '确认应用变更清单',
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
      scenario_id: proposal.kind === 'scenario' ? undefined : context.value.scenario_id,
      thread_id: threadId.value,
      proposal_id: proposal.proposal_id,
      confirm: true,
    })
    const appliedScenarioId = proposal.kind === 'scenario' ? result?.data?.scenario_id : ''
    message.content += proposal.kind === 'scenario'
      ? '\n\n业务场景已创建，正在进入场景建设。'
      : proposal.kind === 'mapping'
        ? '\n\n映射草稿已保存。下一步请预览、测试并刷新对象。'
        : '\n\n变更已应用到当前场景草稿。'
    message.proposal = { ...proposal, status: 'applied', apply_result: result?.data || {} }
    window.dispatchEvent(new CustomEvent('assistant-proposal-applied', { detail: { scenario_id: appliedScenarioId || context.value.scenario_id, kind: proposal.kind } }))
    if (result?.status === 'replayed') ElMessage.info('该变更已应用过，已恢复应用结果')
    else ElMessage.success(proposal.kind === 'scenario' ? '业务场景已创建' : proposal.kind === 'mapping' ? '映射草稿已保存' : '变更已应用到场景草稿')
    if (appliedScenarioId) {
      visible.value = false
      await router.push({ name: 'scenario-detail', params: { id: appliedScenarioId }, query: { stage: 'flow' } })
    }
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

async function onAssistantOpenRequest(event: Event) {
  const detail = (event as CustomEvent<{ mode?: AssistantMode | 'ask'; prompt?: string; files?: File[] }>).detail || {}
  if (loading.value) {
    ElMessage.info('当前助手任务完成后可继续新的建模步骤')
    return
  }
  await nextTick()
  if (!visible.value) await openAssistant()
  historyVisible.value = false
  if (detail.mode) mode.value = detail.mode === 'ask' ? 'explain' : detail.mode
  if (typeof detail.prompt === 'string') input.value = detail.prompt
  const files = Array.isArray(detail.files) ? detail.files.filter((file): file is File => file instanceof File) : []
  if (files.length) await uploadTemporaryFiles(files)
  scrollBottom()
}

watch(() => storageKey.value, async () => {
  streamController?.abort()
  streamController = null
  loading.value = false
  messages.value = []
  threads.value = []
  threadId.value = ''
  historyVisible.value = false
  attachments.value = []
  sourcePreviewVisible.value = false
  Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
  if (visible.value) {
    await loadContext()
  }
})
watch(showLauncher, (show) => {
  if (!show) visible.value = false
})

onMounted(() => {
  window.addEventListener('ontology-selection-change', onSelection)
  window.addEventListener('assistant-open-request', onAssistantOpenRequest)
})
onBeforeUnmount(() => {
  streamController?.abort()
  window.removeEventListener('ontology-selection-change', onSelection)
  window.removeEventListener('assistant-open-request', onAssistantOpenRequest)
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
.proposal-detail { display: grid; gap: 10px; max-height: 300px; margin: 0 12px 10px; padding: 10px; overflow: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }
.proposal-summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 0; }
.proposal-summary-grid div { padding: 7px 8px; border-radius: 7px; background: var(--surface); }
.proposal-summary-grid dt { color: var(--text-3); font-size: 9.5px; }
.proposal-summary-grid dd { margin: 3px 0 0; color: var(--text); font-size: 11px; font-weight: 700; overflow-wrap: anywhere; }
.proposal-description { margin: 0; color: var(--text-2); font-size: 11px; line-height: 1.6; }
.proposal-section { display: grid; gap: 7px; }
.proposal-section h4 { margin: 0; color: var(--text); font-size: 11px; }
.ontology-preview-card { display: grid; gap: 7px; padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.ontology-preview-card > div:first-child { display: flex; flex-direction: column; gap: 2px; }
.ontology-preview-card > div:first-child strong { color: var(--text); font-size: 11px; }
.ontology-preview-card > div:first-child span { color: var(--text-3); font-size: 10px; }
.ontology-property-list { display: flex; flex-wrap: wrap; gap: 5px; }
.ontology-property-list > span { display: inline-flex; align-items: center; gap: 4px; padding: 4px 6px; border-radius: 6px; color: var(--text-3); background: var(--surface-2); font-size: 9.5px; }
.ontology-property-list b { color: var(--text-2); }
.ontology-property-list em { padding: 1px 4px; border-radius: 999px; color: var(--primary-600); background: var(--primary-soft); font-size: 8px; font-style: normal; }
.relation-preview-row { display: grid; grid-template-columns: minmax(80px, .8fr) minmax(120px, 1.2fr) auto; align-items: center; gap: 7px; padding: 7px 8px; border-radius: 7px; background: var(--surface); font-size: 10px; }
.relation-preview-row span { color: var(--text-3); overflow-wrap: anywhere; }
.proposal-empty { color: var(--warning); line-height: 1.5; }
.mapping-preview-list, .workflow-preview-list { display: grid; gap: 5px; }
.mapping-preview-list > div, .workflow-preview-list > div { display: grid; grid-template-columns: minmax(90px, 1fr) auto minmax(90px, 1fr); align-items: center; gap: 7px; padding: 7px 8px; border-radius: 7px; background: var(--surface); font-size: 10px; }
.mapping-preview-list > div span { color: var(--text-3); }
.workflow-preview-list > div { grid-template-columns: 22px minmax(0, 1fr) auto; }
.workflow-preview-list > div > span { display: grid; width: 20px; height: 20px; place-items: center; border-radius: 50%; color: var(--primary-600); background: var(--primary-soft); font-size: 9px; font-weight: 800; }
.proposal-actions { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-top: 1px solid var(--border); }
.proposal-hint { color: var(--text-3); font-size: 10.5px; }
.message-sources { display: grid; gap: 5px; margin-top: 8px; }
.sources-label { color: var(--text-3); font-size: 10.5px; }
.source-card {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-height: 42px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 9px;
  color: var(--text-2);
  background: var(--surface-2);
  font: inherit;
  text-align: left;
}
.source-card.is-clickable { cursor: pointer; }
.source-card.is-clickable:hover, .source-card.is-clickable:focus-visible { border-color: var(--primary); background: var(--primary-soft); outline: none; }
.source-card:disabled { opacity: 1; }
.source-mark { flex: 0 0 auto; padding: 2px 6px; border-radius: 5px; color: var(--primary-600); background: var(--primary-soft); font-size: 9px; font-weight: 800; }
.source-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 2px; }
.source-copy strong { overflow: hidden; color: var(--text-2); font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.source-copy small { color: var(--text-3); font-size: 9.5px; }
.question-list { display: flex; flex-direction: column; gap: 7px; margin-top: 8px; }
.question-card { display: flex; flex-direction: column; gap: 4px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 9px; color: var(--text-2); background: var(--surface-2); font-size: 11.5px; line-height: 1.5; }
.question-card b { color: var(--text); }
.question-card .el-button { align-self: flex-start; padding-left: 0; }
.answer-evidence, .assistant-action-preview { margin-top: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); color: var(--text-2); font-size: 11px; }
.answer-evidence > header, .assistant-action-preview > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 9px; }
.answer-evidence > header > span { display: inline-flex; align-items: center; gap: 5px; color: var(--text); font-weight: 750; }
.evidence-meta-grid { display: grid; gap: 6px; margin-top: 8px; }
.evidence-meta-grid > div { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 7px; }
.evidence-meta-grid b, .evidence-uncertainties b { color: var(--text-3); font-size: 10px; }
.evidence-meta-grid span { overflow-wrap: anywhere; line-height: 1.5; }
.evidence-uncertainties { margin-top: 7px; padding-top: 7px; border-top: 1px dashed var(--border); }
.evidence-uncertainties ul { margin: 3px 0 0; padding-left: 16px; color: var(--warning); }
.assistant-action-preview header > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.assistant-action-preview header b { color: var(--text); font-size: 12px; }
.assistant-action-preview dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; margin: 9px 0 0; }
.assistant-action-preview dl div { min-width: 0; padding: 6px; border-radius: 7px; background: var(--surface); }
.assistant-action-preview dt { color: var(--text-3); font-size: 9px; }
.assistant-action-preview dd { margin: 2px 0 0; overflow-wrap: anywhere; color: var(--text); font-size: 10px; }
.assistant-action-preview p { margin: 8px 0 0; line-height: 1.5; }
.action-preview-params { max-height: 150px; margin: 8px 0 0; padding: 8px; overflow: auto; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); }
.assistant-action-next { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border); }
.assistant-action-next span { min-width: 0; color: var(--text-3); line-height: 1.45; }
.assistant-action-next .el-button { flex: 0 0 auto; }
.question-options { display: grid; gap: 6px; margin-top: 4px; }
.question-options button { display: grid; min-height: 48px; gap: 3px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--text-2); font: inherit; text-align: left; cursor: pointer; transition: border-color var(--dur), background var(--dur); }
.question-options button:hover, .question-options button:focus-visible { border-color: var(--primary); background: var(--primary-soft); outline: none; }
.question-options button > span { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.question-options strong { color: var(--text); font-size: 11.5px; }
.question-options em { padding: 1px 5px; border-radius: 999px; background: var(--primary-soft); color: var(--primary-600); font-size: 9px; font-style: normal; font-weight: 750; }
.question-options small { color: var(--text-3); font-size: 10.5px; line-height: 1.45; }
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
.temporary-context-note { display: flex; align-items: center; gap: 5px; color: var(--text-3); font-size: 10px; line-height: 1.45; }
.composer-tools { display: flex; align-items: center; gap: 7px; min-height: 32px; margin-bottom: 6px; }
.tool-button { display: inline-flex; align-items: center; gap: 4px; min-height: 30px; padding: 0 8px; border: 1px solid var(--border); border-radius: 7px; color: var(--text-2); background: var(--surface); cursor: pointer; font-size: 11.5px; }
.tool-button:hover, .tool-button:focus-within { border-color: var(--primary); color: var(--primary-600); background: var(--primary-soft); }
.tool-button.disabled { cursor: wait; opacity: .72; }
.tool-button input { display: none; }
.assistant-mode-switch { display: grid; min-width: 0; flex: 1; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 3px; padding: 3px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.assistant-mode-switch button { min-width: 0; min-height: 28px; padding: 3px 5px; overflow: hidden; border: 0; border-radius: 6px; background: transparent; color: var(--text-3); font: inherit; font-size: 10px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.assistant-mode-switch button:hover { color: var(--text); background: var(--surface); }
.assistant-mode-switch button.active { background: var(--primary); color: #fff; box-shadow: var(--shadow-xs); }
.composer-mode-hint { display: flex; min-height: 22px; align-items: center; gap: 6px; margin-bottom: 5px; overflow: hidden; color: var(--text-3); font-size: 10px; }
.composer-mode-hint b { flex: 0 0 auto; color: var(--primary-600); }
.composer-mode-hint span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-input-row { display: flex; align-items: flex-end; gap: 8px; }
.composer-input-row :deep(.el-textarea__inner) { min-height: 74px !important; padding-right: 12px; }
.send-button { width: 42px; height: 42px; padding: 0; flex: 0 0 auto; }
.source-preview { min-height: 260px; }
.source-preview-meta { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
.source-preview-meta > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.source-preview-meta strong { overflow: hidden; color: var(--text); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.source-preview-meta span { color: var(--text-3); font-size: 10px; }
.source-preview-text { min-height: 170px; max-height: 50vh; margin: 12px 0 0; padding: 14px; overflow: auto; border-radius: 10px; color: var(--text-2); background: var(--surface-2); font: 12px/1.75 'Cascadia Code', Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }

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
  .composer-mode-hint span { display: none; }
  .tool-button span { display: none; }
  .assistant-action-preview dl { grid-template-columns: 1fr; }
  .assistant-action-next { align-items: stretch; flex-direction: column; }
  .assistant-action-next .el-button { width: 100%; min-height: 44px; }
}
</style>
