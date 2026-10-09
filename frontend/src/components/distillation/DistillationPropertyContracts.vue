<template>
  <section class="property-contracts" aria-label="对象建设要求">
    <el-form-item label="业务身份依据"><el-input v-model="entity.identity" maxlength="4000" placeholder="什么信息可以唯一识别这个业务对象" /></el-form-item>
    <el-checkbox v-model="entity.is_abstract">这是抽象业务概念</el-checkbox>
    <div class="distill-section-head"><h4>属性建设要求</h4><el-button :disabled="!remaining.length" @click="addRequirement">补充属性要求</el-button></div>
    <p class="distill-hint">AI 应根据业务依据补齐这些要求。早期探索可以暂缺，完整交接需覆盖全部必要属性。</p>
    <article v-for="(item, index) in entity.property_contracts" :key="item.attribute" class="contract-item">
      <div class="distill-fields">
        <el-form-item :label="`属性 ${index + 1}`"><el-select v-model="item.attribute"><el-option v-for="attribute in entity.attributes" :key="attribute" :label="attribute" :value="attribute" :disabled="usedByOther(attribute, index)" /></el-select></el-form-item>
        <el-form-item label="业务类型"><el-select v-model="item.data_type"><el-option v-for="option in types" :key="option.value" :label="option.label" :value="option.value" /></el-select></el-form-item>
        <el-form-item label="业务含义"><el-input v-model="item.description" maxlength="2000" /></el-form-item>
        <el-form-item label="是否必填"><el-switch v-model="item.is_required" /></el-form-item>
        <el-form-item label="是否身份属性"><el-switch v-model="item.is_key" /></el-form-item>
        <template v-if="['number', 'float', 'integer'].includes(item.data_type)">
          <el-form-item label="最小值"><el-input-number :model-value="numericConstraint(item, 'minimum')" @update:model-value="(value: number | undefined) => setNumeric(item, 'minimum', value)" /></el-form-item>
          <el-form-item label="最大值"><el-input-number :model-value="numericConstraint(item, 'maximum')" @update:model-value="(value: number | undefined) => setNumeric(item, 'maximum', value)" /></el-form-item>
        </template>
        <el-form-item label="允许取值（每行一项）"><el-input :model-value="item.enum_values.join('\n')" type="textarea" :rows="2" maxlength="8000" @update:model-value="(value: string) => item.enum_values = linesOf(value)" /></el-form-item>
      </div>
      <el-button text type="danger" :aria-label="`移除 ${item.attribute} 的建设要求`" @click="entity.property_contracts?.splice(index, 1)">移除要求</el-button>
    </article>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { DistillationEntity, DistillationPropertyContract } from '@/types/businessDistillation'
import { linesOf } from '@/utils/businessDistillation'
const entity = defineModel<DistillationEntity>({ required: true })
const types: { value: DistillationPropertyContract['data_type']; label: string }[] = [
  { value: 'string', label: '短文本' }, { value: 'text', label: '长文本' },
  { value: 'integer', label: '整数' }, { value: 'number', label: '数值' },
  { value: 'float', label: '浮点数' }, { value: 'boolean', label: '是 / 否' },
  { value: 'date', label: '日期' }, { value: 'datetime', label: '日期时间' }, { value: 'json', label: '结构化内容' },
]
const remaining = computed(() => entity.value.attributes.filter(attribute => !entity.value.property_contracts?.some(item => item.attribute === attribute)))
function usedByOther(attribute: string, index: number) {
  return entity.value.property_contracts?.some((item, position) => position !== index && item.attribute === attribute)
}
function addRequirement() {
  const attribute = remaining.value[0]
  if (!attribute) return
  entity.value.property_contracts = [...(entity.value.property_contracts || []),
    { attribute, data_type: 'string', is_required: false, is_key: false, constraints: {}, enum_values: [] }]
}
function numericConstraint(item: DistillationPropertyContract, field: string): number | undefined {
  const value = item.constraints[field]
  return typeof value === 'number' ? value : undefined
}
function setNumeric(item: DistillationPropertyContract, field: string, value: number | undefined | null) {
  if (value === undefined || value === null) delete item.constraints[field]
  else item.constraints[field] = value
}
</script>
<style scoped>
.property-contracts { margin-top: 12px; }
.contract-item { padding: 12px; margin: 8px 0; border: 1px solid var(--border); border-radius: 8px; }
</style>
