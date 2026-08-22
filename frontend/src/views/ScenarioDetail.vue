<template>
  <div v-if="scenarioLoading" class="sd-page scenario-state" aria-busy="true" aria-label="正在加载业务场景">
    <el-skeleton :rows="5" animated />
  </div>
  <div v-else-if="scenarioAccessDenied" class="sd-page scenario-state">
    <el-result
      icon="warning"
      title="你没有访问此业务场景的权限"
      sub-title="该场景可能已被限制、移除，或当前账号没有读取权限。"
    >
      <template #extra>
        <el-button type="primary" @click="goBack">返回业务场景</el-button>
      </template>
    </el-result>
  </div>
  <div v-else-if="scenarioLoadError" class="sd-page scenario-state">
    <el-result icon="error" title="业务场景加载失败" :sub-title="scenarioLoadError">
      <template #extra>
        <el-button type="primary" @click="load">重新加载</el-button>
        <el-button @click="goBack">返回业务场景</el-button>
      </template>
    </el-result>
  </div>
  <div v-else class="sd-page">
    <div class="page-header sd-header">
      <div class="ph-left">
        <el-button class="back-btn" @click="goBack"><el-icon><ArrowLeft /></el-icon> 返回</el-button>
        <div class="ph-title">
          <h1>{{ detail.name || '场景详情' }}</h1>
          <span class="ph-sub">{{ detail.description }}</span>
        </div>
      </div>
      <div class="ph-right">
        <el-tag v-if="!canWrite" type="info" effect="plain" aria-label="当前场景为只读访问">只读访问</el-tag>
        <el-button v-else class="ai-btn" @click="openAssistantDraft"><el-icon><MagicStick /></el-icon> AI 建模助手</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="sd-tabs">
      <!-- ═══════════ 首个可验收闭环：唯一主路径 ═══════════ -->
      <el-tab-pane label="闭环工作台" name="flow">
        <main class="flow-workbench" aria-labelledby="flow-workbench-title">
          <section class="flow-hero">
            <div>
              <span class="eyebrow">GUIDED DELIVERY</span>
              <h2 id="flow-workbench-title">从业务描述到受控执行</h2>
              <p>沿一条主线完成 AI 草稿、数据映射、对象证据、人工审批与审计回看；下方技术工作区用于深入配置。</p>
            </div>
            <div class="flow-progress" role="status" aria-live="polite">
              <span><b>{{ completedFlowSteps }}</b> / {{ flowSteps.length }} 已完成</span>
              <el-progress :percentage="flowProgress" :show-text="false" :stroke-width="8" />
            </div>
          </section>

          <ol class="flow-step-list" aria-label="业务闭环步骤">
            <li v-for="step in flowSteps" :key="step.id" :class="`is-${step.status}`">
              <button type="button" :aria-current="step.status === 'current' ? 'step' : undefined" @click="activateFlowStep(step.id)">
                <span class="flow-step-index">
                  <el-icon v-if="step.status === 'complete'" aria-hidden="true"><Check /></el-icon>
                  <span v-else>{{ step.number }}</span>
                </span>
                <span class="flow-step-copy"><strong>{{ step.title }}</strong><small>{{ step.description }}</small></span>
                <el-tag size="small" effect="plain" :type="flowStatusType(step.status)">{{ flowStatusLabel(step.status) }}</el-tag>
                <el-icon class="flow-step-arrow" aria-hidden="true"><ArrowRight /></el-icon>
              </button>
            </li>
          </ol>

          <section class="flow-next" aria-labelledby="flow-next-title">
            <div class="flow-next-mark" aria-hidden="true"><el-icon><Guide /></el-icon></div>
            <div>
              <span>建议下一步</span>
              <h3 id="flow-next-title">{{ nextFlowStep.title }}</h3>
              <p>{{ nextFlowStep.nextHint }}</p>
            </div>
            <el-button type="primary" @click="activateFlowStep(nextFlowStep.id)">{{ nextFlowStep.actionLabel }}<el-icon><ArrowRight /></el-icon></el-button>
          </section>

          <div class="flow-main-grid">
            <section ref="querySectionRef" class="flow-section query-workbench" aria-labelledby="object-query-title">
              <header class="flow-section-head">
                <div>
                  <span class="section-number">03</span>
                  <div><h3 id="object-query-title">用自然语言查询对象</h3><p>回答只基于当前账号可见的运行时对象，并逐条给出来源与关系引用。</p></div>
                </div>
                <el-tag size="small" effect="plain" type="info">{{ objectTotal }} 个可见对象</el-tag>
              </header>
              <div class="object-question-box">
                <el-input
                  ref="objectQuestionInputRef"
                  v-model="objectQuestion"
                  clearable
                  maxlength="200"
                  aria-label="输入要查询的对象问题"
                  placeholder="例如：查找张三相关的订单，并告诉我数据来自哪里"
                  @keyup.enter="runObjectQuestion"
                >
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
                <el-button type="primary" :loading="objectQuestionLoading" :disabled="!objectQuestion.trim()" @click="runObjectQuestion">查询并引用</el-button>
              </div>
              <div v-if="querySuggestions.length" class="query-suggestions" aria-label="查询示例">
                <span>可尝试</span>
                <button v-for="suggestion in querySuggestions" :key="suggestion" type="button" @click="askObjectSuggestion(suggestion)">{{ suggestion }}</button>
              </div>
              <div v-if="objectQuestionAnswer" class="query-answer" role="status" aria-live="polite">
                <div class="query-answer-head"><el-icon aria-hidden="true"><DocumentChecked /></el-icon><strong>基于对象运行时的回答</strong></div>
                <p>{{ objectQuestionAnswer }}</p>
                <small v-if="objectQuestionTerm">实际检索词：“{{ objectQuestionTerm }}”；未使用模型推测未命中的事实。</small>
              </div>
              <div v-if="objectQuestionEvidence.length" class="evidence-list" aria-label="对象查询引用">
                <article v-for="(item, index) in objectQuestionEvidence" :key="item.id" class="evidence-card">
                  <header>
                    <span class="evidence-id">[O{{ index + 1 }}]</span>
                    <div><strong>{{ item.name }}</strong><small>{{ item.entity_name || '未分类对象' }}</small></div>
                    <el-button text type="primary" @click="openEvidenceObject(item.id)">查看对象</el-button>
                  </header>
                  <dl>
                    <div><dt>数据来源</dt><dd>{{ objectSourceLabel(item) }}</dd></div>
                    <div><dt>来源定位</dt><dd class="mono">{{ objectSourceReference(item) }}</dd></div>
                    <div><dt>关系证据</dt><dd>{{ objectRelationSummary(item) }}</dd></div>
                  </dl>
                  <div v-if="objectAttributeEntries(item).length" class="evidence-attributes">
                    <span v-for="entry in objectAttributeEntries(item)" :key="entry[0]"><b>{{ entry[0] }}</b>{{ formatObjectValue(entry[1]) }}</span>
                  </div>
                </article>
              </div>
              <el-empty v-else-if="objectQuestionSearched && !objectQuestionLoading" :image-size="56" description="没有找到可引用对象；可缩短关键词或先完成映射刷新" />
            </section>

            <section class="flow-section governed-execution" aria-labelledby="governed-execution-title">
              <header class="flow-section-head">
                <div>
                  <span class="section-number">04</span>
                  <div><h3 id="governed-execution-title">审批后执行 Action</h3><p>先预演影响；需要人工批准时，用“审批 → Action”工作流提交任务。</p></div>
                </div>
                <el-button text type="primary" @click="goToScenarioTasks">任务中心</el-button>
              </header>
              <el-alert
                title="直接执行只包含权限检查、预演和明确确认；人工审批必须通过含审批节点的工作流。"
                type="warning"
                :closable="false"
                show-icon
              />
              <div v-if="detail.actions.length" class="controlled-action-box">
                <label for="controlled-action">选择要受控执行的 Action</label>
                <el-select id="controlled-action" v-model="controlledActionId" placeholder="选择 Action" style="width:100%">
                  <el-option v-for="action in detail.actions" :key="action.id" :label="action.name" :value="action.id" :disabled="action.enabled === false" />
                </el-select>
                <div class="controlled-action-meta" v-if="controlledAction">
                  <span><b>参数</b>{{ Object.keys(actionSchemaRoot(controlledAction.input_schema).properties).length }} 项</span>
                  <span><b>执行器</b>{{ controlledAction.executor_type || '未配置' }}</span>
                  <span><b>幂等</b>{{ controlledAction.idempotency_required === false ? '未要求' : '已要求' }}</span>
                </div>
                <div class="controlled-action-buttons">
                  <el-button :disabled="!controlledAction || !canWrite" @click="controlledAction && doExecuteAction(controlledAction)">先预演直接执行</el-button>
                  <el-button type="primary" :disabled="!controlledAction || !canWrite" @click="prepareApprovalWorkflow">生成审批流程草稿</el-button>
                </div>
              </div>
              <div v-else class="execution-empty">
                <p>还没有类型化 Action。先定义参数 Schema、执行器、权限与幂等约束。</p>
                <el-button v-if="canWrite" type="primary" plain @click="openActionsWorkspace">创建 Action</el-button>
              </div>

              <div class="approval-workflows">
                <div class="subsection-title"><span>可用审批流程</span><b>{{ approvalWorkflows.length }}</b></div>
                <article v-for="workflow in approvalWorkflows" :key="workflow.id" class="approval-workflow-row">
                  <div><strong>{{ workflow.name }}</strong><small>{{ workflow.status === 'active' ? '已启用，可提交审批任务' : '草稿需检查并启用' }}</small></div>
                  <el-tag size="small" effect="plain" :type="workflowStatusType(workflow.status)">{{ workflowStatusLabel(workflow.status) }}</el-tag>
                  <el-button v-if="workflow.status === 'active'" size="small" type="primary" :loading="workflow._executing" @click="doExecuteWorkflow(workflow)">提交</el-button>
                  <el-button v-else-if="canWrite" size="small" @click="openWorkflowWorkspace(workflow.id)">继续配置</el-button>
                </article>
                <p v-if="!approvalWorkflows.length" class="muted">尚无包含人工审批节点的工作流。</p>
              </div>
            </section>
          </div>

          <section ref="auditSectionRef" class="flow-section audit-workbench" aria-labelledby="audit-workbench-title">
            <header class="flow-section-head">
              <div>
                <span class="section-number">05</span>
                <div><h3 id="audit-workbench-title">执行审计与结果证据</h3><p>同页核对环境、定义版本、参数、连接器、结果和错误，不需要在多个页面来回跳转。</p></div>
              </div>
              <el-button :loading="auditLoading" @click="loadExecutionLogs"><el-icon><Refresh /></el-icon>刷新审计</el-button>
            </header>
            <div v-if="executionLogs.length" class="audit-list" aria-live="polite">
              <details v-for="log in executionLogs" :key="log.id" class="audit-entry">
                <summary>
                  <span class="audit-status-dot" :class="`is-${auditStatusTone(log.status)}`" aria-hidden="true"></span>
                  <span class="audit-summary-copy"><strong>{{ log.target_name || '未命名执行' }}</strong><small>{{ log.target_type === 'workflow' ? '工作流' : 'Action' }} · {{ formatDate(log.created_at) }}</small></span>
                  <el-tag size="small" effect="plain" :type="auditStatusType(log.status)">{{ auditStatusLabel(log.status) }}</el-tag>
                  <span class="audit-duration">{{ log.duration_ms || 0 }} ms</span>
                  <el-icon class="audit-chevron" aria-hidden="true"><ArrowDown /></el-icon>
                </summary>
                <div class="audit-detail-grid">
                  <div><span>运行环境</span><b>{{ log.environment || 'dev' }}</b></div>
                  <div><span>定义来源</span><b>{{ log.definition_source === 'release' ? '已发布快照' : '开发中定义' }}</b></div>
                  <div><span>发起主体</span><b>{{ auditActorLabel(log) }}</b></div>
                  <div><span>模型</span><b>{{ log.model_name || log.llm_config_id || '未使用或未知' }}</b></div>
                  <div><span>权限判定</span><b>{{ auditPermissionLabel(log) }}</b></div>
                  <div><span>数据上下文</span><b>{{ auditDataContextLabel(log) }}</b></div>
                  <div v-if="log.correlation_id"><span>决策链路</span><b class="mono">{{ log.correlation_id }}</b></div>
                  <div v-if="log.parent_action_log_id"><span>来源预演</span><b class="mono">{{ log.parent_action_log_id }}</b></div>
                  <div v-if="log.assistant_message_id || log.agent_message_id"><span>AI 消息证据</span><b class="mono">{{ log.assistant_message_id || log.agent_message_id }}</b></div>
                  <div v-if="log.idempotency_key"><span>幂等键</span><b class="mono">{{ log.idempotency_key }}</b></div>
                  <div v-if="log.definition_hash"><span>定义哈希</span><b class="mono">{{ log.definition_hash }}</b></div>
                </div>
                <div v-if="log.connector_audit?.length" class="audit-connectors">
                  <span>连接器证据</span>
                  <el-tag v-for="connector in log.connector_audit" :key="`${connector.kind}-${connector.binding_id || connector.connector_id}`" size="small" effect="plain">
                    {{ connector.environment }} · {{ connector.connector_name || connector.connector_id }}
                  </el-tag>
                </div>
                <div class="audit-payload-grid">
                  <section><h4>输入参数</h4><pre>{{ JSON.stringify(log.input_params || {}, null, 2) }}</pre></section>
                  <section><h4>执行结果</h4><pre>{{ JSON.stringify(log.result || (log.error ? { error: log.error } : {}), null, 2) }}</pre></section>
                </div>
              </details>
            </div>
            <el-empty v-else v-loading="auditLoading" :image-size="64" description="暂无可见执行审计；完成一次预演或受控执行后在这里回看" />
          </section>
        </main>
      </el-tab-pane>

      <!-- ═══════════ 本体 ═══════════ -->
      <el-tab-pane label="本体模型" name="ontology">
        <div class="tab-toolbar">
          <div class="tab-stats">
            <span class="stat">实体 <b>{{ detail.entities.length }}</b></span>
            <span class="stat">关系 <b>{{ detail.relations.length }}</b></span>
          </div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" @click="openEntity()"><el-icon><Plus /></el-icon> 实体</el-button>
            <el-button size="small" @click="openRelation()"><el-icon><Plus /></el-icon> 关系</el-button>
          </div>
        </div>
        <div class="graph-stage">
          <GraphCanvas
            :data="schemaGraph"
            mode="schema"
            :legend="legend"
            :empty-text="canWrite ? '暂无本体，点击「实体」创建，或用 AI 生成' : '暂无本体'"
            @select="onNodeSelect"
            @edge-click="onEdgeClick"
            @add-relation="onAddRelation"
            @canvas-click="clearSelection"
          />
          <EditorPanel
            v-if="editor && canWrite"
            :editor="editor"
            :entities="detail.entities"
            :saving="saving"
            @save="saveEditor"
            @delete="deleteEditor"
            @close="closeEditor"
          />
        </div>
      </el-tab-pane>

      <!-- ═══════════ 实例 ═══════════ -->
      <el-tab-pane label="对象与来源" name="instances">
        <div class="tab-toolbar">
          <div class="tab-stats">
            <span class="stat">实例 <b>{{ detail.instances.length }}</b></span>
            <span class="stat">关系实例 <b>{{ detail.relation_instances.length }}</b></span>
            <span class="stat stat-runtime">运行时对象 <b>{{ objectTotal }}</b></span>
          </div>
          <div class="tab-actions">
            <el-select v-model="instFilter" placeholder="全部实体" clearable size="small" class="inst-filter">
              <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
            </el-select>
            <el-button v-if="canWrite" size="small" type="primary" @click="openInstance()"><el-icon><Plus /></el-icon> 添加实例</el-button>
          </div>
        </div>
        <div class="instance-workspace">
          <div class="graph-stage">
            <GraphCanvas
              :data="instanceGraph"
              mode="instance"
              :legend="legend"
              :empty-text="canWrite ? '暂无实例，点击「添加实例」创建' : '暂无实例'"
              @select="onInstSelect"
              @canvas-click="clearSelection"
            />
            <EditorPanel
              v-if="editor && canWrite"
              :editor="editor"
              :entities="detail.entities"
              :saving="saving"
              @save="saveEditor"
              @delete="deleteEditor"
              @close="closeEditor"
            />
          </div>
          <aside class="object-explorer" aria-label="对象浏览器">
            <div class="explorer-head">
              <div>
                <span class="eyebrow">OBJECT RUNTIME</span>
                <h3>对象浏览</h3>
              </div>
              <el-button
                text
                circle
                :loading="objectLoading"
                aria-label="刷新对象列表"
                title="刷新对象列表"
                @click="searchObjects"
              ><el-icon><Refresh /></el-icon></el-button>
            </div>
            <div class="explorer-tools">
              <el-input
                v-model="objectQuery"
                clearable
                placeholder="搜索对象名称或属性"
                aria-label="搜索对象名称或属性"
                @keyup.enter="searchObjects"
                @clear="searchObjects"
              >
                <template #prefix><el-icon><Search /></el-icon></template>
              </el-input>
              <span class="explorer-hint" role="status" aria-live="polite" aria-atomic="true">{{ objectResultStatus }}</span>
            </div>
            <div class="object-list" v-loading="objectLoading">
              <button
                v-for="item in objectItems"
                :key="item.id"
                type="button"
                class="object-row"
                :class="{ active: selectedObjectId === item.id }"
                @click="selectObject(item.id)"
              >
                <span class="object-dot" :style="{ background: item.entity_color || 'var(--primary)' }"></span>
                <span class="object-row-main">
                  <strong>{{ item.name }}</strong>
                  <small>{{ item.entity_name || '未分类' }} · {{ item.relation_count }} 条关系</small>
                </span>
                <el-icon class="object-arrow"><ArrowRight /></el-icon>
              </button>
              <div v-if="!objectLoading && !objectItems.length" class="object-empty">
                <el-icon><Search /></el-icon>
                <span>{{ objectQuery ? '没有匹配对象，试试更短的关键词' : '暂无可浏览对象' }}</span>
              </div>
            </div>
            <div v-if="!objectLoading && objectItems.length" class="object-pagination">
              <span v-if="objectFilterPending" class="object-pagination-note">搜索条件已变更，按回车应用后会从第 1 条重新加载。</span>
              <template v-else>
                <span class="object-pagination-note">{{ hasMoreObjects ? `还可加载 ${Math.max(objectTotal - objectNextOffset, 0)} 个对象` : `已显示全部 ${objectTotal} 个对象` }}</span>
                <el-button
                  size="small"
                  plain
                  type="primary"
                  :loading="objectLoadingMore"
                  :disabled="objectLoading || objectLoadingMore || !hasMoreObjects"
                  :aria-label="hasMoreObjects ? `加载更多对象，当前已加载 ${objectItems.length} 个，共 ${objectTotal} 个` : `已加载全部 ${objectTotal} 个对象`"
                  @click="loadMoreObjects"
                ><el-icon v-if="hasMoreObjects" aria-hidden="true"><MoreFilled /></el-icon>{{ hasMoreObjects ? '加载更多' : '已加载全部' }}</el-button>
              </template>
            </div>
            <div v-if="objectDetail" class="object-detail" aria-live="polite">
              <div class="object-detail-head">
                <div class="object-title-wrap">
                  <span class="object-detail-dot" :style="{ background: objectDetail.entity_color || 'var(--primary)' }"></span>
                  <div>
                    <strong>{{ objectDetail.name }}</strong>
                    <small>{{ objectDetail.entity_name }}</small>
                  </div>
                </div>
                <el-button v-if="canWrite" size="small" text type="primary" @click="openInstance(objectDetail.id)">编辑</el-button>
              </div>
              <div class="object-meta-line">
                <span class="runtime-badge" :class="`is-${objectDetail.provenance.kind}`">{{ objectDetail.provenance.kind === 'imported' ? '已导入' : '手动' }}</span>
                <span v-if="objectDetail.provenance.reference" class="mono">{{ objectDetail.provenance.reference }}</span>
              </div>
              <div class="object-detail-section">
                <div class="detail-section-title">属性 <span>{{ Object.keys(objectDetail.attributes || {}).length }}</span></div>
                <div v-if="Object.keys(objectDetail.attributes || {}).length" class="attribute-grid">
                  <div v-for="(value, key) in objectDetail.attributes" :key="key" class="attribute-item">
                    <span>{{ key }}</span>
                    <strong>{{ formatObjectValue(value) }}</strong>
                  </div>
                </div>
                <span v-else class="muted">暂无属性值</span>
              </div>
              <div class="object-detail-section">
                <div class="detail-section-title">来源追踪</div>
                <div class="provenance-card">
                  <span>{{ objectDetail.provenance.data_source_name || '手动创建' }}</span>
                  <small v-if="objectDetail.provenance.table_name">{{ objectDetail.provenance.table_name }}</small>
                  <small v-else>当前对象没有绑定数据映射</small>
                </div>
              </div>
              <div class="object-detail-section">
                <div class="detail-section-title">关系 <span>{{ objectDetail.relations.length }}</span></div>
                <button
                  v-for="relation in objectDetail.relations"
                  :key="relation.id"
                  type="button"
                  class="relation-row"
                  @click="selectObject(relation.related_object_id)"
                >
                  <span class="relation-direction">{{ relation.direction === 'outgoing' ? '出' : '入' }}</span>
                  <span><strong>{{ relation.relation_name || '关联' }}</strong> · {{ relation.related_object_name }}</span>
                  <el-icon><ArrowRight /></el-icon>
                </button>
                <span v-if="!objectDetail.relations.length" class="muted">暂无关系</span>
              </div>
            </div>
          </aside>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 数据映射 ═══════════ -->
      <el-tab-pane label="数据映射" name="mappings">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">映射 <b>{{ detail.mappings.length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openMapping()"><el-icon><Plus /></el-icon> 添加映射</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.mappings" class="map-table" empty-text="暂无数据映射">
            <el-table-column label="实体" min-width="120">
              <template #default="{ row }">
                <span class="ent-chip" :style="{ color: entColor(row.entity_id) }">
                  <i :style="{ background: entColor(row.entity_id) }"></i>{{ entName(row.entity_id) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="数据源 / 表" min-width="180">
              <template #default="{ row }">
                <div class="map-dst">
                  <span class="mono">{{ row.table_name }}</span>
                  <span class="muted">{{ dsName(row.data_source_id) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="列映射" min-width="220">
              <template #default="{ row }">
                <div class="col-maps">
                  <span class="col-map" v-for="(v, k) in row.column_map" :key="k">{{ k }} ← {{ v }}</span>
                  <span class="muted" v-if="!Object.keys(row.column_map || {}).length">未配置</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="运行状态" min-width="150">
              <template #default="{ row }">
                <div class="mapping-status-cell">
                  <el-tag size="small" effect="plain" :type="mappingStatusType(row.status)">
                    {{ mappingStatusLabel(row.status) }}
                  </el-tag>
                  <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
                    <el-icon class="mapping-error-icon" aria-label="查看映射错误"><WarningFilled /></el-icon>
                  </el-tooltip>
                  <small v-if="mappingRefreshJob(row)" class="mapping-job-state" role="status" aria-live="polite" aria-atomic="true">
                    {{ row.entity_name || '数据映射' }}：{{ mappingJobLabel(mappingRefreshJob(row)?.status) }}
                    <template v-if="mappingRefreshJob(row)?.status === 'queued'">（最多 {{ mappingRefreshJob(row)?.max_attempts || 0 }} 次）</template>
                    <template v-else> · 第 {{ mappingRefreshJob(row)?.attempt || 0 }}/{{ mappingRefreshJob(row)?.max_attempts || 0 }} 次</template>
                  </small>
                  <small v-if="row.last_refreshed_at" class="muted">{{ formatDate(row.last_refreshed_at) }}</small>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" :width="canWrite ? 340 : 82" fixed="right">
              <template #default="{ row }">
                <el-button v-if="canWrite" size="small" text @click="openMapping(row.id)">编辑</el-button>
                <el-button size="small" text @click="doPreviewMapping(row)">预览</el-button>
                <el-button v-if="canWrite" size="small" text :loading="row._testing" @click="doTestMapping(row)">测试</el-button>
                <el-button v-if="canWrite" size="small" text type="primary" :loading="mappingRefreshActive(row)" :disabled="mappingRefreshActive(row)" @click="doRefreshMapping(row)">{{ mappingRefreshActive(row) ? '刷新中' : '刷新实例' }}</el-button>
                <el-button v-if="canWrite" size="small" text type="danger" @click="removeMapping(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 受治理函数（声明式契约）═══════════ -->
      <el-tab-pane label="函数" name="functions" data-testid="functions-tab">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">函数 <b>{{ detail.functions.length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" data-testid="create-function" @click="openFunction()"><el-icon><Plus /></el-icon> 添加函数</el-button>
          </div>
        </div>
        <el-alert
          class="function-declaration-note"
          title="函数仅登记声明式输入/输出契约，不包含代码、执行器或运行入口。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="card map-card">
          <el-table :data="detail.functions" class="map-table" :empty-text="canWrite ? '暂无函数，点击「添加函数」登记声明式契约' : '暂无函数'">
            <el-table-column label="名称 / 说明" min-width="190">
              <template #default="{ row }">
                <div class="function-name-cell">
                  <b>{{ row.name }}</b>
                  <span class="muted">{{ row.description || '—' }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="输入 Schema" min-width="240">
              <template #default="{ row }">
                <pre class="function-schema mono" :aria-label="`${row.name} 的输入 Schema`">{{ formatFunctionSchema(row.input_schema) }}</pre>
              </template>
            </el-table-column>
            <el-table-column label="输出 Schema" min-width="240">
              <template #default="{ row }">
                <pre class="function-schema mono" :aria-label="`${row.name} 的输出 Schema`">{{ formatFunctionSchema(row.output_schema) }}</pre>
              </template>
            </el-table-column>
            <el-table-column label="标签" min-width="150">
              <template #default="{ row }">
                <div v-if="row.tags?.length" class="function-tags">
                  <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
                </div>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="可见性" width="120">
              <template #default="{ row }">
                <el-tooltip content="仅为声明展示元数据；实际访问仍由场景 ACL 决定" placement="top">
                  <el-tag size="small" effect="plain" :type="row.visibility === 'tenant' ? 'success' : 'info'">
                    {{ row.visibility === 'tenant' ? '租户' : '场景内' }}
                  </el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="145" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text @click="openFunction(row.id)">编辑</el-button>
                <el-button size="small" text type="danger" @click="removeFunction(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 操作（Actions）═══════════ -->
      <el-tab-pane label="操作" name="actions">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">操作 <b>{{ detail.actions.length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openAction()"><el-icon><Plus /></el-icon> 添加操作</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.actions" class="map-table" :empty-text="canWrite ? '暂无操作，点击「添加操作」创建' : '暂无操作'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><b>{{ row.name }}</b></template>
            </el-table-column>
            <el-table-column label="所属实体" min-width="120">
              <template #default="{ row }">
                <span class="ent-chip" :style="{ color: entColor(row.entity_id) }">
                  <i :style="{ background: entColor(row.entity_id) }"></i>{{ entName(row.entity_id) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="执行方式" width="110">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ row.executor_type || '—' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="200">
              <template #default="{ row }"><span class="muted">{{ row.description || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="启用" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="row.enabled === false ? 'info' : 'success'">{{ row.enabled === false ? '否' : '是' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="245" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text @click="openAction(row.id)">编辑</el-button>
                <el-button size="small" text type="primary" :loading="row._executing" @click="doExecuteAction(row)">参数与执行</el-button>
                <el-button size="small" text type="danger" @click="removeAction(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 规则（Rules）═══════════ -->
      <el-tab-pane label="规则" name="rules">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">规则 <b>{{ detail.rules.length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openRule()"><el-icon><Plus /></el-icon> 添加规则</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.rules" class="map-table" :empty-text="canWrite ? '暂无规则，点击「添加规则」创建' : '暂无规则'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><b>{{ row.name }}</b></template>
            </el-table-column>
            <el-table-column label="关联实体" min-width="120">
              <template #default="{ row }">
                <span v-if="row.entity_id" class="ent-chip" :style="{ color: entColor(row.entity_id) }">
                  <i :style="{ background: entColor(row.entity_id) }"></i>{{ entName(row.entity_id) }}
                </span>
                <span v-else class="muted">全局</span>
              </template>
            </el-table-column>
            <el-table-column label="严重级别" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.severity === 'critical' ? 'danger' : row.severity === 'warning' ? 'warning' : 'info'">{{ row.severity || 'info' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="条件" min-width="220">
              <template #default="{ row }"><span class="mono cond-text">{{ condSummary(row.condition) }}</span></template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text @click="openRule(row.id)">编辑</el-button>
                <el-button size="small" text type="primary" @click="doEvalRule(row)">评估</el-button>
                <el-button size="small" text type="danger" @click="removeRule(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 事件（Events）═══════════ -->
      <el-tab-pane label="事件" name="events">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">事件 <b>{{ detail.events.length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openEvent()"><el-icon><Plus /></el-icon> 添加事件</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.events" class="map-table" :empty-text="canWrite ? '暂无事件，点击「添加事件」创建' : '暂无事件'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><b>{{ row.name }}</b></template>
            </el-table-column>
            <el-table-column label="触发来源" width="140">
              <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.trigger_source || '—' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="描述" min-width="220">
              <template #default="{ row }"><span class="muted">{{ row.description || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="启用" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="row.enabled === false ? 'info' : 'success'">{{ row.enabled === false ? '否' : '是' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="205" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  text
                  type="primary"
                  :disabled="row.enabled === false"
                  :loading="publishingEventId === row.id"
                  :title="row.enabled === false ? '请先启用事件后再发布' : '发布事件并异步触发订阅工作流'"
                  @click="publishEvent(row)"
                >发布</el-button>
                <el-button size="small" text @click="openEvent(row.id)">编辑</el-button>
                <el-button size="small" text type="danger" @click="removeEvent(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 工作流（Workflows）═══════════ -->
      <el-tab-pane label="工作流" name="workflows">
        <!-- 可视化编排画布 -->
        <div v-if="wfEditor && canWrite" class="wf-editor-stage">
          <WorkflowEditor
            :model-value="wfEditor"
            :scenario-id="sid"
            :actions="detail.actions"
            :rules="detail.rules"
            :events="detail.events"
            :llm-configs="llmConfigs"
            @update:model-value="wfEditor = $event"
            @close="wfEditor = null"
            @save="saveWorkflow"
            @run-created="openWorkflowRun"
          />
        </div>
        <!-- 工作流列表 -->
        <template v-else>
          <div class="tab-toolbar">
            <div class="tab-stats"><span class="stat">工作流 <b>{{ detail.workflows.length }}</b></span></div>
            <div v-if="canWrite" class="tab-actions">
              <el-button size="small" type="primary" @click="openWorkflow()"><el-icon><Plus /></el-icon> 新建工作流</el-button>
            </div>
          </div>
          <div class="card map-card">
            <el-table :data="detail.workflows" class="map-table" :empty-text="canWrite ? '暂无工作流，点击「新建工作流」开始可视化编排' : '暂无工作流'">
              <el-table-column label="名称" min-width="140">
                <template #default="{ row }"><b>{{ row.name }}</b></template>
              </el-table-column>
              <el-table-column label="触发方式" width="110">
                <template #default="{ row }">
                  <div class="workflow-trigger">
                    <el-tag size="small" effect="plain">{{ workflowTriggerLabel(row.trigger_type) }}</el-tag>
                    <small v-if="workflowTriggerDetail(row)">{{ workflowTriggerDetail(row) }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="workflowStatusType(row.status)">{{ workflowStatusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="流程" min-width="280">
                <template #default="{ row }">
                  <div class="wf-steps">
                    <span class="wf-step" v-for="(s, i) in wfSummary(row)" :key="i">{{ s }}</span>
                    <span class="muted" v-if="!wfSummary(row).length">未配置</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="描述" min-width="160">
                <template #default="{ row }"><span class="muted">{{ row.description || '—' }}</span></template>
              </el-table-column>
              <el-table-column label="操作" :width="canWrite ? 258 : 74" fixed="right">
                <template #default="{ row }">
                  <el-button v-if="canWrite" size="small" text @click="openWorkflow(row.id)">编排</el-button>
                  <el-button v-if="canWrite" size="small" text type="primary" :disabled="row.status !== 'active'" :loading="row._executing" @click="doExecuteWorkflow(row)">执行</el-button>
                  <el-button size="small" text @click="goToWorkflowTasks(row)">任务</el-button>
                  <el-button v-if="canWrite" size="small" text type="danger" @click="removeWorkflow(row.id)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- ═══════════ 数据映射对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="mappingDlg" title="数据映射" width="min(840px, 94vw)" class="glass-dialog">
      <el-form label-position="top">
        <el-form-item label="目标实体">
          <el-select v-model="mappingForm.entity_id" style="width:100%" @change="onMappingEntityChange">
            <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="数据源">
          <el-select v-model="mappingForm.data_source_id" style="width:100%" @change="onMapDsChange">
            <el-option v-for="d in dataSources" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="运行时绑定键（非开发环境必填）">
          <el-input
            v-model.trim="mappingForm.data_source_binding_key"
            class="mono"
            placeholder="例如 data_source:orders:sqlite"
            aria-describedby="mapping-runtime-binding-help"
          />
          <div id="mapping-runtime-binding-help" class="form-help">
            在“连接器与环境”中为同一键配置各环境的数据源；留空仅兼容开发环境的当前数据源。
          </div>
        </el-form-item>
        <el-form-item label="表名">
          <el-select v-model="mappingForm.table_name" style="width:100%" filterable allow-create placeholder="选择或输入表名">
            <el-option v-for="t in mapTables" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="列映射（实体属性 ← 数据列）">
          <div class="colmap-list">
            <div class="mapping-property" v-for="p in (detail.entities.find((e) => e.id === mappingForm.entity_id)?.properties || [])" :key="p.name">
              <div class="colmap-row">
                <span class="colmap-attr">{{ p.name }}</span>
                <el-select v-model="mappingForm.column_map[p.name]" size="small" clearable placeholder="不映射">
                  <el-option v-for="c in mapCols" :key="c" :label="c" :value="c" />
                </el-select>
                <el-button size="small" :disabled="(mappingTransformRules[p.name]?.length || 0) >= 20" @click="addMappingTransform(p.name)">添加转换</el-button>
              </div>
              <div v-if="mappingTransformRules[p.name]?.length" class="transform-rule-list" :aria-label="`${p.name} 的转换规则`">
                <div v-for="(rule, ruleIndex) in mappingTransformRules[p.name]" :key="`${p.name}-${ruleIndex}`" class="transform-rule-row">
                  <span class="transform-order">{{ ruleIndex + 1 }}</span>
                  <el-select v-model="rule.op" size="small" aria-label="转换操作" @change="normalizeMappingTransform(rule)">
                    <el-option v-for="option in mappingTransformOptions" :key="option.value" :label="option.label" :value="option.value" />
                  </el-select>
                  <template v-if="rule.op === 'replace'">
                    <el-input v-model="rule.old" size="small" aria-label="要替换的文本" placeholder="原文本" />
                    <el-input v-model="rule.new" size="small" aria-label="替换后的文本" placeholder="新文本" />
                  </template>
                  <el-input v-else-if="rule.op === 'default'" v-model="rule.value" size="small" aria-label="默认值" placeholder="空值时使用" />
                  <span v-else class="transform-rule-hint">{{ mappingTransformHint(rule.op) }}</span>
                  <el-button text circle type="danger" size="small" :aria-label="`删除 ${p.name} 的第 ${ruleIndex + 1} 条转换`" @click="removeMappingTransform(p.name, ruleIndex)"><el-icon><Delete /></el-icon></el-button>
                </div>
              </div>
            </div>
            <div class="muted" v-if="!mapCols.length">选择表后自动加载列</div>
            <div class="form-help">转换按从上到下执行，仅允许修剪、大小写、替换、默认值及安全类型转换，不支持脚本或任意代码。</div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mappingDlg = false">取消</el-button>
        <el-button type="primary" @click="saveMapping">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 数据映射预览/校验 ═══════════ -->
    <el-dialog v-model="mappingPreviewDlg" title="映射预览与校验" width="920px" class="glass-dialog">
      <div v-if="mappingPreview" v-loading="mappingPreviewLoading" class="mapping-preview" aria-live="polite">
        <div class="mapping-preview-head">
          <div>
            <span class="eyebrow">MAPPING VALIDATION</span>
            <h3>{{ mappingPreview.entity_name }} <span>←</span> {{ mappingPreview.table_name }}</h3>
            <p>{{ mappingPreview.data_source_name || '未命名数据源' }} · {{ mappingPreview.row_count }} 行样本{{ mappingPreview.truncated ? '（已截断）' : '' }}</p>
          </div>
          <el-tag :type="mappingPreview.ok ? 'success' : 'danger'" effect="plain">
            {{ mappingPreview.ok ? '映射可用' : '需要修正' }}
          </el-tag>
        </div>
        <el-alert
          v-if="mappingPreview.errors.length"
          type="error"
          :closable="false"
          title="阻塞问题"
          class="mapping-alert"
          role="alert"
        >
          <ul class="mapping-issue-list">
            <li v-for="error in mappingPreview.errors" :key="error">{{ error }}</li>
          </ul>
        </el-alert>
        <el-alert
          v-if="mappingPreview.warnings.length"
          type="warning"
          :closable="false"
          title="提醒"
          class="mapping-alert"
        >
          <ul class="mapping-issue-list">
            <li v-for="warning in mappingPreview.warnings" :key="warning">{{ warning }}</li>
          </ul>
        </el-alert>
        <div class="mapping-preview-grid">
          <section class="mapping-coverage">
            <div class="preview-section-title">属性覆盖</div>
            <el-table :data="mappingPreview.fields" size="small" empty-text="实体暂无属性">
              <el-table-column label="实体属性" min-width="150">
                <template #default="{ row }">
                  <span>{{ row.property_name }}</span>
                  <el-tag v-if="row.is_key" size="small" effect="plain" class="field-flag">主键</el-tag>
                  <el-tag v-else-if="row.is_required" size="small" effect="plain" type="warning" class="field-flag">必填</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="源列" min-width="150">
                <template #default="{ row }"><span class="mono">{{ row.source_column || '未配置' }}</span></template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain" :type="mappingFieldType(row.status)">{{ mappingFieldLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </section>
          <section class="mapping-samples">
            <div class="preview-section-title">源表样本</div>
            <el-table :data="mappingPreviewRows" size="small" max-height="300" empty-text="暂无数据">
              <el-table-column v-for="column in mappingPreview.columns" :key="column" :prop="column" :label="column" min-width="130" show-overflow-tooltip />
            </el-table>
          </section>
        </div>
        <section v-if="mappingTransformedRows.length" class="mapping-transformed">
          <div class="preview-section-title">转换后的对象样本</div>
          <el-table :data="mappingTransformedRows" size="small" max-height="300">
            <el-table-column v-for="column in mappingTransformedColumns" :key="column" :prop="column" :label="column" min-width="130" show-overflow-tooltip />
          </el-table>
        </section>
        <div v-if="mappingPreview.unmapped_columns.length" class="unmapped-columns">
          未映射源列：<span v-for="column in mappingPreview.unmapped_columns" :key="column" class="col-map">{{ column }}</span>
        </div>
      </div>
      <el-empty v-else description="暂无预览数据" />
      <template #footer>
        <el-button @click="mappingPreviewDlg = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 受治理函数对话框（仅声明式契约）═══════════ -->
    <el-dialog v-if="canWrite" v-model="functionDlg" :title="functionForm.id ? '编辑函数' : '添加函数'" width="680px" class="glass-dialog" data-testid="function-dialog">
      <el-alert
        title="此处仅保存声明式 JSON Schema 契约；不支持代码、脚本、命令、URL 或执行配置。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="function-form">
        <div class="form-row">
          <el-form-item label="函数名称" required class="form-col">
            <el-input v-model="functionForm.name" maxlength="200" show-word-limit placeholder="如：计算订单风险等级" />
          </el-form-item>
          <el-form-item label="可见性" class="form-col">
            <el-select v-model="functionForm.visibility" style="width:100%">
              <el-option label="仅当前场景" value="scenario" />
              <el-option label="租户范围展示" value="tenant" />
            </el-select>
            <div class="form-help">仅影响声明展示；实际授权始终以场景 ACL 为准。</div>
          </el-form-item>
        </div>
        <el-form-item label="说明">
          <el-input v-model="functionForm.description" type="textarea" :rows="2" maxlength="8000" show-word-limit placeholder="说明这个业务函数的输入、输出与适用范围" />
        </el-form-item>
        <el-form-item label="标签（用逗号分隔，可选）">
          <el-input v-model="functionForm.tags_text" maxlength="1619" placeholder="如：订单、风险、只读" />
          <div class="form-help">最多 20 个标签，每个标签最多 80 个字符。</div>
        </el-form-item>
        <el-form-item label="输入 Schema（JSON）" required>
          <el-input
            v-model="functionForm.input_schema_text"
            type="textarea"
            :rows="8"
            class="mono"
            placeholder='{"type":"object","properties":{"order_id":{"type":"string"}},"required":["order_id"],"additionalProperties":false}'
          />
          <div class="form-help">顶层必须是 <code>object</code> 类型 JSON Schema；仅描述数据结构。</div>
        </el-form-item>
        <el-form-item label="输出 Schema（JSON）" required>
          <el-input
            v-model="functionForm.output_schema_text"
            type="textarea"
            :rows="8"
            class="mono"
            placeholder='{"type":"object","properties":{"risk_level":{"type":"string"}},"additionalProperties":false}'
          />
          <div class="form-help">顶层必须是 <code>object</code> 类型 JSON Schema；不会创建执行能力。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="functionSaving" @click="functionDlg = false">取消</el-button>
        <el-button type="primary" :loading="functionSaving" @click="saveFunction">保存声明</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 操作对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="actionDlg" :title="actionForm.id ? '编辑操作' : '添加操作'" width="640px" class="glass-dialog">
      <el-form label-position="top">
        <div class="form-row">
          <el-form-item label="名称" class="form-col">
            <el-input v-model="actionForm.name" placeholder="如：标记违规、生成报告" />
          </el-form-item>
          <el-form-item label="所属实体" class="form-col">
            <el-select v-model="actionForm.entity_id" style="width:100%">
              <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input v-model="actionForm.description" type="textarea" :rows="2" placeholder="这个操作做什么" />
        </el-form-item>
        <div class="form-row">
          <el-form-item label="执行方式" class="form-col">
            <el-select v-model="actionForm.executor_type" style="width:100%">
              <el-option label="SQL 查询" value="sql" />
              <el-option label="受管技能 (Skill)" value="skill" />
              <el-option label="MCP 工具" value="mcp" />
              <el-option label="HTTP 请求" value="http" />
            </el-select>
          </el-form-item>
          <el-form-item label="启用" class="form-col">
            <el-switch v-model="actionForm.enabled" />
          </el-form-item>
        </div>
        <div class="form-row">
          <el-form-item label="执行前需要确认" class="form-col">
            <el-switch v-model="actionForm.requires_confirmation" />
            <div class="form-help">确认前只允许预演，不会调用执行器</div>
          </el-form-item>
          <el-form-item label="要求幂等键" class="form-col">
            <el-switch v-model="actionForm.idempotency_required" />
            <div class="form-help">防止同一请求重复执行</div>
          </el-form-item>
        </div>
        <el-form-item label="执行配置（JSON，按执行方式不同而不同）">
          <el-input v-model="actionForm.executor_config_text" type="textarea" :rows="5" class="mono" placeholder='{"data_source_id": "...", "sql": "SELECT ..."}' />
          <div class="form-help" data-testid="action-runtime-binding-help">
            受治理的 SQL/MCP 可额外保存 <code>data_source_binding_key</code> 或 <code>mcp_binding_key</code>（以及对应的 <code>…_binding_ref</code>）；Skill 必须使用 <code>skill_id</code>。运行环境由部署实例固定；生产和预发布不会回退到开发环境的直接 ID。
          </div>
        </el-form-item>
        <el-form-item label="输入参数 Schema（JSON，可选）">
          <el-input v-model="actionForm.input_schema_text" type="textarea" :rows="3" class="mono" placeholder='{"drug_name": {"type": "string"}}' />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="actionDlg = false">取消</el-button>
        <el-button type="primary" @click="saveAction">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 操作参数与安全执行对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="actionExecuteDlg" :title="`执行操作：${actionExecuteRow?.name || ''}`" width="640px" class="glass-dialog">
      <el-alert
        :title="`权限范围：${actionExecuteRow?.permission_scope || 'scenario'} · ${actionExecuteRow?.requires_confirmation === false ? '可直接执行' : '需要确认后执行'}`"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="action-params-form">
        <el-form-item
          v-for="field in actionParameterFields"
          :key="field.name"
          :label="`${field.name}${field.required ? ' *' : ''}`"
        >
          <div v-if="field.description" class="action-param-hint">{{ field.description }}</div>
          <el-select v-if="field.schema.enum" v-model="actionParamsForm[field.name]" style="width:100%">
            <el-option v-for="option in field.schema.enum" :key="String(option)" :label="String(option)" :value="option" />
          </el-select>
          <el-input-number
            v-else-if="field.schema.type === 'number' || field.schema.type === 'integer'"
            v-model="actionParamsForm[field.name]"
            :precision="field.schema.type === 'integer' ? 0 : undefined"
            controls-position="right"
            style="width:100%"
          />
          <el-switch v-else-if="field.schema.type === 'boolean'" v-model="actionParamsForm[field.name]" />
          <el-input
            v-else-if="field.schema.type === 'array' || field.schema.type === 'object'"
            v-model="actionParamsText[field.name]"
            type="textarea"
            :rows="3"
            class="mono"
            :placeholder="field.schema.type === 'array' ? '请输入 JSON 数组' : '请输入 JSON 对象'"
          />
          <el-input v-else v-model="actionParamsForm[field.name]" :placeholder="field.schema.default !== undefined ? `默认值：${field.schema.default}` : '请输入参数'" />
        </el-form-item>
        <el-empty v-if="!actionParameterFields.length" :image-size="56" description="此操作无需输入参数" />
      </el-form>
      <div class="action-execution-meta">
        <span>幂等键</span>
        <code class="mono">{{ actionIdempotencyKey }}</code>
        <span class="muted">本次确认执行保持不变</span>
      </div>
      <el-alert v-if="actionPreviewResult" class="action-preview-alert" type="success" :closable="false" show-icon>
        <template #title>预演完成：未调用执行器，可确认执行</template>
        <pre class="action-preview-text mono">{{ JSON.stringify(actionPreviewResult.result?.plan || actionPreviewResult.result, null, 2) }}</pre>
      </el-alert>
      <div v-if="actionPreviewResult?.connector_audit?.length" class="action-runtime-audit" role="status">
        <span>运行时连接器</span>
        <el-tag v-for="audit in actionPreviewResult.connector_audit" :key="`${audit.kind}-${audit.binding_id || audit.connector_id}`" size="small" effect="plain">
          {{ audit.environment }} · {{ audit.connector_name || audit.connector_id }}{{ audit.managed ? '（受治理绑定）' : '（兼容直连）' }}
        </el-tag>
      </div>
      <dl v-if="actionPreviewResult" class="action-runtime-provenance" aria-label="本次预演的运行定义证据">
        <div>
          <dt>运行环境</dt>
          <dd>{{ actionPreviewResult.environment || 'dev' }}</dd>
        </div>
        <div>
          <dt>定义来源</dt>
          <dd>{{ actionPreviewResult.definition_source === 'release' ? '已发布快照' : '开发中定义' }}</dd>
        </div>
        <div v-if="actionPreviewResult.definition_snapshot_id">
          <dt>发布快照 ID</dt>
          <dd class="mono">{{ actionPreviewResult.definition_snapshot_id }}</dd>
        </div>
        <div v-if="actionPreviewResult.release_id">
          <dt>发布记录 ID</dt>
          <dd class="mono">{{ actionPreviewResult.release_id }}</dd>
        </div>
        <div v-if="actionPreviewResult.definition_hash">
          <dt>定义校验哈希</dt>
          <dd class="mono">{{ actionPreviewResult.definition_hash }}</dd>
        </div>
      </dl>
      <template #footer>
        <el-button @click="actionExecuteDlg = false">取消</el-button>
        <el-button :loading="actionPreviewing" @click="previewActionExecution">预演</el-button>
        <el-button type="primary" :loading="actionExecuting" :disabled="!hasPinnedActionPreview" @click="confirmActionExecution">确认执行</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 规则对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="ruleDlg" :title="ruleForm.id ? '编辑规则' : '添加规则'" width="640px" class="glass-dialog">
      <el-form label-position="top">
        <div class="form-row">
          <el-form-item label="名称" class="form-col">
            <el-input v-model="ruleForm.name" placeholder="如：数量超过允许范围" />
          </el-form-item>
          <el-form-item label="严重级别" class="form-col">
            <el-select v-model="ruleForm.severity" style="width:100%">
              <el-option label="提示 (info)" value="info" />
              <el-option label="警告 (warning)" value="warning" />
              <el-option label="严重 (critical)" value="critical" />
            </el-select>
          </el-form-item>
        </div>
        <div class="form-row">
          <el-form-item label="关联实体（可选，留空为全局规则）" class="form-col">
            <el-select v-model="ruleForm.entity_id" clearable style="width:100%">
              <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="启用" class="form-col">
            <el-switch v-model="ruleForm.enabled" />
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input v-model="ruleForm.description" type="textarea" :rows="2" placeholder="这条规则检查什么" />
        </el-form-item>
        <el-form-item label="条件表达式（JSON，支持 and/or/not 组合）">
          <el-input v-model="ruleForm.condition_text" type="textarea" :rows="6" class="mono" placeholder='{"op": "and", "conditions": [{"field": "数量", "op": ">", "value": 2}]}' />
        </el-form-item>
        <el-form-item label="命中后动作（文本说明）">
          <el-input v-model="ruleForm.action_on_match" placeholder="如：标记为疑似违规并通知审核员" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDlg = false">取消</el-button>
        <el-button type="primary" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 事件对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="eventDlg" :title="eventForm.id ? '编辑事件' : '添加事件'" width="560px" class="glass-dialog">
      <el-form label-position="top">
        <div class="form-row">
          <el-form-item label="名称" class="form-col">
            <el-input v-model="eventForm.name" placeholder="如：新业务记录录入" />
          </el-form-item>
          <el-form-item label="触发来源" class="form-col">
            <el-input v-model="eventForm.trigger_source" placeholder="如：数据源同步 / 手动 / 定时" />
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input v-model="eventForm.description" type="textarea" :rows="2" placeholder="这个事件代表什么业务含义" />
        </el-form-item>
        <el-form-item label="载荷 Schema（JSON，可选）">
          <el-input v-model="eventForm.payload_schema_text" type="textarea" :rows="4" class="mono" placeholder='{"invoice_id": {"type": "string"}}' />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="eventForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="eventDlg = false">取消</el-button>
        <el-button type="primary" @click="saveEvent">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 执行结果对话框 ═══════════ -->
    <el-dialog v-model="execResultDlg" title="执行结果" width="640px" class="glass-dialog">
      <pre class="exec-result mono">{{ execResultText }}</pre>
      <template #footer>
        <el-button type="primary" @click="execResultDlg = false">关闭</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import GraphCanvas from '@/components/GraphCanvas.vue'
import EditorPanel from '@/components/EditorPanel.vue'
import WorkflowEditor from '@/components/workflow/WorkflowEditor.vue'
import type { ActionExecutionLog, AssistantActionPreview, ScenarioDetail, GraphData, GraphNode, GraphEdge, Entity, DataMapping, DataMappingPreview, DataMappingRefreshJob, FunctionDefinition, ObjectDetail, ObjectSearchItem, WorkflowRun } from '@/types'

const route = useRoute()
const router = useRouter()
const sid = route.params.id as string
const scenarioLoading = ref(true)
const scenarioAccessDenied = ref(false)
const scenarioLoadError = ref('')

const detail = ref<ScenarioDetail>({
  id: sid, name: '', description: '',
  entities: [], relations: [], data_sources: [],
  instances: [], relation_instances: [], mappings: [],
  functions: [],
  actions: [], rules: [], events: [], workflows: [],
})
// The API supplies this per current user; treat an absent value as read-only.
const canWrite = computed(() => detail.value.can_write === true)
const dataSources = ref<any[]>([])
const llmConfigs = ref<any[]>([])
const stageNames = new Set(['flow', 'ontology', 'instances', 'mappings', 'functions', 'actions', 'rules', 'events', 'workflows'])
const requestedStage = Array.isArray(route.query.stage) ? route.query.stage[0] : route.query.stage
const tab = ref(typeof requestedStage === 'string' && stageNames.has(requestedStage) ? requestedStage : 'flow')
const instFilter = ref('')
const saving = ref(false)
const objectQuery = ref('')
const objectItems = ref<ObjectSearchItem[]>([])
const objectTotal = ref(0)
const objectLoading = ref(false)
const objectLoadingMore = ref(false)
const objectNextOffset = ref(0)
const objectAppliedKey = ref('')
const selectedObjectId = ref<string | null>(null)
const objectDetail = ref<ObjectDetail | null>(null)
const objectQuestion = ref('')
const objectQuestionLoading = ref(false)
const objectQuestionSearched = ref(false)
const objectQuestionTerm = ref('')
const objectQuestionAnswer = ref('')
const objectQuestionEvidence = ref<ObjectDetail[]>([])
const objectQuestionInputRef = ref<any>(null)
const querySectionRef = ref<HTMLElement | null>(null)
const auditSectionRef = ref<HTMLElement | null>(null)
const executionLogs = ref<ActionExecutionLog[]>([])
const auditLoading = ref(false)
const controlledActionId = ref('')
const OBJECT_PAGE_SIZE = 50
let objectRequestId = 0
let objectSearchViewDisposed = false
let objectPendingKey = ''

const graphPalette = ['#27b9b0', '#438be5', '#65a9df', '#4aa9c1', '#52c3a1', '#6f93d7']
function visualColor(color: string | undefined, index: number) {
  if (!color || ['#6366f1', '#4f46e5', '#06b6d4'].includes(color.toLowerCase())) {
    return graphPalette[index % graphPalette.length]
  }
  return color
}

// ── 悬浮编辑面板状态 ──
const editor = ref<{ kind: 'entity' | 'relation' | 'instance'; id?: string; form: any } | null>(null)
// 切换 tab 时关闭悬浮编辑器，避免跨 tab 状态残留；进入实例页时刷新对象运行时列表。
watch(tab, (value) => {
  editor.value = null
  if (value === 'instances') searchObjects()
  if (value === 'flow') loadExecutionLogs(true)
  if (route.query.stage !== value) {
    void router.replace({ query: { ...route.query, stage: value } })
  }
})
watch(() => route.query.stage, (value) => {
  const stage = Array.isArray(value) ? value[0] : value
  if (typeof stage === 'string' && stageNames.has(stage) && stage !== tab.value) tab.value = stage
})
watch(instFilter, () => {
  if (tab.value === 'instances') searchObjects()
})

// ── 图谱数据 ──
const schemaGraph = computed<GraphData>(() => {
  const nodes: GraphNode[] = detail.value.entities
    .filter((e) => e.id)
    .map((e, index) => ({
      id: e.id!, label: e.name, type: 'entity', color: visualColor(e.color, index),
      meta: { count: e.properties.length, abstract: e.is_abstract, description: e.description },
    }))
  const edges: GraphEdge[] = detail.value.relations
    .filter((r) => r.id)
    .map((r) => ({
      id: r.id!, source: r.source_entity_id, target: r.target_entity_id,
      label: r.name, type: r.relation_type,
    }))
  return { nodes, edges }
})
const instanceGraph = computed<GraphData>(() => {
  const ents = (instFilter.value ? detail.value.entities.filter((e) => e.id === instFilter.value) : detail.value.entities).filter((e) => e.id)
  const entIds = new Set(ents.map((e) => e.id as string))
  const nodes: GraphNode[] = []
  const entNode = new Map<string, string>()
  for (const [index, e] of ents.entries()) {
    const id = `ent:${e.id}`
    entNode.set(e.id as string, id)
    nodes.push({ id, label: e.name, type: 'entity', color: visualColor(e.color, index), meta: { count: detail.value.instances.filter((i) => i.entity_id === e.id).length, entity_name: e.name } })
  }
  for (const i of detail.value.instances) {
    if (!i.id || !entIds.has(i.entity_id)) continue
    const entityIndex = detail.value.entities.findIndex((e) => e.id === i.entity_id)
    const entity = detail.value.entities.find((e) => e.id === i.entity_id)
    nodes.push({ id: i.id, label: i.name, type: 'instance', color: visualColor(entity?.color, Math.max(0, entityIndex)), meta: { entity: i.entity_id, entity_name: entity?.name || '未分类' } })
  }
  const edges: GraphEdge[] = []
  for (const i of detail.value.instances) {
    if (!i.id || !entIds.has(i.entity_id)) continue
    edges.push({ id: `ie:${i.id}`, source: entNode.get(i.entity_id)!, target: i.id, type: 'belongs' })
  }
  const nodeIds = new Set(nodes.map((n) => n.id))
  for (const ri of detail.value.relation_instances) {
    if (ri.id && nodeIds.has(ri.source_instance_id) && nodeIds.has(ri.target_instance_id)) {
      edges.push({ id: ri.id, source: ri.source_instance_id, target: ri.target_instance_id, label: ri.relation_name || detail.value.relations.find((r) => r.id === ri.relation_id)?.name || '', type: 'rel' })
    }
  }
  return { nodes, edges }
})
const legend = computed(() => detail.value.entities.map((e, index) => ({ label: e.name, color: visualColor(e.color, index) })))
function objectSearchKey() {
  return JSON.stringify([objectQuery.value.trim(), instFilter.value || ''])
}
const objectFilterPending = computed(() => Boolean(objectAppliedKey.value) && objectAppliedKey.value !== objectSearchKey())
const hasMoreObjects = computed(() => !objectFilterPending.value && objectNextOffset.value < objectTotal.value)
const objectResultStatus = computed(() => {
  if (objectLoading.value) return '正在加载对象列表…'
  if (objectLoadingMore.value) return `正在加载更多对象；已加载 ${objectItems.value.length} / ${objectTotal.value} 个结果`
  if (objectFilterPending.value) return `当前显示 ${objectItems.value.length} / ${objectTotal.value} 个结果；搜索条件已变更，按回车应用`
  if (!objectTotal.value) return objectQuery.value.trim() ? '没有匹配对象' : '暂无可浏览对象'
  return hasMoreObjects.value
    ? `已加载 ${objectItems.value.length} / ${objectTotal.value} 个结果，可继续加载更多`
    : `已加载全部 ${objectTotal.value} 个结果`
})

function entName(id: string) { return detail.value.entities.find((e) => e.id === id)?.name || '—' }
function entColor(id: string) {
  const index = detail.value.entities.findIndex((e) => e.id === id)
  return visualColor(detail.value.entities[index]?.color, Math.max(0, index))
}
function dsName(id: string) { return dataSources.value.find((d) => d.id === id)?.name || '—' }

// ── 闭环工作台：状态来自真实资源，不把“访问过”误当作“已完成” ──
type FlowStepId = 'model' | 'mapping' | 'query' | 'approval' | 'audit'
type FlowStepStatus = 'complete' | 'current' | 'pending'
type FlowStep = {
  id: FlowStepId
  number: number
  title: string
  description: string
  nextHint: string
  actionLabel: string
  status: FlowStepStatus
}

const controlledAction = computed<any>(() => detail.value.actions.find((action) => action.id === controlledActionId.value) || null)
const approvalWorkflows = computed<any[]>(() => detail.value.workflows.filter((workflow) =>
  (workflow.nodes || []).some((node: any) => node.type === 'approval'),
))
const querySuggestions = computed(() => {
  const suggestions: string[] = []
  for (const item of objectItems.value.slice(0, 2)) suggestions.push(`查找 ${item.name}，说明属性、关系和来源`)
  for (const entity of detail.value.entities.slice(0, 2)) suggestions.push(`列出可见的${entity.name}对象及其来源`)
  return [...new Set(suggestions)].slice(0, 3)
})
const flowCompletion = computed<Record<FlowStepId, boolean>>(() => ({
  model: detail.value.entities.length > 0,
  mapping: detail.value.mappings.length > 0 && objectTotal.value > 0,
  query: objectQuestionEvidence.value.length > 0,
  approval: executionLogs.value.some((log) => log.target_type === 'workflow' && log.mode !== 'dry_run' && ['success', 'succeeded'].includes(log.status)),
  audit: executionLogs.value.length > 0,
}))
const flowSteps = computed<FlowStep[]>(() => {
  const definitions: Omit<FlowStep, 'status'>[] = [
    { id: 'model', number: 1, title: '描述与附件 → AI 草稿', description: '检查实体、属性、关系和 Change Set 差异', nextHint: '打开 AI 助手，附件保持临时上下文；确认差异后再保存草稿。', actionLabel: '生成或完善草稿' },
    { id: 'mapping', number: 2, title: '映射并刷新真实对象', description: '预览字段、测试主键与转换，再刷新对象运行时', nextHint: '进入数据映射，先预览和测试，再刷新生成可追踪对象。', actionLabel: '配置数据映射' },
    { id: 'query', number: 3, title: '查询对象并核对引用', description: '从对象、关系和数据来源形成可复核回答', nextHint: '输入一个业务问题，核对每条 [O] 引用的对象属性、关系和来源。', actionLabel: '开始对象查询' },
    { id: 'approval', number: 4, title: '预演 → 人工审批 → Action', description: '通过审批工作流暂停副作用，批准后再执行', nextHint: '选择 Action，生成含人工审批节点的流程草稿，启用后提交任务。', actionLabel: '准备审批执行' },
    { id: 'audit', number: 5, title: '回看参数、结果与运行证据', description: '核对环境、定义版本、连接器和执行结果', nextHint: '展开最新审计记录，检查参数、定义版本、权限判定和结果。', actionLabel: '查看执行审计' },
  ]
  const firstIncomplete = definitions.findIndex((step) => !flowCompletion.value[step.id])
  return definitions.map((step, index) => ({
    ...step,
    status: flowCompletion.value[step.id] ? 'complete' : index === firstIncomplete ? 'current' : 'pending',
  }))
})
const completedFlowSteps = computed(() => flowSteps.value.filter((step) => step.status === 'complete').length)
const flowProgress = computed(() => Math.round((completedFlowSteps.value / Math.max(flowSteps.value.length, 1)) * 100))
const nextFlowStep = computed(() => flowSteps.value.find((step) => step.status === 'current') || flowSteps.value[flowSteps.value.length - 1])

function flowStatusLabel(status: FlowStepStatus) {
  return status === 'complete' ? '已完成' : status === 'current' ? '当前步骤' : '待开始'
}
function flowStatusType(status: FlowStepStatus) {
  return status === 'complete' ? 'success' : status === 'current' ? 'warning' : 'info'
}
function openAssistantDraft() {
  if (!canWrite.value) return
  window.dispatchEvent(new CustomEvent('assistant-open-request', {
    detail: {
      mode: 'draft',
      prompt: `请基于“${detail.value.name}”的业务描述生成或完善本体 Change Set 草稿。请覆盖核心实体、属性、关系，列出不确定项，并指出后续数据映射需要确认的数据源、主键和字段。\n\n业务描述：${detail.value.description || '暂无，请先向我询问业务目标和边界。'}`,
    },
  }))
}
async function activateFlowStep(id: FlowStepId) {
  if (id === 'model') {
    openAssistantDraft()
    return
  }
  if (id === 'mapping') {
    tab.value = 'mappings'
    return
  }
  if (id === 'query') {
    tab.value = 'flow'
    await nextTick()
    querySectionRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    objectQuestionInputRef.value?.focus?.()
    return
  }
  if (id === 'approval') {
    if (!detail.value.actions.length) openActionsWorkspace()
    else if (!approvalWorkflows.value.length && canWrite.value) prepareApprovalWorkflow()
    else {
      tab.value = 'flow'
      await nextTick()
      document.getElementById('governed-execution-title')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    return
  }
  tab.value = 'flow'
  await nextTick()
  auditSectionRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  await loadExecutionLogs(true)
}

function normalizeObjectQuestionTerm(value: string, entityName = '') {
  return value
    .replace(/[“”"'‘’]/g, ' ')
    .replace(entityName, ' ')
    .replace(/请|帮我|查询|查找|搜索|查看|找出|列出|展示|告诉我|当前|本场景|可见|所有|相关的?|对象|记录|数据|来源|关系|属性|有哪些|是什么|在哪里|多少|以及|并且|并说明|说明/g, ' ')
    .replace(/[，。！？?,.;；:：()（）\[\]【】]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}
function objectQuestionCandidates(question: string, entityName = '') {
  const quoted = [...question.matchAll(/[“"'‘]([^”"'’]{1,80})[”"'’]/g)].map((match) => match[1])
  const segments = question.split(/[的，。！？?,.;；:：]/).map((part) => normalizeObjectQuestionTerm(part, entityName))
  const cleaned = normalizeObjectQuestionTerm(question, entityName)
  return [...new Set([...quoted, cleaned, ...segments].map((term) => term.trim()).filter((term) => term && term !== entityName))].sort((a, b) => a.length - b.length)
}
async function runObjectQuestion() {
  const question = objectQuestion.value.trim()
  if (!question || objectQuestionLoading.value) return
  objectQuestionLoading.value = true
  objectQuestionSearched.value = true
  objectQuestionAnswer.value = ''
  objectQuestionEvidence.value = []
  objectQuestionTerm.value = ''
  try {
    const mentionedEntity = [...detail.value.entities]
      .filter((entity) => entity.id && question.includes(entity.name))
      .sort((a, b) => b.name.length - a.name.length)[0]
    const terms = objectQuestionCandidates(question, mentionedEntity?.name || '')
    const attempts = terms.length ? terms : mentionedEntity ? [''] : []
    if (!attempts.length) {
      objectQuestionAnswer.value = '请补充一个对象名称、属性值或实体类型；系统不会把缺少依据的推测当作业务事实。'
      return
    }
    let matched: Awaited<ReturnType<typeof api.searchObjects>> | null = null
    let usedTerm = ''
    for (const term of attempts.slice(0, 5)) {
      const result = await api.searchObjects(sid, {
        q: term || undefined,
        entity_id: mentionedEntity?.id || undefined,
        limit: 10,
        offset: 0,
      })
      if (result.items.length) {
        matched = result
        usedTerm = term || mentionedEntity?.name || ''
        break
      }
    }
    if (!matched) {
      objectQuestionAnswer.value = '当前可见对象中没有找到匹配事实。请缩短关键词，或先检查数据映射是否已经测试并刷新。'
      return
    }
    const settled = await Promise.allSettled(matched.items.slice(0, 5).map((item) => api.getObject(sid, item.id)))
    objectQuestionEvidence.value = settled
      .filter((result): result is PromiseFulfilledResult<ObjectDetail> => result.status === 'fulfilled')
      .map((result) => result.value)
    objectQuestionTerm.value = usedTerm
    const shown = objectQuestionEvidence.value.length
    objectQuestionAnswer.value = `找到 ${matched.total} 个匹配对象，下面展示前 ${shown} 个可复核结果。每条结论都绑定对象引用 [O1]–[O${shown}]，可继续查看完整属性、邻接关系与来源。`
  } catch (error: any) {
    objectQuestionAnswer.value = ''
    ElMessage.error(error?.message || '对象查询失败')
  } finally {
    objectQuestionLoading.value = false
  }
}
function askObjectSuggestion(suggestion: string) {
  objectQuestion.value = suggestion
  void runObjectQuestion()
}
function objectAttributeEntries(item: ObjectDetail) {
  return Object.entries(item.attributes || {}).slice(0, 5)
}
function objectSourceLabel(item: ObjectDetail) {
  return item.provenance?.data_source_name || (item.provenance?.kind === 'imported' ? '已映射数据源' : '手动创建')
}
function objectSourceReference(item: ObjectDetail) {
  return [item.provenance?.table_name, item.provenance?.reference].filter(Boolean).join(' · ') || `object:${item.id}`
}
function objectRelationSummary(item: ObjectDetail) {
  if (!item.relations?.length) return '暂无可见关系'
  const summary = item.relations.slice(0, 3).map((relation) => `${relation.relation_name || '关联'} → ${relation.related_object_name}`).join('；')
  return item.relations.length > 3 ? `${summary} 等 ${item.relations.length} 条` : summary
}
async function openEvidenceObject(id: string) {
  tab.value = 'instances'
  await nextTick()
  await selectObject(id)
}

async function loadExecutionLogs(silent = false) {
  if (auditLoading.value) return
  auditLoading.value = true
  try {
    executionLogs.value = await api.listExecutionLogs(sid, 50)
  } catch (error: any) {
    if (!silent) ElMessage.error(error?.message || '执行审计加载失败')
  } finally {
    auditLoading.value = false
  }
}
function auditStatusLabel(status: string) {
  return ({ success: '成功', succeeded: '成功', failed: '失败', dry_run: '预演', confirmation_required: '待确认', running: '执行中' } as Record<string, string>)[status] || status || '未知'
}
function auditStatusType(status: string) {
  if (['success', 'succeeded'].includes(status)) return 'success'
  if (['failed', 'error'].includes(status)) return 'danger'
  if (['dry_run', 'confirmation_required'].includes(status)) return 'warning'
  return 'info'
}
function auditStatusTone(status: string) {
  if (['success', 'succeeded'].includes(status)) return 'success'
  if (['failed', 'error'].includes(status)) return 'danger'
  return 'warning'
}
function auditActorLabel(log: ActionExecutionLog) {
  if (log.actor_type === 'agent') return log.agent_id ? `Agent · ${log.agent_id}` : 'Agent（标识未知）'
  if (log.actor_type === 'user') return log.actor_user_id ? `用户 · ${log.actor_user_id}` : '已认证用户（标识未知）'
  return '未知（旧记录或无可验证上下文）'
}
function auditPermissionLabel(log: ActionExecutionLog) {
  const decision = log.permission_decision || {}
  if (!Object.keys(decision).length) return '未知（旧记录未保存判定）'
  const value = decision.decision ?? decision.result ?? decision.allowed
  return value === undefined ? JSON.stringify(decision) : String(value)
}
function auditDataContextLabel(log: ActionExecutionLog) {
  const context = log.data_context || {}
  if (!Object.keys(context).length) return '无或未知'
  return Object.entries(context).slice(0, 3).map(([key, value]) => {
    const display = value && typeof value === 'object' ? JSON.stringify(value) : String(value)
    return `${key}=${display}`
  }).join(' · ')
}
function openActionsWorkspace() {
  tab.value = 'actions'
  if (canWrite.value && !detail.value.actions.length) nextTick(() => openAction())
}
function openWorkflowWorkspace(id?: string) {
  tab.value = 'workflows'
  nextTick(() => openWorkflow(id))
}
function prepareApprovalWorkflow() {
  if (!canWrite.value || !controlledAction.value?.id) return
  const action = controlledAction.value
  tab.value = 'workflows'
  wfEditor.value = {
    name: `${action.name}审批执行`,
    description: `人工审批通过后执行类型化 Action「${action.name}」。保存前请核对参数映射、审批说明和启用状态。`,
    trigger_type: 'manual',
    trigger_config: { interval_seconds: 300, max_attempts: 3, retry_backoff_seconds: 5, timeout_seconds: 300, event_id: '' },
    steps: [],
    nodes: [
      { id: 'start', type: 'start', name: '开始', position: { x: 40, y: 120 }, data: { name: '开始' } },
      { id: 'approval', type: 'approval', name: '业务影响审批', position: { x: 280, y: 120 }, data: { name: '业务影响审批', instructions: `请核对「${action.name}」的对象范围、输入参数、权限和预演影响后批准或驳回。`, timeout_seconds: 86400, on_timeout: 'reject' } },
      { id: 'action', type: 'action', name: `执行 ${action.name}`, position: { x: 540, y: 120 }, data: { name: `执行 ${action.name}`, action_id: action.id, params: {} } },
      { id: 'end', type: 'end', name: '结束', position: { x: 800, y: 120 }, data: { name: '结束', summary: '审批通过并完成受控 Action 执行。' } },
    ],
    edges: [
      { id: 'e-start-approval', source: 'start', target: 'approval' },
      { id: 'e-approval-action', source: 'approval', target: 'action' },
      { id: 'e-action-end', source: 'action', target: 'end' },
    ],
    status: 'draft',
    enabled: true,
  }
  ElMessage.info('已生成“审批 → Action”流程草稿，请核对参数并保存启用')
}
function goToScenarioTasks() {
  router.push({ name: 'tasks', query: { scenario_id: sid, return_to: route.fullPath } })
}

// ── 对象运行时浏览 ──
async function searchObjects() {
  const requestKey = objectSearchKey()
  if (objectLoading.value && requestKey === objectPendingKey) return
  const requestId = ++objectRequestId
  objectPendingKey = requestKey
  objectLoading.value = true
  // A reset invalidates an older append request. It is not safe to append a
  // page captured under a prior entity filter or keyword after this point.
  objectLoadingMore.value = false
  try {
    const result = await api.searchObjects(sid, {
      q: objectQuery.value.trim() || undefined,
      entity_id: instFilter.value || undefined,
      limit: OBJECT_PAGE_SIZE,
      offset: 0,
    })
    if (objectSearchViewDisposed || requestId !== objectRequestId || requestKey !== objectSearchKey()) return
    objectItems.value = result.items
    objectTotal.value = result.total
    objectNextOffset.value = result.offset + result.items.length
    objectAppliedKey.value = requestKey
    if (selectedObjectId.value && !result.items.some((item) => item.id === selectedObjectId.value)) {
      selectedObjectId.value = null
      objectDetail.value = null
    }
  } catch (e: any) {
    if (!objectSearchViewDisposed && requestId === objectRequestId) {
      // Preserve the current visible page and the existing ACL/error behavior.
      // A failed refresh must never discard objects the user was already allowed to see.
      ElMessage.error(e?.response?.data?.detail || e?.message || '对象列表加载失败')
    }
  } finally {
    if (!objectSearchViewDisposed && requestId === objectRequestId) {
      objectLoading.value = false
      objectPendingKey = ''
    }
  }
}

async function loadMoreObjects() {
  if (objectLoading.value || objectLoadingMore.value || objectFilterPending.value || !hasMoreObjects.value) return
  const requestKey = objectSearchKey()
  const offset = objectNextOffset.value
  const requestId = ++objectRequestId
  objectLoadingMore.value = true
  try {
    const result = await api.searchObjects(sid, {
      q: objectQuery.value.trim() || undefined,
      entity_id: instFilter.value || undefined,
      limit: OBJECT_PAGE_SIZE,
      offset,
    })
    if (objectSearchViewDisposed || requestId !== objectRequestId || requestKey !== objectSearchKey() || objectAppliedKey.value !== requestKey) return
    // Offset pagination may overlap if the dataset changes while the user is
    // browsing. Deduplicate by stable object ID rather than showing duplicate rows.
    const knownIds = new Set(objectItems.value.map((item) => item.id))
    const appended = result.items.filter((item) => !knownIds.has(item.id))
    objectItems.value = [...objectItems.value, ...appended]
    objectTotal.value = result.total
    objectNextOffset.value = Math.max(objectNextOffset.value, result.offset + result.items.length)
  } catch (e: any) {
    if (!objectSearchViewDisposed && requestId === objectRequestId) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '加载更多对象失败')
    }
  } finally {
    if (!objectSearchViewDisposed && requestId === objectRequestId) objectLoadingMore.value = false
  }
}

async function selectObject(id: string) {
  selectedObjectId.value = id
  try {
    objectDetail.value = await api.getObject(sid, id)
  } catch (e: any) {
    objectDetail.value = null
    ElMessage.error(e?.response?.data?.detail || e?.message || '对象详情加载失败')
  }
}

function formatObjectValue(value: any): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// ── 选择 → 打开悬浮编辑器 ──
function onNodeSelect(node: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: node.id, kind: tab.value === 'instances' ? 'instance' : 'entity', label: node.label || node.name || node.id },
  }))
  if (!canWrite.value) return
  if (tab.value === 'instances') openInstance(node.id)
  else openEntity(node.id)
}
function onInstSelect(node: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: node.id, kind: node.id.startsWith('ent:') ? 'entity' : 'instance', label: node.label || node.name || node.id },
  }))
  if (node.id.startsWith('ent:')) {
    if (canWrite.value) openEntity(node.id.slice(4))
  }
  else {
    if (canWrite.value) openInstance(node.id)
    selectObject(node.id)
  }
}
function onEdgeClick(edge: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: edge.id, kind: tab.value === 'instances' ? 'relation-instance' : 'relation', label: edge.label || edge.id },
  }))
  if (canWrite.value && tab.value !== 'instances') openRelation(edge.id)
}
function onAddRelation(sourceId: string, targetId: string) {
  if (!canWrite.value) return
  openRelation()
  if (editor.value) {
    editor.value.form.source_entity_id = sourceId
    editor.value.form.target_entity_id = targetId
  }
}
function clearSelection() {
  editor.value = null
  selectedObjectId.value = null
  objectDetail.value = null
  window.dispatchEvent(new CustomEvent('ontology-selection-change', { detail: {} }))
}
function closeEditor() { editor.value = null }

// ── 打开编辑器 ──
function openEntity(id?: string) {
  if (!canWrite.value) return
  const e = id ? detail.value.entities.find((x) => x.id === id) : null
  editor.value = {
    kind: 'entity',
    id: e?.id,
    form: e
      ? { name: e.name, color: e.color, description: e.description, is_abstract: e.is_abstract, properties: e.properties.map((p) => ({ ...p })) }
      : { name: '', color: graphPalette[0], description: '', is_abstract: false, properties: [] },
  }
}
function openRelation(id?: string) {
  if (!canWrite.value) return
  const r = id ? detail.value.relations.find((x) => x.id === id) : null
  editor.value = {
    kind: 'relation',
    id: r?.id,
    form: r
      ? { name: r.name, namespace: r.namespace || '', source_entity_id: r.source_entity_id, target_entity_id: r.target_entity_id, relation_type: r.relation_type, description: r.description }
      : { name: '', namespace: '', source_entity_id: detail.value.entities[0]?.id || '', target_entity_id: detail.value.entities[1]?.id || '', relation_type: '1:N', description: '' },
  }
}
function openInstance(id?: string) {
  if (!canWrite.value) return
  const i = id ? detail.value.instances.find((x) => x.id === id) : null
  editor.value = {
    kind: 'instance',
    id: i?.id,
    form: i
      ? { entity_id: i.entity_id, name: i.name, attributes: { ...i.attributes } }
      : { entity_id: instFilter.value || detail.value.entities[0]?.id || '', name: '', attributes: {} },
  }
}

// ── 保存 / 删除 ──
async function saveEditor() {
  if (!canWrite.value || !editor.value) return
  const { kind, id, form } = editor.value
  saving.value = true
  try {
    if (kind === 'entity') {
      if (id) await api.updateEntity(id, form)
      else await api.createEntity(sid, form)
    } else if (kind === 'relation') {
      if (id) await api.updateRelation(id, form)
      else await api.createRelation(sid, form)
    } else {
      if (id) await api.updateInstance(id, form)
      else await api.createInstance(sid, form)
    }
    await load()
    if (id) {
      if (kind === 'entity') openEntity(id)
      else if (kind === 'relation') openRelation(id)
      else openInstance(id)
    } else {
      editor.value = null
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}
async function deleteEditor() {
  if (!canWrite.value || !editor.value) return
  const { kind, id } = editor.value
  if (!id) { editor.value = null; return }
  const names = { entity: '实体', relation: '关系', instance: '实例' }
  try {
    await ElMessageBox.confirm(`确定删除该${names[kind]}？`, '提示', { type: 'warning' })
  } catch { return }
  if (kind === 'entity') await api.deleteEntity(id)
  else if (kind === 'relation') await api.deleteRelation(id)
  else await api.deleteInstance(id)
  editor.value = null
  await load()
  ElMessage.success('已删除')
}

// ── 数据映射 ──
type MappingTransformOp = 'trim' | 'lower' | 'upper' | 'replace' | 'default' | 'to_string' | 'to_integer' | 'to_float' | 'to_boolean'
type MappingTransformRule = { op: MappingTransformOp; old?: string; new?: string; value?: any }
const mappingDlg = ref(false)
const mappingForm = ref<Partial<DataMapping> & { column_map: Record<string, string> }>({ column_map: {} })
const mappingTransformRules = ref<Record<string, MappingTransformRule[]>>({})
const mappingTransformOptions: Array<{ value: MappingTransformOp; label: string }> = [
  { value: 'trim', label: '去除首尾空格' },
  { value: 'lower', label: '转为小写' },
  { value: 'upper', label: '转为大写' },
  { value: 'replace', label: '替换文本' },
  { value: 'default', label: '设置空值默认值' },
  { value: 'to_string', label: '转为文本' },
  { value: 'to_integer', label: '转为整数' },
  { value: 'to_float', label: '转为小数' },
  { value: 'to_boolean', label: '转为布尔值' },
]
const mapTables = ref<string[]>([])
const mapCols = ref<string[]>([])
const mappingPreviewDlg = ref(false)
const mappingPreviewLoading = ref(false)
const mappingPreview = ref<DataMappingPreview | null>(null)
const mappingRefreshJobs = ref<Record<string, DataMappingRefreshJob>>({})
const mappingRefreshTimers = new Map<string, number>()
const mappingRefreshFailures = new Map<string, number>()
let mappingRefreshViewDisposed = false
let mappingTableRequest = 0
const mappingPreviewRows = computed(() => {
  const preview = mappingPreview.value
  if (!preview) return []
  return preview.sample_rows.map((row) => Object.fromEntries(
    preview.columns.map((column, index) => [column, formatMappingValue(row[index])]),
  ))
})
const mappingTransformedRows = computed<Record<string, string>[]>(() => {
  const rows = ((mappingPreview.value as DataMappingPreview & { transformed_rows?: Record<string, any>[] } | null)?.transformed_rows || [])
  return rows.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, formatMappingValue(value)])))
})
const mappingTransformedColumns = computed(() => Object.keys(mappingTransformedRows.value[0] || {}))

function mappingTransformHint(op: MappingTransformOp) {
  return ({
    trim: '删除文本首尾空格', lower: '按文本转为小写', upper: '按文本转为大写',
    to_string: '按文本保存', to_integer: '必须可转换为整数', to_float: '必须可转换为小数',
    to_boolean: '接受 true/false、yes/no、1/0、是/否',
  } as Partial<Record<MappingTransformOp, string>>)[op] || ''
}
function normalizeMappingTransform(rule: MappingTransformRule) {
  if (rule.op !== 'replace') { delete rule.old; delete rule.new }
  if (rule.op !== 'default') delete rule.value
  if (rule.op === 'replace') { rule.old ??= ''; rule.new ??= '' }
  if (rule.op === 'default') rule.value ??= ''
}
function addMappingTransform(propertyName: string) {
  const rules = mappingTransformRules.value[propertyName] || []
  if (rules.length >= 20) return
  mappingTransformRules.value[propertyName] = [...rules, { op: 'trim' }]
}
function removeMappingTransform(propertyName: string, index: number) {
  const rules = [...(mappingTransformRules.value[propertyName] || [])]
  rules.splice(index, 1)
  if (rules.length) mappingTransformRules.value[propertyName] = rules
  else delete mappingTransformRules.value[propertyName]
}
function onMappingEntityChange() {
  mappingForm.value.column_map = {}
  mappingTransformRules.value = {}
  void loadTables()
}
function serializedMappingTransformRules(): Record<string, MappingTransformRule[]> {
  const result: Record<string, MappingTransformRule[]> = {}
  for (const [propertyName, rules] of Object.entries(mappingTransformRules.value)) {
    if (!rules.length) continue
    result[propertyName] = rules.map((rule): MappingTransformRule => {
      if (rule.op === 'replace') {
        if (!String(rule.old || '')) throw new Error(`属性“${propertyName}”的替换规则必须填写原文本`)
        return { op: rule.op, old: String(rule.old), new: String(rule.new || '') }
      }
      if (rule.op === 'default') return { op: rule.op, value: rule.value }
      return { op: rule.op }
    })
  }
  return result
}

function mappingStatusLabel(status?: string) {
  return ({ unknown: '未检查', ready: '已通过', queued: '已排队', refreshing: '刷新中', retry_waiting: '等待重试', ok: '已刷新', error: '有错误' } as Record<string, string>)[status || 'unknown'] || '未检查'
}
function mappingStatusType(status?: string) {
  return ({ unknown: 'info', ready: 'success', queued: 'warning', refreshing: 'primary', retry_waiting: 'warning', ok: 'success', error: 'danger' } as Record<string, string>)[status || 'unknown'] || 'info'
}
function mappingRefreshJob(row: DataMapping): DataMappingRefreshJob | undefined {
  return row.id ? mappingRefreshJobs.value[row.id] : undefined
}
function mappingRefreshActive(row: DataMapping): boolean {
  return ['queued', 'running', 'retry_waiting'].includes(mappingRefreshJob(row)?.status || '')
}
function mappingJobLabel(status?: string): string {
  return ({ queued: '已排队', running: '刷新中', retry_waiting: '等待重试', succeeded: '已完成', failed: '失败', timed_out: '超时', cancelled: '已取消', tracking_unavailable: '状态暂不可查询' } as Record<string, string>)[status || ''] || '处理中'
}
function clearMappingRefreshTimer(mappingId: string) {
  const timer = mappingRefreshTimers.get(mappingId)
  if (timer !== undefined) window.clearTimeout(timer)
  mappingRefreshTimers.delete(mappingId)
}
function mappingRefreshIsCurrent(mappingId: string, jobId: string): boolean {
  return mappingRefreshJobs.value[mappingId]?.id === jobId
}
function clearMappingRefreshTracking(mappingId: string) {
  clearMappingRefreshTimer(mappingId)
  mappingRefreshFailures.delete(mappingId)
  if (mappingRefreshJobs.value[mappingId]) {
    const { [mappingId]: _removed, ...remaining } = mappingRefreshJobs.value
    mappingRefreshJobs.value = remaining
  }
}
function stopMappingRefreshPolling(mappingId: string, jobId: string, message: string) {
  if (!mappingRefreshIsCurrent(mappingId, jobId)) return
  clearMappingRefreshTimer(mappingId)
  mappingRefreshFailures.delete(mappingId)
  const job = mappingRefreshJobs.value[mappingId]
  if (job) {
    mappingRefreshJobs.value = {
      ...mappingRefreshJobs.value,
      // 后台任务仍是持久化的未知状态；不能把本地轮询失败误报成服务端失败。
      [mappingId]: { ...job, status: 'tracking_unavailable', error: message },
    }
  }
  ElMessage.error(message)
}
async function pollMappingRefresh(mappingId: string, jobId: string) {
  if (mappingRefreshViewDisposed || !mappingRefreshIsCurrent(mappingId, jobId)) return
  try {
    const job = await api.getMappingRefreshJob(jobId)
    if (mappingRefreshViewDisposed || !mappingRefreshIsCurrent(mappingId, jobId)) return
    mappingRefreshFailures.delete(mappingId)
    mappingRefreshJobs.value = { ...mappingRefreshJobs.value, [mappingId]: job }
    if (['queued', 'running', 'retry_waiting'].includes(job.status)) {
      clearMappingRefreshTimer(mappingId)
      mappingRefreshTimers.set(mappingId, window.setTimeout(() => { void pollMappingRefresh(mappingId, jobId) }, 900))
      return
    }
    clearMappingRefreshTimer(mappingId)
    mappingRefreshFailures.delete(mappingId)
    if (job.status === 'succeeded') {
      ElMessage.success(`刷新完成：扫描 ${job.rows_scanned} 行，新增 ${job.instances_created} 个对象，更新 ${job.instances_updated} 个对象`)
    } else {
      ElMessage.error(job.error || `映射刷新${mappingJobLabel(job.status)}`)
    }
    if (mappingRefreshViewDisposed || !mappingRefreshIsCurrent(mappingId, jobId)) return
    await load()
  } catch (e: any) {
    // 短暂网络抖动不应把持久化任务误报为失败；保留状态并继续轮询。
    if (mappingRefreshViewDisposed || !mappingRefreshIsCurrent(mappingId, jobId)) return
    const responseStatus = Number(e?.status || e?.response?.status || 0)
    const failureCount = (mappingRefreshFailures.get(mappingId) || 0) + 1
    if ([401, 403, 404].includes(responseStatus) || failureCount > 5) {
      const detail = e?.response?.data?.detail || e?.message
      stopMappingRefreshPolling(
        mappingId,
        jobId,
        detail || (responseStatus ? '无法继续读取映射刷新状态，请稍后重试' : '刷新状态暂时不可查询，请稍后再次点击刷新继续跟踪'),
      )
      return
    }
    mappingRefreshFailures.set(mappingId, failureCount)
    clearMappingRefreshTimer(mappingId)
    const retryDelay = Math.min(1500 * (2 ** (failureCount - 1)), 15000)
    mappingRefreshTimers.set(mappingId, window.setTimeout(() => { void pollMappingRefresh(mappingId, jobId) }, retryDelay))
  }
}
function mappingFieldLabel(status: string) {
  return ({ mapped: '已映射', missing: '未配置', invalid: '源列不存在' } as Record<string, string>)[status] || status
}
function mappingFieldType(status: string) {
  return ({ mapped: 'success', missing: 'warning', invalid: 'danger' } as Record<string, string>)[status] || 'info'
}
function formatMappingValue(value: any): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
function formatDate(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function openMapping(id?: string) {
  if (!canWrite.value) return
  const m = id ? detail.value.mappings.find((x) => x.id === id) : null
  mappingForm.value = m
    ? { ...m, data_source_binding_ref: { ...(m.data_source_binding_ref || {}) }, column_map: { ...(m.column_map || {}) } }
    : { entity_id: detail.value.entities[0]?.id, data_source_id: dataSources.value[0]?.id, data_source_binding_key: '', data_source_binding_ref: {}, table_name: '', column_map: {} }
  const savedRules = ((m as unknown as { transform_rules?: Record<string, MappingTransformRule[]> } | null)?.transform_rules || {})
  mappingTransformRules.value = Object.fromEntries(Object.entries(savedRules).map(([propertyName, rules]) => [
    propertyName,
    Array.isArray(rules) ? rules.map((rule) => ({ ...rule })) : [],
  ]))
  mappingDlg.value = true
  void loadTables()
}
async function loadTables() {
  const request = ++mappingTableRequest
  const sourceId = mappingForm.value.data_source_id || ''
  const entityId = mappingForm.value.entity_id || ''
  const tableName = mappingForm.value.table_name || ''
  mapTables.value = []; mapCols.value = []
  if (!sourceId || !mappingDlg.value) return
  try {
    const tables = await api.listTables(sourceId)
    if (
      mappingRefreshViewDisposed
      || request !== mappingTableRequest
      || !mappingDlg.value
      || mappingForm.value.data_source_id !== sourceId
      || (mappingForm.value.entity_id || '') !== entityId
      || (mappingForm.value.table_name || '') !== tableName
    ) return
    mapTables.value = tables.map((t) => t.name)
    const cur = tables.find((t) => t.name === tableName)
    if (cur) mapCols.value = cur.columns.map((c) => c.name)
  } catch { /* ignore */ }
}
function onMapDsChange() {
  mappingTableRequest += 1
  const tableWasEmpty = !mappingForm.value.table_name
  mappingForm.value.table_name = ''
  mapTables.value = []
  mapCols.value = []
  if (tableWasEmpty) void loadTables()
}
watch(() => mappingForm.value.table_name, () => {
  if (mappingDlg.value && mappingForm.value.data_source_id) void loadTables()
})
watch(mappingDlg, (open) => {
  if (open) return
  mappingTableRequest += 1
  mapTables.value = []
  mapCols.value = []
})
async function saveMapping() {
  if (!canWrite.value) return
  try {
    // 后端无独立更新接口：create 会替换同实体的旧映射
    const replacedMappingId = mappingForm.value.id
    const bindingKey = (mappingForm.value.data_source_binding_key || '').trim()
    const saved = await api.createMapping(sid, {
      ...mappingForm.value,
      transform_rules: serializedMappingTransformRules(),
      data_source_binding_key: bindingKey,
      // 表单只编辑键；清空键即明确解除旧的兼容描述，避免提交孤立 ref。
      data_source_binding_ref: bindingKey ? (mappingForm.value.data_source_binding_ref || {}) : {},
    })
    if (replacedMappingId && replacedMappingId !== saved.id) clearMappingRefreshTracking(replacedMappingId)
    mappingDlg.value = false
    await load()
    ElMessage.success('已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function doPreviewMapping(row: DataMapping) {
  if (!row.id) return
  mappingPreview.value = null
  mappingPreviewDlg.value = true
  mappingPreviewLoading.value = true
  try {
    mappingPreview.value = await api.previewMapping(row.id)
  } catch (e: any) {
    mappingPreviewDlg.value = false
    ElMessage.error(e?.response?.data?.detail || e?.message || '映射预览失败')
  } finally {
    mappingPreviewLoading.value = false
  }
}
async function doTestMapping(row: any) {
  if (!canWrite.value || !row.id) return
  row._testing = true
  try {
    const result = await api.testMapping(row.id)
    mappingPreview.value = result
    mappingPreviewDlg.value = true
    if (result.ok) ElMessage.success('映射测试通过')
    else ElMessage.warning('映射存在需要修正的问题')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '映射测试失败')
  } finally { row._testing = false }
}
async function doRefreshMapping(row: any) {
  if (!canWrite.value || !row.id || mappingRefreshActive(row)) return
  try {
    const job = await api.enqueueMappingRefresh(row.id)
    mappingRefreshJobs.value = { ...mappingRefreshJobs.value, [row.id]: job }
    clearMappingRefreshTimer(row.id)
    mappingRefreshFailures.delete(row.id)
    ElMessage.info(job.status === 'queued' ? '映射刷新已入队' : `映射刷新${mappingJobLabel(job.status)}`)
    await load()
    void pollMappingRefresh(row.id, job.id)
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '映射刷新失败') }
}
async function removeMapping(id: string) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm('确定删除该映射？', '提示', { type: 'warning' })
    await api.deleteMapping(id)
    clearMappingRefreshTracking(id)
    await load()
  } catch { /* ignore */ }
}

// ── 受治理函数（仅声明式契约，不执行）──
type FunctionForm = {
  id?: string
  name: string
  description: string
  tags_text: string
  visibility: 'scenario' | 'tenant'
  input_schema_text: string
  output_schema_text: string
}

const emptyFunctionSchema = (): Record<string, unknown> => ({
  type: 'object', properties: {}, additionalProperties: false,
})
const functionDlg = ref(false)
const functionSaving = ref(false)
const functionForm = ref<FunctionForm>({
  name: '', description: '', tags_text: '', visibility: 'scenario',
  input_schema_text: JSON.stringify(emptyFunctionSchema(), null, 2),
  output_schema_text: JSON.stringify(emptyFunctionSchema(), null, 2),
})

function formatFunctionSchema(schema?: Record<string, unknown>) {
  return JSON.stringify(schema || emptyFunctionSchema(), null, 2)
}
function openFunction(id?: string) {
  if (!canWrite.value) return
  const fn = id ? detail.value.functions.find((item) => item.id === id) : null
  functionForm.value = fn
    ? {
        id: fn.id,
        name: fn.name,
        description: fn.description || '',
        tags_text: (fn.tags || []).join(', '),
        visibility: fn.visibility === 'tenant' ? 'tenant' : 'scenario',
        input_schema_text: formatFunctionSchema(fn.input_schema),
        output_schema_text: formatFunctionSchema(fn.output_schema),
      }
    : {
        name: '', description: '', tags_text: '', visibility: 'scenario',
        input_schema_text: JSON.stringify(emptyFunctionSchema(), null, 2),
        output_schema_text: JSON.stringify(emptyFunctionSchema(), null, 2),
      }
  functionDlg.value = true
}
function parseFunctionSchema(text: string, label: string): Record<string, unknown> {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`${label}必须是有效 JSON`)
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${label}必须是 JSON Schema 对象`)
  }
  if ((parsed as Record<string, unknown>).type !== 'object') {
    throw new Error(`${label}顶层 type 必须为 object`)
  }
  return parsed as Record<string, unknown>
}
function parseFunctionTags(text: string): string[] {
  const tags = [...new Set(text.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))]
  if (tags.length > 20) throw new Error('函数标签不能超过 20 个')
  if (tags.some((tag) => tag.length > 80)) throw new Error('单个函数标签不能超过 80 个字符')
  return tags
}
async function saveFunction() {
  if (!canWrite.value || functionSaving.value) return
  const name = functionForm.value.name.trim()
  if (!name) { ElMessage.error('请填写函数名称'); return }
  let payload: FunctionDefinition
  try {
    payload = {
      name,
      description: functionForm.value.description.trim(),
      tags: parseFunctionTags(functionForm.value.tags_text),
      visibility: functionForm.value.visibility,
      input_schema: parseFunctionSchema(functionForm.value.input_schema_text, '输入 Schema'),
      output_schema: parseFunctionSchema(functionForm.value.output_schema_text, '输出 Schema'),
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '函数声明格式错误')
    return
  }
  functionSaving.value = true
  try {
    if (functionForm.value.id) await api.updateFunction(functionForm.value.id, payload)
    else await api.createFunction(sid, payload)
    functionDlg.value = false
    await load()
    ElMessage.success('函数声明已保存')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '函数声明保存失败')
  } finally {
    functionSaving.value = false
  }
}
async function removeFunction(id?: string) {
  if (!canWrite.value || !id) return
  try {
    await ElMessageBox.confirm('确定删除该函数声明？删除不会执行函数，但可能受已发布版本保护。', '删除函数声明', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await api.deleteFunction(id)
    await load()
    ElMessage.success('函数声明已删除')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '函数声明删除失败')
  }
}

// ── 操作（Actions）──
const actionDlg = ref(false)
const actionForm = ref<any>({ executor_config_text: '', input_schema_text: '' })
const actionExecuteDlg = ref(false)
const actionExecuteRow = ref<any>(null)
const actionParamsForm = ref<Record<string, any>>({})
const actionParamsText = ref<Record<string, string>>({})
const actionPreviewResult = ref<any>(null)
const actionPreviewParamsSnapshot = ref('')
const actionPreviewing = ref(false)
const actionExecuting = ref(false)
const actionIdempotencyKey = ref('')
const hasPinnedActionPreview = computed(() => {
  const preview = actionPreviewResult.value
  return Boolean(
    preview?.log_id
    && preview?.status === 'dry_run'
    && preview?.correlation_id
    && preview?.environment
    && preview?.definition_hash,
  )
})

function actionSchemaRoot(schema: any): { properties: Record<string, any>; required: string[] } {
  if (!schema || typeof schema !== 'object') return { properties: {}, required: [] }
  if (schema.properties && typeof schema.properties === 'object') {
    return { properties: schema.properties, required: Array.isArray(schema.required) ? schema.required : [] }
  }
  return { properties: schema, required: [] }
}
const actionParameterFields = computed(() => {
  const root = actionSchemaRoot(actionExecuteRow.value?.input_schema)
  return Object.entries(root.properties).map(([name, schema]: [string, any]) => ({
    name,
    schema: schema && typeof schema === 'object' ? schema : { type: 'string' },
    required: root.required.includes(name) || schema?.required === true,
    description: schema?.description || '',
  }))
})
function createIdempotencyKey() {
  const cryptoApi = globalThis.crypto as Crypto | undefined
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID()
  return `action-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}
function openAction(id?: string) {
  if (!canWrite.value) return
  const a = id ? detail.value.actions.find((x) => x.id === id) : null
  actionForm.value = a
    ? { ...a, executor_config_text: JSON.stringify(a.executor_config || {}, null, 2), input_schema_text: JSON.stringify(a.input_schema || {}, null, 2) }
    : {
        entity_id: detail.value.entities[0]?.id || '', name: '', description: '', executor_type: 'sql',
        executor_config_text: '', input_schema_text: '', enabled: true,
        requires_confirmation: true, idempotency_required: true, permission_scope: 'scenario',
      }
  actionDlg.value = true
}
async function saveAction() {
  if (!canWrite.value) return
  const f = { ...actionForm.value }
  try {
    f.executor_config = f.executor_config_text ? JSON.parse(f.executor_config_text) : {}
    f.input_schema = f.input_schema_text ? JSON.parse(f.input_schema_text) : {}
  } catch { ElMessage.error('JSON 格式错误'); return }
  delete f.executor_config_text; delete f.input_schema_text
  try {
    if (f.id) await api.updateAction(f.id, f)
    else await api.createAction(sid, f)
    actionDlg.value = false
    await load()
    ElMessage.success('已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function removeAction(id: string) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm('确定删除该操作？', '提示', { type: 'warning' })
    await api.deleteAction(id)
    await load()
  } catch { /* ignore */ }
}
async function doExecuteAction(row: any) {
  if (!canWrite.value) return
  actionExecuteRow.value = row
  actionParamsForm.value = {}
  actionParamsText.value = {}
  actionPreviewResult.value = null
  actionPreviewParamsSnapshot.value = ''
  actionIdempotencyKey.value = createIdempotencyKey()
  for (const field of actionParameterFields.value) {
    const schema = field.schema || {}
    const defaultValue = schema.default !== undefined
      ? schema.default
      : schema.type === 'boolean' ? false
        : schema.type === 'array' ? []
          : schema.type === 'object' ? {} : ''
    if (schema.type === 'array' || schema.type === 'object') actionParamsText.value[field.name] = JSON.stringify(defaultValue)
    else actionParamsForm.value[field.name] = defaultValue
  }
  actionExecuteDlg.value = true
}
function buildActionParams(): Record<string, any> {
  const params: Record<string, any> = {}
  for (const field of actionParameterFields.value) {
    const schema = field.schema || {}
    if (schema.type === 'array' || schema.type === 'object') {
      const text = actionParamsText.value[field.name]?.trim() || ''
      if (!text && !field.required) continue
      try { params[field.name] = JSON.parse(text) } catch { throw new Error(`参数 ${field.name} 必须是有效 JSON`) }
      continue
    }
    const value = actionParamsForm.value[field.name]
    if (value === '' || value === undefined || value === null) {
      if (field.required) throw new Error(`请填写必填参数：${field.name}`)
      continue
    }
    params[field.name] = value
  }
  return params
}
async function previewActionExecution() {
  if (!canWrite.value || !actionExecuteRow.value?.id) return
  actionPreviewing.value = true
  actionPreviewResult.value = null
  actionPreviewParamsSnapshot.value = ''
  try {
    const params = buildActionParams()
    const res = await api.executeAction(actionExecuteRow.value.id, { params, dry_run: true, confirm: false })
    actionPreviewResult.value = res
    actionPreviewParamsSnapshot.value = JSON.stringify(params)
    ElMessage.success('预演完成，未调用执行器')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '预演失败')
  } finally { actionPreviewing.value = false }
}
async function confirmActionExecution() {
  if (!canWrite.value || !actionExecuteRow.value?.id || !actionPreviewResult.value) return
  actionExecuting.value = true
  try {
    const params = buildActionParams()
    if (JSON.stringify(params) !== actionPreviewParamsSnapshot.value) {
      actionPreviewResult.value = null
      actionPreviewParamsSnapshot.value = ''
      ElMessage.warning('参数已变化，请用当前参数重新预演后再确认执行')
      return
    }
    if (!hasPinnedActionPreview.value) {
      actionPreviewResult.value = null
      actionPreviewParamsSnapshot.value = ''
      ElMessage.warning('预演缺少执行版本凭据，请重新预演后再确认执行')
      return
    }
    const pinnedPreview = actionPreviewResult.value
    const res = await api.executeAction(actionExecuteRow.value.id, {
      params,
      confirm: true,
      idempotency_key: actionIdempotencyKey.value,
      preview_log_id: String(pinnedPreview.log_id),
      correlation_id: String(pinnedPreview.correlation_id),
      expected_environment: pinnedPreview.environment,
      expected_definition_snapshot_id: pinnedPreview.definition_snapshot_id || undefined,
      expected_release_id: pinnedPreview.release_id || undefined,
      expected_definition_hash: String(pinnedPreview.definition_hash),
    })
    if (res.status === 'idempotent_replay') ElMessage.info('检测到相同幂等键，已返回原执行结果')
    else if (res.status === 'success') ElMessage.success('操作执行成功')
    else ElMessage.warning(res.error || '操作执行未成功')
    actionExecuteDlg.value = false
    showExecResult(res)
    await loadExecutionLogs(true)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { actionExecuting.value = false }
}

watch([actionParamsForm, actionParamsText], () => {
  if (!actionPreviewResult.value) return
  actionPreviewResult.value = null
  actionPreviewParamsSnapshot.value = ''
}, { deep: true })

// ── 规则（Rules）──
const ruleDlg = ref(false)
const ruleForm = ref<any>({ condition_text: '' })
function openRule(id?: string) {
  if (!canWrite.value) return
  const r = id ? detail.value.rules.find((x) => x.id === id) : null
  ruleForm.value = r
    ? { ...r, condition_text: JSON.stringify(r.condition || {}, null, 2) }
    : { name: '', description: '', entity_id: '', severity: 'warning', condition_text: '', action_on_match: '', enabled: true }
  ruleDlg.value = true
}
async function saveRule() {
  if (!canWrite.value) return
  const f = { ...ruleForm.value }
  try {
    f.condition = f.condition_text ? JSON.parse(f.condition_text) : {}
  } catch { ElMessage.error('条件 JSON 格式错误'); return }
  delete f.condition_text
  try {
    if (f.id) await api.updateRule(f.id, f)
    else await api.createRule(sid, f)
    ruleDlg.value = false
    await load()
    ElMessage.success('已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function removeRule(id: string) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm('确定删除该规则？', '提示', { type: 'warning' })
    await api.deleteRule(id)
    await load()
  } catch { /* ignore */ }
}
async function doEvalRule(row: any) {
  if (!canWrite.value) return
  try {
    const record = await promptParams(row.condition, '输入待评估的数据记录（JSON）')
    const res = await api.evaluateRule(row.id!, record)
    showExecResult(res)
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || e?.message || '评估失败')
  }
}
function condSummary(c: any): string {
  if (!c) return '—'
  if (c.op === 'and' || c.op === 'or') {
    return (c.conditions || []).map((x: any) => condSummary(x)).join(` ${c.op.toUpperCase()} `)
  }
  if (c.op === 'not') return `NOT(${condSummary(c.conditions?.[0])})`
  return `${c.field || '?'} ${c.op || ''} ${JSON.stringify(c.value ?? '')}`
}

// ── 事件（Events）──
const eventDlg = ref(false)
const eventForm = ref<any>({ payload_schema_text: '' })
const publishingEventId = ref<string | null>(null)
function openEvent(id?: string) {
  if (!canWrite.value) return
  const e = id ? detail.value.events.find((x) => x.id === id) : null
  eventForm.value = e
    ? { ...e, payload_schema_text: JSON.stringify(e.payload_schema || {}, null, 2) }
    : { name: '', description: '', trigger_source: '', payload_schema_text: '', enabled: true }
  eventDlg.value = true
}
async function saveEvent() {
  if (!canWrite.value) return
  const f = { ...eventForm.value }
  try {
    f.payload_schema = f.payload_schema_text ? JSON.parse(f.payload_schema_text) : {}
  } catch { ElMessage.error('JSON 格式错误'); return }
  delete f.payload_schema_text
  try {
    if (f.id) await api.updateEvent(f.id, f)
    else await api.createEvent(sid, f)
    eventDlg.value = false
    await load()
    ElMessage.success('已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function removeEvent(id: string) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm('确定删除该事件？', '提示', { type: 'warning' })
    await api.deleteEvent(id)
    await load()
  } catch { /* ignore */ }
}

// ── 工作流（Workflows）：可视化编排 ──
const wfEditor = ref<any>(null)
function openWorkflow(id?: string) {
  if (!canWrite.value) return
  const w = id ? detail.value.workflows.find((x) => x.id === id) : null
  wfEditor.value = w
    ? {
        ...w,
        trigger_config: { interval_seconds: 300, max_attempts: 3, retry_backoff_seconds: 5, timeout_seconds: 300, event_id: '', ...(w.trigger_config || {}) },
        steps: (w.steps || []).map((s: any) => ({ ...s })),
        nodes: (w.nodes || []).map((n: any) => ({ ...n, data: { ...(n.data || {}) } })),
        edges: (w.edges || []).map((e: any) => ({ ...e })),
      }
    : {
        name: '', description: '', trigger_type: 'manual',
        trigger_config: { interval_seconds: 300, max_attempts: 3, retry_backoff_seconds: 5, timeout_seconds: 300, event_id: '' },
        steps: [], nodes: [], edges: [], status: 'draft', enabled: true,
      }
}
async function saveWorkflow(w: any) {
  if (!canWrite.value) return
  try {
    if (w.id) await api.updateWorkflow(w.id, w)
    else await api.createWorkflow(sid, w)
    wfEditor.value = null
    await load()
    ElMessage.success('工作流已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function removeWorkflow(id: string) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm('确定删除该工作流？', '提示', { type: 'warning' })
    await api.deleteWorkflow(id)
    await load()
  } catch { /* ignore */ }
}
async function doExecuteWorkflow(row: any) {
  if (!canWrite.value) return
  if (row.status !== 'active') {
    ElMessage.warning('请先将工作流状态设为「启用」')
    return
  }
  row._executing = true
  try {
    const params = await promptParams(null, '输入工作流参数（JSON，可为空 {}）')
    const run = await api.submitWorkflowRun(row.id!, params)
    ElMessage.success(run.status === 'awaiting_approval' ? '任务已提交，正在等待审批' : '工作流任务已提交到队列')
    openWorkflowRun(run)
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { row._executing = false }
}
async function publishEvent(event: { id?: string; name?: string; enabled?: boolean }) {
  if (!canWrite.value || !event.id || event.enabled === false) return
  try {
    const { value } = await ElMessageBox.prompt(
      '输入事件载荷 JSON。发布后，订阅该事件的已启用工作流会异步进入任务队列。',
      `发布事件：${event.name || '未命名事件'}`,
      {
        inputType: 'textarea',
        inputValue: '{}',
        inputPlaceholder: '{"record_id": "..."}',
        inputValidator: (input: string) => {
          try {
            const parsed = JSON.parse(input || '{}')
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? true : '事件载荷必须是 JSON 对象'
          } catch {
            return '请输入有效的 JSON 对象'
          }
        },
        confirmButtonText: '发布事件',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    publishingEventId.value = event.id
    const envelope = await api.publishEvent(event.id, { payload: JSON.parse(value || '{}') })
    const count = envelope.queued_workflow_run_ids?.length || 0
    ElMessage.success(count ? `事件已发布，${count} 个工作流任务已进入队列` : '事件已发布；当前没有可触发的工作流')
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close' && e?.message !== 'cancel' && e?.message !== 'close') {
      ElMessage.error(e?.response?.data?.detail || e?.message || '事件发布失败')
    }
  } finally {
    publishingEventId.value = null
  }
}
function openWorkflowRun(run: WorkflowRun) {
  router.push({
    name: 'tasks',
    query: { scenario_id: sid, workflow_id: run.workflow_id, task: run.id, return_to: route.fullPath },
  })
}
function goToWorkflowTasks(row: any) {
  router.push({ name: 'tasks', query: { scenario_id: sid, workflow_id: row.id, return_to: route.fullPath } })
}
function workflowTriggerLabel(triggerType?: string) {
  return ({ manual: '手动', scheduled: '定时', event: '事件' } as Record<string, string>)[triggerType || 'manual'] || '手动'
}
function workflowTriggerDetail(workflow: any) {
  const config = workflow.trigger_config || {}
  if (workflow.trigger_type === 'scheduled' && config.interval_seconds) return `每 ${config.interval_seconds} 秒`
  if (workflow.trigger_type === 'event' && config.event_id) return detail.value.events.find((event) => event.id === config.event_id)?.name || '已配置事件'
  return ''
}
/** 列表页流程摘要：DAG 节点链（或旧版 steps） */
function wfSummary(w: any): string[] {
  const ns = w.nodes || []
  if (ns.length) {
    return ns
      .filter((n: any) => n.type !== 'start' && n.type !== 'end')
      .map((n: any) => {
        const d = n.data || {}
        if (n.type === 'action') return `操作 · ${d.action_id ? detail.value.actions.find((a) => a.id === d.action_id)?.name || n.name || '未命名' : n.name || '未命名'}`
        if (n.type === 'rule') return `规则 · ${d.rule_id ? detail.value.rules.find((r) => r.id === d.rule_id)?.name || n.name || '未命名' : n.name || '未命名'}`
        if (n.type === 'llm') return `大模型 · ${n.name || '未命名'}`
        if (n.type === 'event') return `事件 · ${d.event_id ? detail.value.events.find((e) => e.id === d.event_id)?.name || n.name || '未命名' : n.name || '未命名'}`
        if (n.type === 'approval') return `审批 · ${n.name || '等待人工决定'}`
        if (n.type === 'http') return `HTTP · ${n.name || '未命名'}`
        if (n.type === 'script') return `脚本 · ${n.name || '未命名'}`
        return n.name || n.type
      })
  }
  return (w.steps || []).map((s: any, i: number) => `${i + 1}.${stepLabel(s)}`)
}
function stepLabel(s: any): string {
  if (s.type === 'action') return `操作:${detail.value.actions.find((a) => a.id === s.action_id)?.name || '?'}`
  if (s.type === 'rule') return `规则:${detail.value.rules.find((r) => r.id === s.rule_id)?.name || '?'}`
  if (s.type === 'event') return `事件:${detail.value.events.find((e) => e.id === s.event_id)?.name || '?'}`
  return s.type || '?'
}

// ── 执行结果 ──
const execResultDlg = ref(false)
const execResultText = ref('')
function showExecResult(res: any) {
  execResultText.value = JSON.stringify(res, null, 2)
  execResultDlg.value = true
}
async function promptParams(schema: any, title = '输入参数（JSON）'): Promise<Record<string, any>> {
  const keys = schema ? Object.keys(schema) : []
  const template = keys.length
    ? JSON.stringify(Object.fromEntries(keys.map((k) => [k, ''])), null, 2)
    : '{}'
  const { value } = await ElMessageBox.prompt(title, '参数输入', {
    inputValue: template,
    inputPattern: /\S/,
    confirmButtonText: '确定',
    cancelButtonText: '取消',
  })
  const parsed = JSON.parse(value || '{}')
  if (typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('参数必须是 JSON 对象')
  return parsed
}

watch(canWrite, (allowed) => {
  if (allowed) return
  editor.value = null
  mappingDlg.value = false
  functionDlg.value = false
  actionDlg.value = false
  actionExecuteDlg.value = false
  ruleDlg.value = false
  eventDlg.value = false
  wfEditor.value = null
})

// ── 加载 ──
async function load() {
  scenarioLoading.value = true
  scenarioAccessDenied.value = false
  scenarioLoadError.value = ''
  try {
    detail.value = await api.getScenario(sid)
    if (!detail.value.actions.some((action) => action.id === controlledActionId.value && action.enabled !== false)) {
      controlledActionId.value = detail.value.actions.find((action) => action.enabled !== false)?.id || ''
    }
  } catch (e: any) {
    if (Number(e?.status || e?.response?.status) === 403) {
      scenarioAccessDenied.value = true
    } else {
      scenarioLoadError.value = e?.response?.data?.detail || e?.message || '请稍后重试。'
    }
    return
  } finally {
    scenarioLoading.value = false
  }
  try {
    const [sources, configs] = await Promise.all([api.listDataSources(), api.listLLM()])
    dataSources.value = sources
    llmConfigs.value = configs
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '场景关联资源加载失败')
  }
  await Promise.all([searchObjects(), loadExecutionLogs(true)])
}
function goBack() { router.push('/scenarios') }
function onAssistantApplied(event: Event) {
  const detail = (event as CustomEvent<{ scenario_id?: string }>).detail || {}
  if (!detail.scenario_id || detail.scenario_id === sid) load()
}
function requestedActionId() {
  const value = route.query.action_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function consumeAssistantActionPreviewState(): AssistantActionPreview | undefined {
  const currentState = window.history.state && typeof window.history.state === 'object'
    ? { ...window.history.state }
    : {}
  const preview = currentState.assistant_action_preview
  if (!preview || typeof preview !== 'object') return undefined
  delete currentState.assistant_action_preview
  window.history.replaceState(currentState, '')
  return preview as AssistantActionPreview
}
function assistantPreviewIsPinned(preview: AssistantActionPreview | undefined, actionId: string) {
  const result = preview?.preview
  return Boolean(
    preview?.target?.id === actionId
    && preview.parameters
    && typeof preview.parameters === 'object'
    && result?.log_id
    && result?.status === 'dry_run'
    && result?.correlation_id
    && result?.environment
    && result?.definition_hash,
  )
}
async function openGovernedActionById(actionId: string, assistantPreview?: AssistantActionPreview) {
  if (!actionId || !canWrite.value) return
  const action = detail.value.actions.find((item) => item.id === actionId)
  if (!action) {
    ElMessage.warning('该 Action 不在当前可编辑场景中，可能已被移除或更换版本')
    return
  }
  tab.value = 'actions'
  await nextTick()
  await doExecuteAction(action)
  if (!assistantPreviewIsPinned(assistantPreview, actionId)) return
  const parameters = assistantPreview?.parameters || {}
  for (const field of actionParameterFields.value) {
    if (!Object.prototype.hasOwnProperty.call(parameters, field.name)) continue
    const value = parameters[field.name]
    if (field.schema?.type === 'array' || field.schema?.type === 'object') {
      actionParamsText.value[field.name] = JSON.stringify(value)
    } else {
      actionParamsForm.value[field.name] = value
    }
  }
  // Let the deep parameter watcher finish before restoring the immutable dry-run.
  await nextTick()
  try {
    const normalized = buildActionParams()
    actionPreviewResult.value = { ...(assistantPreview?.preview || {}) }
    actionPreviewParamsSnapshot.value = JSON.stringify(normalized)
    ElMessage.success('已载入助手的只读预演；请核对参数和版本后确认')
  } catch (error: any) {
    actionPreviewResult.value = null
    actionPreviewParamsSnapshot.value = ''
    ElMessage.warning(error?.message || '助手预演参数已失效，请重新预演')
  }
}
function onOpenGovernedAction(event: Event) {
  if (scenarioLoading.value) return
  const eventDetail = (event as CustomEvent<{ action_id?: string; preview?: AssistantActionPreview }>).detail || {}
  const actionId = String(eventDetail.action_id || '')
  consumeAssistantActionPreviewState()
  void openGovernedActionById(actionId, eventDetail.preview)
}
function workflowStatusLabel(status?: string) {
  return status === 'active' ? '启用' : status === 'disabled' ? '停用' : '草稿'
}
function workflowStatusType(status?: string) {
  return status === 'active' ? 'success' : status === 'disabled' ? 'info' : 'warning'
}
onMounted(async () => {
  mappingRefreshViewDisposed = false
  objectSearchViewDisposed = false
  if (route.query.stage !== tab.value) void router.replace({ query: { ...route.query, stage: tab.value } })
  await load()
  await openGovernedActionById(requestedActionId(), consumeAssistantActionPreviewState())
  window.addEventListener('assistant-proposal-applied', onAssistantApplied)
  window.addEventListener('open-governed-action', onOpenGovernedAction)
})
onBeforeUnmount(() => {
  mappingRefreshViewDisposed = true
  mappingTableRequest += 1
  objectSearchViewDisposed = true
  objectRequestId += 1
  objectPendingKey = ''
  window.removeEventListener('assistant-proposal-applied', onAssistantApplied)
  window.removeEventListener('open-governed-action', onOpenGovernedAction)
  for (const timer of mappingRefreshTimers.values()) window.clearTimeout(timer)
  mappingRefreshTimers.clear()
  mappingRefreshFailures.clear()
  window.dispatchEvent(new CustomEvent('ontology-selection-change', { detail: {} }))
})
</script>

<style scoped>
.sd-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  padding: 20px 24px 24px;
  animation: pageIn 0.32s var(--ease);
}
.ph-left {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}
.back-btn {
  flex-shrink: 0;
}
.ph-title {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.ph-title h1 {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -0.02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ph-sub {
  font-size: 12.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ph-right {
  flex-shrink: 0;
}
.ai-btn {
  background: var(--grad);
  border: none;
  color: #fff;
  font-weight: 600;
  box-shadow: var(--shadow-primary);
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.ai-btn:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-md), var(--shadow-primary);
  color: #fff;
}

/* ── 闭环工作台：单一纵向滚动面，不在卡片内制造更多滚动区 ── */
.flow-workbench {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  padding: 2px 1px 22px;
}
.flow-hero,
.flow-next,
.flow-section {
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  box-shadow: var(--shadow-xs);
}
.flow-hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 28px;
  padding: 20px 22px;
  border-radius: 18px;
  background:
    linear-gradient(115deg, color-mix(in srgb, var(--primary-soft) 72%, var(--surface)), var(--surface) 62%),
    var(--surface);
}
.flow-hero h2 { margin: 5px 0 6px; color: var(--text); font-size: 22px; letter-spacing: -.035em; text-wrap: balance; }
.flow-hero p { max-width: 760px; margin: 0; color: var(--text-2); font-size: 12.5px; line-height: 1.65; }
.flow-progress { flex: 0 0 220px; }
.flow-progress > span { display: block; margin-bottom: 7px; color: var(--text-3); font-size: 11px; text-align: right; }
.flow-progress b { color: var(--primary-600); font-size: 17px; }
.flow-step-list {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.flow-step-list li { min-width: 0; }
.flow-step-list button {
  position: relative;
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  grid-template-rows: auto auto;
  gap: 8px 9px;
  width: 100%;
  min-height: 122px;
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: 14px;
  color: var(--text);
  background: var(--surface);
  box-shadow: var(--shadow-xs);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: border-color var(--dur) var(--ease), transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.flow-step-list button:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.flow-step-list button:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 34%, transparent); outline-offset: 2px; }
.flow-step-list li.is-current button { border-color: color-mix(in srgb, var(--primary) 52%, var(--border)); background: linear-gradient(145deg, var(--primary-soft), var(--surface) 58%); }
.flow-step-list li.is-complete button { border-color: color-mix(in srgb, var(--success) 30%, var(--border)); }
.flow-step-index {
  display: inline-flex;
  width: 32px;
  height: 32px;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  color: var(--text-2);
  background: var(--surface-2);
  font-size: 11px;
  font-weight: 850;
}
.is-current .flow-step-index { border-color: var(--primary); color: #fff; background: var(--primary); }
.is-complete .flow-step-index { border-color: var(--success); color: #fff; background: var(--success); }
.flow-step-copy { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.flow-step-copy strong { font-size: 12px; line-height: 1.4; }
.flow-step-copy small { color: var(--text-3); font-size: 10.5px; line-height: 1.5; }
.flow-step-list .el-tag { grid-column: 2; justify-self: start; }
.flow-step-arrow { position: absolute; right: 10px; bottom: 11px; color: var(--text-3); }
.flow-next {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 15px;
}
.flow-next-mark { display: inline-flex; width: 44px; height: 44px; align-items: center; justify-content: center; border-radius: 13px; color: var(--primary-600); background: var(--primary-soft); font-size: 20px; }
.flow-next > div:nth-child(2) { min-width: 0; }
.flow-next span { color: var(--primary-600); font-size: 9px; font-weight: 850; letter-spacing: .12em; }
.flow-next h3 { margin: 2px 0; color: var(--text); font-size: 14px; }
.flow-next p { margin: 0; color: var(--text-3); font-size: 11px; line-height: 1.5; }
.flow-main-grid { display: grid; grid-template-columns: minmax(0, 1.22fr) minmax(360px, .78fr); gap: 14px; align-items: start; }
.flow-section { min-width: 0; padding: 17px; border-radius: 17px; }
.flow-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
.flow-section-head > div:first-child { display: flex; min-width: 0; align-items: flex-start; gap: 10px; }
.section-number { flex: 0 0 auto; color: var(--primary-600); font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 850; letter-spacing: .08em; }
.flow-section-head h3 { margin: 0 0 4px; color: var(--text); font-size: 15px; letter-spacing: -.02em; }
.flow-section-head p { margin: 0; color: var(--text-3); font-size: 11px; line-height: 1.55; }
.object-question-box { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
.query-suggestions { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.query-suggestions > span { color: var(--text-3); font-size: 10px; }
.query-suggestions button { min-height: 30px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 7px; color: var(--text-2); background: var(--surface-2); cursor: pointer; font: inherit; font-size: 10.5px; }
.query-suggestions button:hover, .query-suggestions button:focus-visible { border-color: var(--primary); color: var(--primary-600); outline: none; }
.query-answer { margin-top: 13px; padding: 11px 12px; border: 1px solid color-mix(in srgb, var(--primary) 24%, var(--border)); border-radius: 11px; background: var(--primary-soft); }
.query-answer-head { display: flex; align-items: center; gap: 6px; color: var(--primary-600); font-size: 11px; }
.query-answer p { margin: 6px 0 3px; color: var(--text-2); font-size: 12px; line-height: 1.65; }
.query-answer small { color: var(--text-3); font-size: 10px; }
.evidence-list { display: grid; gap: 8px; margin-top: 10px; }
.evidence-card { padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }
.evidence-card header { display: flex; align-items: center; gap: 8px; }
.evidence-id { flex: 0 0 auto; color: var(--primary-600); font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; }
.evidence-card header > div { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 2px; }
.evidence-card header strong { overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.evidence-card header small { color: var(--text-3); font-size: 10px; }
.evidence-card dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin: 9px 0 0; }
.evidence-card dl > div { min-width: 0; padding: 7px; border-radius: 7px; background: var(--surface); }
.evidence-card dt { color: var(--text-3); font-size: 9px; }
.evidence-card dd { margin: 3px 0 0; overflow-wrap: anywhere; color: var(--text-2); font-size: 10.5px; line-height: 1.45; }
.evidence-attributes { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.evidence-attributes span { display: inline-flex; gap: 5px; padding: 3px 7px; border: 1px solid var(--border); border-radius: 6px; color: var(--text-2); background: var(--surface); font-size: 10px; }
.evidence-attributes b { color: var(--text-3); font-weight: 600; }
.controlled-action-box { margin-top: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }
.controlled-action-box > label { display: block; margin-bottom: 6px; color: var(--text-2); font-size: 11px; font-weight: 750; }
.controlled-action-meta { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 8px; }
.controlled-action-meta span { display: flex; min-width: 0; flex-direction: column; gap: 2px; padding: 7px; border-radius: 7px; color: var(--text-2); background: var(--surface); font-size: 10px; }
.controlled-action-meta b { color: var(--text-3); font-size: 9px; }
.controlled-action-buttons { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.execution-empty { padding: 18px; text-align: center; }
.execution-empty p { color: var(--text-3); font-size: 11px; line-height: 1.6; }
.approval-workflows { margin-top: 15px; }
.subsection-title { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; color: var(--text-2); font-size: 11px; font-weight: 800; }
.subsection-title b { color: var(--primary-600); }
.approval-workflow-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 7px; padding: 8px 0; border-top: 1px solid var(--border); }
.approval-workflow-row > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.approval-workflow-row strong { overflow: hidden; color: var(--text-2); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.approval-workflow-row small { color: var(--text-3); font-size: 9.5px; }
.audit-list { display: grid; gap: 8px; }
.audit-entry { overflow: hidden; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }
.audit-entry summary { display: grid; grid-template-columns: 9px minmax(0, 1fr) auto auto 16px; align-items: center; gap: 9px; min-height: 56px; padding: 8px 11px; cursor: pointer; list-style: none; }
.audit-entry summary::-webkit-details-marker { display: none; }
.audit-entry summary:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 34%, transparent); outline-offset: -3px; }
.audit-status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warning); }
.audit-status-dot.is-success { background: var(--success); }
.audit-status-dot.is-danger { background: var(--danger); }
.audit-summary-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.audit-summary-copy strong { overflow: hidden; color: var(--text); font-size: 11.5px; text-overflow: ellipsis; white-space: nowrap; }
.audit-summary-copy small, .audit-duration { color: var(--text-3); font-size: 9.5px; }
.audit-chevron { color: var(--text-3); transition: transform var(--dur) var(--ease); }
.audit-entry[open] .audit-chevron { transform: rotate(180deg); }
.audit-detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; padding: 11px; border-top: 1px solid var(--border); }
.audit-detail-grid > div { min-width: 0; padding: 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); }
.audit-detail-grid span { display: block; color: var(--text-3); font-size: 9px; }
.audit-detail-grid b { display: block; margin-top: 3px; overflow-wrap: anywhere; color: var(--text-2); font-size: 10px; }
.audit-connectors { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 0 11px 10px; color: var(--text-3); font-size: 10px; }
.audit-payload-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 0 11px 11px; }
.audit-payload-grid section { min-width: 0; }
.audit-payload-grid h4 { margin: 0 0 5px; color: var(--text-3); font-size: 9px; }
.audit-payload-grid pre { margin: 0; padding: 9px; overflow-x: auto; border-radius: 7px; color: #dce7e9; background: #1d2930; font: 10px/1.55 'Cascadia Code', Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }

/* ── Tabs ── */
.sd-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.sd-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}
.sd-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
  background: var(--border);
}
.sd-tabs :deep(.el-tabs__item) {
  font-size: 14px;
  font-weight: 600;
}
.sd-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.sd-tabs :deep(.el-tab-pane) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* ── 工具栏 ── */
.tab-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.tab-stats {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.stat {
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 5px 12px;
  border-radius: 999px;
}
.stat b {
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
}
.tab-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.inst-filter {
  width: 150px;
}

/* ── 图谱舞台（自适应高度，悬浮编辑器覆盖其上）── */
.graph-stage {
  position: relative;
  flex: 1;
  min-height: 440px;
}
.graph-stage :deep(.graph-wrap) {
  height: 100%;
}

/* ── 对象运行时浏览器 ── */
.instance-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.instance-workspace .graph-stage {
  min-width: 0;
}
.object-explorer {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  box-shadow: var(--shadow-sm);
}
.explorer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
}
.explorer-head h3 {
  margin: 3px 0 0;
  color: var(--text);
  font-size: 15px;
  letter-spacing: -.02em;
}
.eyebrow {
  color: var(--primary-600);
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .13em;
}
.explorer-tools {
  padding: 10px 12px 8px;
}
.explorer-hint {
  display: block;
  margin: 6px 2px 0;
  color: var(--text-3);
  font-size: 11px;
}
.object-list {
  flex: 1 1 42%;
  min-height: 120px;
  overflow: auto;
  padding: 0 8px 8px;
}
.object-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}
.object-pagination-note {
  min-width: 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.45;
}
.object-pagination .el-button { flex: 0 0 auto; }
.object-row,
.relation-row {
  width: 100%;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  text-align: left;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.object-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 54px;
  padding: 8px 8px;
}
.object-row:hover,
.relation-row:hover {
  border-color: var(--border-strong);
  background: var(--surface-2);
}
.object-row:focus-visible,
.relation-row:focus-visible {
  border-color: var(--primary);
  background: var(--surface-2);
  outline: 2px solid color-mix(in srgb, var(--primary) 55%, transparent);
  outline-offset: 2px;
}
.object-row.active {
  border-color: color-mix(in srgb, var(--primary) 38%, var(--border));
  background: var(--primary-soft);
}
.object-dot,
.object-detail-dot {
  flex: 0 0 auto;
  border-radius: 50%;
}
.object-dot {
  width: 9px;
  height: 9px;
}
.object-row-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.object-row-main strong,
.object-row-main small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.object-row-main strong { font-size: 12.5px; }
.object-row-main small { color: var(--text-3); font-size: 11px; }
.object-arrow { color: var(--text-3); font-size: 14px; }
.object-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 90px;
  padding: 12px;
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
}
.object-detail {
  flex: 1 1 58%;
  min-height: 0;
  overflow: auto;
  border-top: 1px solid var(--border);
  padding: 13px 12px 16px;
}
.object-detail-head,
.object-title-wrap,
.object-meta-line {
  display: flex;
  align-items: center;
}
.object-detail-head { justify-content: space-between; gap: 8px; }
.object-title-wrap { min-width: 0; gap: 9px; }
.object-detail-dot { width: 11px; height: 11px; }
.object-title-wrap div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.object-title-wrap strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.object-title-wrap small { color: var(--text-3); font-size: 11px; }
.object-meta-line { flex-wrap: wrap; gap: 7px; margin: 10px 0 12px; }
.runtime-badge {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 7px;
  color: var(--text-2);
  font-size: 10px;
}
.runtime-badge.is-imported { border-color: color-mix(in srgb, var(--success) 36%, var(--border)); color: var(--success); }
.object-detail-section { margin-top: 14px; }
.detail-section-title {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-bottom: 7px;
  color: var(--text-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.detail-section-title span { color: var(--text-3); font-family: 'JetBrains Mono', monospace; font-weight: 500; }
.attribute-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.attribute-item,
.provenance-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
  padding: 7px 8px;
}
.attribute-item span,
.provenance-card small { color: var(--text-3); font-size: 10px; }
.attribute-item strong,
.provenance-card span { overflow: hidden; color: var(--text-2); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.provenance-card { gap: 5px; }
.relation-row {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  padding: 6px 7px;
  font-size: 11px;
}
.relation-row > span:nth-child(2) { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.relation-row strong { color: var(--primary-600); }
.relation-direction {
  min-width: 18px;
  border-radius: 4px;
  background: var(--primary-soft);
  color: var(--primary-600);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  text-align: center;
}

/* ── 工作流可视化编排舞台 ── */
.wf-editor-stage {
  position: relative;
  flex: 1;
  min-height: 480px;
  display: flex;
  flex-direction: column;
}

/* ── 数据映射 ── */
.map-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.map-table {
  flex: 1;
}
.workflow-trigger {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.workflow-trigger small { color: var(--text-3); font-size: 10px; white-space: nowrap; }
.ent-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 600;
  font-size: 13px;
}
.ent-chip i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}
.map-dst {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12.5px;
  color: var(--text);
}
.muted {
  font-size: 12px;
  color: var(--text-3);
}
.col-maps {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.col-map {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px;
  background: var(--primary-soft);
  border: 1px solid var(--border);
  padding: 2px 8px;
  border-radius: 6px;
  color: var(--text-2);
}

/* ── 映射对话框 ── */
.mapping-status-cell {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.mapping-status-cell small { flex-basis: 100%; }
.mapping-error-icon {
  color: var(--danger);
  cursor: help;
  font-size: 15px;
}
.mapping-preview {
  min-height: 220px;
}
.mapping-preview-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.mapping-preview-head h3 {
  margin: 4px 0 5px;
  color: var(--text);
  font-size: 17px;
  letter-spacing: -.02em;
}
.mapping-preview-head h3 span { color: var(--text-3); font-weight: 400; }
.mapping-preview-head p { margin: 0; color: var(--text-3); font-size: 12px; }
.mapping-alert { margin-bottom: 10px; }
.mapping-issue-list {
  margin: 0;
  padding-left: 18px;
  line-height: 1.65;
}
.mapping-preview-grid {
  display: grid;
  grid-template-columns: minmax(250px, .85fr) minmax(360px, 1.15fr);
  gap: 14px;
  margin-top: 14px;
}
.mapping-coverage,
.mapping-samples,
.mapping-transformed {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-2);
}
.preview-section-title {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-2);
  font-size: 12px;
  font-weight: 800;
}
.mapping-coverage :deep(.el-table),
.mapping-samples :deep(.el-table),
.mapping-transformed :deep(.el-table) { background: transparent; }
.mapping-coverage :deep(.el-table__inner-wrapper::before),
.mapping-samples :deep(.el-table__inner-wrapper::before),
.mapping-transformed :deep(.el-table__inner-wrapper::before) { display: none; }
.mapping-transformed { margin-top: 14px; }
.field-flag { margin-left: 5px; transform: scale(.86); transform-origin: left center; }
.unmapped-columns {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
  color: var(--text-3);
  font-size: 11px;
}
.colmap-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.mapping-property {
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
}
.colmap-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.colmap-attr {
  width: 110px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  flex-shrink: 0;
}
.colmap-row .el-select {
  flex: 1;
}
.transform-rule-list { display: grid; gap: 6px; margin: 8px 0 0 120px; padding-top: 8px; border-top: 1px dashed var(--border); }
.transform-rule-row { display: grid; grid-template-columns: 22px minmax(150px, .8fr) minmax(120px, 1fr) minmax(120px, 1fr) 32px; align-items: center; gap: 7px; }
.transform-rule-row > .el-select { grid-column: 2; }
.transform-rule-hint { grid-column: 3 / 5; color: var(--text-3); font-size: 11px; line-height: 1.45; }
.transform-order { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; background: var(--primary-soft); color: var(--primary-600); font-size: 10px; font-weight: 750; }

/* ── AI 预览 ── */
.ai-preview {
  margin-top: 14px;
  padding: 14px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 12px;
}
.ai-sec {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 10px;
}
.ai-ent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.ai-ent {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  box-shadow: var(--shadow-xs);
}
.ai-ent-head {
  display: flex;
  align-items: center;
  gap: 7px;
  font-weight: 700;
  font-size: 13px;
  margin-bottom: 8px;
}
.ai-ent-head i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}
.ai-props {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.ai-prop {
  font-size: 11px;
  background: var(--primary-soft);
  padding: 2px 7px;
  border-radius: 5px;
  color: var(--text-2);
}
.ai-prop em {
  font-style: normal;
  color: var(--text-3);
  margin-left: 4px;
}
.ai-rels {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ai-rel {
  font-size: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 4px 10px;
  border-radius: 8px;
  color: var(--text-2);
}

/* ── 操作/规则/事件/工作流 ── */
.form-row {
  display: flex;
  gap: 16px;
}
.form-col {
  flex: 1;
  min-width: 0;
}
.mono {
  font-family: 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
.form-help,
.action-param-hint {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.45;
}
.function-declaration-note {
  margin: 0 0 12px;
}
.function-form {
  margin-top: 16px;
}
.function-name-cell {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.function-name-cell .muted {
  overflow-wrap: anywhere;
}
.function-schema {
  max-width: 340px;
  max-height: 116px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
  color: var(--text-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
.function-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.action-params-form {
  margin-top: 18px;
  max-height: 360px;
  overflow: auto;
  padding-right: 4px;
}
.action-execution-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
  padding: 9px 11px;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 9px;
  font-size: 12px;
}
.action-execution-meta code {
  color: var(--primary-600);
  word-break: break-all;
}
.action-preview-alert {
  margin-top: 14px;
}
.action-runtime-audit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin-top: 10px;
  color: var(--text-2);
  font-size: 12px;
}
.action-runtime-provenance {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 10px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-2);
  font-size: 12px;
}
.action-runtime-provenance div {
  min-width: 0;
}
.action-runtime-provenance dt {
  color: var(--text-3);
}
.action-runtime-provenance dd {
  margin: 3px 0 0;
  color: var(--text-1);
  overflow-wrap: anywhere;
}
.action-preview-text {
  max-height: 150px;
  overflow: auto;
  margin: 7px 0 0;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
}
.cond-text {
  word-break: break-all;
  color: var(--text-2);
}
.wf-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.wf-step {
  font-size: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 3px 8px;
  border-radius: 6px;
  color: var(--text-2);
}
.exec-result {
  background: #1d2930;
  color: #e2e8f0;
  padding: 14px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.6;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 640px) {
  .action-runtime-provenance {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* ── 响应式 ── */
@media (max-width: 900px) {
  .ph-title h1 { font-size: 17px; }
  .graph-stage { min-height: 380px; }
}
@media (max-width: 640px) {
  .tab-toolbar { flex-direction: column; align-items: stretch; }
  .tab-actions { justify-content: flex-end; }
  .inst-filter { width: 100%; }
}

/* ── 科技白场景工作区：轻玻璃、蓝青高光、明确的操作层级 ── */
.sd-page {
  position: relative;
  padding: 24px 28px 30px;
  background:
    radial-gradient(620px 280px at 92% 0%, rgba(71, 157, 229, .10), transparent 68%),
    radial-gradient(560px 260px at 2% 42%, rgba(41, 190, 177, .07), transparent 70%);
}
.scenario-state {
  align-items: center;
  justify-content: center;
}
.scenario-state :deep(.el-result) {
  max-width: 560px;
}
.sd-page::before {
  content: '';
  position: absolute;
  top: 0;
  right: 28px;
  width: 34%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(57, 157, 221, .45), transparent);
  pointer-events: none;
}
.ph-left { gap: 16px; }
.back-btn {
  border-color: var(--border);
  background: color-mix(in srgb, var(--surface) 78%, transparent);
  color: var(--text-2);
  box-shadow: var(--shadow-xs);
}
.back-btn:hover {
  border-color: var(--border-strong);
  color: var(--primary-600);
  background: var(--primary-soft);
}
.ph-title h1 { margin: 0; color: var(--text); letter-spacing: -.04em; }
.ph-sub { max-width: 620px; margin-top: 3px; }
.ai-btn {
  border-radius: 11px;
  background: var(--grad);
  box-shadow: var(--shadow-primary);
}
.sd-tabs :deep(.el-tabs__header) {
  margin: 4px 0 16px;
  padding: 4px 8px 0;
  background: color-mix(in srgb, var(--surface) 76%, transparent);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-xs);
}
.sd-tabs :deep(.el-tabs__nav-wrap::after) { background: transparent; }
.sd-tabs :deep(.el-tabs__item) {
  height: 40px;
  color: var(--text-2);
  font-size: 13px;
  font-weight: 700;
}
.sd-tabs :deep(.el-tabs__item.is-active) { color: var(--primary-600); }
.sd-tabs :deep(.el-tabs__active-bar) {
  height: 3px;
  border-radius: 3px;
  background: var(--grad);
}
.tab-toolbar {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: color-mix(in srgb, var(--surface) 78%, transparent);
  box-shadow: var(--shadow-xs);
}
.stat {
  background: var(--surface-2);
  border-color: var(--border);
  color: var(--text-2);
}
.stat b { color: var(--primary-600); }
.stat-runtime { border-color: color-mix(in srgb, var(--primary) 28%, var(--border)); }
.graph-stage {
  min-height: 500px;
  height: clamp(500px, 62vh, 700px);
  flex: 0 0 auto;
}
.map-card {
  border-radius: 18px;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  box-shadow: var(--shadow-sm);
}
.inst-filter :deep(.el-input__wrapper) { background: var(--surface); }

@media (max-width: 900px) {
  .sd-page { padding: 18px 16px 22px; }
  .graph-stage { min-height: 440px; height: 58vh; }
  .instance-workspace { grid-template-columns: 1fr; overflow: auto; }
  .instance-workspace .graph-stage { min-height: 420px; height: 52vh; }
  .object-explorer { min-height: 540px; }
  .mapping-preview-grid { grid-template-columns: 1fr; }
}

/* ── 页面滚动归外层 main-area；闭环卡片不再各自抢占滚轮 ── */
.sd-page {
  height: auto;
  min-height: 100%;
  overflow: visible;
  box-sizing: border-box;
  padding: 14px 20px 16px;
}
.sd-header {
  flex: 0 0 auto;
  min-height: 48px;
  margin-bottom: 6px;
  align-items: center;
}
.sd-header .ph-title h1 { font-size: 18px; }
.sd-tabs {
  flex: 0 0 auto;
  min-height: 0;
  overflow: visible;
}
.sd-tabs :deep(.el-tabs__header) {
  flex: 0 0 auto;
  margin: 0 0 8px;
  padding-top: 2px;
}
.sd-tabs :deep(.el-tabs__nav-wrap) { min-width: 0; }
.sd-tabs :deep(.el-tabs__content) { overflow: visible; }
.sd-tabs :deep(.el-tab-pane) { overflow: visible; }
.tab-toolbar {
  flex: 0 0 auto;
  margin-bottom: 8px;
  padding: 8px 10px;
}
.graph-stage {
  flex: 0 0 auto;
  min-height: 500px;
  height: clamp(500px, 66vh, 760px);
  overflow: hidden;
}
.wf-editor-stage {
  flex: 0 0 auto;
  min-height: 560px;
  height: clamp(560px, 72vh, 820px);
  overflow: hidden;
}
.map-card {
  flex: 0 0 auto;
  min-height: 420px;
  overflow-x: auto;
  overflow-y: visible;
}

@media (max-width: 900px) {
  .sd-page { padding: 12px 14px 14px; }
  .sd-header { min-height: 44px; }
  .sd-header .ph-title h1 { font-size: 17px; }
  .graph-stage { min-height: 440px; height: 58vh; }
  .wf-editor-stage { min-height: 520px; height: 68vh; }
}

@media (max-width: 1180px) {
  .flow-step-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .flow-main-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .sd-header :deep(.el-button),
  .flow-workbench :deep(.el-button),
  .query-suggestions button { min-height: 44px; }
  .sd-tabs :deep(.el-tabs__item) { height: 44px; }
  .flow-hero { align-items: stretch; flex-direction: column; gap: 14px; }
  .flow-progress { flex-basis: auto; }
  .flow-progress > span { text-align: left; }
  .flow-step-list { grid-template-columns: 1fr; }
  .flow-step-list button { min-height: 92px; }
  .flow-next { grid-template-columns: 40px minmax(0, 1fr); }
  .flow-next > .el-button { grid-column: 1 / -1; width: 100%; }
  .object-question-box { grid-template-columns: 1fr; }
  .evidence-card dl, .controlled-action-meta, .audit-detail-grid, .audit-payload-grid { grid-template-columns: 1fr; }
  .audit-entry summary { grid-template-columns: 9px minmax(0, 1fr) auto 16px; }
  .audit-duration { display: none; }
  .colmap-row { align-items: stretch; flex-direction: column; }
  .colmap-attr { width: auto; }
  .transform-rule-list { margin-left: 0; }
  .transform-rule-row { grid-template-columns: 22px minmax(0, 1fr) 40px; }
  .transform-rule-row > .el-select { grid-column: 2; }
  .transform-rule-row > .el-input, .transform-rule-hint { grid-column: 2 / 4; }
  .transform-rule-row > .el-button { grid-column: 3; grid-row: 1; }
}
</style>
