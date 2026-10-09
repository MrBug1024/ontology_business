<template>
  <section class="release-list" aria-label="插件开发能力版本">
    <header class="release-toolbar">
      <h2>能力版本与开发任务</h2>
      <div>
        <el-button circle :disabled="loading" aria-label="刷新发布列表" title="刷新发布列表" @click="load"><el-icon><Refresh /></el-icon></el-button>
        <el-button v-if="canManage" type="primary" @click="openCreate"><el-icon><Plus /></el-icon>准备能力版本</el-button>
      </div>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-table v-loading="loading" :data="releases" empty-text="暂无固定能力版本，请先准备一个版本">
      <el-table-column prop="name" label="能力版本" min-width="200" />
      <el-table-column prop="scenario_name" label="业务场景" min-width="180" />
      <el-table-column label="状态" width="120">
        <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ statusText(row) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="启用" width="85">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" :loading="busyId === row.id" :disabled="!row.can_manage || row.status !== 'released' || Boolean(busyId)" :aria-label="`启用发布 ${row.name}`" @change="toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="175"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
      <el-table-column prop="created_by_name" label="创建人" min-width="100" />
      <el-table-column label="操作" width="244" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.can_manage" text :disabled="Boolean(busyId) || row.status === 'retired'" @click="$emit('select', row)">进入开发</el-button>
          <el-button text :disabled="!row.enabled || Boolean(busyId)" @click="router.push({ name: 'agents', query: { scenario_id: row.scenario_id, release_id: row.id } })">验证能力</el-button>
          <el-button v-if="row.can_manage && row.status !== 'retired'" text type="warning" :disabled="Boolean(busyId)" @click="confirmChange(row, 'retire')">退役</el-button>
          <el-button v-if="row.can_manage && row.status === 'retired'" text type="danger" :disabled="Boolean(busyId)" @click="confirmChange(row, 'delete')"><el-icon><Delete /></el-icon>删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <footer v-if="offset || hasMore" class="release-pagination">
      <el-button circle :disabled="loading || offset === 0" aria-label="上一页" title="上一页" @click="page(-50)"><el-icon><ArrowLeft /></el-icon></el-button>
      <span>{{ offset / 50 + 1 }}</span>
      <el-button circle :disabled="loading || !hasMore" aria-label="下一页" title="下一页" @click="page(50)"><el-icon><ArrowRight /></el-icon></el-button>
    </footer>
    <el-dialog v-model="creatingVisible" title="准备固定能力版本" width="min(560px, 94vw)" :close-on-click-modal="!creating" :close-on-press-escape="!creating" :show-close="!creating">
      <p>保存当前场景能力的不可变版本，用于验证和插件封装。创建后需人工启用；插件的交付在开发、审阅完成后进行。</p>
      <el-alert v-if="createError" :title="createError" type="error" :closable="false" show-icon />
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item label="业务场景" required>
          <el-select v-model="form.scenario_id" filterable aria-label="发布业务场景" @change="suggestName">
            <el-option v-for="scenario in scenarios.filter(item => item.status !== 'retired')" :key="scenario.id" :value="scenario.id" :label="scenario.name" />
          </el-select>
        </el-form-item>
        <dl v-if="selectedScenario" class="release-scope">
          <div><dt>业务对象</dt><dd>{{ selectedScenario.entity_count ?? 0 }}</dd></div>
          <div><dt>关系</dt><dd>{{ selectedScenario.relation_count ?? 0 }}</dd></div>
          <div><dt>业务操作</dt><dd>{{ selectedScenario.action_count ?? 0 }}</dd></div>
          <div><dt>工作流</dt><dd>{{ selectedScenario.workflow_count ?? 0 }}</dd></div>
        </dl>
        <el-form-item label="版本名称" required><el-input v-model="form.name" maxlength="160" aria-label="能力版本名称" /></el-form-item>
        <el-form-item label="版本说明"><el-input v-model="form.notes" type="textarea" :rows="3" maxlength="8000" aria-label="能力版本说明" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="creating" @click="creatingVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="create">确认固定版本</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, toRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight, Delete, Plus, Refresh } from '@element-plus/icons-vue'
import { scenarioReleasesApi } from '@/api/scenarioReleases'
import { useScenarioReleases } from '@/composables/useScenarioReleases'
import type { Scenario } from '@/types'
import type { ScenarioRelease } from '@/types/scenarioRelease'

const props = defineProps<{ scenarioId: string; scenarios: Scenario[]; canManage: boolean }>()
const emit = defineEmits<{ select: [release: ScenarioRelease]; changed: [release: ScenarioRelease] }>()
const { releases, loading, error, offset, hasMore, busyId, load, change } = useScenarioReleases(toRef(props, 'scenarioId'))
const creatingVisible = ref(false)
const router = useRouter()
const creating = ref(false)
const createError = ref('')
const form = reactive({ scenario_id: '', name: '', notes: '' })
const selectedScenario = computed(() => props.scenarios.find(item => item.id === form.scenario_id))
let disposed = false
onBeforeUnmount(() => { disposed = true })

function statusText(release: ScenarioRelease) {
  if (release.status !== 'released') return '已退役'
  return release.enabled ? '已启用' : '已停用'
}
function formatDate(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function suggestName() { form.name = `${props.scenarios.find(item => item.id === form.scenario_id)?.name || ''}能力版本` }
function openCreate() {
  form.scenario_id = props.scenarioId
  form.name = ''
  form.notes = ''
  if (form.scenario_id) suggestName()
  createError.value = ''
  creatingVisible.value = true
}
async function create() {
  if (creating.value) return
  if (!form.scenario_id || !form.name.trim()) { createError.value = '请选择业务场景并填写版本名称'; return }
  creating.value = true
  createError.value = ''
  try {
    const created = await scenarioReleasesApi.create({ ...form, name: form.name.trim(), confirmed: true })
    if (disposed) return
    creatingVisible.value = false
    ElMessage.success('能力版本已固定，当前为停用状态')
    await load()
    emit('changed', created)
  } catch (caught: unknown) {
    if (!disposed) createError.value = caught instanceof Error ? caught.message : '发布创建失败'
  } finally { if (!disposed) creating.value = false }
}
async function toggle(release: ScenarioRelease) {
  const updated = await change(release, release.enabled ? 'disable' : 'enable')
  if (updated) emit('changed', updated)
}
async function confirmChange(release: ScenarioRelease, action: 'retire' | 'delete') {
  try {
    await ElMessageBox.confirm(`${action === 'retire' ? '退役' : '删除'}“${release.name}”？`, action === 'retire' ? '退役发布' : '删除发布', { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消' })
    if (disposed) return
    const updated = await change(release, action)
    if (updated) emit('changed', updated)
  } catch (caught: unknown) {
    if (caught !== 'cancel' && caught !== 'close') error.value = caught instanceof Error ? caught.message : '发布操作失败'
  }
}
async function page(delta: number) { offset.value += delta; await load() }
</script>

<style scoped>
.release-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 6px 0 18px; }
.release-toolbar h2 { margin: 0; font-size: 18px; }
.release-toolbar > div { display: flex; gap: 8px; }
.release-list :deep(.el-alert) { margin-bottom: 16px; }
.release-list :deep(.el-select) { width: 100%; }
.release-pagination { display: flex; justify-content: flex-end; align-items: center; gap: 12px; padding-top: 16px; }
.release-scope { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0 0 18px; }
.release-scope dt { font-size: 12px; color: var(--el-text-color-secondary); }
.release-scope dd { margin: 4px 0 0; font-weight: 600; }
</style>
