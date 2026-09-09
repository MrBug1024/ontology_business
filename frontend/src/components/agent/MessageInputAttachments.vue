<template>
  <ul v-if="filenames.length" class="message-attachments" aria-label="消息附件">
    <li v-for="(filename, index) in filenames" :key="index"><el-icon aria-hidden="true"><Document /></el-icon>{{ filename }}</li>
  </ul>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Document } from '@element-plus/icons-vue'
const props = defineProps<{ snapshot?: Record<string, unknown> }>()
const filenames = computed(() => {
  const attachments = props.snapshot?.attachments
  if (!Array.isArray(attachments)) return []
  return attachments.slice(0, 20).flatMap((item: unknown) => item && typeof item === 'object' && 'filename' in item && typeof item.filename === 'string' && item.filename.trim() ? [item.filename.slice(0, 500)] : [])
})
</script>

<style scoped>
.message-attachments { list-style: none; margin: 8px 0 0; padding: 0; }
.message-attachments li { display: flex; align-items: center; gap: 6px; overflow-wrap: anywhere; line-height: 1.6; }
.message-attachments .el-icon { flex: 0 0 16px; }
</style>
