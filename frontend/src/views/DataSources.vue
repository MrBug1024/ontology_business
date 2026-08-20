<template>
  <div class="page data-sources-page">
    <div class="page-header">
      <div>
        <h2>数据源</h2>
        <div class="sub">接入数据库（MySQL / PostgreSQL / SQLite）或文件桶（Excel / Word / PDF / 图片…）</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建数据源</el-button>
    </div>

    <el-row :gutter="16">
      <el-col class="data-sources-list-col" :xs="24" :md="9">
        <div class="card data-sources-list-card" v-loading="loading">
          <div class="card-title"><el-icon><Coin /></el-icon> 数据源列表</div>
          <div class="ds-list">
            <div v-for="ds in dataSources" :key="ds.id" class="ds-item" :class="{ active: selected?.id === ds.id }" role="button" tabindex="0" :aria-current="selected?.id === ds.id ? 'true' : undefined" :aria-label="`选择数据源：${ds.name}`" @click="select(ds)" @keydown.enter.prevent="select(ds)" @keydown.space.prevent="select(ds)">
              <div class="ds-icon" :class="ds.type">
                <el-icon :size="18"><component :is="ds.type === 'file_bucket' ? 'FolderOpened' : 'Coin'" /></el-icon>
              </div>
              <div class="ds-info">
                <div class="ds-name">{{ ds.name }}</div>
                <div class="muted">{{ typeLabel(ds.type) }} · {{ ds.config.host || ds.config.path || '本地' }}</div>
              </div>
              <el-tag v-if="ds.status === 'ok'" size="small" type="success">正常</el-tag>
              <el-tag v-else-if="ds.status === 'error'" size="small" type="danger">异常</el-tag>
              <el-tag v-else size="small" type="info">未测试</el-tag>
            </div>
            <div v-if="!loading && !dataSources.length" class="empty-wrap">
              <div class="empty-icon"><el-icon :size="26"><Coin /></el-icon></div>
              <div>暂无数据源</div>
              <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建数据源</el-button>
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
                <el-button size="small" type="danger" @click="remove(selected)" aria-label="删除数据源" title="删除数据源"><el-icon aria-hidden="true"><Delete /></el-icon></el-button>
              </template>
            </div>
          </div>

          <!-- 数据库：表 + SQL -->
          <template v-if="selected.type !== 'file_bucket'">
            <el-tabs v-model="dbTab">
              <el-tab-pane label="数据表" name="tables" style="height:calc(100vh - 360px)">
                <el-table :data="tables" size="small" height="calc(100% - 36px)" @row-click="(r:any)=>openTable(r)"  style="cursor:pointer">
                  <el-table-column prop="name" label="表名" min-width="160">
                    <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
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
              </el-tab-pane>
              <el-tab-pane label="SQL 查询" name="sql">
                <div style="display:flex;gap:8px;margin-bottom:10px">
                  <el-input v-model="sql" placeholder="SELECT * FROM orders LIMIT 10" class="mono" @keyup.enter="runSql" />
                  <el-button type="primary" @click="runSql" :loading="runningSql">执行</el-button>
                </div>
                <el-alert v-if="sqlError" :title="sqlError" type="error" :closable="false" style="margin-bottom:10px" />
                <el-table v-if="sqlResult" :data="sqlResult.rows" size="small" max-height="380" border>
                  <el-table-column v-for="c in sqlResult.columns" :key="c" :prop="c" :label="c" min-width="110">
                    <template #default="{ row }"><span class="mono">{{ row[c] }}</span></template>
                  </el-table-column>
                </el-table>
                <div class="muted" v-if="sqlResult" style="margin-top:8px">{{ sqlResult.row_count }} 行 · 只读查询（SELECT）</div>
              </el-tab-pane>
            </el-tabs>
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
              <el-table v-else :data="files" size="small" height="calc(100vh -  520px)">
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
                    <el-tag v-else size="small" type="warning">{{ row.status }}</el-tag>
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
        <el-empty v-else description="选择左侧数据源查看详情" />
      </el-col>
    </el-row>

    <!-- 新建/编辑对话框 -->
    <el-dialog v-model="dlg" :title="form.id ? '编辑数据源' : '新建数据源'" width="560px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：业务数据库、业务文档桶" /></el-form-item>
        <el-form-item label="所属场景">
          <el-select v-model="form.scenario_id" clearable placeholder="可选" style="width:100%">
            <el-option v-for="s in scenarios" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型" required>
          <el-radio-group v-model="form.type" @change="onTypeChange">
            <el-radio value="mysql">MySQL</el-radio>
            <el-radio value="postgres">PostgreSQL</el-radio>
            <el-radio value="sqlite">SQLite</el-radio>
            <el-radio value="file_bucket">文件桶</el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.type === 'mysql' || form.type === 'postgres'">
          <el-row :gutter="10">
            <el-col :span="14"><el-form-item label="主机"><el-input v-model="form.config.host" placeholder="127.0.0.1" /></el-form-item></el-col>
            <el-col :span="10"><el-form-item label="端口"><el-input v-model.number="form.config.port" :placeholder="form.type === 'mysql' ? '3306' : '5432'" /></el-form-item></el-col>
          </el-row>
          <el-row :gutter="10">
            <el-col :span="14"><el-form-item label="数据库"><el-input v-model="form.config.database" /></el-form-item></el-col>
            <el-col :span="10"><el-form-item label="用户名"><el-input v-model="form.config.username" /></el-form-item></el-col>
          </el-row>
          <el-form-item label="密码"><el-input v-model="form.config.password" type="password" show-password /></el-form-item>
        </template>
        <template v-else-if="form.type === 'sqlite'">
          <el-form-item label="文件路径"><el-input v-model="form.config.path" placeholder="data/demo.db（相对 backend 或绝对路径）" /></el-form-item>
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
      <pre class="code" style="max-height:60vh">{{ textContent }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { api } from '@/api'
import type { DataSource, Scenario, TableInfo, BucketFile, RagCitation } from '@/types'

const dataSources = ref<DataSource[]>([])
const scenarios = ref<Scenario[]>([])
const selected = ref<DataSource | null>(null)
const loading = ref(false)

const dlg = ref(false)
const saving = ref(false)
const form = ref<Partial<DataSource> & { config: Record<string, any> }>({ type: 'mysql', config: {} })

const dbTab = ref('tables')
const testing = ref(false)
const tables = ref<TableInfo[]>([])
const loadingTables = ref(false)
const sql = ref('')
const runningSql = ref(false)
const sqlError = ref('')
const sqlResult = ref<{ columns: string[]; rows: any[]; row_count: number } | null>(null)
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

const TYPE_LABELS: Record<string, string> = {
  mysql: 'MySQL', postgres: 'PostgreSQL', sqlite: 'SQLite', file_bucket: '文件桶',
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
function indexTagType(status?: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'indexed') return 'success'
  if (status === 'partial') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

async function load() {
  loading.value = true
  try {
    const [ds, sc] = await Promise.all([api.listDataSources(), api.listScenarios()])
    dataSources.value = ds
    scenarios.value = sc
    if (selected.value) {
      const fresh = ds.find((d) => d.id === selected.value!.id)
      if (fresh) select(fresh)
    }
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function select(ds: DataSource) {
  selected.value = ds
  sqlResult.value = null
  sqlError.value = ''
  searchResults.value = []
  searched.value = false
  searchError.value = ''
  searchNotice.value = ''
  if (ds.type !== 'file_bucket') loadTables()
  else loadFiles()
}
async function loadTables() {
  if (!selected.value) return
  loadingTables.value = true
  try {
    tables.value = await api.listTables(selected.value.id!)
  } catch (e: any) {
    ElMessage.error('获取表列表失败：' + e.message)
  } finally {
    loadingTables.value = false
  }
}
async function runSql() {
  if (!sql.value.trim()) return
  runningSql.value = true
  sqlError.value = ''
  sqlResult.value = null
  try {
    sqlResult.value = await api.query(selected.value!.id!, sql.value)
  } catch (e: any) {
    sqlError.value = e.message
  } finally {
    runningSql.value = false
  }
}
function openTable(t: TableInfo) {
  curTable.value = t
  tableDlg.value = true
}
async function testConn() {
  testing.value = true
  try {
    const r: any = await api.testDataSource(selected.value!.id!)
    ElMessage.success(r.message || '连接成功')
    selected.value!.status = 'ok'
  } catch (e: any) {
    ElMessage.error('连接失败：' + e.message)
    selected.value!.status = 'error'
  } finally {
    testing.value = false
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
  if (!selected.value) return
  const sourceId = selected.value.id
  loadingFiles.value = true
  try {
    files.value = await api.listFiles(selected.value.id!)
    if (indexPollTimer) window.clearTimeout(indexPollTimer)
    if (files.value.some((file) => ['pending', 'queued'].includes(file.index_status || 'pending'))) {
      indexPollTimer = window.setTimeout(() => {
        if (selected.value?.id === sourceId) void loadFiles()
      }, 1000)
    }
  } finally {
    loadingFiles.value = false
  }
}
async function viewText(f: BucketFile) {
  try {
    const r = await api.fileText(f.id)
    textFile.value = f.filename
    textContent.value = r.text || '（空）'
    textDlg.value = true
  } catch (e: any) {
    ElMessage.error(e.message)
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
  if (!selected.value || !retrievalQuery.value.trim()) return
  searching.value = true
  searched.value = false
  searchError.value = ''
  searchNotice.value = ''
  searchResults.value = []
  try {
    const result = await api.searchDocuments({
      query: retrievalQuery.value.trim(),
      data_source_ids: [selected.value.id!],
      scenario_id: selected.value.scenario_id,
      top_k: 5,
    })
    searchResults.value = result.results || []
    searchNotice.value = result.permission_message || (
      selected.value?.can_write ? '' : '当前为只读公开资料库，结果仅来自已建立的公开索引。'
    )
    if (!searchResults.value.length && files.value.some((file) => ['pending', 'queued'].includes(file.index_status || 'pending'))) {
      searchNotice.value = '资料仍在后台解析或建立索引，请稍候自动刷新后再次检索。'
    }
    searched.value = true
  } catch (e: any) {
    searchError.value = e.message || '检索失败，请稍后重试。'
    searched.value = true
  } finally {
    searching.value = false
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

// ── 新建/编辑 ──
function onTypeChange() {
  form.value.config =
    form.value.type === 'sqlite' ? { path: '' }
    : form.value.type === 'file_bucket' ? {}
    : form.value.type === 'mysql' ? { host: '127.0.0.1', port: 3306, database: '', username: 'root', password: '' }
    : { host: '127.0.0.1', port: 5432, database: '', username: 'postgres', password: '' }
}
function openCreate() {
  form.value = { name: '', type: 'mysql', config: { host: '127.0.0.1', port: 3306, database: '', username: 'root', password: '' } }
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
    if (form.value.id) await api.updateDataSource(form.value.id, form.value)
    else await api.createDataSource(form.value)
    ElMessage.success('已保存')
    dlg.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function remove(ds: DataSource) {
  try {
    await ElMessageBox.confirm(`删除数据源「${ds.name}」？`, '确认', { type: 'warning' })
    await api.deleteDataSource(ds.id!)
    selected.value = null
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}

onMounted(load)
onBeforeUnmount(() => {
  if (indexPollTimer) window.clearTimeout(indexPollTimer)
})
</script>

<style scoped>
.ds-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  margin-bottom: 6px;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.ds-item:hover { background: var(--surface-2); }
.ds-item.active {
  background: var(--primary-soft);
  border-color: var(--border-strong);
}
.ds-item:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 2px; }
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
.ds-name {
  font-weight: 600;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 数据源工作区：列表与详情各自滚动，避免撑开主页面 ── */
.data-sources-page {
  height: calc(100dvh - 68px);
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}
.data-sources-page > .page-header {
  flex: 0 0 auto;
}
.data-sources-page > .el-row {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  align-items: stretch;
}
.data-sources-page > .el-row > .el-col {
  min-height: 0;
  display: flex;
}
.data-sources-list-col > .card,
.data-source-detail-col > .card,
.data-source-detail-col > .el-empty {
  width: 100%;
  min-height: 0;
}
.data-sources-list-card,
.data-source-detail-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.data-sources-list-card .card-title,
.data-source-detail-card > .card-title {
  flex: 0 0 auto;
}
.ds-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding-right: 2px;
  scrollbar-gutter: stable;
}
.data-source-detail-card > .card-title {
  flex-wrap: wrap;
}
.data-source-detail-card > :deep(.el-tabs) {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.data-source-detail-card :deep(.el-tabs__header) {
  flex: 0 0 auto;
}
.data-source-detail-card :deep(.el-tabs__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  scrollbar-gutter: stable;
}
.data-source-detail-card :deep(.el-tab-pane) {
  min-height: 100%;
}
.bucket-detail {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  scrollbar-gutter: stable;
}
.bucket-detail :deep(.el-upload) {
  flex: 0 0 auto;
}
.bucket-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.bucket-detail > :deep(.el-table) {
  flex: 1 1 auto;
  min-height: 0;
}
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

@media (max-width: 767px) {
  .data-sources-page {
    height: calc(100dvh - 68px);
    padding: 14px;
  }
  .data-sources-page > .el-row {
    flex-direction: column;
    overflow: hidden;
  }
  .data-sources-page > .el-row > .data-sources-list-col {
    flex: 0 0 190px;
    width: 100%;
    max-width: none;
  }
  .data-sources-page > .el-row > .data-source-detail-col {
    flex: 1 1 auto;
    width: 100%;
    max-width: none;
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
}
</style>
