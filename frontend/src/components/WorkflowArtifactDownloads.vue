<template>
  <section v-if="files.length" class="workflow-artifacts" aria-label="工作流交付文件">
    <h4>交付文件</h4>
    <a v-for="file in files" :key="file.id" :href="file.url" :download="file.filename">
      <el-icon aria-hidden="true"><Download /></el-icon><span>{{ file.filename }}</span>
    </a>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { workflowArtifacts } from '@/utils/workflowArtifacts'

const props = defineProps<{ result: unknown }>()
const files = computed(() => workflowArtifacts(props.result))
</script>

<style scoped>
.workflow-artifacts { padding: 12px 0; border-top: 1px solid var(--el-border-color); }
.workflow-artifacts h4 { margin: 0 0 8px; font-size: 14px; }
.workflow-artifacts a { display: flex; align-items: center; gap: 8px; min-height: 44px; color: var(--el-color-primary); }
.workflow-artifacts span { overflow-wrap: anywhere; min-width: 0; }
</style>
