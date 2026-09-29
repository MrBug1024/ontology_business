<template>
  <div class="assistant-message-content">
    <template v-for="(part, index) in parts" :key="index">
      <details v-if="part.kind === 'thinking'" class="assistant-thinking" :open="part.streaming && streaming">
        <summary>
          <span>AI 思考过程</span>
          <span v-if="part.streaming" class="thinking-status" role="status">{{ streaming ? '生成中…' : '未完成' }}</span>
        </summary>
        <SafeMarkdown v-if="part.content" :content="part.content" />
      </details>
      <div v-else class="assistant-answer"><SafeMarkdown :content="part.content" /></div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import SafeMarkdown from './SafeMarkdown.vue'
import { splitAssistantMessage } from '@/utils/distillationConversation'

const props = defineProps<{ content: string; streaming?: boolean }>()
const parts = computed(() => splitAssistantMessage(props.content, props.streaming))
</script>

<style scoped>
.assistant-message-content { min-width: 0; overflow-wrap: anywhere; }
.assistant-thinking { margin: 10px 0; padding: 9px 12px; border: 1px dashed var(--border-strong); border-radius: 8px; background: var(--surface-2); color: var(--text-2); font-size: 12px; }
.assistant-thinking summary { cursor: pointer; color: var(--text-2); }
.assistant-thinking summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; border-radius: 2px; }
.assistant-thinking[open] summary { margin-bottom: 6px; }
.thinking-status { margin-left: 10px; color: var(--primary); font-size: 11px; }
</style>
