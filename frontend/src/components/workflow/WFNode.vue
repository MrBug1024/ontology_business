<script setup lang="ts">
import { computed, inject } from 'vue'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps<{
  id: string
  type: string
  data: Record<string, any>
  selected?: boolean
}>()

// 由 WorkflowEditor 注入的场景资源（操作/规则/事件），用于显示引用名称
const refs = inject<{ actions: any[]; rules: any[]; events: any[] }>('wfRefs', {
  actions: [],
  rules: [],
  events: [],
})
const refName = computed(() => {
  const d = props.data || {}
  if (props.type === 'action') return refs.actions.find((a) => a.id === d.action_id)?.name || ''
  if (props.type === 'rule') return refs.rules.find((r) => r.id === d.rule_id)?.name || ''
  if (props.type === 'event') return refs.events.find((e) => e.id === d.event_id)?.name || ''
  return ''
})

const META: Record<string, { icon: string; color: string; label: string }> = {
  start: { icon: 'VideoPlay', color: 'var(--success)', label: '开始' },
  end: { icon: 'CircleCheck', color: 'var(--text-3)', label: '结束' },
  action: { icon: 'Operation', color: 'var(--graph-blue)', label: '操作' },
  rule: { icon: 'SetUp', color: 'var(--warning)', label: '规则' },
  llm: { icon: 'Cpu', color: 'var(--primary)', label: '大模型' },
  event: { icon: 'Bell', color: 'var(--accent)', label: '事件' },
  http: { icon: 'Link', color: 'var(--graph-teal)', label: 'HTTP' },
  script: { icon: 'Document', color: 'var(--info)', label: '脚本' },
}
const meta = computed(() => META[props.type] || META.action)

const sub = computed(() => {
  const d = props.data || {}
  switch (props.type) {
    case 'start':
      return '流程入口'
    case 'end':
      return d.summary ? String(d.summary).slice(0, 26) : '流程结束'
    case 'action':
      return refName.value || '未选择操作'
    case 'rule':
      return refName.value || '未选择规则'
    case 'llm':
      return (d.prompt || '未填写提示词').slice(0, 26)
    case 'event':
      return d.ref_name || '未选择事件'
    case 'http':
      return `${d.method || 'GET'} ${d.url || '未填写 URL'}`.slice(0, 30)
    case 'script':
      return 'Python 片段'
    default:
      return ''
  }
})
</script>

<template>
  <div class="wf-node" :class="[`wf-node--${type}`, { 'wf-node--sel': selected }]">
    <Handle v-if="type !== 'start'" type="target" :position="Position.Left" class="wf-h" />
    <div class="wf-node-head">
      <span class="wf-node-ico" aria-hidden="true" :style="{ background: `color-mix(in srgb, ${meta.color} 12%, transparent)`, color: meta.color }"><el-icon :size="15"><component :is="meta.icon" /></el-icon></span>
      <div class="wf-node-tt">
        <b>{{ data.name || meta.label }}</b>
        <small>{{ sub }}</small>
      </div>
    </div>
    <!-- 普通节点：右侧单一出口 -->
    <Handle v-if="type !== 'end' && type !== 'rule'" type="source" :position="Position.Right" class="wf-h" />
    <!-- 规则节点：true（右）/ false（下）双出口 -->
    <template v-if="type === 'rule'">
      <Handle type="source" :position="Position.Right" id="true" class="wf-h wf-h--true" />
      <Handle type="source" :position="Position.Bottom" id="false" class="wf-h wf-h--false" />
    </template>
  </div>
</template>

<style scoped>
.wf-node {
  position: relative;
  min-width: 172px;
  max-width: 236px;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 9px 12px;
  box-shadow: var(--shadow-sm);
  cursor: grab;
}
.wf-node:active {
  cursor: grabbing;
}
.wf-node--sel {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-soft), var(--shadow-md);
}
.wf-node-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-node-ico {
  width: 27px;
  height: 27px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
}
.wf-node-tt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.wf-node-tt b {
  font-size: 12.5px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wf-node-tt small {
  font-size: 10.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wf-h {
  width: 9px !important;
  height: 9px !important;
  background: var(--surface);
  border: 2px solid var(--border-strong);
}
.wf-h--true {
  border-color: var(--success);
}
.wf-h--false {
  border-color: var(--danger);
}
</style>
