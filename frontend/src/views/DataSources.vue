<template>
  <div class="page data-sources-page">
    <div class="page-header">
      <div>
        <h1>建模资料</h1>
        <div class="sub">上传历史样本、参考文档或连接样本数据库，帮助平台理解业务场景</div>
      </div>
      <div class="data-source-header-actions">
        <el-button v-if="returnPath" @click="returnToPreviousFlow"><el-icon><ArrowLeft /></el-icon> 返回上一步</el-button>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新增建模资料</el-button>
      </div>
    </div>

    <section class="card resource-boundary" aria-labelledby="resource-boundary-title">
      <header>
        <span class="eyebrow">DATA BOUNDARY</span>
        <h2 id="resource-boundary-title">这里保存的是场景认知材料，不是客户永远不变的业务数据</h2>
      </header>
      <div class="resource-boundary-grid">
        <article>
          <strong>本页全部都是建模资料</strong>
          <p>历史 Excel、文档、规则、表结构和数据库连接默认都可用于场景建模，帮助业务专家与智能顾问理解字段、关系和业务规律，无需再声明用途。</p>
        </article>
        <article>
          <strong>正式运行数据</strong>
          <p>只来自验证对话或第三方 Agent 当次上传的文件，以及创建 Agent 时专门配置的业务数据库；本页任何资料都不会自动进入正式调用。</p>
        </article>
      </div>
    </section>

    <section class="resource-section-group" aria-labelledby="connections-title">
      <header class="resource-group-heading">
        <div><span class="eyebrow">MODELING MATERIALS</span><h2 id="connections-title">资料文件与数据库样本</h2></div>
        <p>本页所有资料默认都可用于场景建模，无需选择场景或声明用途。</p>
      </header>
        <el-alert
          class="physical-connection-note"
          type="info"
          :closable="false"
          show-icon
          title="本页连接和文件永远只用于建模"
          description="正式验证数据只来自对话上传附件或 Agent 创建/编辑时单独配置的业务数据库。"
        />
        <el-row :gutter="16" class="physical-workspace">
      <el-col class="data-sources-list-col" :xs="24" :md="9">
        <div class="card data-sources-list-card" v-loading="loading">
          <div class="card-title"><el-icon><Coin /></el-icon> 建模资料</div>
          <div class="ds-list">
            <button v-for="ds in dataSources" :key="ds.id" type="button" class="ds-item" :class="{ active: selected?.id === ds.id }" :aria-current="selected?.id === ds.id ? 'true' : undefined" :aria-label="`选择数据源：${ds.name}`" @click="select(ds)">
              <div class="ds-icon" :class="ds.type">
                <el-icon :size="18"><component :is="ds.type === 'file_bucket' ? 'FolderOpened' : 'Coin'" /></el-icon>
              </div>
              <div class="ds-info">
                <div class="ds-name">{{ ds.name }}</div>
                <div class="muted">{{ typeLabel(ds.type) }} · {{ dataSourceLocationLabel(ds) }}</div>
              </div>
              <el-tag v-if="ds.status === 'ok'" size="small" type="success">正常</el-tag>
              <el-tag v-else-if="ds.status === 'error'" size="small" type="danger">异常</el-tag>
              <el-tag v-else size="small" type="info">未测试</el-tag>
            </button>
            <div v-if="!loading && !dataSources.length" class="empty-wrap">
              <div class="empty-icon"><el-icon :size="26"><Coin /></el-icon></div>
              <div>暂无建模资料</div>
              <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新增建模资料</el-button>
            </div>
          </div>
        </div>
      </el-col>

      <el-col class="data-source-detail-col" :xs="24" :md="15">
        <div v-if="selected" class="card data-source-detail-card">
          <div class="card-title">
            <el-icon><Setting /></el-icon> {{ selected.name }}
            <el-tag size="small" type="info">{{ typeLabel(selected.type) }}</el-tag>
            <el-tag v-if="!selected.can_write" size="small" type="warning" effect="plain">只读公开资源</el-tag>
            <div style="margin-left:auto;display:flex;gap:6px">
              <template v-if="selected.can_write">
                <el-button size="small" @click="testConn" :loading="testing"><el-icon><Link /></el-icon> 测试连接</el-button>
                <el-button size="small" @click="openEdit(selected)"><el-icon><Edit /></el-icon> 编辑</el-button>
                <el-button size="small" type="danger" @click="remove(selected)" aria-label="删除建模资料" title="删除建模资料"><el-icon aria-hidden="true"><Delete /></el-icon></el-button>
              </template>
            </div>
          </div>

          <!-- 数据库表结构；原始 SQL 仅保留为后端管理诊断能力，不向普通业务用户开放。 -->
          <template v-if="selected.type !== 'file_bucket'">
            <section class="database-tables" aria-labelledby="database-tables-title">
              <h3 id="database-tables-title">数据表</h3>
              <el-table :data="tables" size="small" max-height="520">
                <el-table-column prop="name" label="表名" min-width="160">
                  <template #default="{ row }"><button type="button" class="table-open mono" @click="openTable(row)">{{ row.name }}</button></template>
                </el-table-column>
                <el-table-column prop="row_count" label="行数" width="90" align="right" />
                <el-table-column label="字段" min-width="220">
                  <template #default="{ row }">
                    <el-tag v-for="c in row.columns.slice(0, 5)" :key="c.name" size="small" effect="plain" style="margin:2px">
                      {{ c.name }}<el-icon v-if="c.pk" class="pk-icon" aria-label="主键" title="主键"><Key /></el-icon>
                    </el-tag>
                    <span class="muted" v-if="row.columns.length > 5">+{{ row.columns.length - 5 }}</span>
                  </template>
                </el-table-column>
              </el-table>
              <el-button size="small" style="margin-top:10px" @click="loadTables" :loading="loadingTables">
                <el-icon><Refresh /></el-icon> 刷新表列表
              </el-button>
            </section>
          </template>

          <!-- 文件桶 -->
          <template v-else>
            <div class="bucket-detail">
              <el-alert
                v-if="!selected.can_write"
                title="这是可检索的公开资料库；当前账号只有读取权限，不能上传、重新解析、重建索引或删除文件。"
                type="info"
                :closable="false"
                show-icon
                class="readonly-note"
              />
              <el-upload v-if="selected.can_write" drag :auto-upload="false" :file-list="uploadList" :on-change="onFilePick" :on-remove="() => {}" multiple>
                <el-icon class="el-icon--upload" :size="40"><UploadFilled /></el-icon>
                <div class="el-upload__text">拖拽文件到此处，或 <em>点击选择</em></div>
                <template #tip>
                  <div class="el-upload__tip">支持 Excel / Word / PPT / PDF / 图片 / TXT / MD / CSV / JSON，上传后自动解析为可检索文本</div>
                </template>
              </el-upload>
              <div class="bucket-actions">
                <el-button type="primary" :loading="uploading" :disabled="!selected.can_write || !uploadList.length" @click="doUpload">
                  <el-icon><Upload /></el-icon> 上传并解析（{{ uploadList.length }}）
                </el-button>
                <el-button :loading="reindexing" :disabled="!selected.can_write" @click="reindexFiles">
                  <el-icon><Refresh /></el-icon> 重建索引
                </el-button>
                <el-button @click="loadFiles" :loading="loadingFiles"><el-icon><Refresh /></el-icon> 刷新</el-button>
              </div>
              <section class="retrieval-panel" aria-labelledby="retrieval-title">
                <div class="retrieval-head">
                  <div>
                    <span class="eyebrow">RETRIEVAL PREVIEW</span>
                    <h3 id="retrieval-title">检索预览</h3>
                    <p>只检索当前有权访问的资料库；每条结果都保留原文定位与引用编号。</p>
                  </div>
                  <el-tag type="info" effect="plain">向量 + 关键词</el-tag>
                </div>
                <div class="retrieval-query">
                  <el-input v-model="retrievalQuery" clearable aria-label="资料库检索词" placeholder="输入要验证的业务问题或关键词" @keyup.enter="searchDocuments" />
                  <el-button type="primary" :loading="searching" :disabled="!retrievalQuery.trim()" @click="searchDocuments">
                    检索
                  </el-button>
                </div>
                <p v-if="searchError" class="retrieval-error" role="alert">{{ searchError }}</p>
                <p v-if="searchNotice" class="retrieval-notice" aria-live="polite">{{ searchNotice }}</p>
                <p v-if="searched && !searchError" class="retrieval-summary" aria-live="polite">
                  {{ searchResults.length ? `找到 ${searchResults.length} 条可引用资料` : '没有找到匹配资料；请更换关键词、确认文件已解析并检查索引状态。' }}
                </p>
                <div v-if="searchResults.length" class="citation-list" aria-label="检索引用结果">
                  <article v-for="citation in searchResults" :key="citation.chunk_id" class="citation-card">
                    <div class="citation-meta">
                      <span class="citation-id">{{ citation.citation_id }}</span>
                      <div>
                        <strong>{{ citation.filename }}</strong>
                        <small>{{ citation.data_source_name }} · 字符 {{ citation.char_start }}–{{ citation.char_end }} · 相似度 {{ Math.round(citation.score * 100) }}%</small>
                      </div>
                      <el-button size="small" text type="primary" :aria-label="`查看 ${citation.filename} 原文`" @click="viewCitation(citation)">查看原文</el-button>
                    </div>
                    <p>{{ citation.text }}</p>
                  </article>
                </div>
              </section>
              <el-empty v-if="!loadingFiles && !files.length" description="资料库暂无文件；上传资料后即可建立检索索引。" class="bucket-empty" />
              <el-table v-else :data="files" size="small" max-height="560">
                <el-table-column prop="filename" label="文件名" min-width="180">
                  <template #default="{ row }"><span class="mono">{{ row.filename }}</span></template>
                </el-table-column>
                <el-table-column label="大小" width="90" align="right">
                  <template #default="{ row }">{{ fmtSize(row.size) }}</template>
                </el-table-column>
                <el-table-column label="状态" width="90" align="center">
                  <template #default="{ row }">
                    <el-tag v-if="row.status === 'parsed'" size="small" type="success">已解析</el-tag>
                    <el-tag v-else-if="row.status === 'error'" size="small" type="danger">失败</el-tag>
                    <el-tag v-else size="small" type="warning">{{ fileStatusLabel(row.status) }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="检索索引" width="142" align="center">
                  <template #default="{ row }">
                    <el-tag :type="indexTagType(row.index_status)" size="small">{{ indexLabel(row.index_status) }}</el-tag>
                    <small v-if="row.chunk_count" class="index-count">{{ row.chunk_count }} 段</small>
                    <small v-if="row.index_error" class="index-error" :title="row.index_error">{{ row.index_error }}</small>
                  </template>
                </el-table-column>
                <el-table-column label="" width="170" align="center">
                  <template #default="{ row }">
                    <el-button size="small" text type="primary" @click="viewText(row)">查看文本</el-button>
                    <el-button v-if="selected?.can_write" size="small" text @click="reparse(row)" :loading="row._loading">重解析</el-button>
                    <el-button v-if="selected?.can_write" size="small" text type="danger" @click="removeFile(row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </template>
        </div>
        <el-empty v-else description="选择左侧建模资料查看详情" />
      </el-col>
        </el-row>
    </section>

    <!-- 新建/编辑建模资料 -->
    <el-dialog v-model="dlg" :title="form.id ? '编辑建模资料' : '新增建模资料'" width="560px">
      <el-form :model="form" label-width="90px" class="data-source-form">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：历史业务样本、规则参考资料" /></el-form-item>
        <el-form-item label="类型" required>
          <el-radio-group v-model="form.type" @change="onTypeChange">
            <el-radio value="postgres">PostgreSQL</el-radio>
            <el-radio value="file_bucket">文件桶</el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.type === 'postgres'">
          <el-row :gutter="10">
            <el-col :span="14"><el-form-item label="主机"><el-input v-model="form.config.host" placeholder="127.0.0.1" /></el-form-item></el-col>
            <el-col :span="10"><el-form-item label="端口"><el-input v-model.number="form.config.port" placeholder="5432" /></el-form-item></el-col>
          </el-row>
          <el-row :gutter="10">
            <el-col :span="14"><el-form-item label="数据库"><el-input v-model="form.config.database" /></el-form-item></el-col>
            <el-col :span="10"><el-form-item label="用户名"><el-input v-model="form.config.username" /></el-form-item></el-col>
          </el-row>
          <el-form-item label="密码"><el-input v-model="form.config.password" type="password" show-password /></el-form-item>
        </template>
        <template v-else>
          <el-form-item label="说明"><div class="muted">文件桶用于上传业务文档（Excel / Word / PDF / 图片等），平台自动解析为文本供 Agent 检索。</div></el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dlg=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 表详情 -->
    <el-dialog v-model="tableDlg" :title="'表结构：' + (curTable?.name || '')" width="640px">
      <el-table :data="curTable?.columns || []" size="small">
        <el-table-column prop="name" label="字段" min-width="160">
          <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="140">
          <template #default="{ row }"><span class="mono">{{ row.type }}</span></template>
        </el-table-column>
        <el-table-column label="主键" width="70" align="center">
          <template #default="{ row }"><el-tag v-if="row.pk" size="small" type="warning">PK</el-tag></template>
        </el-table-column>
      </el-table>
      <div class="muted" style="margin-top:8px">共 {{ curTable?.row_count }} 行</div>
    </el-dialog>

    <!-- 文件文本 -->
    <el-dialog v-model="textDlg" :title="textFile" width="720px" top="6vh">
      <pre class="code text-file-preview">{{ textContent }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { api } from '@/api'
import type {
  BucketFile,
  DataSource,
  RagCitation,
  TableInfo,
} from '@/types'
import { dataSourceLocationLabel } from '@/utils/dataSources'

const dataSources = ref<DataSource[]>([])
const selected = ref<DataSource | null>(null)
const loading = ref(false)
const route = useRoute()
const router = useRouter()
function safeReturnPath(value: unknown): string {
  const candidate = Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
  if (!candidate.startsWith('/') || candidate.startsWith('//') || candidate.includes('\\')) return ''
  return candidate
}
const returnPath = ref(safeReturnPath(route.query.return_to))

const dlg = ref(false)
const saving = ref(false)
const form = ref<Partial<DataSource> & { config: Record<string, any> }>({ type: 'postgres', config: {} })

const testing = ref(false)
const tables = ref<TableInfo[]>([])
const loadingTables = ref(false)
const tableDlg = ref(false)
const curTable = ref<TableInfo | null>(null)

const files = ref<(BucketFile & { _loading?: boolean })[]>([])
const loadingFiles = ref(false)
const uploadList = ref<UploadFile[]>([])
const uploading = ref(false)
const reindexing = ref(false)
const textDlg = ref(false)
const textFile = ref('')
const textContent = ref('')
const retrievalQuery = ref('')
const searchResults = ref<RagCitation[]>([])
const searching = ref(false)
const searched = ref(false)
const searchError = ref('')
const searchNotice = ref('')
let indexPollTimer: number | undefined
let viewDisposed = false
let loadRequest = 0
let tableRequest = 0
let fileRequest = 0
let searchRequest = 0
let testRequest = 0
let textRequest = 0

const TYPE_LABELS: Record<string, string> = {
  postgres: 'PostgreSQL', dataset: '数据集', file_bucket: '文件桶',
}
function typeLabel(t: string) { return TYPE_LABELS[t] || t }
function fmtSize(n: number) {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}
function indexLabel(status?: string) {
  return ({ indexed: '已建立', partial: '部分建立', queued: '排队中', pending: '待建立', error: '索引失败' } as Record<string, string>)[status || 'pending'] || '待建立'
}
function fileStatusLabel(status?: string) {
  return ({ pending: '待解析', queued: '排队中', parsing: '解析中', processing: '处理中', uploaded: '待解析' } as Record<string, string>)[status || 'pending'] || '处理中'
}
function indexTagType(status?: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'indexed') return 'success'
  if (status === 'partial') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

async function load() {
  const request = ++loadRequest
  loading.value = true
  try {
    const ds = await api.listDataSources()
    if (viewDisposed || request !== loadRequest) return
    dataSources.value = ds
    const requestedSource = Array.isArray(route.query.source_id) ? route.query.source_id[0] : route.query.source_id
    const nextSelection = ds.find((source) => source.id === requestedSource)
      || ds.find((source) => source.id === selected.value?.id)
      || ds[0]
    if (nextSelection) select(nextSelection, false)
    else clearSelection()
  } catch (e: any) {
    if (!viewDisposed && request === loadRequest) ElMessage.error('加载失败：' + e.message)
  } finally {
    if (!viewDisposed && request === loadRequest) loading.value = false
  }
}
function invalidateDetailRequests() {
  tableRequest += 1
  fileRequest += 1
  searchRequest += 1
  testRequest += 1
  textRequest += 1
  loadingTables.value = false
  loadingFiles.value = false
  searching.value = false
  testing.value = false
  tableDlg.value = false
  curTable.value = null
  textDlg.value = false
  textFile.value = ''
  textContent.value = ''
  if (indexPollTimer) window.clearTimeout(indexPollTimer)
  indexPollTimer = undefined
}
function clearSelection() {
  invalidateDetailRequests()
  selected.value = null
  tables.value = []
  files.value = []
}
function select(ds: DataSource, syncRoute = true) {
  invalidateDetailRequests()
  selected.value = ds
  tables.value = []
  files.value = []
  searchResults.value = []
  searched.value = false
  searchError.value = ''
  searchNotice.value = ''
  if (ds.type !== 'file_bucket') void loadTables()
  else void loadFiles()
  if (syncRoute && route.query.source_id !== ds.id) {
    void router.replace({ name: 'data-sources', query: { ...route.query, source_id: ds.id, view: 'connections' } })
  }
}
async function loadTables() {
  const sourceId = selected.value?.id
  if (!sourceId || selected.value?.type === 'file_bucket') return
  const request = ++tableRequest
  loadingTables.value = true
  try {
    const freshTables = await api.listTables(sourceId)
    if (viewDisposed || request !== tableRequest || selected.value?.id !== sourceId) return
    tables.value = freshTables
  } catch (e: any) {
    if (!viewDisposed && request === tableRequest && selected.value?.id === sourceId) {
      ElMessage.error('获取表列表失败：' + e.message)
    }
  } finally {
    if (!viewDisposed && request === tableRequest) loadingTables.value = false
  }
}
function openTable(t: TableInfo) {
  curTable.value = t
  tableDlg.value = true
}
async function testConn() {
  const source = selected.value
  if (!source?.id) return
  const request = ++testRequest
  testing.value = true
  try {
    const r: any = await api.testDataSource(source.id)
    if (viewDisposed || request !== testRequest || selected.value?.id !== source.id) return
    ElMessage.success(r.message || '连接成功')
    source.status = 'ok'
  } catch (e: any) {
    if (!viewDisposed && request === testRequest && selected.value?.id === source.id) {
      ElMessage.error('连接失败：' + e.message)
      source.status = 'error'
    }
  } finally {
    if (!viewDisposed && request === testRequest) testing.value = false
  }
}

// ── 文件桶 ──
function onFilePick(f: UploadFile) {
  if (!f.raw) {
    uploadList.value = uploadList.value.filter((x) => x.uid !== f.uid)
    return
  }
  if (!uploadList.value.some((x) => x.uid === f.uid)) {
    uploadList.value = [...uploadList.value, f]
  }
}
async function doUpload() {
  const raws = uploadList.value.filter((f) => f.raw).map((f) => f.raw!)
  if (!raws.length) return
  uploading.value = true
  try {
    await api.uploadFiles(selected.value!.id!, raws)
    ElMessage.success('资料已提交，正在后台解析并建立检索索引')
    uploadList.value = []
    await loadFiles()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    uploading.value = false
  }
}
async function loadFiles() {
  const sourceId = selected.value?.id
  if (!sourceId || selected.value?.type !== 'file_bucket') return
  const request = ++fileRequest
  loadingFiles.value = true
  try {
    const freshFiles = await api.listFiles(sourceId)
    if (viewDisposed || request !== fileRequest || selected.value?.id !== sourceId) return
    files.value = freshFiles
    if (indexPollTimer) window.clearTimeout(indexPollTimer)
    if (files.value.some((file) => ['pending', 'queued'].includes(file.index_status || 'pending'))) {
      indexPollTimer = window.setTimeout(() => {
        if (selected.value?.id === sourceId) void loadFiles()
      }, 1000)
    }
  } catch (e: any) {
    if (!viewDisposed && request === fileRequest && selected.value?.id === sourceId) {
      ElMessage.error('获取文件列表失败：' + e.message)
    }
  } finally {
    if (!viewDisposed && request === fileRequest) loadingFiles.value = false
  }
}
async function viewText(f: BucketFile) {
  const sourceId = selected.value?.id
  if (!sourceId) return
  const request = ++textRequest
  try {
    const r = await api.fileText(f.id)
    if (viewDisposed || request !== textRequest || selected.value?.id !== sourceId) return
    textFile.value = f.filename
    textContent.value = r.text || '（空）'
    textDlg.value = true
  } catch (e: any) {
    if (!viewDisposed && request === textRequest && selected.value?.id === sourceId) ElMessage.error(e.message)
  }
}
async function reparse(f: BucketFile & { _loading?: boolean }) {
  f._loading = true
  try {
    await api.reparseFile(f.id)
    ElMessage.success('已提交重新解析，正在后台更新检索索引')
    await loadFiles()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    f._loading = false
  }
}
async function reindexFiles() {
  if (!selected.value) return
  reindexing.value = true
  try {
    const result = await api.reindexFiles(selected.value.id!)
    ElMessage.success(
      result.jobs_queued
        ? `已排队重建 ${result.jobs_queued}/${result.files_total} 个文件的检索索引`
        : result.jobs_existing
          ? `${result.jobs_existing} 个文件已在检索队列中`
          : '没有需要重建的已解析文件',
    )
    await loadFiles()
  } catch (e: any) {
    ElMessage.error(e.message || '重建索引失败')
  } finally {
    reindexing.value = false
  }
}
async function searchDocuments() {
  const source = selected.value
  const query = retrievalQuery.value.trim()
  if (!source?.id || !query) return
  const request = ++searchRequest
  searching.value = true
  searched.value = false
  searchError.value = ''
  searchNotice.value = ''
  searchResults.value = []
  try {
    const result = await api.searchDocuments({
      query,
      data_source_ids: [source.id],
      scenario_id: source.scenario_id,
      top_k: 5,
    })
    if (viewDisposed || request !== searchRequest || selected.value?.id !== source.id) return
    searchResults.value = result.results || []
    searchNotice.value = result.permission_message || (
      source.can_write ? '' : '当前为只读公开资料库，结果仅来自已建立的公开索引。'
    )
    if (!searchResults.value.length && files.value.some((file) => ['pending', 'queued'].includes(file.index_status || 'pending'))) {
      searchNotice.value = '资料仍在后台解析或建立索引，请稍候自动刷新后再次检索。'
    }
    searched.value = true
  } catch (e: any) {
    if (!viewDisposed && request === searchRequest && selected.value?.id === source.id) {
      searchError.value = e.message || '检索失败，请稍后重试。'
      searched.value = true
    }
  } finally {
    if (!viewDisposed && request === searchRequest) searching.value = false
  }
}
function viewCitation(citation: RagCitation) {
  void viewText({ id: citation.file_id, filename: citation.filename } as BucketFile)
}
async function removeFile(f: BucketFile) {
  try {
    await ElMessageBox.confirm(`删除文件「${f.filename}」？`, '确认', { type: 'warning' })
    await api.deleteFile(f.id)
    ElMessage.success('已删除')
    await loadFiles()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}

// ── 建模资料新建/编辑 ──
function onTypeChange() {
  form.value.config =
    form.value.type === 'file_bucket'
      ? {}
      : { host: '127.0.0.1', port: 5432, database: '', username: 'postgres', password: '' }
}
function openCreate() {
  form.value = {
    name: '',
    scenario_id: undefined,
    type: 'postgres',
    config: { host: '127.0.0.1', port: 5432, database: '', username: 'postgres', password: '' },
  }
  dlg.value = true
}
function openEdit(ds: DataSource) {
  form.value = { ...ds, config: { ...ds.config } }
  dlg.value = true
}
async function save() {
  if (!form.value.name) return ElMessage.warning('请填写名称')
  saving.value = true
  try {
    const saved = form.value.id
      ? await api.updateDataSource(form.value.id, form.value)
      : await api.createDataSource(form.value)
    ElMessage.success('建模资料已保存')
    dlg.value = false
    if (saved.id) {
      await router.replace({ name: 'data-sources', query: { ...route.query, source_id: saved.id, view: 'connections' } })
    }
    await load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function remove(ds: DataSource) {
  try {
    await ElMessageBox.confirm(`删除建模资料「${ds.name}」？此操作可能同时影响其托管文件。`, '确认', { type: 'warning' })
    await api.deleteDataSource(ds.id!)
    clearSelection()
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}

async function returnToPreviousFlow() {
  if (returnPath.value) await router.push(returnPath.value)
}

onMounted(() => {
  viewDisposed = false
  void load()
})
watch(() => route.query.source_id, (value) => {
  const id = Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
  if (!id || id === selected.value?.id) return
  const source = dataSources.value.find((item) => item.id === id)
  if (source) select(source, false)
})
watch(() => route.query.return_to, (value) => {
  returnPath.value = safeReturnPath(value)
})
onBeforeUnmount(() => {
  viewDisposed = true
  loadRequest += 1
  invalidateDetailRequests()
  if (indexPollTimer) window.clearTimeout(indexPollTimer)
})
</script>

<style scoped>
.ds-item {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  margin-bottom: 6px;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.ds-item:hover { background: var(--surface-2); }
.ds-item.active {
  background: var(--primary-soft);
  border-color: var(--border-strong);
}
.ds-item:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 2px; }
.table-open { min-height: 44px; padding: 3px 0; border: 0; background: transparent; color: var(--primary-600); font-family: 'Cascadia Code', Consolas, monospace; text-align: left; cursor: pointer; }
.table-open:hover, .table-open:focus-visible { text-decoration: underline; text-underline-offset: 3px; }
.pk-icon { margin-left: 3px; color: var(--warning); vertical-align: -2px; }
.ds-icon {
  width: 38px; height: 38px;
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  background: var(--primary-soft); color: var(--primary-600);
}
.ds-icon.file_bucket { background: var(--warning-soft); color: var(--warning); }
.ds-info { flex: 1; min-width: 0; }
.ds-info .muted { overflow-wrap: anywhere; }
.ds-name {
  font-weight: 600;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ds-classification { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: 5px; margin-top: 5px; color: var(--warning); font-size: 10px; line-height: 1.35; }

/* ── 建模资料与连接工作区 ── */
.data-sources-page {
  min-height: 100%;
  box-sizing: border-box;
}
.resource-boundary { display: grid; gap: 14px; margin-bottom: 22px; }
.resource-boundary header h2 { margin: 4px 0 0; color: var(--text); font-size: clamp(17px, 2vw, 22px); line-height: 1.4; }
.resource-boundary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.resource-boundary-grid article { padding: 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); }
.resource-boundary-grid strong { display: block; margin-bottom: 5px; color: var(--text); font-size: 14px; }
.resource-boundary-grid p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.65; }
.resource-section-group { display: grid; gap: 14px; margin-top: 24px; }
.resource-group-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; }
.resource-group-heading h2 { margin: 3px 0 0; color: var(--text); font-size: 20px; }
.resource-group-heading p { max-width: 640px; margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.55; text-align: right; }
.catalog-principle, .catalog-error, .physical-connection-note { margin-bottom: 14px; }
.catalog-section { min-width: 0; margin-bottom: 16px; }
.catalog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
.catalog-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.catalog-section-head > div { min-width: 0; }
.catalog-section-head h2 { margin: 0; color: var(--text); font-size: 16px; line-height: 1.4; }
.catalog-section-head p { margin: 4px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.55; }
.catalog-table { width: 100%; }
.catalog-primary-cell { min-width: 0; }
.catalog-primary-cell strong, .catalog-primary-cell small { display: block; overflow-wrap: anywhere; }
.catalog-primary-cell strong { color: var(--text); font-size: 13px; line-height: 1.4; }
.catalog-primary-cell small { margin-top: 3px; color: var(--text-3); font-size: 11px; }
.binding-role-cell { display: flex; min-width: 0; align-items: flex-start; flex-direction: column; gap: 5px; }
.binding-role-cell small { color: var(--text-3); font-size: 11px; line-height: 1.4; }
.head-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.legacy-binding-alert { margin-bottom: 12px; }
.physical-detail-note { margin-bottom: 14px; }
.binding-option-description { float: right; max-width: 360px; margin-left: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.binding-safe-note, .legacy-scope-note { margin-bottom: 14px; }
.physical-workspace {
  align-items: flex-start;
}
.physical-workspace > .el-col {
  min-width: 0;
}
.data-sources-list-col > .card,
.data-source-detail-col > .card,
.data-source-detail-col > .el-empty {
  width: 100%;
}
.ds-list {
  padding-right: 2px;
}
.data-source-detail-card > .card-title {
  flex-wrap: wrap;
}
.database-tables h3 { margin: 2px 0 12px; color: var(--text); font-size: 14px; }
.bucket-detail {
  min-width: 0;
}
.bucket-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.data-source-header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.data-source-header-actions :deep(.el-select) { width: min(240px, 38vw); }
.retrieval-panel {
  flex: 0 0 auto;
  margin: 2px 0 14px;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--primary) 24%, var(--border));
  border-radius: 14px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--primary-soft) 72%, var(--surface)), var(--surface));
}
.retrieval-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.eyebrow { display: block; color: var(--primary-600); font-size: 10px; font-weight: 750; letter-spacing: .1em; }
.retrieval-head h3 { margin: 3px 0; color: var(--text); font-size: 16px; }
.retrieval-head p { max-width: 620px; margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.55; }
.retrieval-query { display: flex; gap: 8px; margin-top: 12px; }
.retrieval-query :deep(.el-input) { flex: 1; min-width: 0; }
.retrieval-query :deep(.el-button) { min-width: 76px; }
.retrieval-summary { margin: 10px 0 0; color: var(--text-2); font-size: 13px; }
.retrieval-notice { margin: 10px 0 0; color: var(--primary-600); font-size: 13px; line-height: 1.5; }
.retrieval-error { margin: 10px 0 0; color: var(--danger); font-size: 13px; line-height: 1.5; }
.citation-list { display: grid; gap: 8px; margin-top: 12px; }
.citation-card { padding: 10px 11px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
.citation-meta { display: flex; align-items: flex-start; gap: 9px; }
.citation-meta > div { min-width: 0; flex: 1; }
.citation-meta strong { display: block; color: var(--text); font-size: 13px; overflow-wrap: anywhere; }
.citation-meta small { display: block; margin-top: 2px; color: var(--text-3); font-size: 11px; overflow-wrap: anywhere; }
.citation-id { flex: 0 0 auto; padding: 3px 6px; border-radius: 6px; background: var(--primary-soft); color: var(--primary-600); font: 700 11px/1.2 var(--font-mono, monospace); }
.citation-card p { margin: 8px 0 0; color: var(--text-2); font-size: 13px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.index-count { display: block; margin-top: 3px; color: var(--text-3); font-size: 10px; }
.index-error { display: block; max-width: 120px; margin-top: 3px; color: var(--danger); font-size: 10px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.readonly-note { margin-bottom: 12px; }
.bucket-empty { padding: 18px 0 24px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.text-file-preview { max-height: none; overflow-x: auto; overflow-y: visible; }

@media (max-width: 767px) {
  .data-sources-page {
    padding: 14px;
  }
  .catalog-grid {
    grid-template-columns: 1fr;
  }
  .resource-boundary-grid { grid-template-columns: 1fr; }
  .resource-group-heading { align-items: flex-start; flex-direction: column; gap: 7px; }
  .resource-group-heading p { text-align: left; }
  .catalog-section-head {
    align-items: stretch;
    flex-direction: column;
  }
  .catalog-section-head > :deep(.el-tag) { align-self: flex-start; }
  .physical-workspace {
    flex-direction: column;
  }
  .physical-workspace > .data-sources-list-col {
    width: 100%;
    max-width: none;
  }
  .physical-workspace > .data-source-detail-col {
    width: 100%;
    max-width: none;
    margin-top: 12px;
  }
  .data-sources-list-card { padding: 14px; }
  .data-source-detail-card { padding: 14px; }
  .data-source-detail-card > .card-title > div:last-child {
    width: 100%;
    margin-left: 0 !important;
    justify-content: flex-end;
  }
  .retrieval-head, .retrieval-query { align-items: stretch; flex-direction: column; }
  .retrieval-head > :deep(.el-tag) { align-self: flex-start; }
  .retrieval-query :deep(.el-button) { min-height: 44px; }
  .data-source-header-actions { width: 100%; align-items: stretch; }
  .data-source-header-actions :deep(.el-select) { min-width: 0; flex: 1; width: auto; }
  .data-source-form > .el-row { margin-right: 0 !important; margin-left: 0 !important; }
  .data-source-form > .el-row > .el-col { max-width: 100%; flex: 0 0 100%; padding-right: 0 !important; padding-left: 0 !important; }
  .data-source-form :deep(.el-form-item) { display: block; }
  .data-source-form :deep(.el-form-item__label) { width: auto !important; height: auto; justify-content: flex-start; margin-bottom: 6px; padding: 0; line-height: 1.45; }
  .data-source-form :deep(.el-form-item__content) { margin-left: 0 !important; }
  .binding-form > .el-row { margin-right: 0 !important; margin-left: 0 !important; }
  .binding-form > .el-row > .el-col { max-width: 100%; flex: 0 0 100%; padding-right: 0 !important; padding-left: 0 !important; }
  .binding-form :deep(.el-form-item) { display: block; }
  .binding-form :deep(.el-form-item__label) { width: auto !important; height: auto; justify-content: flex-start; margin-bottom: 6px; padding: 0; line-height: 1.45; }
  .binding-form :deep(.el-form-item__content) { margin-left: 0 !important; }
  :deep(.binding-dialog) { width: calc(100% - 24px) !important; }
}
</style>
