<template>
  <section class="distill-publication" aria-label="已保存到资料库的阶段资料">
    <h3>资料库交付记录</h3><p v-if="!publications.length" class="distill-hint">确认并保存后，流程图、ER 图、血缘图及阶段说明会登记到资料库。</p>
    <article v-for="version in publications" :key="version.id" class="distill-item"><div class="distill-section-head"><strong>阶段资料 · 版本 {{ version.project_revision }}</strong><el-button @click="$emit('open', version)">查看资料库</el-button></div><el-button v-for="artifact in version.artifacts" :key="artifact.key" :disabled="busy" @click="$emit('download', version, artifact)">下载 {{ artifact.filename }}</el-button></article>
  </section>
</template>
<script setup lang="ts">
import type { DistillationArtifact, DistillationPublication } from '@/types/businessDistillation'
defineProps<{ publications: DistillationPublication[]; busy: boolean }>()
defineEmits<{ open: [publication: DistillationPublication]; download: [publication: DistillationPublication, artifact: DistillationArtifact] }>()
</script>
