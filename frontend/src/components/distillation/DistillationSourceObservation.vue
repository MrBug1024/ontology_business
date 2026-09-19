<template>
  <details class="discovery-source-observation">
    <summary>查看查证依据</summary>
    <strong>{{ source.title || '网页观察' }}</strong><p>{{ source.url }}</p>
    <p>{{ labels[source.status] }} · {{ observedAt }}</p>
    <ul v-if="source.limitations.length"><li v-for="(limit, index) in source.limitations" :key="index">{{ limit }}</li></ul>
    <div v-if="source.visible_fields.length"><strong>页面可见字段</strong><p>{{ source.visible_fields.join('、') }}</p></div>
    <details v-if="source.text"><summary>阅读页面摘录</summary><pre>{{ source.text }}</pre></details>
  </details>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { DistillationWebsiteObservation } from '@/types/distillationConversation'
const props = defineProps<{ source: DistillationWebsiteObservation }>()
const labels = { observed: '已读取', login_required: '需要登录', redirect_blocked: '重定向已阻止', javascript_required: '需要浏览器渲染' }
const observedAt = computed(() => new Date(props.source.retrieved_at).toLocaleString())
</script>
