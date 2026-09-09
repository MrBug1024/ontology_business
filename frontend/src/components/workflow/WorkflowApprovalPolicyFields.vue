<template>
  <div class="approval-policy">
    <label :for="`${id}-people`">指定审批人员</label>
    <el-select :id="`${id}-people`" :model-value="people" multiple filterable remote reserve-keyword :remote-method="search" :loading="loading" placeholder="具有审批权限的人员" @update:model-value="setPeople">
      <el-option v-for="member in members" :key="member.user_id" :value="member.user_id" :label="member.display_name || member.email" :disabled="member.status !== 'active' || member.account_status !== 'active'" />
      <el-option v-for="missing in missingPeople" :key="missing" :value="missing" label="已失效的审批人员" disabled />
    </el-select>
    <p v-if="error" role="alert">{{ error }}</p>
    <label :for="`${id}-roles`">审批角色限制</label>
    <el-select :id="`${id}-roles`" :model-value="roles" multiple placeholder="不额外限制角色" @update:model-value="setRoles">
      <el-option v-for="option in roleOptions" :key="option.value" :label="option.label" :value="option.value" />
    </el-select>
    <label class="evidence-setting"><span>同意时须附佐证文件</span><el-switch :model-value="modelValue.requires_evidence === true" aria-label="同意时须附佐证文件" @update:model-value="setEvidence" /></label>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { workspaceAccess } from '@/api/workspaceAccess'
import type { Member } from '@/types/access'

const props = defineProps<{ modelValue: Record<string, unknown> }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: Record<string, unknown>): void }>()
const id = useId()
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
const people = computed(() => strings(props.modelValue.approver_user_ids))
const roles = computed(() => strings(props.modelValue.approver_roles))
const members = ref<Member[]>([])
const missingPeople = computed(() => people.value.filter(person => !members.value.some(member => member.user_id === person)))
const roleOptions = [{ value: 'owner', label: '所有者' }, { value: 'admin', label: '管理员' }, { value: 'operator', label: '操作员' }, { value: 'viewer', label: '查看者' }]
const loading = ref(false)
const error = ref('')
let controller: AbortController | undefined
let timer: ReturnType<typeof setTimeout> | undefined

function setPeople(value: unknown) { emit('update:modelValue', { ...props.modelValue, approver_user_ids: strings(value) }) }
function setRoles(value: unknown) { emit('update:modelValue', { ...props.modelValue, approver_roles: strings(value) }) }
function setEvidence(value: unknown) { emit('update:modelValue', { ...props.modelValue, requires_evidence: value === true }) }

async function load(term = '') {
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const [options, selected] = await Promise.all([
      workspaceAccess.memberOptions(term, [], request.signal),
      people.value.length ? workspaceAccess.memberOptions('', people.value, request.signal) : Promise.resolve({ items: [] }),
    ])
    if (request.signal.aborted) return
    members.value = [...new Map([...selected.items, ...options.items].map(member => [member.user_id, member])).values()]
  } catch (failure: unknown) {
    if (!request.signal.aborted) error.value = failure instanceof Error ? failure.message : '审批人员加载失败'
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}
function search(term: string) {
  controller?.abort()
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { void load(term) }, 250)
}
watch(() => people.value.join(','), () => { void load() }, { immediate: true })
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer) })
</script>

<style scoped>
.approval-policy { display: grid; gap: 8px; margin-bottom: 14px; min-width: 0; }
.approval-policy > label { font-size: 12px; }
.approval-policy .el-select { width: 100%; min-width: 0; }
.evidence-setting { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.approval-policy p { color: var(--el-color-danger); font-size: 12px; overflow-wrap: anywhere; margin: 0; }
</style>
