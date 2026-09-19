<template>
  <div class="discovery-library-picker">
    <p class="discovery-muted">选择资料库中已有的资料供当前对话查证。只建立引用，不复制或上传文件。</p>
    <el-input v-model="query" placeholder="查找资料名称" aria-label="查找资料库" clearable />
    <div v-if="visible.length" class="discovery-library-options"><label v-for="source in visible" :key="source.id"><input type="checkbox" :checked="selected.has(source.id || '')" :disabled="disabled" @change="toggle(source)" /><span><strong>{{ source.name }}</strong><small>{{ source.scenario_id ? '当前场景' : '共享资料' }} · {{ source.type === 'file_bucket' ? '文件库' : source.type === 'distillation' ? '阶段交接资料' : '数据库' }}</small></span></label></div>
    <el-empty v-else description="没有匹配的可用资料" />
    <p v-if="document.evidence.some(item => item.kind === 'material')" class="discovery-muted">已引用 {{ document.evidence.filter(item => item.kind === 'material').length }} 项资料。保存后，顾问可在调查中读取。</p>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DataSource } from '@/types'
import type { DistillationDocument } from '@/types/businessDistillation'
import { removeEvidence } from '@/utils/businessDistillation'
const document = defineModel<DistillationDocument>({ required: true })
const props = defineProps<{ materials: DataSource[]; disabled: boolean }>()
const query = ref('')
const selected = computed(() => new Set(document.value.evidence.filter(item => item.kind === 'material' && item.data_source_id).map(item => item.data_source_id)))
const visible = computed(() => props.materials.filter(item => item.name.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
function toggle(source: DataSource) {
  if (!source.id) return
  if (selected.value.has(source.id)) { for (const item of [...document.value.evidence]) if (item.kind === 'material' && item.data_source_id === source.id) removeEvidence(document.value, item.key); return }
  document.value.evidence.push({ key: `library_${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`, title: source.name, kind: 'material', role: 'reference', data_source_id: source.id, bucket_file_id: null, summary: '', coverage: '', limitations: '' })
}
</script>
