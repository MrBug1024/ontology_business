<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>数据源</h2>
        <div class="sub">接入数据库（MySQL / PostgreSQL / SQLite）或文件桶（Excel / Word / PDF / 图片…）</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建数据源</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="9">
        <div class="card" v-loading="loading">
          <div class="card-title"><el-icon><Coin /></el-icon> 数据源列表</div>
          <div v-for="ds in dataSources" :key="ds.id" class="ds-item" :class="{ active: selected?.id === ds.id }" @click="select(ds)">
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
      </el-col>

      <el-col :span="15">
        <div v-if="selected" class="card">
          <div class="card-title">
            <el-icon><Setting /></el-icon> {{ selected.name }}
            <el-tag size="small" type="info">{{ typeLabel(selected.type) }}</el-tag>
            <div style="margin-left:auto;display:flex;gap:6px">
              <el-button size="small" @click="testConn" :loading="testing"><el-icon><Link /></el-icon> 测试连接</el-button>
              <el-button size="small" @click="openEdit(selected)"><el-icon><Edit /></el-icon> 编辑</el-button>
              <el-button size="small" type="danger" @click="remove(selected)"><el-icon><Delete /></el-icon></el-button>
            </div>
          </div>

          <!-- 数据库：表 + SQL -->
          <template v-if="selected.type !== 'file_bucket'">
            <el-tabs v-model="dbTab">
              <el-tab-pane label="数据表" name="tables">
                <el-table :data="tables" size="small" @row-click="(r:any)=>openTable(r)" style="cursor:pointer">
                  <el-table-column prop="name" label="表名" min-width="160">
                    <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
                  </el-table-column>
                  <el-table-column prop="row_count" label="行数" width="90" align="right" />
                  <el-table-column label="字段" min-width="220">
                    <template #default="{ row }">
                      <el-tag v-for="c in row.columns.slice(0, 5)" :key="c.name" size="small" effect="plain" style="margin:2px">
                        {{ c.name }}<span v-if="c.pk" style="color:#f59e0b">🔑</span>
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
            <el-upload drag :auto-upload="false" :file-list="uploadList" :on-change="onFilePick" :on-remove="() => {}" multiple>
              <el-icon class="el-icon--upload" :size="40"><UploadFilled /></el-icon>
              <div class="el-upload__text">拖拽文件到此处，或 <em>点击选择</em></div>
              <template #tip>
                <div class="el-upload__tip">支持 Excel / Word / PPT / PDF / 图片 / TXT / MD / CSV / JSON，上传后自动解析为可检索文本</div>
              </template>
            </el-upload>
            <div style="display:flex;gap:8px;margin:12px 0">
              <el-button type="primary" :loading="uploading" :disabled="!uploadList.length" @click="doUpload">
                <el-icon><Upload /></el-icon> 上传并解析（{{ uploadList.length }}）
              </el-button>
              <el-button @click="loadFiles" :loading="loadingFiles"><el-icon><Refresh /></el-icon> 刷新</el-button>
            </div>
            <el-table :data="files" size="small">
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
              <el-table-column label="" width="170" align="center">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="viewText(row)">查看文本</el-button>
                  <el-button size="small" text @click="reparse(row)" :loading="row._loading">重解析</el-button>
                  <el-button size="small" text type="danger" @click="removeFile(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>
        <el-empty v-else description="选择左侧数据源查看详情" />
      </el-col>
    </el-row>

    <!-- 新建/编辑对话框 -->
    <el-dialog v-model="dlg" :title="form.id ? '编辑数据源' : '新建数据源'" width="560px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：销售数据库、业务文档桶" /></el-form-item>
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
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { api } from '@/api'
import type { DataSource, Scenario, TableInfo, BucketFile } from '@/types'

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
const textDlg = ref(false)
const textFile = ref('')
const textContent = ref('')

const TYPE_LABELS: Record<string, string> = {
  mysql: 'MySQL', postgres: 'PostgreSQL', sqlite: 'SQLite', file_bucket: '文件桶',
}
function typeLabel(t: string) { return TYPE_LABELS[t] || t }
function fmtSize(n: number) {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
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
    ElMessage.success('上传完成，正在解析…')
    uploadList.value = []
    setTimeout(loadFiles, 1500)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    uploading.value = false
  }
}
async function loadFiles() {
  if (!selected.value) return
  loadingFiles.value = true
  try {
    files.value = await api.listFiles(selected.value.id!)
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
    ElMessage.success('已重新解析')
    setTimeout(loadFiles, 1200)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    f._loading = false
  }
}
async function removeFile(f: BucketFile) {
  await ElMessageBox.confirm(`删除文件「${f.filename}」？`, '确认', { type: 'warning' })
  await api.deleteFile(f.id)
  ElMessage.success('已删除')
  loadFiles()
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
  await ElMessageBox.confirm(`删除数据源「${ds.name}」？`, '确认', { type: 'warning' })
  await api.deleteDataSource(ds.id!)
  selected.value = null
  ElMessage.success('已删除')
  load()
}

onMounted(load)
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
</style>
