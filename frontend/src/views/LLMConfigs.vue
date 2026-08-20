<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>LLM 配置</h2>
        <div class="sub">管理大模型接入（OpenAI 兼容协议：OpenAI / DeepSeek / 通义 / Ollama / vLLM…）</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建配置</el-button>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="l in llms" :key="l.id">
        <div class="card llm-card">
          <div class="ll-head">
            <div class="ll-icon"><el-icon :size="20"><ChatDotRound /></el-icon></div>
            <div class="ll-title">
              <div class="ll-name">{{ l.name }}</div>
              <div class="muted mono">{{ l.model }}</div>
            </div>
            <el-tag v-if="l.is_default" size="small" type="success" effect="light" style="margin-left:auto"><el-icon><Star /></el-icon> 默认</el-tag>
          </div>
          <div class="ll-meta">
            <div><span class="muted">Provider</span><div>{{ l.provider }}</div></div>
            <div><span class="muted">Base URL</span><div class="mono" style="font-size:12px">{{ l.base_url }}</div></div>
            <div><span class="muted">温度 / 最大Token</span><div>{{ l.temperature }} / {{ l.max_tokens }}</div></div>
          </div>
          <div class="ll-actions">
            <el-button size="small" plain @click="test(l)" :loading="l._testing"><el-icon><Link /></el-icon> 测试</el-button>
            <el-button v-if="!l.is_default" size="small" text type="primary" @click="setDefault(l)">设为默认</el-button>
            <el-button size="small" text type="primary" @click="openEdit(l)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(l)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </div>
      </el-col>
    </el-row>
    <div v-if="!loading && !llms.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><ChatDotRound /></el-icon></div>
      <div>暂无 LLM 配置，点击右上角「新建配置」接入大模型</div>
      <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建配置</el-button>
    </div>

    <el-dialog v-model="dlg" :title="form.id ? '编辑 LLM 配置' : '新建 LLM 配置'" width="560px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：DeepSeek 主模型" /></el-form-item>
        <el-form-item label="Provider">
          <el-select v-model="form.provider" style="width:100%">
            <el-option v-for="p in PROVIDERS" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="Base URL" required><el-input v-model="form.base_url" class="mono" placeholder="https://api.deepseek.com/v1" /></el-form-item>
        <el-form-item label="API Key" required><el-input v-model="form.api_key" type="password" show-password class="mono" placeholder="新建时填写；编辑时留空保持原密钥" /></el-form-item>
        <el-form-item label="模型" required><el-input v-model="form.model" class="mono" placeholder="deepseek-chat" /></el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="温度">
              <el-slider v-model="form.temperature" :min="0" :max="2" :step="0.1" show-input />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="最大 Token">
              <el-input-number v-model="form.max_tokens" :min="256" :max="32768" :step="256" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="设为默认"><el-switch v-model="form.is_default" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { LLMConfig } from '@/types'

const PROVIDERS = ['openai', 'deepseek', 'dashscope', 'ollama', 'vllm', 'openai_compatible']
const llms = ref<(LLMConfig & { _testing?: boolean })[]>([])
const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<LLMConfig>>({
  provider: 'openai_compatible', base_url: '', api_key: '', model: '',
  temperature: 0.7, max_tokens: 4096, is_default: false,
})

async function load() {
  loading.value = true
  try {
    llms.value = await api.listLLM()
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function openCreate() {
  form.value = { provider: 'openai_compatible', base_url: '', api_key: '', model: '', temperature: 0.7, max_tokens: 4096, is_default: false }
  dlg.value = true
}
function openEdit(l: LLMConfig) {
  form.value = { ...l }
  dlg.value = true
}
async function save() {
  if (!form.value.name || !form.value.base_url || !form.value.model) return ElMessage.warning('请填写名称、Base URL、模型')
  saving.value = true
  try {
    if (form.value.id) await api.updateLLM(form.value.id, form.value)
    else await api.createLLM(form.value)
    ElMessage.success('已保存')
    dlg.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function test(l: LLMConfig & { _testing?: boolean }) {
  l._testing = true
  try {
    const r: any = await api.testLLM(l.id!)
    ElMessage.success(r.message || '连接成功')
  } catch (e: any) {
    ElMessage.error('连接失败：' + e.message)
  } finally {
    l._testing = false
  }
}
async function setDefault(l: LLMConfig) {
  await api.updateLLM(l.id!, { ...l, is_default: true })
  ElMessage.success('已设为默认')
  load()
}
async function remove(l: LLMConfig) {
  try {
    await ElMessageBox.confirm(`删除 LLM 配置「${l.name}」？`, '确认', { type: 'warning' })
    await api.deleteLLM(l.id!)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}
onMounted(load)
</script>

<style scoped>
.llm-card {
  margin-bottom: 16px;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.llm-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.ll-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.ll-icon {
  width: 40px; height: 40px;
  border-radius: 11px;
  background: var(--success-soft);
  color: var(--success);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.ll-title { flex: 1; min-width: 0; }
.ll-name {
  font-weight: 700; font-size: 15px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ll-meta {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
  padding: 10px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  margin-bottom: 10px;
}
.ll-meta div { font-size: 13px; }
.ll-actions {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
</style>
