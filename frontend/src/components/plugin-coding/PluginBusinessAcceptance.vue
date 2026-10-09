<template>
  <section class="business-acceptance" aria-label="插件业务验收">
    <h3>业务验收</h3><p>开发可以先进行。定版时，核对此插件全部能力的成功、边界和失败处理案例。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" /><el-button v-if="error" @click="load">重新读取验收证据</el-button>
    <p v-if="loading" role="status">读取当前能力版本和完成回执…</p>
    <article v-for="capability in workspace.capabilities" :key="identity(capability)"><b>{{ capability.name }}</b><el-form label-position="top"><el-form-item v-for="role in roles" :key="role.key" :label="`${role.label}案例`"><el-select v-model="choices[`${identity(capability)}:${role.key}`]" :aria-label="`${capability.name} ${role.label}案例`" :disabled="loading || disabled" placeholder="选择已核对业务结果的执行"><el-option v-for="item in available(capability)" :key="item.invocation_id" :value="item.invocation_id" :label="receiptLabel(item)" :disabled="!item.eligible || (role.key === 'success' && (item.status !== 'succeeded' || (item.kind === 'workflow' && item.workflow_status !== 'succeeded')))" /></el-select></el-form-item></el-form></article>
    <RouterLink v-if="release" :to="{ name: 'agents', query: { scenario_id: release.scenario_id, release_id: release.id } }">到验证中心补充此版本的案例</RouterLink>
    <el-checkbox v-model="confirmed" :disabled="disabled || loading">我已核对三类案例的业务输出，并确认符合预期</el-checkbox><p v-if="confirmed && !value" role="status">尚未满足验收条件：每项能力需要三条不同且已完成的执行回执。</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { scenarioPackagesApi } from '@/api/scenarioPackages'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import { executionStatusLabel } from '@/utils/agentExecutionTrace'
import { packageCases } from '@/utils/scenarioPackage'
import type { CodingWorkspace } from '@/types/pluginCoding'
import type { ScenarioRelease } from '@/types/scenarioRelease'
import type { AcceptanceRole, PackageCapability, PackageEvidence, ScenarioPackageBuild } from '@/types/scenarioPackage'
const props = defineProps<{ workspace: CodingWorkspace; disabled: boolean }>()
const emit = defineEmits<{ change: [value: ScenarioPackageBuild | null] }>()
const evidence = ref<PackageEvidence[]>([])
const release = ref<ScenarioRelease | null>(null)
const choices = reactive<Record<string, string>>({})
const confirmed = ref(false)
const loading = ref(false)
const error = ref('')
let generation = 0
let controller: AbortController | undefined
const roles: { key: AcceptanceRole; label: string }[] = [{ key: 'success', label: '成功' }, { key: 'boundary', label: '边界' }, { key: 'failure', label: '失败处理' }]
const value = computed<ScenarioPackageBuild | null>(() => {
  if (!confirmed.value || !release.value || loading.value || error.value) return null
  const capabilities = props.workspace.capabilities.map(({ kind, key }) => ({ kind, key }))
  try { return { expected_revision: release.value.revision, target: props.workspace.host, capabilities, acceptance_cases: packageCases(capabilities, evidence.value, choices), confirmed_business_acceptance: true } } catch { return null }
})
function identity(item: PackageCapability) { return `${item.kind}:${item.key}` }
function available(item: PackageCapability) { return evidence.value.filter(row => row.kind === item.kind && row.key === item.key) }
function receiptLabel(item: PackageEvidence) {
  const date = new Date(item.created_at)
  return `${date.toLocaleString('zh-CN', { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')} · ${executionStatusLabel(item.workflow_status || item.status)}`
}
async function load() {
  controller?.abort()
  const current = ++generation
  controller = new AbortController()
  loading.value = true
  error.value = ''
  confirmed.value = false
  try {
    const results = await Promise.allSettled([scenarioReleasesApi.get(props.workspace.release_id, controller.signal), scenarioPackagesApi.evidence(props.workspace.release_id, controller.signal)])
    if (current !== generation) return
    if (results[0].status === 'fulfilled') release.value = results[0].value
    if (results[1].status === 'fulfilled') evidence.value = results[1].value
    for (const result of results) if (result.status === 'rejected') throw result.reason
  } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '验收证据加载失败' }
  finally { if (current === generation) loading.value = false }
}
watch(value, result => emit('change', result), { immediate: true })
watch(() => props.workspace.release_id, load, { immediate: true })
onBeforeUnmount(() => { generation++; controller?.abort() })
</script>
<style scoped>
.business-acceptance { border-top: 1px solid var(--border); margin: 18px 0; padding-top: 12px; }
h3 { font-size: 15px; }
p { color: var(--text-2); font-size: 12px; line-height: 1.8; }
article { padding: 12px 0; }
article > b { display: block; margin-bottom: 12px; font-size: 13px; }
.el-select { width: 100%; }
a { display: block; margin: 12px 0 18px; color: var(--primary); font-size: 12px; }
:deep(.el-checkbox) { height: auto; align-items: flex-start; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
</style>
