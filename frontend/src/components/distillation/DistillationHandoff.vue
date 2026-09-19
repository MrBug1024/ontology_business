<template>
  <section>
    <h2>判断是否值得建设，再交接</h2>
    <p class="distill-hint">结论可以是继续、调整或暂缓。保存到资料库的成果保留发现过程和未决问题，下一阶段仍需治理、验证及人工正式发布能力。</p>
    <div class="distill-fields">
      <el-form-item label="人工建设决策"><el-select v-model="document.decision"><el-option v-for="(label, value) in DECISION_LABELS" :key="value" :label="label" :value="value" /></el-select></el-form-item>
      <el-form-item label="判断理由与前提"><el-input v-model="document.decision_reason" type="textarea" :rows="3" maxlength="4000" /></el-form-item>
      <el-form-item label="待解决问题（每行一项）"><el-input :model-value="document.open_questions.join('\n')" type="textarea" :rows="4" maxlength="8000" @update:model-value="(value: string) => document.open_questions = linesOf(value)" /></el-form-item>
    </div>
    <div v-if="questions.length" class="distill-review"><h3>交付前的核对提示</h3><ul><li v-for="question in questions" :key="question">{{ question }}</li></ul><p>保存到资料库前，请核对业务价值、证据和人工决策。</p></div>
    <details><summary>预览阶段资料</summary><DistillationSummary :document="document" /></details>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { DistillationDocument } from '@/types/businessDistillation'
import { DECISION_LABELS, linesOf, reviewQuestions } from '@/utils/businessDistillation'
import DistillationSummary from './DistillationSummary.vue'
const document = defineModel<DistillationDocument>({ required: true })
const questions = computed(() => reviewQuestions(document.value))
</script>
