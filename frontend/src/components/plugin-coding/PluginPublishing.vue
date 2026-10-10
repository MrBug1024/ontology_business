<template>
  <section class="plugin-publishing" aria-label="插件发布">
    <header class="publishing-heading"><div><h2>选择场景插件，发布给安装方</h2><p>选择已定版的插件版本，发布安装来源，再复制对应版本的安装命令。</p></div><RouterLink to="/plugin-studio">进入插件开发<el-icon aria-hidden="true"><ArrowRight /></el-icon></RouterLink></header>
    <el-alert v-if="error || deliveryError" :title="error || deliveryError" type="error" :closable="false" show-icon />
    <div class="publishing-layout" v-loading="loading">
      <div class="version-list"><header><span>定版快照</span><el-button text :disabled="loading" aria-label="刷新插件版本" @click="load"><el-icon><Refresh /></el-icon></el-button></header>
        <button v-for="item in items" :key="item.id" type="button" class="version-item" :class="{ selected: item.id === selected?.id }" :aria-pressed="item.id === selected?.id" :disabled="loading || downloading" @click="emit('select', item)"><span><b>{{ item.scenario_name }}</b><small>{{ item.release_name }} · {{ item.host_label }}</small></span><span class="version">v{{ item.plugin_version }}<small>{{ statusLabel(item) }}</small></span></button>
        <el-empty v-if="!loading && !items.length && !error" description="尚无定版快照，请先完成开发和审阅" :image-size="65" />
        <footer v-if="offset || hasMore"><el-button :disabled="loading || downloading || offset === 0" @click="offset -= 50">上一页</el-button><el-button :disabled="loading || downloading || !hasMore" @click="offset += 50">下一页</el-button></footer>
      </div>
      <article v-if="selected" class="publication-detail"><span class="eyebrow">REVIEWED SNAPSHOT</span><h3>{{ selected.scenario_name }} <span>v{{ selected.plugin_version }}</span></h3><dl><div><dt>安装宿主</dt><dd>{{ selected.host_label }}</dd></div><div><dt>能力版本</dt><dd>{{ selected.release_name }}</dd></div><div><dt>定版时间</dt><dd>{{ new Date(selected.created_at).toLocaleString('zh-CN') }}</dd></div><div v-if="selected.retired"><dt>下线时间</dt><dd>{{ selected.retired_at ? new Date(selected.retired_at).toLocaleString('zh-CN') : '—' }}</dd></div></dl>
        <el-alert v-if="!selected.available" :title="selected.unavailable_reason" type="warning" :closable="false" show-icon />
        <PluginPublicationPanel :key="selected.id" :artifact="selected" />
        <div class="delivery-options"><button type="button" :disabled="!selected.available || loading || downloading" @click="download('plugin')"><el-icon><Box /></el-icon><span><b>插件安装包</b><small>用于本地加载、第三方集成与安装</small></span><el-icon><Download /></el-icon></button><button type="button" :disabled="!selected.available || loading || downloading" @click="download('marketplace')"><el-icon><Connection /></el-icon><span><b>Marketplace 发布包</b><small>市场目录、插件和仓库部署说明</small></span><el-icon><Download /></el-icon></button></div>
        <p v-if="downloading" role="status">正在恢复已定版工件并校验内容…</p><p v-if="done" role="status" class="delivery-result">{{ done }}</p><p class="delivery-note">发布中心独立管理快照生命周期：上线、下线与删除只作用于快照，不影响插件开发中的源码。也可按 Marketplace 包内说明自托管 Git 仓库；调用凭据与 MCP 地址由安装方另行配置。</p>
        <div class="foot-links">
          <RouterLink :to="{ name: 'plugin-development', query: { workspace: selected.workspace_id } }">继续开发新版本</RouterLink>
          <button v-if="!selected.retired" type="button" class="retire" :disabled="retiring || deleting || loading || downloading" @click="retire">{{ retiring ? '正在下线…' : '下线此版本' }}</button>
          <button v-else type="button" class="retire danger" :disabled="retiring || deleting || loading || downloading" @click="remove">{{ deleting ? '正在删除…' : '删除此版本' }}</button>
        </div>
      </article><div v-else class="publication-empty"><el-icon :size="36"><Box /></el-icon><h3>先选一个插件版本</h3><p>开发、审阅和交付各有明确的完成状态。</p></div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onBeforeUnmount, ref, toRef, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { ArrowRight, Box, Connection, Download, Refresh } from '@element-plus/icons-vue'
import { pluginArtifactsApi } from '@/api/pluginArtifacts'
import { usePluginArtifacts } from '@/composables/usePluginArtifacts'
import PluginPublicationPanel from './PluginPublicationPanel.vue'
import type { PluginArtifact } from '@/types/pluginArtifact'
const props = defineProps<{ scenarioId: string; artifactId: string }>()
const emit = defineEmits<{ select: [value: PluginArtifact] }>()
const { items, selected, loading, error, offset, hasMore, load } = usePluginArtifacts(toRef(props, 'scenarioId'), toRef(props, 'artifactId'))
const downloading = ref(false)
const deliveryError = ref('')
const done = ref('')
const retiring = ref(false)
const deleting = ref(false)
let controller: AbortController | undefined
let generation = 0
function statusLabel(item: PluginArtifact) {
  if (item.retired) return '已下线'
  return item.available ? '可交付' : '需处理'
}
watch(() => [props.scenarioId, props.artifactId], () => { generation++; controller?.abort(); downloading.value = false; done.value = ''; deliveryError.value = '' })
async function download(format: 'plugin' | 'marketplace') {
  const value = selected.value
  if (!value?.available || downloading.value || loading.value) return
  controller = new AbortController()
  const current = ++generation
  downloading.value = true
  done.value = ''
  deliveryError.value = ''
  try {
    const blob = await pluginArtifactsApi.download(value, format, controller.signal)
    if (current !== generation) return
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${value.package_name}-${value.plugin_version}${format === 'marketplace' ? '-marketplace' : ''}.zip`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    done.value = format === 'marketplace' ? '已生成所选版本的发布材料，尚未上传外部仓库。' : '已生成所选版本的插件安装包。'
  } catch (caught: unknown) { if (current === generation) deliveryError.value = caught instanceof Error ? caught.message : '插件工件下载失败，请刷新后重试' }
  finally { if (current === generation) downloading.value = false }
}
async function retire() {
  const value = selected.value
  if (!value || value.retired || retiring.value || loading.value) return
  try {
    await ElMessageBox.confirm(`下线 v${value.plugin_version} 后，此版本不再对外交付，公开安装源与安装命令立即失效；插件开发源码不受影响。下线后仍可删除此版本。`, '下线此插件版本？', { confirmButtonText: '下线', cancelButtonText: '取消', type: 'warning' })
  } catch { return }
  if (selected.value?.id !== value.id) return
  controller = new AbortController()
  const current = ++generation
  retiring.value = true
  deliveryError.value = ''
  done.value = ''
  try {
    const updated = await pluginArtifactsApi.retire(value, controller.signal)
    if (current !== generation) return
    done.value = `v${value.plugin_version} 已下线；插件开发源码不受影响。`
    await load()
    if (updated?.id && items.value.some(item => item.id === updated.id)) emit('select', updated)
  } catch (caught: unknown) {
    if (current !== generation) return
    deliveryError.value = caught instanceof Error ? caught.message : '插件版本下线失败，请刷新后重试'
    await load()
  } finally { if (current === generation) retiring.value = false }
}
async function remove() {
  const value = selected.value
  if (!value || !value.retired || deleting.value || loading.value) return
  try {
    await ElMessageBox.confirm(`删除 v${value.plugin_version} 后，此快照从发布中心移除，不再出现在任何列表；审计记录保留。该操作不可恢复，且与插件开发源码无关。`, '删除已下线的版本？', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
  } catch { return }
  if (selected.value?.id !== value.id) return
  controller = new AbortController()
  const current = ++generation
  deleting.value = true
  deliveryError.value = ''
  done.value = ''
  try {
    await pluginArtifactsApi.remove(value, controller.signal)
    if (current !== generation) return
    done.value = `v${value.plugin_version} 已从发布中心删除；审计保留。`
    emit('select', value)
    await load()
  } catch (caught: unknown) {
    if (current !== generation) return
    deliveryError.value = caught instanceof Error ? caught.message : '插件版本删除失败，请刷新后重试'
    await load()
  } finally { if (current === generation) deleting.value = false }
}
onBeforeUnmount(() => { generation++; controller?.abort() })
</script>
<style scoped>
.publishing-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin: 8px 0 26px; }
h2 { font-size: 19px; margin: 0; font-weight: 600; }
p { font-size: 13px; line-height: 1.8; color: var(--text-2); }
a { display: inline-flex; align-items: center; gap: 6px; color: var(--primary); font-size: 13px; text-decoration: none; }
.publishing-layout { display: grid; grid-template-columns: minmax(240px, 30%) minmax(0, 1fr); border: 1px solid var(--border); border-radius: 12px; overflow: clip; min-height: 420px; background: var(--surface); }
.version-list { position: sticky; top: 0; align-self: start; max-height: calc(100dvh - 90px); overflow: auto; border-right: 1px solid var(--border); padding: 12px; }
.version-list > header { display: flex; justify-content: space-between; align-items: center; padding: 4px 10px 14px; font-size: 12px; color: var(--text-2); }
.version-item { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 16px; border: 1px solid transparent; background: transparent; border-radius: 8px; padding: 15px 12px; text-align: left; margin-bottom: 6px; }
.version-item:hover, .version-item.selected { background: var(--primary-soft); border-color: var(--border); }
.version-item b, .version-item small { display: block; overflow-wrap: anywhere; }
.version-item b { font-size: 13px; font-weight: 600; line-height: 1.7; }
.version-item small { font-size: 11px; color: var(--text-2); margin-top: 5px; }
.version { flex: 0 0 auto; text-align: right; font-size: 12px; }
.publication-detail { padding: 30px; min-width: 0; }
.eyebrow { font-size: 10px; letter-spacing: 1.5px; color: var(--text-2); }
h3 { font-size: 20px; font-weight: 600; margin: 12px 0 24px; overflow-wrap: anywhere; }
h3 span { font-size: 14px; font-weight: 400; white-space: nowrap; }
dl { font-size: 12px; }
dl > div { display: flex; gap: 22px; margin-bottom: 12px; }
dt { flex: 0 0 60px; color: var(--text-2); }
dd { margin: 0; overflow-wrap: anywhere; }
.delivery-options { display: grid; gap: 12px; margin: 26px 0 18px; }
.delivery-options button { display: flex; align-items: center; gap: 16px; padding: 18px; border: 1px solid var(--border); background: var(--surface); border-radius: 9px; text-align: left; }
.delivery-options span { flex: 1; }
.delivery-options b, .delivery-options small { display: block; }
.delivery-options b { font-size: 14px; font-weight: 600; }
.delivery-options small { margin-top: 6px; font-size: 12px; color: var(--text-2); line-height: 1.6; }
.delivery-options button:hover:not(:disabled) { border-color: var(--primary); }
.delivery-note { margin-bottom: 22px; }
.delivery-result { color: var(--success); }
.foot-links { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.retire { padding: 6px 14px; border: 1px solid var(--border-strong); border-radius: 7px; background: transparent; color: var(--text-2); font-size: 12px; cursor: pointer; }
.retire:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }
.retire.danger { color: var(--danger); border-color: var(--danger); }
.retire:disabled { opacity: .5; cursor: not-allowed; }
.retired-note { color: var(--text-2); font-size: 12px; }
.publication-empty { display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 28px; color: var(--text-2); text-align: center; }
.publication-empty h3 { font-size: 16px; margin-bottom: 0; }
footer { display: flex; justify-content: space-between; margin-top: 20px; }
button { font: inherit; color: inherit; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
button:focus-visible, a:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 800px) { .publishing-layout { grid-template-columns: minmax(0, 1fr); } .version-list { position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--border); } .publication-detail { padding: 22px 18px; } .publishing-heading { align-items: flex-start; flex-direction: column; } }
</style>
