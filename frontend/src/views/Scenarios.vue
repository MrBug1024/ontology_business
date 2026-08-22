<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>业务场景</h1>
        <div class="sub">从业务描述开始，依次完成本体建模、数据接入与映射、业务能力和 Agent 配置</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon> 新建场景</el-button>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="s in scenarios" :key="s.id">
        <article class="card scenario-card">
          <div class="sc-head">
            <div class="sc-icon"><el-icon :size="22"><OfficeBuilding /></el-icon></div>
            <div class="sc-title">
              <div class="sc-name">{{ s.name }}</div>
              <el-tag size="small" type="info" effect="light">{{ s.industry || '通用' }}</el-tag>
              <el-tag size="small" effect="plain" class="namespace-tag">{{ s.namespace || 'default' }}</el-tag>
            </div>
            <el-icon class="sc-arrow" aria-hidden="true"><ArrowRight /></el-icon>
          </div>
          <div class="sc-desc">{{ s.description || '暂无描述' }}</div>
          <div class="sc-stats">
            <div><b>{{ s.entity_count || 0 }}</b><span>对象类型</span></div>
            <div class="sc-sep"></div>
            <div><b>{{ s.relation_count || 0 }}</b><span>关系类型</span></div>
            <div class="sc-sep"></div>
            <div><b>{{ s.data_source_count || 0 }}</b><span>数据源</span></div>
          </div>
          <div class="sc-actions">
            <el-button size="small" type="primary" plain @click="$router.push('/scenarios/' + s.id)"><el-icon><ArrowRight /></el-icon> 进入场景</el-button>
            <el-button size="small" text type="primary" @click="openEdit(s)"><el-icon><Edit /></el-icon> 编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(s)"><el-icon><Delete /></el-icon> 删除</el-button>
          </div>
        </article>
      </el-col>
    </el-row>
    <div v-if="!loading && !scenarios.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><OfficeBuilding /></el-icon></div>
      <div>暂无业务场景，点击右上角「新建场景」开始</div>
      <el-button type="primary" size="small" @click="openCreate"><el-icon><Plus /></el-icon> 新建场景</el-button>
    </div>

    <el-dialog v-model="dlg" class="scenario-dialog" :title="form.id ? '编辑场景' : '新建业务场景'" width="min(560px, 94vw)">
      <el-form :model="form" label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：运营分析、供应链、人力资源…" />
        </el-form-item>
        <el-form-item label="行业">
          <el-input v-model="form.industry" placeholder="可选，填写所属行业或业务领域" />
        </el-form-item>
        <el-form-item label="命名空间" required>
          <el-input v-model.trim="form.namespace" maxlength="180" placeholder="如 supply.procurement" />
          <div class="scenario-field-help">用于稳定区分不同业务域中的同名类型；建议使用英文、数字、点、下划线或短横线。</div>
        </el-form-item>
        <el-form-item label="业务描述">
          <el-input v-model="form.description" type="textarea" :rows="4" maxlength="4000" show-word-limit placeholder="说明业务目标、核心对象、关键规则和希望执行的动作；保存后 AI 会据此生成可检查的草稿" />
        </el-form-item>
        <el-form-item v-if="!form.id" label="参考文件">
          <div class="scenario-files">
            <label class="scenario-file-picker" :class="{ disabled: scenarioFiles.length >= 8 }">
              <el-icon aria-hidden="true"><Paperclip /></el-icon>
              <span>{{ scenarioFiles.length ? '继续添加附件' : '添加业务资料' }}</span>
              <input
                type="file"
                multiple
                :disabled="scenarioFiles.length >= 8"
                accept=".pdf,.docx,.xlsx,.xls,.pptx,.md,.txt,.csv,.json,.png,.jpg,.jpeg"
                @change="onScenarioFilesPicked"
              />
            </label>
            <div v-if="scenarioFiles.length" class="scenario-file-list" aria-label="待交给 AI 的临时附件">
              <div v-for="(file, index) in scenarioFiles" :key="`${file.name}-${file.size}-${file.lastModified}`" class="scenario-file-row">
                <el-icon aria-hidden="true"><Document /></el-icon>
                <span><b>{{ file.name }}</b><small>{{ formatFileSize(file.size) }}</small></span>
                <el-button text circle :aria-label="`移除附件 ${file.name}`" title="移除附件" @click="removeScenarioFile(index)">
                  <el-icon aria-hidden="true"><Close /></el-icon>
                </el-button>
              </div>
            </div>
            <p class="scenario-file-help">附件只会先进入 AI 临时上下文，不会写入正式数据源；你确认变更清单后，本体草稿才会保存。</p>
          </div>
        </el-form-item>
        <el-form-item v-if="!form.id" label="下一步">
          <div class="scenario-next-step">
            <el-checkbox v-model="startWithAI">创建后打开 AI 建模助手</el-checkbox>
            <span>推荐：在新场景中继续检查附件解析、AI 草稿和逐项差异。</span>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ form.id ? '保存修改' : '创建并开始建模' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { Scenario } from '@/types'

const scenarios = ref<Scenario[]>([])
const router = useRouter()
const dlg = ref(false)
const saving = ref(false)
const loading = ref(false)
const form = ref<Partial<Scenario>>({})
const scenarioFiles = ref<File[]>([])
const startWithAI = ref(true)

async function load() {
  loading.value = true
  try {
    scenarios.value = await api.listScenarios()
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
function openCreate() {
  form.value = { namespace: 'default' }
  scenarioFiles.value = []
  startWithAI.value = true
  dlg.value = true
}
function openEdit(s: Scenario) {
  form.value = { ...s }
  scenarioFiles.value = []
  dlg.value = true
}
function onScenarioFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const incoming = Array.from(input.files || [])
  const known = new Set(scenarioFiles.value.map((file) => `${file.name}:${file.size}:${file.lastModified}`))
  const unique = incoming.filter((file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`))
  const available = Math.max(8 - scenarioFiles.value.length, 0)
  scenarioFiles.value.push(...unique.slice(0, available))
  if (unique.length > available) ElMessage.warning('单次最多添加 8 个参考文件')
  input.value = ''
}
function removeScenarioFile(index: number) {
  scenarioFiles.value.splice(index, 1)
}
function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
async function save() {
  if (!form.value.name) return ElMessage.warning('请填写名称')
  if (!form.value.namespace?.trim()) return ElMessage.warning('请填写命名空间')
  saving.value = true
  try {
    if (form.value.id) {
      await api.updateScenario(form.value.id, form.value)
      ElMessage.success('场景信息已保存')
      dlg.value = false
      await load()
      return
    }
    const created = await api.createScenario(form.value)
    const files = [...scenarioFiles.value]
    const shouldOpenAssistant = startWithAI.value
    const description = (form.value.description || '').trim()
    dlg.value = false
    ElMessage.success('场景已创建，正在进入本体建模')
    await router.push({ name: 'scenario-detail', params: { id: created.id }, query: { stage: 'ontology' } })
    if (shouldOpenAssistant) {
      window.dispatchEvent(new CustomEvent('assistant-open-request', {
        detail: {
          mode: 'draft',
          files,
          prompt: `请只根据以下业务描述${files.length ? '和临时附件' : ''}生成本体模型变更清单。完整提取对象类型、属性、主键、关系类型、基数和约束，逐项标注来源并列出不确定项；不要生成数据映射或工作流。\n\n业务场景：${created.name}\n业务描述：${description || '请先根据场景名称提出需要补充的关键信息。'}`,
        },
      }))
    }
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
async function remove(s: Scenario) {
  try {
    await ElMessageBox.confirm(`删除场景「${s.name}」？其本体与数据源绑定将一并移除。`, '确认', { type: 'warning' })
    await api.deleteScenario(s.id)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.response?.data?.detail || e?.message || '删除失败')
  }
}
onMounted(load)
</script>

<style scoped>
.scenario-card {
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
  margin-bottom: 16px;
  position: relative;
  overflow: hidden;
}
.scenario-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--grad);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.scenario-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.scenario-card:hover::before { opacity: 1; }
.sc-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.sc-icon {
  width: 44px; height: 44px;
  border-radius: 12px;
  background: var(--primary-soft);
  color: var(--primary-600);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.sc-title { flex: 1; min-width: 0; }
.namespace-tag { margin-left: 4px; font-family: 'Cascadia Code', Consolas, monospace; }
.sc-name {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sc-arrow {
  color: var(--text-3);
  transition: transform var(--dur) var(--ease), color var(--dur);
}
.scenario-card:hover .sc-arrow {
  transform: translateX(3px);
  color: var(--primary);
}
.sc-desc {
  color: var(--text-2);
  font-size: 13px;
  height: 40px;
  overflow: hidden;
  margin-bottom: 14px;
  line-height: 1.5;
}
.sc-stats {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 12px 0;
  border-top: 1px solid var(--border);
}
.sc-stats div { display: flex; flex-direction: column; }
.sc-stats b { font-size: 18px; font-weight: 800; letter-spacing: -0.3px; }
.sc-stats span { color: var(--text-3); font-size: 12px; margin-top: 1px; }
.sc-sep {
  width: 1px;
  height: 22px;
  background: var(--border);
}
.sc-actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.scenario-files { width: 100%; }
.scenario-field-help { margin-top: 5px; color: var(--text-3); font-size: 11px; line-height: 1.5; }
.scenario-file-picker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 38px;
  padding: 0 12px;
  border: 1px dashed var(--border-strong);
  border-radius: 9px;
  color: var(--primary-600);
  background: var(--primary-soft);
  cursor: pointer;
  font-size: 12px;
  font-weight: 700;
}
.scenario-file-picker:hover, .scenario-file-picker:focus-within { border-color: var(--primary); }
.scenario-file-picker.disabled { cursor: not-allowed; opacity: .6; }
.scenario-file-picker input { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; }
.scenario-file-list { display: grid; gap: 6px; margin-top: 9px; }
.scenario-file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
}
.scenario-file-row > span { display: flex; flex: 1; min-width: 0; flex-direction: column; }
.scenario-file-row b { overflow: hidden; color: var(--text-2); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.scenario-file-row small { color: var(--text-3); font-size: 10px; }
.scenario-file-help { margin: 7px 0 0; color: var(--text-3); font-size: 11px; line-height: 1.55; }
.scenario-next-step { display: flex; flex-direction: column; gap: 2px; }
.scenario-next-step span { color: var(--text-3); font-size: 11px; line-height: 1.5; }
:global(.scenario-dialog) { display: flex; max-height: calc(100dvh - 32px); flex-direction: column; }
:global(.scenario-dialog .el-dialog__body) { min-height: 0; overflow-y: auto; }
:global(.scenario-dialog .el-dialog__header),
:global(.scenario-dialog .el-dialog__footer) { flex: 0 0 auto; }
</style>
