<template>
  <div class="composer-dock">
    <form class="coding-composer" @submit.prevent="send">
      <label class="sr-only" :for="inputId">{{ label }}</label>
      <textarea ref="inputElement" :id="inputId" :value="modelValue" :aria-label="label" :placeholder="placeholder" maxlength="4000" :disabled="busy" rows="3" @input="updateInput" @keydown="keydown" />
      <div v-if="allowDiscussion" class="mode-switch" role="group" aria-label="编码 AI 交互模式"><button type="button" :aria-pressed="mode === 'generate'" :disabled="busy" @click="emit('update:mode', 'generate')">编码</button><button type="button" :aria-pressed="mode === 'discuss'" :disabled="busy" @click="emit('update:mode', 'discuss')">讨论</button></div>
      <footer>
        <button ref="settingsTrigger" class="settings-button" type="button" aria-label="编码 AI 设置：模型、技能与 MCP" :disabled="busy" @click="emit('settings', settingsTrigger)"><el-icon aria-hidden="true"><Setting /></el-icon><span>{{ modelLabel || '配置编码 AI' }}</span><el-icon aria-hidden="true"><ArrowDown /></el-icon></button>
        <div class="send-actions"><button v-if="generating" class="stop-button" type="button" :disabled="busy" @click="emit('stop')"><el-icon aria-hidden="true"><VideoPause /></el-icon><span>停止</span></button><button class="send-button" type="submit" :disabled="!canSend || busy || !modelValue.trim()" :aria-label="mode === 'discuss' ? '发送插件讨论' : generating ? '应用修正并开始新轮次' : '发送插件编码目标'"><el-icon aria-hidden="true"><Top /></el-icon></button></div>
      </footer>
    </form>
    <div class="composer-meta"><span>{{ skillCount }} 个技能 · {{ mcpCount }} 个 MCP</span><span>Enter 发送 · Shift + Enter 换行</span></div>
    <p v-if="hint" class="composer-hint" role="status">{{ hint }}</p>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { ArrowDown, Setting, Top, VideoPause } from '@element-plus/icons-vue'

const props = withDefaults(defineProps<{
  modelValue: string
  inputId: string
  label: string
  placeholder: string
  modelLabel?: string
  skillCount?: number
  mcpCount?: number
  busy?: boolean
  generating?: boolean
  canSend: boolean
  hint?: string
  allowDiscussion?: boolean
  mode?: 'generate' | 'discuss'
}>(), { modelLabel: '', skillCount: 0, mcpCount: 0, busy: false, generating: false, hint: '', allowDiscussion: false, mode: 'generate' })
const emit = defineEmits<{ 'update:modelValue': [value: string]; 'update:mode': [value: 'generate' | 'discuss']; submit: []; stop: []; settings: [opener: HTMLButtonElement | null] }>()
const inputElement = ref<HTMLTextAreaElement | null>(null)
const settingsTrigger = ref<HTMLButtonElement | null>(null)
function updateInput(event: Event) { const target = event.target as HTMLTextAreaElement | null; if (target) emit('update:modelValue', target.value) }
function send() { if (props.canSend && !props.busy && props.modelValue.trim()) emit('submit') }
function keydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229 || (event.shiftKey && !event.ctrlKey && !event.metaKey)) return
  event.preventDefault()
  send()
}
defineExpose({ focus: () => inputElement.value?.focus() })
</script>
<style scoped>
.composer-dock { flex: 0 0 auto; padding: 12px 14px max(12px, env(safe-area-inset-bottom)); background: var(--surface-2); }
.coding-composer { border: 1px solid var(--border-strong); border-radius: 12px; background: var(--surface); }
.coding-composer:focus-within { border-color: var(--primary); box-shadow: 0 0 0 1px var(--primary); }
textarea { display: block; width: 100%; min-height: 88px; max-height: 180px; resize: vertical; padding: 12px; border: 0; outline: 0; color: var(--text); background: transparent; font: inherit; font-size: 13px; line-height: 1.7; }
footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 8px 8px; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; border: 0; border-radius: 8px; font: inherit; cursor: pointer; }
.settings-button { min-width: 0; max-width: 65%; padding: 0 8px; color: var(--text-2); background: transparent; font-size: 12px; }
.settings-button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.settings-button .el-icon { flex-shrink: 0; }
.settings-button:hover, .stop-button:hover { background: var(--surface-3); color: var(--text); }
.send-actions { display: flex; flex-shrink: 0; gap: 4px; align-items: center; }
.mode-switch { display: flex; gap: 4px; padding: 0 12px; }
.mode-switch button { padding: 0 10px; background: transparent; color: var(--text-2); font-size: 12px; }
.mode-switch button[aria-pressed='true'] { color: var(--primary); background: var(--primary-soft); }
.send-button { width: 44px; color: var(--surface); background: var(--primary); font-size: 20px; }
.stop-button { padding: 0 8px; color: var(--text); background: transparent; font-size: 12px; }
button:disabled { opacity: .4; cursor: not-allowed; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.composer-meta { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px 8px; margin-top: 8px; color: var(--text-2); font-size: 11px; line-height: 1.6; }
.composer-hint { margin: 6px 0 0; color: var(--text-2); font-size: 11px; line-height: 1.65; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 600px) { .composer-dock { padding-inline: 12px; } }
</style>
