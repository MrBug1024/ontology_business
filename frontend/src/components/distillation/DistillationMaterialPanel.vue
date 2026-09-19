<template>
  <section class="distillation-material" v-loading="loading" aria-label="业务蒸馏交接资料">
    <el-alert title="这是已保存的业务蒸馏阶段资料" description="流程、对象和血缘用于指导场景建模。假设、冲突与暂缓决定仍需保留；这些资料不会成为正式运行数据。" :closable="false" type="info" />
    <p v-if="error" role="alert">{{ error }} <el-button text @click="load">重试</el-button></p>
    <template v-if="publication">
      <h3>交接版本 {{ publication.project_revision }}</h3>
      <p>场景建模顾问可将该阶段资料作为资料库依据读取，并按文件用途理解流程、ER 关系、数据血缘及业务边界。</p>
      <p v-if="!source.scenario_id">此版本尚未关联场景。请返回业务蒸馏，使用“复制到目标场景”并发布场景版本，供该场景顾问自动读取。</p>
      <div class="material-artifacts"><article v-for="artifact in publication.artifacts" :key="artifact.key"><strong>{{ artifact.filename }}</strong><el-button :loading="busy === artifact.key" :disabled="!!busy" @click="download(artifact)">下载文件</el-button></article></div>
      <el-button @click="router.push(`/business-distillation/${projectId}`)">回到业务蒸馏</el-button>
      <el-button v-if="source.scenario_id" type="primary" @click="router.push(`/scenarios/${source.scenario_id}`)">进入场景能力建设</el-button>
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
.distillation-material p { color: var(--text-2); line-height: 1.75; }
.material-artifacts { display: grid; gap: 12px; margin: 20px 0; }
.material-artifacts article { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; flex-wrap: wrap; }
.material-artifacts strong { overflow-wrap: anywhere; }
</style>
