<template>
  <button
    v-if="showLauncher && !visible"
    class="assistant-launcher"
    :class="{ 'is-working': advisorWorking }"
    type="button"
    :aria-label="advisorWorking ? `打开智能业务顾问，${launcherStatus}` : '打开智能业务顾问'"
    :title="advisorWorking ? launcherStatus : '打开智能业务顾问'"
    @click="openAssistant"
  >
    <span class="assistant-launcher-icon" aria-hidden="true">
      <el-icon v-if="advisorWorking" class="is-loading"><Loading /></el-icon>
      <el-icon v-else><MagicStick /></el-icon>
    </span>
    <span class="assistant-launcher-copy" aria-live="polite">
      <strong>智能业务顾问</strong>
      <small v-if="advisorWorking">{{ launcherStatus }}</small>
    </span>
    <span class="assistant-live-dot" aria-hidden="true"></span>
  </button>

  <el-drawer
    v-model="visible"
    direction="rtl"
    size="min(560px, 100vw)"
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
            <div class="assistant-title">智能业务顾问 <el-tag size="small" type="success" effect="plain">上下文感知</el-tag></div>
            <div class="assistant-subtitle">理解业务资料，拆解任务并受控建设模型</div>
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
          <el-button text circle aria-label="关闭智能业务顾问" title="关闭" @click="visible = false">
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
          <h3>直接描述要完成的工作</h3>
          <p>智能业务顾问会结合当前页面、附件和会话上下文自动规划处理路径。</p>
        </div>

        <article v-for="(message, index) in messages" :key="message.id || index" class="assistant-message" :class="message.role">
          <div v-if="message.role === 'assistant'" class="message-avatar assistant-message-avatar" aria-hidden="true"><el-icon><MagicStick /></el-icon></div>
          <div class="message-content">
            <div v-if="message.role === 'user'" class="message-label">你</div>
            <div v-else class="message-label">智能业务顾问</div>
            <section v-if="shouldShowWorkLog(message)" class="assistant-worklog" :class="{ 'is-active': message.streaming }" aria-label="助手工作过程" aria-live="polite">
              <header class="worklog-head">
                <span><el-icon aria-hidden="true"><Operation /></el-icon>工作过程</span>
                <span class="worklog-summary">{{ workLogSummary(message) }}</span>
                <span v-if="message.streaming" class="worklog-live" role="status">进行中</span>
              </header>
              <div class="worklog-body">
                <div v-for="entry in workTranscriptEntries(message)" :key="entry.id" class="worklog-entry" :class="`is-${entry.status}`">
                  <span class="worklog-step-dot" aria-hidden="true"></span>
                  <div><strong>{{ entry.title }}</strong><span>{{ entry.detail }}</span></div>
                  <small>{{ activityKindLabel(entry.kind) }}</small>
                </div>
                <div v-if="compilationLiveEntries(message).length" class="worklog-live-feed" aria-label="实时工作播报">
                  <div
                    v-for="(entry, liveIndex) in compilationLiveEntries(message)"
                    :key="`${entry.job_id}:${entry.emitted_at}`"
                    class="worklog-live-entry"
                    :class="{ 'is-latest': liveIndex === compilationLiveEntries(message).length - 1, 'is-disconnected': entry.stream_state === 'disconnected' }"
                    :role="liveIndex === compilationLiveEntries(message).length - 1 ? 'status' : undefined"
                    :aria-live="liveIndex === compilationLiveEntries(message).length - 1 ? 'polite' : undefined"
                  >
                    <time :datetime="entry.emitted_at">{{ formatLivenessClock(entry.emitted_at) }}</time>
                    <div>
                      <strong>{{ entry.stream_state === 'disconnected' ? '任务流连接已中断' : `仍在「${entry.stage_title}」` }}</strong>
                      <span>{{ entry.message }}</span>
                    </div>
                    <small>{{ formatElapsedSeconds(entry.elapsed_seconds) }}</small>
                  </div>
                </div>
                <div class="worklog-note">仅展示可验证、可回看的工作信息，不包含模型的隐藏推理。</div>
              </div>
            </section>
            <div class="message-bubble" :class="{ user: message.role === 'user' }">
              <SafeMarkdown v-if="message.role === 'assistant'" :content="assistantMessageContent(message)" />
              <span v-if="message.role === 'assistant' && message.streaming" class="stream-cursor" aria-hidden="true">▍</span>
              <div v-else-if="message.role !== 'assistant'" class="user-content">{{ message.content }}</div>
            </div>
            <div v-if="proposalOf(message)" class="proposal-card" :class="{ 'is-model-result': proposalOf(message)?.kind === 'scenario_model' }">
              <div class="proposal-head">
                <div>
                  <div class="proposal-title"><el-icon aria-hidden="true"><DocumentChecked /></el-icon>{{ proposalOf(message)?.kind === 'scenario_model' ? '建模产物' : proposalOf(message)?.title }}</div>
                  <div class="proposal-summary">{{ proposalOf(message)?.summary }}</div>
                </div>
                <el-tag size="small" :type="proposalStatusType(proposalOf(message))" effect="plain">
                  {{ proposalStatusLabel(proposalOf(message)) }}
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
                  <span>待补全原因 {{ scenarioIssueGroups(proposalOf(message)).length }}</span>
                </template>
                <span v-if="proposalOf(message)?.changes?.length">差异 {{ proposalOf(message)?.changes?.length }}</span>
                <button
                  type="button"
                  class="preview-toggle"
                  :aria-expanded="Boolean(expandedProposal[index])"
                  :aria-controls="proposalDetailId(message, index)"
                  @click="toggleProposal(index)"
                >{{ expandedProposal[index] ? '收起详情' : proposalOf(message)?.kind === 'scenario_model' ? '查看建模产物' : '查看详情' }}</button>
              </div>
              <section
                v-if="modelTasks(proposalOf(message)).length && Boolean(expandedProposal[index])"
                class="model-task-plan"
                aria-label="完整场景建模任务"
              >
                <button type="button" class="model-task-plan-toggle" :aria-expanded="isModelPlanExpanded(message, index)" @click="toggleModelPlan(message, index)">
                  <span class="model-task-plan-copy">
                    <strong>场景建模计划</strong>
                    <small>{{ modelPlanSummary(proposalOf(message)) }}</small>
                  </span>
                  <el-tag size="small" effect="plain" :type="modelRunStatusType(proposalOf(message))">
                    {{ modelTaskProgress(proposalOf(message)) }}
                  </el-tag>
                  <span class="model-task-plan-action">{{ isModelPlanExpanded(message, index) ? '收起' : '查看计划' }}<el-icon class="worklog-chevron" :class="{ rotated: isModelPlanExpanded(message, index) }" aria-hidden="true"><ArrowDown /></el-icon></span>
                </button>
                <article v-if="currentModelTask(proposalOf(message)) && !isModelPlanExpanded(message, index)" class="model-current-task" :class="`is-${currentModelTask(proposalOf(message))?.status}`">
                  <div class="model-task-head">
                    <span class="model-task-index">{{ currentModelTask(proposalOf(message))?.order }}</span>
                    <div class="model-task-title"><strong>{{ currentModelTask(proposalOf(message))?.title }}</strong><small>{{ modelTaskOutputCount(currentModelTask(proposalOf(message))) }} 项资源 · {{ currentModelTask(proposalOf(message))?.description }}</small></div>
                    <el-tag size="small" effect="plain" :type="modelTaskStatusType(currentModelTask(proposalOf(message)))">{{ modelTaskStatusLabel(currentModelTask(proposalOf(message))) }}</el-tag>
                  </div>
                  <p v-if="currentModelTask(proposalOf(message))?.status === 'blocked'">草稿已准备完成，确认后会将本任务写入对应画布或模块。</p>
                  <p v-else-if="currentModelTask(proposalOf(message))?.status === 'awaiting_generation'">前置任务已处理。原始资料和已确认定义会保留；由你决定何时开始生成本任务。</p>
                  <p v-else-if="currentModelTask(proposalOf(message))?.status === 'waiting'">正在等待前一项任务完成。</p>
                  <div v-if="blockedModelProposalId !== proposalOf(message)?.proposal_id && isActiveModelRun(message) && modelNextAction(proposalOf(message))?.type === 'confirm_task'" class="model-task-actions">
                    <el-button v-if="modelNextAction(proposalOf(message))?.can_apply" size="small" type="primary" :loading="applyingIndex === index || recoveringModelProposalId === proposalOf(message)?.proposal_id" @click="applyCurrentModelTask(message, index)">
                      <el-icon aria-hidden="true"><Check /></el-icon>应用本任务并继续
                    </el-button>
                  </div>
                  <div v-if="blockedModelProposalId !== proposalOf(message)?.proposal_id && isActiveModelRun(message) && modelNextAction(proposalOf(message))?.type === 'generate_task'" class="model-task-actions">
                    <el-button v-if="modelNextAction(proposalOf(message))?.can_generate" size="small" type="primary" :loading="startingModelTaskId === `${proposalOf(message)?.proposal_id}:${currentModelTask(proposalOf(message))?.id}` || compilationBusy" @click="generateCurrentModelTask(message, index)">
                      <el-icon aria-hidden="true"><Promotion /></el-icon>开始生成本任务
                    </el-button>
                  </div>
                </article>
                <div v-show="isModelPlanExpanded(message, index)" class="model-task-list">
                  <article v-for="task in modelTasks(proposalOf(message))" :key="task.id" class="model-task" :class="[`is-${task.status}`, { 'is-current': isCurrentModelTask(proposalOf(message), task) }]">
                    <div class="model-task-head">
                      <span class="model-task-index">{{ task.order }}</span>
                      <div class="model-task-title"><strong>{{ task.title }}</strong><small>{{ modelTaskOutputCount(task) }} 项资源 · {{ task.description }}</small></div>
                      <el-tag size="small" effect="plain" :type="modelTaskStatusType(task)">{{ modelTaskStatusLabel(task) }}</el-tag>
                    </div>
                    <div v-if="task.status === 'blocked' && task.issues?.length" class="model-task-blocker">
                      <strong>草稿已落位：</strong><span>确认后会把本任务定义写入对应画布或模块。</span>
                      <small>{{ taskIssueCount(task) }} 项待补全内容只在助手最终汇总中展示。</small>
                    </div>
                    <div v-else-if="['drafted_with_gaps', 'deferred', 'skipped'].includes(task.status) && task.issues?.length" class="model-task-blocker">
                      <strong>待校验草稿：</strong><span>本轮未确认到正式写入，候选定义仍保持停用。</span>
                      <small>{{ taskIssueCount(task) }} 项待补全内容与解决建议保留在助手最终汇总中。</small>
                    </div>
                    <div v-else-if="task.status === 'waiting'" class="model-task-waiting">等待当前任务：{{ waitingTaskTitles(proposalOf(message), task).join('、') }}</div>
                    <div v-else-if="task.status === 'awaiting_generation'" class="model-task-note">前置任务已处理；原始资料和已确认定义会保留，等待你开始生成本任务。</div>
                    <div v-else-if="task.status === 'partially_applied' && modelTaskHasPersistedWrites(task)" class="model-task-note">本任务的安全部分已确认写入；暂不能正式运行的定义仍以草稿形式保留。</div>
                    <div v-else-if="modelTaskNeedsWriteVerification(task)" class="model-task-note">任务已结束，但暂未读取到可核验的正式写入结果。</div>
                    <div v-else-if="['deferred', 'drafted_with_gaps', 'skipped'].includes(task.status)" class="model-task-note">本任务草稿已经生成并保留；缺失与解决建议只在助手会话中汇总。</div>
                    <div v-if="blockedModelProposalId !== proposalOf(message)?.proposal_id && isActiveModelRun(message) && isCurrentModelTask(proposalOf(message), task) && modelNextAction(proposalOf(message))?.type === 'confirm_task'" class="model-task-actions">
                      <el-button v-if="modelNextAction(proposalOf(message))?.can_apply" size="small" type="primary" :loading="applyingIndex === index || recoveringModelProposalId === proposalOf(message)?.proposal_id" @click="applyModelTask(message, index, task, 'apply')">
                        <el-icon aria-hidden="true"><Check /></el-icon>应用本任务并继续
                      </el-button>
                    </div>
                    <div v-if="blockedModelProposalId !== proposalOf(message)?.proposal_id && isActiveModelRun(message) && isCurrentModelTask(proposalOf(message), task) && modelNextAction(proposalOf(message))?.type === 'generate_task'" class="model-task-actions">
                      <el-button v-if="modelNextAction(proposalOf(message))?.can_generate" size="small" type="primary" :loading="startingModelTaskId === `${proposalOf(message)?.proposal_id}:${task.id}` || compilationBusy" @click="generateModelTask(message, index, task)">
                        <el-icon aria-hidden="true"><Promotion /></el-icon>开始生成本任务
                      </el-button>
                    </div>
                  </article>
                </div>
                <div v-if="!modelExecutionSummary(proposalOf(message))?.final && modelExecutionSummary(proposalOf(message))?.current_task_title" class="model-run-waiting" aria-live="polite">
                  <template v-if="modelNextAction(proposalOf(message))?.type === 'generate_task'">当前停留在「{{ modelExecutionSummary(proposalOf(message))?.current_task_title }}」；前置任务已处理，等待你开始生成。</template>
                  <template v-else>当前停留在「{{ modelExecutionSummary(proposalOf(message))?.current_task_title }}」等待确认；确认后会写入本任务并继续下一项。</template>
                </div>
                <section v-if="isModelPlanExpanded(message, index) && modelExecutionSummary(proposalOf(message))?.final" class="model-run-summary" :class="`is-${modelRunStatusType(proposalOf(message))}`" aria-live="polite">
                  <header><strong>{{ modelRunSummaryTitle(proposalOf(message)) }}</strong><el-tag size="small" effect="plain" :type="modelRunStatusType(proposalOf(message))">最终总结</el-tag></header>
                  <p>{{ modelRunSummaryMessage(proposalOf(message)) }}</p>
                  <div class="model-run-summary-counts">
                    <span>正式写入任务 {{ modelFullyAppliedTaskCount(proposalOf(message)) }}</span>
                    <span>部分写入任务 {{ modelPartiallyAppliedTaskCount(proposalOf(message)) }}</span>
                    <span>仅保留草稿任务 {{ modelDraftOnlyTaskCount(proposalOf(message)) }}</span>
                    <span>无变更任务 {{ modelEmptyTaskCount(proposalOf(message)) }}</span>
                    <span>问题/说明 {{ modelExecutionSummary(proposalOf(message))?.remaining_issue_count || 0 }}</span>
                  </div>
                  <div v-if="scenarioIssueGroups(proposalOf(message)).length" class="model-run-root-causes">
                    <article v-for="group in scenarioIssueGroups(proposalOf(message))" :key="group.key">
                      <strong>{{ scenarioModelIssueLabel(group.code) }}</strong>
                      <span>{{ group.affectedCount || group.count }} 项资源受此原因影响</span>
                      <small>{{ group.resolutionHint || group.message }}</small>
                    </article>
                  </div>
                  <small>你可以直接在下方继续补充资料或修正要求，AI 会基于当前已实现草稿开启下一轮优化。</small>
                </section>
              </section>
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
                  <section v-if="scenarioIssueGroups(proposalOf(message)).length && !modelExecutionSummary(proposalOf(message))?.final" class="proposal-section unresolved-section">
                    <header class="unresolved-head">
                      <div><h4>待补全汇总</h4><p>相同原因已合并；草稿已经写入对应画布或模块，缺口不会阻止其他任务继续。</p></div>
                      <div class="unresolved-counts" aria-label="预检问题统计">
                        <span class="is-blocking">{{ blockingScenarioIssueGroups(proposalOf(message)).length }} 类阻塞</span>
                        <span>{{ nonBlockingScenarioIssueGroups(proposalOf(message)).length }} 类提示</span>
                      </div>
                    </header>
                    <div class="issue-groups">
                      <article v-for="group in scenarioIssueGroups(proposalOf(message))" :key="group.key" class="issue-group-summary" :class="group.blocking ? 'is-blocking' : 'is-notice'">
                        <span class="issue-severity">{{ group.blocking ? '阻塞' : '提示' }}</span>
                        <div>
                          <strong>{{ scenarioModelIssueLabel(group.code) }}</strong>
                          <p>{{ group.message }}</p>
                          <small>{{ group.count }} 项受此原因影响<span v-if="group.affectedCount && group.affectedCount !== group.count"> · 涉及 {{ group.affectedCount }} 个资源</span></small>
                          <span v-if="group.resolutionHint" class="issue-resolution">解决：{{ group.resolutionHint }}</span>
                        </div>
                      </article>
                    </div>
                  </section>
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
              <div v-if="proposalOf(message)?.kind !== 'scenario_model' && !modelTasks(proposalOf(message)).length" class="proposal-actions">
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

            <details v-if="hasAssistantEvidence(message) && proposalOf(message)?.kind !== 'scenario_model'" class="answer-evidence">
              <summary><span><el-icon aria-hidden="true"><DocumentChecked /></el-icon>查看回答依据</span><small>置信度 {{ confidencePercent(message.evidence?.confidence) }}</small></summary>
              <div class="evidence-meta-grid">
                <div v-if="message.evidence?.rules_used?.length"><b>使用规则</b><span>{{ message.evidence.rules_used.map((item) => item.result || item.name).join('；') }}</span></div>
                <div v-if="message.evidence?.tools_called?.length"><b>调用工具</b><span>{{ message.evidence.tools_called.map((item) => `${item.name}${item.purpose ? ` · ${item.purpose}` : ''}`).join('；') }}</span></div>
              </div>
              <div v-if="message.evidence?.uncertainties?.length" class="evidence-uncertainties"><b>仍需确认</b><ul><li v-for="item in message.evidence.uncertainties" :key="item">{{ item }}</li></ul></div>
            </details>

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
        <div v-if="modelRunAwaitingConfirmation || modelTaskRecoveryBusy || modelTaskRecoveryFailure" class="model-run-composer-wait" role="status">
          <el-icon aria-hidden="true"><Clock /></el-icon>
          <span v-if="modelTaskRecoveryBusy"><strong>当前任务已提交，正在恢复最新进度</strong>系统会持续读取持久化任务计划；不会重复应用，也不需要你手工刷新或重开会话。</span>
          <span v-else-if="modelTaskRecoveryFailure"><strong>当前计划的持久化状态已无法访问</strong>{{ modelTaskRecoveryFailure }} 你仍可在下方发起新的建模轮次；系统不会继续重复提交旧任务。</span>
          <span v-else><strong>当前持续任务停在确认点</strong>确认后会把本任务定义写入场景并继续；你也可以直接在下方说明修正、新增或删除要求，助手会基于已保存草稿开启下一轮优化。</span>
        </div>
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
        </div>
        <div class="composer-input-row">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="12000"
            show-word-limit
            :placeholder="modelTaskRecoveryBusy ? '正在恢复已提交任务的最新进度' : compilationRunning ? '补充、纠正或删除要求；将在当前安全检查点后继续' : modelRunAwaitingConfirmation ? '说明要修正或新增的内容，智能业务顾问会基于当前草稿继续' : composerPlaceholder"
            :disabled="modelTaskRecoveryBusy || (compilationRunning && !compilationAcceptingGuidance)"
            @keydown.enter.exact.prevent="send()"
          />
          <el-button class="send-button" type="primary" :loading="guidanceSubmitting || (!compilationRunning && (loading || modelTaskRecoveryBusy))" :disabled="!canSend || uploadingFiles > 0 || modelTaskRecoveryBusy || (compilationRunning && !compilationAcceptingGuidance)" :aria-label="compilationRunning ? '向当前任务补充指导' : '发送消息'" :title="compilationRunning ? '补充指导' : '发送'" @click="send()">
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
import { isNavigationFailure, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, streamAssistantChat, streamAssistantCompilationJob } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { AssistantActionPreview, AssistantAttachment, AssistantCompilationActivity, AssistantCompilationJobStatus, AssistantCompilationLiveness, AssistantCompilationStep, AssistantMessage, AssistantModelExecutionSummary, AssistantModelNextAction, AssistantModelTask, AssistantProposal, AssistantProposalApplyResult, AssistantQuestion, AssistantSource, AssistantThread, AssistantThought, LLMConfig, MCPConfig, Skill } from '@/types'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import KeyValueEditor from '@/components/KeyValueEditor.vue'
import {
  clearCompilationJobBookmark,
  clearPendingCompilationJobBookmark,
  compilationJobMatchesScenario,
  readCompilationJobBookmark,
  readPendingCompilationJobBookmark,
  saveCompilationJobBookmark,
  savePendingCompilationJobBookmark,
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
const visible = ref(false)
const loading = ref(false)
const input = ref('')
const messages = ref<AssistantMessage[]>([])
const attachments = ref<AssistantAttachment[]>([])
const threadId = ref('')
const threads = ref<AssistantThread[]>([])
const historyVisible = ref(false)
const threadsLoading = ref(false)
const messageRef = ref<HTMLElement>()
const fileInput = ref<HTMLInputElement>()
const uploadingFiles = ref(0)
const guidanceSubmitting = ref(false)
const applyingIndex = ref<number | null>(null)
const startingModelTaskId = ref('')
const sourcePreviewVisible = ref(false)
const sourcePreviewLoading = ref(false)
const sourcePreview = ref<AssistantSource | null>(null)
const sourcePreviewText = ref('')
const expandedProposal = reactive<Record<number, boolean>>({})
const expandedModelPlans = reactive<Record<string, boolean>>({})
const expandedChangeLists = reactive<Record<string, boolean>>({})
const expandedIssueGroups = reactive<Record<string, boolean>>({})
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
const compilationLiveness = reactive<Record<string, AssistantCompilationLiveness[]>>({})
const recoveringModelProposalId = ref('')
const blockedModelProposalId = ref('')
const modelTaskRecoveryFailure = ref('')
let compilationStreamController: AbortController | null = null
let compilationRecoveryEpoch = 0
let lastProjectedCheckpoint = ''
let modelTaskRecoveryGeneration = 0
let streamGeneration = 0
let componentDisposed = false

const assistantConfigStorageKey = 'ontology-assistant-capabilities'

const context = computed(() => ({
  page: props.context.page || '工作台',
  path: props.context.path || '',
  scenario_id: props.context.scenario_id || '',
}))
const composerPlaceholder = '描述你要完成的工作，智能业务顾问会结合当前页面和上下文自动判断下一步'
const canSend = computed(() => Boolean(
  input.value.trim()
  || (attachments.value.length && attachments.value.every((item) => item.status === 'parsed'))
))
const assistantScopeKey = computed(() => `${context.value.scenario_id || 'global'}|${normalizedAssistantPath(context.value.path)}`)
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
const compilationAcceptingGuidance = computed(() => (
  compilationRunning.value
  && activeCompilationJob.value?.progress?.accepting_guidance !== false
))
const advisorWorking = computed(() => Boolean(
  compilationRunning.value || loading.value || modelTaskRecoveryBusy.value,
))
const launcherStatus = computed(() => {
  const job = activeCompilationJob.value
  if (compilationRunning.value && job) {
    const current = job.progress?.steps?.find((step) => step.status === 'running')
    const count = Number(job.progress?.draft_resource_count || 0)
    const liveness = latestCompilationLiveness(job.id)
    const elapsed = liveness ? formatElapsedSeconds(liveness.elapsed_seconds) : ''
    if (count > 0) return `${current?.title || '正在建模'} · 已同步 ${count} 项 · ${elapsed || '持续运行中'}`
    return `${current?.title || '正在建设场景模型'} · ${elapsed || '持续运行中'}`
  }
  if (modelTaskRecoveryBusy.value) return '正在恢复任务'
  return '正在回复'
})
const latestModelRunMessage = computed(() => [...messages.value].reverse().find(
  (message) => proposalOf(message)?.kind === 'scenario_model',
))
const activeModelRunMessage = computed(() => {
  const message = latestModelRunMessage.value
  const proposal = message ? proposalOf(message) : null
  return proposal?.status === 'in_progress'
    && modelExecutionSummary(proposal)?.final !== true
    ? message
    : undefined
})
const modelRunAwaitingConfirmation = computed(() => Boolean(activeModelRunMessage.value)
  && blockedModelProposalId.value !== proposalOf(activeModelRunMessage.value as AssistantMessage)?.proposal_id
  && modelNextAction(proposalOf(activeModelRunMessage.value as AssistantMessage))?.type === 'confirm_task')
const modelTaskRecoveryBusy = computed(() => Boolean(recoveringModelProposalId.value))
const fallbackCompilationSteps: AssistantCompilationStep[] = [
  { id: 'analyze', title: '分析业务资料', detail: '正在读取附件和用户补充描述。', status: 'running' },
  { id: 'plan', title: '制定建模任务', detail: '正在拆解建模范围和执行顺序。', status: 'pending' },
  { id: 'ontology', title: '建设本体模型', detail: '等待执行。', status: 'pending' },
  { id: 'mapping', title: '整理数据映射', detail: '等待执行。', status: 'pending' },
  { id: 'rules', title: '校验规则、事件与工作流', detail: '等待执行。', status: 'pending' },
  { id: 'review', title: '生成待审核变更清单', detail: '等待执行。', status: 'pending' },
  { id: 'result', title: '汇总执行结果', detail: '等待执行。', status: 'pending' },
]
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

function isActiveModelRun(message: AssistantMessage) {
  const active = activeModelRunMessage.value
  if (!active) return false
  if (active === message) return true
  const activeMessageId = String(active.id || '')
  const messageId = String(message.id || '')
  return Boolean(activeMessageId && messageId && activeMessageId === messageId)
}

function modelTasks(proposal: AssistantProposal | null): AssistantModelTask[] {
  if (proposal?.kind !== 'scenario_model') return []
  return Array.isArray(proposal.payload?.tasks)
    ? proposal.payload.tasks as AssistantModelTask[]
    : []
}

function modelExecutionSummary(proposal: AssistantProposal | null): AssistantModelExecutionSummary | null {
  if (proposal?.kind !== 'scenario_model') return null
  const summary = proposal.payload?.execution_summary
  return summary && typeof summary === 'object'
    ? summary as AssistantModelExecutionSummary
    : null
}

function modelTaskHasPersistedWrites(task: AssistantModelTask | undefined) {
  if (!task || !['applied', 'partially_applied'].includes(task.status)) return false
  const result = task.apply_result
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false
  if (String(result.kind || '') !== 'scenario_model') return false
  if (String(result.task_id || '') !== task.id) return false
  if (String(result.task_status || '') !== task.status) return false
  const appliedChangeKeys = Array.isArray(result.applied_change_keys)
    ? result.applied_change_keys.map(String).filter(Boolean)
    : []
  if (appliedChangeKeys.length) return true
  const counts = result.counts && typeof result.counts === 'object' && !Array.isArray(result.counts)
    ? result.counts
    : {}
  return Object.entries(counts).some(([key, value]) => (
    /_(?:added|updated|deleted|extended|filled)$/.test(key)
    && Number.isFinite(Number(value))
    && Number(value) > 0
  ))
}

function modelFullyAppliedTaskCount(proposal: AssistantProposal | null) {
  return modelTasks(proposal).filter((task) => task.status === 'applied' && modelTaskHasPersistedWrites(task)).length
}

function modelPartiallyAppliedTaskCount(proposal: AssistantProposal | null) {
  return modelTasks(proposal).filter((task) => task.status === 'partially_applied' && modelTaskHasPersistedWrites(task)).length
}

function modelPersistedTaskCount(proposal: AssistantProposal | null) {
  return modelFullyAppliedTaskCount(proposal) + modelPartiallyAppliedTaskCount(proposal)
}

function modelDraftOnlyTaskCount(proposal: AssistantProposal | null) {
  return modelTasks(proposal).filter((task) => ['deferred', 'drafted_with_gaps', 'skipped'].includes(task.status)).length
}

function modelEmptyTaskCount(proposal: AssistantProposal | null) {
  return modelTasks(proposal).filter((task) => task.status === 'empty').length
}

function modelRunHasPersistedWrites(proposal: AssistantProposal | null) {
  return modelPersistedTaskCount(proposal) > 0
}

function modelRunFinishedWithoutPersistedWrites(proposal: AssistantProposal | null) {
  return Boolean(modelExecutionSummary(proposal)?.final && !modelRunHasPersistedWrites(proposal))
}

function modelRunStatusType(proposal: AssistantProposal | null): 'primary' | 'success' | 'warning' | 'info' {
  const summary = modelExecutionSummary(proposal)
  if (!summary?.final) return 'primary'
  if (!modelRunHasPersistedWrites(proposal)) {
    return modelDraftOnlyTaskCount(proposal) || Number(summary.remaining_issue_count || 0) > 0
      ? 'warning'
      : 'info'
  }
  return summary.status === 'completed'
    && modelPartiallyAppliedTaskCount(proposal) === 0
    && modelDraftOnlyTaskCount(proposal) === 0
    ? 'success'
    : 'warning'
}

function modelRunSummaryTitle(proposal: AssistantProposal | null) {
  if (modelRunFinishedWithoutPersistedWrites(proposal)) {
    return modelDraftOnlyTaskCount(proposal)
      ? '建模计划已结束，仅保留草稿'
      : '建模计划已结束，无正式写入'
  }
  return modelRunStatusType(proposal) === 'success'
    ? '建模计划已完成并写入'
    : '全部任务已推进，存在待补全项'
}

function modelRunSummaryMessage(proposal: AssistantProposal | null) {
  const summary = modelExecutionSummary(proposal)
  if (!summary) return ''
  if (!modelRunFinishedWithoutPersistedWrites(proposal)) return summary.message
  const draftCount = modelDraftOnlyTaskCount(proposal)
  const sourceCount = Number(proposal?.payload?.coverage_summary?.total || 0)
  const taskTitles = modelTasks(proposal)
    .filter((task) => ['drafted_with_gaps', 'deferred', 'skipped'].includes(task.status))
    .map((task) => task.title)
  const causes = [...new Set(
    scenarioIssueGroups(proposal)
      .map((group) => `${scenarioModelIssueLabel(group.code)}（${group.count} 项）`),
  )].slice(0, 3)
  const lines = [
    sourceCount
      ? `我已完成对 **${sourceCount} 个来源段落** 的分析，并把结果整理为可继续编辑的场景建模草稿。`
      : '我已完成本轮资料分析，并把结果整理为可继续编辑的场景建模草稿。',
    draftCount
      ? `- 已形成 ${draftCount} 项任务草稿：${taskTitles.join('、') || '各场景建模任务'}`
      : '- 本轮没有形成可用的任务草稿',
    '- 正式写入：0 项；当前场景中的正式定义没有被修改',
  ]
  if (causes.length) lines.push(`- 主要待补全：${causes.join('；')}`)
  lines.push('下一步：展开下方“建模产物”查看具体来源、草稿和问题；补充信息后可以直接在当前对话继续。')
  return lines.join('\n')
}

function modelRunContextStatus(proposal: AssistantProposal | null) {
  if (!modelExecutionSummary(proposal)?.final) return 'waiting_confirmation'
  return modelRunHasPersistedWrites(proposal) ? 'success' : 'completed_without_writes'
}

function assistantMessageContent(message: AssistantMessage) {
  const content = String(message.content || '')
  const proposal = proposalOf(message)
  if (!modelRunFinishedWithoutPersistedWrites(proposal)) return content
  // The durable proposal ledger is authoritative for a finished run. When it
  // proves zero writes, do not expose older free-form completion prose that
  // can contradict that structured result.
  return modelRunSummaryMessage(proposal)
}

function proposalStatusType(proposal: AssistantProposal | null): 'primary' | 'success' | 'warning' | 'info' {
  if (!proposal) return 'primary'
  if (proposal.kind === 'scenario_model') {
    if (modelExecutionSummary(proposal)?.final) return modelRunStatusType(proposal)
    return ['completed_with_gaps', 'partially_applied'].includes(proposal.status || '') ? 'warning' : 'primary'
  }
  return ['applied', 'completed'].includes(proposal.status || '')
    ? 'success'
    : ['completed_with_gaps', 'partially_applied'].includes(proposal.status || '') ? 'warning' : 'primary'
}

function proposalStatusLabel(proposal: AssistantProposal | null) {
  if (!proposal) return '待确认'
  if (proposal.kind === 'scenario_model') {
    if (modelRunFinishedWithoutPersistedWrites(proposal)) {
      return modelDraftOnlyTaskCount(proposal) ? '已结束，仅保留草稿' : '已结束，无正式写入'
    }
    if (modelExecutionSummary(proposal)?.final) {
      return modelRunStatusType(proposal) === 'success' ? '已完成并写入' : '已推进，存在待补全'
    }
    return proposal.status === 'in_progress' ? '计划进行中 · 等待确认' : '待确认'
  }
  return ['applied', 'completed'].includes(proposal.status || '')
    ? '已完成'
    : proposal.status === 'completed_with_gaps' ? '已完成草稿，待补全'
      : proposal.status === 'partially_applied' ? '已应用可用部分'
        : proposal.status === 'in_progress' ? '计划进行中 · 等待确认' : '待确认'
}

function modelNextAction(proposal: AssistantProposal | null): AssistantModelNextAction | null {
  if (proposal?.kind !== 'scenario_model') return null
  const action = proposal.payload?.next_action
  return action && typeof action === 'object'
    ? action as AssistantModelNextAction
    : null
}

function modelRunRevision(proposal: AssistantProposal | null) {
  const revisions = [
    Number(proposal?.run_revision || 0),
    Number(proposal?.payload?.execution_revision || 0),
  ].filter((revision) => Number.isFinite(revision) && revision >= 0)
  return Math.trunc(Math.max(0, ...revisions))
}

function latestProposalMessage(items: AssistantMessage[], proposalId: string) {
  let latest: AssistantMessage | null = null
  let latestRevision = -1
  for (const item of items) {
    const candidate = proposalOf(item)
    if (String(candidate?.proposal_id || '') !== proposalId) continue
    const revision = modelRunRevision(candidate)
    if (!latest || revision >= latestRevision) {
      latest = item
      latestRevision = revision
    }
  }
  return latest
}

function modelTaskWasProcessed(proposal: AssistantProposal | null, taskId: string) {
  const task = modelTasks(proposal).find((item) => item.id === taskId)
  return Boolean(task && ['applied', 'partially_applied', 'deferred', 'drafted_with_gaps', 'skipped', 'empty'].includes(task.status))
}

function modelTaskOf(proposal: AssistantProposal | null, taskId: string) {
  return modelTasks(proposal).find((item) => item.id === taskId)
}

function modelTaskNeedsWriteVerification(task: AssistantModelTask) {
  return ['applied', 'partially_applied'].includes(task.status) && !modelTaskHasPersistedWrites(task)
}

function isCurrentModelTask(proposal: AssistantProposal | null, task: AssistantModelTask) {
  return String(proposal?.payload?.current_task_id || '') === task.id
}

function waitingTaskTitles(proposal: AssistantProposal | null, task: AssistantModelTask) {
  const tasks = modelTasks(proposal)
  const waitingFor = task.waiting_for?.length ? task.waiting_for : task.depends_on
  const titles = waitingFor.map((id) => tasks.find((item) => item.id === id)?.title || id)
  return titles.length ? titles : ['前一项任务']
}

function modelTaskProgress(proposal: AssistantProposal | null) {
  const tasks = modelTasks(proposal)
  if (!tasks.length) return ''
  const summary = modelExecutionSummary(proposal)
  const completed = summary?.completed_task_count
    ?? tasks.filter((task) => ['applied', 'partially_applied', 'deferred', 'drafted_with_gaps', 'skipped', 'empty'].includes(task.status)).length
  return summary?.final
    ? `${completed}/${tasks.length} 项已推进`
    : `${completed}/${tasks.length} 项 · 计划进行中`
}

function modelTaskStatusLabel(task?: AssistantModelTask) {
  if (!task) return '待处理'
  if (modelTaskNeedsWriteVerification(task)) return '写入结果待核验'
  const status = task.status
  return ({
    empty: '无此类变更',
    ready: '等待确认',
    blocked: '有缺口，等待确认',
    waiting: '等待当前任务',
    awaiting_generation: '等待开始生成',
    applied: '已应用',
    partially_applied: '已应用安全部分',
    deferred: '草稿已保留',
    drafted_with_gaps: '草稿已建，待补全',
    skipped: '草稿已保留',
  } as Record<string, string>)[status] || '待处理'
}

function modelTaskStatusType(task?: AssistantModelTask) {
  if (!task) return 'info'
  if (modelTaskNeedsWriteVerification(task)) return 'warning'
  const status = task.status
  return ({
    empty: 'info',
    ready: 'warning',
    blocked: 'warning',
    waiting: 'info',
    awaiting_generation: 'primary',
    applied: 'success',
    partially_applied: 'warning',
    deferred: 'info',
    drafted_with_gaps: 'warning',
    skipped: 'info',
  } as Record<string, string>)[status] || 'info'
}

function taskIssueCount(task: AssistantModelTask) {
  const groupedCount = (task.issues || []).reduce((total, issue: any) => {
    const count = Number(issue?.count)
    return total + (Number.isFinite(count) && count > 0 ? Math.trunc(count) : 1)
  }, 0)
  const persistedCount = Number((task as any).issue_count)
  return Math.max(
    groupedCount,
    Number.isFinite(persistedCount) && persistedCount >= 0
      ? Math.trunc(persistedCount)
      : 0,
  )
}

function modelTaskOutputCount(task?: AssistantModelTask) {
  if (!task) return 0
  const values = [
    Number((task as any).output_count),
    Number((task as any).draft_candidate_count),
    Number(task.change_count),
  ].filter((value) => Number.isFinite(value) && value >= 0)
  return Math.trunc(Math.max(0, ...values))
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
  if (proposal.status === 'partially_applied') return '已确认并写入'
  return ({ scenario: '确认并创建场景', mapping: '确认并保存映射', ontology: '确认并应用本体', workflow: '确认并保存流程', scenario_model: '确认并原子应用' } as Record<string, string>)[proposal.kind] || '确认并应用变更'
}

function proposalApplyHint(proposal: AssistantProposal | null) {
  if (!proposal?.proposal_id) return '此草稿缺少安全标识，请重新生成'
  if (proposal.kind === 'scenario' && context.value.scenario_id) return '场景草稿只能在全局工作区创建'
  if (proposal.kind !== 'scenario' && !context.value.scenario_id) return '请先打开业务场景'
  if (proposal.kind === 'scenario_model' && blockingIssues(proposal).length) {
    return `当前有 ${blockingIssues(proposal).length} 类待补全内容；确认后仍会把本任务定义写入画布，问题只在助手会话中汇总`
  }
  if (proposal.kind === 'scenario_model' && !hasApplyableChanges(proposal)) return '当前任务没有可写入的定义；请继续补充附件或描述后重新编译'
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

function modelPlanKey(message: AssistantMessage, index: number) {
  return `${messageKey(message, index)}::__model-plan__`
}

function isModelPlanExpanded(message: AssistantMessage, index: number) {
  return Boolean(expandedModelPlans[modelPlanKey(message, index)])
}

function toggleModelPlan(message: AssistantMessage, index: number) {
  const key = modelPlanKey(message, index)
  expandedModelPlans[key] = !isModelPlanExpanded(message, index)
}

function currentModelTask(proposal: AssistantProposal | null) {
  const tasks = modelTasks(proposal)
  return tasks.find((task) => isCurrentModelTask(proposal, task))
    || tasks.find((task) => ['ready', 'blocked', 'awaiting_generation'].includes(task.status))
}

function modelPlanSummary(proposal: AssistantProposal | null) {
  const summary = modelExecutionSummary(proposal)
  if (summary?.final) return '已完成的任务、草稿和待补全项仍可随时查看。'
  if (modelNextAction(proposal)?.type === 'generate_task' && summary?.current_task_title) {
    return `前置任务已处理，等待你开始生成「${summary.current_task_title}」。`
  }
  if (summary?.current_task_title) return `当前停在「${summary.current_task_title}」，等待你的确认。`
  return '按依赖顺序执行；需要写入时会明确向你确认。'
}

function hasToolWork(message: AssistantMessage) {
  const routing = (message.context as Record<string, any> | undefined)?.routing
  return Boolean(
    message.context?.compilation_job_id
    || proposalOf(message)?.kind === 'scenario_model'
    || routing?.intent === 'scenario_model'
    || assistantMessageActivities(message).length,
  )
}

function visibleWorkSteps(message: AssistantMessage) {
  const steps = message.thinking || []
  if (message.streaming || hasToolWork(message)) return steps
  return steps.filter((step) => (
    !['routing', 'context', 'response'].includes(String(step.id))
    || step.status === 'error'
  ))
}

function workTranscriptEntries(message: AssistantMessage): AssistantCompilationActivity[] {
  const activities = assistantMessageActivities(message)
  if (!activities.length) {
    return visibleWorkSteps(message).map((step) => ({
      id: `step:${step.id}`,
      kind: 'stage' as const,
      step_id: step.id,
      title: step.title,
      detail: String(step.detail || ''),
      status: step.status || 'pending',
    }))
  }
  const grouped = new Map<string, AssistantCompilationActivity & { count?: number }>()
  for (const activity of activities) {
    const key = `${activity.kind}:${activity.step_id || activity.id}`
    const existing = grouped.get(key)
    if (!existing) {
      grouped.set(key, activity.kind === 'model'
        ? {
            ...activity,
            title: '模型分析',
            detail: activity.status === 'running'
              ? '正在处理当前阶段；本阶段已发起 1 次受控模型调用。'
              : '当前阶段的 1 次受控模型调用已经结束。',
            count: 1,
          }
        : { ...activity, count: 1 })
      continue
    }
    const count = Number(existing.count || 1) + 1
    Object.assign(existing, activity, { count })
    if (activity.kind === 'model') {
      existing.title = '模型分析'
      existing.detail = activity.status === 'running'
        ? `正在处理当前阶段；本阶段已发起 ${count} 次受控模型调用。`
        : `当前阶段的 ${count} 次受控模型调用已经结束。`
    }
  }
  return [...grouped.values()].map(({ count: _count, ...entry }) => entry).slice(-16)
}

function shouldShowWorkLog(message: AssistantMessage) {
  return message.role === 'assistant' && Boolean(
    message.streaming
    || visibleWorkSteps(message).length
    || assistantMessageActivities(message).length,
  )
}

function workLogSummary(message: AssistantMessage) {
  const steps = visibleWorkSteps(message)
  const running = steps.find((step) => step.status === 'running')
  if (running) return running.title
  const error = steps.find((step) => step.status === 'error')
  if (error) return error.title
  const pending = steps.find((step) => step.status === 'pending')
  if (pending) return `等待执行：${pending.title}`
  if (assistantMessageActivities(message).length) return '本次建模的工作记录'
  return message.streaming ? '正在理解你的请求' : `已完成 ${steps.length} 个处理步骤`
}

function upsertThinking(message: AssistantMessage, step: AssistantThought) {
  const thinking = (message.thinking || []) as AssistantThought[]
  const existing = thinking.find((item) => item.id === step.id)
  if (existing) Object.assign(existing, step)
  else thinking.push(step)
  message.thinking = thinking
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

function compilationPathFromScopeKey(scopeKey: string | null | undefined, scenarioId: string) {
  const prefix = `scenario:${scenarioId}|path:`
  const value = String(scopeKey || '')
  if (!value.startsWith(prefix)) return ''
  const path = value.slice(prefix.length).trim()
  return safeScenarioAssistantPath(path, scenarioId)
}

function normalizedAssistantPath(path: string | null | undefined) {
  return (String(path || '/') || '/').split('?', 1)[0].split('#', 1)[0] || '/'
}

function safeScenarioAssistantPath(path: string | null | undefined, scenarioId: string) {
  const normalized = normalizedAssistantPath(path)
  if (!/^\/(?!\/)[^\\\u0000-\u001f\u007f]*$/.test(normalized)) return ''
  return normalized === `/scenarios/${encodeURIComponent(scenarioId)}` ? normalized : ''
}

function clearCompilationStream() {
  compilationStreamController?.abort()
  compilationStreamController = null
}

function detachCompilationRecovery(clearStatus = true) {
  clearCompilationStream()
  compilationRecoveryEpoch += 1
  compilationRecoveryThreadId.value = ''
  if (clearStatus) {
    const jobId = activeCompilationJob.value?.id
    if (jobId) delete compilationLiveness[jobId]
    activeCompilationJob.value = null
  }
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

function compilationProgressDetail(job: AssistantCompilationJobStatus) {
  const detail = String(job.progress?.detail || '').trim()
  if (detail) return detail
  if (job.status === 'running') return '任务仍在服务端运行；恢复过程只查询状态，不会重新提交聊天请求。'
  if (job.status === 'succeeded') return '服务端任务已完成，正在恢复持久化变更清单。'
  return job.error_message || '系统已保持零写入。请修改附件或描述后显式重试。'
}

function compilationElapsedSeconds(job: AssistantCompilationJobStatus) {
  const startedAt = Date.parse(String(job.started_at || ''))
  const completedAt = Date.parse(String(job.completed_at || ''))
  if (!Number.isFinite(startedAt)) return 0
  const endedAt = job.status === 'running' || !Number.isFinite(completedAt)
    ? Date.now()
    : completedAt
  return Math.max(0, Math.floor((endedAt - startedAt) / 1000))
}

function formatElapsedSeconds(rawSeconds: number) {
  const seconds = Math.max(0, Math.floor(Number(rawSeconds) || 0))
  if (seconds < 60) return `已运行 ${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `已运行 ${minutes} 分 ${remainder} 秒`
}

function compilationElapsedLabel(job: AssistantCompilationJobStatus) {
  if (!job.started_at) return ''
  return formatElapsedSeconds(compilationElapsedSeconds(job)).replace('已运行', '已耗时')
}

function compilationRuntimeSummary(job: AssistantCompilationJobStatus) {
  const elapsed = compilationElapsedLabel(job)
  const used = Number(job.llm_calls_used || job.progress?.calls_used || 0)
  const budget = Number(job.llm_call_budget || job.progress?.call_budget || 0)
  const calls = budget > 0 ? `模型调用 ${used}/${budget}` : ''
  return [elapsed, calls].filter(Boolean).join(' · ')
}

function compilationNarrative(job: AssistantCompilationJobStatus) {
  const steps = job.progress?.steps?.length ? job.progress.steps : fallbackCompilationSteps
  const completed = steps.filter((step) => step.status === 'done').map((step) => step.title)
  const current = steps.find((step) => step.status === 'running')
    || steps.find((step) => step.status === 'error')
  const detail = compilationProgressDetail(job)
  const runtime = compilationRuntimeSummary(job)
  if (job.status === 'failed') {
    return `这次场景建模在「${current?.title || '当前阶段'}」停止，未写入正式资源。\n\n${[runtime, detail].filter(Boolean).join('；')}\n\n我已保留本次可恢复的上下文；补充资料或调整要求后，可以从当前会话重新发起。`
  }
  if (job.status === 'succeeded') {
    return `业务资料分析和草稿生成已经完成。\n\n${[runtime, detail].filter(Boolean).join('；')}\n\n我正在读取可核对的结果；涉及正式写入时，会在对应任务处明确等待你的确认。`
  }
  const completedText = completed.length ? `已完成：${completed.join('、')}。` : '我正在先梳理资料和建模范围。'
  const currentText = current ? `当前在处理「${current.title}」：${current.detail}` : detail
  const progressBoundary = '我会持续回显真实阶段和调用记录；如果资料需要分段处理，会明确说明当前正在处理哪一部分。'
  return `我正在处理这份业务资料。\n\n${completedText}\n${currentText}\n\n${[runtime, detail].filter(Boolean).join('；')}\n\n${progressBoundary}\n\n每个需要正式写入的任务都会先在这里说明影响并等待你的确认。`
}

function activityKindLabel(kind: string) {
  if (kind === 'model') return '模型调用'
  if (kind === 'tool') return '工具调用'
  return '阶段'
}

function assistantMessageActivities(message: AssistantMessage): AssistantCompilationActivity[] {
  const raw = message.context?.compilation_progress?.activities
  if (!Array.isArray(raw)) return []
  return raw.slice(-12) as AssistantCompilationActivity[]
}

function latestCompilationLiveness(jobId: string) {
  const entries = compilationLiveness[jobId] || []
  return entries[entries.length - 1]
}

function compilationLiveEntries(message: AssistantMessage) {
  const jobId = String(message.context?.compilation_job_id || '').trim()
  return jobId ? compilationLiveness[jobId] || [] : []
}

function formatLivenessClock(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚'
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function recordCompilationLiveness(raw: Record<string, any>) {
  const job = activeCompilationJob.value
  const jobId = String(raw.job_id || '').trim()
  const scenarioId = String(raw.scenario_id || '').trim()
  if (!job || !jobId || job.id !== jobId) return
  if (scenarioId && scenarioId !== context.value.scenario_id) return
  const emittedAt = Number.isFinite(Date.parse(String(raw.emitted_at || '')))
    ? String(raw.emitted_at)
    : new Date().toISOString()
  const entry: AssistantCompilationLiveness = {
    job_id: jobId,
    thread_id: raw.thread_id || job.thread_id || null,
    scenario_id: scenarioId || job.scenario_id || null,
    status: 'running',
    stream_state: raw.stream_state === 'disconnected' ? 'disconnected' : 'connected',
    emitted_at: emittedAt,
    elapsed_seconds: Math.max(0, Number(raw.elapsed_seconds ?? compilationElapsedSeconds(job)) || 0),
    phase: String(raw.phase || job.progress?.phase || ''),
    current_step: String(raw.current_step || job.progress?.current_step || ''),
    stage_title: String(raw.stage_title || job.progress?.steps?.find((step) => step.status === 'running')?.title || '场景建模'),
    message: String(raw.message || '服务端任务仍在运行，新的阶段结果会立即同步到当前对话。'),
    calls_used: Math.max(0, Number(raw.calls_used ?? job.llm_calls_used) || 0),
    call_budget: Math.max(0, Number(raw.call_budget ?? job.llm_call_budget) || 0),
    draft_checkpoint_revision: Math.max(0, Number(raw.draft_checkpoint_revision ?? job.progress?.draft_checkpoint_revision) || 0),
    draft_resource_count: Math.max(0, Number(raw.draft_resource_count ?? job.progress?.draft_resource_count) || 0),
    guidance_pending_count: Math.max(0, Number(raw.guidance_pending_count ?? job.progress?.guidance_pending_count) || 0),
  }
  const previous = compilationLiveness[jobId] || []
  if (previous.some((item) => item.emitted_at === entry.emitted_at)) return
  compilationLiveness[jobId] = [...previous, entry].slice(-4)
  scrollBottom()
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
  message.streaming = job.status === 'running'
  message.context = {
    ...(message.context || {}),
    compilation_job_id: job.id,
    status: job.status === 'running' ? 'processing' : job.status,
    compilation_progress: job.progress,
  }
  if (!message.proposal) {
    message.content = compilationNarrative(job)
  }
  const steps = job.progress?.steps || []
  if (steps.length) {
    steps.forEach((step) => upsertThinking(message, {
      id: step.id,
      title: step.title,
      detail: step.detail,
      status: step.status,
    }))
  } else {
    upsertThinking(message, {
      id: 'scenario-model',
      title: job.status === 'running' ? '执行完整场景建模' : job.status === 'succeeded' ? '完整场景建模已完成' : '完整场景建模未完成',
      detail: compilationProgressDetail(job),
      status: job.status === 'running' ? 'running' : job.status === 'succeeded' ? 'done' : 'error',
    })
  }
  scrollBottom()
}

function projectDraftCheckpoint(data: Record<string, any>) {
  const scenarioId = String(data.scenario_id || context.value.scenario_id || '').trim()
  const jobId = String(data.job_id || activeCompilationJob.value?.id || '').trim()
  const revision = Math.max(0, Number(data.revision || data.draft_checkpoint_revision || 0))
  if (!scenarioId || scenarioId !== context.value.scenario_id || !jobId || revision <= 0) return
  const identity = `${jobId}:${revision}`
  if (identity === lastProjectedCheckpoint) return
  lastProjectedCheckpoint = identity
  window.dispatchEvent(new CustomEvent('assistant-scenario-drafts-updated', {
    detail: {
      scenario_id: scenarioId,
      job_id: jobId,
      revision,
      resource_count: Number(data.resource_count || data.draft_resource_count || 0),
      live: true,
    },
  }))
}

function applyCompilationSnapshot(
  data: Record<string, any>,
  scope: CompilationRecoveryThreadScope,
) {
  const job = data?.job as AssistantCompilationJobStatus | undefined
  if (!job || !compilationJobMatchesScenario(job, scope.scenarioId)) {
    clearCompilationJobBookmark(localStorage, scope)
    detachCompilationRecovery()
    ElMessage.error('检测到跨场景的建模事件，已拒绝关联。')
    return
  }
  projectDraftCheckpoint({
    scenario_id: scope.scenarioId,
    job_id: job.id,
    draft_checkpoint_revision: job.progress?.draft_checkpoint_revision,
    draft_resource_count: job.progress?.draft_resource_count,
  })
  activeCompilationJob.value = job
  markCompilationMessage(job, scope)
  const durable = data?.message as AssistantMessage | undefined
  if (durable?.role === 'assistant') {
    const message = messages.value.find((item) => (
      item.context?.compilation_job_id === job.id
      || (durable.id && item.id === durable.id)
    ))
    if (message) {
      Object.assign(message, durable, { streaming: job.status === 'running' })
    } else {
      messages.value.push({ ...durable, streaming: job.status === 'running' })
    }
  }
  if (job.status === 'running') {
    saveCompilationJobBookmark(localStorage, scope, job.id)
    return
  }
  delete compilationLiveness[job.id]
  clearCompilationJobBookmark(localStorage, scope)
  const owner = compilationOwnerScope()
  if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
  clearCompilationStream()
  compilationRecoveryThreadId.value = ''
  compilationRecoveryEpoch += 1
  loading.value = false
  const proposal = durable ? proposalOf(durable) : null
  if (proposal?.kind === 'scenario_model') {
    window.dispatchEvent(new CustomEvent('assistant-scenario-drafts-updated', {
      detail: { scenario_id: scope.scenarioId, proposal_id: proposal.proposal_id },
    }))
  }
  if (job.status === 'succeeded') {
    activeCompilationJob.value = null
  } else {
    ElMessage.error(job.error_message || '完整业务模型编译未完成，系统已保持零写入。')
  }
  scrollBottom()
}

function attachCompilationEventStream(
  job: AssistantCompilationJobStatus,
  scope: CompilationRecoveryThreadScope,
) {
  clearCompilationStream()
  compilationRecoveryEpoch += 1
  const epoch = compilationRecoveryEpoch
  compilationRecoveryThreadId.value = scope.threadId
  activeCompilationJob.value = job
  saveCompilationJobBookmark(localStorage, scope, job.id)
  markCompilationMessage(job, scope)
  let controller: AbortController | null = null
  controller = streamAssistantCompilationJob(
    job.id,
    (event) => {
      if (!controller || compilationStreamController !== controller) return
      if (!compilationRecoveryIsCurrent(epoch, scope)) return
      if (event.type === 'compilation_liveness') {
        recordCompilationLiveness(event.data || {})
      } else if (event.type === 'compilation_progress' || event.type === 'compilation_result') {
        applyCompilationSnapshot(event.data || {}, scope)
      }
    },
    () => {
      if (controller && compilationStreamController === controller) {
        compilationStreamController = null
      }
    },
    (error) => {
      if (!controller || compilationStreamController !== controller) return
      compilationStreamController = null
      if (!compilationRecoveryIsCurrent(epoch, scope)) return
      if (activeCompilationJob.value?.status === 'running') {
        recordCompilationLiveness({
          job_id: activeCompilationJob.value.id,
          thread_id: activeCompilationJob.value.thread_id,
          scenario_id: activeCompilationJob.value.scenario_id,
          stream_state: 'disconnected',
          emitted_at: new Date().toISOString(),
          elapsed_seconds: compilationElapsedSeconds(activeCompilationJob.value),
          stage_title: activeCompilationJob.value.progress?.steps?.find((step) => step.status === 'running')?.title || '场景建模',
          message: '没有收到新的服务端事件；任务记录仍已保留，重新打开会话即可续接同一次执行。',
        })
        activeCompilationJob.value = {
          ...activeCompilationJob.value,
          progress: {
            ...(activeCompilationJob.value.progress || {}),
            detail: '对话事件流暂时中断；服务端任务仍会继续。重新打开会话即可续接同一条事件流。',
          },
        }
        markCompilationMessage(activeCompilationJob.value, scope)
      }
      ElMessage.warning(error.message || '场景建模事件流已中断')
    },
  )
  compilationStreamController = controller
}

async function recoverSucceededCompilation(
  job: AssistantCompilationJobStatus,
  epoch: number,
  scope: CompilationRecoveryThreadScope,
) {
  clearCompilationStream()
  const result = await api.getAssistantCompilationJobResult(job.id)
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  if (result.job_id !== job.id || result.scenario_id !== scope.scenarioId) {
    clearCompilationJobBookmark(localStorage, scope)
    detachCompilationRecovery()
    ElMessage.error('编译结果与当前业务场景不一致，已拒绝恢复。')
    return
  }

  const proposalThreadId = String(result.proposal_thread_id || scope.threadId).trim()
  const resultThreadId = proposalThreadId
  const canonicalPath = compilationPathFromScopeKey(result.proposal_scope_key, scope.scenarioId)
    || (resultThreadId === scope.threadId
      ? safeScenarioAssistantPath(context.value.path, scope.scenarioId)
      : '')
  if (!canonicalPath) {
    throw new Error('编译结果缺少权威会话范围，系统将继续重试，避免恢复到错误页面。')
  }
  const recoveryContext = { scenario_id: scope.scenarioId, path: canonicalPath }
  let recoveredMessages = await api.listAssistantMessages(resultThreadId, recoveryContext)
  if (!compilationRecoveryIsCurrent(epoch, scope)) return
  const proposal = result.proposal as AssistantProposal | undefined
  const proposalId = String(proposal?.proposal_id || '')
  if (result.apply_ready && !proposalId) {
    throw new Error('编译结果缺少可恢复的持久化计划，系统将继续重试。')
  }
  let hasProposalMessage = Boolean(proposalId && latestProposalMessage(recoveredMessages, proposalId))
  if (result.apply_ready && proposalId && !hasProposalMessage) {
    // The result endpoint guarantees a durable canonical message. A list/read
    // race must converge by reading that row again, never by inventing a
    // browser-only proposal that disappears on refresh.
    recoveredMessages = await api.listAssistantMessages(resultThreadId, recoveryContext)
    if (!compilationRecoveryIsCurrent(epoch, scope)) return
    hasProposalMessage = Boolean(latestProposalMessage(recoveredMessages, proposalId))
    if (!hasProposalMessage) {
      throw new Error('编译结果已完成，但持久化任务计划暂未可见；系统将继续重试恢复。')
    }
  }
  const durableMessage = proposalId
    ? latestProposalMessage(recoveredMessages, proposalId)
    : null
  const durableProposal = durableMessage ? proposalOf(durableMessage) : null
  if (result.apply_ready && modelRunRevision(durableProposal) < modelRunRevision(proposal || null)) {
    throw new Error('持久化任务计划仍是旧版本，系统将继续重试直至恢复最新进度。')
  }

  const currentPath = normalizedAssistantPath(context.value.path)
  if (canonicalPath !== currentPath) {
    const canonicalStorageKey = `ontology-assistant-thread:${encodeURIComponent(`${scope.scenarioId}|${canonicalPath}`)}`
    const previousCanonicalThreadId = localStorage.getItem(canonicalStorageKey)
    localStorage.setItem(canonicalStorageKey, resultThreadId)
    const navigationResult = await router.push(canonicalPath)
    await nextTick()
    const reachedCanonicalPath = normalizedAssistantPath(router.currentRoute.value.path) === canonicalPath
    if (isNavigationFailure(navigationResult) && !reachedCanonicalPath) {
      if (previousCanonicalThreadId === null) localStorage.removeItem(canonicalStorageKey)
      else localStorage.setItem(canonicalStorageKey, previousCanonicalThreadId)
      throw new Error('计划所属页面暂时无法打开；系统会保留当前恢复任务并继续重试。')
    }
    clearCompilationJobBookmark(localStorage, scope)
    const owner = compilationOwnerScope()
    if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
    streamGeneration += 1
    streamController = null
    loading.value = false
    ElMessage.success('持续建模计划已恢复，已返回该计划所属页面。')
    return
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
  window.dispatchEvent(new CustomEvent('assistant-scenario-drafts-updated', {
    detail: { scenario_id: scope.scenarioId, proposal_id: proposalId },
  }))
  activeCompilationJob.value = null
  compilationRecoveryThreadId.value = ''
  compilationRecoveryEpoch += 1
  scrollBottom()
  ElMessage.success(result.apply_ready ? '持续建模计划已恢复，并停留在当前确认点' : '建模草稿已恢复')
}

async function recoverFailedCompilation(
  job: AssistantCompilationJobStatus,
  epoch: number,
  scope: CompilationRecoveryThreadScope,
) {
  clearCompilationStream()
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
    attachCompilationEventStream(job, scope)
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
  clearCompilationStream()
  compilationRecoveryEpoch += 1
  compilationRecoveryThreadId.value = id
  const bookmarkedJobId = readCompilationJobBookmark(localStorage, scope)
  const pendingJobId = readPendingCompilationJobBookmark(localStorage, scope)
  const placeholderJobId = String(
    [...messages.value].reverse().find((message) => (
      message.thread_id === id
      && message.context?.compilation_job_id
      && ['processing', 'running'].includes(String(message.context?.status || ''))
    ))?.context?.compilation_job_id || '',
  ).trim()
  const knownJobId = bookmarkedJobId || pendingJobId || placeholderJobId
  if (!knownJobId) {
    activeCompilationJob.value = null
    compilationRecoveryThreadId.value = ''
    return
  }
  const job = optimisticCompilationJob({
    job_id: knownJobId,
    thread_id: id,
    scenario_id: scope.scenarioId,
    progress: {
      phase: 'recovering',
      detail: '正在续接同一个场景建模事件流；不会重复提交聊天或建模请求。',
      steps: fallbackCompilationSteps,
    },
  })
  activeCompilationJob.value = job
  saveCompilationJobBookmark(localStorage, scope, job.id)
  if (pendingJobId === job.id) clearPendingCompilationJobBookmark(localStorage, scope)
  attachCompilationEventStream(job, scope)
}

function beginCompilationRecoveryFromEvent(data: Record<string, any>) {
  const owner = compilationOwnerScope()
  const jobId = String(data?.job_id || '').trim()
  if (!owner || !jobId) return
  savePendingCompilationJobBookmark(localStorage, owner, jobId)
  const eventThreadId = String(data?.thread_id || threadId.value || '').trim()
  if (eventThreadId && !threadId.value) {
    threadId.value = eventThreadId
    localStorage.setItem(storageKey.value, eventThreadId)
    syncThread(eventThreadId, '完整业务模型持续任务')
  }
  const existingScope = compilationThreadScope(eventThreadId || threadId.value)
  if (existingScope) {
    compilationRecoveryThreadId.value = existingScope.threadId
    saveCompilationJobBookmark(localStorage, existingScope, jobId)
    activeCompilationJob.value = optimisticCompilationJob(data)
  }
}

function settleCompilationFromStream(proposal?: AssistantProposal | Record<string, any>) {
  if (!proposal || proposal.kind !== 'scenario_model') return
  window.dispatchEvent(new CustomEvent('assistant-scenario-drafts-updated', {
    detail: { scenario_id: context.value.scenario_id, proposal_id: proposal.proposal_id },
  }))
  const scope = compilationThreadScope(compilationRecoveryThreadId.value || threadId.value)
  if (scope) clearCompilationJobBookmark(localStorage, scope)
  const owner = compilationOwnerScope()
  if (owner) clearPendingCompilationJobBookmark(localStorage, owner)
  detachCompilationRecovery()
}

function onCompilationVisibilityChange() {
  if (document.hidden || compilationStreamController) return
  const job = activeCompilationJob.value
  const scope = compilationThreadScope(compilationRecoveryThreadId.value)
  if (!job || job.status !== 'running' || !scope) return
  attachCompilationEventStream(job, scope)
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
      ? `我已进入「${context.value.page}」工作区。你可以直接提问，也可以提供附件或描述建模目标；我会结合上下文判断是否需要调用资料解析、场景建模或其他内部能力。`
      : '你可以直接咨询业务问题，也可以描述需要建设的场景；我会根据上下文选择合适的内部能力，并在产生变更前明确说明。',
  }
}

async function loadThread(id: string, closeHistory = true) {
  detachCompilationRecovery()
  clearModelTaskRecovery()
  if (id !== threadId.value) attachments.value = []
  try {
    messages.value = await api.listAssistantMessages(id, apiContext())
    threadId.value = id
    localStorage.setItem(storageKey.value, id)
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
    Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
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
  if (!threadId.value) {
    await loadContext()
    return
  }
  historyVisible.value = false
  onCompilationVisibilityChange()
  scrollBottom()
}

async function toggleHistory() {
  historyVisible.value = !historyVisible.value
  if (historyVisible.value) await loadThreads()
}

async function createNewThread() {
  if (loading.value || threadsLoading.value) return
  try {
    detachCompilationRecovery()
    clearModelTaskRecovery()
    const thread = await api.createAssistantThread(apiContext())
    threads.value = [thread, ...threads.value.filter((item) => item.id !== thread.id)]
    threadId.value = thread.id
    localStorage.setItem(storageKey.value, thread.id)
    messages.value = [welcomeMessage()]
    attachments.value = []
    Object.keys(expandedProposal).forEach((key) => delete expandedProposal[Number(key)])
    Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
    Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
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
      clearModelTaskRecovery()
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

function optimisticCompilationJob(data: Record<string, any>): AssistantCompilationJobStatus {
  const now = new Date().toISOString()
  const status = ['running', 'succeeded', 'failed'].includes(String(data.status))
    ? String(data.status) as AssistantCompilationJobStatus['status']
    : 'running'
  const progress = data.progress && typeof data.progress === 'object'
    ? data.progress
    : { phase: 'queued', detail: '已取得编译任务，正在等待服务端进度。', steps: fallbackCompilationSteps }
  return {
    id: String(data.job_id || ''),
    thread_id: data.thread_id || threadId.value || null,
    scenario_id: context.value.scenario_id || null,
    status,
    progress,
    llm_calls_used: Number(data.llm_calls_used || progress.calls_used || 0),
    llm_call_budget: Number(data.llm_call_budget || progress.call_budget || 0),
    result_ready: status === 'succeeded',
    error_code: String(data.error_code || ''),
    error_message: String(data.error_message || ''),
    started_at: String(data.started_at || now),
    completed_at: data.completed_at || null,
    updated_at: String(data.updated_at || now),
  }
}

function handleAssistantEvent(event: { type: string; data: any }, ai: AssistantMessage) {
  switch (event.type) {
    case 'compilation_job':
      if (event.data?.job_id) {
        // Render the durable task immediately.  Recovery still re-reads the
        // server DTO below, but the user must see that a real compilation was
        // accepted even when that follow-up GET is slower than the SSE stream.
        activeCompilationJob.value = optimisticCompilationJob(event.data)
        const eventThreadId = String(event.data.thread_id || threadId.value || '').trim()
        if (eventThreadId) {
          if (!threadId.value) {
            threadId.value = eventThreadId
            localStorage.setItem(storageKey.value, eventThreadId)
            syncThread(eventThreadId, '完整业务模型持续任务')
          }
          compilationRecoveryThreadId.value = eventThreadId
        }
        // The job event arrives before the final meta event on a brand-new
        // thread. Bind it to the already visible streaming response so the
        // recovery poller updates that card instead of inserting a second
        // "processing" assistant message for the same server-side job.
        ai.context = {
          ...(ai.context || {}),
          compilation_job_id: String(event.data.job_id),
          status: 'processing',
        }
        if (event.data.assistant_message_id) ai.id = String(event.data.assistant_message_id)
      }
      beginCompilationRecoveryFromEvent(event.data || {})
      break
    case 'tool_event': {
      const activity = event.data?.activity || {}
      const status = activity.status === 'error'
        ? 'error'
        : activity.status === 'done' || activity.status === 'completed'
          ? 'done'
          : 'running'
      upsertThinking(ai, {
        id: `tool-${String(activity.id || activity.step_id || Date.now())}`,
        title: `${String(event.data?.tool_label || '内部能力')} · ${String(activity.title || '执行工具')}`,
        detail: String(activity.detail || ''),
        status,
      })
      break
    }
    case 'draft_checkpoint':
      projectDraftCheckpoint(event.data || {})
      break
    case 'compilation_liveness':
      recordCompilationLiveness(event.data || {})
      break
    case 'compilation_progress':
    case 'compilation_result': {
      const eventThreadId = String(event.data?.job?.thread_id || threadId.value || '').trim()
      const scope = compilationThreadScope(eventThreadId)
      if (scope) applyCompilationSnapshot(event.data || {}, scope)
      break
    }
    case 'progress':
      if (event.data?.id !== 'routing' && ai.thinking?.some((step) => step.id === 'routing' && step.status === 'running')) {
        upsertThinking(ai, {
          id: 'routing',
          title: '规划当前任务',
          detail: '处理路径已确定，开始执行对应任务。',
          status: 'done',
        })
      }
      upsertThinking(ai, event.data as AssistantThought)
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
      upsertThinking(ai, { id: 'error', title: '处理未完成', detail: '助手遇到问题，请检查配置后重试。', status: 'error' })
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

async function submitCompilationGuidance(content: string) {
  const job = activeCompilationJob.value
  const scope = compilationThreadScope(compilationRecoveryThreadId.value || threadId.value)
  if (!job || job.status !== 'running' || !scope || guidanceSubmitting.value) return
  const currentAttachments = [...attachments.value]
  guidanceSubmitting.value = true
  try {
    const response = await api.submitAssistantCompilationGuidance(job.id, {
      request_id: typeof crypto?.randomUUID === 'function'
        ? crypto.randomUUID()
        : `guidance-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      message: content,
      attachment_ids: currentAttachments.map((item) => item.id),
    })
    input.value = ''
    attachments.value = []
    activeCompilationJob.value = response.job
    if (response.message) {
      messages.value.push({ ...response.message, streaming: false })
    } else if (response.accepted) {
      messages.value.push({
        role: 'user',
        content,
        attachments: currentAttachments,
        context: { compilation_job_id: job.id, status: 'queued_guidance' },
      })
    }
    markCompilationMessage(response.job, scope)
    if (response.accepted) {
      ElMessage.success('补充指导已加入当前任务，将在安全检查点后继续')
    }
    scrollBottom()
  } catch (error: any) {
    ElMessage.warning(error.message || '当前任务暂时无法接收补充指导')
  } finally {
    guidanceSubmitting.value = false
  }
}

function send(text?: string) {
  const providedContent = text !== undefined ? text : input.value.trim()
  const content = (providedContent || (
    attachments.value.length
      ? '请分析本次上传的业务资料，先整理可见任务计划；根据语义判断需要建设哪些业务模型，存在歧义时先向我确认。'
      : ''
  )).trim()
  if (content && compilationRunning.value) {
    void submitCompilationGuidance(content)
    return
  }
  if (!content || loading.value || compilationBusy.value || modelTaskRecoveryBusy.value || uploadingFiles.value > 0) return
  clearModelTaskRecovery()
  if (activeCompilationJob.value?.status === 'failed') detachCompilationRecovery()
  if (messages.value.length === 1 && messages.value[0].role === 'assistant' && !messages.value[0].id) messages.value = []
  const currentAttachments = [...attachments.value]
  messages.value.push({ role: 'user', content, attachments: currentAttachments })
  messages.value.push({ role: 'assistant', content: '', thinking: [], streaming: true })
  // 从响应式数组中重新取出消息，避免直接修改未被 Vue 代理的原始对象。
  const aiIndex = messages.value.length - 1
  const ai = messages.value[aiIndex]
  upsertThinking(ai, {
    id: 'routing',
    title: '规划当前任务',
    detail: '正在结合消息、附件、当前页面和历史草稿确定处理路径。',
    status: 'running',
  })
  input.value = ''
  loading.value = true
  const generation = ++streamGeneration
  scrollBottom()
  streamController = streamAssistantChat(
    {
      message: content,
      request_id: typeof crypto?.randomUUID === 'function'
        ? crypto.randomUUID()
        : `assistant-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      thread_id: threadId.value || undefined,
      scenario_id: context.value.scenario_id || undefined,
      page: context.value.page,
      path: context.value.path,
      selection: selection.id ? { ...selection } : {},
      attachment_ids: currentAttachments.map((item) => item.id),
      llm_config_id: assistantConfig.llmConfigId || undefined,
      skill_ids: [...assistantConfig.skillIds],
      mcp_ids: [...assistantConfig.mcpIds],
      // Route by the LangGraph/LLM semantic planner. The client only sends
      // the user's intent and context; it does not select a hidden task mode.
      mode: 'ask',
      draft_kind: 'auto',
    },
    (event) => {
      if (generation !== streamGeneration || componentDisposed) return
      handleAssistantEvent(event, ai)
      if (event.type === 'meta') attachments.value = []
    },
    () => {
      if (generation === streamGeneration && !componentDisposed) {
        finishStream(ai)
        const job = activeCompilationJob.value
        const scope = compilationThreadScope(compilationRecoveryThreadId.value)
        if (job?.status === 'running' && scope && !compilationStreamController) {
          attachCompilationEventStream(job, scope)
        }
      }
    },
    (error) => {
      if (generation !== streamGeneration || componentDisposed) return
      ai.content += compilationRunning.value
        ? `${ai.content ? '\n\n' : ''}当前对话连接已中断；服务端会继续同一个建模任务，我正在重新订阅它的事件流，不会重新提交请求。`
        : `${ai.content ? '\n\n' : ''}这次请求没有完成：${error.message || '请求失败'}`
      ai.streaming = false
      loading.value = false
      streamController = null
      if (compilationRunning.value) {
        const job = activeCompilationJob.value
        const scope = compilationThreadScope(compilationRecoveryThreadId.value)
        if (job && scope) attachCompilationEventStream(job, scope)
        ElMessage.warning('连接已中断，正在续接同一条建模事件流')
      }
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

function clearModelTaskRecovery(clearFailure = true) {
  modelTaskRecoveryGeneration += 1
  recoveringModelProposalId.value = ''
  if (clearFailure) {
    blockedModelProposalId.value = ''
    modelTaskRecoveryFailure.value = ''
  }
}

function modelTaskOutcomeFallback(
  task: AssistantModelTask | undefined,
  taskTitle: string,
  nextTask?: AssistantModelTask,
) {
  let update = ''
  if (!task) {
    update = `「${taskTitle}」已结束，但暂未读取到可核验的任务结果。`
  } else if (modelTaskHasPersistedWrites(task)) {
    update = task.status === 'partially_applied'
      ? `「${task.title}」的安全部分已确认写入当前场景；其余定义仍作为待校验草稿保留。`
      : `「${task.title}」已确认写入当前场景。`
  } else if (task.status === 'drafted_with_gaps') {
    update = `「${task.title}」本轮没有可核验的正式写入；待校验草稿已保留，计划继续推进。`
  } else if (['deferred', 'skipped'].includes(task.status)) {
    update = `「${task.title}」已保留为草稿，本次未写入正式模型。`
  } else if (task.status === 'empty') {
    update = `「${task.title}」没有可写入的正式变更，计划继续推进。`
  } else {
    update = `「${task.title}」已结束，但暂未读取到可核验的正式写入结果。`
  }
  if (nextTask) {
    update += nextTask.status === 'awaiting_generation'
      ? ` 下一步是「${nextTask.title}」；原始资料和已确认定义已保留，等待开始生成。`
      : ` 下一步已停留在「${nextTask.title}」等待确认。`
  }
  return update
}

function announceModelTaskOutcome(
  proposal: AssistantProposal,
  taskId: string,
  recovered = false,
) {
  const task = modelTaskOf(proposal, taskId)
  if (!task) {
    ElMessage.warning('任务已结束，但暂未读取到可核验的持久化结果')
    return
  }
  if (modelTaskHasPersistedWrites(task)) {
    window.dispatchEvent(new CustomEvent('assistant-proposal-applied', {
      detail: { scenario_id: context.value.scenario_id, kind: 'scenario_model', task_id: taskId },
    }))
    ElMessage.success(
      recovered
        ? task.status === 'partially_applied'
          ? '已恢复本任务的安全部分写入结果，计划将继续推进'
          : '已恢复本任务的正式写入结果，计划将继续推进'
        : task.status === 'partially_applied'
          ? '本任务的安全部分已确认写入，计划已继续'
          : '本任务已确认写入，计划已继续',
    )
    return
  }
  if (['deferred', 'drafted_with_gaps', 'skipped'].includes(task.status)) {
    window.dispatchEvent(new CustomEvent('assistant-scenario-drafts-updated', {
      detail: { scenario_id: context.value.scenario_id, proposal_id: proposal.proposal_id, task_id: taskId },
    }))
  }
  if (task.status === 'drafted_with_gaps') {
    ElMessage.warning('本任务未确认到正式写入；待校验草稿已保留，计划已继续')
  } else if (['deferred', 'skipped'].includes(task.status)) {
    ElMessage.info('本任务仅保留为草稿，未写入正式模型，计划已继续')
  } else if (task.status === 'empty') {
    ElMessage.info('本任务没有可写入的正式变更，计划已继续')
  } else {
    ElMessage.warning('任务状态已恢复，但暂未读取到可核验的正式写入结果')
  }
}

function beginModelTaskRecovery(
  message: AssistantMessage,
  proposalThreadId: string,
  proposalId: string,
  taskId: string,
  minimumRevision: number,
  retryRequest: () => Promise<AssistantProposalApplyResult>,
) {
  clearModelTaskRecovery()
  const generation = modelTaskRecoveryGeneration
  const ownerThreadId = threadId.value
  const ownerScopeKey = assistantScopeKey.value
  const recoveryContext = apiContext()
  recoveringModelProposalId.value = proposalId
  const isCurrent = () => (
    !componentDisposed
    && generation === modelTaskRecoveryGeneration
    && recoveringModelProposalId.value === proposalId
    && threadId.value === ownerThreadId
    && assistantScopeKey.value === ownerScopeKey
  )
  const recover = async () => {
    if (!isCurrent()) return
    try {
      // Resolve one ambiguous POST once through its idempotent server claim.
      // Do not turn the browser into a task runner or status poller.
      await retryRequest()
    } catch {
      // The original request may already be committed; verify it once below.
    }
    if (!isCurrent()) return
    try {
      const recoveredMessages = await api.listAssistantMessages(proposalThreadId, recoveryContext)
      if (!isCurrent()) return
      const recoveredMessage = latestProposalMessage(recoveredMessages, proposalId)
      const recoveredProposal = recoveredMessage ? proposalOf(recoveredMessage) : null
      if (
        recoveredMessage
        && recoveredProposal
        && modelTaskWasProcessed(recoveredProposal, taskId)
        && modelRunRevision(recoveredProposal) >= minimumRevision
      ) {
        Object.assign(message, recoveredMessage, { streaming: false })
        message.context = {
          ...(message.context || {}),
          status: modelRunContextStatus(recoveredProposal),
          run_revision: modelRunRevision(recoveredProposal),
        }
        clearModelTaskRecovery()
        announceModelTaskOutcome(recoveredProposal, taskId, true)
        scrollBottom()
        return
      }
    } catch (error: any) {
      if (!isCurrent()) return
      const status = Number(error?.status || 0)
      if ([403, 404, 409].includes(status)) {
        clearModelTaskRecovery(false)
        blockedModelProposalId.value = proposalId
        modelTaskRecoveryFailure.value = String(error?.message || '会话、权限或页面范围已失效。')
        ElMessage.error('当前任务状态已无法恢复；系统已停止重试旧任务。')
        return
      }
    }
    if (!isCurrent()) return
    clearModelTaskRecovery(false)
    blockedModelProposalId.value = proposalId
    modelTaskRecoveryFailure.value = '任务已提交，但本次没有读取到可验证的新版本；重新打开会话即可读取服务端权威结果。'
    ElMessage.warning('任务已提交；请重新打开会话查看服务端权威结果。')
  }
  void recover()
}

function applyCurrentModelTask(message: AssistantMessage, index: number) {
  const task = currentModelTask(proposalOf(message))
  if (!task) return
  void applyModelTask(message, index, task, 'apply')
}

function generateCurrentModelTask(message: AssistantMessage, index: number) {
  const task = currentModelTask(proposalOf(message))
  if (!task) return
  void generateModelTask(message, index, task)
}

async function generateModelTask(
  message: AssistantMessage,
  _index: number,
  task: AssistantModelTask,
) {
  const proposal = proposalOf(message)
  const nextAction = modelNextAction(proposal)
  if (
    !proposal
    || proposal.kind !== 'scenario_model'
    || !proposal.proposal_id
    || !threadId.value
    || !context.value.scenario_id
    || compilationBusy.value
    || startingModelTaskId.value
    || !isActiveModelRun(message)
    || task.status !== 'awaiting_generation'
    || nextAction?.type !== 'generate_task'
    || nextAction.task_id !== task.id
  ) return
  const operationId = `${proposal.proposal_id}:${task.id}`
  startingModelTaskId.value = operationId
  try {
    const job = await api.continueAssistantModelTask({
      scenario_id: context.value.scenario_id,
      thread_id: String(message.context?.proposal_thread_id || threadId.value),
      proposal_id: proposal.proposal_id,
      task_id: task.id,
    })
    message.streaming = job.status === 'running'
    message.context = {
      ...(message.context || {}),
      compilation_job_id: job.id,
      status: job.status === 'running' ? 'processing' : job.status,
      continuation_task_id: task.id,
      compilation_progress: job.progress,
    }
    upsertThinking(message, {
      id: `stage-${task.id}`,
      title: `生成${task.title}`,
      detail: '已接收继续请求，正在基于原始资料和已确认定义生成候选。',
      status: job.status === 'failed' ? 'error' : job.status === 'succeeded' ? 'done' : 'running',
    })
    beginCompilationRecoveryFromEvent({
      job_id: job.id,
      thread_id: job.thread_id || threadId.value,
      scenario_id: context.value.scenario_id,
    })
    const scope = compilationThreadScope(String(job.thread_id || threadId.value))
    if (scope && job.status === 'running') attachCompilationEventStream(job, scope)
    if (job.status !== 'failed') ElMessage.success(`已开始生成「${task.title}」`)
  } catch (error: any) {
    ElMessage.error(error.message || '无法开始生成下一项建模任务')
  } finally {
    if (startingModelTaskId.value === operationId) startingModelTaskId.value = ''
  }
}

async function applyModelTask(
  message: AssistantMessage,
  index: number,
  task: AssistantModelTask,
  action: 'apply' | 'defer',
) {
  const proposal = proposalOf(message)
  if (
    !proposal
    || proposal.kind !== 'scenario_model'
    || !proposal.proposal_id
    || !threadId.value
    || applyingIndex.value !== null
    || recoveringModelProposalId.value
    || blockedModelProposalId.value === proposal.proposal_id
    || !isActiveModelRun(message)
    || !['ready', 'blocked'].includes(task.status)
    || !isCurrentModelTask(proposal, task)
  ) return
  const confirmation = action === 'defer'
    ? `本次不会写入“${task.title}”。已生成草稿、缺失项和解决建议都会保留，计划将继续推进其他任务。`
    : `确认后会把“${task.title}”中通过校验的资源写入正式模型；不完整定义只会作为停用草稿保存，问题汇总保留在助手会话，完成后再继续下一项任务。`
  try {
    await ElMessageBox.confirm(
      confirmation,
      action === 'defer' ? '确认保留草稿并继续' : '确认应用当前任务',
      {
        type: action === 'defer' ? 'warning' : 'info',
        confirmButtonText: action === 'defer' ? '保留草稿并继续' : '确认应用并继续',
        cancelButtonText: '返回查看',
        distinguishCancelAndClose: true,
      },
    )
  } catch {
    return
  }
  applyingIndex.value = index
  const proposalThreadId = String(message.context?.proposal_thread_id || threadId.value)
  const localRevision = modelRunRevision(proposal)
  const submitModelTask = () => api.applyAssistantProposal({
    kind: 'scenario_model',
    scenario_id: context.value.scenario_id,
    thread_id: proposalThreadId,
    proposal_id: proposal.proposal_id,
    confirm: true,
    allow_partial: action === 'apply',
    task_id: task.id,
    task_action: action,
  })
  try {
    const result = await submitModelTask()
    const responseProposal = result?.proposal || null
    const responseRevision = modelRunRevision(responseProposal)
    let recoveredMessage: AssistantMessage | null = null
    try {
      const recoveredMessages = await api.listAssistantMessages(proposalThreadId, apiContext())
      recoveredMessage = latestProposalMessage(recoveredMessages, proposal.proposal_id)
    } catch {
      // A successful POST is authoritative. If neither response nor this read
      // contains the advanced task, the durable recovery loop below takes over.
    }
    const recoveredProposal = recoveredMessage ? proposalOf(recoveredMessage) : null
    const recoveredRevision = modelRunRevision(recoveredProposal)
    if (recoveredMessage && recoveredProposal && modelTaskWasProcessed(recoveredProposal, task.id) && recoveredRevision >= localRevision && recoveredRevision >= responseRevision) {
      Object.assign(message, recoveredMessage, { streaming: false })
    } else if (responseProposal && modelTaskWasProcessed(responseProposal, task.id) && responseRevision >= localRevision) {
      message.proposal = responseProposal
    } else {
      beginModelTaskRecovery(
        message,
        proposalThreadId,
        proposal.proposal_id,
        task.id,
        Math.max(localRevision, responseRevision),
        submitModelTask,
      )
      ElMessage.warning('任务已提交，正在自动恢复服务端最新计划；不会重复应用。')
      return
    }
    const updatedProposal = message.proposal as AssistantProposal
    const updatedTask = modelTaskOf(updatedProposal, task.id)
    const nextTask = modelTasks(updatedProposal).find((item) => ['ready', 'blocked', 'awaiting_generation'].includes(item.status))
    const durableUpdate = String(result?.task_update_text || '').trim()
    if (durableUpdate && !modelTaskNeedsWriteVerification(updatedTask || task) && !message.content.includes(durableUpdate)) {
      message.content += `\n\n${durableUpdate}`
    } else if (!durableUpdate || modelTaskNeedsWriteVerification(updatedTask || task)) {
      const fallbackUpdate = modelTaskOutcomeFallback(updatedTask, task.title, nextTask)
      if (!message.content.includes(fallbackUpdate)) message.content += `\n\n${fallbackUpdate}`
    }
    message.context = {
      ...(message.context || {}),
      status: modelRunContextStatus(updatedProposal),
      run_revision: modelRunRevision(updatedProposal),
    }
    announceModelTaskOutcome(updatedProposal, task.id, result?.status === 'replayed')
  } catch (error: any) {
    const status = Number(error?.status || 0)
    const ambiguousOutcome = !status
      || status >= 500
      || (status === 409 && /正在应用|处理中/.test(String(error?.message || '')))
    if (ambiguousOutcome) {
      beginModelTaskRecovery(
        message,
        proposalThreadId,
        proposal.proposal_id,
        task.id,
        localRevision,
        submitModelTask,
      )
      ElMessage.warning('确认请求结果暂不明确，系统会持续核对持久化状态并安全重试，无需再次点击。')
    } else {
      ElMessage.error(error.message || '处理当前建模任务失败')
    }
  } finally {
    applyingIndex.value = null
  }
}

async function applyProposal(message: AssistantMessage, index: number) {
  const proposal = proposalOf(message)
  if (!proposal || proposal.kind === 'scenario_model' || ['applied', 'partially_applied'].includes(proposal.status || '') || !proposalCanApply(proposal) || !threadId.value || !proposal.proposal_id || applyingIndex.value !== null) return
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
    const proposalThreadId = String(message.context?.proposal_thread_id || threadId.value)
    const result = await api.applyAssistantProposal({
      kind: proposal.kind,
      scenario_id: proposal.kind === 'scenario' ? undefined : context.value.scenario_id,
      thread_id: proposalThreadId,
      proposal_id: proposal.proposal_id,
      confirm: true,
      allow_partial: false,
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
  clearModelTaskRecovery()
  loading.value = false
  messages.value = []
  threads.value = []
  threadId.value = ''
  historyVisible.value = false
  attachments.value = []
  sourcePreviewVisible.value = false
  Object.keys(expandedChangeLists).forEach((key) => delete expandedChangeLists[key])
  Object.keys(expandedIssueGroups).forEach((key) => delete expandedIssueGroups[key])
  Object.keys(expandedModelPlans).forEach((key) => delete expandedModelPlans[key])
  if (visible.value) {
    await loadContext()
  }
})
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
  clearCompilationStream()
  clearModelTaskRecovery()
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
.assistant-launcher.is-working {
  max-width: min(340px, calc(100vw - 28px));
  border-color: color-mix(in srgb, var(--primary) 58%, var(--border));
  background: var(--surface);
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
.assistant-launcher-copy { display: flex; min-width: 0; flex-direction: column; align-items: flex-start; line-height: 1.25; }
.assistant-launcher-copy strong { font-size: 13px; font-weight: 750; white-space: nowrap; }
.assistant-launcher-copy small { display: block; max-width: 240px; overflow: hidden; color: var(--text-2); font-size: 11px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.assistant-live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.assistant-launcher.is-working .assistant-live-dot { background: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }

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
.model-task-plan { margin: 10px 0 2px; border-top: 1px solid var(--border); }
.model-task-plan-toggle { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 50px; padding: 8px 0; border: 0; color: var(--text-2); background: transparent; cursor: pointer; font: inherit; text-align: left; }
.model-task-plan-toggle:hover, .model-task-plan-toggle:focus-visible { color: var(--primary-600); outline: none; }
.model-task-plan-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 2px; }
.model-task-plan-copy strong { color: var(--text); font-size: 11.5px; }
.model-task-plan-copy small { overflow-wrap: anywhere; color: var(--text-3); font-size: 9.5px; line-height: 1.4; }
.model-task-plan-action { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 3px; color: var(--primary-600); font-size: 10px; font-weight: 700; }
.model-current-task { display: grid; gap: 7px; padding: 8px 0 5px 10px; border-left: 2px solid var(--primary); }
.model-current-task.is-blocked { border-left-color: var(--warning); }
.model-current-task.is-awaiting_generation { border-left-color: var(--primary-600); }
.model-current-task p { margin: 0; color: var(--text-2); font-size: 9.5px; line-height: 1.5; }
.model-task-list { display: grid; margin-top: 2px; border-top: 1px solid var(--border); }
.model-task { display: grid; gap: 7px; padding: 9px 0; border-bottom: 1px solid var(--border); background: transparent; }
.model-task.is-current { padding-left: 10px; border-left: 2px solid var(--primary); }
.model-task.is-blocked { border-left-color: var(--warning); }
.model-task.is-waiting, .model-task.is-empty { opacity: .72; }
.model-task-head { display: flex; align-items: center; gap: 7px; min-width: 0; }
.model-task-index { display: inline-flex; align-items: center; justify-content: center; width: 21px; height: 21px; flex: 0 0 auto; border-radius: 7px; color: var(--primary-600); background: var(--primary-soft); font-size: 10px; font-weight: 800; }
.model-task-title { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 2px; }
.model-task-title strong { overflow: hidden; color: var(--text); font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.model-task-title small { overflow-wrap: anywhere; color: var(--text-3); font-size: 9px; line-height: 1.4; }
.model-task-blocker { display: grid; gap: 3px; padding-left: 8px; border-left: 2px solid var(--warning); color: var(--text-2); font-size: 9.5px; line-height: 1.45; }
.model-task-blocker strong { color: var(--warning-700, var(--warning)); }
.model-task-blocker small, .model-task-note, .model-task-waiting { color: var(--text-3); font-size: 9px; line-height: 1.45; }
.model-task-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }
.model-task-actions :deep(.el-button) { min-height: 36px; }
.model-run-waiting { margin-top: 6px; color: var(--primary-600); font-size: 9.5px; line-height: 1.5; }
.model-run-summary { display: grid; gap: 8px; padding: 10px 0 0; border-top: 1px dashed var(--border); }
.model-run-summary.is-success > header strong { color: var(--success); }
.model-run-summary.is-warning > header strong { color: var(--warning); }
.model-run-summary > header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.model-run-summary > header strong { color: var(--text); font-size: 11px; }
.model-run-summary p, .model-run-summary small { margin: 0; color: var(--text-2); font-size: 9.5px; line-height: 1.55; }
.model-run-summary-counts { display: flex; flex-wrap: wrap; gap: 5px; }
.model-run-summary-counts span { color: var(--text-2); font-size: 9px; font-variant-numeric: tabular-nums; }
.model-run-root-causes { display: grid; gap: 5px; }
.model-run-root-causes article { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; padding: 6px 0; border-top: 1px solid color-mix(in srgb, var(--warning) 26%, var(--border)); }
.model-run-root-causes strong { color: var(--text); font-size: 10px; }
.model-run-root-causes span { color: var(--warning); font-size: 9px; font-variant-numeric: tabular-nums; }
.model-run-root-causes small { grid-column: 1 / -1; }
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
.assistant-message.assistant .message-content { width: calc(100% - 39px); max-width: calc(100% - 39px); }
.assistant-message.user .message-content { max-width: 82%; }
.message-label { margin: 0 4px 5px; color: var(--text-3); font-size: 10.5px; font-weight: 700; }
.assistant-message.user .message-label { text-align: right; }
.assistant-worklog { margin: 0 0 10px; padding-left: 10px; border-left: 2px solid color-mix(in srgb, var(--primary) 46%, var(--border)); }
.assistant-worklog.is-active { border-left-color: var(--primary); }
.worklog-head { display: flex; align-items: center; gap: 8px; min-height: 28px; color: var(--text-2); font-size: 11px; }
.worklog-head > span:first-child { display: inline-flex; align-items: center; gap: 5px; flex: 0 0 auto; color: var(--text); font-weight: 750; }
.worklog-summary { min-width: 0; flex: 1; overflow: hidden; color: var(--text-3); text-overflow: ellipsis; white-space: nowrap; }
.worklog-live { flex: 0 0 auto; color: var(--primary-600); font-size: 10px; }
.worklog-body { display: grid; gap: 7px; padding: 4px 0 2px; }
.worklog-entry { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; align-items: start; gap: 7px; color: var(--text-2); font-size: 10px; line-height: 1.45; }
.worklog-step-dot { width: 7px; height: 7px; margin-top: 4px; border-radius: 50%; background: var(--text-3); }
.worklog-entry.is-running .worklog-step-dot { background: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }
.worklog-entry.is-error .worklog-step-dot { background: var(--danger); }
.worklog-entry div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.worklog-entry strong { color: var(--text); font-weight: 700; }
.worklog-entry span { color: var(--text-3); overflow-wrap: anywhere; }
.worklog-entry small { color: var(--text-3); font-size: 9px; white-space: nowrap; }
.worklog-live-feed { display: grid; gap: 6px; margin-top: 2px; padding-top: 7px; border-top: 1px dashed var(--border); }
.worklog-live-entry { display: grid; grid-template-columns: 52px minmax(0, 1fr) auto; align-items: start; gap: 7px; color: var(--text-3); font-size: 9.5px; line-height: 1.45; }
.worklog-live-entry time, .worklog-live-entry > small { color: var(--text-3); font-size: 9px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.worklog-live-entry > div { display: flex; min-width: 0; flex-direction: column; gap: 1px; }
.worklog-live-entry strong { color: var(--text-2); font-size: 9.5px; font-weight: 700; }
.worklog-live-entry span { overflow-wrap: anywhere; }
.worklog-live-entry.is-latest time, .worklog-live-entry.is-latest strong { color: var(--primary-600); }
.worklog-live-entry.is-disconnected time, .worklog-live-entry.is-disconnected strong { color: var(--warning); }
.worklog-note { padding-top: 2px; color: var(--text-3); font-size: 9px; line-height: 1.45; }
.message-bubble { padding: 10px 12px; border: 1px solid var(--border); border-radius: 13px 13px 13px 4px; background: var(--surface); box-shadow: var(--shadow-xs); }
.assistant-message.assistant .message-bubble { padding: 0; border: 0; border-radius: 0; background: transparent; box-shadow: none; }
.message-bubble.user { border-color: var(--border-strong); border-radius: 13px 13px 4px 13px; color: var(--primary-600); background: var(--primary-soft); white-space: pre-wrap; }
.user-content { line-height: 1.65; font-size: 13px; }
.stream-cursor { display: inline-block; margin-left: 2px; color: var(--primary); animation: stream-cursor-blink 900ms steps(2, jump-none) infinite; }
@keyframes stream-cursor-blink { 50% { opacity: 0; } }
.proposal-card { margin-top: 9px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--warning) 38%, var(--border)); border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-xs); }
.proposal-card.is-model-result { margin-top: 12px; overflow: visible; border: 0; border-top: 1px solid var(--border); border-radius: 0; background: transparent; box-shadow: none; }
.proposal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 11px 12px 8px; background: var(--warning-soft); }
.is-model-result .proposal-head { padding: 12px 0 5px; background: transparent; }
.proposal-title { display: flex; align-items: center; gap: 6px; color: var(--text); font-size: 12.5px; font-weight: 800; }
.proposal-summary { margin-top: 4px; color: var(--text-2); font-size: 11.5px; line-height: 1.5; }
.proposal-preview { display: flex; align-items: center; gap: 10px; padding: 9px 12px; color: var(--text-2); font-size: 11.5px; }
.is-model-result .proposal-preview { flex-wrap: wrap; padding: 6px 0 0; }
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
.is-model-result .proposal-detail { margin: 10px 0 0; padding: 12px 0 0; border: 0; border-top: 1px dashed var(--border); border-radius: 0; background: transparent; }
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
.issue-group-summary { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: flex-start; gap: 7px; padding: 7px 0; border-top: 1px solid var(--border); }
.issue-group-summary:first-child { border-top: 0; }
.issue-group-summary > div { display: grid; min-width: 0; gap: 2px; }
.issue-group-summary strong { color: var(--text); font-size: 10.5px; }
.issue-group-summary p { margin: 0; color: var(--text-2); font-size: 9.5px; line-height: 1.5; }
.issue-group-summary small, .issue-group-summary .issue-resolution { color: var(--text-3); font-size: 9px; line-height: 1.45; }
.issue-group-summary .issue-resolution { color: var(--primary-600); }
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
.answer-evidence { margin-top: 8px; padding-top: 7px; border-top: 1px solid var(--border); color: var(--text-2); font-size: 11px; }
.answer-evidence > summary { display: flex; align-items: center; justify-content: space-between; gap: 9px; min-height: 30px; color: var(--text-3); cursor: pointer; list-style: none; }
.answer-evidence > summary::-webkit-details-marker { display: none; }
.answer-evidence > summary:hover, .answer-evidence > summary:focus-visible { color: var(--primary-600); outline: none; }
.answer-evidence > summary > span { display: inline-flex; align-items: center; gap: 5px; font-weight: 700; }
.answer-evidence > summary > small { font-size: 9.5px; }
.assistant-action-preview { margin-top: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); color: var(--text-2); font-size: 11px; }
.assistant-action-preview > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 9px; }
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
.model-run-composer-wait { display: flex; align-items: flex-start; gap: 7px; margin-bottom: 8px; padding: 8px 9px; border: 1px solid color-mix(in srgb, var(--primary) 30%, var(--border)); border-radius: 9px; color: var(--primary-600); background: var(--primary-soft); }
.model-run-composer-wait > .el-icon { flex: 0 0 auto; margin-top: 2px; }
.model-run-composer-wait span { display: grid; gap: 2px; color: var(--text-2); font-size: 9.5px; line-height: 1.45; }
.model-run-composer-wait strong { color: var(--text); font-size: 10px; }
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
  .worklog-chevron, .disclosure-chevron, .stream-cursor { transition: none; animation: none; }
}

@media (max-width: 560px), (max-height: 480px) {
  .assistant-launcher {
    top: 8px;
    right: 136px;
    bottom: auto;
    width: 44px;
    min-width: 44px;
    height: 44px;
    min-height: 44px;
    padding: 4px;
    justify-content: center;
    gap: 0;
    border-radius: 50%;
  }
  .assistant-launcher-copy {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  .assistant-live-dot { display: none; }
}

@media (max-height: 480px) and (min-width: 901px) {
  .assistant-launcher { right: 296px; }
}

@media (max-width: 560px) {
  .assistant-head { padding: 10px 12px; }
  .assistant-title-wrap { gap: 8px; }
  .assistant-title { font-size: 14px; white-space: nowrap; }
  .assistant-title :deep(.el-tag), .assistant-subtitle { display: none; }
  .context-hint { width: 100%; margin-left: 0; }
  .assistant-session-bar { align-items: flex-start; flex-direction: column; }
  .session-actions { width: 100%; }
  .session-actions :deep(.el-button) { flex: 1; }
  .assistant-messages { padding: 14px; }
  .assistant-history { padding: 14px; }
  .assistant-composer { padding: 9px 10px 12px; }
  .thread-delete, .attachment-chip button, .tool-button, .send-button { min-width: 44px; min-height: 44px; }
  .attachment-chip button { width: 44px; height: 44px; }
  .tool-button span { display: none; }
  .composer-tools { flex-wrap: wrap; }
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
