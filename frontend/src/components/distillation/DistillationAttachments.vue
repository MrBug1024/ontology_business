<template>
  <div v-if="items.length || error" class="discovery-attachments" aria-label="场景临时附件">
    <p v-if="error" class="distill-error" role="alert">{{ error }} <el-button text :disabled="disabled" @click="$emit('reload')">刷新附件状态</el-button></p>
    <article v-for="item in items" :key="item.key" class="discovery-attachment">
      <el-icon aria-hidden="true"><Document /></el-icon><div><strong>{{ item.filename }}</strong><small>{{ formatSize(item.byte_size) }} · {{ stateLabel(item) }}</small><p v-if="item.error" class="distill-error" role="alert">{{ item.error }}</p></div>
      <el-button v-if="item.status === 'failed'" text :disabled="disabled" @click="$emit('retry', item.key)">重试</el-button><el-button text circle :aria-label="`移除附件 ${item.filename}`" :disabled="disabled || ['uploading', 'removing'].includes(item.status)" @click="$emit('remove', item.key)"><el-icon><Close /></el-icon></el-button>
    </article>
    <p v-if="items.length" class="discovery-muted">供当前场景协作分析，24 小时后到期；不会进入资料库。每份最多 10 MB。</p>
  </div>
</template>
<script setup lang="ts">
import { Close, Document } from '@element-plus/icons-vue'
import type { DistillationAttachmentDraft } from '@/types/distillationConversation'
defineProps<{ items: DistillationAttachmentDraft[]; error: string; disabled: boolean }>()
defineEmits<{ retry: [key: string]; remove: [key: string]; reload: [] }>()
function formatSize(bytes: number) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB` }
function stateLabel(item: DistillationAttachmentDraft) { if (item.status === 'uploading') return item.progress >= 99 ? '解析中…' : `上传中 ${item.progress}%`; if (item.status === 'removing') return '移除中…'; if (item.status === 'failed') return '未就绪'; if (item.attachment && item.attachment.status === 'bound') return '已发送，可复用'; return item.attachment && Date.parse(item.attachment.expires_at) <= Date.now() ? '已到期，请重新上传' : '已解析，可发送' }
</script>
