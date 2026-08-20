<template>
  <div class="sd-page">
    <div class="page-header sd-header">
      <div class="ph-left">
        <el-button class="back-btn" @click="goBack"><el-icon><ArrowLeft /></el-icon> 返回</el-button>
        <div class="ph-title">
          <b>{{ detail.name || '场景详情' }}</b>
          <span class="ph-sub">{{ detail.description }}</span>
        </div>
      </div>
      <div class="ph-right">
        <el-button class="ai-btn" @click="runGenerate"><el-icon><MagicStick /></el-icon> AI 生成本体</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="sd-tabs">
      <!-- ═══════════ 本体 ═══════════ -->
      <el-tab-pane label="本体" name="ontology">
        <div class="tab-toolbar">
          <div class="tab-stats">
            <span class="stat">实体 <b>{{ detail.entities.length }}</b></span>
            <span class="stat">关系 <b>{{ detail.relations.length }}</b></span>
          </div>
          <div class="tab-actions">
            <el-button size="small" @click="openEntity()"><el-icon><Plus /></el-icon> 实体</el-button>
            <el-button size="small" @click="openRelation()"><el-icon><Plus /></el-icon> 关系</el-button>
          </div>
        </div>
        <div class="graph-stage">
          <GraphCanvas
            :data="schemaGraph"
            mode="schema"
            :legend="legend"
            empty-text="暂无本体，点击「实体」创建，或用 AI 生成"
            @select="onNodeSelect"
            @edge-click="onEdgeClick"
            @add-relation="onAddRelation"
            @canvas-click="clearSelection"
          />
          <EditorPanel
            v-if="editor"
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
      <el-tab-pane label="实例" name="instances">
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
            <el-button size="small" type="primary" @click="openInstance()"><el-icon><Plus /></el-icon> 添加实例</el-button>
          </div>
        </div>
        <div class="instance-workspace">
          <div class="graph-stage">
            <GraphCanvas
              :data="instanceGraph"
              mode="instance"
              :legend="legend"
              empty-text="暂无实例，点击「添加实例」创建"
              @select="onInstSelect"
              @canvas-click="clearSelection"
            />
            <EditorPanel
              v-if="editor"
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
              <span class="explorer-hint">回车搜索 · {{ objectTotal }} 个结果</span>
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
            <div v-if="objectDetail" class="object-detail" aria-live="polite">
              <div class="object-detail-head">
                <div class="object-title-wrap">
                  <span class="object-detail-dot" :style="{ background: objectDetail.entity_color || 'var(--primary)' }"></span>
                  <div>
                    <strong>{{ objectDetail.name }}</strong>
                    <small>{{ objectDetail.entity_name }}</small>
                  </div>
                </div>
                <el-button size="small" text type="primary" @click="openInstance(objectDetail.id)">编辑</el-button>
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
          <div class="tab-actions">
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
                  <small v-if="row.last_refreshed_at" class="muted">{{ formatDate(row.last_refreshed_at) }}</small>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="340" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text @click="openMapping(row.id)">编辑</el-button>
                <el-button size="small" text @click="doPreviewMapping(row)">预览</el-button>
                <el-button size="small" text :loading="row._testing" @click="doTestMapping(row)">测试</el-button>
                <el-button size="small" text type="primary" :loading="row._refreshing" @click="doRefreshMapping(row)">刷新实例</el-button>
                <el-button size="small" text type="danger" @click="removeMapping(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 操作（Actions）═══════════ -->
      <el-tab-pane label="操作" name="actions">
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">操作 <b>{{ detail.actions.length }}</b></span></div>
          <div class="tab-actions">
            <el-button size="small" type="primary" @click="openAction()"><el-icon><Plus /></el-icon> 添加操作</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.actions" class="map-table" empty-text="暂无操作，点击「添加操作」创建">
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
            <el-table-column label="操作" width="245" fixed="right">
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
          <div class="tab-actions">
            <el-button size="small" type="primary" @click="openRule()"><el-icon><Plus /></el-icon> 添加规则</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.rules" class="map-table" empty-text="暂无规则，点击「添加规则」创建">
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
            <el-table-column label="操作" width="200" fixed="right">
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
          <div class="tab-actions">
            <el-button size="small" type="primary" @click="openEvent()"><el-icon><Plus /></el-icon> 添加事件</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="detail.events" class="map-table" empty-text="暂无事件，点击「添加事件」创建">
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
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
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
        <div v-if="wfEditor" class="wf-editor-stage">
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
          />
        </div>
        <!-- 工作流列表 -->
        <template v-else>
          <div class="tab-toolbar">
            <div class="tab-stats"><span class="stat">工作流 <b>{{ detail.workflows.length }}</b></span></div>
            <div class="tab-actions">
              <el-button size="small" type="primary" @click="openWorkflow()"><el-icon><Plus /></el-icon> 新建工作流</el-button>
            </div>
          </div>
          <div class="card map-card">
            <el-table :data="detail.workflows" class="map-table" empty-text="暂无工作流，点击「新建工作流」开始可视化编排">
              <el-table-column label="名称" min-width="140">
                <template #default="{ row }"><b>{{ row.name }}</b></template>
              </el-table-column>
              <el-table-column label="触发方式" width="110">
                <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.trigger_type || 'manual' }}</el-tag></template>
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
              <el-table-column label="操作" width="200" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text @click="openWorkflow(row.id)">编排</el-button>
                  <el-button size="small" text type="primary" :disabled="row.status !== 'active'" :loading="row._executing" @click="doExecuteWorkflow(row)">执行</el-button>
                  <el-button size="small" text type="danger" @click="removeWorkflow(row.id)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- ═══════════ 数据映射对话框 ═══════════ -->
    <el-dialog v-model="mappingDlg" title="数据映射" width="600px" class="glass-dialog">
      <el-form label-position="top">
        <el-form-item label="目标实体">
          <el-select v-model="mappingForm.entity_id" style="width:100%">
            <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="数据源">
          <el-select v-model="mappingForm.data_source_id" style="width:100%" @change="onMapDsChange">
            <el-option v-for="d in dataSources" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="表名">
          <el-select v-model="mappingForm.table_name" style="width:100%" filterable allow-create placeholder="选择或输入表名">
            <el-option v-for="t in mapTables" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="列映射（实体属性 ← 数据列）">
          <div class="colmap-list">
            <div class="colmap-row" v-for="p in (detail.entities.find((e) => e.id === mappingForm.entity_id)?.properties || [])" :key="p.name">
              <span class="colmap-attr">{{ p.name }}</span>
              <el-select v-model="mappingForm.column_map[p.name]" size="small" clearable placeholder="—">
                <el-option v-for="c in mapCols" :key="c" :label="c" :value="c" />
              </el-select>
            </div>
            <div class="muted" v-if="!mapCols.length">选择表后自动加载列</div>
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
        <div v-if="mappingPreview.unmapped_columns.length" class="unmapped-columns">
          未映射源列：<span v-for="column in mappingPreview.unmapped_columns" :key="column" class="col-map">{{ column }}</span>
        </div>
      </div>
      <el-empty v-else description="暂无预览数据" />
      <template #footer>
        <el-button @click="mappingPreviewDlg = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 操作对话框 ═══════════ -->
    <el-dialog v-model="actionDlg" :title="actionForm.id ? '编辑操作' : '添加操作'" width="640px" class="glass-dialog">
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
              <el-option label="技能 (Skill)" value="skill" />
              <el-option label="MCP 工具" value="mcp" />
              <el-option label="HTTP 请求" value="http" />
              <el-option label="Python 脚本" value="script" />
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
    <el-dialog v-model="actionExecuteDlg" :title="`执行操作：${actionExecuteRow?.name || ''}`" width="640px" class="glass-dialog">
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
      <template #footer>
        <el-button @click="actionExecuteDlg = false">取消</el-button>
        <el-button :loading="actionPreviewing" @click="previewActionExecution">预演</el-button>
        <el-button type="primary" :loading="actionExecuting" :disabled="!actionPreviewResult" @click="confirmActionExecution">确认执行</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 规则对话框 ═══════════ -->
    <el-dialog v-model="ruleDlg" :title="ruleForm.id ? '编辑规则' : '添加规则'" width="640px" class="glass-dialog">
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
    <el-dialog v-model="eventDlg" :title="eventForm.id ? '编辑事件' : '添加事件'" width="560px" class="glass-dialog">
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

    <!-- ═══════════ AI 生成对话框 ═══════════ -->
    <el-dialog v-model="aiDlg" title="AI 生成本体" width="640px" class="glass-dialog">
      <el-input v-model="aiDesc" type="textarea" :rows="3" placeholder="描述你的业务场景，AI 将自动设计实体、属性与关系…" />
      <div class="ai-preview" v-if="aiResult">
        <div class="ai-sec">
          <span>实体（{{ aiResult.entities.length }}）</span>
          <el-button size="small" text type="primary" @click="applyAI">应用到场景</el-button>
        </div>
        <div class="ai-ent-grid">
          <div class="ai-ent" v-for="e in aiResult.entities" :key="e.name">
            <div class="ai-ent-head"><i :style="{ background: e.color }"></i>{{ e.name }}</div>
            <div class="ai-props">
              <span class="ai-prop" v-for="p in e.properties" :key="p.name">{{ p.name }}<em>{{ p.data_type }}</em></span>
            </div>
          </div>
        </div>
        <div class="ai-sec" v-if="aiResult.relations.length">关系（{{ aiResult.relations.length }}）</div>
        <div class="ai-rels">
          <span class="ai-rel" v-for="(r, i) in aiResult.relations" :key="i">{{ r.source }} —{{ r.name }}→ {{ r.target }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="aiDlg = false">关闭</el-button>
        <el-button type="primary" :loading="aiLoading" @click="runGenerate">生成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import GraphCanvas from '@/components/GraphCanvas.vue'
import EditorPanel from '@/components/EditorPanel.vue'
import WorkflowEditor from '@/components/workflow/WorkflowEditor.vue'
import type { ScenarioDetail, GraphData, GraphNode, GraphEdge, Entity, DataMapping, DataMappingPreview, ObjectDetail, ObjectSearchItem } from '@/types'

const route = useRoute()
const router = useRouter()
const sid = route.params.id as string

const detail = ref<ScenarioDetail>({
  id: sid, name: '', description: '',
  entities: [], relations: [], data_sources: [],
  instances: [], relation_instances: [], mappings: [],
  actions: [], rules: [], events: [], workflows: [],
})
const dataSources = ref<any[]>([])
const llmConfigs = ref<any[]>([])
const tab = ref('ontology')
const instFilter = ref('')
const saving = ref(false)
const objectQuery = ref('')
const objectItems = ref<ObjectSearchItem[]>([])
const objectTotal = ref(0)
const objectLoading = ref(false)
const selectedObjectId = ref<string | null>(null)
const objectDetail = ref<ObjectDetail | null>(null)

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

function entName(id: string) { return detail.value.entities.find((e) => e.id === id)?.name || '—' }
function entColor(id: string) {
  const index = detail.value.entities.findIndex((e) => e.id === id)
  return visualColor(detail.value.entities[index]?.color, Math.max(0, index))
}
function dsName(id: string) { return dataSources.value.find((d) => d.id === id)?.name || '—' }

// ── 对象运行时浏览 ──
async function searchObjects() {
  objectLoading.value = true
  try {
    const result = await api.searchObjects(sid, {
      q: objectQuery.value.trim() || undefined,
      entity_id: instFilter.value || undefined,
      limit: 50,
    })
    objectItems.value = result.items
    objectTotal.value = result.total
    if (selectedObjectId.value && !result.items.some((item) => item.id === selectedObjectId.value)) {
      selectedObjectId.value = null
      objectDetail.value = null
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '对象列表加载失败')
  } finally {
    objectLoading.value = false
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
  if (tab.value === 'instances') openInstance(node.id)
  else openEntity(node.id)
}
function onInstSelect(node: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: node.id, kind: node.id.startsWith('ent:') ? 'entity' : 'instance', label: node.label || node.name || node.id },
  }))
  if (node.id.startsWith('ent:')) openEntity(node.id.slice(4))
  else {
    openInstance(node.id)
    selectObject(node.id)
  }
}
function onEdgeClick(edge: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: edge.id, kind: tab.value === 'instances' ? 'relation-instance' : 'relation', label: edge.label || edge.id },
  }))
  if (tab.value !== 'instances') openRelation(edge.id)
}
function onAddRelation(sourceId: string, targetId: string) {
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
  const r = id ? detail.value.relations.find((x) => x.id === id) : null
  editor.value = {
    kind: 'relation',
    id: r?.id,
    form: r
      ? { name: r.name, source_entity_id: r.source_entity_id, target_entity_id: r.target_entity_id, relation_type: r.relation_type, description: r.description }
      : { name: '', source_entity_id: detail.value.entities[0]?.id || '', target_entity_id: detail.value.entities[1]?.id || '', relation_type: '1:N', description: '' },
  }
}
function openInstance(id?: string) {
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
  if (!editor.value) return
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
  if (!editor.value) return
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
const mappingDlg = ref(false)
const mappingForm = ref<Partial<DataMapping> & { column_map: Record<string, string> }>({ column_map: {} })
const mapTables = ref<string[]>([])
const mapCols = ref<string[]>([])
const mappingPreviewDlg = ref(false)
const mappingPreviewLoading = ref(false)
const mappingPreview = ref<DataMappingPreview | null>(null)
const mappingPreviewRows = computed(() => {
  const preview = mappingPreview.value
  if (!preview) return []
  return preview.sample_rows.map((row) => Object.fromEntries(
    preview.columns.map((column, index) => [column, formatMappingValue(row[index])]),
  ))
})

function mappingStatusLabel(status?: string) {
  return ({ unknown: '未检查', ready: '已通过', ok: '已刷新', error: '有错误' } as Record<string, string>)[status || 'unknown'] || '未检查'
}
function mappingStatusType(status?: string) {
  return ({ unknown: 'info', ready: 'success', ok: 'success', error: 'danger' } as Record<string, string>)[status || 'unknown'] || 'info'
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
  const m = id ? detail.value.mappings.find((x) => x.id === id) : null
  mappingForm.value = m
    ? { ...m, column_map: { ...(m.column_map || {}) } }
    : { entity_id: detail.value.entities[0]?.id, data_source_id: dataSources.value[0]?.id, table_name: '', column_map: {} }
  mappingDlg.value = true
  loadTables()
}
async function loadTables() {
  mapTables.value = []; mapCols.value = []
  if (!mappingForm.value.data_source_id) return
  try {
    const tables = await api.listTables(mappingForm.value.data_source_id)
    mapTables.value = tables.map((t) => t.name)
    const cur = tables.find((t) => t.name === mappingForm.value.table_name)
    if (cur) mapCols.value = cur.columns.map((c) => c.name)
  } catch { /* ignore */ }
}
function onMapDsChange() { mappingForm.value.table_name = ''; mapTables.value = []; mapCols.value = [] }
watch(() => mappingForm.value.table_name, () => {
  if (mappingForm.value.data_source_id) loadTables()
})
async function saveMapping() {
  try {
    // 后端无独立更新接口：create 会替换同实体的旧映射
    await api.createMapping(sid, mappingForm.value)
    mappingDlg.value = false
    await load()
    ElMessage.success('已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
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
  if (!row.id) return
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
  if (!row.id) return
  row._refreshing = true
  try {
    const result = await api.refreshMapping(row.id)
    if (!result.ok) {
      ElMessage.error(result.message || result.last_error || '映射刷新失败')
      await load()
      return
    }
    ElMessage.success(`刷新完成：扫描 ${result.rows_scanned} 行，新增 ${result.instances_created} 个对象`)
    await load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '映射刷新失败') }
  finally { row._refreshing = false }
}
async function removeMapping(id: string) {
  try {
    await ElMessageBox.confirm('确定删除该映射？', '提示', { type: 'warning' })
    await api.deleteMapping(id)
    await load()
  } catch { /* ignore */ }
}

// ── 操作（Actions）──
const actionDlg = ref(false)
const actionForm = ref<any>({ executor_config_text: '', input_schema_text: '' })
const actionExecuteDlg = ref(false)
const actionExecuteRow = ref<any>(null)
const actionParamsForm = ref<Record<string, any>>({})
const actionParamsText = ref<Record<string, string>>({})
const actionPreviewResult = ref<any>(null)
const actionPreviewing = ref(false)
const actionExecuting = ref(false)
const actionIdempotencyKey = ref('')

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
  try {
    await ElMessageBox.confirm('确定删除该操作？', '提示', { type: 'warning' })
    await api.deleteAction(id)
    await load()
  } catch { /* ignore */ }
}
async function doExecuteAction(row: any) {
  actionExecuteRow.value = row
  actionParamsForm.value = {}
  actionParamsText.value = {}
  actionPreviewResult.value = null
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
  if (!actionExecuteRow.value?.id) return
  actionPreviewing.value = true
  try {
    const params = buildActionParams()
    const res = await api.executeAction(actionExecuteRow.value.id, { params, dry_run: true, confirm: false })
    actionPreviewResult.value = res
    ElMessage.success('预演完成，未调用执行器')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '预演失败')
  } finally { actionPreviewing.value = false }
}
async function confirmActionExecution() {
  if (!actionExecuteRow.value?.id) return
  actionExecuting.value = true
  try {
    const params = buildActionParams()
    const res = await api.executeAction(actionExecuteRow.value.id, {
      params,
      confirm: true,
      idempotency_key: actionIdempotencyKey.value,
    })
    if (res.status === 'idempotent_replay') ElMessage.info('检测到相同幂等键，已返回原执行结果')
    else if (res.status === 'success') ElMessage.success('操作执行成功')
    else ElMessage.warning(res.error || '操作执行未成功')
    actionExecuteDlg.value = false
    showExecResult(res)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { actionExecuting.value = false }
}

// ── 规则（Rules）──
const ruleDlg = ref(false)
const ruleForm = ref<any>({ condition_text: '' })
function openRule(id?: string) {
  const r = id ? detail.value.rules.find((x) => x.id === id) : null
  ruleForm.value = r
    ? { ...r, condition_text: JSON.stringify(r.condition || {}, null, 2) }
    : { name: '', description: '', entity_id: '', severity: 'warning', condition_text: '', action_on_match: '', enabled: true }
  ruleDlg.value = true
}
async function saveRule() {
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
  try {
    await ElMessageBox.confirm('确定删除该规则？', '提示', { type: 'warning' })
    await api.deleteRule(id)
    await load()
  } catch { /* ignore */ }
}
async function doEvalRule(row: any) {
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
function openEvent(id?: string) {
  const e = id ? detail.value.events.find((x) => x.id === id) : null
  eventForm.value = e
    ? { ...e, payload_schema_text: JSON.stringify(e.payload_schema || {}, null, 2) }
    : { name: '', description: '', trigger_source: '', payload_schema_text: '', enabled: true }
  eventDlg.value = true
}
async function saveEvent() {
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
  try {
    await ElMessageBox.confirm('确定删除该事件？', '提示', { type: 'warning' })
    await api.deleteEvent(id)
    await load()
  } catch { /* ignore */ }
}

// ── 工作流（Workflows）：可视化编排 ──
const wfEditor = ref<any>(null)
function openWorkflow(id?: string) {
  const w = id ? detail.value.workflows.find((x) => x.id === id) : null
  wfEditor.value = w
    ? {
        ...w,
        steps: (w.steps || []).map((s: any) => ({ ...s })),
        nodes: (w.nodes || []).map((n: any) => ({ ...n, data: { ...(n.data || {}) } })),
        edges: (w.edges || []).map((e: any) => ({ ...e })),
      }
    : { name: '', description: '', trigger_type: 'manual', steps: [], nodes: [], edges: [], status: 'draft', enabled: true }
}
async function saveWorkflow(w: any) {
  try {
    if (w.id) await api.updateWorkflow(w.id, w)
    else await api.createWorkflow(sid, w)
    wfEditor.value = null
    await load()
    ElMessage.success('工作流已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败') }
}
async function removeWorkflow(id: string) {
  try {
    await ElMessageBox.confirm('确定删除该工作流？', '提示', { type: 'warning' })
    await api.deleteWorkflow(id)
    await load()
  } catch { /* ignore */ }
}
async function doExecuteWorkflow(row: any) {
  if (row.status !== 'active') {
    ElMessage.warning('请先将工作流状态设为「启用」')
    return
  }
  row._executing = true
  try {
    const params = await promptParams(null, '输入工作流参数（JSON，可为空 {}）')
    const res = await api.executeWorkflow(row.id!, params)
    showExecResult(res)
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { row._executing = false }
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

// ── AI 生成 ──
const aiDlg = ref(false)
const aiDesc = ref('')
const aiLoading = ref(false)
const aiResult = ref<any>(null)
async function runGenerate() {
  aiDlg.value = true
  if (aiResult.value) return
  aiLoading.value = true
  try {
    aiResult.value = await api.generateOntology(sid, aiDesc.value || detail.value.description || '')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '生成失败') }
  finally { aiLoading.value = false }
}
async function applyAI() {
  if (!aiResult.value) return
  try {
    await api.applyOntology(sid, aiResult.value)
    aiDlg.value = false
    aiResult.value = null
    await load()
    ElMessage.success('已应用 AI 本体')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '应用失败') }
}

// ── 加载 ──
async function load() {
  try {
    detail.value = await api.getScenario(sid)
    dataSources.value = await api.listDataSources()
    llmConfigs.value = await api.listLLM()
    await searchObjects()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  }
}
function goBack() { router.push('/scenarios') }
function onAssistantApplied(event: Event) {
  const detail = (event as CustomEvent<{ scenario_id?: string }>).detail || {}
  if (!detail.scenario_id || detail.scenario_id === sid) load()
}
function workflowStatusLabel(status?: string) {
  return status === 'active' ? '启用' : status === 'disabled' ? '停用' : '草稿'
}
function workflowStatusType(status?: string) {
  return status === 'active' ? 'success' : status === 'disabled' ? 'info' : 'warning'
}
onMounted(() => {
  load()
  window.addEventListener('assistant-proposal-applied', onAssistantApplied)
})
onBeforeUnmount(() => {
  window.removeEventListener('assistant-proposal-applied', onAssistantApplied)
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
.ph-title b {
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
.mapping-samples {
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
.mapping-samples :deep(.el-table) { background: transparent; }
.mapping-coverage :deep(.el-table__inner-wrapper::before),
.mapping-samples :deep(.el-table__inner-wrapper::before) { display: none; }
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

/* ── 响应式 ── */
@media (max-width: 900px) {
  .ph-title b { font-size: 17px; }
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
.ph-title b { color: var(--text); letter-spacing: -.04em; }
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

/* ── 工作区高度约束：头部固定，画布/表格只在内容区内伸缩 ── */
.sd-page {
  height: calc(100dvh - 68px);
  min-height: 0;
  overflow: hidden;
  box-sizing: border-box;
  padding: 14px 20px 16px;
}
.sd-header {
  flex: 0 0 auto;
  min-height: 48px;
  margin-bottom: 6px;
  align-items: center;
}
.sd-header .ph-title b { font-size: 18px; }
.sd-tabs {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.sd-tabs :deep(.el-tabs__header) {
  flex: 0 0 auto;
  margin: 0 0 8px;
  padding-top: 2px;
}
.sd-tabs :deep(.el-tabs__nav-wrap) { min-width: 0; }
.sd-tabs :deep(.el-tabs__content) { overflow: hidden; }
.sd-tabs :deep(.el-tab-pane) { overflow: hidden; }
.tab-toolbar {
  flex: 0 0 auto;
  margin-bottom: 8px;
  padding: 8px 10px;
}
.graph-stage {
  flex: 1 1 auto;
  min-height: 0;
  height: auto;
  overflow: hidden;
}
.wf-editor-stage {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.map-card {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}

@media (max-width: 900px) {
  .sd-page { padding: 12px 14px 14px; }
  .sd-header { min-height: 44px; }
  .sd-header .ph-title b { font-size: 17px; }
  .graph-stage { min-height: 0; height: auto; }
  .wf-editor-stage { min-height: 0; }
}
</style>
