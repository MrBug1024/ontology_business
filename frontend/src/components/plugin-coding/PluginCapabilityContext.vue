<template>
  <section class="capability-context" aria-label="AI 可读取的场景能力">
    <div class="context-heading"><b>本插件选中能力</b><span>{{ capabilities.length }} 项 · 固定发布的输入、输出与执行约束</span></div>
    <p v-if="!capabilities.length">尚未选择要封装的能力。</p>
    <details v-for="capability in capabilities" :key="`${capability.kind}:${capability.key}`">
      <summary><span class="kind-label">{{ kindLabels[capability.kind] }}</span>{{ capability.name }}<small>{{ capability.readiness.ready ? '可调用' : '有待解决的条件' }}</small></summary>
      <p>{{ capability.description || '此能力未提供业务描述。' }}</p>
      <p>{{ capability.requires_confirmation ? '执行前需要人工确认' : '无需副作用确认' }} · {{ capability.idempotency_required ? '调用需要幂等键' : '按当前请求执行' }}</p>
      <p v-for="issue in capability.readiness.issues" :key="issue.code" class="issue">{{ issue.message }}</p>
      <div v-for="direction in directions" :key="direction.key" class="schema-fields"><b>{{ direction.label }}</b><ul><li v-for="field in fields(capability[direction.key])" :key="field.name"><code>{{ field.name }}</code><span>{{ field.type }}{{ field.required ? ' · 必填' : '' }}</span><p v-if="field.description">{{ field.description }}</p></li></ul><p v-if="!fields(capability[direction.key]).length">{{ direction.key === 'input_schema' ? '未列出结构字段，请查看完整契约；不能据此推断无需输入。' : '此版本未声明输出字段，AI 无法提前确定业务结果结构；插件需保留原始结果，并在验证中心核对。' }}</p><details><summary>查看完整{{ direction.label }}契约</summary><pre>{{ JSON.stringify(capability[direction.key], null, 2) }}</pre></details></div>
    </details>
  </section>
</template>
<script setup lang="ts">
import type { CodingCapability } from '@/types/pluginCoding'
defineProps<{ capabilities: CodingCapability[] }>()
const kindLabels: Record<CodingCapability['kind'], string> = { function: '函数', action: '业务操作', rule: '规则', workflow: '工作流' }
const directions = [{ key: 'input_schema', label: '输入' }, { key: 'output_schema', label: '输出' }] as const
function fields(schema: Record<string, unknown>) {
  const properties = schema.properties
  const required = Array.isArray(schema.required) ? schema.required : []
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return []
  return Object.entries(properties).map(([name, value]) => {
    const field = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
    return { name, type: typeof field.type === 'string' ? field.type : '复合类型', description: typeof field.description === 'string' ? field.description : '', required: required.includes(name) }
  })
}
</script>
<style scoped>
.capability-context { padding: 16px 20px; overflow: auto; color: var(--text); }
.context-heading { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; font-size: 13px; }
.context-heading span, small { color: var(--text-2); font-size: 11px; }
details { border-top: 1px solid var(--border); padding: 12px 0; font-size: 13px; }
summary { cursor: pointer; line-height: 1.8; overflow-wrap: anywhere; }
summary small { float: right; margin-left: 10px; }
.kind-label { display: inline-block; margin-right: 8px; color: var(--text-2); font-size: 11px; }
p { color: var(--text-2); font-size: 12px; line-height: 1.8; }
.issue { color: var(--warning); }
.schema-fields { margin-top: 18px; }
ul { list-style: none; padding: 0; }
li { padding: 8px 0; border-bottom: 1px solid var(--border); }
li span { margin-left: 14px; color: var(--text-2); font-size: 11px; }
pre { font-size: 11px; overflow: auto; max-height: 220px; background: var(--surface-2); padding: 12px; }
summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
</style>
