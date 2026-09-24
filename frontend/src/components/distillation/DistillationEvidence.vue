<template>
  <section aria-labelledby="evidence-title">
    <div class="distill-section-head"><h2 id="evidence-title">把证据与解释分开</h2><el-button @click="addEvidence">添加证据</el-button></div>
    <p class="distill-hint">历史快照能证明某一时点的状态，通常不能独自证明完整过程。记录案例范围、时间和缺失信息。</p>
    <el-empty v-if="!document.evidence.length" description="从一个真实案例或一次访谈开始，说明你知道什么、还不知道什么。" />
    <article v-for="(item, index) in document.evidence" :key="item.key" class="distill-item">
      <div class="distill-section-head"><h3>证据 {{ index + 1 }}</h3><el-button text type="danger" :aria-label="`移除证据 ${item.title || index + 1}`" @click="removeEvidence(document, item.key)">移除</el-button></div>
      <div class="distill-fields">
        <el-form-item label="证据名称"><el-input v-model="item.title" maxlength="200" placeholder="便于识别的案例、访谈或资料名称" /></el-form-item>
        <el-form-item label="来源方式"><el-select v-model="item.kind" @change="clearSource(item)"><el-option label="受管资料库" value="material" /><el-option label="人工观察 / 访谈" value="observation" /><el-option label="系统导出 / 页面记录" value="system_export" /></el-select></el-form-item>
        <el-form-item label="业务角色"><el-select v-model="item.role"><el-option label="业务输入" value="input" /><el-option label="知识 / 制度 / 规则" value="knowledge" /><el-option label="历史结果（逆向起点）" value="result" /><el-option label="过程日志 / 流转记录" value="process" /><el-option label="其他参考资料" value="reference" /></el-select></el-form-item>
        <el-form-item label="引用资料库"><el-select v-model="item.data_source_id" filterable clearable placeholder="选择已登记的资料" @change="sourceChanged(item)"><el-option v-for="source in materials" :key="source.id" :label="source.name" :value="source.id || ''" /></el-select></el-form-item>
        <el-form-item v-if="isFileSource(item)" label="引用具体文件"><el-select v-model="item.bucket_file_id" filterable clearable :loading="loadingSources.has(item.data_source_id || '')" placeholder="选择需要分析的文件" @visible-change="(open: boolean) => open && loadFiles(item.data_source_id)"><el-option v-for="file in filesBySource[item.data_source_id || ''] || []" :key="file.id" :label="file.filename" :value="file.id" /></el-select><span v-if="fileError" class="distill-error" role="alert">{{ fileError }}</span></el-form-item>
        <el-form-item label="观察到的内容"><el-input v-model="item.summary" type="textarea" :rows="3" maxlength="4000" placeholder="描述事实、记录时间、来源位置；不要放入账号、密码等凭据" /></el-form-item>
        <el-form-item label="覆盖范围"><el-input v-model="item.coverage" type="textarea" :rows="2" maxlength="4000" placeholder="覆盖哪些时间、角色、案例与异常路径？" /></el-form-item>
        <el-form-item label="限制与缺口"><el-input v-model="item.limitations" type="textarea" :rows="2" maxlength="4000" placeholder="是否缺少中间步骤、失败案例、实际结果或责任人确认？" /></el-form-item>
      </div>
    </article>

    <div class="distill-section-head"><h2>事实、推断与待验证问题</h2><el-button @click="document.assertions.push({ key: key('claim'), statement: '', status: 'hypothesis', evidence_refs: [] })">添加断言</el-button></div>
    <article v-for="(item, index) in document.assertions" :key="item.key" class="distill-item">
      <div class="distill-fields">
        <el-form-item :label="`断言 ${index + 1}`"><el-input v-model="item.statement" type="textarea" :rows="2" maxlength="4000" /></el-form-item>
        <el-form-item label="证据状态"><el-select v-model="item.status"><el-option v-for="(label, value) in ASSERTION_LABELS" :key="value" :label="label" :value="value" /></el-select></el-form-item>
        <el-form-item label="依据"><el-select v-model="item.evidence_refs" multiple placeholder="选择支持或冲突的证据"><el-option v-for="source in document.evidence" :key="source.key" :label="source.title || '未命名证据'" :value="source.key" /></el-select></el-form-item>
      </div>
      <el-button text type="danger" :aria-label="`移除断言 ${index + 1}`" @click="document.assertions.splice(index, 1)">移除断言</el-button>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onBeforeUnmount, reactive, ref } from 'vue'
import { businessDistillationApi } from '@/api/businessDistillation'
import type { BucketFile, DataSource } from '@/types'
import type { DistillationDocument, DistillationEvidence } from '@/types/businessDistillation'
import { ASSERTION_LABELS, removeEvidence } from '@/utils/businessDistillation'
import { createClientRequestId } from '@/utils/clientRequestId'
const document = defineModel<DistillationDocument>({ required: true })
const props = defineProps<{ materials: DataSource[] }>()
const filesBySource = reactive<Record<string, BucketFile[]>>({})
const loadingSources = reactive(new Set<string>())
const fileError = ref('')
const controller = new AbortController()
const key = (prefix: string) => `${prefix}_${createClientRequestId().replace(/-/g, '').slice(0, 16)}`
function addEvidence() {
  document.value.evidence.push({ key: key('evidence'), title: '', kind: 'observation', role: 'reference', data_source_id: null, bucket_file_id: null, summary: '', coverage: '', limitations: '' })
}
function clearSource(item: DistillationEvidence) {
  if (item.kind === 'observation') { item.data_source_id = null; item.bucket_file_id = null }
}
function isFileSource(item: DistillationEvidence) { return props.materials.some(source => source.id === item.data_source_id && source.type === 'file_bucket') }
function sourceChanged(item: DistillationEvidence) {
  item.data_source_id ||= null
  item.bucket_file_id = null
  if (isFileSource(item)) void loadFiles(item.data_source_id)
}
async function loadFiles(sourceId: string | null) {
  if (!sourceId || loadingSources.has(sourceId)) return
  loadingSources.add(sourceId)
  fileError.value = ''
  try {
    const files = await businessDistillationApi.files(sourceId, controller.signal)
    if (!controller.signal.aborted) filesBySource[sourceId] = files
  } catch (caught: unknown) {
    if (!controller.signal.aborted) fileError.value = caught instanceof Error ? caught.message : '文件列表加载失败，请重新打开选项重试。'
  } finally { loadingSources.delete(sourceId) }
}
onBeforeUnmount(() => controller.abort())
</script>
