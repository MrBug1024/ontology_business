<template>
  <section class="publication-panel" aria-label="场景插件发布与安装">
    <header><h4>发布这个插件版本</h4><el-button text :disabled="loading || saving" aria-label="刷新插件发布状态" @click="load"><el-icon aria-hidden="true"><Refresh /></el-icon></el-button></header>
    <p v-if="loading" role="status">正在读取发布状态…</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <p v-if="error">当前状态尚未确认。请刷新后再操作，避免重复发布。</p>
    <template v-if="publication">
      <p class="publication-status" role="status">{{ labels[publication.status] }}<span v-if="publication.published_at"> · {{ new Date(publication.published_at).toLocaleString('zh-CN') }}</span></p>
      <el-alert v-if="publication.unavailable_reason" :title="publication.unavailable_reason" type="warning" :closable="false" show-icon />
      <template v-if="publication.status !== 'published'">
        <p>发布后，安装方可通过安装地址下载这个版本的插件代码和能力契约。业务数据与调用凭据不包含在包中。</p>
        <el-checkbox v-model="confirmed" :disabled="saving || loading">我确认发布此版本的插件代码与能力契约</el-checkbox>
        <el-button class="publish-button" type="primary" :loading="saving" :disabled="!confirmed || loading || Boolean(error) || !artifact.available || !publication.configuration_ready" @click="publish">发布插件并生成安装命令</el-button>
      </template>
      <template v-else>
        <p v-if="publication.installation?.scope === 'local_test'" class="local-note">当前为本机试装地址。远程第三方安装需要部署者配置公开 HTTPS 地址。</p>
        <div v-if="publication.available && publication.installation" class="installation">
          <h4>复制命令，安装插件</h4><p>目标宿主：{{ publication.installation.host === 'codex' ? 'OpenAI Codex' : 'Claude Code' }} · v{{ publication.installation.plugin_version }}</p>
          <nav aria-label="安装命令系统"><button type="button" :aria-pressed="platform === 'powershell'" @click="platform = 'powershell'">Windows / PowerShell</button><button type="button" :aria-pressed="platform === 'bash'" @click="platform = 'bash'">macOS / Linux</button></nav>
          <textarea :value="command" readonly :aria-label="platform === 'powershell' ? 'PowerShell 安装命令' : 'shell 安装命令'" spellcheck="false" />
          <el-button :disabled="saving || loading" @click="copyCommand"><el-icon aria-hidden="true"><DocumentCopy /></el-icon>{{ copied ? '已复制安装命令' : '复制安装命令' }}</el-button>
          <p v-if="copyError" role="alert">{{ copyError }}</p>
          <ul><li v-for="requirement in publication.installation.requirements" :key="requirement">{{ requirement }}</li></ul>
          <p>{{ publication.installation.host === 'codex' ? 'Codex 插件安装与启用指引：' : '安装完成后，启动 Claude Code，在对话中执行：' }}</p>
          <code class="usage-command">{{ publication.installation.usage_command }}</code>
          <ul><li v-for="note in publication.installation.configuration_notes" :key="note">{{ note }}</li></ul>
          <p>安装命令会校验下载内容并准备插件运行依赖。安装完成后，由管理员提供此场景的专用凭据；凭据通过运行环境配置，不写入安装命令。</p>
        </div>
        <el-button class="withdraw-button" :loading="saving" :disabled="loading || Boolean(error)" @click="update('withdraw', false)">撤回安装发布</el-button>
      </template>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { DocumentCopy, Refresh } from '@element-plus/icons-vue'
import { usePluginPublication } from '@/composables/usePluginPublication'
import type { PluginArtifact } from '@/types/pluginArtifact'
const props = defineProps<{ artifact: PluginArtifact }>()
const { publication, loading, saving, error, load, update } = usePluginPublication(toRef(props, 'artifact'))
const confirmed = ref(false)
const platform = ref<'powershell' | 'bash'>('powershell')
const copied = ref(false)
const copyError = ref('')
const labels = { unpublished: '尚未发布安装来源', published: '已发布', withdrawn: '已撤回发布' }
const command = computed(() => { const item = publication.value?.installation; return item ? platform.value === 'powershell' ? item.powershell_command : item.bash_command : '' })
watch(() => props.artifact.id, () => { confirmed.value = false; copied.value = false; copyError.value = '' })
watch(command, () => { copied.value = false; copyError.value = '' })
async function publish() { if (await update('publish', confirmed.value)) confirmed.value = false }
async function copyCommand() {
  if (!command.value) return
  try { await navigator.clipboard.writeText(command.value); copied.value = true; copyError.value = '' }
  catch { copied.value = false; copyError.value = '浏览器未允许复制，请选中上方命令手动复制。' }
}
</script>
<style scoped>
.publication-panel { border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); margin: 24px 0; padding: 18px 0; }
header { display: flex; align-items: center; justify-content: space-between; }
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; }
p, li { font-size: 12px; line-height: 1.8; color: var(--text-2); }
.publication-status { color: var(--text); }
.publication-status span { color: var(--text-2); font-size: 11px; }
.el-checkbox { height: auto; margin: 10px 0 16px; align-items: flex-start; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
.publish-button { display: flex; margin-top: 4px; min-height: 40px; }
.installation { margin-top: 20px; }
nav { display: flex; flex-wrap: wrap; border-bottom: 1px solid var(--border); margin-top: 18px; }
nav button { background: transparent; color: var(--text-2); padding: 10px; font: inherit; font-size: 12px; border: 0; border-bottom: 2px solid transparent; cursor: pointer; }
nav button[aria-pressed='true'] { color: var(--text); border-color: var(--primary); }
textarea { display: block; width: 100%; min-height: 142px; resize: vertical; margin: 12px 0; padding: 12px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; font: 12px/1.8 Consolas, monospace; }
.withdraw-button { margin-top: 14px; }
.local-note { color: var(--warning); }
.usage-command { display: block; padding: 10px 12px; background: var(--surface-2); color: var(--text); overflow-wrap: anywhere; }
ul { padding-left: 20px; }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
</style>
