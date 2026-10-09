<template>
  <section class="ide-start" :class="`show-${panel}`" aria-label="插件开发工作区">
    <nav class="mobile-panels" aria-label="开发工作区面板"><button v-for="item in panels" :key="item.key" type="button" :aria-pressed="panel === item.key" @click="panel = item.key">{{ item.label }}</button></nav>
    <aside class="explorer" aria-label="插件资源管理器"><header><el-icon aria-hidden="true"><FolderOpened /></el-icon>资源管理器</header><div class="project-empty"><span>插件项目</span><p>新任务创建后，AI 生成的目录和文件会出现在这里。</p></div><h2>最近的插件项目</h2><nav aria-label="最近插件开发任务"><RouterLink v-for="task in tasks" :key="task.id" :to="{ name: 'plugin-coding-studio', params: { releaseId: task.release_id }, query: { workspace: task.id } }"><el-icon aria-hidden="true"><Folder /></el-icon><span>{{ task.title }}<small>{{ task.host === 'codex' ? 'OpenAI Codex' : 'Claude Code' }} · v{{ task.plugin_version }} · {{ phaseLabel(task.phase) }}</small></span></RouterLink><p v-if="!tasks.length">{{ loading ? '读取项目中…' : '尚无插件项目' }}</p></nav></aside>
    <section class="editor" aria-label="代码编辑器"><header><el-icon aria-hidden="true"><Document /></el-icon>插件代码工作区</header><div class="editor-welcome"><el-icon :size="50" aria-hidden="true"><Box /></el-icon><h1>插件开发</h1><p>打开左侧项目继续编码，或在右侧向 AI 提出新任务。</p><dl><div><dt>场景上下文</dt><dd>选择业务场景与固定能力版本</dd></div><div><dt>AI 编码</dt><dd>生成 Skill、工具脚本与安装说明</dd></div><div><dt>源代码审阅</dt><dd>查看文件、差异、校验结果并修正</dd></div><div><dt>发布安装</dt><dd>定版后进入发布中心，生成安装命令</dd></div></dl></div></section>
    <aside class="assistant" aria-label="AI 编码"><header><el-icon aria-hidden="true"><ChatDotRound /></el-icon>AI 编码 <span>新建任务</span></header><div class="assistant-content"><slot /></div></aside>
  </section>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { Box, ChatDotRound, Document, Folder, FolderOpened } from '@element-plus/icons-vue'
import type { CodingTask } from '@/types/pluginCoding'
defineProps<{ tasks: CodingTask[]; loading: boolean }>()
const panel = ref('assistant')
const panels = [{ key: 'explorer', label: '项目' }, { key: 'editor', label: '代码' }, { key: 'assistant', label: 'AI 编码' }]
function phaseLabel(value: string) { return ({ draft: '草稿', generating: '编码中', ready_for_review: '待审阅', validation_failed: '需修正', released: '已定版' } as Record<string, string>)[value] || '可继续' }
</script>
<style scoped>
.ide-start { display: grid; grid-template-columns: 210px minmax(260px, 1fr) minmax(330px, 360px); flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
aside, .editor { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
header { display: flex; align-items: center; gap: 8px; height: 42px; flex: 0 0 auto; border-bottom: 1px solid var(--border); padding: 0 14px; font-size: 12px; }
header span { margin-left: auto; color: var(--text-2); font-size: 11px; }
.explorer { background: var(--surface-2); border-right: 1px solid var(--border); }
.project-empty { padding: 16px 14px; border-bottom: 1px solid var(--border); }
.project-empty span { font-size: 12px; }
p { font-size: 12px; line-height: 1.8; color: var(--text-2); }
h2 { margin: 20px 14px 8px; font-size: 11px; font-weight: 500; color: var(--text-2); }
nav { min-height: 0; overflow: auto; }
nav a { display: flex; gap: 8px; padding: 12px 14px; color: var(--text); text-decoration: none; font-size: 12px; line-height: 1.6; }
nav a:hover { background: var(--surface-3); }
nav a > .el-icon { flex-shrink: 0; margin-top: 2px; }
nav a > span { min-width: 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
nav small { display: block; color: var(--text-2); margin-top: 6px; font-size: 10px; }
nav > p { padding: 0 14px; }
.editor { background: var(--surface); }
.editor-welcome { margin: auto; max-width: 440px; padding: 28px; color: var(--text-2); }
.editor-welcome > .el-icon { opacity: .45; }
h1 { font-size: 26px; font-weight: 500; color: var(--text); margin: 18px 0 8px; }
dl { margin-top: 32px; font-size: 12px; }
dl > div { padding: 12px 0; border-bottom: 1px solid var(--border); }
dt { color: var(--text); margin-bottom: 6px; }
dd { margin: 0; line-height: 1.6; }
.assistant { border-left: 1px solid var(--border); background: var(--surface); }
.assistant-content { display: flex; flex-direction: column; overflow: hidden; flex: 1; min-height: 0; }
.mobile-panels { display: none; }
a:focus-visible, button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (max-width: 1100px) { .ide-start { grid-template-columns: 180px minmax(220px, 1fr) minmax(300px, 330px); } }
@media (max-width: 800px) {
  .ide-start { display: flex; flex-direction: column; }
  .mobile-panels { display: flex; min-height: 44px; flex: 0 0 auto; border-bottom: 1px solid var(--border); }
  .mobile-panels button { flex: 1; min-height: 44px; background: transparent; border: 0; color: var(--text-2); cursor: pointer; font: inherit; font-size: 12px; }
  .mobile-panels button[aria-pressed='true'] { color: var(--text); border-bottom: 2px solid var(--primary); }
  .ide-start > aside, .ide-start > .editor { display: none; flex: 1; border: 0; }
  .show-explorer > .explorer, .show-editor > .editor, .show-assistant > .assistant { display: flex; }
}
</style>
