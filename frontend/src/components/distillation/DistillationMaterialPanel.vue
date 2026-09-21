<template>
  <section class="distillation-material" v-loading="loading" aria-label="业务蒸馏交接资料">
    <p v-if="error" role="alert">{{ error }} <el-button text @click="load">重试</el-button></p>
    <template v-if="publication">
      <header><h3>交接版本 {{ publication.project_revision }}</h3><el-tag size="small" type="info" effect="plain">不可变</el-tag></header>
      <el-alert v-if="!source.scenario_id" title="该版本尚未关联场景" :closable="false" type="warning" />
      <div class="material-artifacts"><article v-for="artifact in publication.artifacts" :key="artifact.key"><strong>{{ artifact.filename }}</strong><el-button :loading="busy === artifact.key" :disabled="!!busy" @click="download(artifact)">下载文件</el-button></article></div>
      <el-button @click="openDistillation">回到业务蒸馏</el-button>
      <el-button v-if="source.scenario_id" type="primary" @click="router.push({ name: 'scenario-detail', params: { id: source.scenario_id }, query: { stage: 'ontology' } })">进入能力建设</el-button>
      <el-button v-else @click="router.push('/scenarios')">前往场景能力</el-button>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { businessDistillationApi } from '@/api/businessDistillation'
import type { DataSource } from '@/types'
import type { DistillationArtifact, DistillationPublication } from '@/types/businessDistillation'
const props = defineProps<{ source: DataSource }>()
const router = useRouter()
const projectId = computed(() => typeof props.source.config.distillation_project_id === 'string' ? props.source.config.distillation_project_id : '')
const publicationId = computed(() => typeof props.source.config.publication_id === 'string' ? props.source.config.publication_id : '')
const publication = ref<DistillationPublication | null>(null)
const loading = ref(false), error = ref(''), busy = ref('')
let controller: AbortController | undefined
function openDistillation() {
  if (props.source.scenario_id) {
    void router.push({ name: 'scenario-detail', params: { id: props.source.scenario_id }, query: { stage: 'distillation', distillation_id: projectId.value } })
    return
  }
  void router.push({ name: 'business-distillation', params: { id: projectId.value } })
}
async function load() {
  controller?.abort()
  const request = new AbortController()
  controller = request
  publication.value = null
  error.value = ''
  loading.value = true
  try {
    if (!projectId.value || !publicationId.value) throw new Error('资料库中的阶段交接资料引用不完整，请返回业务蒸馏核对。')
    const result = await businessDistillationApi.publication(projectId.value, publicationId.value, request.signal)
    if (!request.signal.aborted) publication.value = result
  } catch (caught: unknown) {
    if (!request.signal.aborted) error.value = caught instanceof Error ? caught.message : '资料加载失败'
  } finally { if (!request.signal.aborted) loading.value = false }
}
async function download(artifact: DistillationArtifact) {
  if (!publication.value || !controller || busy.value) return
  const request = controller
  busy.value = artifact.key
  try {
    const blob = await businessDistillationApi.artifact(projectId.value, publicationId.value, artifact.key, request.signal)
    if (request.signal.aborted) return
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = artifact.filename
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (caught: unknown) {
    if (!request.signal.aborted) error.value = caught instanceof Error ? caught.message : '下载失败'
  } finally { if (!request.signal.aborted) busy.value = '' }
}
watch(() => props.source.id, () => { busy.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => controller?.abort())
</script>
<style scoped>
.distillation-material > header { display: flex; align-items: center; gap: 8px; }
.distillation-material h3 { margin: 0; }
.distillation-material p { color: var(--text-2); line-height: 1.6; }
.material-artifacts { display: grid; gap: 12px; margin: 20px 0; }
.material-artifacts article { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; flex-wrap: wrap; }
.material-artifacts strong { overflow-wrap: anywhere; }
</style>
