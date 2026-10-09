<template>
  <section class="construction-summary" aria-label="本体交接完整性">
    <p role="status"><strong>{{ quality.construction_complete ? '本体交接要求已齐备' : '探索成果仍有本体建设缺口' }}</strong> · 完整对象 {{ quality.complete_entity_count }}/{{ quality.entity_count }}</p>
    <p class="distill-hint">此检查覆盖本体对象交接。函数、规则、操作与工作流仍需后续建设和业务验证。</p>
    <details v-if="quality.issues.length">
      <summary>查看待解决事项</summary>
      <ul><li v-for="gap in quality.issues" :key="gap.entity_key"><strong>{{ gap.name }}</strong>：尚缺{{ gap.missing.join('、') }}。<p>{{ gap.next_step }}</p></li></ul>
    </details>
    <p v-if="!quality.construction_complete" class="distill-hint">可以保留探索成果；顾问应继续补齐要求，不能把这些缺口视为已完成建设。</p>
  </section>
</template>
<script setup lang="ts">
import type { DistillationConstructionQuality } from '@/types/businessDistillation'
defineProps<{ quality: DistillationConstructionQuality }>()
</script>
<style scoped>
.construction-summary { padding: 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; line-height: 1.6; }
summary { cursor: pointer; padding: 8px 0; }
li p { margin: 2px 0 8px; color: var(--text-2); }
</style>
