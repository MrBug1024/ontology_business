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
            <div class="assistant-subtitle">理解业务资料，生成完整模型并受控执行</div>
          </div>
        </div>
        <div class="assistant-head-actions">
          <el-popover placement="bottom-end" :width="340" trigger="click" @show="loadAssistantCapabilities">
            <template #reference>
              <el-button text circle aria-label="配置助手能力" title="配置助手能力">
                <el-icon aria-hidden="true"><Setting /></el-icon>
              </el-button>
            </template>
            <section class="assistant-settings" aria-label="助手能力配置">
              <div class="assistant-settings-head">
                <div><strong>模型与参考配置</strong><span>选择本次请求使用的模型及可参考能力</span></div>
                <el-icon v-if="capabilitiesLoading" class="is-loading" aria-hidden="true"><Loading /></el-icon>
              </div>
              <label class="assistant-setting-label" for="assistant-llm">AI 模型</label>
              <el-select id="assistant-llm" v-model="assistantConfig.llmConfigId" class="assistant-setting-control" clearable placeholder="自动选择默认模型" @change="persistAssistantConfig">
                <el-option v-for="llm in assistantLLMs" :key="llm.id" :label="`${llm.name} · ${llm.model}`" :value="llm.id" />
              </el-select>
              <label class="assistant-setting-label" for="assistant-skills">参考技能</label>
              <el-select id="assistant-skills" v-model="assistantConfig.skillIds" class="assistant-setting-control" multiple collapse-tags collapse-tags-tooltip placeholder="不指定技能" @change="persistAssistantConfig">
                <el-option v-for="skill in assistantSkills" :key="skill.id" :label="skill.name" :value="skill.id" />
              </el-select>
              <label class="assistant-setting-label" for="assistant-mcps">参考 MCP</label>
              <el-select id="assistant-mcps" v-model="assistantConfig.mcpIds" class="assistant-setting-control" multiple collapse-tags collapse-tags-tooltip placeholder="不指定 MCP" @change="persistAssistantConfig">
                <el-option v-for="mcp in assistantMCPs" :key="mcp.id" :label="mcp.name" :value="mcp.id" />
              </el-select>
              <p class="assistant-settings-note">这里选择的是模型参考配置；只有平台为本轮明确注册工具时才会调用。外部调用、写入和高风险操作仍遵循权限、预演与确认流程。</p>
            </section>
          </el-popover>
          <el-button text circle aria-label="关闭 AI 助手" title="关闭" @click="visible = false">
            <el-icon aria-hidden="true"><Close /></el-icon>
          </el-button>
        </div>
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

      <section
        v-if="activeCompilationJob && compilationRecoveryThreadId === threadId"
        class="compilation-recovery"
        :class="`is-${activeCompilationJob.status}`"
        role="status"
        aria-live="polite"
      >
        <el-icon v-if="activeCompilationJob.status !== 'failed'" class="is-loading" aria-hidden="true"><Loading /></el-icon>
        <el-icon v-else aria-hidden="true"><WarningFilled /></el-icon>
        <div>
          <strong>{{ activeCompilationJob.status === 'running' ? '正在恢复完整业务模型编译' : activeCompilationJob.status === 'succeeded' ? '正在恢复完整业务模型结果' : '完整业务模型编译未完成' }}</strong>
          <span v-if="activeCompilationJob.status !== 'failed'">{{ activeCompilationJob.status === 'succeeded' ? '服务端任务已完成，正在读取持久化变更清单并重载会话。' : (activeCompilationJob.progress?.detail || '任务仍在服务端运行；页面恢复不会重新提交聊天请求。') }}</span>
          <span v-else>{{ activeCompilationJob.error_message || '系统已保持零写入。请修改附件或描述后显式重试。' }}</span>
        </div>
        <el-tag v-if="activeCompilationJob.status === 'running'" size="small" type="info" effect="plain">{{ compilationCallSummary }}</el-tag>
        <el-tag v-else-if="activeCompilationJob.status === 'succeeded'" size="small" type="success" effect="plain">正在恢复结果</el-tag>
        <el-button v-else size="small" text type="danger" @click="dismissCompilationFailure">知道了</el-button>
      </section>

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
        <div v-if="showQuickStarts" class="assistant-empty">
          <div class="empty-mark" aria-hidden="true"><el-icon :size="28"><ChatDotRound /></el-icon></div>
          <h3>告诉我你要建设什么业务场景</h3>
          <p>我会结合当前本体、业务说明和附件，生成带来源证据、冲突检查与变更清单的建模结果。</p>
          <div v-if="context.scenario_id" class="assistant-quick-start" aria-label="常用建模任务">
            <button type="button" @click="chooseTask('scenario_model')"><strong>完整场景建模</strong><span>对象、关系、映射、函数、操作、规则、事件和工作流</span></button>
            <button type="button" @click="chooseTask('ontology')"><strong>本体模型</strong><span>对象类型、属性、关系与约束</span></button>
            <button type="button" @click="chooseTask('capabilities')"><strong>新增业务能力</strong><span>新增函数、操作、规则和事件；修改已有定义时仅提供只读指导</span></button>
            <button type="button" @click="chooseTask('workflow')"><strong>工作流</strong><span>触发条件、节点、分支与审批</span></button>
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
                <el-tag size="small" :type="proposalOf(message)?.status === 'applied' || proposalOf(message)?.status === 'partially_applied' ? 'success' : 'warning'" effect="plain">
                  {{ proposalOf(message)?.status === 'applied' ? '已应用' : proposalOf(message)?.status === 'partially_applied' ? '已应用可用部分' : '待确认' }}
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
                <template v-else-if="proposalOf(message)?.kind === 'scenario_model'">
                  <span>来源段落 {{ proposalOf(message)?.payload?.coverage_summary?.total || 0 }}</span>
                      <span>阻塞 {{ blockingIssues(proposalOf(message)).length }}</span>
                </template>
                <span v-if="proposalOf(message)?.changes?.length">差异 {{ proposalOf(message)?.changes?.length }}</span>
                <button
                  type="button"
                  class="preview-toggle"
                  :aria-expanded="Boolean(expandedProposal[index])"
                  :aria-controls="proposalDetailId(message, index)"
                  @click="toggleProposal(index)"
                >{{ expandedProposal[index] ? '收起详情' : '查看详情' }}</button>
              </div>
              <section
                v-if="expandedProposal[index] && proposalOf(message)?.kind === 'scenario_model' && proposalOf(message)?.changes?.length"
                class="proposal-disclosure proposal-change-disclosure"
                aria-label="变更清单"
              >
                <button
                  type="button"
                  class="proposal-disclosure-toggle"
                  :aria-expanded="isChangeListExpanded(message, index)"
                  :aria-controls="proposalChangesId(message, index)"
                  @click="toggleChangeList(message, index)"
                >
                  <span class="disclosure-copy"><strong>变更清单</strong><small>共 {{ proposalOf(message)?.changes?.length || 0 }} 项，默认收起以便先处理预检问题</small></span>
                  <span class="disclosure-action">{{ isChangeListExpanded(message, index) ? '收起' : '展开全部' }}<el-icon class="disclosure-chevron" :class="{ rotated: isChangeListExpanded(message, index) }" aria-hidden="true"><ArrowDown /></el-icon></span>
                </button>
                <div v-show="isChangeListExpanded(message, index)" :id="proposalChangesId(message, index)" class="proposal-changes" aria-label="变更清单差异">
                  <div v-for="(change, changeIndex) in proposalOf(message)?.changes" :key="change.change_id || `${change.resource}-${change.name}-${changeIndex}`" class="proposal-change">
                    <el-tag size="small" effect="plain" :type="proposalOperationType(change.operation)">{{ proposalOperationLabel(change.operation) }}</el-tag>
                    <div class="proposal-change-copy">
                      <strong>{{ proposalResourceLabel(change.resource) }} · {{ change.name }}</strong>
                      <span>{{ change.summary }}</span>
                    </div>
                  </div>
                </div>
              </section>
              <div v-else-if="expandedProposal[index] && proposalOf(message)?.changes?.length" class="proposal-changes" aria-label="变更清单差异">
                <div v-for="(change, changeIndex) in proposalOf(message)?.changes" :key="`${change.resource}-${change.name}-${changeIndex}`" class="proposal-change">
                  <el-tag size="small" effect="plain" :type="proposalOperationType(change.operation)">{{ proposalOperationLabel(change.operation) }}</el-tag>
                  <div class="proposal-change-copy">
                    <strong>{{ proposalResourceLabel(change.resource) }} · {{ change.name }}</strong>
                    <span>{{ change.summary }}</span>
                  </div>
                </div>
              </div>
              <div v-if="expandedProposal[index]" :id="proposalDetailId(message, index)" class="proposal-detail" aria-label="草稿结构化内容">
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
                <template v-else-if="proposalOf(message)?.kind === 'scenario_model'">
                  <section class="proposal-section compound-overview">
                    <h4>完整业务模型</h4>
                    <dl class="proposal-summary-grid">
                      <div><dt>对象类型</dt><dd>{{ proposalOf(message)?.payload?.entities?.length || 0 }}</dd></div>
                      <div><dt>关系类型</dt><dd>{{ proposalOf(message)?.payload?.relations?.length || 0 }}</dd></div>
                      <div><dt>函数 / 操作</dt><dd>{{ (proposalOf(message)?.payload?.functions?.length || 0) + (proposalOf(message)?.payload?.actions?.length || 0) }}</dd></div>
                      <div><dt>规则 / 事件</dt><dd>{{ (proposalOf(message)?.payload?.rules?.length || 0) + (proposalOf(message)?.payload?.events?.length || 0) }}</dd></div>
                      <div><dt>工作流</dt><dd>{{ proposalOf(message)?.payload?.workflows?.length || 0 }}</dd></div>
                      <div><dt>数据映射</dt><dd>{{ proposalOf(message)?.payload?.mappings?.length || 0 }}</dd></div>
                    </dl>
                    <div class="coverage-summary">
                      <span>全文 {{ proposalOf(message)?.payload?.coverage_summary?.total || 0 }} 段</span>
                      <span>已建模 {{ proposalOf(message)?.payload?.coverage_summary?.modeled || 0 }}</span>
                      <span>背景 {{ proposalOf(message)?.payload?.coverage_summary?.context || 0 }}</span>
                      <span>无关 {{ proposalOf(message)?.payload?.coverage_summary?.irrelevant || 0 }}</span>
                      <span :class="{ danger: (proposalOf(message)?.payload?.coverage_summary?.ambiguous || 0) > 0 }">歧义 {{ proposalOf(message)?.payload?.coverage_summary?.ambiguous || 0 }}</span>
                    </div>
                  </section>
                  <section v-for="group in compoundReviewGroups(proposalOf(message))" :key="group.key" class="proposal-section compound-resource-group">
                    <h4>{{ group.label }} <span>{{ group.items.length }}</span></h4>
                    <div class="compound-resource-list">
                      <article v-for="item in group.items" :key="item.key || item.name" class="compound-resource-card">
                        <header>
                          <strong>{{ item.name || item.key || '未命名定义' }}</strong>
                          <div>
                            <el-tag size="small" effect="plain" :type="compoundOperationType(item, proposalOf(message))">{{ compoundOperationLabel(item, proposalOf(message)) }}</el-tag>
                            <el-tag size="small" effect="plain">置信度 {{ confidencePercent(item.confidence) }}</el-tag>
                          </div>
                        </header>
                        <p>{{ compoundResourceSummary(group.key, item) }}</p>
                        <footer v-if="item.evidence_refs?.length"><span>来源</span><b>{{ item.evidence_refs.join('、') }}</b></footer>
                      </article>
                    </div>
                  </section>
                  <section v-if="proposalOf(message)?.payload?.source_manifest?.length" class="proposal-section">
                    <h4>来源文档</h4>
                    <div class="source-manifest-list">
                      <div v-for="source in proposalOf(message)?.payload?.source_manifest || []" :key="source.source_id">
                        <strong>{{ source.filename }}</strong><span>{{ source.paragraph_count }} 段 · {{ source.characters }} 字符</span>
                      </div>
                    </div>
                  </section>
                  <section v-if="scenarioIssueGroups(proposalOf(message)).length" class="proposal-section unresolved-section">
                    <header class="unresolved-head">
                      <div><h4>预检问题</h4><p>阻塞定义会跳过不写入；解决建议已列在明细中，后续补充资料后重新编译即可补齐。</p></div>
                      <div class="unresolved-counts" aria-label="预检问题统计">
                        <span class="is-blocking">阻塞 {{ blockingIssues(proposalOf(message)).length }}</span>
                        <span>提示 {{ nonBlockingIssueCount(proposalOf(message)) }}</span>
                      </div>
                    </header>
                    <div class="issue-groups">
                      <div class="issue-category-head is-blocking">
                        <strong>阻塞问题</strong>
                        <span>{{ blockingScenarioIssueGroups(proposalOf(message)).length }} 类 · {{ blockingIssues(proposalOf(message)).length }} 项 · 本次将跳过</span>
                      </div>
                      <section
                        v-for="group in blockingScenarioIssueGroups(proposalOf(message))"
                        :key="group.key"
                        class="issue-group"
                        :class="group.blocking ? 'is-blocking' : 'is-notice'"
                      >
                        <button
                          type="button"
                          class="issue-group-toggle"
                          :aria-expanded="isIssueGroupExpanded(message, index, group.key)"
                          :aria-controls="issueGroupId(message, index, group.key)"
                          @click="toggleIssueGroup(message, index, group.key)"
                        >
                          <span class="issue-severity">{{ group.blocking ? '阻塞' : '提示' }}</span>
                          <span class="issue-group-copy">
                            <strong>{{ scenarioModelIssueLabel(group.code) }}</strong>
                            <small><template v-if="scenarioModelIssueLabel(group.code) === '其他预检问题'">{{ group.code }} · </template>{{ group.issues.length }} 项</small>
                          </span>
                          <el-icon class="disclosure-chevron" :class="{ rotated: isIssueGroupExpanded(message, index, group.key) }" aria-hidden="true"><ArrowDown /></el-icon>
                        </button>
                        <div
                          v-show="isIssueGroupExpanded(message, index, group.key)"
                          :id="issueGroupId(message, index, group.key)"
                          class="issue-group-details"
                          role="region"
                          :aria-label="`${scenarioModelIssueLabel(group.code)}明细`"
                        >
                            <article v-for="(issue, issueIndex) in group.issues" :key="`${group.key}-${issueIndex}-${issue.message}`" class="unresolved-row">
                              <span class="issue-number" aria-hidden="true">{{ issueIndex + 1 }}</span>
                            <div><strong>{{ issue.message }}</strong><span v-if="issue.sourceRefs.length">来源：{{ issue.sourceRefs.join('、') }}</span><span v-if="issue.resolutionHint" class="issue-resolution">解决：{{ issue.resolutionHint }}</span></div>
                          </article>
                        </div>
                      </section>
                      <section v-if="nonBlockingScenarioIssueGroups(proposalOf(message)).length" class="issue-notice-category">
                        <button
                          type="button"
                          class="issue-category-toggle"
                          :aria-expanded="isNoticeCategoryExpanded(message, index)"
                          :aria-controls="noticeCategoryId(message, index)"
                          @click="toggleNoticeCategory(message, index)"
                        >
                          <span class="issue-severity">提示</span>
                          <span class="issue-group-copy">
                            <strong>非阻塞提示</strong>
                            <small>{{ nonBlockingScenarioIssueGroups(proposalOf(message)).length }} 类 · {{ nonBlockingIssueCount(proposalOf(message)) }} 项 · 不影响应用</small>
                          </span>
                          <el-icon class="disclosure-chevron" :class="{ rotated: isNoticeCategoryExpanded(message, index) }" aria-hidden="true"><ArrowDown /></el-icon>
                        </button>
                        <div v-show="isNoticeCategoryExpanded(message, index)" :id="noticeCategoryId(message, index)" class="issue-notice-groups">
                          <section
                            v-for="group in nonBlockingScenarioIssueGroups(proposalOf(message))"
                            :key="group.key"
                            class="issue-group is-notice"
                          >
                            <button
                              type="button"
                              class="issue-group-toggle"
                              :aria-expanded="isIssueGroupExpanded(message, index, group.key)"
                              :aria-controls="issueGroupId(message, index, group.key)"
                              @click="toggleIssueGroup(message, index, group.key)"
                            >
                              <span class="issue-severity">提示</span>
                              <span class="issue-group-copy">
                                <strong>{{ scenarioModelIssueLabel(group.code) }}</strong>
                                <small><template v-if="scenarioModelIssueLabel(group.code) === '其他预检问题'">{{ group.code }} · </template>{{ group.issues.length }} 项</small>
                              </span>
                              <el-icon class="disclosure-chevron" :class="{ rotated: isIssueGroupExpanded(message, index, group.key) }" aria-hidden="true"><ArrowDown /></el-icon>
                            </button>
                            <div
                              v-show="isIssueGroupExpanded(message, index, group.key)"
                              :id="issueGroupId(message, index, group.key)"
                              class="issue-group-details"
                              role="region"
                              :aria-label="`${scenarioModelIssueLabel(group.code)}明细`"
                            >
                              <article v-for="(issue, issueIndex) in group.issues" :key="`${group.key}-${issueIndex}-${issue.message}`" class="unresolved-row">
                                <span class="issue-number" aria-hidden="true">{{ issueIndex + 1 }}</span>
                                <div><strong>{{ issue.message }}</strong><span v-if="issue.sourceRefs.length">来源：{{ issue.sourceRefs.join('、') }}</span><span v-if="issue.resolutionHint" class="issue-resolution">解决：{{ issue.resolutionHint }}</span></div>
                              </article>
                            </div>
                          </section>
                        </div>
                      </section>
                    </div>
                  </section>
                  <el-alert
                    v-if="blockingIssues(proposalOf(message)).length"
                    title="确认后只会原子写入通过依赖与结构预检的可用部分；阻塞项会保留在清单中，不会被强行写入。"
                    type="warning"
                    :closable="false"
                    show-icon
                  />
                  <el-alert
                    v-else
                    title="所有引用、冲突与来源段落已通过预检；确认后将在同一事务中应用，任一失败都会整体回滚。"
                    type="success"
                    :closable="false"
                    show-icon
                  />
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
                <el-button size="small" type="primary" :loading="applyingIndex === index" :disabled="!proposalCanApply(proposalOf(message)) || ['applied', 'partially_applied'].includes(proposalOf(message)?.status || '') || !proposalOf(message)?.proposal_id" @click="applyProposal(message, index)">
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
          <div class="task-preset-control">
            <label for="assistant-task-preset">任务</label>
            <el-select id="assistant-task-preset" v-model="taskPreset" size="small" aria-label="选择助手任务" @change="onTaskPresetChange">
              <el-option label="智能协助" value="smart" />
              <el-option v-if="!context.scenario_id" label="创建业务场景" value="scenario" />
              <el-option v-if="context.scenario_id" label="完整场景建模" value="scenario_model" />
              <el-option v-if="context.scenario_id" label="本体模型" value="ontology" />
              <el-option v-if="context.scenario_id" label="数据映射" value="mapping" />
              <el-option v-if="context.scenario_id" label="新增业务能力" value="capabilities" />
              <el-option v-if="context.scenario_id" label="工作流" value="workflow" />
              <el-option label="只读解释" value="explain" />
              <el-option v-if="context.scenario_id" label="操作安全预演" value="execute" />
            </el-select>
          </div>
          <span class="task-preset-hint">{{ taskPresetHint }}</span>
        </div>
        <div class="composer-input-row">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="12000"
            show-word-limit
            :placeholder="composerPlaceholder"
            @keydown.enter.exact.prevent="send()"
          />
          <el-button class="send-button" type="primary" :loading="loading || compilationBusy" :disabled="!canSend || uploadingFiles > 0 || compilationBusy" aria-label="发送消息" title="发送" @click="send()">
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
import { useAuthStore } from '@/stores/auth'
import type { AssistantActionPreview, AssistantAttachment, AssistantCompilationJobStatus, AssistantMessage, AssistantProposal, AssistantQuestion, AssistantSource, AssistantThread, AssistantThought, LLMConfig, MCPConfig, Skill } from '@/types'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import KeyValueEditor from '@/components/KeyValueEditor.vue'
import {
  clearCompilationJobBookmark,
  clearPendingCompilationJobBookmark,
  compilationJobMatchesScenario,
  compilationPollDelay,
  readCompilationJobBookmark,
  readPendingCompilationJobBookmark,
  saveCompilationJobBookmark,
  savePendingCompilationJobBookmark,
  selectCompilationJobForRecovery,
  type CompilationRecoveryOwnerScope,
  type CompilationRecoveryThreadScope,
} from '@/utils/assistantCompilationRecovery'
import { compilationRetryDraft, retryAttachmentsForMessage } from '@/utils/assistantRetry'
import { groupScenarioModelIssues, scenarioModelIssueLabel } from '@/utils/assistantProposalGroups'

interface AssistantContext {
  page?: string
  path?: string
  scenario_id?: string
}

const props = withDefaults(defineProps<{ context: AssistantContext; hideLauncher?: boolean }>(), {
  hideLauncher: false,
})
const router = useRouter()
const auth = useAuthStore()
type AssistantMode = 'ask' | 'explain' | 'draft' | 'apply' | 'execute'
type AssistantDraftKind = 'auto' | 'scenario' | 'ontology' | 'mapping' | 'workflow' | 'scenario_model'
type AssistantTaskPreset = 'smart' | 'scenario' | 'scenario_model' | 'ontology' | 'mapping' | 'capabilities' | 'workflow' | 'explain' | 'execute'
const visible = ref(false)
const loading = ref(false)
const input = ref('')
const mode = ref<AssistantMode>('ask')
const draftKind = ref<AssistantDraftKind>('auto')
const taskPreset = ref<AssistantTaskPreset>('smart')
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
const expandedChangeLists = reactive<Record<string, boolean>>({})
const expandedIssueGroups = reactive<Record<string, boolean>>({})
const expandedThinking = reactive<Record<string, boolean>>({})
const selection = reactive<{ label: string; kind: string; id: string }>({ label: '', kind: '', id: '' })
const assistantConfig = reactive<{ llmConfigId: string; skillIds: string[]; mcpIds: string[] }>({
  llmConfigId: '',
  skillIds: [],
  mcpIds: [],
})
const assistantLLMs = ref<LLMConfig[]>([])
const assistantSkills = ref<Skill[]>([])
const assistantMCPs = ref<MCPConfig[]>([])
const capabilitiesLoading = ref(false)
const capabilitiesLoaded = ref(false)
const activeCompilationJob = ref<AssistantCompilationJobStatus | null>(null)
const compilationRecoveryThreadId = ref('')
let compilationPollTimer: number | null = null
let compilationRecoveryEpoch = 0
let compilationPollErrors = 0
let streamGeneration = 0
let componentDisposed = false

const assistantConfigStorageKey = 'ontology-assistant-capabilities'

const context = computed(() => ({
  page: props.context.page || '工作台',
  path: props.context.path || '',
  scenario_id: props.context.scenario_id || '',
}))
const taskPresetHint = computed(() => ({
  smart: '自动判断问答或建模任务',
  scenario: '生成待确认的场景草稿',
  scenario_model: '一次生成跨资源原子变更清单',
  ontology: '只处理对象、属性、关系与约束',
  mapping: '只使用已检查的真实表和字段',
  capabilities: '仅新增业务能力；修改或删除将切换为只读指导',
  workflow: '生成可审核的流程草稿',
  explain: '只读分析，不生成变更',
  execute: '仅做权限与副作用预演',
} as Record<AssistantTaskPreset, string>)[taskPreset.value])
const composerPlaceholder = computed(() => ({
  scenario_model: '介绍业务目标、范围与关键规则，或直接添加业务文档后发送',
  capabilities: '描述要新增的计算、业务动作、判断规则和业务事件',
  ontology: '描述核心对象、属性、关系、基数和业务约束',
  mapping: '说明目标对象及期望映射；系统只会选择真实存在的表和字段',
  workflow: '描述触发条件、处理步骤、判断分支、动作与最终结果',
  explain: '询问当前业务、本体、映射或流程，我只做只读解释',
  execute: '说明要预演的已配置操作及参数，不会直接产生副作用',
  scenario: '描述场景名称、行业、业务目标和边界',
  smart: '描述你要完成的工作，助手会结合当前业务上下文自动判断',
} as Record<AssistantTaskPreset, string>)[taskPreset.value])
const canSend = computed(() => Boolean(
  input.value.trim()
  || (
    ['scenario', 'scenario_model', 'ontology', 'mapping', 'capabilities', 'workflow'].includes(taskPreset.value)
    && attachments.value.length
    && attachments.value.every((item) => item.status === 'parsed')
  )
))
const assistantScopeKey = computed(() => `${context.value.scenario_id || 'global'}|${(context.value.path || '/').split('?', 1)[0] || '/'}`)
const storageKey = computed(() => `ontology-assistant-thread:${encodeURIComponent(assistantScopeKey.value)}`)
const currentThread = computed(() => threads.value.find((thread) => thread.id === threadId.value))
const showQuickStarts = computed(() => !messages.value.length || (
  messages.value.length === 1
  && messages.value[0].role === 'assistant'
  && !messages.value[0].id
))
const hasStreamingAssistant = computed(() => messages.value.some((message) => message.role === 'assistant' && message.streaming))
const compilationRunning = computed(() => activeCompilationJob.value?.status === 'running'
  && compilationRecoveryThreadId.value === threadId.value)
const compilationBusy = computed(() => Boolean(activeCompilationJob.value)
  && activeCompilationJob.value?.status !== 'failed'
  && compilationRecoveryThreadId.value === threadId.value)
const compilationCallSummary = computed(() => {
  const job = activeCompilationJob.value
  if (!job || job.status !== 'running') return ''
  const used = Number(job.llm_calls_used ?? job.progress?.calls_used ?? 0)
  const budget = Number(job.llm_call_budget ?? job.progress?.call_budget ?? 0)
  return budget > 0 ? `模型调用 ${used}/${budget}` : '正在等待服务端进度'
})
// The Agent chat owns the bottom-right composer controls. A persistent global
// launcher must not cover its primary send action or keyboard focus target.
const showLauncher = computed(() => {
  if (props.hideLauncher) return false
  const path = context.value.path
  // Agent 对话页已有自己的输入区；其他业务页面统一使用全局浮动助手。
  return !/^\/agents\/[^/]+\/chat(?:\/|$|\?)/.test(path)
})
function proposalOf(message: AssistantMessage): AssistantProposal | null {
  const proposal = message.proposal as AssistantProposal | undefined
  return proposal && proposal.kind && proposal.payload ? proposal : null
}

function applyTaskPreset(preset: AssistantTaskPreset) {
  if (preset === 'explain') {
    mode.value = 'explain'
    draftKind.value = 'auto'
    return
  }
  if (preset === 'execute') {
    mode.value = 'execute'
    draftKind.value = 'auto'
    return
  }
  if (preset === 'smart') {
    mode.value = 'ask'
    draftKind.value = 'auto'
    return
  }
  mode.value = 'draft'
  draftKind.value = preset === 'capabilities' ? 'scenario_model' : preset
}

function onTaskPresetChange(value: AssistantTaskPreset) {
  applyTaskPreset(value)
}

function chooseTask(preset: AssistantTaskPreset) {
  taskPreset.value = preset
  applyTaskPreset(preset)
  if (input.value.trim()) return
  input.value = ({
    scenario_model: '请根据以下业务介绍和附件，完成当前场景的完整建模，并列出所有未识别、歧义和冲突项。\n\n业务介绍：',
    ontology: '请根据以下业务描述和附件，建立对象类型、属性、关系及约束。\n\n业务描述：',
    capabilities: '请根据以下业务描述和附件，新增所需函数、操作、规则和事件，并保持待审核状态。\n\n业务描述：',
    workflow: '请根据以下业务描述和附件，设计触发条件、处理节点、判断分支和审批流程。\n\n业务描述：',
  } as Partial<Record<AssistantTaskPreset, string>>)[preset] || ''
}

function restoreAssistantConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(assistantConfigStorageKey) || '{}') as Partial<typeof assistantConfig>
    assistantConfig.llmConfigId = String(saved.llmConfigId || '')
    assistantConfig.skillIds = Array.isArray(saved.skillIds) ? saved.skillIds.map(String) : []
    assistantConfig.mcpIds = Array.isArray(saved.mcpIds) ? saved.mcpIds.map(String) : []
  } catch {
    assistantConfig.llmConfigId = ''
    assistantConfig.skillIds = []
    assistantConfig.mcpIds = []
  }
}

function persistAssistantConfig() {
  localStorage.setItem(assistantConfigStorageKey, JSON.stringify(assistantConfig))
}

async function loadAssistantCapabilities() {
  if (capabilitiesLoaded.value || capabilitiesLoading.value) return
  capabilitiesLoading.value = true
  try {
    const [llms, skills, mcps] = await Promise.all([api.listLLM(), api.listSkills(), api.listMCP()])
    assistantLLMs.value = llms.filter((item) => item.enabled !== false && (!item.capabilities?.length || item.capabilities.includes('chat')))
    assistantSkills.value = skills.filter((item) => item.enabled)
    assistantMCPs.value = mcps.filter((item) => item.enabled !== false)
    if (assistantConfig.llmConfigId && !assistantLLMs.value.some((item) => item.id === assistantConfig.llmConfigId)) assistantConfig.llmConfigId = ''
    assistantConfig.skillIds = assistantConfig.skillIds.filter((id) => assistantSkills.value.some((item) => item.id === id))
    assistantConfig.mcpIds = assistantConfig.mcpIds.filter((id) => assistantMCPs.value.some((item) => item.id === id))
    persistAssistantConfig()
    capabilitiesLoaded.value = true
  } catch (error: any) {
    ElMessage.warning(error.message || '助手能力配置加载失败，将使用平台默认配置')
  } finally {
    capabilitiesLoading.value = false
  }
}

function proposalCanApply(proposal: AssistantProposal | null) {
  if (!proposal) return false
  if (proposal.kind === 'scenario_model' && !hasApplyableChanges(proposal)) return false
  return proposal.kind === 'scenario' ? !context.value.scenario_id : Boolean(context.value.scenario_id)
}

function hasEffectiveChanges(proposal: AssistantProposal | null) {
  return Boolean(proposal?.changes?.some((change) => change.operation !== 'skip'))
}

function hasApplyableChanges(proposal: AssistantProposal | null) {
  if (!proposal || proposal.kind !== 'scenario_model') return hasEffectiveChanges(proposal)
  const safeKeys = proposal.payload?.applyability?.safe_change_keys
  return Boolean(proposal.changes?.some((change) => {
    if (change.operation === 'skip') return false
    if (!Array.isArray(safeKeys)) return true
    const id = String(change.change_id || '')
    return safeKeys.some((key: unknown) => id === String(key) || id.startsWith(`${String(key)}:`))
  }))
}

function blockingIssues(proposal: AssistantProposal | null) {
  if (proposal?.kind !== 'scenario_model') return []
  const issues = proposal.payload?.unresolved
  return Array.isArray(issues) ? issues.filter((item) => item?.blocking !== false) : []
}

function scenarioIssueGroups(proposal: AssistantProposal | null) {
  if (proposal?.kind !== 'scenario_model') return []
  return groupScenarioModelIssues(proposal.payload?.unresolved)
}

function blockingScenarioIssueGroups(proposal: AssistantProposal | null) {
  return scenarioIssueGroups(proposal).filter((group) => group.blocking)
}

function nonBlockingScenarioIssueGroups(proposal: AssistantProposal | null) {
  return scenarioIssueGroups(proposal).filter((group) => !group.blocking)
}

function nonBlockingIssueCount(proposal: AssistantProposal | null) {
  return nonBlockingScenarioIssueGroups(proposal)
    .reduce((total, group) => total + group.issues.length, 0)
}

function proposalApplyLabel(proposal: AssistantProposal | null) {
  if (!proposal) return '确认并应用变更'
  if (proposal.status === 'applied') return proposal.kind === 'scenario' ? '场景已创建' : '变更已应用'
  if (proposal.status === 'partially_applied') return '已应用可用部分'
  if (proposal.kind === 'scenario_model' && blockingIssues(proposal).length) return '确认并应用可用部分'
  return ({ scenario: '确认并创建场景', mapping: '确认并保存映射', ontology: '确认并应用本体', workflow: '确认并保存流程', scenario_model: '确认并原子应用' } as Record<string, string>)[proposal.kind] || '确认并应用变更'
}

function proposalApplyHint(proposal: AssistantProposal | null) {
  if (!proposal?.proposal_id) return '此草稿缺少安全标识，请重新生成'
  if (proposal.kind === 'scenario' && context.value.scenario_id) return '场景草稿只能在全局工作区创建'
  if (proposal.kind !== 'scenario' && !context.value.scenario_id) return '请先打开业务场景'
  if (proposal.kind === 'scenario_model' && blockingIssues(proposal).length) {
    const safeCount = proposal.payload?.applyability?.safe_change_count
    const countText = typeof safeCount === 'number' ? `，当前可应用 ${safeCount} 项` : ''
    return `仍有 ${blockingIssues(proposal).length} 个阻塞项；确认后将跳过受影响定义，只写入安全部分${countText}。解决阻塞项后重新编译即可补齐`
  }
  if (proposal.kind === 'scenario_model' && !hasApplyableChanges(proposal)) return '当前没有可独立应用的业务模型变更，请先补充文档后重新编译'
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
    function: '函数', action: '操作', rule: '规则', event: '事件', workflow: '工作流', workflow_node: '工作流节点', workflow_edge: '工作流连线',
  } as Record<string, string>)[resource] || resource
}

function compoundReviewGroups(proposal: AssistantProposal | null) {
  if (proposal?.kind !== 'scenario_model') return []
  const payload = proposal.payload || {}
  return [
    ['entities', '对象类型'], ['relations', '关系类型'], ['functions', '函数'],
    ['actions', '操作'], ['rules', '规则'], ['events', '事件'],
    ['workflows', '工作流'], ['mappings', '对象数据映射'],
    ['relation_mappings', '关系数据映射'],
  ].map(([key, label]) => ({ key, label, items: Array.isArray(payload[key]) ? payload[key] : [] }))
    .filter((group) => group.items.length)
}

function compoundReferenceLabel(reference: any) {
  if (!reference || typeof reference !== 'object') return '待确认定义'
  const displayName = String(reference.display_name || '').trim()
  if (displayName) return displayName
  if (reference.kind === 'generated') return String(reference.key || '本次生成定义')
  return String(reference.id || '场景已有定义')
}

function compoundOperation(item: any) {
  return String(item?.operation || (item?.existing_id ? 'update' : 'add'))
}

function compoundOperationLabel(item: any, proposal: AssistantProposal | null) {
  if (proposal?.kind === 'scenario_model' && isCompoundItemBlocked(item, proposal)) return '待补全后应用'
  return proposalOperationLabel(compoundOperation(item))
}

function compoundOperationType(item: any, proposal: AssistantProposal | null) {
  if (proposal?.kind === 'scenario_model' && isCompoundItemBlocked(item, proposal)) return 'warning'
  return proposalOperationType(compoundOperation(item))
}

function isCompoundItemBlocked(item: any, proposal: AssistantProposal | null) {
  if (proposal?.kind !== 'scenario_model') return false
  const blockedKeys = proposal.payload?.applyability?.blocked_change_keys
  if (!Array.isArray(blockedKeys)) return false
  const key = String(item?.key || '')
  return blockedKeys.some((value: unknown) => {
    const candidate = String(value)
    return candidate === key || candidate.startsWith(`${key}:`)
  })
}

function schemaFieldNames(schema: any) {
  const names = Object.keys(schema?.properties || {})
  return names.length ? names.slice(0, 8).join('、') + (names.length > 8 ? ` 等 ${names.length} 项` : '') : '无字段'
}

function compoundResourceSummary(section: string, item: any) {
  if (section === 'entities') {
    const properties = (item.properties || []).map((property: any) => property.name).filter(Boolean)
    return `${item.is_abstract ? '抽象对象' : '业务对象'}；属性：${properties.slice(0, 10).join('、') || '无'}${properties.length > 10 ? ` 等 ${properties.length} 项` : ''}`
  }
  if (section === 'relations') return `${compoundReferenceLabel(item.source)} → ${compoundReferenceLabel(item.target)}；基数 ${item.relation_type || '待确认'}`
  if (section === 'functions') return `输入：${schemaFieldNames(item.input_schema)}；输出：${schemaFieldNames(item.output_schema)}`
  if (section === 'actions') return `作用对象：${compoundReferenceLabel(item.entity)}；输入：${schemaFieldNames(item.input_schema)}；保存后保持待绑定和停用`
  if (section === 'rules') return `作用对象：${item.entity ? compoundReferenceLabel(item.entity) : '场景级'}；级别 ${item.severity || 'info'}；保存后保持停用`
  if (section === 'events') return `载荷：${schemaFieldNames(item.payload_schema)}；来源：${item.trigger_source || '待确认'}`
  if (section === 'workflows') return `${item.trigger_type || 'manual'} 触发；${item.nodes?.length || 0} 个节点，${item.edges?.length || 0} 条连线；保存后为停用草稿`
  if (section === 'mappings') return `${compoundReferenceLabel(item.entity)} ← ${item.table_name || '待确认表'}；${Object.keys(item.column_map || {}).length} 个字段映射`
  if (section === 'relation_mappings') return `${item.mode || '待确认模式'}；关联两端对象映射与关系定义`
  return item.description || '待审核业务定义'
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

function stableDomToken(value: string) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function proposalExpansionKey(message: AssistantMessage, index: number) {
  return messageKey(message, index)
}

function proposalDetailId(message: AssistantMessage, index: number) {
  return `proposal-detail-${stableDomToken(proposalExpansionKey(message, index))}`
}

function proposalChangesId(message: AssistantMessage, index: number) {
  return `proposal-changes-${stableDomToken(proposalExpansionKey(message, index))}`
}

function isChangeListExpanded(message: AssistantMessage, index: number) {
  return Boolean(expandedChangeLists[proposalExpansionKey(message, index)])
}

function toggleChangeList(message: AssistantMessage, index: number) {
  const key = proposalExpansionKey(message, index)
  expandedChangeLists[key] = !expandedChangeLists[key]
}

function issueExpansionKey(message: AssistantMessage, index: number, groupKey: string) {
  return `${proposalExpansionKey(message, index)}::${groupKey}`
}

function issueGroupId(message: AssistantMessage, index: number, groupKey: string) {
  return `proposal-issues-${stableDomToken(issueExpansionKey(message, index, groupKey))}`
}

function noticeCategoryKey(message: AssistantMessage, index: number) {
  return `${proposalExpansionKey(message, index)}::__notices__`
}

function noticeCategoryId(message: AssistantMessage, index: number) {
  return `proposal-notices-${stableDomToken(noticeCategoryKey(message, index))}`
}

function isNoticeCategoryExpanded(message: AssistantMessage, index: number) {
  return Boolean(expandedIssueGroups[noticeCategoryKey(message, index)])
}

function toggleNoticeCategory(message: AssistantMessage, index: number) {
  const key = noticeCategoryKey(message, index)
  expandedIssueGroups[key] = !expandedIssueGroups[key]
}

function isIssueGroupExpanded(message: AssistantMessage, index: number, groupKey: string) {
  return Boolean(expandedIssueGroups[issueExpansionKey(message, index, groupKey)])
}

function toggleIssueGroup(message: AssistantMessage, index: number, groupKey: string) {
  const key = issueExpansionKey(message, index, groupKey)
  expandedIssueGroups[key] = !expandedIssueGroups[key]
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

function compilationOwnerScope(): CompilationRecoveryOwnerScope | null {
  const tenantId = String(auth.user?.tenant_id || '').trim()
  const userId = String(auth.user?.id || '').trim()
  const scenarioId = String(context.value.scenario_id || '').trim()
  return tenantId && userId && scenarioId ? { tenantId, userId, scenarioId } : null
}

function compilationThreadScope(id = threadId.value): CompilationRecoveryThreadScope | null {
  const owner = compilationOwnerScope()
  const normalizedThreadId = String(id || '').trim()
  return owner && normalizedThreadId ? { ...owner, threadId: normalizedThreadId } : null
}

function clearCompilationPollTimer() {
  if (compilationPollTimer) window.clearTimeout(compilationPollTimer)
  compilationPollTimer = null
}

function detachCompilationRecovery(clearStatus = true) {
  clearCompilationPollTimer()
  compilationRecoveryEpoch += 1
  compilationPollErrors = 0
  compilationRecoveryThreadId.value = ''
  if (clearStatus) activeCompilationJob.value = null
}

function compilationRecoveryIsCurrent(epoch: number, scope: CompilationRecoveryThreadScope) {
  const owner = compilationOwnerScope()
  return !componentDisposed
    && epoch === compilationRecoveryEpoch
    && Boolean(owner)
    && owner?.tenantId === scope.tenantId
    && owner?.userId === scope.userId
    && owner?.scenarioId === scope.scenarioId
    && threadId.value === scope.threadId
    && compilationRecoveryThreadId.value === scope.threadId
}

function dismissCompilationFailure() {
  if (activeCompilationJob.value?.status !== 'failed') return
  activeCompilationJob.value = null
  compilationRecoveryThreadId.value = ''
}

function compilationProgressDetail(job: AssistantCompilationJobStatus) {
  const detail = String(job.progress?.detail || '').trim()
  if (detail) return detail
  if (job.status === 'running') return '任务仍在服务端运行；恢复过程只查询状态，不会重新提交聊天请求。'
  if (job.status === 'succeeded') return '服务端任务已完成，正在恢复持久化变更清单。'
  return job.error_message || '系统已保持零写入。请修改附件或描述后显式重试。'
}

function markCompilationMessage(job: AssistantCompilationJobStatus, scope: CompilationRecoveryThreadScope) {
  let index = messages.value.findIndex((message) => message.context?.compilation_job_id === job.id)
  if (index < 0 && job.status === 'running') {
    messages.value.push({
      id: `compilation-recovery-${job.id}`,
      thread_id: scope.threadId,
      role: 'assistant',
      content: '正在恢复完整业务模型编译；不会重复提交附件或聊天请求。',
      context: { compilation_job_id: job.id, status: 'processing' },
      thinking: [],
      streaming: true,
    })
    index = messages.value.length - 1
  }
  if (index < 0) return
  const message = messages.value[index]
  message.streaming = job.status !== 'failed'
  message.context = {
    ...(message.context || {}),
    compilation_job_id: job.id,
    status: job.status === 'running' ? 'processing' : job.status,
  }
  upsertThinking(message, {
    id: 'scenario-model',
    title: job.status === 'running' ? '编译完整业务模型' : job.status === 'succeeded' ? '完整业务模型已编译' : '完整业务模型编译未完成',
    detail: compilationProgressDetail(job),
    status: job.status === 'running' ? 'running' : job.status === 'succeeded' ? 'done' : 'error',
  }, index)
  scrollBottom()
}

function scheduleCompilationPoll(jobId: string, epoch: number, scope: CompilationRecoveryThreadScope) {
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  clearCompilationPollTimer()
  compilationPollTimer = window.setTimeout(
    () => { void pollCompilationJob(jobId, epoch, scope) },
    compilationPollDelay(document.hidden, compilationPollErrors),
  )
}

async function pollCompilationJob(jobId: string, epoch: number, scope: CompilationRecoveryThreadScope) {
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  compilationPollTimer = null
  try {
    const job = await api.getAssistantCompilationJob(jobId)
    if (!compilationRecoveryIsCurrent(epoch, scope)) return
    compilationPollErrors = 0
    await consumeCompilationJob(job, epoch, scope)
  } catch (error: any) {
    if (!compilationRecoveryIsCurrent(epoch, scope)) return
    if (error?.status === 403 || error?.status === 404) {
      clearCompilationJobBookmark(localStorage, scope)
      activeCompilationJob.value = null
      compilationRecoveryThreadId.value = ''
      ElMessage.warning('当前账号或场景已无法访问这项编译任务，已停止恢复。')
      return
    }
    compilationPollErrors += 1
    if (activeCompilationJob.value?.status === 'running') {
      activeCompilationJob.value = {
        ...activeCompilationJob.value,
        progress: {
          ...(activeCompilationJob.value.progress || {}),
          detail: '暂时无法刷新任务进度；系统不会重新提交请求，将继续安全重试状态查询。',
        },
      }
      markCompilationMessage(activeCompilationJob.value, scope)
    }
    scheduleCompilationPoll(jobId, epoch, scope)
  }
}

async function recoverSucceededCompilation(
  job: AssistantCompilationJobStatus,
  epoch: number,
  scope: CompilationRecoveryThreadScope,
) {
  clearCompilationPollTimer()
  const result = await api.getAssistantCompilationJobResult(job.id)
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  if (result.job_id !== job.id || result.scenario_id !== scope.scenarioId) {
    clearCompilationJobBookmark(localStorage, scope)
    detachCompilationRecovery()
    ElMessage.error('编译结果与当前业务场景不一致，已拒绝恢复。')
    return
  }

  const proposalThreadId = String(result.proposal_thread_id || scope.threadId).trim()
  let resultThreadId = proposalThreadId
  let recoveredMessages: AssistantMessage[]
  try {
    recoveredMessages = await api.listAssistantMessages(resultThreadId, apiContext())
  } catch {
    // A deduplicated job can point at its original proposal thread while the
    // current thread is merely a subscriber. Keep the current scoped thread
    // visible and use the server result as a display-only recovery copy.
    resultThreadId = scope.threadId
    recoveredMessages = await api.listAssistantMessages(resultThreadId, apiContext())
  }
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  const proposal = result.proposal as AssistantProposal | undefined
  const proposalId = String(proposal?.proposal_id || '')
  const hasProposalMessage = proposalId && recoveredMessages.some((message) => {
    const value = message.proposal as AssistantProposal | undefined
    return value?.proposal_id === proposalId
  })
  if (result.apply_ready && proposalId && !hasProposalMessage) {
    recoveredMessages = [...recoveredMessages, {
      id: result.proposal_message_id || `compilation-result-${job.id}`,
      thread_id: resultThreadId,
      role: 'assistant',
      content: '完整业务模型已在服务端完成并恢复；请核对变更清单后再决定是否应用。',
      context: { compilation_job_id: job.id, status: 'succeeded', proposal_thread_id: proposalThreadId },
      proposal,
    }]
  }

  streamGeneration += 1
  streamController = null
  loading.value = false
  clearCompilationJobBookmark(localStorage, scope)
  const owner = compilationOwnerScope()
  if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
  if (resultThreadId !== scope.threadId) {
    const targetScope = compilationThreadScope(resultThreadId)
    if (targetScope) clearCompilationJobBookmark(localStorage, targetScope)
    threadId.value = resultThreadId
    localStorage.setItem(storageKey.value, resultThreadId)
    syncThread(resultThreadId, '已恢复的完整业务模型')
  }
  messages.value = recoveredMessages
  activeCompilationJob.value = null
  compilationRecoveryThreadId.value = ''
  compilationRecoveryEpoch += 1
  scrollBottom()
  ElMessage.success(result.apply_ready ? '完整业务模型编译已恢复，请核对变更清单' : '完整业务模型编译已完成，服务端结果已恢复')
}

async function recoverFailedCompilation(
  job: AssistantCompilationJobStatus,
  epoch: number,
  scope: CompilationRecoveryThreadScope,
) {
  clearCompilationPollTimer()
  try {
    const recoveredMessages = await api.listAssistantMessages(scope.threadId, apiContext())
    if (compilationRecoveryIsCurrent(epoch, scope)) messages.value = recoveredMessages
  } catch {
    // The status DTO already contains the server-sanitized failure message.
  }
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  streamGeneration += 1
  streamController = null
  loading.value = false
  clearCompilationJobBookmark(localStorage, scope)
  const owner = compilationOwnerScope()
  if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
  activeCompilationJob.value = job
  markCompilationMessage(job, scope)
  ElMessage.error(job.error_message || '完整业务模型编译未完成，系统已保持零写入。')
}

async function consumeCompilationJob(
  job: AssistantCompilationJobStatus,
  epoch: number,
  scope: CompilationRecoveryThreadScope,
) {
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  if (!compilationJobMatchesScenario(job, scope.scenarioId)) {
    clearCompilationJobBookmark(localStorage, scope)
    detachCompilationRecovery()
    ElMessage.error('检测到跨场景的编译任务，已拒绝恢复。')
    return
  }
  activeCompilationJob.value = job
  markCompilationMessage(job, scope)
  if (job.status === 'running') {
    saveCompilationJobBookmark(localStorage, scope, job.id)
    scheduleCompilationPoll(job.id, epoch, scope)
    return
  }
  if (job.status === 'succeeded') {
    await recoverSucceededCompilation(job, epoch, scope)
    return
  }
  await recoverFailedCompilation(job, epoch, scope)
}

async function discoverCompilationForThread(id: string) {
  const scope = compilationThreadScope(id)
  if (!scope) return
  clearCompilationPollTimer()
  compilationRecoveryEpoch += 1
  compilationPollErrors = 0
  const epoch = compilationRecoveryEpoch
  compilationRecoveryThreadId.value = id
  activeCompilationJob.value = null
  try {
    const bookmarkedJobId = readCompilationJobBookmark(localStorage, scope)
    const pendingJobId = readPendingCompilationJobBookmark(localStorage, scope)
    const jobs = await api.listAssistantCompilationJobs(id, apiContext())
    if (!compilationRecoveryIsCurrent(epoch, scope)) return
    let job = selectCompilationJobForRecovery(jobs, scope.scenarioId, bookmarkedJobId || pendingJobId)
    if (!job && pendingJobId) {
      const pending = await api.getAssistantCompilationJob(pendingJobId)
      if (!compilationRecoveryIsCurrent(epoch, scope)) return
      if (pending.thread_id === id && compilationJobMatchesScenario(pending, scope.scenarioId)) job = pending
    }
    if (!job) {
      compilationRecoveryThreadId.value = ''
      if (pendingJobId) clearPendingCompilationJobBookmark(localStorage, scope)
      return
    }
    saveCompilationJobBookmark(localStorage, scope, job.id)
    if (pendingJobId === job.id) clearPendingCompilationJobBookmark(localStorage, scope)
    await consumeCompilationJob(job, epoch, scope)
  } catch (error: any) {
    if (!compilationRecoveryIsCurrent(epoch, scope)) return
    compilationRecoveryThreadId.value = ''
    if (error?.status !== 404) ElMessage.warning('暂时无法恢复编译任务进度；再次打开该会话时会继续查询。')
  }
}

async function beginCompilationRecoveryFromEvent(data: Record<string, any>) {
  const owner = compilationOwnerScope()
  const jobId = String(data?.job_id || '').trim()
  if (!owner || !jobId) return
  savePendingCompilationJobBookmark(localStorage, owner, jobId)
  const existingScope = compilationThreadScope()
  if (existingScope) saveCompilationJobBookmark(localStorage, existingScope, jobId)
  clearCompilationPollTimer()
  compilationRecoveryEpoch += 1
  compilationPollErrors = 0
  const lookupEpoch = compilationRecoveryEpoch
  if (existingScope) compilationRecoveryThreadId.value = existingScope.threadId
  try {
    const job = await api.getAssistantCompilationJob(jobId)
    const currentOwner = compilationOwnerScope()
    if (lookupEpoch !== compilationRecoveryEpoch
      || !currentOwner
      || currentOwner.tenantId !== owner.tenantId
      || currentOwner.userId !== owner.userId
      || currentOwner.scenarioId !== owner.scenarioId) return
    if (!compilationJobMatchesScenario(job, owner.scenarioId)) {
      clearPendingCompilationJobBookmark(localStorage, owner)
      if (existingScope) clearCompilationJobBookmark(localStorage, existingScope)
      ElMessage.error('服务端返回了其他场景的编译任务，已拒绝关联。')
      return
    }
    const targetThreadId = String(threadId.value || job.thread_id || '').trim()
    const scope = compilationThreadScope(targetThreadId)
    if (!scope) return
    if (!threadId.value) {
      threadId.value = targetThreadId
      localStorage.setItem(storageKey.value, targetThreadId)
      syncThread(targetThreadId, '完整业务模型编译')
    }
    const epoch = lookupEpoch
    compilationRecoveryThreadId.value = targetThreadId
    saveCompilationJobBookmark(localStorage, scope, job.id)
    clearPendingCompilationJobBookmark(localStorage, owner)
    await consumeCompilationJob(job, epoch, scope)
  } catch {
    // Keep the pending bookmark: HMR/reload can resolve it from the owner-scoped status API.
  }
}

function settleCompilationFromStream(proposal?: AssistantProposal | Record<string, any>) {
  if (!proposal || proposal.kind !== 'scenario_model') return
  const scope = compilationThreadScope(compilationRecoveryThreadId.value || threadId.value)
  if (scope) clearCompilationJobBookmark(localStorage, scope)
  const owner = compilationOwnerScope()
  if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
  detachCompilationRecovery()
}

function onCompilationVisibilityChange() {
  const job = activeCompilationJob.value
  const scope = compilationThreadScope(compilationRecoveryThreadId.value)
  if (!job || job.status !== 'running' || !scope) return
  scheduleCompilationPoll(job.id, compilationRecoveryEpoch, scope)
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
      ? `我已进入「${context.value.page}」工作区。选择“完整场景建模”并提供业务介绍或附件，我可以一次生成本体、映射、函数、操作、规则、事件和工作流的待审核变更清单。`
      : '我可以先根据业务介绍创建场景草稿；打开具体场景后，可继续完成完整业务模型建设。',
  }
}

async function loadThread(id: string, closeHistory = true) {
  detachCompilationRecovery()
  if (id !== threadId.value) attachments.value = []
  try {
    messages.value = await api.listAssistantMessages(id, apiContext())
    threadId.value = id
    localStorage.setItem(storageKey.value, id)
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
    Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
    Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
    if (closeHistory) historyVisible.value = false
    scrollBottom()
    await discoverCompilationForThread(id)
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
  let candidate = threads.value.find((thread) => thread.id === saved) || threads.value[0]
  const owner = compilationOwnerScope()
  const pendingJobId = owner ? readPendingCompilationJobBookmark(localStorage, owner) : ''
  if (owner && pendingJobId) {
    try {
      const pendingJob = await api.getAssistantCompilationJob(pendingJobId)
      if (compilationJobMatchesScenario(pendingJob, owner.scenarioId) && pendingJob.thread_id) {
        candidate = threads.value.find((thread) => thread.id === pendingJob.thread_id) || candidate
      } else if (!compilationJobMatchesScenario(pendingJob, owner.scenarioId)) {
        clearPendingCompilationJobBookmark(localStorage, owner)
      }
    } catch {
      // Keep the owner-scoped bookmark for a later recovery attempt.
    }
  }
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
  if (!auth.initialized) await auth.initialize()
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
    detachCompilationRecovery()
    const thread = await api.createAssistantThread(apiContext())
    threads.value = [thread, ...threads.value.filter((item) => item.id !== thread.id)]
    threadId.value = thread.id
    localStorage.setItem(storageKey.value, thread.id)
    messages.value = [welcomeMessage()]
    attachments.value = []
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
    Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
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
      const scope = compilationThreadScope(thread.id)
      if (scope) clearCompilationJobBookmark(localStorage, scope)
      detachCompilationRecovery()
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
    case 'compilation_job':
      if (event.data?.job_id) {
        // The job event arrives before the final meta event on a brand-new
        // thread. Bind it to the already visible streaming response so the
        // recovery poller updates that card instead of inserting a second
        // "processing" assistant message for the same server-side job.
        ai.context = {
          ...(ai.context || {}),
          compilation_job_id: String(event.data.job_id),
          status: 'processing',
        }
      }
      void beginCompilationRecoveryFromEvent(event.data || {})
      break
    case 'progress':
      upsertThinking(ai, event.data as AssistantThought, index)
      break
    case 'token':
      ai.content += String(event.data || '')
      break
    case 'proposal':
      ai.proposal = event.data || {}
      settleCompilationFromStream(ai.proposal)
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
      settleCompilationFromStream(ai.proposal)
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
  const attachmentOnlyPrompt = ({
    scenario: '请根据本次上传的业务文档生成一个待确认的业务场景草稿，并标明无法确定的信息。',
    scenario_model: '请逐段编译本次上传的完整业务文档，生成完整业务模型，并列出所有未识别、歧义和冲突项。',
    ontology: '请根据本次上传的业务文档建立对象类型、属性、关系与约束，并列出无法从原文确定的信息。',
    mapping: '请把本次上传文档作为业务语义参考，只根据当前场景中已经检查的真实数据源表和字段生成映射草稿。',
    capabilities: '请根据本次上传的完整业务文档，新增其中明确描述的函数、操作、规则和事件，并列出所有未识别、歧义和冲突项。',
    workflow: '请根据本次上传的业务文档生成待审核的工作流，明确触发条件、处理节点、分支和引用资源。',
  } as Partial<Record<AssistantTaskPreset, string>>)[taskPreset.value] || ''
  const providedContent = text !== undefined ? text : input.value.trim()
  const content = (providedContent || (
    attachmentOnlyPrompt && attachments.value.length
      ? attachmentOnlyPrompt
      : ''
  )).trim()
  if (!content || loading.value || compilationBusy.value || uploadingFiles.value > 0) return
  if (activeCompilationJob.value?.status === 'failed') detachCompilationRecovery()
  if (messages.value.length === 1 && messages.value[0].role === 'assistant' && !messages.value[0].id) messages.value = []
  const currentAttachments = [...attachments.value]
  messages.value.push({ role: 'user', content, attachments: currentAttachments })
  messages.value.push({ role: 'assistant', content: '', thinking: [], streaming: true })
  // 从响应式数组中重新取出消息，避免直接修改未被 Vue 代理的原始对象。
  const aiIndex = messages.value.length - 1
  const ai = messages.value[aiIndex]
  input.value = ''
  loading.value = true
  const generation = ++streamGeneration
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
      llm_config_id: assistantConfig.llmConfigId || undefined,
      skill_ids: [...assistantConfig.skillIds],
      mcp_ids: [...assistantConfig.mcpIds],
      mode: mode.value,
      draft_kind: mode.value === 'draft' ? draftKind.value : 'auto',
    },
    (event) => {
      if (generation !== streamGeneration || componentDisposed) return
      handleAssistantEvent(event, ai, aiIndex)
      if (event.type === 'meta') attachments.value = []
    },
    () => {
      if (generation === streamGeneration && !componentDisposed) finishStream(ai)
    },
    (error) => {
      if (generation !== streamGeneration || componentDisposed) return
      ai.content += compilationRunning.value
        ? `${ai.content ? '\n\n' : ''}连接已中断，但完整业务模型仍由服务端任务处理；页面会继续查询任务状态，不会自动重发请求。`
        : `${ai.content ? '\n\n' : ''}这次请求没有完成：${error.message || '请求失败'}`
      ai.streaming = false
      loading.value = false
      streamController = null
      if (compilationRunning.value) ElMessage.warning('连接已中断，正在从服务端恢复编译任务')
      else ElMessage.error(error.message || '助手请求失败')
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
    taskPreset.value = 'scenario'
    mode.value = 'draft'
    draftKind.value = 'scenario'
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
  if (['retry', 'revise_and_retry'].includes(String(option.value || ''))) {
    // A failed model call must not silently turn the next attempt into an
    // attachment-free request. Temporary attachments remain reusable inside
    // the same thread until their server-side TTL expires, so restore the most
    // parsed attachment set paired with this assistant response, then let the
    // user edit the correction draft before explicitly submitting it.
    attachments.value = retryAttachmentsForMessage(messages.value, sourceMessage, threadId.value)
    taskPreset.value = 'scenario_model'
    mode.value = 'draft'
    draftKind.value = 'scenario_model'
    input.value = compilationRetryDraft(option.value, option.prompt)
    scrollBottom()
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
  if (!proposal || ['applied', 'partially_applied'].includes(proposal.status || '') || !proposalCanApply(proposal) || !threadId.value || !proposal.proposal_id || applyingIndex.value !== null) return
  const effectiveChanges = proposal.changes?.filter((change) => change.operation !== 'skip').length || 0
  const blockerCount = blockingIssues(proposal).length
  const partialApply = proposal.kind === 'scenario_model' && blockerCount > 0
  const confirmation = proposal.kind === 'scenario'
    ? `将根据这份草稿创建业务场景“${proposal.payload?.name || '未命名场景'}”。附件仍只属于助手临时上下文，不会成为正式数据源。`
    : proposal.kind === 'mapping'
      ? `将把 ${effectiveChanges} 项映射差异保存到当前场景。保存不会导入数据，之后仍需预览、测试并刷新对象。`
      : proposal.kind === 'scenario_model'
        ? partialApply
          ? `当前有 ${blockerCount} 个阻塞项。确认后只会把通过依赖、引用和结构预检的可用变更在同一事务中写入；受阻定义会跳过并保留解决建议，不会被强行写入。`
          : `将把 ${effectiveChanges} 项跨资源变更在同一事务中写入当前场景；任一对象、引用、规则、流程或映射校验失败都会整体回滚。AI 生成的操作、规则、事件和工作流仍保持停用。`
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
    const proposalThreadId = String(message.context?.proposal_thread_id || threadId.value)
    const result = await api.applyAssistantProposal({
      kind: proposal.kind,
      scenario_id: proposal.kind === 'scenario' ? undefined : context.value.scenario_id,
      thread_id: proposalThreadId,
      proposal_id: proposal.proposal_id,
      confirm: true,
      allow_partial: partialApply,
    })
    const appliedScenarioId = proposal.kind === 'scenario' ? result?.data?.scenario_id : ''
    const partiallyApplied = proposal.kind === 'scenario_model' && result?.data?.partial === true
    message.content += proposal.kind === 'scenario'
      ? '\n\n业务场景已创建，正在进入场景建设。'
      : proposal.kind === 'mapping'
        ? '\n\n映射草稿已保存。下一步请预览、测试并刷新对象。'
        : partiallyApplied
          ? `\n\n可用部分已应用；仍有 ${result?.data?.blocked_issue_count || blockerCount} 个阻塞项，请按清单补充资料后重新编译。`
          : '\n\n变更已应用到当前场景草稿。'
    message.proposal = { ...proposal, status: partiallyApplied ? 'partially_applied' : 'applied', apply_result: result?.data || {} }
    window.dispatchEvent(new CustomEvent('assistant-proposal-applied', { detail: { scenario_id: appliedScenarioId || context.value.scenario_id, kind: proposal.kind } }))
    if (result?.status === 'replayed') ElMessage.info('该变更已应用过，已恢复应用结果')
    else ElMessage.success(partiallyApplied ? '可用模型已应用，阻塞项已保留' : proposal.kind === 'scenario' ? '业务场景已创建' : proposal.kind === 'mapping' ? '映射草稿已保存' : '变更已应用到场景草稿')
    if (appliedScenarioId) {
      visible.value = false
      await router.push({ name: 'scenario-detail', params: { id: appliedScenarioId }, query: { stage: 'workflows' } })
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

watch(() => storageKey.value, async () => {
  streamGeneration += 1
  streamController = null
  detachCompilationRecovery()
  loading.value = false
  messages.value = []
  threads.value = []
  threadId.value = ''
  historyVisible.value = false
  attachments.value = []
  sourcePreviewVisible.value = false
  Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
  Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
  Object.keys(expandedThinking).forEach((key) => delete expandedThinking[key])
  if (visible.value) {
    await loadContext()
  }
})
watch(() => context.value.scenario_id, () => {
  taskPreset.value = 'smart'
  mode.value = 'ask'
  draftKind.value = 'auto'
}, { immediate: true })
watch(showLauncher, (show) => {
  if (!show) visible.value = false
})

onMounted(() => {
  restoreAssistantConfig()
  window.addEventListener('ontology-selection-change', onSelection)
  document.addEventListener('visibilitychange', onCompilationVisibilityChange)
})
onBeforeUnmount(() => {
  componentDisposed = true
  streamGeneration += 1
  streamController = null
  clearCompilationPollTimer()
  window.removeEventListener('ontology-selection-change', onSelection)
  document.removeEventListener('visibilitychange', onCompilationVisibilityChange)
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
:global(.assistant-drawer .el-drawer__body) { min-height: 0; padding: 0; overflow: hidden; }
.assistant-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 18px 13px; border-bottom: 1px solid var(--border); background: var(--surface); }
.assistant-head-actions { display: flex; align-items: center; gap: 2px; }
.assistant-title-wrap { display: flex; align-items: center; gap: 10px; min-width: 0; }
.assistant-avatar, .message-avatar { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; color: #fff; background: var(--grad); box-shadow: var(--shadow-sm); }
.assistant-avatar { width: 38px; height: 38px; border-radius: 12px; }
.assistant-title { display: flex; align-items: center; gap: 7px; font-size: 15px; font-weight: 800; color: var(--text); }
.assistant-subtitle { margin-top: 3px; color: var(--text-3); font-size: 11px; }
.assistant-settings { display: grid; gap: 7px; color: var(--text-2); }
.assistant-settings-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
.assistant-settings-head div { display: flex; flex-direction: column; gap: 2px; }
.assistant-settings-head strong { color: var(--text); font-size: 13px; }
.assistant-settings-head span { color: var(--text-3); font-size: 10px; }
.assistant-setting-label { margin-top: 3px; color: var(--text-2); font-size: 10.5px; font-weight: 750; }
.assistant-setting-control { width: 100%; }
.assistant-settings-note { margin: 2px 0 0; padding-top: 7px; border-top: 1px dashed var(--border); color: var(--text-3); font-size: 10px; line-height: 1.5; }
.assistant-context { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 10px 18px; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.context-hint { color: var(--text-3); font-size: 11px; margin-left: auto; }
.assistant-session-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 18px; border-bottom: 1px solid var(--border); background: var(--surface); }
.session-current { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.session-current strong { overflow: hidden; color: var(--text); font-size: 12px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.session-label { color: var(--text-3); font-size: 10px; }
.session-actions { display: flex; align-items: center; flex: 0 0 auto; gap: 6px; }
.session-actions :deep(.el-button) { min-height: 32px; }
.compilation-recovery { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 9px; padding: 9px 18px; border-bottom: 1px solid color-mix(in srgb, var(--primary) 24%, var(--border)); color: var(--primary-600); background: var(--primary-soft); }
.compilation-recovery.is-failed { border-bottom-color: color-mix(in srgb, var(--danger) 35%, var(--border)); color: var(--danger); background: var(--danger-soft); }
.compilation-recovery > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.compilation-recovery strong { color: var(--text); font-size: 11px; }
.compilation-recovery span { overflow: hidden; color: var(--text-2); font-size: 9.5px; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
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
.assistant-quick-start { display: grid; width: min(100%, 390px); grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-top: 16px; }
.assistant-quick-start button { display: flex; min-height: 64px; flex-direction: column; justify-content: center; gap: 3px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; color: var(--text-2); background: var(--surface); cursor: pointer; font: inherit; text-align: left; transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease; }
.assistant-quick-start button:hover, .assistant-quick-start button:focus-visible { border-color: var(--primary); background: var(--primary-soft); box-shadow: var(--shadow-sm); outline: none; }
.assistant-quick-start strong { color: var(--text); font-size: 11.5px; }
.assistant-quick-start span { color: var(--text-3); font-size: 9.5px; line-height: 1.4; }
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
.preview-toggle { min-height: 28px; margin-left: auto; padding: 3px 0; border: 0; color: var(--primary-600); background: transparent; cursor: pointer; font: inherit; }
.preview-toggle:hover, .preview-toggle:focus-visible { color: var(--primary); text-decoration: underline; outline: none; text-underline-offset: 3px; }
.proposal-disclosure { margin: 0 12px 10px; overflow: hidden; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }
.proposal-disclosure-toggle { display: flex; align-items: center; justify-content: space-between; gap: 10px; width: 100%; min-height: 48px; padding: 8px 10px; border: 0; color: var(--text-2); background: transparent; cursor: pointer; font: inherit; text-align: left; }
.proposal-disclosure-toggle:hover, .proposal-disclosure-toggle:focus-visible { color: var(--primary-600); background: var(--primary-soft); outline: none; }
.disclosure-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.disclosure-copy strong { color: var(--text); font-size: 11px; }
.disclosure-copy small { color: var(--text-3); font-size: 9.5px; line-height: 1.45; }
.disclosure-action { display: inline-flex; align-items: center; flex: 0 0 auto; gap: 4px; color: var(--primary-600); font-size: 10px; font-weight: 700; }
.disclosure-chevron { flex: 0 0 auto; transition: transform 160ms ease; }
.disclosure-chevron.rotated { transform: rotate(180deg); }
.proposal-changes { display: flex; flex-direction: column; gap: 7px; padding: 0 12px 10px; }
.proposal-disclosure .proposal-changes { padding: 0 10px 10px; border-top: 1px solid var(--border); }
.proposal-disclosure .proposal-change:first-child { margin-top: 10px; }
.proposal-change { display: flex; align-items: flex-start; gap: 7px; padding: 7px 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.proposal-change-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.proposal-change-copy strong { color: var(--text); font-size: 11px; font-weight: 750; line-height: 1.4; }
.proposal-change-copy span { color: var(--text-3); font-size: 10.5px; line-height: 1.45; }
.proposal-detail { display: grid; gap: 10px; margin: 0 12px 10px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }
.proposal-summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 0; }
.proposal-summary-grid div { padding: 7px 8px; border-radius: 7px; background: var(--surface); }
.proposal-summary-grid dt { color: var(--text-3); font-size: 9.5px; }
.proposal-summary-grid dd { margin: 3px 0 0; color: var(--text); font-size: 11px; font-weight: 700; overflow-wrap: anywhere; }
.proposal-description { margin: 0; color: var(--text-2); font-size: 11px; line-height: 1.6; }
.proposal-section { display: grid; gap: 7px; }
.proposal-section h4 { margin: 0; color: var(--text); font-size: 11px; }
.compound-resource-group > h4 { display: flex; align-items: center; justify-content: space-between; }
.compound-resource-group > h4 span { display: inline-grid; min-width: 20px; height: 20px; place-items: center; border-radius: 999px; color: var(--primary-600); background: var(--primary-soft); font-size: 9px; }
.compound-resource-list { display: grid; gap: 6px; }
.compound-resource-card { display: grid; gap: 6px; padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.compound-resource-card header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.compound-resource-card header strong { color: var(--text); font-size: 10.5px; line-height: 1.45; }
.compound-resource-card header > div { display: flex; flex: 0 0 auto; gap: 4px; }
.compound-resource-card p { margin: 0; color: var(--text-3); font-size: 9.5px; line-height: 1.5; }
.compound-resource-card footer { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 5px; padding-top: 5px; border-top: 1px dashed var(--border); font-size: 9px; }
.compound-resource-card footer span { color: var(--text-3); }
.compound-resource-card footer b { overflow-wrap: anywhere; color: var(--text-2); font-weight: 600; }
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
.coverage-summary { display: flex; flex-wrap: wrap; gap: 5px; }
.coverage-summary span { padding: 4px 7px; border-radius: 999px; color: var(--text-2); background: var(--surface); font-size: 9.5px; }
.coverage-summary span.danger { color: var(--danger); background: var(--danger-soft); }
.source-manifest-list, .unresolved-section { display: grid; gap: 8px; }
.source-manifest-list > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 8px; border-radius: 7px; background: var(--surface); font-size: 10px; }
.source-manifest-list strong { min-width: 0; overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
.source-manifest-list span { flex: 0 0 auto; color: var(--text-3); }
.unresolved-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.unresolved-head h4 { margin: 0; }
.unresolved-head p { margin: 3px 0 0; color: var(--text-3); font-size: 9.5px; line-height: 1.45; }
.unresolved-counts { display: flex; flex: 0 0 auto; gap: 5px; }
.unresolved-counts span { padding: 3px 6px; border: 1px solid var(--border); border-radius: 999px; color: var(--text-2); background: var(--surface); font-size: 9px; font-weight: 700; }
.unresolved-counts .is-blocking { border-color: color-mix(in srgb, var(--danger) 38%, var(--border)); color: var(--danger); background: var(--danger-soft); }
.issue-groups { display: grid; gap: 6px; }
.issue-category-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 7px; border-radius: 7px; color: var(--text-2); background: var(--surface); font-size: 9.5px; }
.issue-category-head strong { color: var(--text); font-size: 10.5px; }
.issue-category-head.is-blocking { color: var(--danger); background: var(--danger-soft); }
.issue-notice-category { overflow: hidden; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.issue-category-toggle { display: grid; width: 100%; min-height: 48px; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 7px 8px; border: 0; color: var(--text-2); background: transparent; cursor: pointer; font: inherit; text-align: left; }
.issue-category-toggle:hover, .issue-category-toggle:focus-visible { color: var(--primary-600); background: var(--primary-soft); outline: none; }
.issue-notice-groups { display: grid; gap: 6px; padding: 7px; border-top: 1px solid var(--border); background: var(--surface-2); }
.issue-group { overflow: hidden; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.issue-group.is-blocking { border-color: color-mix(in srgb, var(--danger) 35%, var(--border)); }
.issue-group-toggle { display: grid; width: 100%; min-height: 48px; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 7px 8px; border: 0; color: var(--text-2); background: transparent; cursor: pointer; font: inherit; text-align: left; }
.issue-group-toggle:hover, .issue-group-toggle:focus-visible { color: var(--primary-600); background: var(--primary-soft); outline: none; }
.issue-group.is-blocking .issue-group-toggle:hover, .issue-group.is-blocking .issue-group-toggle:focus-visible { background: var(--danger-soft); }
.issue-severity { min-width: 34px; padding: 3px 5px; border-radius: 999px; color: var(--text-2); background: var(--surface-2); font-size: 9px; font-weight: 800; text-align: center; }
.issue-group.is-blocking .issue-severity { color: var(--danger); background: var(--danger-soft); }
.issue-group-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.issue-group-copy strong { color: var(--text); font-size: 10.5px; }
.issue-group-copy small { color: var(--text-3); font-size: 9px; overflow-wrap: anywhere; }
.issue-group-details { display: grid; gap: 5px; padding: 7px; border-top: 1px solid var(--border); background: var(--surface-2); }
.unresolved-row { display: flex; align-items: flex-start; gap: 7px; padding: 7px 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); }
.issue-number { display: inline-grid; width: 18px; height: 18px; flex: 0 0 auto; place-items: center; border-radius: 50%; color: var(--text-3); background: var(--surface-2); font-size: 8.5px; font-weight: 800; }
.unresolved-row > div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.unresolved-row strong { color: var(--text); font-size: 10.5px; line-height: 1.45; }
.unresolved-row span { color: var(--text-3); font-size: 9.5px; overflow-wrap: anywhere; }
.unresolved-row .issue-resolution { color: var(--warning); line-height: 1.5; }
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
.action-preview-params { margin: 8px 0 0; padding: 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); }
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
.task-preset-control { display: flex; align-items: center; gap: 5px; min-width: 0; }
.task-preset-control > label { color: var(--text-3); font-size: 10px; font-weight: 700; }
.task-preset-control :deep(.el-select) { width: 150px; }
.task-preset-hint { min-width: 0; overflow: hidden; color: var(--text-3); font-size: 9.5px; text-overflow: ellipsis; white-space: nowrap; }
.composer-input-row { display: flex; align-items: flex-end; gap: 8px; }
.composer-input-row :deep(.el-textarea__inner) { min-height: 74px !important; padding-right: 12px; }
.send-button { width: 42px; height: 42px; padding: 0; flex: 0 0 auto; }
.source-preview { min-height: 260px; }
.source-preview-meta { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
.source-preview-meta > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.source-preview-meta strong { overflow: hidden; color: var(--text); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.source-preview-meta span { color: var(--text-3); font-size: 10px; }
.source-preview-text { min-height: 170px; margin: 12px 0 0; padding: 14px; border-radius: 10px; color: var(--text-2); background: var(--surface-2); font: 12px/1.75 'Cascadia Code', Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }

@media (prefers-reduced-motion: reduce) {
  .thinking-chevron, .disclosure-chevron, .stream-cursor { transition: none; animation: none; }
}

@media (max-width: 560px) {
  .assistant-launcher { right: 14px; bottom: 14px; }
  .context-hint { width: 100%; margin-left: 0; }
  .assistant-session-bar { align-items: flex-start; flex-direction: column; }
  .session-actions { width: 100%; }
  .session-actions :deep(.el-button) { flex: 1; }
  .compilation-recovery { grid-template-columns: auto minmax(0, 1fr); padding: 9px 14px; }
  .compilation-recovery > :deep(.el-tag), .compilation-recovery > :deep(.el-button) { grid-column: 2; justify-self: start; }
  .compilation-recovery > :deep(.el-button) { min-height: 44px; }
  .assistant-messages { padding: 14px; }
  .assistant-history { padding: 14px; }
  .assistant-composer { padding: 9px 10px 12px; }
  .thread-delete, .attachment-chip button, .tool-button, .send-button { min-width: 44px; min-height: 44px; }
  .attachment-chip button { width: 44px; height: 44px; }
  .tool-button span { display: none; }
  .composer-tools { flex-wrap: wrap; }
  .task-preset-control { flex: 1; }
  .task-preset-control :deep(.el-select) { width: 100%; }
  .task-preset-hint { width: 100%; padding-left: 1px; }
  .assistant-quick-start { grid-template-columns: 1fr; }
  .assistant-action-preview dl { grid-template-columns: 1fr; }
  .assistant-action-next { align-items: stretch; flex-direction: column; }
  .assistant-action-next .el-button { width: 100%; min-height: 44px; }
  .unresolved-head { flex-direction: column; }
}

/* 低高度横屏仍保持“固定外壳 + 消息区滚动 + 输入区常驻”。 */
@media (max-height: 480px) {
  .assistant-head { min-height: 52px; padding: 6px 10px; }
  .assistant-head :deep(.el-button) { min-width: 44px; min-height: 44px; }
  .assistant-subtitle, .context-hint, .session-current { display: none; }
  .assistant-context { min-height: 36px; padding: 5px 10px; flex-wrap: nowrap; overflow: hidden; }
  .assistant-session-bar { min-height: 52px; padding: 4px 10px; }
  .session-actions { width: 100%; }
  .session-actions :deep(.el-button) { flex: 1; min-height: 44px; }
  .assistant-messages, .assistant-history { padding: 8px 10px; }
  .assistant-composer { flex: 0 0 auto; padding: 6px 10px 8px; }
  .composer-tools { min-height: 44px; margin-bottom: 4px; }
  .attachment-chip button, .tool-button, .send-button { min-width: 44px; min-height: 44px; }
  .attachment-chip button { width: 44px; height: 44px; }
  .composer-input-row :deep(.el-textarea__inner) { min-height: 52px !important; max-height: 64px; }
}

@media (max-height: 400px) {
  .assistant-session-bar { display: none; }
}

/* 软键盘或超矮横屏无法同时容纳消息、附件与输入控件时，明确退化为
   抽屉正文这一个滚动容器，避免隐藏附件或裁掉发送按钮。 */
@media (max-height: 340px) {
  :global(.assistant-drawer .el-drawer__body) { overflow-y: auto; }
  .assistant-shell { height: auto; min-height: 100%; }
  .assistant-messages, .assistant-history { flex: 0 0 auto; min-height: 96px; overflow: visible; }
  .attachment-strip { max-height: none; overflow: visible; }
}
</style>
