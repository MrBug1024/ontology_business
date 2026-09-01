<template>
  <section class="page template-page" aria-labelledby="template-page-title">
    <header class="page-header">
      <div>
        <div class="eyebrow">GOVERNED ARTIFACTS</div>
        <h1 id="template-page-title">模板中心</h1>
        <p class="sub">集中管理各业务场景的 Word、Excel 与 Markdown 模板；版本、变量和引用关系始终可追溯。</p>
      </div>
      <el-tooltip :disabled="Boolean(writableBuckets.length)" content="请先创建一个可写文件桶" placement="bottom">
        <span :tabindex="writableBuckets.length ? undefined : 0" :aria-label="writableBuckets.length ? undefined : '上传模板不可用：请先创建一个可写文件桶'">
          <el-button type="primary" :disabled="!writableBuckets.length" @click="openCreate">
            <el-icon aria-hidden="true"><Upload /></el-icon>上传模板
          </el-button>
        </span>
      </el-tooltip>
    </header>

    <el-alert
      v-if="operationNotice.message"
      :type="operationNotice.type"
      :title="operationNotice.message"
      :closable="true"
      show-icon
      role="status"
      class="operation-notice"
      @close="operationNotice.message = ''"
    />

    <section class="card filter-card" aria-label="模板筛选">
      <div class="filter-grid">
        <label class="filter-field filter-search">
          <span>搜索</span>
          <el-input v-model="filters.q" clearable placeholder="名称、用途、说明或标识" aria-label="搜索模板">
            <template #prefix><el-icon aria-hidden="true"><Search /></el-icon></template>
          </el-input>
        </label>
        <label class="filter-field">
          <span>业务场景</span>
          <el-select v-model="filters.scenario_id" clearable aria-label="按业务场景筛选">
            <el-option label="全部场景" value="" />
            <el-option label="租户共享" value="__shared__" />
            <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
          </el-select>
        </label>
        <label class="filter-field">
          <span>文件格式</span>
          <el-select v-model="filters.artifact_format" clearable aria-label="按文件格式筛选">
            <el-option label="全部格式" value="" />
            <el-option label="Word (.docx)" value="docx" />
            <el-option label="Excel (.xlsx)" value="xlsx" />
            <el-option label="Markdown (.md)" value="markdown" />
          </el-select>
        </label>
        <label class="filter-field">
          <span>状态</span>
          <el-select v-model="filters.status" clearable aria-label="按模板状态筛选">
            <el-option label="全部状态" value="" />
            <el-option label="使用中" value="active" />
            <el-option label="已停用" value="deprecated" />
          </el-select>
        </label>
      </div>
      <div class="filter-summary" role="status" aria-live="polite">
        <span v-if="!loading">共 {{ visibleTemplates.length }} 个模板</span>
        <span v-else>正在加载模板…</span>
        <el-button v-if="hasFilters" text type="primary" @click="clearFilters">清除筛选</el-button>
      </div>
    </section>

    <section class="card template-list-card" aria-label="模板列表">
      <el-skeleton v-if="loading && !templates.length" :rows="7" animated aria-label="正在加载模板" />
      <el-result v-else-if="loadError" icon="error" title="模板加载失败" :sub-title="loadError">
        <template #extra><el-button type="primary" @click="loadTemplates">重试</el-button></template>
      </el-result>
      <el-empty v-else-if="!visibleTemplates.length" :description="hasFilters ? '没有符合当前筛选条件的模板' : '还没有统一管理的附件模板'">
        <div class="empty-actions">
          <el-button v-if="hasFilters" @click="clearFilters">清除筛选</el-button>
          <el-button v-if="writableBuckets.length" type="primary" @click="openCreate">上传第一个模板</el-button>
          <el-button v-else type="primary" plain @click="goToDataSources">创建文件桶</el-button>
        </div>
      </el-empty>
      <template v-else>
        <el-table v-loading="loading" :data="visibleTemplates" row-key="id" class="template-table">
          <el-table-column label="模板" min-width="240">
            <template #default="{ row }">
              <button type="button" class="template-name-button" :aria-label="`查看模板 ${row.name}`" @click="openDetail(row)">
                <span class="format-mark" :class="`format-${row.current_version?.artifact_format || 'unknown'}`" aria-hidden="true">
                  {{ formatShort(row.current_version?.artifact_format) }}
                </span>
                <span>
                  <b>{{ row.name }}</b>
                  <small>{{ row.purpose || row.description || '未填写业务用途' }}</small>
                </span>
              </button>
            </template>
          </el-table-column>
          <el-table-column label="归属" min-width="145">
            <template #default="{ row }"><span>{{ scenarioName(row.scenario_id) }}</span></template>
          </el-table-column>
          <el-table-column label="当前版本" width="118">
            <template #default="{ row }">
              <div class="version-cell">
                <b v-if="row.current_version">v{{ row.current_version.version }}</b>
                <span v-else>无可用版本</span>
                <small>{{ templateFormatLabel(row.current_version?.artifact_format) }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="变量" width="84" align="center">
            <template #default="{ row }">{{ row.current_version?.placeholder_paths?.length || 0 }}</template>
          </el-table-column>
          <el-table-column label="版本 / 引用" width="112" align="center">
            <template #default="{ row }"><span>{{ row.version_count }} / {{ row.reference_count }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="92">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain">
                {{ row.status === 'active' ? '使用中' : '已停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新于" width="130">
            <template #default="{ row }"><span class="muted">{{ formatDate(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <div class="row-actions">
                <el-button text type="primary" @click="openDetail(row)">查看</el-button>
                <el-button text @click="openEdit(row)">编辑</el-button>
                <el-dropdown trigger="click" @command="(command: string) => onRowCommand(command, row)">
                  <el-button text aria-label="更多模板操作">更多<el-icon aria-hidden="true"><ArrowDown /></el-icon></el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="version" :disabled="row.status !== 'active'">上传新版本</el-dropdown-item>
                      <el-dropdown-item :command="row.status === 'active' ? 'deprecate' : 'activate'">
                        {{ row.status === 'active' ? '停用模板' : '恢复模板' }}
                      </el-dropdown-item>
                      <el-dropdown-item command="delete" divided :disabled="!row.deletable" :title="row.deletable ? '' : deleteDisabledReason(row)">删除模板</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <div class="template-cards" aria-label="模板卡片列表">
          <article v-for="template in visibleTemplates" :key="template.id" class="template-card">
            <header>
              <span class="format-mark" :class="`format-${template.current_version?.artifact_format || 'unknown'}`" aria-hidden="true">
                {{ formatShort(template.current_version?.artifact_format) }}
              </span>
              <div><h2>{{ template.name }}</h2><p>{{ template.purpose || template.description || '未填写业务用途' }}</p></div>
              <el-tag :type="template.status === 'active' ? 'success' : 'info'" effect="plain">{{ template.status === 'active' ? '使用中' : '已停用' }}</el-tag>
            </header>
            <dl>
              <div><dt>归属</dt><dd>{{ scenarioName(template.scenario_id) }}</dd></div>
              <div><dt>当前版本</dt><dd>{{ template.current_version ? `v${template.current_version.version}` : '无' }}</dd></div>
              <div><dt>变量</dt><dd>{{ template.current_version?.placeholder_paths?.length || 0 }}</dd></div>
              <div><dt>引用</dt><dd>{{ template.reference_count }}</dd></div>
            </dl>
            <footer>
              <el-button type="primary" plain @click="openDetail(template)">查看详情</el-button>
              <el-button @click="openEdit(template)">编辑</el-button>
            </footer>
          </article>
        </div>
      </template>
    </section>

    <el-dialog v-model="createDialog" title="添加模板" width="680px" destroy-on-close @closed="resetCreateForm">
      <el-alert title="模板原文件保存在所选文件桶；模板中心只管理业务元数据、版本和引用。" type="info" :closable="false" show-icon />
      <el-form label-position="top" class="template-form" @submit.prevent="createTemplate">
        <el-radio-group v-model="createForm.mode" aria-label="模板来源方式" class="source-mode">
          <el-radio-button value="upload">上传本地文件</el-radio-button>
          <el-radio-button value="register">登记桶内文件</el-radio-button>
        </el-radio-group>
        <div class="form-grid">
          <el-form-item label="业务场景">
            <el-select v-model="createForm.scenario_id" clearable placeholder="租户共享" aria-label="模板所属业务场景" @change="onCreateScenarioChanged">
              <el-option label="租户共享（所有场景可用）" value="" />
              <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
            </el-select>
            <div class="field-help">共享模板可被本租户的多个业务场景复用。</div>
          </el-form-item>
          <el-form-item label="文件桶" required>
            <el-select v-model="createForm.data_source_id" filterable placeholder="选择可写文件桶" aria-label="模板文件桶" @change="loadRegistrationFiles">
              <el-option v-for="bucket in createBuckets" :key="bucket.id" :label="bucket.name" :value="bucket.id" />
            </el-select>
            <div v-if="!createBuckets.length" class="field-error" role="status">当前归属下没有可写文件桶，请先到数据源页面创建。</div>
          </el-form-item>
        </div>
        <el-form-item v-if="createForm.mode === 'upload'" label="模板文件" required>
          <label class="file-picker" for="new-template-file">
            <el-icon aria-hidden="true"><DocumentAdd /></el-icon>
            <span><b>{{ createForm.file?.name || '选择 DOCX、XLSX 或 Markdown 文件' }}</b><small>支持 .docx、.xlsx、.md、.markdown</small></span>
          </label>
          <input id="new-template-file" class="visually-hidden" type="file" :accept="TEMPLATE_FILE_ACCEPT" aria-label="选择模板文件" @change="onCreateFilePicked" />
        </el-form-item>
        <el-form-item v-else label="桶内模板文件" required>
          <el-select v-model="createForm.file_id" filterable :loading="registrationFilesLoading" placeholder="选择尚未登记或需要复用的模板文件" aria-label="选择桶内模板文件">
            <el-option v-for="file in registrationFiles" :key="file.id" :label="`${file.filename} · ${formatSize(file.size)}`" :value="file.id" />
          </el-select>
          <div v-if="createForm.data_source_id && !registrationFilesLoading && !registrationFiles.length" class="field-help">该文件桶暂无支持的模板文件，可切换为“上传本地文件”。</div>
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="模板名称" required><el-input v-model.trim="createForm.name" maxlength="120" show-word-limit placeholder="如：年度业务报告" /></el-form-item>
          <el-form-item label="业务用途" required><el-input v-model.trim="createForm.purpose" maxlength="160" show-word-limit placeholder="如：生成项目年度报告" /></el-form-item>
        </div>
        <el-form-item label="说明"><el-input v-model.trim="createForm.description" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="适用范围、填写口径或注意事项" /></el-form-item>
        <div class="form-grid">
          <el-form-item label="稳定标识"><el-input v-model.trim="createForm.key" placeholder="可不填，由系统生成" class="mono" /></el-form-item>
          <el-form-item label="版本说明"><el-input v-model.trim="createForm.version_note" placeholder="如：首版，依据 2024 年底稿格式" /></el-form-item>
        </div>
        <el-progress v-if="createSaving && uploadProgress > 0" :percentage="uploadProgress" :stroke-width="8" aria-label="模板上传进度" />
        <div v-if="createError" class="dialog-error" role="status" aria-live="assertive">{{ createError }}</div>
      </el-form>
      <template #footer>
        <el-button :disabled="createSaving" @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" :disabled="!canSubmitCreate" @click="createTemplate">保存模板</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editDialog" title="编辑模板信息" width="620px">
      <el-form label-position="top" @submit.prevent="saveMetadata">
        <div class="form-grid">
          <el-form-item label="模板名称" required><el-input v-model.trim="editForm.name" maxlength="120" /></el-form-item>
          <el-form-item label="业务用途" required><el-input v-model.trim="editForm.purpose" maxlength="160" /></el-form-item>
        </div>
        <el-form-item label="说明"><el-input v-model.trim="editForm.description" type="textarea" :rows="3" maxlength="500" show-word-limit /></el-form-item>
        <div class="form-grid">
          <el-form-item label="业务场景">
            <el-select v-model="editForm.scenario_id" clearable placeholder="租户共享">
              <el-option label="租户共享（所有场景可用）" value="" />
              <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="稳定标识"><el-input v-model.trim="editForm.key" class="mono" /></el-form-item>
        </div>
        <div v-if="editError" class="dialog-error" role="status" aria-live="assertive">{{ editError }}</div>
      </el-form>
      <template #footer>
        <el-button :disabled="editSaving" @click="editDialog = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" :disabled="!editForm.name || !editForm.purpose" @click="saveMetadata">保存更改</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="versionDialog" :title="`添加版本 · ${versionTarget?.name || ''}`" width="620px" destroy-on-close @closed="resetVersionForm">
      <el-form label-position="top" @submit.prevent="createVersion">
        <el-radio-group v-model="versionForm.mode" aria-label="新版本来源方式" class="source-mode">
          <el-radio-button value="upload">上传本地文件</el-radio-button>
          <el-radio-button value="register">登记桶内文件</el-radio-button>
        </el-radio-group>
        <el-form-item label="文件桶" required>
          <el-select v-model="versionForm.data_source_id" filterable placeholder="选择可写文件桶" @change="loadVersionRegistrationFiles">
            <el-option v-for="bucket in versionBuckets" :key="bucket.id" :label="bucket.name" :value="bucket.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="versionForm.mode === 'upload'" label="新版本文件" required>
          <label class="file-picker" for="template-version-file">
            <el-icon aria-hidden="true"><DocumentAdd /></el-icon>
            <span><b>{{ versionForm.file?.name || '选择与模板用途一致的新文件' }}</b><small>支持 .docx、.xlsx、.md、.markdown</small></span>
          </label>
          <input id="template-version-file" class="visually-hidden" type="file" :accept="TEMPLATE_FILE_ACCEPT" aria-label="选择模板新版本文件" @change="onVersionFilePicked" />
        </el-form-item>
        <el-form-item v-else label="桶内模板文件" required>
          <el-select v-model="versionForm.file_id" filterable :loading="versionFilesLoading" placeholder="选择模板文件">
            <el-option v-for="file in versionRegistrationFiles" :key="file.id" :label="`${file.filename} · ${formatSize(file.size)}`" :value="file.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="版本说明"><el-input v-model.trim="versionForm.version_note" placeholder="本版本变更了什么" /></el-form-item>
        <el-form-item label="设为当前版本"><el-switch v-model="versionForm.set_current" /><div class="field-help">新 Action 默认绑定当前版本；既有 Action 的固定版本不会被改写。</div></el-form-item>
        <el-progress v-if="versionSaving && uploadProgress > 0" :percentage="uploadProgress" :stroke-width="8" aria-label="新版本上传进度" />
        <div v-if="versionError" class="dialog-error" role="status" aria-live="assertive">{{ versionError }}</div>
      </el-form>
      <template #footer>
        <el-button :disabled="versionSaving" @click="versionDialog = false">取消</el-button>
        <el-button type="primary" :loading="versionSaving" :disabled="!canSubmitVersion" @click="createVersion">添加版本</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailDrawer" size="min(720px, 94vw)" direction="rtl" destroy-on-close class="template-drawer">
      <template #header>
        <div class="drawer-title"><span>模板详情</span><small v-if="templateDetail">{{ templateDetail.name }}</small></div>
      </template>
      <el-skeleton v-if="detailLoading" :rows="8" animated aria-label="正在加载模板详情" />
      <el-result v-else-if="detailError" icon="error" title="详情加载失败" :sub-title="detailError">
        <template #extra><el-button type="primary" @click="reloadDetail">重试</el-button></template>
      </el-result>
      <div v-else-if="templateDetail" class="detail-content">
        <section class="detail-hero">
          <span class="format-mark large" :class="`format-${templateDetail.current_version?.artifact_format || 'unknown'}`" aria-hidden="true">{{ formatShort(templateDetail.current_version?.artifact_format) }}</span>
          <div><h2>{{ templateDetail.name }}</h2><p>{{ templateDetail.purpose || '未填写业务用途' }}</p></div>
          <el-tag :type="templateDetail.status === 'active' ? 'success' : 'info'" effect="plain">{{ templateDetail.status === 'active' ? '使用中' : '已停用' }}</el-tag>
        </section>
        <dl class="detail-facts">
          <div><dt>归属</dt><dd>{{ scenarioName(templateDetail.scenario_id) }}</dd></div>
          <div><dt>稳定标识</dt><dd class="mono">{{ templateDetail.key }}</dd></div>
          <div><dt>当前版本</dt><dd>{{ templateDetail.current_version ? `v${templateDetail.current_version.version}` : '无' }}</dd></div>
          <div><dt>全部引用</dt><dd>{{ templateDetail.reference_count }}</dd></div>
        </dl>
        <p class="detail-description">{{ templateDetail.description || '暂无补充说明。' }}</p>
        <el-tabs v-model="detailTab" class="detail-tabs">
          <el-tab-pane :label="`变量 (${currentVariables.length})`" name="variables">
            <el-empty v-if="!currentVariables.length" description="当前版本没有模板变量" :image-size="56" />
            <div v-else class="variable-list" aria-label="当前版本模板变量">
              <code v-for="variable in currentVariables" :key="variable">{{ variable }}</code>
            </div>
          </el-tab-pane>
          <el-tab-pane :label="`版本 (${templateDetail.versions.length})`" name="versions">
            <div class="detail-tab-actions">
              <span>切换当前版本不会改写已固定版本的 Action。</span>
              <el-button type="primary" plain :disabled="templateDetail.status !== 'active'" @click="openVersion(templateDetail)">添加版本</el-button>
            </div>
            <div class="version-list">
              <article v-for="version in sortedVersions" :key="version.id" class="version-card">
                <header>
                  <div><b>v{{ version.version }}</b><el-tag v-if="version.id === templateDetail.current_version_id" size="small" type="success">当前</el-tag></div>
                  <span>{{ formatDate(version.created_at) }}</span>
                </header>
                <p>{{ version.version_note || '未填写版本说明' }}</p>
                <dl>
                  <div><dt>文件</dt><dd>{{ version.filename }}</dd></div>
                  <div><dt>格式</dt><dd>{{ templateFormatLabel(version.artifact_format) }}</dd></div>
                  <div><dt>大小</dt><dd>{{ formatSize(version.size) }}</dd></div>
                  <div><dt>变量</dt><dd>{{ version.placeholder_paths.length }}</dd></div>
                </dl>
                <footer>
                  <a class="el-button el-button--small is-text" :href="`/api/data-sources/files/${version.bucket_file_id}/download`">下载原文件</a>
                  <el-button v-if="version.id !== templateDetail.current_version_id" text type="primary" :disabled="templateDetail.status !== 'active'" @click="setCurrentVersion(version.id)">设为当前</el-button>
                </footer>
              </article>
            </div>
          </el-tab-pane>
          <el-tab-pane :label="`当前 Action (${templateDetail.references.length})`" name="references">
            <el-alert v-if="releaseReferenceCount" :title="`另有 ${releaseReferenceCount} 条发布或治理快照引用；解除当前 Action 引用后仍不能直接删除模板。`" type="info" :closable="false" show-icon class="reference-release-alert" />
            <el-empty v-if="!templateDetail.references.length" :description="releaseReferenceCount ? '当前场景定义没有直接引用，发布或治理快照仍保留固定版本' : '尚未被任何 Action、发布或治理快照引用'" :image-size="56" />
            <div v-else class="reference-list">
              <article v-for="reference in templateDetail.references" :key="reference.action_id">
                <div><b>{{ reference.action_name }}</b><span>{{ reference.scenario_name }}<template v-if="reference.entity_name"> · {{ reference.entity_name }}</template></span></div>
                <el-tag effect="plain" size="small">{{ reference.uses_current ? '跟随当前' : `固定 v${reference.pinned_version || '—'}` }}</el-tag>
                <el-button text type="primary" @click="goToAction(reference)">查看 Action</el-button>
              </article>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { ArtifactTemplate, ArtifactTemplateDetail, ArtifactTemplateReference, BucketFile, DataSource, Scenario } from '@/types'
import { TEMPLATE_FILE_ACCEPT, isSupportedTemplateFilename, isTemplateBucketInScope, templateFormatLabel } from '@/utils/templates'

const route = useRoute()
const router = useRouter()
const templates = ref<ArtifactTemplate[]>([])
const scenarios = ref<Scenario[]>([])
const dataSources = ref<DataSource[]>([])
const loading = ref(true)
const loadError = ref('')
const operationNotice = reactive<{ type: 'success' | 'warning' | 'info' | 'error'; message: string }>({ type: 'success', message: '' })
const filters = reactive({
  q: typeof route.query.q === 'string' ? route.query.q : '',
  scenario_id: typeof route.query.scenario_id === 'string' ? route.query.scenario_id : '',
  artifact_format: typeof route.query.artifact_format === 'string' ? route.query.artifact_format : '',
  status: typeof route.query.status === 'string' ? route.query.status : 'active',
})
const writableBuckets = computed(() => dataSources.value.filter((source) => source.type === 'file_bucket' && source.can_write !== false && source.id))
const visibleTemplates = computed(() => filters.scenario_id === '__shared__'
  ? templates.value.filter((template) => !template.scenario_id)
  : templates.value)
const hasFilters = computed(() => Boolean(filters.q || filters.scenario_id || filters.artifact_format || filters.status !== 'active'))

let searchTimer: number | undefined
watch(() => [filters.q, filters.scenario_id, filters.artifact_format, filters.status], () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    void router.replace({ query: {
      ...(filters.q ? { q: filters.q } : {}),
      ...(filters.scenario_id ? { scenario_id: filters.scenario_id } : {}),
      ...(filters.artifact_format ? { artifact_format: filters.artifact_format } : {}),
      ...(filters.status ? { status: filters.status } : {}),
    } })
    void loadTemplates()
  }, 260)
})

async function loadResources() {
  try {
    const [scenarioRows, sourceRows] = await Promise.all([api.listScenarios(), api.listDataSources()])
    scenarios.value = scenarioRows
    dataSources.value = sourceRows
  } catch (error: any) {
    operationNotice.type = 'error'
    operationNotice.message = error?.message || '场景与文件桶加载失败，部分操作暂不可用'
  }
}

async function loadTemplates() {
  loading.value = true
  loadError.value = ''
  try {
    templates.value = await api.listTemplates({
      ...(filters.scenario_id && filters.scenario_id !== '__shared__' ? { scenario_id: filters.scenario_id } : {}),
      ...(filters.status ? { status: filters.status } : {}),
      ...(filters.artifact_format ? { artifact_format: filters.artifact_format } : {}),
      ...(filters.q.trim() ? { q: filters.q.trim() } : {}),
    })
  } catch (error: any) {
    loadError.value = error?.message || '请稍后重试。'
  } finally {
    loading.value = false
  }
}

function clearFilters() {
  Object.assign(filters, { q: '', scenario_id: '', artifact_format: '', status: 'active' })
}
function scenarioName(id?: string | null) {
  if (!id) return '租户共享'
  return scenarios.value.find((scenario) => scenario.id === id)?.name || '未知场景'
}
function formatShort(format?: string) {
  return ({ docx: 'W', xlsx: 'X', markdown: 'M' } as Record<string, string>)[String(format || '')] || '—'
}
function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date)
}
function formatSize(size = 0) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
function deleteDisabledReason(template: ArtifactTemplate) {
  if (template.reference_count) return `仍有 ${template.reference_count} 条 Action、发布或治理快照引用，请先解除引用`
  return '当前模板暂不可删除'
}
function goToDataSources() {
  void router.push({ name: 'data-sources', query: { return_to: route.fullPath } })
}

const createDialog = ref(false)
const createSaving = ref(false)
const createError = ref('')
const uploadProgress = ref(0)
const createForm = reactive({
  mode: 'upload' as 'upload' | 'register', scenario_id: '', data_source_id: '', file: null as File | null,
  file_id: '', name: '', purpose: '', description: '', key: '', version_note: '',
})
const registrationFiles = ref<BucketFile[]>([])
const registrationFilesLoading = ref(false)
const createBuckets = computed(() => writableBuckets.value.filter((bucket) => isTemplateBucketInScope(bucket.scenario_id, createForm.scenario_id)))
const canSubmitCreate = computed(() => Boolean(
  createForm.data_source_id && createForm.name.trim() && createForm.purpose.trim()
  && (createForm.mode === 'upload' ? createForm.file : createForm.file_id),
))

function openCreate() {
  resetCreateForm()
  const scenarioId = filters.scenario_id && filters.scenario_id !== '__shared__' ? filters.scenario_id : ''
  createForm.scenario_id = scenarioId
  createForm.data_source_id = createBuckets.value[0]?.id || ''
  createDialog.value = true
}
function resetCreateForm() {
  Object.assign(createForm, { mode: 'upload', scenario_id: '', data_source_id: '', file: null, file_id: '', name: '', purpose: '', description: '', key: '', version_note: '' })
  registrationFiles.value = []
  createError.value = ''
  uploadProgress.value = 0
}
function onCreateScenarioChanged() {
  if (!createBuckets.value.some((bucket) => bucket.id === createForm.data_source_id)) createForm.data_source_id = createBuckets.value[0]?.id || ''
  createForm.file_id = ''
  registrationFiles.value = []
  if (createForm.mode === 'register') void loadRegistrationFiles()
}
function onCreateFilePicked(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] || null
  if (file && !isSupportedTemplateFilename(file.name)) {
    createForm.file = null
    createError.value = '仅支持 .docx、.xlsx、.md 或 .markdown 模板文件'
    return
  }
  createForm.file = file
  createError.value = ''
  if (file && !createForm.name) createForm.name = file.name.replace(/\.(docx|xlsx|md|markdown)$/i, '')
}
async function loadRegistrationFiles() {
  createForm.file_id = ''
  registrationFiles.value = []
  if (createForm.mode !== 'register' || !createForm.data_source_id) return
  registrationFilesLoading.value = true
  try {
    const rows = await api.listFiles(createForm.data_source_id)
    registrationFiles.value = rows.filter((file) => isSupportedTemplateFilename(file.filename) && file.status !== 'error')
  } catch (error: any) {
    createError.value = error?.message || '桶内文件加载失败'
  } finally {
    registrationFilesLoading.value = false
  }
}
watch(() => createForm.mode, () => {
  createError.value = ''
  if (createForm.mode === 'register') void loadRegistrationFiles()
})
async function createTemplate() {
  if (!canSubmitCreate.value || createSaving.value) return
  createSaving.value = true
  createError.value = ''
  uploadProgress.value = 0
  try {
    if (createForm.mode === 'upload' && createForm.file) {
      await api.uploadTemplate({
        file: createForm.file, data_source_id: createForm.data_source_id, scenario_id: createForm.scenario_id || null,
        name: createForm.name, purpose: createForm.purpose, description: createForm.description,
        key: createForm.key, version_note: createForm.version_note, onProgress: (percent) => { uploadProgress.value = percent },
      })
    } else {
      await api.registerTemplate({
        file_id: createForm.file_id, scenario_id: createForm.scenario_id || null, name: createForm.name,
        purpose: createForm.purpose, description: createForm.description, key: createForm.key, version_note: createForm.version_note,
      })
    }
    createDialog.value = false
    operationNotice.type = 'success'
    operationNotice.message = `模板“${createForm.name}”已登记，可在 Action 中直接选择`
    ElMessage.success('模板已保存')
    await loadTemplates()
  } catch (error: any) {
    createError.value = error?.message || '模板保存失败'
  } finally {
    createSaving.value = false
  }
}

const editDialog = ref(false)
const editSaving = ref(false)
const editError = ref('')
const editForm = reactive({ id: '', name: '', purpose: '', description: '', key: '', scenario_id: '' })
function openEdit(template: ArtifactTemplate) {
  Object.assign(editForm, {
    id: template.id, name: template.name, purpose: template.purpose || '', description: template.description || '',
    key: template.key, scenario_id: template.scenario_id || '',
  })
  editError.value = ''
  editDialog.value = true
}
async function saveMetadata() {
  if (!editForm.id || !editForm.name || !editForm.purpose || editSaving.value) return
  editSaving.value = true
  editError.value = ''
  try {
    await api.updateTemplate(editForm.id, {
      name: editForm.name, purpose: editForm.purpose, description: editForm.description,
      key: editForm.key, scenario_id: editForm.scenario_id || null,
    })
    editDialog.value = false
    operationNotice.type = 'success'
    operationNotice.message = '模板信息已更新'
    ElMessage.success('已保存模板信息')
    await loadTemplates()
    if (templateDetail.value?.id === editForm.id) await reloadDetail()
  } catch (error: any) {
    editError.value = error?.message || '模板信息保存失败'
  } finally {
    editSaving.value = false
  }
}

const versionDialog = ref(false)
const versionSaving = ref(false)
const versionError = ref('')
const versionFilesLoading = ref(false)
const versionTarget = ref<ArtifactTemplate | null>(null)
const versionRegistrationFiles = ref<BucketFile[]>([])
const versionForm = reactive({ mode: 'upload' as 'upload' | 'register', data_source_id: '', file: null as File | null, file_id: '', version_note: '', set_current: true })
const versionBuckets = computed(() => writableBuckets.value.filter((bucket) => isTemplateBucketInScope(bucket.scenario_id, versionTarget.value?.scenario_id)))
const canSubmitVersion = computed(() => Boolean(versionForm.data_source_id && (versionForm.mode === 'upload' ? versionForm.file : versionForm.file_id)))
function openVersion(template: ArtifactTemplate) {
  if (template.status !== 'active') return ElMessage.warning('已停用模板不能添加新版本，请先恢复模板')
  resetVersionForm()
  versionTarget.value = template
  versionForm.data_source_id = template.current_version?.data_source_id && versionBuckets.value.some((bucket) => bucket.id === template.current_version?.data_source_id)
    ? template.current_version.data_source_id
    : versionBuckets.value[0]?.id || ''
  versionDialog.value = true
}
function resetVersionForm() {
  Object.assign(versionForm, { mode: 'upload', data_source_id: '', file: null, file_id: '', version_note: '', set_current: true })
  versionRegistrationFiles.value = []
  versionError.value = ''
  uploadProgress.value = 0
}
function onVersionFilePicked(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] || null
  if (file && !isSupportedTemplateFilename(file.name)) {
    versionForm.file = null
    versionError.value = '仅支持 .docx、.xlsx、.md 或 .markdown 模板文件'
    return
  }
  versionForm.file = file
  versionError.value = ''
}
async function loadVersionRegistrationFiles() {
  versionForm.file_id = ''
  versionRegistrationFiles.value = []
  if (versionForm.mode !== 'register' || !versionForm.data_source_id) return
  versionFilesLoading.value = true
  try {
    const rows = await api.listFiles(versionForm.data_source_id)
    versionRegistrationFiles.value = rows.filter((file) => isSupportedTemplateFilename(file.filename) && file.status !== 'error')
  } catch (error: any) {
    versionError.value = error?.message || '桶内文件加载失败'
  } finally {
    versionFilesLoading.value = false
  }
}
watch(() => versionForm.mode, () => {
  versionError.value = ''
  if (versionForm.mode === 'register') void loadVersionRegistrationFiles()
})
async function createVersion() {
  if (!versionTarget.value || !canSubmitVersion.value || versionSaving.value) return
  versionSaving.value = true
  versionError.value = ''
  uploadProgress.value = 0
  try {
    if (versionForm.mode === 'upload' && versionForm.file) {
      await api.uploadTemplateVersion(versionTarget.value.id, {
        file: versionForm.file, data_source_id: versionForm.data_source_id, version_note: versionForm.version_note,
        set_current: versionForm.set_current, onProgress: (percent) => { uploadProgress.value = percent },
      })
    } else {
      await api.registerTemplateVersion(versionTarget.value.id, {
        file_id: versionForm.file_id, version_note: versionForm.version_note, set_current: versionForm.set_current,
      })
    }
    versionDialog.value = false
    operationNotice.type = 'success'
    operationNotice.message = `模板“${versionTarget.value.name}”的新版本已添加`
    ElMessage.success('新版本已添加')
    await loadTemplates()
    if (templateDetail.value?.id === versionTarget.value.id) await reloadDetail()
  } catch (error: any) {
    versionError.value = error?.message || '新版本添加失败'
  } finally {
    versionSaving.value = false
  }
}

const detailDrawer = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detailTab = ref('variables')
const templateDetail = ref<ArtifactTemplateDetail | null>(null)
const detailTemplateId = ref('')
const currentVariables = computed(() => templateDetail.value?.current_version?.placeholder_paths || [])
const sortedVersions = computed(() => [...(templateDetail.value?.versions || [])].sort((a, b) => b.version - a.version))
const releaseReferenceCount = computed(() => Math.max(0, Number(templateDetail.value?.reference_count || 0) - Number(templateDetail.value?.references.length || 0)))
async function openDetail(template: ArtifactTemplate) {
  detailDrawer.value = true
  detailTab.value = 'variables'
  detailTemplateId.value = template.id
  templateDetail.value = null
  detailError.value = ''
  await loadDetail(template.id)
}
async function loadDetail(id: string) {
  detailLoading.value = true
  detailError.value = ''
  try {
    templateDetail.value = await api.getTemplate(id)
  } catch (error: any) {
    detailError.value = error?.message || '模板详情加载失败'
  } finally {
    detailLoading.value = false
  }
}
async function reloadDetail() {
  const id = templateDetail.value?.id || detailTemplateId.value
  if (id) await loadDetail(id)
}
async function setCurrentVersion(versionId: string) {
  if (!templateDetail.value) return
  try {
    templateDetail.value = await api.updateTemplate(templateDetail.value.id, { current_version_id: versionId })
    operationNotice.type = 'success'
    operationNotice.message = `模板“${templateDetail.value.name}”的当前版本已切换`
    ElMessage.success('当前版本已切换')
    await loadTemplates()
  } catch (error: any) {
    ElMessage.error(error?.message || '当前版本切换失败')
  }
}
function goToAction(reference: ArtifactTemplateReference) {
  void router.push({ name: 'scenario-detail', params: { id: reference.scenario_id }, query: { stage: 'actions', edit_action_id: reference.action_id, return_to: route.fullPath } })
}

async function onRowCommand(command: string, template: ArtifactTemplate) {
  if (command === 'version') return openVersion(template)
  if (command === 'deprecate') return changeStatus(template, 'deprecated')
  if (command === 'activate') return changeStatus(template, 'active')
  if (command === 'delete') return removeTemplate(template)
}
async function changeStatus(template: ArtifactTemplate, status: 'active' | 'deprecated') {
  if (status === 'deprecated') {
    try {
      await ElMessageBox.confirm(
        '停用后不能建立新的 Action 绑定，但已固定版本的现有 Action 可继续执行。确认停用？',
        `停用模板“${template.name}”`,
        { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  try {
    if (status === 'active') await api.activateTemplate(template.id)
    else await api.deprecateTemplate(template.id)
    operationNotice.type = 'success'
    operationNotice.message = status === 'active' ? `模板“${template.name}”已恢复` : `模板“${template.name}”已停用`
    await loadTemplates()
  } catch (error: any) {
    operationNotice.type = 'error'
    operationNotice.message = error?.message || '模板状态更新失败'
  }
}
async function removeTemplate(template: ArtifactTemplate) {
  if (!template.deletable) return ElMessage.warning(deleteDisabledReason(template))
  try {
    await ElMessageBox.confirm('模板及其版本记录将被删除；文件桶中的原文件由文件桶独立管理。确认继续？', `删除模板“${template.name}”`, {
      type: 'error', confirmButtonText: '删除模板', cancelButtonText: '取消',
    })
    await api.deleteTemplate(template.id)
    operationNotice.type = 'success'
    operationNotice.message = `模板“${template.name}”已删除`
    await loadTemplates()
  } catch (error: any) {
    if (error === 'cancel' || error?.message === 'cancel') return
    operationNotice.type = 'error'
    operationNotice.message = error?.message || '模板删除失败'
  }
}

onMounted(async () => {
  await Promise.all([loadResources(), loadTemplates()])
})
</script>

<style scoped>
.template-page { position: relative; }
.eyebrow { margin-bottom: 5px; color: var(--primary-600); font-size: 10px; font-weight: 800; letter-spacing: 1.6px; }
.operation-notice { margin-bottom: 14px; }
.filter-card { padding: 16px 18px 12px; }
.filter-grid { display: grid; grid-template-columns: minmax(230px, 1.6fr) repeat(3, minmax(150px, .75fr)); gap: 12px; }
.filter-field { min-width: 0; display: grid; gap: 6px; color: var(--text-2); font-size: 11px; font-weight: 700; }
.filter-field :deep(.el-select) { width: 100%; }
.filter-summary { display: flex; min-height: 34px; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; color: var(--text-3); font-size: 12px; }
.template-list-card { min-height: 360px; padding: 0; overflow: hidden; }
.template-table { width: 100%; }
.template-name-button { display: flex; width: 100%; min-width: 0; align-items: center; gap: 10px; padding: 6px 0; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; font: inherit; }
.template-name-button > span:last-child { min-width: 0; display: grid; gap: 3px; }
.template-name-button b { overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
.template-name-button small { overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.format-mark { display: inline-flex; flex: 0 0 36px; width: 36px; height: 36px; align-items: center; justify-content: center; border: 1px solid var(--border); border-radius: 10px; font-size: 12px; font-weight: 850; }
.format-mark.large { flex-basis: 48px; width: 48px; height: 48px; border-radius: 13px; font-size: 15px; }
.format-docx { border-color: color-mix(in srgb, var(--primary) 32%, var(--border)); background: color-mix(in srgb, var(--primary) 10%, var(--surface)); color: var(--primary-600); }
.format-xlsx { border-color: color-mix(in srgb, var(--success) 30%, var(--border)); background: color-mix(in srgb, var(--success) 9%, var(--surface)); color: var(--success); }
.format-markdown { border-color: var(--border-strong); background: var(--surface-2); color: var(--text-2); }
.format-unknown { background: var(--surface-2); color: var(--text-3); }
.version-cell { display: grid; gap: 1px; }
.version-cell b { color: var(--text); }
.version-cell span, .version-cell small { color: var(--text-3); font-size: 10.5px; }
.row-actions { display: flex; align-items: center; gap: 0; }
.row-actions :deep(.el-button + .el-button) { margin-left: 0; }
.empty-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.template-cards { display: none; }
.source-mode { margin: 16px 0 18px; }
.template-form > .el-alert { margin-bottom: 0; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.form-grid :deep(.el-select) { width: 100%; }
.field-help { margin-top: 5px; color: var(--text-3); font-size: 11px; line-height: 1.5; }
.field-error, .dialog-error { color: var(--danger); font-size: 12px; line-height: 1.5; }
.dialog-error { margin-top: 12px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--border)); border-radius: 9px; background: var(--danger-soft); }
.file-picker { display: flex; width: 100%; min-height: 78px; align-items: center; gap: 12px; padding: 14px; border: 1px dashed var(--border-strong); border-radius: 12px; background: var(--surface-2); color: var(--text-2); cursor: pointer; transition: border-color var(--dur), background var(--dur); }
.file-picker:hover, .file-picker:focus-within { border-color: var(--primary); background: var(--primary-soft); }
.file-picker .el-icon { flex: 0 0 auto; color: var(--primary-600); font-size: 24px; }
.file-picker span { min-width: 0; display: grid; gap: 4px; }
.file-picker b { overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
.file-picker small { color: var(--text-3); font-size: 11px; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }
.drawer-title { display: grid; gap: 2px; }
.drawer-title span { color: var(--text); font-size: 16px; font-weight: 760; }
.drawer-title small { max-width: 460px; overflow: hidden; color: var(--text-3); text-overflow: ellipsis; white-space: nowrap; }
.detail-content { display: grid; gap: 16px; }
.detail-hero { display: flex; align-items: center; gap: 12px; }
.detail-hero > div { min-width: 0; flex: 1; }
.detail-hero h2 { margin: 0; color: var(--text); font-size: 20px; }
.detail-hero p { margin: 3px 0 0; color: var(--text-2); }
.detail-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 0; overflow: hidden; border: 1px solid var(--border); border-radius: 12px; background: var(--border); }
.detail-facts div { min-width: 0; padding: 11px; background: var(--surface-2); }
.detail-facts dt, .version-card dt { color: var(--text-3); font-size: 10px; }
.detail-facts dd, .version-card dd { min-width: 0; margin: 3px 0 0; overflow-wrap: anywhere; color: var(--text); font-size: 12px; }
.detail-description { margin: 0; padding: 12px 14px; border-left: 3px solid var(--primary); background: var(--primary-soft); color: var(--text-2); line-height: 1.65; }
.detail-tabs { min-width: 0; }
.variable-list { display: flex; flex-wrap: wrap; gap: 7px; padding: 6px 0; }
.variable-list code { max-width: 100%; padding: 5px 9px; overflow-wrap: anywhere; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-2); color: var(--primary-600); font-size: 11.5px; }
.detail-tab-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; color: var(--text-3); font-size: 11px; }
.version-list, .reference-list { display: grid; gap: 10px; }
.reference-release-alert { margin-bottom: 10px; }
.version-card { padding: 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); }
.version-card header, .version-card header div, .version-card footer { display: flex; align-items: center; gap: 8px; }
.version-card header { justify-content: space-between; }
.version-card header > span { color: var(--text-3); font-size: 11px; }
.version-card p { margin: 8px 0; color: var(--text-2); font-size: 12px; }
.version-card dl { display: grid; grid-template-columns: 1.6fr repeat(3, .7fr); gap: 8px; margin: 0; }
.version-card dl div { min-width: 0; }
.version-card footer { justify-content: flex-end; margin-top: 9px; border-top: 1px solid var(--border); padding-top: 7px; }
.version-card footer a { text-decoration: none; }
.reference-list article { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }
.reference-list article > div { min-width: 0; display: grid; gap: 3px; }
.reference-list b { overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
.reference-list span { color: var(--text-3); font-size: 11px; }

@media (max-width: 1100px) {
  .filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .filter-grid, .form-grid { grid-template-columns: minmax(0, 1fr); }
  .template-table { display: none; }
  .template-list-card { padding: 12px; }
  .template-cards { display: grid; gap: 10px; }
  .template-card { padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface-2); }
  .template-card header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 10px; }
  .template-card h2 { margin: 1px 0 2px; overflow-wrap: anywhere; color: var(--text); font-size: 14px; }
  .template-card p { margin: 0; color: var(--text-3); font-size: 11px; line-height: 1.5; }
  .template-card dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }
  .template-card dl div { padding: 8px; border-radius: 8px; background: var(--surface); }
  .template-card dt { color: var(--text-3); font-size: 10px; }
  .template-card dd { margin: 2px 0 0; color: var(--text); font-size: 12px; }
  .template-card footer { display: flex; gap: 8px; }
  .template-card footer .el-button { flex: 1; margin: 0; }
  .detail-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .version-card dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .reference-list article { grid-template-columns: minmax(0, 1fr) auto; }
  .reference-list article .el-button { grid-column: 1 / -1; justify-self: stretch; }
}
@media (max-width: 420px) {
  .filter-grid, .detail-facts { grid-template-columns: minmax(0, 1fr); }
  .detail-hero { align-items: flex-start; flex-wrap: wrap; }
  .detail-hero .el-tag { margin-left: 60px; }
}
</style>
