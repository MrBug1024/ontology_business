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
        <el-tag v-if="detail.status === 'retired'" type="info" effect="plain">已退役</el-tag>
        <el-tag v-else-if="!canWrite" type="info" effect="plain" aria-label="当前场景为只读访问">只读访问</el-tag>
      </div>
    </div>

    <el-alert
      v-if="detail.status === 'retired'"
      class="retired-notice"
      type="info"
      :closable="false"
      title="该场景已退役，定义、目录资产和运行审计保留为只读。"
    />

    <el-tabs v-model="tab" class="sd-tabs">
      <!-- ═══════════ 本体 ═══════════ -->
      <el-tab-pane label="本体模型" name="ontology" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats">
            <span class="stat">正式对象类型 <b>{{ detail.entities.length }}</b></span>
            <span class="stat">候选对象类型 <b>{{ scenarioDraftsOf('entity').length }}</b></span>
            <span class="stat">正式关系类型 <b>{{ detail.relations.length }}</b></span>
            <span class="stat">候选关系类型 <b>{{ scenarioDraftsOf('relation').length }}</b></span>
          </div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" @click="openEntity()"><el-icon><Plus /></el-icon> 对象类型</el-button>
            <el-button size="small" @click="openRelation()"><el-icon><Plus /></el-icon> 关系类型</el-button>
          </div>
        </div>
        <div class="graph-stage">
          <GraphCanvas
            :data="schemaGraph"
            mode="schema"
            :legend="legend"
            :empty-text="canWrite ? '暂无本体，点击「对象类型」创建，或用 AI 生成' : '暂无本体'"
            @select="onNodeSelect"
            @edge-click="onEdgeClick"
            @add-relation="onAddRelation"
            @canvas-click="clearSelection"
          />
          <EditorPanel
            v-if="editor && canWrite"
            :editor="editor"
            :entities="detail.entities"
            :relations="detail.relations"
            :saving="saving"
            :focus-property-index="draftPropertyEditorIndex"
            @save="saveEditor"
            @delete="deleteEditor"
            @close="closeEditor"
          />
        </div>
      </el-tab-pane>

      <!-- ═══════════ 实例 ═══════════ -->
      <el-tab-pane label="实例数据" name="instances" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats">
            <span class="stat">正式对象实例 <b>{{ detail.runtime_instance_count || objectTotal }}</b></span>
            <span class="stat">候选对象实例 <b>{{ scenarioDraftsOf('instance').length }}</b></span>
            <span class="stat">关系实例 <b>{{ relationInstanceTotalIsExact ? relationInstanceTotal : `约 ${relationInstanceTotal}` }}</b></span>
            <span class="stat stat-runtime">运行时对象 <b>{{ objectTotal }}</b></span>
          </div>
          <div class="tab-actions">
            <el-select v-model="instFilter" placeholder="全部对象类型" clearable size="small" class="inst-filter">
              <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
            </el-select>
            <el-button v-if="canWrite" size="small" plain @click="openRelationInstanceManager"><el-icon><Connection /></el-icon> 管理关系实例</el-button>
            <el-button v-if="canWrite" size="small" type="primary" @click="openInstance()"><el-icon><Plus /></el-icon> 添加对象实例</el-button>
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
              @edge-click="openRelationInstanceManager"
              @canvas-click="clearSelection"
            />
            <EditorPanel
              v-if="editor && canWrite"
              :editor="editor"
              :entities="detail.entities"
              :relations="detail.relations"
              :saving="saving"
              :focus-property-index="draftPropertyEditorIndex"
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
                v-memo="[item.id, selectedObjectId === item.id]"
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
                    <StructuredValueCell :value="value" />
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
      <el-tab-pane label="数据映射" name="mappings" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats mapping-stats">
            <span class="stat">正式对象映射 <b>{{ detail.mappings.length }}</b></span>
            <span class="stat">候选对象映射 <b>{{ scenarioDraftsOf('mapping', 'data_mapping', 'conceptual_mapping').length }}</b></span>
            <span class="stat">正式关系映射 <b>{{ detail.relation_mappings.length }}</b></span>
            <span class="stat">候选关系映射 <b>{{ scenarioDraftsOf('relation_mapping').length }}</b></span>
          </div>
        </div>

        <section class="mapping-section" aria-labelledby="object-mapping-heading">
          <div class="mapping-section-head">
            <div>
              <h2 id="object-mapping-heading">对象映射</h2>
              <p>把真实数据表字段映射为对象属性；主键保证身份稳定，标题保证 Agent 回答可读。</p>
            </div>
            <el-button v-if="canWrite" size="small" type="primary" :disabled="!databaseDataSources.length || !detail.entities.length" @click="openMapping()"><el-icon><Plus /></el-icon> 添加对象映射</el-button>
          </div>
          <div v-if="objectMappingRows.length" class="mapping-card-grid">
            <article v-for="row in objectMappingRows" :key="row.id" class="mapping-item-card">
              <header class="mapping-item-head">
                <span class="ent-chip" :style="{ color: entColor(row.entity_id) }"><i :style="{ background: entColor(row.entity_id) }"></i>{{ row._isAiDraft ? row._entityLabel : entName(row.entity_id) }}</span>
                <el-tag size="small" effect="plain" :type="mappingStatusType(row.status)">{{ mappingStatusLabel(row.status) }}</el-tag>
              </header>
              <div class="mapping-source"><b>{{ row._isAiDraft ? row._sourceLabel : dsName(row.data_source_id) }}</b><span>{{ row.table_name || '—' }}</span></div>
              <div class="col-maps">
                <span class="col-map" v-for="(value, key) in row.column_map" :key="key">{{ key }} ← {{ value }}</span>
                <span v-if="!Object.keys(row.column_map || {}).length" class="muted">尚未映射字段</span>
              </div>
              <p v-if="row.last_error" class="mapping-inline-error"><el-icon><WarningFilled /></el-icon>{{ row.last_error }}</p>
              <small v-if="mappingRefreshJob(row)" class="mapping-job-state" role="status" aria-live="polite" aria-atomic="true">
                {{ mappingJobLabel(mappingRefreshJob(row)?.status) }} · 第 {{ mappingRefreshJob(row)?.attempt || 0 }}/{{ mappingRefreshJob(row)?.max_attempts || 0 }} 次
              </small>
              <footer class="mapping-item-actions">
                <el-button v-if="canWrite && row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
                  <el-button v-if="canWrite" size="small" text @click="openMapping(row.id)">编辑</el-button>
                  <el-button size="small" text @click="doPreviewMapping(row)">预览</el-button>
                  <el-button v-if="canWrite" size="small" text :loading="(row as any)._testing" @click="doTestMapping(row)">测试</el-button>
                  <el-button v-if="canWrite" size="small" text type="primary" :loading="mappingRefreshActive(row)" :disabled="mappingRefreshActive(row)" @click="doRefreshMapping(row)">{{ mappingRefreshActive(row) ? '刷新中' : '刷新对象' }}</el-button>
                  <el-button v-if="canWrite && row.id" size="small" text type="danger" @click="removeMapping(row.id)">删除</el-button>
                </template>
              </footer>
            </article>
          </div>
          <el-empty v-else description="暂无对象映射；请先选择对象类型、数据源、表和字段" :image-size="58" />
        </section>

        <section class="mapping-section" aria-labelledby="relation-mapping-heading">
          <div class="mapping-section-head">
            <div>
              <h2 id="relation-mapping-heading">关系映射</h2>
              <p>基于两端对象映射，从外键或中间表生成真实对象之间的链接。</p>
            </div>
            <el-button v-if="canWrite" size="small" type="primary" :disabled="!detail.relations.length" @click="openRelationMapping()"><el-icon><Plus /></el-icon> 添加关系映射</el-button>
          </div>
          <div v-if="relationMappingRows.length" class="mapping-card-grid relation-mapping-grid">
            <article v-for="row in relationMappingRows" :key="row.id" class="mapping-item-card relation-mapping-card">
              <header class="mapping-item-head">
                <div class="relation-mapping-title"><b>{{ row.relation_name }}</b><span>{{ row.source_entity_name }} → {{ row.target_entity_name }}</span></div>
                <el-tag size="small" effect="plain" :type="mappingStatusType(row.status)">{{ mappingStatusLabel(row.status) }}</el-tag>
              </header>
              <dl class="relation-mapping-facts">
                <div><dt>映射方式</dt><dd>{{ relationMappingModeLabel(row.mode) }}</dd></div>
                <div><dt>关系来源</dt><dd>{{ row.data_source_name || '—' }} / {{ row.table_name || '—' }}</dd></div>
                <div><dt>已生成链接</dt><dd>{{ row.last_link_count || 0 }} 条</dd></div>
                <div><dt>最近检查</dt><dd>{{ row.last_checked_at ? formatDate(row.last_checked_at) : '尚未检查' }}</dd></div>
              </dl>
              <p v-if="row.last_error" class="mapping-inline-error"><el-icon><WarningFilled /></el-icon>{{ row.last_error }}</p>
              <footer class="mapping-item-actions">
                <el-button v-if="canWrite && row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
                  <el-button v-if="canWrite" size="small" text @click="preflightSavedRelationMapping(row)">预检</el-button>
                  <el-button v-if="canWrite" size="small" text @click="openRelationMapping(row.id)">编辑</el-button>
                  <el-button v-if="canWrite" size="small" text type="danger" @click="removeRelationMapping(row)">删除</el-button>
                </template>
              </footer>
            </article>
          </div>
          <el-empty v-else description="暂无关系映射；先完成关系两端的对象映射" :image-size="58" />
        </section>
      </el-tab-pane>

      <!-- ═══════════ 业务函数（无副作用、可由 Agent 调用）═══════════ -->
      <el-tab-pane label="函数" name="functions" data-testid="functions-tab" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">正式函数 <b>{{ detail.functions.length }}</b></span><span class="stat">候选函数 <b>{{ scenarioDraftsOf('function').length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" data-testid="create-function" @click="openFunction()"><el-icon><Plus /></el-icon> 添加函数</el-button>
          </div>
        </div>
        <el-alert
          class="function-declaration-note"
          title="函数用于确定性计算且不产生外部副作用；需要写数据或调用外部系统时，请使用“操作”。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="card map-card">
          <el-table :data="functionRows" class="map-table" :empty-text="canWrite ? '暂无函数，点击「添加函数」配置可供 Agent 调用的确定性计算' : '暂无函数'">
            <el-table-column label="名称 / 说明" min-width="190">
              <template #default="{ row }">
                <div class="function-name-cell">
                  <div class="inline-resource-name"><b>{{ row.name }}</b></div>
                  <span class="muted">{{ row.description || '—' }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="输入字段" min-width="250">
              <template #default="{ row }"><SchemaFieldBuilder :model-value="row.input_schema" readonly empty-text="无需输入" /></template>
            </el-table-column>
            <el-table-column label="输出字段" min-width="250">
              <template #default="{ row }"><SchemaFieldBuilder :model-value="row.output_schema" readonly empty-text="无结构化输出" /></template>
            </el-table-column>
            <el-table-column label="标签" min-width="150">
              <template #default="{ row }">
                <div v-if="row.tags?.length" class="function-tags">
                  <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
                </div>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="运行方式" min-width="130">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" :type="row.runtime_kind === 'contract' ? 'info' : 'success'">{{ functionRuntimeLabel(row.runtime_kind) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="可见性" width="120">
              <template #default="{ row }">
                <el-tooltip content="这里只说明适用范围；实际访问仍由当前场景的权限决定" placement="top">
                  <el-tag size="small" effect="plain" :type="row.visibility === 'tenant' ? 'success' : 'info'">
                    {{ row.visibility === 'tenant' ? '租户' : '场景内' }}
                  </el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="210" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
                  <el-button size="small" text @click="openFunction(row.id)">编辑</el-button>
                  <el-button v-if="row.runtime_kind !== 'contract'" size="small" text type="primary" @click="doRunFunction(row)">运行</el-button>
                  <el-button size="small" text type="danger" @click="removeFunction(row.id)">删除</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 操作（Actions）═══════════ -->
      <el-tab-pane label="操作" name="actions" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">正式操作 <b>{{ detail.actions.length }}</b></span><span class="stat">候选操作 <b>{{ scenarioDraftsOf('action').length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openAction()"><el-icon><Plus /></el-icon> 添加操作</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="actionRows" class="map-table" :empty-text="canWrite ? '暂无操作，点击「添加操作」创建' : '暂无操作'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><div class="inline-resource-name"><b>{{ row.name }}</b></div></template>
            </el-table-column>
            <el-table-column label="所属对象类型" min-width="120">
              <template #default="{ row }">
                <span class="ent-chip" :style="{ color: entColor(row.entity_id) }">
                  <i :style="{ background: entColor(row.entity_id) }"></i>{{ row._isAiDraft ? row._entityLabel : entName(row.entity_id) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="执行方式" width="110">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ actionExecutorLabel(row.executor_type) }}</el-tag>
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
                <el-button v-if="row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
                  <el-button size="small" text @click="openAction(row.id)">编辑</el-button>
                  <el-button size="small" text type="primary" :loading="row._executing" @click="doExecuteAction(row)">参数与执行</el-button>
                  <el-button size="small" text type="danger" @click="removeAction(row.id)">删除</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 规则（Rules）═══════════ -->
      <el-tab-pane label="规则" name="rules" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">正式规则 <b>{{ detail.rules.length }}</b></span><span class="stat">候选规则 <b>{{ scenarioDraftsOf('rule').length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openRule()"><el-icon><Plus /></el-icon> 添加规则</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="ruleRows" class="map-table" :empty-text="canWrite ? '暂无规则，点击「添加规则」创建' : '暂无规则'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><div class="inline-resource-name"><b>{{ row.name }}</b></div></template>
            </el-table-column>
            <el-table-column label="关联对象类型" min-width="120">
              <template #default="{ row }">
                <span v-if="row.entity_id" class="ent-chip" :style="{ color: entColor(row.entity_id) }">
                    <i :style="{ background: entColor(row.entity_id) }"></i>{{ row._isAiDraft ? row._entityLabel : entName(row.entity_id) }}
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
                <el-button v-if="row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
                  <el-button size="small" text @click="openRule(row.id)">编辑</el-button>
                  <el-button size="small" text type="primary" @click="doEvalRule(row)">评估</el-button>
                  <el-button size="small" text type="danger" @click="removeRule(row.id)">删除</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 事件（Events）═══════════ -->
      <el-tab-pane label="事件" name="events" lazy>
        <div class="tab-toolbar">
          <div class="tab-stats"><span class="stat">正式事件 <b>{{ detail.events.length }}</b></span><span class="stat">候选事件 <b>{{ scenarioDraftsOf('event').length }}</b></span></div>
          <div v-if="canWrite" class="tab-actions">
            <el-button size="small" type="primary" @click="openEvent()"><el-icon><Plus /></el-icon> 添加事件</el-button>
          </div>
        </div>
        <div class="card map-card">
          <el-table :data="eventRows" class="map-table" :empty-text="canWrite ? '暂无事件，点击「添加事件」创建' : '暂无事件'">
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }"><div class="inline-resource-name"><b>{{ row.name }}</b></div></template>
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
                <el-button v-if="row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                <template v-else>
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
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ═══════════ 工作流（Workflows）═══════════ -->
      <el-tab-pane label="工作流" name="workflows" lazy>
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
            @close="closeWorkflowEditor"
            @save="saveWorkflow"
            @run-created="openWorkflowRun"
          />
        </div>
        <!-- 工作流列表 -->
        <template v-else>
          <div class="tab-toolbar">
            <div class="tab-stats"><span class="stat">正式工作流 <b>{{ detail.workflows.length }}</b></span><span class="stat">候选工作流 <b>{{ scenarioDraftsOf('workflow').length }}</b></span></div>
            <div v-if="canWrite" class="tab-actions">
              <el-button size="small" type="primary" @click="openWorkflow()"><el-icon><Plus /></el-icon> 新建工作流</el-button>
            </div>
          </div>
          <div class="card map-card">
            <el-table :data="workflowRows" class="map-table" :empty-text="canWrite ? '暂无工作流，点击「新建工作流」开始可视化编排' : '暂无工作流'">
              <el-table-column label="名称" min-width="140">
                <template #default="{ row }"><div class="inline-resource-name"><b>{{ row.name }}</b></div></template>
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
                  <el-button v-if="canWrite && row._isAiDraft" size="small" text @click="startEditingScenarioDraft(row._scenarioDraft)">编辑</el-button>
                  <template v-else>
                    <el-button v-if="canWrite" size="small" text @click="openWorkflow(row.id)">编排</el-button>
                    <el-button v-if="canWrite" size="small" text type="primary" :disabled="row.status !== 'active'" :loading="row._executing" @click="doExecuteWorkflow(row)">执行</el-button>
                    <el-button size="small" text @click="goToWorkflowTasks(row)">任务</el-button>
                    <el-button v-if="canWrite" size="small" text type="danger" @click="removeWorkflow(row.id)">删除</el-button>
                  </template>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-tab-pane>

      <el-tab-pane label="能力输入" name="capability-inputs" lazy>
        <CapabilityPortsPanel
          :scenario-id="sid"
          :can-write="canWrite"
          :capability-names="capabilityNames"
        />
      </el-tab-pane>

      <el-tab-pane name="candidates" lazy>
        <template #label>
          <span>候选评审 <b class="candidate-tab-count">{{ scenarioDraftSummary.candidate_count ?? '—' }}</b></span>
        </template>
        <CandidateReviewPanel
          :scenario-id="sid"
          :candidates="scenarioDrafts"
          :summary="scenarioDraftSummary"
          :formal-count="formalDefinitionCount"
          :loading="scenarioDraftsLoading"
          :load-error="scenarioDraftsError"
          :can-write="canWrite"
          @refresh="refreshCandidateReview"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- ═══════════ 数据映射对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="relationInstanceDlg" title="关系实例" width="min(780px, 94vw)" class="glass-dialog">
      <el-alert v-if="!detail.relations.length" type="warning" :closable="false" show-icon title="请先在「本体模型」中创建关系类型，才能连接对象实例。" />
      <template v-else>
        <el-form label-position="top">
          <el-form-item label="关系类型" required>
            <el-select v-model="relationInstanceForm.relation_id" style="width:100%" placeholder="选择关系类型" @change="resetRelationInstanceEndpoints">
              <el-option v-for="relation in detail.relations" :key="relation.id" :value="relation.id" :label="`${entName(relation.source_entity_id)} — ${relation.name} → ${entName(relation.target_entity_id)}`" />
            </el-select>
          </el-form-item>
          <div class="form-row relation-instance-form-row">
            <el-form-item label="来源对象实例" class="form-col" required>
              <el-select v-model="relationInstanceForm.source_instance_id" filterable placeholder="选择来源对象" style="width:100%">
                <el-option v-for="instance in relationSourceInstances" :key="instance.id" :value="instance.id" :label="instance.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标对象实例" class="form-col" required>
              <el-select v-model="relationInstanceForm.target_instance_id" filterable placeholder="选择目标对象" style="width:100%">
                <el-option v-for="instance in relationTargetInstances" :key="instance.id" :value="instance.id" :label="instance.name" />
              </el-select>
            </el-form-item>
          </div>
          <el-alert v-if="selectedRelationDefinition && (!relationSourceInstances.length || !relationTargetInstances.length)" type="warning" :closable="false" show-icon title="这个关系类型的一端还没有对象实例，请先添加对应对象。" />
          <el-form-item label="关系属性">
            <KeyValueEditor v-model="relationInstanceForm.attributes" empty-text="此关系实例没有额外属性" />
          </el-form-item>
          <el-button type="primary" :loading="relationInstanceSaving" :disabled="!canCreateRelationInstance" @click="saveRelationInstance"><el-icon><Plus /></el-icon> 添加关系实例</el-button>
        </el-form>
      </template>
      <el-divider content-position="left">已有关系实例</el-divider>
      <el-table v-loading="relationInstancesLoading" class="relation-instance-table" :data="relationInstanceRows" size="small" empty-text="暂无关系实例">
        <el-table-column prop="relation_name" label="关系类型" min-width="130" />
        <el-table-column prop="source_instance_name" label="来源对象" min-width="140" />
        <el-table-column label="" width="38" align="center"><template #default>→</template></el-table-column>
        <el-table-column prop="target_instance_name" label="目标对象" min-width="140" />
        <el-table-column label="操作" width="72" align="right">
          <template #default="{ row }"><el-button text type="danger" size="small" @click="removeRelationInstance(row)">删除</el-button></template>
        </el-table-column>
      </el-table>
      <div class="relation-instance-cards">
        <article v-for="row in relationInstanceRows" :key="row.id" class="relation-instance-card">
          <div><b>{{ row.relation_name }}</b><span>{{ row.source_instance_name }} → {{ row.target_instance_name }}</span></div>
          <el-button text type="danger" size="small" @click="removeRelationInstance(row)">删除</el-button>
        </article>
        <div v-if="!relationInstancesLoading && !relationInstanceRows.length" class="muted">暂无关系实例</div>
      </div>
    </el-dialog>

    <el-dialog v-if="canWrite" v-model="mappingDlg" title="数据映射" width="min(840px, 94vw)" class="glass-dialog">
      <el-form label-position="top">
        <el-form-item label="目标对象类型">
          <el-select v-model="mappingForm.entity_id" style="width:100%" @change="onMappingEntityChange">
            <el-option v-for="e in detail.entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="数据源">
          <el-select v-model="mappingForm.data_source_id" style="width:100%" @change="onMapDsChange">
            <el-option v-for="d in databaseDataSources" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="表名">
          <el-select v-model="mappingForm.table_name" style="width:100%" filterable placeholder="选择真实数据表">
            <el-option v-for="t in mapTables" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="字段映射（对象属性 ← 数据列）">
          <div class="colmap-list">
            <div class="mapping-property" v-for="p in (detail.entities.find((e) => e.id === mappingForm.entity_id)?.properties || [])" :key="p.name">
              <div class="colmap-row">
                <span class="colmap-attr">
                  <b>{{ p.name }}</b>
                  <span class="mapping-property-flags">
                    <el-tag v-if="p.is_key" size="small" effect="plain">主键属性</el-tag>
                    <el-tag v-if="p.is_title" size="small" effect="plain" type="success">标题属性</el-tag>
                  </span>
                </span>
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
        <el-button type="primary" :loading="scenarioDraftPromotionSyncing" @click="saveMapping">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-if="canWrite"
      v-model="relationMappingDlg"
      :title="relationMappingEditingId ? '编辑关系映射' : '添加关系映射'"
      width="min(760px, 94vw)"
      class="glass-dialog relation-mapping-dialog"
      @closed="resetRelationMappingDialog"
    >
      <el-steps :active="relationMappingStep" finish-status="success" simple class="relation-mapping-steps">
        <el-step title="选择关系" />
        <el-step title="选择真实数据列" />
        <el-step title="预检确认" />
      </el-steps>
      <el-form label-position="top" class="relation-mapping-form">
        <el-form-item label="关系类型" required>
          <el-select v-model="relationMappingForm.relation_id" filterable style="width:100%" placeholder="选择要从数据生成的关系" @change="onRelationMappingRelationChange">
            <el-option
              v-for="relation in detail.relations"
              :key="relation.id"
              :label="`${relation.name}（${entName(relation.source_entity_id)} → ${entName(relation.target_entity_id)}）`"
              :value="relation.id"
            />
          </el-select>
        </el-form-item>

        <template v-if="selectedRelationMappingRelation">
          <div class="relation-endpoint-grid">
            <el-form-item :label="`来源对象映射 · ${entName(selectedRelationMappingRelation.source_entity_id)}`" required>
              <el-select v-model="relationMappingForm.source_mapping_id" style="width:100%" placeholder="选择来源对象映射" @change="onRelationMappingEndpointChange">
                <el-option v-for="mapping in relationSourceObjectMappings" :key="mapping.id" :label="objectMappingOptionLabel(mapping)" :value="mapping.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="`目标对象映射 · ${entName(selectedRelationMappingRelation.target_entity_id)}`" required>
              <el-select v-model="relationMappingForm.target_mapping_id" style="width:100%" placeholder="选择目标对象映射" @change="onRelationMappingEndpointChange">
                <el-option v-for="mapping in relationTargetObjectMappings" :key="mapping.id" :label="objectMappingOptionLabel(mapping)" :value="mapping.id" />
              </el-select>
            </el-form-item>
          </div>
          <el-alert
            v-if="relationMappingMissingEndpointNames.length"
            class="relation-mapping-prerequisite"
            type="warning"
            :closable="false"
            show-icon
            title="关系两端必须先完成对象映射"
          >
            <template #default>
              <span>缺少：{{ relationMappingMissingEndpointNames.join('、') }}</span>
              <div class="inline-guidance-actions">
                <el-button v-if="!relationSourceObjectMappings.length" size="small" plain type="primary" @click="openMappingForEntity(selectedRelationMappingRelation.source_entity_id)">配置来源对象映射</el-button>
                <el-button v-if="!relationTargetObjectMappings.length" size="small" plain type="primary" @click="openMappingForEntity(selectedRelationMappingRelation.target_entity_id)">配置目标对象映射</el-button>
              </div>
            </template>
          </el-alert>

          <el-form-item label="关系在数据中如何保存" required>
            <el-radio-group v-model="relationMappingForm.mode" class="relation-mode-options" @change="onRelationMappingModeChange">
              <el-radio v-for="mode in RELATION_MAPPING_MODES" :key="mode.value" :value="mode.value" class="relation-mode-option">
                <span><b>{{ mode.label }}</b><small>{{ mode.description }}</small></span>
              </el-radio>
            </el-radio-group>
          </el-form-item>

          <template v-if="relationMappingForm.mode !== 'join_table'">
            <el-alert
              class="carrier-source-alert"
              type="info"
              :closable="false"
              :title="relationMappingCarrier ? `从 ${relationMappingCarrier.data_source_name || dsName(relationMappingCarrier.data_source_id)} / ${relationMappingCarrier.table_name} 选择外键` : '请先选择两端对象映射'"
              :description="relationMappingModeDescription(relationMappingForm.mode)"
              show-icon
            />
            <el-form-item label="外键列" required>
              <el-select v-model="relationMappingForm.foreign_key_column" filterable style="width:100%" :loading="relationMappingOptionsLoading" placeholder="选择真实数据列" @change="invalidateRelationMappingPreview">
                <el-option v-for="column in relationMappingColumns" :key="column" :label="column" :value="column" />
              </el-select>
              <div v-if="relationMappingCarrier && !relationMappingColumns.length && !relationMappingOptionsLoading" class="form-help">当前表没有可用列，请检查对象映射的数据源和表是否仍然存在。</div>
            </el-form-item>
          </template>

          <template v-else>
            <div class="relation-endpoint-grid">
              <el-form-item label="中间表数据源" required>
                <el-select v-model="relationMappingForm.join_data_source_id" filterable style="width:100%" placeholder="选择已接入的数据源" @change="onRelationJoinDataSourceChange">
                  <el-option v-for="source in databaseDataSources" :key="source.id" :label="source.name" :value="source.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="中间表" required>
                <el-select v-model="relationMappingForm.join_table_name" filterable style="width:100%" :loading="relationMappingOptionsLoading" placeholder="选择真实数据表" @change="onRelationJoinTableChange">
                  <el-option v-for="table in relationMappingTables" :key="table.name" :label="table.name" :value="table.name" />
                </el-select>
              </el-form-item>
            </div>
            <div class="relation-endpoint-grid">
              <el-form-item :label="`指向来源对象“${selectedRelationMappingRelation.source_entity_name || entName(selectedRelationMappingRelation.source_entity_id)}”主键的列`" required>
                <el-select v-model="relationMappingForm.source_key_column" filterable style="width:100%" placeholder="选择来源主键列" @change="invalidateRelationMappingPreview">
                  <el-option v-for="column in relationMappingColumns" :key="`source-${column}`" :label="column" :value="column" />
                </el-select>
              </el-form-item>
              <el-form-item :label="`指向目标对象“${selectedRelationMappingRelation.target_entity_name || entName(selectedRelationMappingRelation.target_entity_id)}”主键的列`" required>
                <el-select v-model="relationMappingForm.target_key_column" filterable style="width:100%" placeholder="选择目标主键列" @change="invalidateRelationMappingPreview">
                  <el-option v-for="column in relationMappingColumns" :key="`target-${column}`" :label="column" :value="column" />
                </el-select>
              </el-form-item>
            </div>
          </template>
        </template>
      </el-form>

      <section v-if="relationMappingPreview" class="relation-preflight-result" aria-live="polite">
        <header>
          <div><b>预检结果</b><span>{{ relationMappingPreview.message }}</span></div>
          <el-tag :type="relationMappingPreview.ok ? 'success' : 'danger'" effect="plain">{{ relationMappingPreview.ok ? '可以保存' : '需要修正' }}</el-tag>
        </header>
        <ul v-if="relationMappingPreview.errors.length" class="mapping-issue-list relation-preflight-errors">
          <li v-for="error in relationMappingPreview.errors" :key="error">{{ error }}</li>
        </ul>
        <ul v-if="relationMappingPreview.warnings.length" class="mapping-issue-list relation-preflight-warnings">
          <li v-for="warning in relationMappingPreview.warnings" :key="warning">{{ warning }}</li>
        </ul>
      </section>

      <template #footer>
        <el-button @click="relationMappingDlg = false">取消</el-button>
        <el-button :loading="relationMappingPreflighting" :disabled="Boolean(relationMappingMissingFields.length)" @click="preflightRelationMapping">预检</el-button>
        <el-button type="primary" :loading="relationMappingSaving" :disabled="!relationMappingCanSave" @click="saveRelationMapping">确认保存</el-button>
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
            <div v-if="mappingPreview.fields.length" class="mapping-field-list">
              <article v-for="field in mappingPreview.fields" :key="field.property_name" class="mapping-field-card">
                <div class="mapping-field-name">
                  <b>{{ field.property_name }}</b>
                  <span>
                    <el-tag v-if="field.is_key" size="small" effect="plain">主键</el-tag>
                    <el-tag v-if="field.is_title" size="small" effect="plain" type="success">标题</el-tag>
                    <el-tag v-if="field.is_required" size="small" effect="plain" type="warning">必填</el-tag>
                  </span>
                </div>
                <span class="mono">{{ field.source_column || '未配置源列' }}</span>
                <el-tag size="small" effect="plain" :type="mappingFieldType(field.status)">{{ mappingFieldLabel(field.status) }}</el-tag>
              </article>
            </div>
            <el-empty v-else description="对象类型暂无属性" :image-size="48" />
          </section>
          <section class="mapping-samples">
            <div class="preview-section-title">源表样本</div>
            <div v-if="mappingPreviewRows.length" class="mapping-sample-list">
              <article v-for="(row, rowIndex) in mappingPreviewRows" :key="rowIndex" class="mapping-sample-card">
                <b class="mapping-sample-index">样本 {{ rowIndex + 1 }}</b>
                <dl>
                  <div v-for="column in mappingPreview.columns" :key="column"><dt>{{ column }}</dt><dd><StructuredValueCell :value="row[column]" /></dd></div>
                </dl>
              </article>
            </div>
            <el-empty v-else description="暂无样本数据" :image-size="48" />
          </section>
        </div>
        <section v-if="mappingTransformedRows.length" class="mapping-transformed">
          <div class="preview-section-title">转换后的对象样本</div>
          <div class="mapping-sample-list transformed-sample-list">
            <article v-for="(row, rowIndex) in mappingTransformedRows" :key="rowIndex" class="mapping-sample-card">
              <b class="mapping-sample-index">对象样本 {{ rowIndex + 1 }}</b>
              <dl>
                <div v-for="column in mappingTransformedColumns" :key="column"><dt>{{ column }}</dt><dd><StructuredValueCell :value="row[column]" /></dd></div>
              </dl>
            </article>
          </div>
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

    <!-- ═══════════ 业务函数对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="functionDlg" :title="functionForm.id ? '编辑函数' : '添加函数'" width="680px" class="glass-dialog" data-testid="function-dialog" @closed="clearActiveScenarioDraftPromotion('function')">
      <el-alert
        title="选择受治理的内置计算方式，不填写代码、网址或连接凭据。函数可被 Agent 安全调用。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="function-form">
        <div class="form-row">
          <el-form-item label="函数名称" required class="form-col">
            <el-input v-model="functionForm.name" maxlength="200" show-word-limit placeholder="如：计算业务指标" />
          </el-form-item>
          <el-form-item label="可见性" class="form-col">
            <el-select v-model="functionForm.visibility" style="width:100%">
              <el-option label="仅当前场景" value="scenario" />
              <el-option label="租户范围展示" value="tenant" />
            </el-select>
            <div class="form-help">这里只说明适用范围；实际访问仍遵循当前场景的权限。</div>
          </el-form-item>
        </div>
        <el-form-item label="说明">
          <el-input v-model="functionForm.description" type="textarea" :rows="2" maxlength="8000" show-word-limit placeholder="说明这个业务函数的输入、输出与适用范围" />
        </el-form-item>
        <el-form-item label="标签（用逗号分隔，可选）">
          <el-input v-model="functionForm.tags_text" maxlength="1619" placeholder="如：指标、只读" />
          <div class="form-help">最多 20 个标签，每个标签最多 80 个字符。</div>
        </el-form-item>
        <el-form-item label="运行方式" required>
          <el-select v-model="functionForm.runtime_kind" style="width:100%" @change="resetFunctionRuntime">
            <el-option label="仅定义输入输出（暂不可调用）" value="contract" />
            <el-option label="加权评分" value="weighted_score" />
            <el-option label="阈值判断" value="threshold" />
            <el-option label="两点地理距离" value="geo_distance" />
            <el-option label="时序数值聚合" value="timeseries_aggregate" />
          </el-select>
        </el-form-item>
        <el-form-item label="输入字段" required>
          <SchemaFieldBuilder v-model="functionForm.input_schema" empty-text="该函数暂时不需要输入字段" />
          <div class="form-help">逐项定义函数需要的业务参数；选择可运行的计算方式后，Agent 会按这些字段调用。</div>
        </el-form-item>
        <el-form-item label="输出字段" required>
          <SchemaFieldBuilder v-model="functionForm.output_schema" empty-text="该函数暂时没有结构化输出" />
          <div class="form-help">逐项说明计算结果，Agent 会按这个结构理解并使用返回值。</div>
        </el-form-item>
        <template v-if="functionForm.runtime_kind === 'weighted_score'">
          <el-form-item label="字段权重" required>
            <div v-if="functionNumericFields.length" class="function-runtime-fields">
              <div v-for="field in functionNumericFields" :key="field.name"><span>{{ field.name }}</span><el-input-number v-model="functionForm.runtime_config.weights[field.name]" :step="0.1" controls-position="right" placeholder="权重" /></div>
            </div>
            <el-alert v-else title="请先在输入字段中添加至少一个整数或数值字段" type="warning" :closable="false" show-icon />
          </el-form-item>
          <el-form-item label="基础分"><el-input-number v-model="functionForm.runtime_config.bias" :step="0.1" controls-position="right" /></el-form-item>
        </template>
        <template v-else-if="functionForm.runtime_kind === 'threshold'">
          <div class="form-row">
            <el-form-item label="判断字段" required class="form-col">
              <el-select v-model="functionForm.runtime_config.field" style="width:100%" placeholder="选择数值输入字段">
                <el-option v-for="field in functionNumericFields" :key="field.name" :label="field.name" :value="field.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="比较方式" class="form-col">
              <el-select v-model="functionForm.runtime_config.operator" style="width:100%"><el-option v-for="operator in ['>', '>=', '<', '<=', '==', '!=']" :key="operator" :label="operator" :value="operator" /></el-select>
            </el-form-item>
          </div>
          <el-form-item label="阈值" required><el-input-number v-model="functionForm.runtime_config.threshold" :step="0.1" controls-position="right" /></el-form-item>
        </template>
        <el-form-item v-else-if="functionForm.runtime_kind === 'geo_distance'" label="距离单位">
          <el-radio-group v-model="functionForm.runtime_config.unit"><el-radio-button value="km">千米</el-radio-button><el-radio-button value="m">米</el-radio-button></el-radio-group>
        </el-form-item>
        <el-form-item v-else-if="functionForm.runtime_kind === 'timeseries_aggregate'" label="聚合方式">
          <el-select v-model="functionForm.runtime_config.aggregation" style="width:100%"><el-option label="求和" value="sum" /><el-option label="平均值" value="avg" /><el-option label="最小值" value="min" /><el-option label="最大值" value="max" /><el-option label="计数" value="count" /></el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="functionSaving" @click="functionDlg = false">取消</el-button>
        <el-button type="primary" :loading="functionSaving" @click="saveFunction">保存函数</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 操作对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="actionDlg" :title="actionForm.id ? '编辑操作' : '添加操作'" width="640px" class="glass-dialog" @closed="clearActiveScenarioDraftPromotion('action')">
      <el-form label-position="top">
        <div class="form-row">
          <el-form-item label="名称" class="form-col">
            <el-input v-model="actionForm.name" placeholder="如：更新状态、生成报告" />
          </el-form-item>
          <el-form-item label="所属对象类型" class="form-col">
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
            <el-select v-model="actionForm.executor_type" style="width:100%" @change="resetActionExecutorConfig">
              <el-option v-if="actionForm.executor_type === 'unbound'" label="待绑定（请先选择执行方式）" value="unbound" disabled />
              <el-option label="SQL 查询" value="sql" />
              <el-option label="按模板生成附件" value="template" />
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
            <el-switch v-model="actionForm.requires_confirmation" :disabled="actionForm.executor_type === 'template'" />
            <div class="form-help">确认前只允许预演，不会调用执行器</div>
          </el-form-item>
          <el-form-item label="防止重复提交" class="form-col">
            <el-switch v-model="actionForm.idempotency_required" :disabled="actionForm.executor_type === 'template'" />
            <div class="form-help">防止同一请求重复执行</div>
          </el-form-item>
        </div>
        <template v-if="actionForm.executor_type === 'sql'">
          <el-form-item label="数据源" required>
            <el-select v-model="actionForm.executor_config.data_source_id" filterable placeholder="选择已接入的数据源" style="width:100%">
              <el-option v-for="source in databaseDataSources" :key="source.id" :label="source.name" :value="source.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="只读 SQL" required>
            <el-input v-model="actionForm.executor_config.sql" type="textarea" :rows="5" class="mono" placeholder="SELECT * FROM business_records WHERE record_id = {record_id}" />
            <div class="form-help">参数用 <code>{字段名}</code> 引用；系统只允许只读查询。</div>
          </el-form-item>
        </template>
        <template v-else-if="actionForm.executor_type === 'template'">
          <el-alert title="Action 绑定模板中心中的受管模板；保存时会固定所选模板的当前版本，后续模板升级不会悄悄改变既有执行结果。" type="info" :closable="false" show-icon />
          <div class="template-binding-head">
            <span>模板资源由模板中心统一管理，不再直接绑定文件 ID。</span>
            <el-button text type="primary" @click="goToTemplates">打开模板中心</el-button>
          </div>
          <el-alert
            v-if="hasLegacyTemplateBinding"
            title="这是旧式文件绑定。请选择一个模板中心资源后保存，即可完成迁移；未迁移前现有 Action 仍可继续执行。"
            type="warning"
            :closable="false"
            show-icon
            class="template-legacy-alert"
          />
          <el-form-item label="模板资源 / 版本" required>
            <el-select
              v-model="actionForm.executor_config.template_id"
              filterable
              :loading="actionTemplatesLoading"
              placeholder="选择当前场景或租户共享模板"
              aria-label="选择模板资源及当前版本"
              style="width:100%"
              @change="onActionTemplateChanged"
            >
              <el-option
                v-for="template in actionTemplates"
                :key="template.id"
                :value="template.id"
                :label="`${template.name} · ${templateVersionLabel(template)}`"
                :disabled="Boolean(templateUnavailableReason(template)) && template.id !== originalActionTemplateId"
              >
                <div class="template-option">
                  <span><b>{{ template.name }}</b><small>{{ template.purpose || scenarioTemplateScope(template) }}</small></span>
                  <span>
                    <el-tag size="small" effect="plain">{{ templateFormatLabel(template.current_version?.artifact_format) }}</el-tag>
                    <em>{{ templateVersionLabel(template) }}</em>
                  </span>
                </div>
              </el-option>
            </el-select>
            <div v-if="actionTemplatesError" class="mapping-inline-error" role="status">
              <el-icon aria-hidden="true"><WarningFilled /></el-icon><span>{{ actionTemplatesError }}</span>
              <el-button text type="primary" size="small" @click="loadActionTemplates">重试</el-button>
            </div>
            <div v-else-if="!actionTemplatesLoading && !actionTemplates.length" class="form-help">当前场景没有可选模板，请先在模板中心上传并登记。</div>
          </el-form-item>
          <section v-if="selectedActionTemplate" class="selected-template-summary" aria-label="已选模板摘要">
            <header>
              <div><b>{{ selectedActionTemplate.name }}</b><span>{{ selectedActionTemplate.purpose || '未填写业务用途' }}</span></div>
              <div class="selected-template-tags">
                <el-tag size="small" :type="selectedActionTemplate.status === 'active' ? 'success' : 'info'">{{ selectedActionTemplate.status === 'active' ? '使用中' : '已停用' }}</el-tag>
                <el-tag size="small" effect="plain">{{ templateFormatLabel(selectedActionTemplate.current_version?.artifact_format) }}</el-tag>
                <el-tag size="small" effect="plain">{{ boundTemplateVersionLabel }}</el-tag>
              </div>
            </header>
            <div v-if="selectedTemplateVariables.length" class="selected-template-variables">
              <span>变量</span><code v-for="variable in selectedTemplateVariables.slice(0, 8)" :key="variable">{{ variable }}</code>
              <span v-if="selectedTemplateVariables.length > 8">另 {{ selectedTemplateVariables.length - 8 }} 项</span>
            </div>
            <footer>
              <el-button size="small" plain @click="syncActionSchemaFromTemplate">同步变量到输入参数</el-button>
              <el-button v-if="usesOlderTemplateVersion" size="small" text type="primary" @click="useCurrentTemplateVersion">改用当前 {{ templateVersionLabel(selectedActionTemplate) }}</el-button>
            </footer>
            <p v-if="templateUnavailableReason(selectedActionTemplate)" class="mapping-inline-error" role="status">
              <el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ templateUnavailableReason(selectedActionTemplate) }}；既有固定版本仍可执行，但不能建立新绑定。
            </p>
          </section>
          <div class="form-row">
            <el-form-item label="生成附件保存到" class="form-col" required>
              <el-select v-model="actionForm.executor_config.target_data_source_id" filterable placeholder="选择输出文件资料库" style="width:100%">
                <el-option v-for="source in fileBucketSources" :key="source.id" :label="`${source.name}${source.can_write === false ? '（只读，不能保存附件）' : ''}`" :value="source.id" :disabled="source.can_write === false" />
              </el-select>
            </el-form-item>
            <el-form-item label="输出文件名" class="form-col">
              <el-input v-model.trim="actionForm.executor_config.output_filename" placeholder="可不填；自动沿用模板格式" />
              <div class="form-help">即使不写扩展名，系统也会使用源模板的扩展名。</div>
            </el-form-item>
          </div>
          <el-empty v-if="!writableFileBucketSources.length" description="当前场景没有可写文件桶，无法保存 Agent 生成的附件" :image-size="54">
            <el-button type="primary" plain @click="goToDataSources">创建文件桶</el-button>
          </el-empty>
        </template>
        <el-form-item v-else-if="actionForm.executor_type === 'skill'" label="本地技能" required>
          <el-select v-model="actionForm.executor_config.skill_id" filterable placeholder="选择已启用技能" style="width:100%">
            <el-option v-for="skill in skills" :key="skill.id" :label="skill.name" :value="skill.id" :disabled="skill.enabled === false" />
          </el-select>
        </el-form-item>
        <template v-else-if="actionForm.executor_type === 'mcp'">
          <el-form-item label="MCP 服务" required>
            <el-select v-model="actionForm.executor_config.mcp_id" filterable placeholder="选择外部工具服务" style="width:100%">
              <el-option v-for="config in mcpConfigs" :key="config.id" :label="config.name" :value="config.id" :disabled="config.enabled === false" />
            </el-select>
          </el-form-item>
          <el-form-item label="工具名称" required><el-input v-model.trim="actionForm.executor_config.tool_name" placeholder="例如 create_ticket" /></el-form-item>
        </template>
        <template v-else-if="actionForm.executor_type === 'http'">
          <div class="form-row">
            <el-form-item label="请求方式" class="form-col"><el-select v-model="actionForm.executor_config.method" style="width:100%"><el-option v-for="method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']" :key="method" :label="method" :value="method" /></el-select></el-form-item>
            <el-form-item label="HTTPS 地址" class="form-col"><el-input v-model.trim="actionForm.executor_config.url" placeholder="https://api.example.com/resource/{id}" /></el-form-item>
          </div>
          <el-form-item label="请求头"><KeyValueEditor v-model="actionForm.executor_config.headers" key-placeholder="请求头名称" value-placeholder="请求头值" empty-text="没有额外请求头" /></el-form-item>
        </template>
        <el-form-item label="输入参数">
          <SchemaFieldBuilder v-model="actionForm.input_schema" empty-text="此操作不需要输入参数" />
        </el-form-item>
        <el-alert
          v-if="actionLegacyConditions.length"
          type="warning"
          :closable="false"
          show-icon
          title="旧条件只是自然语言，系统不能把它当作可靠的执行门禁"
          class="action-condition-alert"
        >
          <template #default>
            <span>请按下面的表单重新配置；如果旧说明不再需要，可明确清除后再保存。</span>
            <el-button size="small" plain type="warning" @click="clearLegacyActionConditions">清除旧说明</el-button>
          </template>
        </el-alert>
        <el-form-item label="执行前条件">
          <RuleConditionBuilder v-model="actionPrecondition" :fields="actionInputFieldNames" />
          <div class="form-help">只使用输入参数做可验证判断；不满足时，预演和实际执行都会被拒绝。</div>
        </el-form-item>
        <el-form-item label="执行后校验">
          <RuleConditionBuilder v-model="actionPostcondition" :fields="[]" />
          <div class="form-help">按执行器返回的字段校验结果；属性名可直接输入，校验失败会将本次执行标记为失败。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="actionDlg = false">取消</el-button>
        <el-button type="primary" :loading="scenarioDraftPromotionSyncing" @click="saveAction">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 操作参数与安全执行对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="actionExecuteDlg" :title="`执行操作：${actionExecuteRow?.name || ''}`" width="640px" class="glass-dialog">
      <el-alert
        :title="`权限范围：当前业务场景 · ${actionExecuteRow?.requires_confirmation === false ? '可直接执行' : '需要确认后执行'}`"
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
          <el-select v-else-if="field.schema.type === 'array'" v-model="actionParamsForm[field.name]" multiple filterable allow-create default-first-option placeholder="输入一项后按回车添加" style="width:100%" />
          <KeyValueEditor v-else-if="field.schema.type === 'object'" v-model="actionParamsForm[field.name]" empty-text="添加对象字段" />
          <el-input v-else v-model="actionParamsForm[field.name]" :placeholder="field.schema.default !== undefined ? `默认值：${field.schema.default}` : '请输入参数'" />
        </el-form-item>
        <el-empty v-if="!actionParameterFields.length" :image-size="56" description="此操作无需输入参数" />
      </el-form>
      <div class="action-execution-meta">
        <span>防重复标识</span>
        <code class="mono">{{ actionIdempotencyKey }}</code>
        <span class="muted">本次确认执行保持不变</span>
      </div>
      <el-alert v-if="actionPreviewResult" class="action-preview-alert" type="success" :closable="false" show-icon>
        <template #title>预演完成：未调用执行器，可确认执行</template>
        <KeyValueEditor :model-value="actionPreviewResult.result?.plan || actionPreviewResult.result || {}" readonly empty-text="预演没有返回明细" />
      </el-alert>
      <dl v-if="actionPreviewResult" class="action-runtime-provenance" aria-label="本次预演的安全检查">
        <div>
          <dt>定义依据</dt>
          <dd>{{ actionPreviewResult.definition_source === 'release' ? '已固定的场景定义' : '当前场景定义' }}</dd>
        </div>
        <div>
          <dt>外部连接</dt>
          <dd>{{ actionPreviewResult.connector_audit?.length ? '已检查且可用' : '本操作无需外部连接' }}</dd>
        </div>
      </dl>
      <template #footer>
        <el-button @click="actionExecuteDlg = false">取消</el-button>
        <el-button :loading="actionPreviewing" @click="previewActionExecution">预演</el-button>
        <el-button type="primary" :loading="actionExecuting" :disabled="!hasPinnedActionPreview" @click="confirmActionExecution">确认执行</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 规则对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="ruleDlg" :title="ruleForm.id ? '编辑规则' : '添加规则'" width="640px" class="glass-dialog" @closed="clearActiveScenarioDraftPromotion('rule')">
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
          <el-form-item label="关联对象类型（可选，留空为全局规则）" class="form-col">
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
        <el-form-item label="判断条件">
          <RuleConditionBuilder v-model="ruleForm.condition" :fields="ruleFieldOptions" />
        </el-form-item>
        <el-form-item label="命中后动作（文本说明）">
          <el-input v-model="ruleForm.action_on_match" placeholder="如：更新状态并通知负责人" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDlg = false">取消</el-button>
        <el-button type="primary" :loading="ruleSaving || scenarioDraftPromotionSyncing" :disabled="ruleSaving" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 事件对话框 ═══════════ -->
    <el-dialog v-if="canWrite" v-model="eventDlg" :title="eventForm.id ? '编辑事件' : '添加事件'" width="560px" class="glass-dialog" @closed="clearActiveScenarioDraftPromotion('event')">
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
        <el-form-item label="事件载荷字段">
          <SchemaFieldBuilder v-model="eventForm.payload_schema" empty-text="该事件不携带业务字段" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="eventForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="eventDlg = false">取消</el-button>
        <el-button type="primary" :loading="scenarioDraftPromotionSyncing" @click="saveEvent">保存</el-button>
      </template>
    </el-dialog>

    <!-- ═══════════ 执行结果对话框 ═══════════ -->
    <el-dialog v-model="execResultDlg" title="执行结果" width="640px" class="glass-dialog">
      <StructuredValueViewer :value="execResult" empty-text="本次执行没有返回结果" />
      <template #footer>
        <el-button type="primary" @click="execResultDlg = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="recordInputDlg" :title="recordInputTitle" width="600px" class="glass-dialog" @closed="cancelRecordInput">
      <p class="record-input-help">按字段填写业务数据；需要引用工作流参数时可直接填写 <code>{{ '{params.field}' }}</code>。</p>
      <StructuredValueEditor v-model="recordInputValue" root />
      <template #footer>
        <el-button @click="cancelRecordInput">取消</el-button>
        <el-button type="primary" @click="confirmRecordInput">确定</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, computed, watch, nextTick, onMounted, onBeforeUnmount, toRaw } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { cloneForForm } from '@/utils/clone'
import GraphCanvas from '@/components/GraphCanvas.vue'
import EditorPanel from '@/components/EditorPanel.vue'
import KeyValueEditor from '@/components/KeyValueEditor.vue'
import RuleConditionBuilder from '@/components/RuleConditionBuilder.vue'
import SchemaFieldBuilder from '@/components/SchemaFieldBuilder.vue'
import StructuredValueCell from '@/components/StructuredValueCell.vue'
import StructuredValueEditor from '@/components/StructuredValueEditor.vue'
import StructuredValueViewer from '@/components/StructuredValueViewer.vue'
import CandidateReviewPanel from '@/components/CandidateReviewPanel.vue'
import CapabilityPortsPanel from '@/components/CapabilityPortsPanel.vue'
import WorkflowEditor from '@/components/workflow/WorkflowEditor.vue'
import { safeInternalReturnPath } from '@/utils/navigation'
import {
  RELATION_MAPPING_MODES,
  buildRelationMappingPayload,
  missingRelationMappingFields,
  relationMappingModeDescription,
  relationMappingModeLabel,
  relationMappingPayloadFingerprint,
} from '@/utils/relationMappings'
import type { ArtifactTemplate, AssistantActionPreview, ScenarioDetail, ScenarioModelCandidateSummary, ScenarioModelDraftIssue, ScenarioModelDraftResource, GraphData, GraphNode, GraphEdge, Entity, Relation, RelationInstance, DataMapping, DataMappingPreview, DataMappingRefreshJob, FunctionDefinition, ObjectDetail, ObjectSearchItem, RelationDataMapping, RelationDataMappingInput, RelationDataMappingPreview, TableInfo, WorkflowRun } from '@/types'
import { cleanTemplateExecutorConfig, templateFormatLabel, templatePathsToSchema, templateUnavailableReason } from '@/utils/templates'
import { draftRefToken, normalizeScenarioModelDrafts, scenarioDraftIsOpen, scenarioDraftKindLabel, scenarioDraftStage } from '@/utils/scenarioModelDrafts'

const route = useRoute()
const router = useRouter()
let sid = String(route.params.id || '')
let scenarioLoadRequest = 0
const scenarioLoading = ref(true)
const scenarioAccessDenied = ref(false)
const scenarioLoadError = ref('')
// Draft resources can be numerous and are replaced as a batch; deep-reactive
// proxying them makes route teardown and graph projection unnecessarily costly.
const scenarioDrafts = shallowRef<ScenarioModelDraftResource[]>([])
const scenarioDraftSummary = ref<ScenarioModelCandidateSummary>({})
const scenarioDraftsLoading = ref(false)
const scenarioDraftsError = ref('')
type PendingScenarioDraftResolution = {
  draftId: string
  expectedRevision: number
  resolvedResourceId: string
  resourceKind: string
  title: string
  error: string
  retryable: boolean
}
const activeScenarioDraftPromotion = ref<ScenarioModelDraftResource | null>(null)
const pendingScenarioDraftResolutions = ref<PendingScenarioDraftResolution[]>([])
const scenarioDraftPromotionSyncing = ref(false)
let scenarioDraftRequest = 0
let scenarioDraftViewDisposed = false

function emptyScenarioDetail(id: string): ScenarioDetail {
  return {
    id, name: '', description: '',
    entities: [], relations: [], data_sources: [],
    instances: [], relation_instances: [], mappings: [], relation_mappings: [],
    functions: [],
    actions: [], rules: [], events: [], workflows: [],
  }
}
const detail = ref<ScenarioDetail>(emptyScenarioDetail(sid))
const capabilityNames = computed(() => Object.fromEntries([
  ...detail.value.functions.map((item) => [`function:${item.id}`, item.name]),
  ...detail.value.actions.map((item) => [`action:${item.id}`, item.name]),
  ...detail.value.workflows.map((item) => [`workflow:${item.id}`, item.name]),
]))
// The API supplies this per current user; treat an absent value as read-only.
const canWrite = computed(() => detail.value.can_write === true)
const returnPath = computed(() => safeInternalReturnPath(route.query.return_to, '/scenarios'))
const dataSources = ref<any[]>([])
const llmConfigs = ref<any[]>([])
const skills = ref<any[]>([])
const mcpConfigs = ref<any[]>([])
const scenarioDataSources = computed(() => dataSources.value.filter((source) => !source.scenario_id || source.scenario_id === sid))
const databaseDataSources = computed(() => scenarioDataSources.value.filter((source) => source.type !== 'file_bucket'))
const fileBucketSources = computed(() => scenarioDataSources.value.filter((source) => source.type === 'file_bucket'))
const writableFileBucketSources = computed(() => fileBucketSources.value.filter((source) => source.can_write !== false))
const stageNames = new Set(['ontology', 'instances', 'mappings', 'functions', 'actions', 'rules', 'events', 'workflows', 'capability-inputs', 'candidates'])
const requestedStage = Array.isArray(route.query.stage) ? route.query.stage[0] : route.query.stage
const tab = ref(typeof requestedStage === 'string' && stageNames.has(requestedStage) ? requestedStage : 'ontology')
const instFilter = ref('')
const saving = ref(false)
const objectQuery = ref('')
const objectItems = ref<ObjectSearchItem[]>([])
const objectTotal = ref(0)
const objectLoading = ref(false)
const objectLoadingMore = ref(false)
const objectNextOffset = ref(0)
const objectHasMore = ref(false)
const objectTotalIsExact = ref(true)
const objectAppliedKey = ref('')
const selectedObjectId = ref<string | null>(null)
const objectDetail = ref<ObjectDetail | null>(null)
const OBJECT_PAGE_SIZE = 50
let objectRequestId = 0
let objectSearchViewDisposed = false
let objectPendingKey = ''
const relationInstanceRows = ref<RelationInstance[]>([])
const relationInstanceTotal = ref(0)
const relationInstancesLoading = ref(false)
const relationInstanceTotalIsExact = ref(true)
let relationInstanceRequestId = 0

const graphPalette = ['#27b9b0', '#438be5', '#65a9df', '#4aa9c1', '#52c3a1', '#6f93d7']
function visualColor(color: string | undefined, index: number) {
  if (!color || ['#6366f1', '#4f46e5', '#06b6d4'].includes(color.toLowerCase())) {
    return graphPalette[index % graphPalette.length]
  }
  return color
}

// ── 悬浮编辑面板状态 ──
const editor = ref<{ kind: 'entity' | 'relation' | 'instance'; id?: string; form: any } | null>(null)
const draftPropertyEditorIndex = ref<number | null>(null)
// 切换 tab 时关闭悬浮编辑器，避免跨 tab 状态残留；实例页只读取独立的
// 分页运行时接口，不重新请求整个场景详情。
watch(tab, (value, previousValue) => {
  editor.value = null
  draftPropertyEditorIndex.value = null
  clearActiveScenarioDraftPromotion()
  if (value === 'instances' && previousValue !== 'instances') {
    void searchObjects()
    void loadRelationInstances()
  }
  if (value === 'candidates' && previousValue !== 'candidates') void loadScenarioDrafts(true)
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
type InlineScenarioDraftRow = Record<string, any> & {
  _isAiDraft: true
  _scenarioDraft: ScenarioModelDraftResource
}

const openScenarioDrafts = computed(() => scenarioDrafts.value.filter(scenarioDraftIsOpen))
const formalDefinitionCount = computed(() => (
  detail.value.entities.length
  + detail.value.entities.reduce((total, entity) => total + entity.properties.length, 0)
  + detail.value.relations.length
  + detail.value.mappings.length
  + detail.value.relation_mappings.length
  + detail.value.functions.length
  + detail.value.actions.length
  + detail.value.rules.length
  + detail.value.events.length
  + detail.value.workflows.length
))

function scenarioDraftsOf(...kinds: string[]) {
  const accepted = new Set(kinds)
  return openScenarioDrafts.value.filter((item) => accepted.has(item.resource_kind))
}

function scenarioDraftDisplayId(item: ScenarioModelDraftResource) {
  return `ai-draft:${item.resource_kind}:${item.id}`
}

function scenarioDraftRow(item: ScenarioModelDraftResource): InlineScenarioDraftRow {
  const payload = draftPayload(item)
  const entityReference = payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name
  const dataSourceReference = payload.data_source_id || payload.data_source_ref || payload.data_source || payload.data_source_name
  return {
    ...payload,
    id: scenarioDraftDisplayId(item),
    name: String(payload.name || payload.display_name || item.title || scenarioDraftKindLabel(item.resource_kind)),
    description: String(payload.description || ''),
    entity_id: draftEntityId(entityReference),
    data_source_id: draftDataSourceId(dataSourceReference),
    table_name: String(payload.table_name || payload.table || ''),
    column_map: cloneForForm(payload.column_map || payload.field_mappings || {}),
    input_schema: cloneForForm(payload.input_schema || {}),
    output_schema: cloneForForm(payload.output_schema || {}),
    payload_schema: cloneForForm(payload.payload_schema || {}),
    runtime_kind: 'contract',
    executor_type: String(payload.executor_type || 'unbound'),
    status: 'draft',
    enabled: false,
    publishable: false,
    _entityLabel: String(payload.entity_name || draftRefToken(entityReference) || '—'),
    _sourceLabel: String(payload.source_label || payload.data_source_name || draftRefToken(dataSourceReference) || '—'),
    _isAiDraft: true,
    _scenarioDraft: item,
  }
}

const objectMappingRows = computed<Record<string, any>[]>(() => [
  ...scenarioDraftsOf('mapping', 'data_mapping', 'conceptual_mapping').map(scenarioDraftRow),
  ...detail.value.mappings,
])
const relationMappingRows = computed<Record<string, any>[]>(() => [
  ...scenarioDraftsOf('relation_mapping').map((item) => {
    const row = scenarioDraftRow(item)
    const payload = row._scenarioDraft.payload || {}
    return {
      ...row,
      relation_name: String(payload.relation_name || payload.name || item.title || '关系映射'),
      source_entity_name: String(payload.source_entity_name || draftRefToken(payload.source_entity_ref || payload.source_entity_id) || '—'),
      target_entity_name: String(payload.target_entity_name || draftRefToken(payload.target_entity_ref || payload.target_entity_id) || '—'),
      data_source_name: row._sourceLabel,
      mode: String(payload.mode || 'foreign_key'),
    }
  }),
  ...detail.value.relation_mappings,
])
const functionRows = computed<Record<string, any>[]>(() => [...scenarioDraftsOf('function').map(scenarioDraftRow), ...detail.value.functions])
const actionRows = computed<Record<string, any>[]>(() => [...scenarioDraftsOf('action').map(scenarioDraftRow), ...detail.value.actions])
const ruleRows = computed<Record<string, any>[]>(() => [...scenarioDraftsOf('rule').map(scenarioDraftRow), ...detail.value.rules])
const eventRows = computed<Record<string, any>[]>(() => [...scenarioDraftsOf('event').map(scenarioDraftRow), ...detail.value.events])
const workflowRows = computed<Record<string, any>[]>(() => [...scenarioDraftsOf('workflow').map(scenarioDraftRow), ...detail.value.workflows])

function draftEntityGraphNodeId(value: unknown) {
  const formalId = draftEntityId(value)
  if (formalId) return formalId
  const draft = referencedScenarioEntityDraft(value)
  return draft ? scenarioDraftDisplayId(draft) : ''
}

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
  for (const [index, draft] of scenarioDraftsOf('entity').entries()) {
    const payload = draftPayload(draft)
    nodes.push({
      id: scenarioDraftDisplayId(draft),
      label: String(payload.name || draft.title || '对象类型'),
      type: 'entity',
      color: visualColor(String(payload.color || ''), detail.value.entities.length + index),
      meta: {
        aiDraft: draft,
        count: Array.isArray(payload.properties) ? payload.properties.length : 0,
        abstract: payload.is_abstract === true,
        description: String(payload.description || ''),
        colorIndex: detail.value.entities.length + index,
      },
    })
  }
  for (const draft of scenarioDraftsOf('property')) {
    const payload = draftPayload(draft)
    const parent = draftEntityGraphNodeId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name)
    const nodeId = scenarioDraftDisplayId(draft)
    nodes.push({
      id: nodeId,
      label: String(payload.name || draft.title || '属性'),
      type: 'property',
      color: visualColor(undefined, nodes.length),
      meta: { aiDraft: draft, count: 0, subtype: '属性' },
    })
    if (parent) {
      edges.push({ id: `${nodeId}:parent`, source: parent, target: nodeId, label: '属性', type: 'belongs' })
    }
  }
  for (const draft of scenarioDraftsOf('relation')) {
    const payload = draftPayload(draft)
    const source = draftEntityGraphNodeId(payload.source_entity_id || payload.source_ref || payload.source_entity || payload.source_entity_name)
    const target = draftEntityGraphNodeId(payload.target_entity_id || payload.target_ref || payload.target_entity || payload.target_entity_name)
    if (source && target) {
      edges.push({
        id: scenarioDraftDisplayId(draft),
        source,
        target,
        label: String(payload.name || draft.title || '关系'),
        type: String(payload.relation_type || payload.cardinality || '1:N'),
        meta: { aiDraft: draft },
      } as GraphEdge)
    } else {
      nodes.push({
        id: scenarioDraftDisplayId(draft),
        label: String(payload.name || draft.title || '关系'),
        type: 'relation',
        color: visualColor(undefined, nodes.length),
        meta: { aiDraft: draft, count: 0, subtype: '关系' },
      })
    }
  }
  return { nodes, edges }
})
const instanceGraph = computed<GraphData>(() => {
  const ents = (instFilter.value ? detail.value.entities.filter((e) => e.id === instFilter.value) : detail.value.entities).filter((e) => e.id)
  const entIds = new Set(ents.map((e) => e.id as string))
  // The explorer can keep paging, but the canvas stays a bounded overview so
  // a very large runtime dataset never turns the graph into a second bulk UI.
  const loadedInstances = objectItems.value.slice(0, 200)
  const nodes: GraphNode[] = []
  const entNode = new Map<string, string>()
  for (const [index, e] of ents.entries()) {
    const id = `ent:${e.id}`
    entNode.set(e.id as string, id)
    nodes.push({ id, label: e.name, type: 'entity', color: visualColor(e.color, index), meta: { count: loadedInstances.filter((i) => i.entity_id === e.id).length, entity_name: e.name } })
  }
  for (const i of loadedInstances) {
    if (!i.id || !entIds.has(i.entity_id)) continue
    const entityIndex = detail.value.entities.findIndex((e) => e.id === i.entity_id)
    const entity = detail.value.entities.find((e) => e.id === i.entity_id)
    nodes.push({ id: i.id, label: i.name, type: 'instance', color: visualColor(entity?.color, Math.max(0, entityIndex)), meta: { entity: i.entity_id, entity_name: entity?.name || '未分类' } })
  }
  for (const draft of scenarioDraftsOf('instance')) {
    const payload = draftPayload(draft)
    const entityReference = payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name
    const entityId = draftEntityId(entityReference)
    if (instFilter.value && entityId !== instFilter.value) continue
    const entity = detail.value.entities.find((item) => item.id === entityId)
    const nodeId = scenarioDraftDisplayId(draft)
    nodes.push({
      id: nodeId,
      label: String(payload.name || payload.display_name || draft.title || '对象实例'),
      type: 'instance',
      color: visualColor(entity?.color, Math.max(0, detail.value.entities.findIndex((item) => item.id === entityId))),
      meta: {
        aiDraft: draft,
        entity: entityId,
        entity_name: entity?.name || draftRefToken(entityReference) || '实例节点',
      },
    })
  }
  const edges: GraphEdge[] = []
  for (const i of loadedInstances) {
    if (!i.id || !entIds.has(i.entity_id)) continue
    edges.push({ id: `ie:${i.id}`, source: entNode.get(i.entity_id)!, target: i.id, type: 'belongs' })
  }
  for (const draft of scenarioDraftsOf('instance')) {
    const payload = draftPayload(draft)
    const entityId = draftEntityId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name)
    const nodeId = scenarioDraftDisplayId(draft)
    if (nodes.some((node) => node.id === nodeId) && entityId && entNode.has(entityId)) {
      edges.push({ id: `${nodeId}:entity`, source: entNode.get(entityId)!, target: nodeId, type: 'belongs' })
    }
  }
  const nodeIds = new Set(nodes.map((n) => n.id))
  for (const ri of relationInstanceRows.value) {
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
const hasMoreObjects = computed(() => !objectFilterPending.value && objectHasMore.value)
function objectTotalLabel() {
  return objectTotalIsExact.value ? String(objectTotal.value) : `约 ${objectTotal.value}`
}
const objectResultStatus = computed(() => {
  if (objectLoading.value) return '正在加载对象列表…'
  if (objectLoadingMore.value) return `正在加载更多对象；已加载 ${objectItems.value.length} / ${objectTotalLabel()} 个结果`
  if (objectFilterPending.value) return `当前显示 ${objectItems.value.length} / ${objectTotalLabel()} 个结果；搜索条件已变更，按回车应用`
  if (!objectTotal.value) return objectQuery.value.trim() ? '没有匹配对象' : '暂无可浏览对象'
  return hasMoreObjects.value
    ? `已加载 ${objectItems.value.length} / ${objectTotalLabel()} 个结果，可继续加载更多`
    : objectTotalIsExact.value ? `已加载全部 ${objectTotal.value} 个结果` : `已加载当前可见结果`
})

function entName(id: string) { return detail.value.entities.find((e) => e.id === id)?.name || '—' }
function entColor(id: string) {
  const index = detail.value.entities.findIndex((e) => e.id === id)
  return visualColor(detail.value.entities[index]?.color, Math.max(0, index))
}
function dsName(id: string) { return dataSources.value.find((d) => d.id === id)?.name || '—' }
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
    objectNextOffset.value = result.next_offset ?? (result.offset + result.items.length)
    objectHasMore.value = Boolean(result.has_more)
    objectTotalIsExact.value = result.total_is_exact !== false
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
    objectNextOffset.value = result.next_offset ?? Math.max(objectNextOffset.value, result.offset + result.items.length)
    objectHasMore.value = Boolean(result.has_more)
    objectTotalIsExact.value = result.total_is_exact !== false
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

// ── 选择 → 打开悬浮编辑器 ──
function onNodeSelect(node: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: node.id, kind: tab.value === 'instances' ? 'instance' : 'entity', label: node.label || node.name || node.id },
  }))
  if (!canWrite.value) return
  if (node.meta?.aiDraft) {
    void startEditingScenarioDraft(node.meta.aiDraft)
    return
  }
  if (tab.value === 'instances') openInstance(node.id)
  else openEntity(node.id)
}
function onInstSelect(node: any) {
  window.dispatchEvent(new CustomEvent('ontology-selection-change', {
    detail: { id: node.id, kind: node.id.startsWith('ent:') ? 'entity' : 'instance', label: node.label || node.name || node.id },
  }))
  if (node.meta?.aiDraft) {
    if (canWrite.value) void startEditingScenarioDraft(node.meta.aiDraft)
    return
  }
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
  if (edge.meta?.aiDraft) {
    if (canWrite.value) void startEditingScenarioDraft(edge.meta.aiDraft)
    return
  }
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
  draftPropertyEditorIndex.value = null
  clearActiveScenarioDraftPromotion()
  selectedObjectId.value = null
  objectDetail.value = null
  window.dispatchEvent(new CustomEvent('ontology-selection-change', { detail: {} }))
}
function closeEditor() {
  editor.value = null
  draftPropertyEditorIndex.value = null
  clearActiveScenarioDraftPromotion()
}

// ── 打开编辑器 ──
function openEntity(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  draftPropertyEditorIndex.value = null
  const e = id ? detail.value.entities.find((x) => x.id === id) : null
  editor.value = {
    kind: 'entity',
    id: e?.id,
    form: e
      ? { name: e.name, api_name: e.api_name || '', namespace: e.namespace || '', color: e.color, description: e.description, is_abstract: e.is_abstract, state_property: e.state_property || '', properties: e.properties.map((p) => ({ ...cloneForForm(p), _apiNameLocked: true })) }
      : { name: '', api_name: '', namespace: '', color: graphPalette[0], description: '', is_abstract: false, state_property: '', properties: [] },
  }
}
function openRelation(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  draftPropertyEditorIndex.value = null
  const r = id ? detail.value.relations.find((x) => x.id === id) : null
  editor.value = {
    kind: 'relation',
    id: r?.id,
    form: r
      ? { name: r.name, namespace: r.namespace || '', source_entity_id: r.source_entity_id, target_entity_id: r.target_entity_id, relation_type: r.relation_type, description: r.description, constraints: cloneForForm(r.constraints || {}) }
      : { name: '', namespace: '', source_entity_id: detail.value.entities[0]?.id || '', target_entity_id: detail.value.entities[1]?.id || detail.value.entities[0]?.id || '', relation_type: '1:N', description: '', constraints: {} },
  }
}
function openInstance(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  draftPropertyEditorIndex.value = null
  const i = id ? objectItems.value.find((x) => x.id === id) : null
  editor.value = {
    kind: 'instance',
    id: i?.id,
    form: i
      ? { entity_id: i.entity_id, name: i.name, attributes: cloneForForm(i.attributes || {}) }
      : { entity_id: instFilter.value || detail.value.entities[0]?.id || '', name: '', attributes: {} },
  }
}

const relationInstanceDlg = ref(false)
const relationInstanceSaving = ref(false)
const relationInstanceForm = ref<Partial<RelationInstance>>({ relation_id: '', source_instance_id: '', target_instance_id: '', attributes: {} })
const selectedRelationDefinition = computed(() => detail.value.relations.find((relation) => relation.id === relationInstanceForm.value.relation_id))
const relationSourceInstances = computed(() => objectItems.value.filter((instance) => instance.entity_id === selectedRelationDefinition.value?.source_entity_id))
const relationTargetInstances = computed(() => objectItems.value.filter((instance) => instance.entity_id === selectedRelationDefinition.value?.target_entity_id))
const canCreateRelationInstance = computed(() => Boolean(
  relationInstanceForm.value.relation_id
  && relationInstanceForm.value.source_instance_id
  && relationInstanceForm.value.target_instance_id,
))
function resetRelationInstanceEndpoints() {
  relationInstanceForm.value.source_instance_id = relationSourceInstances.value[0]?.id || ''
  relationInstanceForm.value.target_instance_id = relationTargetInstances.value[0]?.id || ''
  relationInstanceForm.value.attributes = {}
}
async function loadRelationInstances() {
  const requestId = ++relationInstanceRequestId
  relationInstancesLoading.value = true
  try {
    const result = await api.listRelationInstances(sid, { limit: 100, offset: 0 })
    if (requestId !== relationInstanceRequestId || objectSearchViewDisposed) return
    relationInstanceRows.value = result.items || []
    relationInstanceTotal.value = Number(result.total || 0)
    relationInstanceTotalIsExact.value = result.total_is_exact !== false
  } catch (e: any) {
    if (requestId === relationInstanceRequestId && !objectSearchViewDisposed) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '关系实例加载失败')
    }
  } finally {
    if (requestId === relationInstanceRequestId) relationInstancesLoading.value = false
  }
}
function openRelationInstanceManager() {
  if (!canWrite.value) return
  relationInstanceForm.value = {
    relation_id: detail.value.relations[0]?.id || '',
    source_instance_id: '',
    target_instance_id: '',
    attributes: {},
  }
  resetRelationInstanceEndpoints()
  relationInstanceDlg.value = true
  void loadRelationInstances()
}
async function saveRelationInstance() {
  if (!canWrite.value || !canCreateRelationInstance.value) return
  relationInstanceSaving.value = true
  try {
    await api.createRelationInstance(sid, {
      relation_id: relationInstanceForm.value.relation_id,
      source_instance_id: relationInstanceForm.value.source_instance_id,
      target_instance_id: relationInstanceForm.value.target_instance_id,
      attributes: { ...(relationInstanceForm.value.attributes || {}) },
    })
    await load()
    resetRelationInstanceEndpoints()
    ElMessage.success('关系实例已添加')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '添加关系实例失败')
  } finally {
    relationInstanceSaving.value = false
  }
}
async function removeRelationInstance(row: RelationInstance) {
  if (!canWrite.value || !row.id) return
  try {
    await ElMessageBox.confirm(`删除“${row.source_instance_name || '来源对象'} → ${row.target_instance_name || '目标对象'}”这条关系？`, '确认删除', { type: 'warning' })
    await api.deleteRelationInstance(row.id)
    await load()
    ElMessage.success('关系实例已删除')
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}

// ── 保存 / 删除 ──
async function saveEditor() {
  if (!canWrite.value || !editor.value) return
  const { kind, id, form } = editor.value
  const promotion = claimScenarioDraftPromotionSave(kind)
  if (!promotion.allowed) return
  saving.value = true
  try {
    let saved: any
    if (kind === 'entity') {
      const entityPayload = {
        ...form,
        properties: (form.properties || []).map((property: any) => {
          const propertyPayload = { ...property }
          delete propertyPayload._apiNameLocked
          return propertyPayload
        }),
      }
      if (id) saved = await api.updateEntity(id, entityPayload)
      else saved = await api.createEntity(sid, entityPayload)
    } else if (kind === 'relation') {
      if (id) saved = await api.updateRelation(id, form)
      else saved = await api.createRelation(sid, form)
    } else {
      if (id) saved = await api.updateInstance(id, form)
      else saved = await api.createInstance(sid, form)
    }
    const draftResolved = await resolveScenarioDraftAfterFormalSave(kind, saved, id, promotion.draft)
    await load()
    if (id) {
      if (kind === 'entity') openEntity(id)
      else if (kind === 'relation') openRelation(id)
      else openInstance(id)
    } else {
      editor.value = null
    }
    scenarioDraftSaveToast('已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    saving.value = false
    releaseScenarioDraftPromotionSave(promotion)
  }
}
async function deleteEditor() {
  if (!canWrite.value || !editor.value) return
  const { kind, id } = editor.value
  if (!id) { editor.value = null; return }
  const names = { entity: '对象类型', relation: '关系类型', instance: '对象实例' }
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
    preview.columns.map((column, index) => [column, row[index]]),
  ))
})
const mappingTransformedRows = computed<Record<string, unknown>[]>(() => {
  const rows = ((mappingPreview.value as DataMappingPreview & { transformed_rows?: Record<string, any>[] } | null)?.transformed_rows || [])
  return rows
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
function mappingRefreshJob(row: any): DataMappingRefreshJob | undefined {
  return row.id ? mappingRefreshJobs.value[row.id] : undefined
}
function mappingRefreshActive(row: any): boolean {
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
function formatDate(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function openMapping(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const m = id ? detail.value.mappings.find((x) => x.id === id) : null
  mappingForm.value = m
    ? { ...m, data_source_binding_ref: { ...(m.data_source_binding_ref || {}) }, column_map: { ...(m.column_map || {}) } }
    : { entity_id: detail.value.entities[0]?.id, data_source_id: databaseDataSources.value[0]?.id, data_source_binding_key: '', data_source_binding_ref: {}, table_name: '', column_map: {} }
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
  clearActiveScenarioDraftPromotion('mapping')
  mappingTableRequest += 1
  mapTables.value = []
  mapCols.value = []
})
async function saveMapping() {
  if (!canWrite.value) return
  const promotion = claimScenarioDraftPromotionSave('mapping')
  if (!promotion.allowed) return
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
    const draftResolved = await resolveScenarioDraftAfterFormalSave('mapping', saved, replacedMappingId, promotion.draft)
    if (replacedMappingId && replacedMappingId !== saved.id) clearMappingRefreshTracking(replacedMappingId)
    mappingDlg.value = false
    await load()
    scenarioDraftSaveToast('已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    releaseScenarioDraftPromotionSave(promotion)
  }
}
async function doPreviewMapping(row: any) {
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

// ── 关系映射：结构化选择 → 服务端预检 → 保存 ──
type SavedDataMapping = DataMapping & { id: string }
function emptyRelationMappingForm(): RelationDataMappingInput {
  return {
    relation_id: '',
    source_mapping_id: '',
    target_mapping_id: '',
    mode: 'source_fk',
    foreign_key_column: '',
    join_data_source_id: '',
    join_table_name: '',
    source_key_column: '',
    target_key_column: '',
  }
}
const relationMappingDlg = ref(false)
const relationMappingEditingId = ref('')
const relationMappingForm = ref<RelationDataMappingInput>(emptyRelationMappingForm())
const relationMappingPreview = ref<RelationDataMappingPreview | null>(null)
const relationMappingPreviewFingerprint = ref('')
const relationMappingPreflighting = ref(false)
const relationMappingSaving = ref(false)
const relationMappingOptionsLoading = ref(false)
const relationMappingTables = ref<TableInfo[]>([])
const relationMappingColumns = ref<string[]>([])
let relationMappingOptionsRequest = 0
let relationMappingPreflightRequest = 0

const selectedRelationMappingRelation = computed<Relation | undefined>(() => (
  detail.value.relations.find((relation) => relation.id === relationMappingForm.value.relation_id)
))
const relationSourceObjectMappings = computed<SavedDataMapping[]>(() => {
  const entityId = selectedRelationMappingRelation.value?.source_entity_id
  return detail.value.mappings.filter((mapping): mapping is SavedDataMapping => Boolean(mapping.id) && mapping.entity_id === entityId)
})
const relationTargetObjectMappings = computed<SavedDataMapping[]>(() => {
  const entityId = selectedRelationMappingRelation.value?.target_entity_id
  return detail.value.mappings.filter((mapping): mapping is SavedDataMapping => Boolean(mapping.id) && mapping.entity_id === entityId)
})
const relationMappingMissingEndpointNames = computed(() => {
  const relation = selectedRelationMappingRelation.value
  if (!relation) return []
  const missing: string[] = []
  if (!relationSourceObjectMappings.value.length) missing.push(`来源对象“${entName(relation.source_entity_id)}”`)
  if (!relationTargetObjectMappings.value.length) missing.push(`目标对象“${entName(relation.target_entity_id)}”`)
  return missing
})
const relationMappingCarrier = computed<SavedDataMapping | undefined>(() => {
  const mappingId = relationMappingForm.value.mode === 'source_fk'
    ? relationMappingForm.value.source_mapping_id
    : relationMappingForm.value.target_mapping_id
  return detail.value.mappings.find((mapping): mapping is SavedDataMapping => mapping.id === mappingId)
})
const relationMappingPayload = computed(() => buildRelationMappingPayload(relationMappingForm.value))
const relationMappingCurrentFingerprint = computed(() => relationMappingPayloadFingerprint(relationMappingForm.value))
const relationMappingMissingFields = computed(() => missingRelationMappingFields(relationMappingPayload.value))
const relationMappingPreviewIsCurrent = computed(() => Boolean(
  relationMappingPreview.value
  && relationMappingPreviewFingerprint.value
  && relationMappingPreviewFingerprint.value === relationMappingCurrentFingerprint.value,
))
const relationMappingCanSave = computed(() => Boolean(
  canWrite.value
  && relationMappingPreviewIsCurrent.value
  && relationMappingPreview.value?.ok
  && !relationMappingMissingFields.value.length
  && !relationMappingSaving.value,
))
const relationMappingStep = computed(() => {
  if (!relationMappingForm.value.relation_id) return 0
  if (relationMappingMissingFields.value.length) return 1
  if (!relationMappingPreviewIsCurrent.value) return 2
  return relationMappingPreview.value?.ok ? 3 : 2
})

function objectMappingOptionLabel(mapping: DataMapping) {
  return `${mapping.data_source_name || dsName(mapping.data_source_id)} / ${mapping.table_name || '未选择表'}`
}
function invalidateRelationMappingPreview() {
  relationMappingPreflightRequest += 1
  relationMappingPreview.value = null
  relationMappingPreviewFingerprint.value = ''
}
function resetRelationMappingFields() {
  relationMappingForm.value.foreign_key_column = ''
  relationMappingForm.value.join_data_source_id = ''
  relationMappingForm.value.join_table_name = ''
  relationMappingForm.value.source_key_column = ''
  relationMappingForm.value.target_key_column = ''
  relationMappingTables.value = []
  relationMappingColumns.value = []
  invalidateRelationMappingPreview()
}
function selectOnlyEndpointMappings() {
  relationMappingForm.value.source_mapping_id = relationSourceObjectMappings.value.length === 1
    ? relationSourceObjectMappings.value[0].id
    : ''
  relationMappingForm.value.target_mapping_id = relationTargetObjectMappings.value.length === 1
    ? relationTargetObjectMappings.value[0].id
    : ''
}
async function loadRelationMappingOptions() {
  const request = ++relationMappingOptionsRequest
  relationMappingOptionsLoading.value = true
  relationMappingTables.value = []
  relationMappingColumns.value = []
  try {
    if (relationMappingForm.value.mode === 'join_table') {
      const dataSourceId = relationMappingForm.value.join_data_source_id
      if (!dataSourceId) return
      const tables = await api.listTables(dataSourceId)
      if (request !== relationMappingOptionsRequest) return
      relationMappingTables.value = tables
      const table = tables.find((item) => item.name === relationMappingForm.value.join_table_name)
      relationMappingColumns.value = table?.columns.map((column) => column.name) || []
      return
    }
    const carrier = relationMappingCarrier.value
    if (!carrier?.data_source_id || !carrier.table_name) return
    const tables = await api.listTables(carrier.data_source_id)
    if (request !== relationMappingOptionsRequest) return
    relationMappingTables.value = tables
    relationMappingColumns.value = tables.find((item) => item.name === carrier.table_name)?.columns.map((column) => column.name) || []
  } catch (error: any) {
    if (request === relationMappingOptionsRequest) ElMessage.error(error?.message || '真实数据表字段加载失败')
  } finally {
    if (request === relationMappingOptionsRequest) relationMappingOptionsLoading.value = false
  }
}
function onRelationMappingRelationChange() {
  selectOnlyEndpointMappings()
  resetRelationMappingFields()
  void loadRelationMappingOptions()
}
function onRelationMappingEndpointChange() {
  resetRelationMappingFields()
  void loadRelationMappingOptions()
}
function onRelationMappingModeChange() {
  resetRelationMappingFields()
  void loadRelationMappingOptions()
}
function onRelationJoinDataSourceChange() {
  relationMappingForm.value.join_table_name = ''
  relationMappingForm.value.source_key_column = ''
  relationMappingForm.value.target_key_column = ''
  invalidateRelationMappingPreview()
  void loadRelationMappingOptions()
}
function onRelationJoinTableChange() {
  relationMappingForm.value.source_key_column = ''
  relationMappingForm.value.target_key_column = ''
  invalidateRelationMappingPreview()
  const table = relationMappingTables.value.find((item) => item.name === relationMappingForm.value.join_table_name)
  relationMappingColumns.value = table?.columns.map((column) => column.name) || []
}
async function openRelationMapping(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const existing = id ? detail.value.relation_mappings.find((mapping) => mapping.id === id) : undefined
  relationMappingEditingId.value = existing?.id || ''
  relationMappingForm.value = existing
    ? {
        relation_id: existing.relation_id,
        source_mapping_id: existing.source_mapping_id,
        target_mapping_id: existing.target_mapping_id,
        mode: existing.mode,
        foreign_key_column: existing.foreign_key_column || '',
        join_data_source_id: existing.mode === 'join_table' ? existing.data_source_id : '',
        join_table_name: existing.mode === 'join_table' ? existing.table_name : '',
        source_key_column: existing.source_key_column || '',
        target_key_column: existing.target_key_column || '',
      }
    : emptyRelationMappingForm()
  if (!existing) {
    const firstUnmapped = detail.value.relations.find((relation) => (
      relation.id && !detail.value.relation_mappings.some((mapping) => mapping.relation_id === relation.id)
    ))
    relationMappingForm.value.relation_id = firstUnmapped?.id || detail.value.relations[0]?.id || ''
    selectOnlyEndpointMappings()
  }
  invalidateRelationMappingPreview()
  relationMappingDlg.value = true
  await nextTick()
  await loadRelationMappingOptions()
}
function resetRelationMappingDialog() {
  clearActiveScenarioDraftPromotion('relation_mapping')
  relationMappingOptionsRequest += 1
  relationMappingPreflightRequest += 1
  relationMappingEditingId.value = ''
  relationMappingForm.value = emptyRelationMappingForm()
  relationMappingPreview.value = null
  relationMappingPreviewFingerprint.value = ''
  relationMappingTables.value = []
  relationMappingColumns.value = []
  relationMappingOptionsLoading.value = false
  relationMappingPreflighting.value = false
}
function openMappingForEntity(entityId: string) {
  relationMappingDlg.value = false
  openMapping()
  mappingForm.value.entity_id = entityId
  onMappingEntityChange()
}
async function preflightRelationMapping(): Promise<boolean> {
  if (!canWrite.value) return false
  const payload = relationMappingPayload.value
  const missing = missingRelationMappingFields(payload)
  if (missing.length) {
    ElMessage.warning(`请先完成：${missing.join('、')}`)
    return false
  }
  const fingerprint = relationMappingPayloadFingerprint(payload)
  const request = ++relationMappingPreflightRequest
  relationMappingPreflighting.value = true
  try {
    const preview = await api.preflightRelationMapping(sid, payload)
    if (request !== relationMappingPreflightRequest || fingerprint !== relationMappingCurrentFingerprint.value) return false
    relationMappingPreview.value = preview
    relationMappingPreviewFingerprint.value = fingerprint
    if (preview.ok) ElMessage.success('预检通过，可以保存关系映射')
    else ElMessage.warning('预检发现阻塞问题，请按提示修正')
    return preview.ok
  } catch (error: any) {
    if (request === relationMappingPreflightRequest) {
      relationMappingPreview.value = null
      relationMappingPreviewFingerprint.value = ''
      ElMessage.error(error?.message || '关系映射预检失败')
    }
    return false
  } finally {
    if (request === relationMappingPreflightRequest) relationMappingPreflighting.value = false
  }
}
async function saveRelationMapping() {
  if (!relationMappingCanSave.value) return
  const promotion = claimScenarioDraftPromotionSave('relation_mapping')
  if (!promotion.allowed) return
  relationMappingSaving.value = true
  try {
    // Re-run the server preflight immediately before the write so a stale UI
    // success cannot bypass changed mappings, tables or columns.
    if (!await preflightRelationMapping()) return
    const payload = relationMappingPayload.value
    const saved = relationMappingEditingId.value
      ? await api.updateRelationMapping(relationMappingEditingId.value, payload)
      : await api.createRelationMapping(sid, payload)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('relation_mapping', saved, relationMappingEditingId.value, promotion.draft)
    relationMappingDlg.value = false
    await load()
    scenarioDraftSaveToast('关系映射已保存', draftResolved)
  } catch (error: any) {
    ElMessage.error(error?.message || '关系映射保存失败')
  } finally {
    relationMappingSaving.value = false
    releaseScenarioDraftPromotionSave(promotion)
  }
}
async function preflightSavedRelationMapping(row: any) {
  if (!canWrite.value) return
  await openRelationMapping(row.id)
  await preflightRelationMapping()
}
async function removeRelationMapping(row: any) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm(`删除关系“${row.relation_name}”的数据映射？系统会同时删除由该映射自动生成的关系链接，手工创建的关系不受影响。`, '确认删除', { type: 'warning' })
    await api.deleteRelationMapping(row.id)
    await load()
    ElMessage.success('关系映射已删除')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || '关系映射删除失败')
  }
}

// ── 业务函数：闭集、无副作用的确定性运行方式 ──
type FunctionForm = {
  id?: string
  name: string
  description: string
  tags_text: string
  visibility: 'scenario' | 'tenant'
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  runtime_kind: string
  runtime_config: Record<string, any>
}

const emptyFunctionSchema = (): Record<string, unknown> => ({
  type: 'object', properties: {}, additionalProperties: false,
})
const functionDlg = ref(false)
const functionSaving = ref(false)
const functionForm = ref<FunctionForm>({
  name: '', description: '', tags_text: '', visibility: 'scenario',
  input_schema: emptyFunctionSchema(), output_schema: emptyFunctionSchema(),
  runtime_kind: 'contract', runtime_config: {},
})
const functionInputFields = computed(() => {
  const root = actionSchemaRoot(functionForm.value.input_schema)
  return Object.entries(root.properties).map(([name, schema]: [string, any]) => ({ name, type: String(schema?.type || 'string') }))
})
const functionNumericFields = computed(() => functionInputFields.value.filter((field) => ['number', 'integer'].includes(field.type)))
function functionRuntimeLabel(kind?: string) {
  return ({
    contract: '仅定义（不可调用）', weighted_score: '加权评分', threshold: '阈值判断',
    geo_distance: '地理距离', timeseries_aggregate: '时序聚合',
  } as Record<string, string>)[kind || 'contract'] || kind || '仅定义（不可调用）'
}
function runtimeSchema(properties: Record<string, any>, required: string[] = Object.keys(properties)) {
  return { type: 'object', properties, required, additionalProperties: false }
}
function resetFunctionRuntime(kind: string) {
  if (kind === 'weighted_score') {
    functionForm.value.runtime_config = { weights: {}, bias: 0 }
    functionForm.value.output_schema = runtimeSchema({ score: { type: 'number', description: '加权计算结果' } })
  } else if (kind === 'threshold') {
    functionForm.value.runtime_config = { field: functionNumericFields.value[0]?.name || '', operator: '>=', threshold: 0 }
    functionForm.value.output_schema = runtimeSchema({ matched: { type: 'boolean', description: '是否命中阈值' }, value: { type: 'number' }, threshold: { type: 'number' } })
  } else if (kind === 'geo_distance') {
    functionForm.value.runtime_config = { unit: 'km' }
    functionForm.value.input_schema = runtimeSchema({ origin: { type: 'array', description: '起点坐标：[经度, 纬度]' }, target: { type: 'array', description: '终点坐标：[经度, 纬度]' } })
    functionForm.value.output_schema = runtimeSchema({ distance: { type: 'number', description: '两点距离' }, unit: { type: 'string', description: '距离单位' } })
  } else if (kind === 'timeseries_aggregate') {
    functionForm.value.runtime_config = { aggregation: 'avg', value_field: 'value' }
    functionForm.value.input_schema = runtimeSchema({ values: { type: 'array', description: '待聚合的数值列表' } })
    functionForm.value.output_schema = runtimeSchema({ aggregation: { type: 'string' }, value: { type: 'number' }, count: { type: 'integer' } })
  } else {
    functionForm.value.runtime_config = {}
  }
}
function openFunction(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const fn = id ? detail.value.functions.find((item) => item.id === id) : null
  functionForm.value = fn
    ? {
        id: fn.id,
        name: fn.name,
        description: fn.description || '',
        tags_text: (fn.tags || []).join(', '),
        visibility: fn.visibility === 'tenant' ? 'tenant' : 'scenario',
        input_schema: cloneForForm(fn.input_schema || emptyFunctionSchema()),
        output_schema: cloneForForm(fn.output_schema || emptyFunctionSchema()),
        runtime_kind: fn.runtime_kind || 'contract',
        runtime_config: cloneForForm(fn.runtime_config || {}),
      }
    : {
        name: '', description: '', tags_text: '', visibility: 'scenario',
        input_schema: emptyFunctionSchema(), output_schema: emptyFunctionSchema(),
        runtime_kind: 'contract', runtime_config: {},
      }
  functionDlg.value = true
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
      input_schema: functionForm.value.input_schema,
      output_schema: functionForm.value.output_schema,
      runtime_kind: functionForm.value.runtime_kind,
      runtime_config: functionForm.value.runtime_config,
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '函数声明格式错误')
    return
  }
  const promotion = claimScenarioDraftPromotionSave('function')
  if (!promotion.allowed) return
  functionSaving.value = true
  try {
    const saved = functionForm.value.id
      ? await api.updateFunction(functionForm.value.id, payload)
      : await api.createFunction(sid, payload)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('function', saved, functionForm.value.id, promotion.draft)
    functionDlg.value = false
    await load()
    scenarioDraftSaveToast('函数已保存', draftResolved)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '函数保存失败')
  } finally {
    functionSaving.value = false
    releaseScenarioDraftPromotionSave(promotion)
  }
}
async function removeFunction(id?: string) {
  if (!canWrite.value || !id) return
  try {
    await ElMessageBox.confirm('确定删除该函数？正在使用或已固定的场景定义可能会阻止删除。', '删除函数', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await api.deleteFunction(id)
    await load()
    ElMessage.success('函数已删除')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '函数删除失败')
  }
}
async function doRunFunction(row: FunctionDefinition) {
  if (!row.id || row.runtime_kind === 'contract') return ElMessage.warning('该函数尚未选择可运行的计算方式')
  try {
    const params = await promptParams(schemaValueTemplate(row.input_schema || {}), `运行函数：${row.name}`)
    const run = await api.runFunction(row.id, { params, idempotency_key: `function-${row.id}-${Date.now()}` })
    execResult.value = run.status === 'succeeded'
      ? run.output_payload || {}
      : { status: run.status, error: run.error || '函数运行失败' }
    execResultDlg.value = true
    if (run.status !== 'succeeded') ElMessage.error(run.error || '函数运行失败')
  } catch (error: any) {
    if (error !== 'cancel' && error?.message !== 'cancel') ElMessage.error(error?.response?.data?.detail || error?.message || '函数运行失败')
  }
}

// ── 操作（Actions）──
const actionDlg = ref(false)
const actionForm = ref<any>({ executor_type: 'sql', executor_config: {}, input_schema: emptyFunctionSchema() })
const actionPrecondition = ref<Record<string, any>>({})
const actionPostcondition = ref<Record<string, any>>({})
const actionLegacyConditions = ref<string[]>([])
const actionTemplates = ref<ArtifactTemplate[]>([])
const actionTemplatesLoading = ref(false)
const actionTemplatesError = ref('')
const originalActionTemplateId = ref('')
const actionExecuteDlg = ref(false)
const actionExecuteRow = ref<any>(null)
const actionParamsForm = ref<Record<string, any>>({})
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

function cloneActionJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function actionSchemaRoot(schema: any): { properties: Record<string, any>; required: string[] } {
  if (!schema || typeof schema !== 'object') return { properties: {}, required: [] }
  if (schema.properties && typeof schema.properties === 'object') {
    return { properties: schema.properties, required: Array.isArray(schema.required) ? schema.required : [] }
  }
  return { properties: schema, required: [] }
}
function actionExecutorLabel(type?: string) {
  return ({ unbound: '待绑定', sql: '数据库查询', template: '模板附件', skill: '本地技能', mcp: '外部工具', http: 'HTTPS 接口', script: '受控脚本' } as Record<string, string>)[type || ''] || '未配置'
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
const actionInputFieldNames = computed(() => Object.keys(actionSchemaRoot(actionForm.value?.input_schema).properties))
const selectedActionTemplate = computed(() => actionTemplates.value.find((template) => template.id === actionForm.value?.executor_config?.template_id) || null)
const hasLegacyTemplateBinding = computed(() => Boolean(
  actionForm.value?.executor_type === 'template'
  && actionForm.value?.executor_config?.template_file_id
  && !actionForm.value?.executor_config?.template_id,
))
const selectedTemplateVariables = computed(() => {
  const configPaths = actionForm.value?.executor_config?.template_variable_paths
  if (Array.isArray(configPaths) && actionForm.value?.executor_config?.template_version) return configPaths
  return selectedActionTemplate.value?.current_version?.placeholder_paths || []
})
const usesOlderTemplateVersion = computed(() => {
  const pinned = Number(actionForm.value?.executor_config?.template_version || 0)
  const current = Number(selectedActionTemplate.value?.current_version?.version || 0)
  return Boolean(pinned && current && pinned !== current)
})
const boundTemplateVersionLabel = computed(() => {
  const pinned = Number(actionForm.value?.executor_config?.template_version || 0)
  if (pinned) return usesOlderTemplateVersion.value ? `固定 v${pinned}` : `v${pinned}（已固定）`
  return templateVersionLabel(selectedActionTemplate.value)
})
function parseActionCondition(value: unknown, label: string): Record<string, any> {
  if (value === null || value === undefined || value === '') return {}
  if (typeof value === 'object' && !Array.isArray(value)) return cloneActionJson(value as Record<string, any>)
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
    } catch { /* 旧自然语言条件由下方阻塞提示处理。 */ }
    actionLegacyConditions.value.push(`${label}：${value}`)
  }
  return {}
}
function clearLegacyActionConditions() {
  actionLegacyConditions.value = []
}
function serializedActionCondition(value: Record<string, any>) {
  return Object.keys(value || {}).length ? JSON.stringify(value) : ''
}
function createIdempotencyKey() {
  const cryptoApi = globalThis.crypto as Crypto | undefined
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID()
  return `action-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}
function emptyExecutorConfig(type: string) {
  if (type === 'sql') return { data_source_id: '', sql: '' }
  if (type === 'skill') return { skill_id: '' }
  if (type === 'mcp') return { mcp_id: '', tool_name: '' }
  if (type === 'http') return { method: 'GET', url: '', headers: {} }
  if (type === 'template') return { template_id: '', target_data_source_id: '', output_filename: '' }
  return {}
}
function resetActionExecutorConfig(type: string) {
  actionForm.value.executor_config = emptyExecutorConfig(type)
  originalActionTemplateId.value = ''
  if (type === 'template') {
    actionForm.value.requires_confirmation = true
    actionForm.value.idempotency_required = true
    actionForm.value.executor_config.target_data_source_id = writableFileBucketSources.value[0]?.id || ''
    void loadActionTemplates()
  }
}
function templateVersionLabel(template?: ArtifactTemplate | null) {
  return template?.current_version ? `v${template.current_version.version}（当前）` : '无可用版本'
}
function scenarioTemplateScope(template: ArtifactTemplate) {
  return template.scenario_id ? '当前场景模板' : '租户共享模板'
}
async function loadActionTemplates() {
  actionTemplatesLoading.value = true
  actionTemplatesError.value = ''
  try {
    actionTemplates.value = await api.listTemplates({ scenario_id: sid })
  } catch (error: any) {
    actionTemplatesError.value = error?.message || '模板资源加载失败'
  } finally {
    actionTemplatesLoading.value = false
  }
}
function onActionTemplateChanged(templateId: string) {
  const template = actionTemplates.value.find((item) => item.id === templateId)
  if (!template || templateUnavailableReason(template)) return
  let generatedSchema: Record<string, any>
  try {
    generatedSchema = templatePathsToSchema(template.current_version?.placeholder_paths || [])
  } catch (error: any) {
    actionForm.value.executor_config.template_id = originalActionTemplateId.value || ''
    ElMessage.error(error?.message || '模板变量路径不安全，不能绑定该模板')
    return
  }
  actionForm.value.executor_config = cleanTemplateExecutorConfig(actionForm.value.executor_config, templateId)
  if (!Object.keys(actionSchemaRoot(actionForm.value.input_schema).properties).length) {
    actionForm.value.input_schema = generatedSchema
  }
}
async function syncActionSchemaFromTemplate() {
  if (!selectedActionTemplate.value) return
  let generatedSchema: Record<string, any>
  try {
    generatedSchema = templatePathsToSchema(selectedActionTemplate.value.current_version?.placeholder_paths || [])
  } catch (error: any) {
    ElMessage.error(error?.message || '模板变量路径不安全，不能同步输入参数')
    return
  }
  if (Object.keys(actionSchemaRoot(actionForm.value.input_schema).properties).length) {
    try {
      await ElMessageBox.confirm('这会用当前模板变量替换已有输入参数定义，是否继续？', '同步模板变量', {
        type: 'warning', confirmButtonText: '替换并同步', cancelButtonText: '取消',
      })
    } catch { return }
  }
  actionForm.value.input_schema = generatedSchema
  ElMessage.success('模板变量已同步到输入参数')
}
function useCurrentTemplateVersion() {
  if (!selectedActionTemplate.value) return
  actionForm.value.executor_config = cleanTemplateExecutorConfig(
    actionForm.value.executor_config,
    selectedActionTemplate.value.id,
  )
}
function openAction(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const a = id ? detail.value.actions.find((x) => x.id === id) : null
  actionForm.value = a
    ? {
        ...a,
        executor_config: { ...emptyExecutorConfig(a.executor_type || 'sql'), ...cloneActionJson(a.executor_config || {}) },
        input_schema: cloneActionJson(a.input_schema || emptyFunctionSchema()),
      }
    : {
        entity_id: detail.value.entities[0]?.id || '', name: '', description: '', executor_type: 'sql',
        executor_config: { data_source_id: '', sql: '' }, input_schema: emptyFunctionSchema(), enabled: true,
        requires_confirmation: true, idempotency_required: true, permission_scope: 'scenario',
      }
  actionLegacyConditions.value = []
  actionPrecondition.value = parseActionCondition(actionForm.value.precondition, '执行前条件')
  actionPostcondition.value = parseActionCondition(actionForm.value.postcondition, '执行后校验')
  originalActionTemplateId.value = actionForm.value.executor_type === 'template'
    ? String(actionForm.value.executor_config.template_id || '')
    : ''
  if (actionForm.value.executor_type === 'template') void loadActionTemplates()
  actionDlg.value = true
}
async function saveAction() {
  if (!canWrite.value) return
  if (actionLegacyConditions.value.length) return ElMessage.warning('请先重新配置或明确清除旧的自然语言条件')
  const f = {
    ...actionForm.value,
    executor_config: cloneActionJson(actionForm.value.executor_config || {}),
    input_schema: cloneActionJson(actionForm.value.input_schema || emptyFunctionSchema()),
    precondition: serializedActionCondition(actionPrecondition.value),
    postcondition: serializedActionCondition(actionPostcondition.value),
  }
  if (f.executor_type === 'unbound') return ElMessage.warning('请先选择并配置实际执行方式')
  if (f.executor_type === 'template') {
    const usesManagedTemplate = Boolean(f.executor_config.template_id)
    const keepsLegacyBinding = Boolean(f.id && f.executor_config.template_file_id && !usesManagedTemplate)
    if ((!usesManagedTemplate && !keepsLegacyBinding) || !f.executor_config.target_data_source_id) {
      return ElMessage.warning('请选择模板中心资源和生成附件的保存文件桶')
    }
    const targetBucket = fileBucketSources.value.find((source) => source.id === f.executor_config.target_data_source_id)
    if (targetBucket?.can_write === false) return ElMessage.warning('所选附件文件桶为只读，请改选可写文件桶')
    const selected = actionTemplates.value.find((template) => template.id === f.executor_config.template_id)
    if (selected && templateUnavailableReason(selected) && selected.id !== originalActionTemplateId.value) {
      return ElMessage.warning(templateUnavailableReason(selected))
    }
    if (usesManagedTemplate) {
      f.executor_config = cleanTemplateExecutorConfig(
        f.executor_config,
        f.executor_config.template_id,
        Number(f.executor_config.template_version || 0) || '',
      )
    }
  }
  const promotion = claimScenarioDraftPromotionSave('action')
  if (!promotion.allowed) return
  try {
    const saved = f.id ? await api.updateAction(f.id, f) : await api.createAction(sid, f)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('action', saved, f.id, promotion.draft)
    actionDlg.value = false
    await load()
    scenarioDraftSaveToast('已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    releaseScenarioDraftPromotionSave(promotion)
  }
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
    actionParamsForm.value[field.name] = defaultValue
  }
  actionExecuteDlg.value = true
}
function buildActionParams(): Record<string, any> {
  const params: Record<string, any> = {}
  for (const field of actionParameterFields.value) {
    const value = actionParamsForm.value[field.name]
    if (value === '' || value === undefined || value === null || (Array.isArray(value) && !value.length)) {
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
    if (res.status === 'idempotent_replay') ElMessage.info('检测到重复提交，已返回原执行结果')
    else if (res.status === 'success') ElMessage.success('操作执行成功')
    else ElMessage.warning(res.error || '操作执行未成功')
    actionExecuteDlg.value = false
    showExecResult(res)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { actionExecuting.value = false }
}

watch(actionParamsForm, () => {
  if (!actionPreviewResult.value) return
  actionPreviewResult.value = null
  actionPreviewParamsSnapshot.value = ''
}, { deep: true })

// ── 规则（Rules）──
const ruleDlg = ref(false)
const ruleSaving = ref(false)
const ruleForm = ref<any>({ condition: {} })
const ruleFieldOptions = computed(() => {
  const entity = detail.value.entities.find((item) => item.id === ruleForm.value.entity_id)
  return (entity?.properties || []).map((property) => property.name).filter(Boolean)
})
function openRule(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const r = id ? detail.value.rules.find((x) => x.id === id) : null
  ruleForm.value = r
    ? { ...r, condition: cloneForForm(r.condition || {}) }
    : { name: '', description: '', entity_id: '', severity: 'warning', condition: {}, action_on_match: '', enabled: true }
  ruleDlg.value = true
}
async function saveRule() {
  if (!canWrite.value || ruleSaving.value) return
  const f = { ...ruleForm.value, condition: cloneForForm(ruleForm.value.condition || {}) }
  const promotion = claimScenarioDraftPromotionSave('rule')
  if (!promotion.allowed) return
  ruleSaving.value = true
  try {
    const saved = f.id ? await api.updateRule(f.id, f) : await api.createRule(sid, f)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('rule', saved, f.id, promotion.draft)
    ruleDlg.value = false
    await load()
    scenarioDraftSaveToast('已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    ruleSaving.value = false
    releaseScenarioDraftPromotionSave(promotion)
  }
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
    const record = await promptParams(ruleRecordTemplate(row.condition), '填写待评估的业务数据')
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
  if (c.value_field) return `${c.field || '?'} ${c.op || ''} 属性「${c.value_field}」`
  const value = Array.isArray(c.value)
    ? c.value.join('、')
    : c.value && typeof c.value === 'object'
      ? '结构化值'
      : c.value ?? ''
  return `${c.field || '?'} ${c.op || ''} ${String(value)}`
}

// ── 事件（Events）──
const eventDlg = ref(false)
const eventForm = ref<any>({ payload_schema: emptyFunctionSchema() })
const publishingEventId = ref<string | null>(null)
function openEvent(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
  const e = id ? detail.value.events.find((x) => x.id === id) : null
  eventForm.value = e
    ? { ...e, payload_schema: cloneForForm(e.payload_schema || emptyFunctionSchema()) }
    : { name: '', description: '', trigger_source: '', payload_schema: emptyFunctionSchema(), enabled: true }
  eventDlg.value = true
}
async function saveEvent() {
  if (!canWrite.value) return
  const f = { ...eventForm.value, payload_schema: cloneForForm(eventForm.value.payload_schema || emptyFunctionSchema()) }
  const promotion = claimScenarioDraftPromotionSave('event')
  if (!promotion.allowed) return
  try {
    const saved = f.id ? await api.updateEvent(f.id, f) : await api.createEvent(sid, f)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('event', saved, f.id, promotion.draft)
    eventDlg.value = false
    await load()
    scenarioDraftSaveToast('已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    releaseScenarioDraftPromotionSave(promotion)
  }
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
function closeWorkflowEditor() {
  wfEditor.value = null
  clearActiveScenarioDraftPromotion('workflow')
}
function openWorkflow(id?: string) {
  if (!canWrite.value) return
  clearActiveScenarioDraftPromotion()
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
  const promotion = claimScenarioDraftPromotionSave('workflow')
  if (!promotion.allowed) return
  try {
    const saved = w.id ? await api.updateWorkflow(w.id, w) : await api.createWorkflow(sid, w)
    const draftResolved = await resolveScenarioDraftAfterFormalSave('workflow', saved, w.id, promotion.draft)
    wfEditor.value = null
    await load()
    scenarioDraftSaveToast('工作流已保存', draftResolved)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '保存失败')
  } finally {
    releaseScenarioDraftPromotionSave(promotion)
  }
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
    const params = await promptParams(workflowParameterTemplate(row), '填写工作流参数')
    const run = await api.submitWorkflowRun(row.id!, params)
    ElMessage.success(run.status === 'awaiting_approval' ? '任务已提交，正在等待审批' : '工作流任务已提交到队列')
    openWorkflowRun(run)
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || e?.message || '执行失败')
  } finally { row._executing = false }
}
async function publishEvent(event: { id?: string; name?: string; enabled?: boolean; payload_schema?: Record<string, any> }) {
  if (!canWrite.value || !event.id || event.enabled === false) return
  try {
    const payload = await promptParams(schemaValueTemplate(event.payload_schema || {}), `发布事件：${event.name || '未命名事件'}`)
    publishingEventId.value = event.id
    const envelope = await api.publishEvent(event.id, { payload })
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
function workflowParameterTemplate(workflow: any) {
  const names = new Set<string>()
  const source = JSON.stringify({ nodes: workflow.nodes || [], steps: workflow.steps || [] })
  for (const match of source.matchAll(/\{\{\s*params\.([a-zA-Z0-9_]+)[^}]*\}\}/g)) names.add(match[1])
  return Object.fromEntries([...names].map((name) => [name, '']))
}
function schemaValueTemplate(schema: any) {
  const root = actionSchemaRoot(schema)
  return Object.fromEntries(Object.entries(root.properties).map(([name, field]: [string, any]) => {
    if (field?.default !== undefined) return [name, field.default]
    if (field?.type === 'boolean') return [name, false]
    if (field?.type === 'number' || field?.type === 'integer') return [name, 0]
    if (field?.type === 'array') return [name, []]
    if (field?.type === 'object') return [name, {}]
    return [name, '']
  }))
}
function ruleRecordTemplate(condition: any): Record<string, any> {
  const fields = new Set<string>()
  const visit = (node: any) => {
    if (!node || typeof node !== 'object') return
    if (node.field) fields.add(String(node.field))
    if (node.value_field) fields.add(String(node.value_field))
    ;(node.conditions || []).forEach(visit)
  }
  visit(condition)
  return Object.fromEntries([...fields].map((field) => [field, '']))
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
const execResult = ref<Record<string, any>>({})
function showExecResult(res: any) {
  execResult.value = res && typeof res === 'object' && !Array.isArray(res) ? res : { result: res }
  execResultDlg.value = true
}
const recordInputDlg = ref(false)
const recordInputTitle = ref('填写参数')
const recordInputValue = ref<Record<string, any>>({})
let recordInputResolve: ((value: Record<string, any>) => void) | null = null
let recordInputReject: ((reason: string) => void) | null = null
function promptParams(initialValue: Record<string, any> | null, title = '填写参数'): Promise<Record<string, any>> {
  if (recordInputReject) recordInputReject('cancel')
  recordInputTitle.value = title
  recordInputValue.value = cloneForForm(initialValue || {})
  recordInputDlg.value = true
  return new Promise((resolve, reject) => {
    recordInputResolve = resolve
    recordInputReject = reject
  })
}
function confirmRecordInput() {
  const resolve = recordInputResolve
  recordInputResolve = null
  recordInputReject = null
  recordInputDlg.value = false
  resolve?.(cloneForForm(recordInputValue.value))
}
function cancelRecordInput() {
  const reject = recordInputReject
  recordInputResolve = null
  recordInputReject = null
  recordInputDlg.value = false
  reject?.('cancel')
}

watch(canWrite, (allowed) => {
  if (allowed) return
  editor.value = null
  mappingDlg.value = false
  relationMappingDlg.value = false
  functionDlg.value = false
  actionDlg.value = false
  actionExecuteDlg.value = false
  ruleDlg.value = false
  eventDlg.value = false
  wfEditor.value = null
})

function scenarioDraftCanEdit(item: ScenarioModelDraftResource) {
  return ['entity', 'property', 'relation', 'instance', 'mapping', 'data_mapping', 'conceptual_mapping', 'relation_mapping', 'function', 'action', 'rule', 'event', 'workflow']
    .includes(String(item.resource_kind || ''))
}

function draftPayload(item: ScenarioModelDraftResource) {
  // ``scenarioDrafts`` is reactive; cloning a proxied payload throws a
  // DataCloneError as soon as an accepted AI draft is projected into a tab.
  // Payloads come from the JSON API, so JSON round-tripping also unwraps any
  // nested proxies that `toRaw` cannot reach.
  const payload = JSON.parse(JSON.stringify(toRaw(item.payload || {}))) as Record<string, any>
  delete payload.id
  delete payload.scenario_id
  delete payload.tenant_id
  delete payload.created_at
  delete payload.updated_at
  return payload
}

function draftRefCandidates(value: unknown) {
  const token = draftRefToken(value)
  if (!token) return []
  const parts = token.split(/[:/.]/).filter(Boolean)
  const tail = parts[parts.length - 1] || token
  return [...new Set([token, tail])]
}

function referencedScenarioEntityDraft(value: unknown) {
  const candidates = draftRefCandidates(value)
  return scenarioDrafts.value.find((item) => item.resource_kind === 'entity' && (
    candidates.includes(item.resource_key)
    || candidates.includes(String(item.payload?.key || ''))
    || candidates.includes(String(item.payload?.id || ''))
  ))
}

function draftEntityId(value: unknown) {
  const candidates = draftRefCandidates(value)
  if (!candidates.length) return ''
  const referencedDraft = referencedScenarioEntityDraft(value)
  const aliases = [...new Set([
    ...candidates,
    String(referencedDraft?.payload?.name || ''),
    String(referencedDraft?.payload?.namespace || ''),
    String(referencedDraft?.title || ''),
  ].filter(Boolean))]
  return detail.value.entities.find((entity) => aliases.some((reference) => (
    entity.id === reference || entity.name === reference || entity.namespace === reference || (entity as any).key === reference
  )))?.id || ''
}

function draftDataSourceId(value: unknown) {
  const candidates = draftRefCandidates(value)
  if (!candidates.length) return ''
  return scenarioDataSources.value.find((source) => candidates.some((reference) => (
    source.id === reference || source.name === reference || source.key === reference || source.binding_key === reference
  )))?.id || ''
}

function scenarioDraftFormalKind(kind: string) {
  if (['mapping', 'data_mapping', 'conceptual_mapping'].includes(kind)) return 'mapping'
  if (kind === 'property') return 'entity'
  return kind
}

function clearActiveScenarioDraftPromotion(kind?: string) {
  const active = activeScenarioDraftPromotion.value
  if (!active || (kind && scenarioDraftFormalKind(active.resource_kind) !== kind)) return
  activeScenarioDraftPromotion.value = null
}

type ScenarioDraftPromotionClaim = {
  allowed: boolean
  draft: ScenarioModelDraftResource | null
}

function claimScenarioDraftPromotionSave(formalKind: string): ScenarioDraftPromotionClaim {
  const draft = activeScenarioDraftPromotion.value
  if (!draft || scenarioDraftFormalKind(draft.resource_kind) !== formalKind) return { allowed: true, draft: null }
  if (scenarioDraftPromotionSyncing.value) return { allowed: false, draft }
  scenarioDraftPromotionSyncing.value = true
  return { allowed: true, draft }
}

function releaseScenarioDraftPromotionSave(claim: ScenarioDraftPromotionClaim) {
  if (claim.draft) scenarioDraftPromotionSyncing.value = false
}

function upsertPendingScenarioDraftResolution(pending: PendingScenarioDraftResolution) {
  const index = pendingScenarioDraftResolutions.value.findIndex((item) => item.draftId === pending.draftId)
  if (index >= 0) pendingScenarioDraftResolutions.value.splice(index, 1, pending)
  else pendingScenarioDraftResolutions.value.push(pending)
}

function removePendingScenarioDraftResolution(draftId: string) {
  pendingScenarioDraftResolutions.value = pendingScenarioDraftResolutions.value.filter((item) => item.draftId !== draftId)
}

function formalResourceId(result: any, fallback = '') {
  return String(result?.id || result?.item?.id || result?.resource?.id || fallback || '').trim()
}

function formalPropertyResourceId(draft: ScenarioModelDraftResource, result: any, fallbackEntityId = '') {
  const entity = result?.item || result?.resource || result
  const properties = Array.isArray(entity?.properties) ? entity.properties : []
  const payload = draft.payload || {}
  const apiName = String(payload.api_name || '').trim()
  const name = String(payload.name || draft.title || '').trim()
  const property = properties.find((item: any) => (
    (apiName && String(item?.api_name || '') === apiName)
    || (name && String(item?.name || '') === name)
  ))
  // Older EntityOut responses omit child IDs. The resolve contract accepts the
  // just-saved parent ID and uniquely matches this draft by api_name/name.
  return String(property?.id || formalResourceId(entity, fallbackEntityId)).trim()
}

async function resolvePendingScenarioDraft(pending: PendingScenarioDraftResolution): Promise<boolean> {
  try {
    const response = await api.resolveScenarioModelDraft(sid, pending.draftId, {
      expected_revision: pending.expectedRevision,
      resolved_resource_id: pending.resolvedResourceId,
    })
    const raw = (response as any)?.item || (response as any)?.draft || response
    const resolved = normalizeScenarioModelDrafts([raw])[0]
    if (resolved) {
      const index = scenarioDrafts.value.findIndex((item) => item.id === resolved.id)
      if (index >= 0) scenarioDrafts.value = scenarioDrafts.value.map((item, itemIndex) => itemIndex === index ? resolved : item)
    }
    removePendingScenarioDraftResolution(pending.draftId)
    await loadScenarioDrafts()
    return true
  } catch (error: any) {
    await loadScenarioDrafts()
    const stillOpen = scenarioDrafts.value.some((item) => item.id === pending.draftId && scenarioDraftIsOpen(item))
    // A timed-out resolve may have committed successfully. The normal list no
    // longer returns resolved drafts, so absence after a successful refresh is
    // the durable confirmation and must not leave a false retry banner.
    if (!scenarioDraftsError.value && !stillOpen) {
      removePendingScenarioDraftResolution(pending.draftId)
      return true
    }
    const status = Number(error?.status || 0)
    const retryable = ![400, 403, 404, 409].includes(status)
    upsertPendingScenarioDraftResolution({
      ...pending,
      retryable,
      error: status === 409
        ? '草稿修订或资源绑定已经变化，不能把旧编辑结果自动关联到新修订。请重新打开该草稿并再次带入资源编辑器。'
        : error?.message || '请重试同步；系统不会重复创建已经保存的正式资源。',
    })
    return false
  }
}

async function resolveScenarioDraftAfterFormalSave(
  formalKind: string,
  result: any,
  fallbackResourceId = '',
  claimedDraft: ScenarioModelDraftResource | null = null,
): Promise<boolean | null> {
  const active = claimedDraft || activeScenarioDraftPromotion.value
  if (!active || scenarioDraftFormalKind(active.resource_kind) !== formalKind) return null
  const relatedPropertyDrafts = active.resource_kind === 'entity'
    ? scenarioDrafts.value.filter((item) => (
      item.resource_kind === 'property'
      && scenarioDraftIsOpen(item)
      && referencedScenarioEntityDraft(item.payload?.entity_id || item.payload?.entity_ref || item.payload?.entity || item.payload?.entity_name)?.id === active.id
    ))
    : []
  const resolvedResourceId = active.resource_kind === 'property'
    ? formalPropertyResourceId(active, result, fallbackResourceId)
    : formalResourceId(result, fallbackResourceId)
  if (activeScenarioDraftPromotion.value?.id === active.id) activeScenarioDraftPromotion.value = null
  const pending: PendingScenarioDraftResolution = {
    draftId: active.id,
    expectedRevision: active.revision,
    resolvedResourceId,
    resourceKind: active.resource_kind,
    title: active.title || scenarioDraftKindLabel(active.resource_kind),
    error: '',
    retryable: Boolean(resolvedResourceId),
  }
  if (!resolvedResourceId) {
    upsertPendingScenarioDraftResolution({
      ...pending,
      error: '正式资源接口没有返回资源 ID，无法自动关联。请重新打开草稿并确认正式资源。',
      retryable: false,
    })
    return false
  }
  upsertPendingScenarioDraftResolution(pending)
  const primaryResolved = await resolvePendingScenarioDraft(pending)
  if (!primaryResolved || !relatedPropertyDrafts.length) return primaryResolved
  let allResolved = true
  for (const propertyDraft of relatedPropertyDrafts) {
    const propertyResourceId = formalPropertyResourceId(propertyDraft, result, fallbackResourceId)
    if (!propertyResourceId) {
      allResolved = false
      continue
    }
    const childPending: PendingScenarioDraftResolution = {
      draftId: propertyDraft.id,
      expectedRevision: propertyDraft.revision,
      resolvedResourceId: propertyResourceId,
      resourceKind: propertyDraft.resource_kind,
      title: propertyDraft.title || scenarioDraftKindLabel(propertyDraft.resource_kind),
      error: '',
      retryable: true,
    }
    upsertPendingScenarioDraftResolution(childPending)
    if (!await resolvePendingScenarioDraft(childPending)) allResolved = false
  }
  return allResolved
}

function scenarioDraftSaveToast(message: string, _resolved: boolean | null) {
  ElMessage.success(message)
}

async function startEditingScenarioDraft(item: ScenarioModelDraftResource) {
  if (!item || !canWrite.value || !scenarioDraftCanEdit(item)) return
  const payload = draftPayload(item)
  if (item.resource_kind === 'property') {
    const entityReference = payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name
    const entityId = draftEntityId(entityReference)
    const entity = detail.value.entities.find((candidate) => candidate.id === entityId)
    const entityDraft = entity?.id ? undefined : referencedScenarioEntityDraft(entityReference)
    if (!entity?.id) {
      if (entityDraft) {
        await startEditingScenarioDraft(entityDraft)
      } else {
        if (tab.value !== 'ontology') {
          tab.value = 'ontology'
          await nextTick()
        }
        const references = draftRefCandidates(entityReference)
        activeScenarioDraftPromotion.value = item
        removePendingScenarioDraftResolution(item.id)
        editor.value = {
          kind: 'entity',
          form: {
            name: references[references.length - 1] || '对象类型',
            namespace: '',
            color: graphPalette[0],
            description: '',
            is_abstract: false,
            state_property: '',
            properties: [],
          },
        }
      }
    } else if (tab.value !== 'ontology') {
      tab.value = 'ontology'
      await nextTick()
      openEntity(entity.id)
    } else {
      openEntity(entity.id)
    }
    if (!editor.value || editor.value.kind !== 'entity') return
    const properties = editor.value.form.properties as any[]
    const propertyName = String(payload.name || item.title || '').trim()
    const apiName = String(payload.api_name || '').trim()
    const existingIndex = properties.findIndex((property) => (
      (apiName && String(property.api_name || '') === apiName)
      || (propertyName && String(property.name || '') === propertyName)
    ))
    const existing = existingIndex >= 0 ? properties[existingIndex] : {}
    const has = (field: string) => Object.prototype.hasOwnProperty.call(payload, field)
    const enumValues = Array.isArray(payload.enum_values)
      ? payload.enum_values.map((value: unknown) => String(value).trim()).filter(Boolean)
      : Array.isArray(existing.enum_values) ? [...existing.enum_values] : []
    const property = {
      ...existing,
      name: propertyName,
      api_name: apiName || String(existing.api_name || ''),
      data_type: String(payload.data_type || payload.type || existing.data_type || 'string'),
      description: String(payload.description ?? existing.description ?? ''),
      is_key: has('is_key') ? payload.is_key === true : Boolean(existing.is_key),
      is_title: has('is_title') ? payload.is_title === true : Boolean(existing.is_title),
      is_required: has('is_required') ? payload.is_required === true : Boolean(existing.is_required),
      is_enum: has('is_enum') ? payload.is_enum === true : (Boolean(existing.is_enum) || enumValues.length > 0),
      enum_values: enumValues,
      default_value: has('default_value') ? cloneForForm(payload.default_value) : existing.default_value ?? '',
      constraints: cloneForForm(payload.constraints || existing.constraints || {}),
      is_sensitive: has('is_sensitive') ? payload.is_sensitive === true : Boolean(existing.is_sensitive),
    }
    const propertyIndex = existingIndex >= 0 ? existingIndex : properties.length
    if (existingIndex >= 0) properties.splice(existingIndex, 1, property)
    else properties.push(property)
    const promotion = entityDraft || item
    activeScenarioDraftPromotion.value = promotion
    removePendingScenarioDraftResolution(promotion.id)
    draftPropertyEditorIndex.value = propertyIndex
    return
  }
  const stage = scenarioDraftStage(item.resource_kind)
  if (stage) tab.value = stage
  await nextTick()
  activeScenarioDraftPromotion.value = item
  removePendingScenarioDraftResolution(item.id)
  draftPropertyEditorIndex.value = null

  if (item.resource_kind === 'entity') {
    editor.value = {
      kind: 'entity',
      form: {
        name: String(payload.name || item.title || ''),
        namespace: String(payload.namespace || ''),
        color: String(payload.color || graphPalette[0]),
        description: String(payload.description || ''),
        is_abstract: payload.is_abstract === true,
        state_property: String(payload.state_property || ''),
        properties: Array.isArray(payload.properties) ? payload.properties.map((property: any) => ({ ...property })) : [],
      },
    }
    return
  }
  if (item.resource_kind === 'relation') {
    editor.value = {
      kind: 'relation',
      form: {
        name: String(payload.name || item.title || ''),
        namespace: String(payload.namespace || ''),
        source_entity_id: draftEntityId(payload.source_entity_id || payload.source_ref || payload.source_entity || payload.source_entity_name),
        target_entity_id: draftEntityId(payload.target_entity_id || payload.target_ref || payload.target_entity || payload.target_entity_name),
        relation_type: String(payload.relation_type || payload.cardinality || '1:N'),
        description: String(payload.description || ''),
        constraints: cloneForForm(payload.constraints || {}),
      },
    }
    return
  }
  if (item.resource_kind === 'instance') {
    editor.value = {
      kind: 'instance',
      form: {
        entity_id: draftEntityId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name),
        name: String(payload.name || payload.display_name || item.title || ''),
        attributes: cloneForForm(payload.attributes || payload.values || {}),
        state: String(payload.state || ''),
        valid_from: payload.valid_from || null,
        valid_to: payload.valid_to || null,
        quality: cloneForForm(payload.quality || {}),
        access_scope: payload.access_scope === 'restricted' ? 'restricted' : 'tenant',
        source: 'assistant_draft',
        source_ref: item.id,
      },
    }
    return
  }
  if (['mapping', 'data_mapping', 'conceptual_mapping'].includes(item.resource_kind)) {
    mappingForm.value = {
      entity_id: draftEntityId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name),
      // Conceptual AI mappings must stay visibly unbound. Never guess a source.
      data_source_id: draftDataSourceId(payload.data_source_id || payload.data_source_ref || payload.data_source || payload.data_source_name),
      data_source_binding_key: String(payload.data_source_binding_key || ''),
      data_source_binding_ref: cloneForForm(payload.data_source_binding_ref || {}),
      table_name: String(payload.table_name || payload.table || ''),
      column_map: cloneForForm(payload.column_map || payload.field_mappings || {}),
    }
    const draftTransforms = payload.transform_rules && typeof payload.transform_rules === 'object' ? payload.transform_rules : {}
    mappingTransformRules.value = Object.fromEntries(Object.entries(draftTransforms).map(([propertyName, rules]) => [
      propertyName,
      Array.isArray(rules) ? rules.map((rule: any) => ({ ...rule })) : [],
    ]))
    mappingDlg.value = true
    void loadTables()
    return
  }
  if (item.resource_kind === 'relation_mapping') {
    relationMappingEditingId.value = ''
    relationMappingForm.value = {
      ...emptyRelationMappingForm(),
      ...payload,
      relation_id: String(payload.relation_id || ''),
      source_mapping_id: String(payload.source_mapping_id || ''),
      target_mapping_id: String(payload.target_mapping_id || ''),
      join_data_source_id: draftDataSourceId(payload.join_data_source_id || payload.data_source_id),
    }
    invalidateRelationMappingPreview()
    relationMappingDlg.value = true
    await nextTick()
    await loadRelationMappingOptions()
    return
  }
  if (item.resource_kind === 'function') {
    functionForm.value = {
      name: String(payload.name || item.title || ''),
      description: String(payload.description || ''),
      tags_text: Array.isArray(payload.tags) ? payload.tags.join(', ') : String(payload.tags_text || ''),
      visibility: payload.visibility === 'tenant' ? 'tenant' : 'scenario',
      input_schema: cloneForForm(payload.input_schema || emptyFunctionSchema()),
      output_schema: cloneForForm(payload.output_schema || emptyFunctionSchema()),
      // A promoted AI draft starts as a non-runnable declaration. The user can
      // explicitly select a governed built-in runtime after reviewing it.
      runtime_kind: 'contract',
      runtime_config: {},
    }
    functionDlg.value = true
    return
  }
  if (item.resource_kind === 'action') {
    const executorType = String(payload.executor_type || 'unbound')
    actionForm.value = {
      ...payload,
      entity_id: draftEntityId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name),
      name: String(payload.name || item.title || ''),
      description: String(payload.description || ''),
      executor_type: executorType,
      executor_config: { ...emptyExecutorConfig(executorType), ...cloneForForm(payload.executor_config || {}) },
      input_schema: cloneForForm(payload.input_schema || emptyFunctionSchema()),
      enabled: false,
      requires_confirmation: payload.requires_confirmation !== false,
      idempotency_required: payload.idempotency_required !== false,
      permission_scope: 'scenario',
    }
    actionLegacyConditions.value = []
    actionPrecondition.value = parseActionCondition(actionForm.value.precondition, '执行前条件')
    actionPostcondition.value = parseActionCondition(actionForm.value.postcondition, '执行后校验')
    originalActionTemplateId.value = ''
    if (executorType === 'template') void loadActionTemplates()
    actionDlg.value = true
    return
  }
  if (item.resource_kind === 'rule') {
    ruleForm.value = {
      ...payload,
      name: String(payload.name || item.title || ''),
      entity_id: draftEntityId(payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name),
      condition: cloneForForm(payload.condition || {}),
      enabled: false,
    }
    ruleDlg.value = true
    return
  }
  if (item.resource_kind === 'event') {
    eventForm.value = {
      ...payload,
      name: String(payload.name || item.title || ''),
      payload_schema: cloneForForm(payload.payload_schema || emptyFunctionSchema()),
      enabled: false,
    }
    eventDlg.value = true
    return
  }
  if (item.resource_kind === 'workflow') {
    wfEditor.value = {
      ...payload,
      name: String(payload.name || item.title || ''),
      trigger_type: String(payload.trigger_type || 'manual'),
      trigger_config: { interval_seconds: 300, max_attempts: 3, retry_backoff_seconds: 5, timeout_seconds: 300, event_id: '', ...cloneForForm(payload.trigger_config || {}) },
      steps: Array.isArray(payload.steps) ? payload.steps.map((step: any) => ({ ...step })) : [],
      nodes: Array.isArray(payload.nodes) ? payload.nodes.map((node: any) => ({ ...node, data: { ...(node.data || {}) } })) : [],
      edges: Array.isArray(payload.edges) ? payload.edges.map((edge: any) => ({ ...edge })) : [],
      status: 'draft',
      enabled: false,
    }
  }
}

function unresolvedDraftReferenceIssue(
  label: string,
  value: unknown,
  resolvedId: string,
): ScenarioModelDraftIssue | null {
  const token = draftRefToken(value)
  if (!token || resolvedId) return null
  return {
    code: 'FRONTEND_UNRESOLVED_DRAFT_REFERENCE',
    message: `${label}“${token}”尚未绑定到当前场景中的正式资源。`,
    blocking: true,
    resolution_hint: '先创建或修正被引用资源，再在草稿编辑器中选择正式资源。',
    source_refs: [token],
  }
}

function withDraftReferenceIssues(item: ScenarioModelDraftResource): ScenarioModelDraftResource {
  const payload = item.payload || {}
  const localIssues: ScenarioModelDraftIssue[] = []
  const add = (issue: ScenarioModelDraftIssue | null) => { if (issue) localIssues.push(issue) }
  if (item.resource_kind === 'relation') {
    const source = payload.source_entity_id || payload.source_ref || payload.source_entity || payload.source_entity_name
    const target = payload.target_entity_id || payload.target_ref || payload.target_entity || payload.target_entity_name
    add(unresolvedDraftReferenceIssue('来源对象', source, draftEntityId(source)))
    add(unresolvedDraftReferenceIssue('目标对象', target, draftEntityId(target)))
  }
  if (['property', 'instance', 'mapping', 'data_mapping', 'conceptual_mapping', 'action', 'rule'].includes(item.resource_kind)) {
    const entity = payload.entity_id || payload.entity_ref || payload.entity || payload.entity_name
    add(unresolvedDraftReferenceIssue('对象类型', entity, draftEntityId(entity)))
  }
  if (['mapping', 'data_mapping'].includes(item.resource_kind)) {
    const source = payload.data_source_id || payload.data_source_ref || payload.data_source || payload.data_source_name
    add(unresolvedDraftReferenceIssue('数据源', source, draftDataSourceId(source)))
  }
  const issues = [...item.validation_issues]
  for (const local of localIssues) {
    if (!issues.some((existing) => existing.code === local.code && existing.message === local.message)) issues.push(local)
  }
  return {
    ...item,
    validation_issues: issues,
    issues_count: Math.max(Number(item.issues_count || 0), issues.length),
    blocking_issue_count: Math.max(
      Number(item.blocking_issue_count || 0),
      issues.filter((issue) => issue.blocking).length,
    ),
  }
}

async function loadScenarioDrafts(includeIssues = tab.value === 'candidates') {
  const request = ++scenarioDraftRequest
  scenarioDraftsLoading.value = true
  scenarioDraftsError.value = ''
  try {
    const draftsById = new Map<string, ScenarioModelDraftResource>()
    const limit = 1000
    let offset = 0
    let expectedTotal: number | undefined
    let governanceSummary: ScenarioModelCandidateSummary = {}
    while (true) {
      const response = await api.listScenarioModelDrafts(sid, {
        offset,
        limit,
        // The scene projection only needs editable payloads and lifecycle
        // metadata. Large validation evidence is loaded on demand by callers
        // that explicitly request the detailed draft view.
        include_issues: includeIssues,
      })
      if (scenarioDraftViewDisposed || request !== scenarioDraftRequest) return
      const normalizedPage = normalizeScenarioModelDrafts(response)
        .filter((item) => !item.scenario_id || item.scenario_id === sid)
      for (const item of normalizedPage) draftsById.set(item.id, item)

      const metadata = response && !Array.isArray(response) ? response : undefined
      if (offset === 0 && metadata?.summary) governanceSummary = metadata.summary
      const responseTotal = Number(metadata?.total)
      if (Number.isSafeInteger(responseTotal) && responseTotal >= 0) {
        if (expectedTotal === undefined) expectedTotal = responseTotal
        else if (responseTotal !== expectedTotal) {
          throw new Error('草稿列表在分页读取期间发生变化，请重试以获取一致的完整列表。')
        }
      }
      if (metadata?.has_more !== true) {
        if (expectedTotal !== undefined && draftsById.size !== expectedTotal) {
          throw new Error(`草稿分页结果不完整（应有 ${expectedTotal} 项，实际收到 ${draftsById.size} 项），请重试。`)
        }
        break
      }
      const nextOffset = Number(metadata.next_offset)
      if (
        !Number.isSafeInteger(nextOffset)
        || nextOffset <= offset
        || (expectedTotal !== undefined && nextOffset >= expectedTotal)
      ) {
        throw new Error('草稿分页游标无效，已停止继续请求以避免循环；请重试。')
      }
      offset = nextOffset
    }
    const normalized = [...draftsById.values()]
    // Resolve compiler refs against both the formal model and entity drafts in
    // the complete paginated result before surfacing local binding issues.
    scenarioDrafts.value = normalized
    scenarioDrafts.value = normalized.map(withDraftReferenceIssues)
    scenarioDraftSummary.value = governanceSummary
  } catch (error: any) {
    if (scenarioDraftViewDisposed || request !== scenarioDraftRequest) return
    scenarioDraftsError.value = error?.message || '请稍后重试；正式场景资源不受影响。'
  } finally {
    if (!scenarioDraftViewDisposed && request === scenarioDraftRequest) scenarioDraftsLoading.value = false
  }
}

async function refreshCandidateReview(definitionChanged: boolean) {
  if (definitionChanged) {
    try {
      const loaded = await api.getScenario(sid, { include_runtime_facts: false })
      detail.value = { ...loaded, relation_mappings: loaded.relation_mappings || [] }
    } catch (error: any) {
      ElMessage.error(error?.message || '正式定义已更新，但场景统计刷新失败，请稍后重试。')
    }
  }
  await loadScenarioDrafts(true)
}

// ── 加载 ──
async function load() {
  const request = ++scenarioLoadRequest
  const scenarioId = sid
  scenarioLoading.value = true
  scenarioAccessDenied.value = false
  scenarioLoadError.value = ''
  try {
    // Scenario detail is a schema projection. Runtime objects and relations
    // are always fetched through bounded pages below, so a large dataset can
    // never hold the whole page hostage.
    const loaded = await api.getScenario(scenarioId, { include_runtime_facts: false })
    if (request !== scenarioLoadRequest || scenarioId !== sid) return
    detail.value = { ...loaded, relation_mappings: loaded.relation_mappings || [] }
  } catch (e: any) {
    if (request !== scenarioLoadRequest || scenarioId !== sid) return
    if (Number(e?.status || e?.response?.status) === 403) {
      scenarioAccessDenied.value = true
    } else {
      scenarioLoadError.value = e?.response?.data?.detail || e?.message || '请稍后重试。'
    }
    return
  } finally {
    if (request === scenarioLoadRequest && scenarioId === sid) scenarioLoading.value = false
  }
  try {
    const [sources, configs, skillItems, mcpItems] = await Promise.all([
      api.listDataSources(), api.listLLM(), api.listSkills(), api.listMCP(),
    ])
    if (request !== scenarioLoadRequest || scenarioId !== sid) return
    dataSources.value = sources
    llmConfigs.value = configs
    skills.value = skillItems
    mcpConfigs.value = mcpItems
  } catch (e: any) {
    if (request === scenarioLoadRequest && scenarioId === sid) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '场景关联资源加载失败')
    }
  } finally {
    if (request === scenarioLoadRequest && scenarioId === sid) await loadScenarioDrafts()
  }
  if (request !== scenarioLoadRequest || scenarioId !== sid) return
  // The object explorer is only visible on the instances tab. Avoid doing a
  // runtime object scan while the ontology tab is becoming interactive.
  if (tab.value === 'instances') {
    void searchObjects()
    void loadRelationInstances()
  }
}
function goBack() { void router.push(returnPath.value) }
function goToDataSources() {
  router.push({ name: 'data-sources', query: { scenario_id: sid, return_to: route.fullPath } })
}
function goToTemplates() {
  router.push({ name: 'templates', query: { scenario_id: sid, return_to: route.fullPath } })
}
function onAssistantApplied(event: Event) {
  const detail = (event as CustomEvent<{ scenario_id?: string }>).detail || {}
  if (!detail.scenario_id || detail.scenario_id === sid) load()
}
function onAssistantScenarioDraftsUpdated(event: Event) {
  const eventDetail = (event as CustomEvent<{ scenario_id?: string }>).detail || {}
  if (!eventDetail.scenario_id || eventDetail.scenario_id === sid) void loadScenarioDrafts()
}
function requestedActionId() {
  const value = route.query.action_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function requestedEditActionId() {
  const value = route.query.edit_action_id
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
    ElMessage.warning('该操作不在当前可编辑场景中，可能已被移除或更换版本')
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
    actionParamsForm.value[field.name] = cloneForForm(value)
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
  return status === 'active' ? 'success' : 'info'
}
async function openRequestedRouteAction() {
  const editActionId = requestedEditActionId()
  if (editActionId && canWrite.value && detail.value.actions.some((action) => action.id === editActionId)) {
    openAction(editActionId)
    return
  }
  await openGovernedActionById(requestedActionId(), consumeAssistantActionPreviewState())
}
onMounted(async () => {
  mappingRefreshViewDisposed = false
  objectSearchViewDisposed = false
  scenarioDraftViewDisposed = false
  if (route.query.stage !== tab.value) void router.replace({ query: { ...route.query, stage: tab.value } })
  await load()
  await openRequestedRouteAction()
  window.addEventListener('assistant-proposal-applied', onAssistantApplied)
  window.addEventListener('assistant-scenario-drafts-updated', onAssistantScenarioDraftsUpdated)
  window.addEventListener('open-governed-action', onOpenGovernedAction)
})
watch(() => route.params.id, async (value) => {
  const nextId = Array.isArray(value) ? String(value[0] || '') : String(value || '')
  if (!nextId || nextId === sid) return
  scenarioLoadRequest += 1
  scenarioDraftRequest += 1
  objectRequestId += 1
  mappingTableRequest += 1
  for (const timer of mappingRefreshTimers.values()) window.clearTimeout(timer)
  mappingRefreshTimers.clear()
  mappingRefreshFailures.clear()
  sid = nextId
  detail.value = emptyScenarioDetail(sid)
  scenarioDrafts.value = []
  scenarioDraftSummary.value = {}
  pendingScenarioDraftResolutions.value = []
  activeScenarioDraftPromotion.value = null
  editor.value = null
  selectedObjectId.value = null
  objectItems.value = []
  objectDetail.value = null
  relationInstanceRows.value = []
  mappingDlg.value = false
  mappingPreviewDlg.value = false
  relationMappingDlg.value = false
  relationInstanceDlg.value = false
  functionDlg.value = false
  actionDlg.value = false
  actionExecuteDlg.value = false
  ruleDlg.value = false
  eventDlg.value = false
  execResultDlg.value = false
  recordInputDlg.value = false
  wfEditor.value = null
  const nextStage = Array.isArray(route.query.stage) ? route.query.stage[0] : route.query.stage
  tab.value = typeof nextStage === 'string' && stageNames.has(nextStage) ? nextStage : 'ontology'
  await load()
  await openRequestedRouteAction()
})
onBeforeUnmount(() => {
  scenarioLoadRequest += 1
  mappingRefreshViewDisposed = true
  mappingTableRequest += 1
  objectSearchViewDisposed = true
  objectRequestId += 1
  objectPendingKey = ''
  scenarioDraftViewDisposed = true
  scenarioDraftRequest += 1
  window.removeEventListener('assistant-proposal-applied', onAssistantApplied)
  window.removeEventListener('assistant-scenario-drafts-updated', onAssistantScenarioDraftsUpdated)
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
  display: flex;
  align-items: center;
  gap: 6px;
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
.candidate-tab-count {
  display: inline-flex;
  min-width: 20px;
  height: 20px;
  align-items: center;
  justify-content: center;
  margin-inline-start: 5px;
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--text-2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
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
  flex: 0 0 auto;
  height: clamp(500px, 66dvh, 760px);
  min-height: 0;
}
.instance-workspace .graph-stage {
  height: 100%;
  min-height: 0;
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
.mapping-stats {
  flex-wrap: wrap;
}
.mapping-section {
  min-width: 0;
  margin-bottom: 16px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  box-shadow: var(--shadow-xs);
}
.mapping-section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.mapping-section-head > div {
  min-width: 0;
}
.mapping-section-head h2 {
  margin: 0;
  color: var(--text);
  font-size: 15px;
}
.mapping-section-head p {
  margin: 4px 0 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.55;
}
.mapping-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr));
  gap: 12px;
}
.mapping-item-card {
  min-width: 0;
  display: grid;
  align-content: start;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 13px;
  background: var(--surface-2);
}
.mapping-item-head {
  min-width: 0;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.mapping-source,
.relation-mapping-title {
  min-width: 0;
  display: grid;
  gap: 3px;
}
.mapping-source b,
.relation-mapping-title b {
  color: var(--text);
  overflow-wrap: anywhere;
}
.mapping-source span,
.relation-mapping-title span {
  color: var(--text-3);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.mapping-item-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.mapping-item-actions {
  padding-top: 4px;
  border-top: 1px solid var(--border);
}
.mapping-item-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}
.mapping-inline-error {
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0;
  color: var(--danger);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.mapping-inline-error .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
}
.relation-mapping-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
  margin: 0;
}
.relation-mapping-facts div {
  min-width: 0;
}
.relation-mapping-facts dt {
  color: var(--text-3);
  font-size: 10.5px;
}
.relation-mapping-facts dd {
  margin: 2px 0 0;
  color: var(--text-2);
  font-size: 12px;
  overflow-wrap: anywhere;
}
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
.inline-resource-name {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.inline-resource-name b { overflow-wrap: anywhere; }
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
.mapping-transformed { margin-top: 14px; }
.mapping-field-list,
.mapping-sample-list {
  display: grid;
  gap: 8px;
  padding: 10px;
}
.mapping-field-card {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(120px, 1fr) minmax(100px, .8fr) auto;
  align-items: center;
  gap: 8px;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
}
.mapping-field-name {
  min-width: 0;
  display: grid;
  gap: 4px;
}
.mapping-field-name b {
  overflow-wrap: anywhere;
}
.mapping-field-name span {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.mapping-sample-card {
  min-width: 0;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
}
.mapping-sample-index {
  display: block;
  margin-bottom: 7px;
  color: var(--text-2);
  font-size: 11px;
}
.mapping-sample-card dl {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 120px), 1fr));
  gap: 7px;
  margin: 0;
}
.mapping-sample-card dl div {
  min-width: 0;
}
.mapping-sample-card dt {
  color: var(--text-3);
  font-size: 10px;
  overflow-wrap: anywhere;
}
.mapping-sample-card dd {
  min-width: 0;
  margin: 3px 0 0;
  color: var(--text);
  font-size: 12px;
  overflow-wrap: anywhere;
}
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
.colmap-attr b {
  display: block;
  overflow-wrap: anywhere;
}
.mapping-property-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  margin-top: 3px;
}
.mapping-property-flags :deep(.el-tag) {
  transform: scale(.84);
  transform-origin: left center;
}
.transform-rule-list { display: grid; gap: 6px; margin: 8px 0 0 120px; padding-top: 8px; border-top: 1px dashed var(--border); }
.transform-rule-row { display: grid; grid-template-columns: 22px minmax(150px, .8fr) minmax(120px, 1fr) minmax(120px, 1fr) 32px; align-items: center; gap: 7px; }
.transform-rule-row > .el-select { grid-column: 2; }
.transform-rule-hint { grid-column: 3 / 5; color: var(--text-3); font-size: 11px; line-height: 1.45; }
.transform-order { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; background: var(--primary-soft); color: var(--primary-600); font-size: 10px; font-weight: 750; }

/* ── 关系映射向导：弹窗正文是唯一滚动所有者 ── */
.relation-mapping-steps {
  margin-bottom: 16px;
}
.relation-mapping-form {
  min-width: 0;
}
.relation-endpoint-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.relation-mapping-prerequisite,
.carrier-source-alert {
  margin-bottom: 14px;
}
.inline-guidance-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.relation-mode-options {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.relation-mode-option {
  width: 100%;
  height: auto;
  min-width: 0;
  align-items: flex-start;
  margin: 0;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
  white-space: normal;
}
.relation-mode-option.is-checked {
  border-color: var(--primary);
  background: var(--primary-soft);
}
.relation-mode-option :deep(.el-radio__label) {
  min-width: 0;
  padding-left: 7px;
  white-space: normal;
}
.relation-mode-option span {
  min-width: 0;
  display: grid;
  gap: 3px;
}
.relation-mode-option b {
  color: var(--text);
  font-size: 12px;
}
.relation-mode-option small {
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.45;
}
.relation-preflight-result {
  min-width: 0;
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-2);
}
.relation-preflight-result header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.relation-preflight-result header > div {
  min-width: 0;
  display: grid;
  gap: 3px;
}
.relation-preflight-result header span {
  color: var(--text-3);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.relation-preflight-errors { color: var(--danger); }
.relation-preflight-warnings { color: var(--warning); }

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
.relation-instance-cards { display: none; }
.relation-instance-card {
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
}
.relation-instance-card div { display: grid; gap: 3px; min-width: 0; }
.relation-instance-card b { color: var(--text-1); font-size: 12px; }
.relation-instance-card span { color: var(--text-3); font-size: 11px; overflow-wrap: anywhere; }
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
  padding-right: 4px;
}
.template-binding-head {
  display: flex;
  min-height: 40px;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--text-3);
  font-size: 11px;
}
.template-legacy-alert { margin-bottom: 12px; }
.template-option {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.template-option > span { min-width: 0; display: flex; align-items: center; gap: 7px; }
.template-option > span:first-child { flex-direction: column; align-items: flex-start; gap: 1px; }
.template-option b { max-width: 300px; overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
.template-option small { max-width: 300px; overflow: hidden; color: var(--text-3); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.template-option em { color: var(--text-3); font-size: 10px; font-style: normal; white-space: nowrap; }
.selected-template-summary {
  display: grid;
  gap: 9px;
  margin: 0 0 14px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--surface-2);
}
.selected-template-summary > header,
.selected-template-summary > footer,
.selected-template-tags,
.selected-template-variables { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.selected-template-summary > header { justify-content: space-between; gap: 10px; }
.selected-template-summary > header > div:first-child { min-width: 0; display: grid; gap: 2px; }
.selected-template-summary > header b { color: var(--text); font-size: 12px; }
.selected-template-summary > header span { color: var(--text-3); font-size: 10.5px; }
.selected-template-summary > footer { justify-content: flex-end; padding-top: 4px; border-top: 1px solid var(--border); }
.selected-template-summary > footer :deep(.el-button + .el-button) { margin-left: 0; }
.selected-template-variables > span { color: var(--text-3); font-size: 10px; }
.selected-template-variables code { max-width: 100%; padding: 2px 6px; overflow-wrap: anywhere; border-radius: 5px; background: var(--primary-soft); color: var(--primary-600); font-size: 10px; }
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
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 640px) {
  .template-binding-head { align-items: flex-start; flex-direction: column; padding: 8px 0; }
  .selected-template-summary > header { align-items: flex-start; flex-direction: column; }
  .selected-template-summary > footer { align-items: stretch; flex-direction: column; }
  .selected-template-summary > footer :deep(.el-button) { width: 100%; min-height: 42px; }
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
  .relation-instance-form-row { flex-direction: column; gap: 0; }
  .relation-instance-table { display: none; }
  .relation-instance-cards { display: grid; gap: 8px; }
  .relation-instance-card { display: flex; }
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
  .instance-workspace { grid-template-columns: 1fr; height: auto; overflow: visible; }
  .instance-workspace .graph-stage { min-height: 360px; height: clamp(360px, 52dvh, 520px); }
  .object-explorer { height: clamp(480px, 72dvh, 680px); min-height: 480px; overflow-y: auto; overscroll-behavior: contain; }
  .object-list, .object-detail { flex: none; overflow: visible; }
  .explorer-head { position: sticky; top: 0; z-index: 4; min-height: 67px; background: var(--surface); }
  .explorer-tools { position: sticky; top: 67px; z-index: 3; border-bottom: 1px solid var(--border); background: var(--surface); }
  .mapping-preview-grid { grid-template-columns: 1fr; }
}

/* ── 页面滚动归外层 main-area，场景卡片不再各自抢占滚轮 ── */
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
  overflow: visible;
}

@media (max-width: 900px) {
  .sd-page { padding: 12px 14px 14px; }
  .sd-header { min-height: 44px; }
  .sd-header .ph-title h1 { font-size: 17px; }
  .graph-stage { min-height: 360px; height: clamp(360px, 58dvh, 560px); }
  .wf-editor-stage { min-height: 0; height: auto; overflow: visible; }
}

@media (max-width: 760px) {
  .ph-sub { display: -webkit-box; max-width: 100%; overflow: hidden; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
  .sd-header :deep(.el-button) { min-height: 44px; }
  .sd-tabs :deep(.el-tabs__item) { height: 44px; }
  .colmap-row { align-items: stretch; flex-direction: column; }
  .colmap-attr { width: auto; }
  .transform-rule-list { margin-left: 0; }
  .transform-rule-row { grid-template-columns: 22px minmax(0, 1fr) 40px; }
  .transform-rule-row > .el-select { grid-column: 2; }
  .transform-rule-row > .el-input, .transform-rule-hint { grid-column: 2 / 4; }
  .transform-rule-row > .el-button { grid-column: 3; grid-row: 1; }
  .mapping-section { padding: 12px; }
  .mapping-section-head { flex-direction: column; align-items: stretch; }
  .mapping-section-head :deep(.el-button) { width: 100%; min-height: 42px; }
  .relation-endpoint-grid,
  .relation-mode-options { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .relation-mode-options { gap: 7px; }
  .relation-mapping-facts { grid-template-columns: minmax(0, 1fr); }
  .mapping-field-card { grid-template-columns: minmax(0, 1fr); align-items: start; }
  .relation-mapping-steps :deep(.el-step__title) { font-size: 11px; }
}
</style>
