<template>
  <el-dialog :model-value="modelValue" :title="source ? '编辑资料库' : '创建资料库'" width="min(580px, 94vw)"
    :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @update:model-value="close">
    <el-form label-position="top" :disabled="saving" @submit.prevent="save">
      <el-form-item label="资料库名称" required><el-input v-model="form.name" maxlength="200" :disabled="saving || !!pendingSource" placeholder="为这组业务资料命名" /></el-form-item>
      <el-form-item label="资料类型" required>
        <el-radio-group v-model="form.type" :disabled="saving || !!source || !!pendingSource" @change="changeType">
          <el-radio v-for="type in LIBRARY_TYPES" :key="type.value" :value="type.value">{{ type.label }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="!lockScenario" label="业务场景">
        <el-select v-model="form.scenario_id" clearable filterable :disabled="saving || !!pendingSource || (source?.type === 'sqlite3' && !!source.file_count)" placeholder="工作区共享资料" aria-label="选择资料库业务场景">
          <el-option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id" :label="scenario.name" />
        </el-select>
      </el-form-item>
      <template v-if="remote">
        <div class="connection-grid">
          <el-form-item label="主机" required><el-input v-model="form.host" autocomplete="off" /></el-form-item>
          <el-form-item label="端口" required><el-input-number v-model="form.port" :min="1" :max="65535" :controls="false" /></el-form-item>
          <el-form-item label="数据库" required><el-input v-model="form.database" autocomplete="off" /></el-form-item>
          <el-form-item label="用户名" required><el-input v-model="form.user" autocomplete="off" /></el-form-item>
        </div>
        <el-form-item :label="source ? '密码（留空保留已配置密码）' : '密码'"><el-input v-model="form.password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <p class="help">请使用只有读取权限的数据库账户。连接凭据加密保存，不会发送给 AI。</p>
        <p v-if="form.type === 'mysql'" class="help">MySQL 使用受信 TLS 连接；管理员需将数据库主机加入部署允许名单。</p>
      </template>
      <template v-else-if="form.type === 'sqlite3'">
        <template v-if="!source?.file_count">
          <label for="library-sqlite-upload" class="file-label">SQLite3 数据库快照</label>
          <input id="library-sqlite-upload" type="file" accept=".sqlite3,.sqlite,.db" :disabled="saving" @change="chooseFile" />
          <p class="help">上传完整、已关闭写入的数据库文件，最大 32 MB。快照保存在受管存储中；新快照请创建新的资料库。</p>
        </template>
        <p v-else class="help">该库已保存不可变 SQLite3 快照，可在资料详情查看结构。</p>
      </template>
      <p v-else class="help">创建后可上传历史文档、表格、图片及结果文件，用于业务蒸馏与场景建模。</p>
      <p v-if="pendingSource" class="help" role="status">资料库已创建，文件尚未上传成功。保留当前窗口可重试上传。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="close(false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">{{ pendingSource ? '重试上传' : source ? '保存' : '创建资料库' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DataSource, Scenario } from '@/types'
import { libraryApi } from '@/api/library'
import { LIBRARY_TYPES, defaultLibraryPort, libraryForm, libraryPayload, validateSqliteFile } from '@/utils/library'

const props = withDefaults(defineProps<{ modelValue: boolean; source: DataSource | null; scenarios: Scenario[]; scenarioId?: string; lockScenario?: boolean }>(), {
  scenarioId: '', lockScenario: false,
})
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [source: DataSource] }>()
const form = ref(libraryForm())
const saving = ref(false)
const error = ref('')
const picked = ref<File | null>(null)
const pendingSource = ref<DataSource | null>(null)
const remote = computed(() => ['mysql', 'postgres'].includes(form.value.type))
watch(() => props.modelValue, (open) => {
  if (!open) return
  form.value = libraryForm(props.source, props.scenarioId)
  error.value = ''
  picked.value = null
  pendingSource.value = null
})
function changeType() {
  form.value.password = ''
  form.value.port = defaultLibraryPort(form.value.type)
  picked.value = null
  error.value = ''
}
function chooseFile(event: Event) {
  const input = event.target instanceof HTMLInputElement ? event.target : null
  picked.value = input?.files?.[0] || null
  error.value = picked.value ? validateSqliteFile(picked.value) : ''
}
function close(open: boolean) {
  if (saving.value) return
  if (!open) {
    form.value.password = ''
    if (pendingSource.value) emit('saved', pendingSource.value)
  }
  emit('update:modelValue', open)
}
async function save() {
  if (saving.value) return
  error.value = ''
  try {
    if (props.lockScenario) form.value.scenario_id = props.scenarioId
    const payload = libraryPayload(form.value)
    if (payload.type === 'sqlite3' && !props.source?.file_count) {
      if (!picked.value) throw new Error('请选择 SQLite3 数据库快照')
      const problem = validateSqliteFile(picked.value)
      if (problem) throw new Error(problem)
    }
    saving.value = true
    const saved = pendingSource.value || (props.source?.id
      ? await libraryApi.update(props.source.id, payload)
      : await libraryApi.create(payload))
    if (payload.type === 'sqlite3' && picked.value && saved.id) {
      pendingSource.value = saved
      await libraryApi.uploadSqlite(saved.id, picked.value)
      saved.file_count = 1
      saved.status = 'ok'
    }
    form.value.password = ''
    pendingSource.value = null
    emit('saved', saved)
    emit('update:modelValue', false)
  } catch (cause: unknown) {
    error.value = cause instanceof Error ? cause.message : '保存失败，输入已保留，请重试'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.connection-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
.connection-grid :deep(.el-input-number), :deep(.el-select) { width: 100%; }
.help { color: var(--text-3); font-size: 13px; line-height: 1.6; }
.error { color: var(--danger); line-height: 1.6; }
.file-label { display: block; margin-bottom: 8px; font-size: 14px; }
input[type=file] { max-width: 100%; min-height: 44px; }
@media(max-width:480px) { .connection-grid { grid-template-columns: 1fr; } }
</style>
