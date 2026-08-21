<template>
  <main class="permissions-page" aria-labelledby="permissions-page-title">
    <header class="permissions-header">
      <div>
        <div class="eyebrow">GOVERNANCE</div>
        <h2 id="permissions-page-title">权限与成员</h2>
        <p>管理组织成员、系统角色以及面向场景、对象、属性、Action 与工作流的精确授权。</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon aria-hidden="true"><Refresh /></el-icon> 刷新
      </el-button>
    </header>

    <el-alert
      v-if="loadError"
      class="page-alert"
      type="error"
      :title="loadError"
      show-icon
      :closable="false"
      role="alert"
    >
      <template #default><el-button size="small" type="primary" plain @click="load">重新加载</el-button></template>
    </el-alert>

    <section v-if="loading && !organization" class="loading-card card" aria-live="polite" aria-label="正在加载权限管理信息">
      <el-skeleton :rows="8" animated />
    </section>

    <template v-else-if="organization">
      <section class="organization-card card" aria-label="当前组织">
        <div class="organization-mark" aria-hidden="true"><el-icon><OfficeBuilding /></el-icon></div>
        <div class="organization-copy">
          <span>当前组织</span>
          <h3>{{ organization.name || '未命名组织' }}</h3>
          <p>组织是成员、角色与授权规则的作用范围；租户仍然是数据隔离边界。</p>
        </div>
        <dl class="organization-meta">
          <div><dt>系统角色</dt><dd>{{ roles.length }}</dd></div>
          <div><dt>有效成员</dt><dd>{{ activeMembers.length }}</dd></div>
          <div><dt>精确授权</dt><dd>{{ grants.length }}</dd></div>
        </dl>
      </section>

      <section class="role-section" aria-labelledby="role-section-title">
        <div class="section-heading">
          <div>
            <span class="section-kicker">RBAC</span>
            <h3 id="role-section-title">系统角色</h3>
          </div>
          <span>角色定义由平台维护；精确授权可在下方进一步收窄或放宽特定资源的权限。</span>
        </div>
        <div class="role-grid">
          <article v-for="role in roles" :key="role.id" class="role-card">
            <div class="role-card-head">
              <strong>{{ role.name || role.key }}</strong>
              <el-tag size="small" effect="plain" :type="roleTagType(role.key)">{{ roleLabel(role.key) }}</el-tag>
            </div>
            <p>{{ role.description || '平台系统角色' }}</p>
          </article>
        </div>
      </section>

      <el-alert
        v-if="!canManage"
        class="manage-alert"
        :type="managementUnavailable ? 'error' : 'warning'"
        :title="manageError || '当前账号没有组织管理权限'"
        :description="managementUnavailable ? '成员和授权规则暂时不可用。请刷新后重试；在服务端成功返回前，页面不会显示或开放管理操作。' : '你可以查看组织和角色定义；成员、授权规则以及可授权资源仅向拥有组织管理权限的账号开放。'"
        :closable="false"
        show-icon
        role="status"
      />

      <section v-else class="management-card card" v-loading="loading" aria-label="成员和细粒度授权管理">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="成员" name="members">
            <div class="section-toolbar">
              <div>
                <h3>组织成员</h3>
                <p>变更角色或移除成员会立即影响后续访问；移除不会删除用户的审计记录。</p>
              </div>
              <el-button type="primary" @click="openMemberDialog()"><el-icon aria-hidden="true"><Plus /></el-icon> 添加成员</el-button>
            </div>
            <el-table :data="members" class="permission-table" empty-text="当前组织没有成员记录">
              <el-table-column label="成员" min-width="210">
                <template #default="{ row }">
                  <div class="member-cell">
                    <strong>{{ row.display_name || row.email || shortId(row.user_id) }}</strong>
                    <span>{{ row.email || row.user_id }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="角色" min-width="130">
                <template #default="{ row }"><el-tag size="small" effect="plain" :type="roleTagType(row.role_key)">{{ row.role_name || roleLabel(row.role_key) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="状态" width="92">
                <template #default="{ row }"><el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '有效' : '已移除' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="加入时间" min-width="142">
                <template #default="{ row }">{{ formatDate(row.created_at) || '—' }}</template>
              </el-table-column>
              <el-table-column label="操作" width="180" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="openMemberDialog(row)">{{ row.status === 'active' ? '调整角色' : '重新加入' }}</el-button>
                  <el-button v-if="row.status === 'active'" size="small" text type="danger" :loading="removingMemberId === row.id" @click="removeMember(row)">移除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="精确授权" name="grants">
            <div class="section-toolbar grant-toolbar">
              <div>
                <h3>细粒度授权</h3>
                <p>一条规则只指向一个角色或成员。拒绝规则优先于角色默认权限和允许规则。</p>
              </div>
              <el-button type="primary" :disabled="!selectedScenarioId || resourcesLoading || Boolean(resourceError)" @click="openGrantDialog">
                <el-icon aria-hidden="true"><Plus /></el-icon> 新建授权
              </el-button>
            </div>

            <div class="resource-picker">
              <div class="resource-picker-copy">
                <span>授权资源所在场景</span>
                <small>选择后加载可授权的场景、对象、属性、Action 和工作流。</small>
              </div>
              <el-select v-model="selectedScenarioId" class="scenario-select" placeholder="选择业务场景" :loading="scenariosLoading" @change="changeScenario">
                <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
              </el-select>
            </div>
            <el-alert v-if="resourceError" class="resource-error" type="warning" :title="resourceError" :closable="false" show-icon role="status" />
            <el-alert v-else-if="selectedScenarioId && !resourcesLoading && !resources.length" class="resource-error" type="info" title="此场景暂时没有可授权资源" :closable="false" show-icon />

            <el-table :data="grants" class="permission-table" empty-text="暂无精确授权；默认角色权限仍然生效">
              <el-table-column label="授权主体" min-width="175">
                <template #default="{ row }"><div class="grant-subject"><strong>{{ grantSubject(row) }}</strong><small>{{ row.role_key ? '系统角色' : '单个成员' }}</small></div></template>
              </el-table-column>
              <el-table-column label="资源" min-width="220">
                <template #default="{ row }">
                  <div class="grant-resource">
                    <span>{{ resourceTypeLabel(row.resource_type) }} · {{ grantResourceName(row) }}</span>
                    <small class="mono">{{ shortId(row.resource_id) }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="102">
                <template #default="{ row }"><el-tag size="small" effect="plain">{{ verbLabel(row.verb) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="结果" width="96">
                <template #default="{ row }"><el-tag size="small" :type="row.effect === 'deny' ? 'danger' : 'success'">{{ row.effect === 'deny' ? '拒绝' : '允许' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="创建时间" min-width="142">
                <template #default="{ row }">{{ formatDate(row.created_at) || '—' }}</template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{ row }"><el-button size="small" text type="danger" :loading="removingGrantId === row.id" @click="removeGrant(row)">删除</el-button></template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </section>
    </template>

    <el-dialog v-model="memberDialog" :title="editingMember ? (editingMember.status === 'active' ? '调整成员角色' : '重新加入组织') : '添加组织成员'" width="min(520px, calc(100vw - 28px))" destroy-on-close>
      <el-form label-position="top">
        <div v-if="memberFormError" ref="memberErrorRef" class="dialog-error" role="alert" tabindex="-1"><el-alert type="error" :title="memberFormError" :closable="false" show-icon /></div>
        <template v-if="editingMember">
          <el-form-item label="成员"><div class="member-readonly"><strong>{{ editingMember.display_name || editingMember.email || shortId(editingMember.user_id) }}</strong><span>{{ editingMember.email || editingMember.user_id }}</span></div></el-form-item>
        </template>
        <el-form-item v-else label="同租户用户 ID" required>
          <el-input v-model.trim="memberForm.user_id" maxlength="32" placeholder="输入要加入组织的用户 ID" aria-describedby="member-user-id-help" />
          <div id="member-user-id-help" class="field-help">当前后端以稳定用户 ID 管理成员；可在这里重新加入“已移除”的同租户成员。</div>
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="memberForm.role_key" style="width:100%" aria-label="选择组织成员角色">
            <el-option v-for="role in roles" :key="role.id" :label="`${role.name || role.key}（${roleLabel(role.key)}）`" :value="role.key" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="savingMember" @click="memberDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingMember" @click="saveMember">保存成员</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="grantDialog" title="新建精确授权" width="min(620px, calc(100vw - 28px))" destroy-on-close>
      <el-form label-position="top">
        <div v-if="grantFormError" ref="grantErrorRef" class="dialog-error" role="alert" tabindex="-1"><el-alert type="error" :title="grantFormError" :closable="false" show-icon /></div>
        <el-form-item label="授权主体" required>
          <el-radio-group v-model="grantForm.subject" aria-label="选择授权主体类型" @change="changeGrantSubject">
            <el-radio value="role">角色</el-radio>
            <el-radio value="user">成员</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="grantForm.subject === 'role'" label="系统角色" required>
          <el-select v-model="grantForm.role_key" style="width:100%" placeholder="选择角色">
            <el-option v-for="role in roles" :key="role.id" :label="`${role.name || role.key}（${roleLabel(role.key)}）`" :value="role.key" />
          </el-select>
        </el-form-item>
        <el-form-item v-else label="组织成员" required>
          <el-select v-model="grantForm.user_id" style="width:100%" placeholder="选择有效成员" filterable>
            <el-option v-for="member in activeMembers" :key="member.user_id" :label="memberLabel(member)" :value="member.user_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="授权资源" required>
          <el-select v-model="grantForm.resource_id" style="width:100%" placeholder="先选择上方业务场景" filterable :loading="resourcesLoading" :disabled="!resources.length" @change="changeGrantResource">
            <el-option-group v-for="group in resourceGroups" :key="group.type" :label="resourceTypeLabel(group.type)">
              <el-option v-for="resource in group.resources" :key="resource.id" :label="resourceOptionLabel(resource)" :value="resource.id" />
            </el-option-group>
          </el-select>
          <div v-if="selectedGrantResource?.is_sensitive" class="field-help sensitive-help"><el-icon aria-hidden="true"><WarningFilled /></el-icon>这是敏感属性；请优先采用最小范围和明确的拒绝/允许规则。</div>
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="操作" required>
            <el-select v-model="grantForm.verb" style="width:100%"><el-option v-for="verb in availableVerbs" :key="verb" :label="verbLabel(verb)" :value="verb" /></el-select>
          </el-form-item>
          <el-form-item label="结果" required>
            <el-radio-group v-model="grantForm.effect" aria-label="选择授权结果">
              <el-radio value="allow">允许</el-radio>
              <el-radio value="deny">拒绝</el-radio>
            </el-radio-group>
          </el-form-item>
        </div>
        <el-alert type="info" :closable="false" show-icon title="拒绝优先：同一主体的拒绝规则会覆盖默认角色权限和允许规则。" />
      </el-form>
      <template #footer>
        <el-button :disabled="savingGrant" @click="grantDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingGrant" @click="saveGrant">保存授权</el-button>
      </template>
    </el-dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type {
  Organization,
  OrganizationMember,
  OrganizationRole,
  PermissionEffect,
  PermissionGrant,
  PermissionGrantInput,
  PermissionResource,
  PermissionResourceType,
  PermissionVerb,
  Scenario,
} from '@/types'

type ActiveTab = 'members' | 'grants'
type GrantSubject = 'role' | 'user'
type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary' | ''

const ROLE_LABELS: Record<string, string> = { owner: '所有者', admin: '管理员', operator: '操作员', viewer: '查看者' }
const RESOURCE_TYPE_LABELS: Record<string, string> = { scenario: '场景', object: '对象', property: '属性', action: 'Action', workflow: '工作流' }
const VERB_LABELS: Record<string, string> = { read: '读取', write: '写入', execute: '执行', approve: '审批', manage: '管理' }
const VERBS_BY_RESOURCE: Record<PermissionResourceType, PermissionVerb[]> = {
  scenario: ['read', 'write', 'manage'],
  object: ['read', 'write'],
  property: ['read', 'write'],
  action: ['read', 'write', 'execute'],
  workflow: ['read', 'write', 'execute', 'approve', 'manage'],
}

const loading = ref(false)
const loadError = ref('')
const manageError = ref('')
const canManage = ref(false)
const managementUnavailable = ref(false)
const organization = ref<Organization | null>(null)
const roles = ref<OrganizationRole[]>([])
const members = ref<OrganizationMember[]>([])
const grants = ref<PermissionGrant[]>([])
const scenarios = ref<Scenario[]>([])
const scenariosLoading = ref(false)
const activeTab = ref<ActiveTab>('members')

const selectedScenarioId = ref('')
const resources = ref<PermissionResource[]>([])
const resourceNames = ref<Record<string, string>>({})
const resourcesLoading = ref(false)
const resourceError = ref('')
let resourceRequest = 0

const memberDialog = ref(false)
const editingMember = ref<OrganizationMember | null>(null)
const savingMember = ref(false)
const removingMemberId = ref<string | null>(null)
const memberFormError = ref('')
const memberErrorRef = ref<HTMLElement | null>(null)
const memberForm = ref({ user_id: '', role_key: 'viewer' })

const grantDialog = ref(false)
const savingGrant = ref(false)
const removingGrantId = ref<string | null>(null)
const grantFormError = ref('')
const grantErrorRef = ref<HTMLElement | null>(null)
const grantForm = ref<{ subject: GrantSubject; role_key: string; user_id: string; resource_id: string; resource_type: PermissionResourceType; verb: PermissionVerb; effect: PermissionEffect }>({
  subject: 'role', role_key: 'viewer', user_id: '', resource_id: '', resource_type: 'scenario', verb: 'read', effect: 'allow',
})

const activeMembers = computed(() => members.value.filter((member) => member.status === 'active'))
const memberByUserId = computed(() => new Map(members.value.map((member) => [member.user_id, member])))
const selectedGrantResource = computed(() => resources.value.find((resource) => resource.id === grantForm.value.resource_id) || null)
const availableVerbs = computed(() => VERBS_BY_RESOURCE[grantForm.value.resource_type] || ['read'])
const resourceGroups = computed(() => {
  const grouped = new Map<string, PermissionResource[]>()
  for (const resource of resources.value) {
    const group = grouped.get(resource.resource_type) || []
    group.push(resource)
    grouped.set(resource.resource_type, group)
  }
  return Array.from(grouped, ([type, groupedResources]) => ({ type, resources: groupedResources }))
})

function errorMessage(cause: unknown, fallback: string) {
  const error = cause as { message?: string; response?: { data?: { detail?: string } } }
  return error?.response?.data?.detail || error?.message || fallback
}
function errorStatus(cause: unknown) {
  const error = cause as { status?: number; response?: { status?: number } }
  return Number(error?.status || error?.response?.status || 0)
}
function isDismissal(cause: unknown) {
  if (cause === 'cancel' || cause === 'close') return true
  const error = cause as { message?: string }
  return error?.message === 'cancel' || error?.message === 'close'
}
function roleLabel(value?: string) { return ROLE_LABELS[value || ''] || value || '未分配' }
function roleTagType(value?: string): TagType {
  return ({ owner: 'danger', admin: 'warning', operator: 'primary', viewer: 'info' } as Record<string, TagType>)[value || ''] || 'info'
}
function resourceTypeLabel(value?: string) { return RESOURCE_TYPE_LABELS[value || ''] || value || '资源' }
function verbLabel(value?: string) { return VERB_LABELS[value || ''] || value || '—' }
function shortId(value?: string | null) { return value && value.length > 12 ? `${value.slice(0, 8)}…` : value || '—' }
function formatDate(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function memberLabel(member: OrganizationMember) {
  return `${member.display_name || member.email || shortId(member.user_id)} · ${member.email || member.user_id}`
}
function resourceKey(type: string, id: string) { return `${type}:${id}` }
function resourceOptionLabel(resource: PermissionResource) {
  const details = [resourceTypeLabel(resource.resource_type)]
  if (resource.is_sensitive) details.push('敏感')
  if (resource.access_scope === 'restricted') details.push('受限')
  return `${resource.name}（${details.join(' · ')}）`
}
function grantSubject(grant: PermissionGrant) {
  if (grant.role_key) return roleLabel(grant.role_key)
  const member = grant.user_id ? memberByUserId.value.get(grant.user_id) : null
  return member ? memberLabel(member) : shortId(grant.user_id)
}
function grantResourceName(grant: PermissionGrant) {
  return resourceNames.value[resourceKey(grant.resource_type, grant.resource_id)] || shortId(grant.resource_id)
}
function defaultRoleKey() {
  return roles.value.find((role) => role.key === 'viewer')?.key || roles.value[0]?.key || 'viewer'
}

function setMemberFormError(message: string) {
  memberFormError.value = message
  void nextTick(() => memberErrorRef.value?.focus())
}
function setGrantFormError(message: string) {
  grantFormError.value = message
  void nextTick(() => grantErrorRef.value?.focus())
}

async function loadManagementData() {
  try {
    const [nextMembers, nextGrants] = await Promise.all([api.listOrganizationMembers(), api.listPermissionGrants()])
    members.value = nextMembers
    grants.value = nextGrants
    canManage.value = true
    managementUnavailable.value = false
    manageError.value = ''
  } catch (cause) {
    members.value = []
    grants.value = []
    canManage.value = false
    managementUnavailable.value = errorStatus(cause) !== 403
    manageError.value = errorMessage(cause, managementUnavailable.value ? '无法读取成员与授权规则' : '当前账号没有组织管理权限')
  }
}

async function loadScenarios() {
  scenariosLoading.value = true
  try {
    scenarios.value = await api.listScenarios()
    if (!scenarios.value.some((scenario) => scenario.id === selectedScenarioId.value)) {
      selectedScenarioId.value = scenarios.value[0]?.id || ''
    }
    if (selectedScenarioId.value) await loadResources(selectedScenarioId.value)
  } catch (cause) {
    resourceError.value = errorMessage(cause, '无法加载可授权场景')
  } finally {
    scenariosLoading.value = false
  }
}

async function loadResources(scenarioId: string) {
  const request = ++resourceRequest
  resourcesLoading.value = true
  resourceError.value = ''
  resources.value = []
  try {
    const nextResources = await api.listPermissionResources(scenarioId)
    if (request !== resourceRequest || scenarioId !== selectedScenarioId.value) return
    resources.value = nextResources
    resourceNames.value = {
      ...resourceNames.value,
      ...Object.fromEntries(nextResources.map((resource) => [resourceKey(resource.resource_type, resource.id), resource.name])),
    }
  } catch (cause) {
    if (request !== resourceRequest || scenarioId !== selectedScenarioId.value) return
    resourceError.value = errorMessage(cause, '无法读取此场景的可授权资源')
  } finally {
    if (request === resourceRequest) resourcesLoading.value = false
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [nextOrganization, nextRoles] = await Promise.all([api.getOrganization(), api.listOrganizationRoles()])
    organization.value = nextOrganization
    roles.value = nextRoles
    await loadManagementData()
    if (canManage.value) await loadScenarios()
  } catch (cause) {
    organization.value = null
    roles.value = []
    canManage.value = false
    managementUnavailable.value = false
    loadError.value = errorMessage(cause, '权限管理信息加载失败')
  } finally {
    loading.value = false
  }
}

async function changeScenario(value: string | number | boolean) {
  const scenarioId = String(value || '')
  selectedScenarioId.value = scenarioId
  grantForm.value.resource_id = ''
  grantForm.value.resource_type = 'scenario'
  if (scenarioId) await loadResources(scenarioId)
}

function openMemberDialog(member?: OrganizationMember) {
  editingMember.value = member || null
  memberFormError.value = ''
  memberForm.value = {
    user_id: member?.user_id || '',
    role_key: member?.role_key || defaultRoleKey(),
  }
  memberDialog.value = true
}
async function saveMember() {
  const userId = memberForm.value.user_id.trim()
  if (!userId) {
    setMemberFormError('请填写同租户用户 ID')
    return
  }
  if (!memberForm.value.role_key) {
    setMemberFormError('请选择组织角色')
    return
  }
  savingMember.value = true
  memberFormError.value = ''
  try {
    await api.saveOrganizationMember({ user_id: userId, role_key: memberForm.value.role_key })
    memberDialog.value = false
    await loadManagementData()
    ElMessage.success(editingMember.value?.status === 'removed' ? '成员已重新加入组织' : '成员角色已保存')
  } catch (cause) {
    setMemberFormError(errorMessage(cause, '成员保存失败'))
  } finally {
    savingMember.value = false
  }
}
async function removeMember(member: OrganizationMember) {
  try {
    await ElMessageBox.confirm(
      `移除「${member.display_name || member.email || shortId(member.user_id)}」后，该账号无法继续访问当前组织资源；审计记录会保留。确定继续吗？`,
      '移除组织成员',
      { type: 'warning', confirmButtonText: '移除成员', cancelButtonText: '取消' },
    )
    removingMemberId.value = member.id
    await api.removeOrganizationMember(member.id)
    await loadManagementData()
    ElMessage.success('成员已移除')
  } catch (cause) {
    if (!isDismissal(cause)) ElMessage.error(errorMessage(cause, '移除成员失败'))
  } finally {
    removingMemberId.value = null
  }
}

function openGrantDialog() {
  if (!resources.value.length) return
  grantFormError.value = ''
  grantForm.value = {
    subject: 'role',
    role_key: defaultRoleKey(),
    user_id: '',
    resource_id: '',
    resource_type: 'scenario',
    verb: 'read',
    effect: 'allow',
  }
  grantDialog.value = true
}
function changeGrantSubject() {
  grantFormError.value = ''
  grantForm.value.role_key = grantForm.value.subject === 'role' ? defaultRoleKey() : ''
  grantForm.value.user_id = ''
}
function changeGrantResource(value: string | number | boolean) {
  const resource = resources.value.find((item) => item.id === String(value))
  if (!resource) return
  grantForm.value.resource_id = resource.id
  grantForm.value.resource_type = resource.resource_type as PermissionResourceType
  if (!availableVerbs.value.includes(grantForm.value.verb)) grantForm.value.verb = availableVerbs.value[0] || 'read'
}
async function saveGrant() {
  if (!grantForm.value.resource_id) {
    setGrantFormError('请选择授权资源')
    return
  }
  if (grantForm.value.subject === 'role' && !grantForm.value.role_key) {
    setGrantFormError('请选择授权角色')
    return
  }
  if (grantForm.value.subject === 'user' && !grantForm.value.user_id) {
    setGrantFormError('请选择组织成员')
    return
  }
  const payload: PermissionGrantInput = {
    resource_type: grantForm.value.resource_type,
    resource_id: grantForm.value.resource_id,
    verb: grantForm.value.verb,
    effect: grantForm.value.effect,
    ...(grantForm.value.subject === 'role'
      ? { role_key: grantForm.value.role_key as PermissionGrantInput['role_key'] }
      : { user_id: grantForm.value.user_id }),
  }
  savingGrant.value = true
  grantFormError.value = ''
  try {
    await api.createPermissionGrant(payload)
    grantDialog.value = false
    await loadManagementData()
    ElMessage.success('精确授权已保存')
  } catch (cause) {
    setGrantFormError(errorMessage(cause, '授权保存失败'))
  } finally {
    savingGrant.value = false
  }
}
async function removeGrant(grant: PermissionGrant) {
  try {
    await ElMessageBox.confirm(
      `删除这条「${grantSubject(grant)} · ${resourceTypeLabel(grant.resource_type)} · ${verbLabel(grant.verb)}」授权规则？删除后将恢复默认角色和其他精确规则的计算结果。`,
      '删除授权规则',
      { type: 'warning', confirmButtonText: '删除规则', cancelButtonText: '取消' },
    )
    removingGrantId.value = grant.id
    await api.deletePermissionGrant(grant.id)
    await loadManagementData()
    ElMessage.success('授权规则已删除')
  } catch (cause) {
    if (!isDismissal(cause)) ElMessage.error(errorMessage(cause, '删除授权规则失败'))
  } finally {
    removingGrantId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.permissions-page { min-height: 100%; padding: 24px 28px 34px; }
.permissions-header, .section-heading, .section-toolbar, .resource-picker { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.eyebrow, .section-kicker { color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .14em; }
.permissions-header { margin-bottom: 18px; }
.permissions-header h2 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.permissions-header p { max-width: 760px; margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.65; }
.page-alert { margin-bottom: 16px; }
.loading-card { padding: 22px; }
.organization-card { display: grid; grid-template-columns: 48px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 18px; }
.organization-mark { width: 48px; height: 48px; display: inline-flex; align-items: center; justify-content: center; border-radius: 14px; color: var(--primary-600); background: var(--grad-soft); font-size: 21px; }
.organization-copy { min-width: 0; }
.organization-copy > span { color: var(--text-3); font-size: 11px; font-weight: 700; }
.organization-copy h3 { margin: 3px 0 4px; color: var(--text); font-size: 18px; letter-spacing: -.02em; }
.organization-copy p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.55; }
.organization-meta { display: grid; grid-template-columns: repeat(3, minmax(72px, 1fr)); gap: 10px; margin: 0; }
.organization-meta div { min-width: 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); text-align: center; }
.organization-meta dt { color: var(--text-3); font-size: 10px; }
.organization-meta dd { margin: 3px 0 0; color: var(--text); font-size: 17px; font-weight: 750; font-variant-numeric: tabular-nums; }
.role-section { margin-top: 18px; }
.section-heading { margin-bottom: 10px; }
.section-heading h3, .section-toolbar h3 { margin: 3px 0 0; color: var(--text); font-size: 16px; }
.section-heading > span { max-width: 500px; color: var(--text-3); font-size: 12px; line-height: 1.55; text-align: right; }
.role-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.role-card { min-height: 116px; padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); box-shadow: var(--shadow-xs); }
.role-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.role-card strong { color: var(--text); font-size: 14px; }
.role-card p { margin: 9px 0 0; color: var(--text-2); font-size: 11.5px; line-height: 1.6; }
.manage-alert { margin-top: 18px; }
.management-card { margin-top: 18px; padding: 8px 16px 16px; }
.section-toolbar { padding: 10px 0 15px; }
.section-toolbar h3 { margin: 0 0 4px; }
.section-toolbar p { max-width: 660px; margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.55; }
.permission-table { width: 100%; }
.member-cell, .grant-subject, .grant-resource { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.member-cell strong, .grant-subject strong, .grant-resource span { overflow: hidden; color: var(--text); font-size: 12.5px; text-overflow: ellipsis; white-space: nowrap; }
.member-cell span, .grant-subject small, .grant-resource small { overflow: hidden; color: var(--text-3); font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.resource-picker { align-items: center; margin: 0 0 12px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); }
.resource-picker-copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.resource-picker-copy span { color: var(--text-2); font-size: 12px; font-weight: 700; }
.resource-picker-copy small { color: var(--text-3); font-size: 11px; }
.scenario-select { width: min(300px, 100%); }
.resource-error { margin: 0 0 12px; }
.dialog-error { margin-bottom: 14px; outline: none; }
.member-readonly { display: flex; flex-direction: column; gap: 3px; width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }
.member-readonly strong { color: var(--text); font-size: 13px; }
.member-readonly span, .field-help { color: var(--text-3); font-size: 11px; line-height: 1.55; }
.field-help { margin-top: 6px; }
.sensitive-help { display: flex; align-items: center; gap: 5px; color: var(--warning); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1024px) { .role-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .organization-card { grid-template-columns: 48px minmax(0, 1fr); } .organization-meta { grid-column: 1 / -1; } }
@media (max-width: 720px) { .permissions-page { padding: 18px 14px 24px; } .permissions-header, .section-heading, .section-toolbar, .resource-picker { flex-direction: column; } .permissions-header > .el-button, .section-toolbar > .el-button { align-self: stretch; } .section-heading > span { text-align: left; } .organization-card { grid-template-columns: 42px minmax(0, 1fr); padding: 14px; } .organization-mark { width: 42px; height: 42px; } .organization-meta { grid-template-columns: repeat(3, minmax(0, 1fr)); } .scenario-select { width: 100%; } }
@media (max-width: 440px) { .role-grid, .form-grid { grid-template-columns: 1fr; } .organization-meta { grid-template-columns: 1fr; } }
</style>
