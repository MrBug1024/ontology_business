<template>
  <section aria-labelledby="purpose-title">
    <h2 id="purpose-title">先定义值得解决的问题</h2>
    <p class="distill-hint">需求方、付款方与真正受益的人可能不同。用业务结果检验建设价值，并保留应当拒绝的需求。</p>
    <div class="distill-fields">
      <el-form-item label="真正服务谁"><el-input v-model="document.beneficiary" type="textarea" :rows="2" maxlength="4000" placeholder="谁承受问题，谁受益，谁对结果负责？" /></el-form-item>
      <el-form-item label="核心痛点与根本矛盾"><el-input v-model="document.pain" type="textarea" :rows="3" maxlength="4000" placeholder="现在发生了什么损失？为何现有办法没有解决？" /></el-form-item>
      <el-form-item label="最终业务结果"><el-input v-model="document.desired_outcome" type="textarea" :rows="3" maxlength="4000" placeholder="走完整个过程后，当事人的处境发生什么实际改善？" /></el-form-item>
      <el-form-item label="可核验的成功标准"><el-input v-model="document.success_metric" type="textarea" :rows="3" maxlength="4000" placeholder="用什么证据判断问题已解决？由谁核验、何时核验？" /></el-form-item>
      <el-form-item label="本次建设范围"><el-input v-model="document.scope" type="textarea" :rows="3" maxlength="4000" placeholder="需要哪些能力、人工职责和外部协作才能闭环？" /></el-form-item>
      <el-form-item label="不做什么 / 应拒绝的需求"><el-input v-model="document.non_goals" type="textarea" :rows="3" maxlength="4000" placeholder="哪些要求与核心结果无关、造成负担或缺少合理依据？" /></el-form-item>
    </div>
  </section>
</template>
<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
const document = defineModel<DistillationDocument>({ required: true })
</script>
