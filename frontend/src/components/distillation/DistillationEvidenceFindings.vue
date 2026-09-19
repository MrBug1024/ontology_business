<template>
  <div class="discovery-evidence-findings">
    <h3 v-if="document.evidence.length">资料与观察</h3>
    <article v-for="item in document.evidence" :key="item.key" class="discovery-finding-card">
      <header><strong>{{ item.title }}</strong><span class="discovery-finding-label">{{ evidenceRoles[item.role] }}</span></header>
      <p v-if="item.summary">{{ item.summary }}</p>
      <dl class="discovery-value-list"><div v-if="item.coverage"><dt>覆盖范围</dt><dd>{{ item.coverage }}</dd></div><div v-if="item.limitations"><dt>依据的限制</dt><dd>{{ item.limitations }}</dd></div></dl>
      <small>{{ evidenceKinds[item.kind] }}<span v-if="item.investigation_source"> · 有系统调查记录</span><span v-if="item.library_read"> · 有资料查阅记录</span><span v-if="item.mcp_read"> · 有 MCP 资料查阅记录</span><span v-if="item.interview"> · 可追溯到专家原始对话，尚未独立核实</span></small>
    </article>
    <p v-if="!document.evidence.length" class="discovery-empty-note">暂无调查证据</p>
    <h3 v-if="document.assertions.length">事实、推断与待验证判断</h3>
    <article v-for="item in document.assertions" :key="item.key" class="discovery-assertion discovery-finding-card">
      <span :data-status="item.status">{{ ASSERTION_LABELS[item.status] }}</span><p>{{ item.statement }}</p>
      <small v-if="item.evidence_refs.length">依据：{{ evidenceNames(item.evidence_refs) }}</small>
      <small v-else>尚未关联依据，请在对话中继续查证。</small>
    </article>
  </div>
</template>
<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
import { ASSERTION_LABELS } from '@/utils/businessDistillation'
const props = defineProps<{ document: DistillationDocument }>()
const evidenceRoles = { input: '业务输入', knowledge: '业务知识', result: '业务结果', process: '业务过程', reference: '参考资料' }
const evidenceKinds = { material: '资料库材料', observation: '业务观察', system_export: '系统导出' }
function evidenceNames(keys: string[]) { return keys.map(key => props.document.evidence.find(item => item.key === key)?.title || '待核实依据').join('、') }
</script>
