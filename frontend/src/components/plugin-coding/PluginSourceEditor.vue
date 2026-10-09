<template>
  <div class="code-editor" :class="{ readonly }">
    <div class="line-gutter" aria-hidden="true"><div :style="{ transform: `translateY(-${scrollTop}px)` }"><span v-for="(_, index) in lines" :key="index">{{ index + 1 }}</span></div></div>
    <div class="code-surface">
      <div class="syntax-viewport" aria-hidden="true"><pre :style="{ transform: `translate(-${scrollLeft}px, -${scrollTop}px)` }"><span v-for="(line, index) in lines" :key="index" class="source-line"><span v-for="(token, tokenIndex) in line" :key="tokenIndex" :class="`token-${token.kind}`">{{ token.text }}</span>{{ '\n' }}</span></pre></div>
      <textarea ref="input" :value="modelValue" :aria-label="`${readonly ? '只读' : '编辑'} ${path}`" :readonly="readonly" :spellcheck="false" :maxlength="32768" wrap="off" autocomplete="off" autocapitalize="off" @input="update" @scroll="trackScroll" @keydown="keyDown" @click="reportCursor" @keyup="reportCursor" />
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { indentSource, sourceTokens } from '@/utils/pluginCodeEditor'
const props = defineProps<{ modelValue: string; path: string; readonly: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; save: []; cursor: [line: number, column: number] }>()
const input = ref<HTMLTextAreaElement | null>(null)
const scrollTop = ref(0)
const scrollLeft = ref(0)
let tabMovesFocus = false
const lines = computed(() => sourceTokens(props.modelValue, props.path))
function trackScroll() { if (input.value) { scrollTop.value = input.value.scrollTop; scrollLeft.value = input.value.scrollLeft } }
function update(event: Event) { if (event.target instanceof HTMLTextAreaElement) { emit('update:modelValue', event.target.value); reportCursor() } }
function reportCursor() { const editor = input.value; if (!editor) return; const before = editor.value.slice(0, editor.selectionStart); const parts = before.split('\n'); emit('cursor', parts.length, (parts[parts.length - 1]?.length || 0) + 1) }
async function keyDown(event: KeyboardEvent) {
  if (event.key === 'Escape') { tabMovesFocus = true; return }
  if (event.key === 'Tab' && tabMovesFocus) { tabMovesFocus = false; return }
  if (event.key !== 'Tab') tabMovesFocus = false
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!props.readonly) emit('save'); return }
  if (event.key !== 'Tab' || props.readonly || event.ctrlKey || event.metaKey || event.altKey) return
  const editor = input.value
  if (!editor) return
  event.preventDefault()
  const changed = indentSource(props.modelValue, editor.selectionStart, editor.selectionEnd, event.shiftKey, props.path.endsWith('.py') ? 4 : 2)
  if (changed.value.length > 32768) return
  emit('update:modelValue', changed.value)
  await nextTick()
  editor.setSelectionRange(changed.start, changed.end)
  reportCursor()
}
watch(() => props.path, async () => { scrollTop.value = 0; scrollLeft.value = 0; await nextTick(); const editor = input.value; if (editor) { editor.scrollTop = 0; editor.scrollLeft = 0; editor.setSelectionRange(0, 0) }; reportCursor() })
</script>
<style scoped>
.code-editor { display: flex; flex: 1; min-height: 0; min-width: 0; background: var(--surface); font: 13px/24px ui-monospace, Consolas, 'Courier New', monospace; }
.line-gutter { width: 54px; flex: 0 0 54px; overflow: hidden; padding-top: 14px; color: var(--text-2); background: var(--surface-2); user-select: none; }
.line-gutter span { display: block; height: 24px; text-align: right; padding-right: 12px; }
.code-surface { position: relative; min-width: 0; flex: 1; overflow: hidden; }
.syntax-viewport { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
pre { margin: 0; padding: 14px 16px 40px; min-width: max-content; width: 100%; font: inherit; color: var(--text); white-space: pre; }
.source-line { height: 24px; }
textarea { position: absolute; inset: 0; display: block; width: 100%; height: 100%; border: 0; border-radius: 0; resize: none; padding: 14px 16px 40px; margin: 0; font: inherit; color: transparent; -webkit-text-fill-color: transparent; caret-color: var(--text); background: transparent; white-space: pre; overflow: auto; tab-size: 4; }
textarea:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
textarea::selection { background: color-mix(in srgb, var(--primary) 25%, transparent); }
.token-keyword { color: var(--primary); }
.token-string { color: var(--success); }
.token-number { color: var(--warning); }
.token-comment { color: var(--text-2); }
.token-heading { color: var(--primary); font-weight: 600; }
@media (max-width: 600px) { .line-gutter { width: 42px; flex-basis: 42px; } }
</style>
