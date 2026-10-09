<template>
  <details class="scenario-blueprint" aria-label="场景能力画像与插件交付">
    <summary><b>场景能力画像与插件交付</b><span>{{ selectedCapabilities.length }} 项{{ draftSelection ? '拟封装能力' : '选中能力' }}</span></summary>
    <div class="blueprint-body">
      <p v-if="loading" role="status">正在读取固定版本的场景画像…</p>
      <p v-else-if="!blueprint" class="missing-blueprint" role="status">{{ legacyWorkspace ? '旧编码任务尚未保存画像。下面仅说明各阶段职责；已保存代码和能力契约保持原样。' : '此任务尚未取得场景画像。各阶段职责如下，实际业务范围以读取到的固定版本能力契约为准。' }}</p>
      <template v-if="blueprint">
        <section aria-labelledby="blueprint-goal"><h3 id="blueprint-goal">固定版本的业务目标</h3><b>{{ blueprint.scenario.name }}</b><p>{{ blueprint.scenario.description || '此版本未提供业务目标描述。' }}</p><p class="boundary">来自此插件绑定的正式发布快照；后续草稿和资料变化不会改写这个版本。</p></section>
        <section aria-labelledby="blueprint-ontology"><h3 id="blueprint-ontology">本体语义</h3><p>{{ blueprint.ontology.objects.length }} 类对象 · {{ blueprint.ontology.relations.length }} 类关系</p><p v-if="!blueprint.ontology.objects.length">此版本未定义本体对象。纯计算、规则等能力仍可通过明确的输入输出契约使用。</p>
          <details v-for="object in blueprint.ontology.objects" :key="object.key" class="object-detail"><summary>{{ object.name }}<span>{{ object.properties.length }} 项属性</span></summary><p>{{ object.description || '未提供对象说明。' }}</p><ul><li v-for="property in object.properties" :key="property.key"><b>{{ property.name }}</b><span class="property-type">{{ property.data_type }}{{ property.is_key ? ' · 身份属性' : '' }}{{ property.is_required ? ' · 必填' : '' }}{{ property.is_title ? ' · 显示标题' : '' }}</span><p v-if="property.description">{{ property.description }}</p><p v-if="property.is_enum && property.enum_values.length">可选值：{{ property.enum_values.join('、') }}</p></li></ul></details>
          <ul v-if="blueprint.ontology.relations.length" class="relations"><li v-for="relation in blueprint.ontology.relations" :key="relation.key"><b>{{ relation.name }}</b><p>{{ objectName(relation.source_object_key) }} → {{ objectName(relation.target_object_key) }}<span v-if="relation.cardinality"> · {{ relation.cardinality }}</span></p><p v-if="relation.description">{{ relation.description }}</p></li></ul>
        </section>
        <section aria-labelledby="blueprint-scope">
          <h3 id="blueprint-scope">插件能力范围</h3>
          <p>{{ draftSelection ? '以下选择将在新任务提交时固定，尚未代表已创建插件。' : '以下能力来自这个编码任务保存的选择。' }}</p>
          <p>场景目录可发现 {{ blueprint.coverage.available.length }} 项能力。选中与可发现均不代表执行授权；运行时仍由服务端裁决。</p>
          <ul class="capabilities">
            <li v-for="capability in chosen" :key="`${capability.kind}:${capability.key}`">
              <b>{{ kindLabels[capability.kind] }} · {{ capability.name }}</b>
              <p>{{ capability.description || capability.semantic.role || '未提供业务说明。' }}</p>
              <p>画像记录：{{ capability.invocation_supported ? capability.invocation_authorized ? '有调用权限' : '无调用权限' : '不支持独立调用' }} · {{ capability.enabled ? '已启用' : '未启用' }} · {{ capability.ready ? '运行条件齐备' : '运行条件待解决' }}<span v-if="capability.semantic.requires_approval"> · 流程含人工审批</span></p>
              <p v-if="capability.semantic.object_keys.length">使用对象：{{ capability.semantic.object_keys.map(objectName).join('、') }}</p>
              <p v-if="capability.semantic.dependencies.length">依赖：{{ capability.semantic.dependencies.map(dependencyName).join('、') }}</p>
              <p v-for="binding in capability.semantic.input_bindings" :key="binding.path">输入 <code>{{ binding.path }}</code> 绑定{{ binding.many ? '多个' : '一个' }}{{ objectName(binding.object_key) }}，{{ binding.partial ? '接受部分字段' : '按完整对象契约校验' }}。</p>
              <p v-if="capability.semantic.output_node_keys.length">通过 {{ capability.semantic.output_node_keys.length }} 个流程输出节点返回结果，实际完成状态以服务端回执为准。</p>
            </li>
          </ul>
          <p v-if="!chosen.length">尚未选择要封装的能力。</p>
          <details v-if="dependencies.length" class="dependency-detail">
            <summary>查看选中能力的发布依赖</summary>
            <ul><li v-for="capability in dependencies" :key="`${capability.kind}:${capability.key}`"><b>{{ kindLabels[capability.kind] }} · {{ capability.name }}</b><p>{{ capability.description || capability.semantic.role }}</p></li></ul>
            <p class="boundary">依赖提供流程与语义支持，不会自动成为插件可调用入口。事件是事件语义，不作为插件调用工具。</p>
          </details>
        </section>
      </template>
      <section aria-labelledby="blueprint-stages"><h3 id="blueprint-stages">各建设阶段提供什么</h3><ol><li v-for="stage in stages" :key="stage.key"><b>{{ stage.label }}</b><p>{{ stage.contribution }}</p><p class="boundary">{{ stage.boundary }}</p></li></ol></section>
      <template v-if="profile">
        <section aria-labelledby="blueprint-components">
          <h3 id="blueprint-components">插件交付组成</h3>
          <p>安装目标：{{ profile.host.label }}。这是此宿主的交付范围，不代表其他宿主兼容。</p>
          <p>{{ files ? `当前项目包含 ${files.length} 个文件，可在资源管理器核对。` : '下面列出支持的交付组成；具体文件在创建项目后查看。' }}支持范围不等于这些文件已经生成或执行。</p>
          <ul class="components"><li v-for="component in profile.components" :key="component.key"><b>{{ component.label }}</b><span>{{ component.supported ? component.required ? '必需组成' : '按需定制' : '本平台未支持' }}</span><p>{{ component.purpose }}</p></li></ul>
        </section>
        <section aria-labelledby="blueprint-standards">
          <h3 id="blueprint-standards">遵循的规范与平台规则</h3>
          <p>格式、宿主安装和协议分别约束交付方式；业务权限与完成证据由平台裁决。</p>
          <ul class="standards"><li v-for="standard in profile.standards" :key="standard.key"><a :href="standard.url" target="_blank" rel="noopener noreferrer">{{ standard.label }} · 官方规范<span class="sr-only">（在新窗口打开）</span></a><p>{{ standard.purpose }}</p></li></ul>
          <details><summary>平台交付规则</summary><ul><li v-for="rule in profile.platform_rules" :key="rule.key"><b>{{ rule.label }}</b><p>{{ rule.purpose }}</p></li></ul></details>
          <details><summary>编码扩展与业务运行的边界</summary><ul><li v-for="boundary in profile.boundaries" :key="boundary.key"><b>{{ boundary.label }}</b><p>{{ boundary.purpose }}</p></li></ul></details>
          <details v-if="profile.protected_references.length"><summary>只读交付参考</summary><ul><li v-for="path in profile.protected_references" :key="path"><code>{{ path }}</code><span v-if="files"> · {{ files.some(file => file.path === path) ? '项目中已存在' : '项目中尚无此文件' }}</span></li></ul></details>
        </section>
      </template>
      <p v-else-if="!loading" class="boundary">此任务尚未保存交付规范，请根据已保存代码与实际产物核对支持内容。</p>
    </div>
  </details>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { CodingCapability, CodingFile } from '@/types/pluginCoding'
import type { CodingBlueprintCapability, CodingBlueprintCapabilityKind, CodingDeliveryProfile, CodingScenarioBlueprint } from '@/types/pluginBlueprint'

const props = defineProps<{ blueprint?: CodingScenarioBlueprint | null; profile?: CodingDeliveryProfile | null; selectedCapabilities: CodingCapability[]; files?: CodingFile[]; loading?: boolean; legacyWorkspace?: boolean; draftSelection?: boolean }>()
const kindLabels: Record<CodingBlueprintCapabilityKind, string> = { function: '函数', rule: '规则', action: '业务操作', workflow: '工作流', event: '事件' }
const stageRoles = [
  { key: 'materials', label: '场景资料', contribution: '提供业务事实、术语、流程和约束的理解依据。', boundary: '资料用于建模理解与候选生成，不会自动成为本次调用输入。' },
  { key: 'distillation', label: '业务蒸馏', contribution: '整理目标、角色、任务、决策、流程、验收与待澄清问题，形成建设交接资料。', boundary: '蒸馏成果仍需候选治理与验证；此说明不证明当前发布曾使用某份蒸馏成果。' },
  { key: 'ontology', label: '本体模型', contribution: '定义对象身份、属性和关系，为输入输出和能力绑定提供共同业务语言。', boundary: '对象图本身不执行业务；没有对象图的纯计算也可以建设能力。' },
  { key: 'capabilities', label: '能力建设', contribution: '用函数、规则、业务操作和工作流实现计算、判断、执行与编排，并声明事件语义和运行端口。', boundary: '经治理、确定性验证和人工正式发布后，受支持能力才能通过统一执行入口使用。' },
  { key: 'plugin', label: '插件交付', contribution: '将选定版本的能力组织为宿主可安装的入口、Skill 方法与客户端适配。', boundary: '插件携带本次输入调用固定发布；业务结果与审批状态以服务端真实回执为准。' },
]
const stages = computed(() => props.blueprint?.stages || stageRoles)
const selectedKeys = computed(() => new Set(props.selectedCapabilities.map(item => `${item.kind}:${item.key}`)))
const chosen = computed(() => props.blueprint?.capabilities.filter(item => selectedKeys.value.has(`${item.kind}:${item.key}`)) || [])
const dependencies = computed(() => {
  const refs = new Set(chosen.value.flatMap(item => item.semantic.dependencies.map(ref => `${ref.kind}:${ref.key}`)))
  return props.blueprint?.capabilities.filter(item => refs.has(`${item.kind}:${item.key}`) && !selectedKeys.value.has(`${item.kind}:${item.key}`)) || []
})
function objectName(key: string) { return props.blueprint?.ontology.objects.find(item => item.key === key)?.name || '未提供名称的对象' }
function dependencyName(ref: CodingBlueprintCapability['semantic']['dependencies'][number]) { return props.blueprint?.capabilities.find(item => item.kind === ref.kind && item.key === ref.key)?.name || '未提供名称的依赖' }
</script>
<style scoped>
.scenario-blueprint { min-width: 0; border-top: 1px solid var(--border); color: var(--text); font-size: 12px; }
summary { cursor: pointer; min-height: 44px; padding: 12px 0; line-height: 1.8; overflow-wrap: anywhere; }
summary > span { margin-left: 10px; color: var(--text-2); font-size: 11px; }
summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.blueprint-body { padding-bottom: 12px; }
section + section { border-top: 1px solid var(--border); margin-top: 16px; padding-top: 4px; }
h3 { font-size: 13px; font-weight: 600; margin: 16px 0 10px; }
p { margin: 6px 0; color: var(--text-2); font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
b { font-weight: 600; overflow-wrap: anywhere; }
.boundary { font-size: 11px; }
.missing-blueprint { padding: 10px 12px; background: var(--surface-2); border-radius: 6px; }
ul, ol { padding-left: 18px; margin: 10px 0; }
li { margin: 12px 0; }
.capabilities, .relations, .object-detail ul { padding: 0; list-style: none; }
.capabilities > li, .relations > li, .object-detail li { padding: 10px 0; border-bottom: 1px solid var(--border); }
.components { padding: 0; list-style: none; }
.components > li { padding: 8px 0; border-bottom: 1px solid var(--border); }
.components > li > span { margin-left: 8px; color: var(--text-2); font-size: 11px; }
.standards a { color: var(--primary); text-underline-offset: 3px; }
.standards a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
code { overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.property-type { margin-left: 8px; color: var(--text-2); font-size: 11px; }
.object-detail, .dependency-detail { border-top: 1px solid var(--border); }
@media (max-width: 600px) { summary > span, .components > li > span { display: block; margin-left: 0; } .property-type { display: block; margin-left: 0; } }
</style>
