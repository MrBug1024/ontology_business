<template>
  <section aria-label="已配置业务系统">
    <el-alert v-if="error && !editorOpen" :title="error" type="error" :closable="false" />
    <p v-if="loading" role="status">正在加载业务系统…</p>
    <p v-else-if="!project.document.target_systems.length" class="discovery-empty-note">尚未添加业务系统</p>
    <article v-for="target in project.document.target_systems" :key="target.key" class="discovery-finding-card">
      <strong>{{ target.name }}</strong><p class="system-url">{{ target.base_url }}{{ target.browser?.entry_path || '' }}</p>
      <small>{{ !target.enabled ? '已停用' : target.access_mode === 'anonymous_readonly' ? '公开网站' : accessLabel(target.key) }}</small>
      <div class="distill-actions"><el-button :disabled="disabled" @click="edit(target)">配置</el-button><el-button v-if="accessOf(target.key)?.status === 'active'" text :disabled="disabled" @click="revoke(target.key)">撤销授权</el-button></div>
    </article>
    <el-button :disabled="disabled || project.document.target_systems.length >= 10" @click="edit()">添加业务系统</el-button>
    <el-dialog v-model="editorOpen" title="配置业务系统" width="min(560px, 94vw)" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @closed="clearCredentials">
      <el-form label-position="top" :disabled="busy" @submit.prevent="saveSystem">
        <el-form-item label="系统名称"><el-input v-model="targetDraft.name" :maxlength="200" /></el-form-item>
        <el-form-item label="网页链接"><el-input v-model="website" placeholder="https://example.com/#/workbench" :maxlength="2000" /></el-form-item>
        <el-checkbox v-model="publicSite" @change="clearCredentials">公开网站，无需登录</el-checkbox>
        <template v-if="!publicSite">
          <label for="distillation-system-account">登录账号</label><input id="distillation-system-account" ref="accountInput" class="system-input" autocomplete="off" :disabled="busy" maxlength="200" placeholder="专用只读账号" />
          <label for="distillation-system-secret">登录密码</label><input id="distillation-system-secret" ref="secretInput" class="system-input" type="password" autocomplete="new-password" :disabled="busy" maxlength="4096" :placeholder="existingActive ? '不修改时留空' : '登录密码'" />
          <el-checkbox v-model="authorized">已获得该系统的只读调查权限</el-checkbox>
        </template>
        <details class="system-advanced"><summary>访问范围与登录设置</summary>
          <el-form-item label="调查范围说明"><el-input v-model="targetDraft.purpose" type="textarea" :maxlength="4000" /></el-form-item>
          <el-form-item label="允许访问的路径（每行一个，/ 表示本站）"><el-input v-model="paths" type="textarea" :rows="2" /></el-form-item>
          <el-form-item label="动态登录接口路径（需要时填写）"><el-input v-model="loginPaths" type="textarea" :rows="2" placeholder="/api/login" /></el-form-item>
          <el-form-item label="允许 POST 的只读查询接口（需要时填写）"><el-input v-model="queryPaths" type="textarea" :rows="2" placeholder="/api/search" /></el-form-item>
          <label v-if="!publicSite" for="distillation-access-expiry">授权到期时间</label><input v-if="!publicSite" id="distillation-access-expiry" v-model="expiry" class="system-input" type="datetime-local" :disabled="busy" />
          <el-checkbox v-model="targetDraft.enabled">启用调查</el-checkbox>
        </details>
      </el-form>
      <p v-if="!publicSite" class="discovery-muted">凭据加密保存，仅用于该系统登录。授权至 {{ expiry.replace('T', ' ') }}。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <template #footer><el-button :disabled="busy" @click="editorOpen = false">取消</el-button><el-button type="primary" :loading="busy" :disabled="!targetDraft.name.trim() || !website.trim() || (!publicSite && !authorized)" @click="saveSystem">保存系统</el-button></template>
    </el-dialog>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { businessDistillationApi as api, type SystemAccessStatus, type SystemAccessInput } from '@/api/businessDistillation'
import type { DistillationProject, DistillationTargetSystem } from '@/types/businessDistillation'
import { createClientRequestId } from '@/utils/clientRequestId'
const props = defineProps<{ project: DistillationProject; canEdit: boolean; dirty: boolean }>()
const emit = defineEmits<{ updated: [project: DistillationProject] }>()
function emptyTarget(): DistillationTargetSystem { return { key: `system_${createClientRequestId().replace(/-/g, '')}`, name: '', base_url: '', purpose: '业务流程与历史数据调查', allowed_paths: ['/'], notes: '', access_mode: 'authorized_readonly', enabled: true, browser: { entry_path: '/', login_paths: [], readonly_post_paths: [] } } }
function defaultExpiry() { const date = new Date(Date.now() + 7 * 86400_000); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) }
const targetDraft = ref(emptyTarget()), website = ref(''), paths = ref('/'), loginPaths = ref(''), queryPaths = ref('')
const access = ref<SystemAccessStatus[]>([]), error = ref(''), busy = ref(false), loading = ref(false), editorOpen = ref(false)
const publicSite = ref(false), expiry = ref(defaultExpiry()), authorized = ref(false)
const secretInput = ref<HTMLInputElement>(), accountInput = ref<HTMLInputElement>()
const disabled = computed(() => !props.canEdit || props.dirty || busy.value || loading.value)
const existingActive = computed(() => accessOf(targetDraft.value.key)?.status === 'active')
let controller: AbortController | undefined
let ownerRevision = 0
function clearCredentials() { if (secretInput.value) secretInput.value.value = ''; if (accountInput.value) accountInput.value.value = '' }
function accessOf(key: string) { return access.value.find(item => item.target_key === key) }
function accessLabel(key: string) { return ({ active: '已获只读授权', expired: '授权已到期', revoked: '授权已撤销', scope_changed: '范围已变化，需更新授权', missing: '待配置登录授权' })[accessOf(key)?.status || 'missing'] }
function edit(target?: DistillationTargetSystem) {
  error.value = ''; ownerRevision = props.project.revision
  targetDraft.value = target ? { ...target, allowed_paths: [...target.allowed_paths], browser: target.browser ? { ...target.browser, login_paths: [...target.browser.login_paths], readonly_post_paths: [...target.browser.readonly_post_paths] } : emptyTarget().browser } : emptyTarget()
  website.value = target ? targetDraft.value.base_url + (targetDraft.value.browser?.entry_path || '') : ''
  paths.value = targetDraft.value.allowed_paths.join('\n'); loginPaths.value = targetDraft.value.browser?.login_paths.join('\n') || ''; queryPaths.value = targetDraft.value.browser?.readonly_post_paths.join('\n') || ''
  publicSite.value = targetDraft.value.access_mode === 'anonymous_readonly'; authorized.value = false; expiry.value = defaultExpiry(); clearCredentials(); editorOpen.value = true
}
async function loadAccess() {
  controller?.abort(); const current = new AbortController(); controller = current; loading.value = true; access.value = []
  try { const result = await api.systemAccess(props.project.id, current.signal); if (!current.signal.aborted) access.value = result }
  catch (caught: unknown) { if (!current.signal.aborted) error.value = caught instanceof Error ? caught.message : '系统授权状态加载失败' }
  finally { if (!current.signal.aborted) loading.value = false }
}
async function perform(action: (signal: AbortSignal) => Promise<DistillationProject>) {
  if (disabled.value) return
  busy.value = true; error.value = ''; controller?.abort(); const current = new AbortController(); controller = current
  try { const result = await action(current.signal); if (!current.signal.aborted) { editorOpen.value = false; clearCredentials(); emit('updated', result) } }
  catch (caught: unknown) { if (!current.signal.aborted) error.value = caught instanceof Error ? caught.message : '保存未完成，请重试' }
  finally { if (!current.signal.aborted) busy.value = false }
}
const lines = (text: string) => text.split('\n').map(value => value.trim()).filter(Boolean)
async function saveSystem() {
  if (disabled.value) return
  if (ownerRevision !== props.project.revision) { error.value = '项目已更新，请重新打开配置'; return }
  let url: URL
  try { url = new URL(website.value.trim()); if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search) throw new Error() }
  catch { error.value = '请填写网页链接，不包含账号、密码或查询参数'; return }
  const target: DistillationTargetSystem = { ...targetDraft.value, base_url: url.origin, allowed_paths: lines(paths.value), access_mode: publicSite.value ? 'anonymous_readonly' : 'authorized_readonly', browser: { entry_path: url.pathname + url.hash, login_paths: lines(loginPaths.value), readonly_post_paths: lines(queryPaths.value) } }
  const username = accountInput.value?.value || '', secret = secretInput.value?.value || ''
  clearCredentials()
  let credentials: Omit<SystemAccessInput, 'expected_revision'> | null = null
  if (!publicSite.value && (secret || username || !existingActive.value)) {
    const expires = new Date(expiry.value)
    if (!authorized.value || !secret || !username || !Number.isFinite(expires.getTime())) { error.value = '请填写登录账号、密码和授权期限'; return }
    credentials = { auth_type: 'browser', username, secret, expires_at: expires.toISOString(), authorization_basis: target.purpose, authorized_readonly: true }
  }
  await perform(signal => api.configureSystem(props.project.id, { expected_revision: ownerRevision, target, credentials }, signal))
}
async function revoke(key: string) { await perform(signal => api.revokeSystem(props.project.id, key, props.project.revision, signal)) }
watch(() => [props.project.id, props.project.revision], () => { editorOpen.value = false; busy.value = false; clearCredentials(); void loadAccess() }, { immediate: true })
onBeforeUnmount(() => { controller?.abort(); clearCredentials() })
</script>
<style scoped>
.system-input { box-sizing: border-box; display: block; width: 100%; min-height: 40px; margin: 8px 0 18px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); font: inherit; }
label { display: block; margin-top: 16px; }
.system-input:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.system-advanced { margin-top: 18px; }
.system-advanced summary { cursor: pointer; margin-bottom: 16px; }
.system-url { overflow-wrap: anywhere; }
</style>
