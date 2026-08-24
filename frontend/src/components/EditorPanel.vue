<template>
  <div class="editor-panel" @mousedown.stop @wheel.stop>
    <div class="ep-head">
      <span class="ep-dot" :style="{ background: accent }"></span>
      <div class="ep-title">
        <b>{{ title }}</b>
        <span class="ep-sub">{{ subtitle }}</span>
      </div>
      <el-button class="ep-close" size="small" text @click="emit('close')" aria-label="关闭编辑面板" title="关闭编辑面板"><el-icon aria-hidden="true"><Close /></el-icon></el-button>
    </div>

    <div class="ep-body">
      <!-- ══ 实体编辑 ══ -->
      <template v-if="editor.kind === 'entity'">
        <el-alert
          v-if="editor.id && editedEntity"
          class="model-readiness-alert"
          :type="editedEntity.model_ready ? 'success' : 'warning'"
          :title="editedEntity.model_ready ? '对象模型已就绪' : '对象模型尚未就绪'"
          :closable="false"
          show-icon
        >
          <template v-if="!editedEntity.model_ready" #default>
            <ul class="model-issue-list">
              <li v-for="issue in editedEntity.model_issues || []" :key="issue">{{ issue }}</li>
            </ul>
          </template>
        </el-alert>
        <div class="ep-field">
          <label>名称</label>
          <el-input v-model="editor.form.name" placeholder="如：业务对象、资源、事项" />
        </div>
        <div class="ep-field">
          <label>命名空间</label>
          <el-input v-model.trim="editor.form.namespace" placeholder="如：supply.procurement" />
          <small class="field-help">以字母开头，可使用字母、数字、点、横线和下划线；留空继承场景命名空间。</small>
        </div>
        <div class="ep-row2">
          <div class="ep-field">
            <label>颜色</label>
            <el-color-picker v-model="editor.form.color" size="small" />
          </div>
          <div class="ep-field">
            <label>抽象对象类型</label>
            <el-switch v-model="editor.form.is_abstract" />
          </div>
        </div>
        <div class="ep-field">
          <label>描述</label>
          <el-input v-model="editor.form.description" type="textarea" :rows="2" placeholder="对象类型说明" />
        </div>
        <div class="ep-field">
          <label>生命周期状态属性</label>
          <el-select v-model="editor.form.state_property" clearable style="width:100%" placeholder="可选：选择一个枚举属性">
            <el-option v-for="p in statePropertyOptions" :key="p.name" :label="p.name" :value="p.name" />
          </el-select>
          <small class="field-help">只有已启用枚举且至少包含一个枚举值的属性可作为稳定状态。</small>
        </div>

        <div class="ep-sec">
          <span>属性（{{ editor.form.properties.length }}）</span>
          <el-button size="small" text type="primary" @click="addProp"><el-icon><Plus /></el-icon> 添加</el-button>
        </div>
        <div class="identity-selectors" aria-label="对象标识属性">
          <div class="ep-field">
            <label>主键属性</label>
            <el-select v-model="keyPropertyIndex" clearable style="width:100%" placeholder="选择稳定唯一标识">
              <el-option v-for="(property, index) in editor.form.properties" :key="`key-${index}`" :label="propertyOptionLabel(property, index)" :value="index" />
            </el-select>
            <small class="field-help">用于稳定识别和更新同一个业务对象。</small>
          </div>
          <div class="ep-field">
            <label>标题属性</label>
            <el-select v-model="titlePropertyIndex" clearable style="width:100%" placeholder="选择对人可读的名称">
              <el-option v-for="(property, index) in editor.form.properties" :key="`title-${index}`" :label="propertyOptionLabel(property, index)" :value="index" />
            </el-select>
            <small class="field-help">用于图谱展示和 Agent 回答；可与主键属性相同。</small>
          </div>
        </div>
        <el-alert
          v-if="!editor.form.is_abstract && (!hasKeyProperty || !hasTitleProperty)"
          class="draft-readiness-alert"
          type="warning"
          :closable="false"
          show-icon
          title="保存草稿后仍不能发布或用于可靠数据映射"
        >
          <template #default>具体对象类型需要各选择一个主键属性和标题属性；同一属性可以兼任。</template>
        </el-alert>
        <div class="ep-props">
          <div class="prop-row" v-for="(p, i) in editor.form.properties" :key="i">
            <el-input v-model="p.name" size="small" placeholder="属性名" />
            <el-select v-model="p.data_type" size="small" class="prop-type" @change="handlePropertyTypeChange(i)">
              <el-option v-for="t in DATA_TYPES" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
            <div class="prop-flags">
              <el-checkbox v-model="p.is_required" label="必填" />
              <el-checkbox v-model="p.is_enum" label="枚举" />
              <el-checkbox v-model="p.is_sensitive" label="敏感" />
              <el-button size="small" text type="danger" @click="removeProp(i)" :aria-label="`删除属性：${p.name || i + 1}`" title="删除属性"><el-icon aria-hidden="true"><Delete /></el-icon></el-button>
            </div>
            <div v-if="p.is_enum" class="prop-detail">
              <label>枚举值</label>
              <el-select
                v-model="p.enum_values"
                multiple
                filterable
                allow-create
                default-first-option
                style="width:100%"
                size="small"
                placeholder="输入值后按回车，可添加多个"
              >
                <el-option v-for="value in p.enum_values" :key="value" :label="value" :value="value" />
              </el-select>
            </div>
            <div class="prop-detail">
              <label>属性说明</label>
              <el-input v-model="p.description" size="small" placeholder="说明这个属性的业务含义" />
            </div>
            <div v-if="numericProperty(p.data_type)" class="prop-detail constraint-grid">
              <label>数值范围</label>
              <el-input-number v-model="constraintForms[i].minimum" controls-position="right" placeholder="最小值" />
              <el-input-number v-model="constraintForms[i].maximum" controls-position="right" placeholder="最大值" />
              <el-input-number v-model="constraintForms[i].exclusive_minimum" controls-position="right" placeholder="必须大于" />
              <el-input-number v-model="constraintForms[i].exclusive_maximum" controls-position="right" placeholder="必须小于" />
            </div>
            <div v-else-if="textProperty(p.data_type)" class="prop-detail constraint-grid">
              <label>文本约束</label>
              <el-input-number v-model="constraintForms[i].min_length" :min="0" :precision="0" controls-position="right" placeholder="最短长度" />
              <el-input-number v-model="constraintForms[i].max_length" :min="0" :precision="0" controls-position="right" placeholder="最长长度" />
              <el-select v-model="constraintForms[i].format" clearable placeholder="常用格式">
                <el-option label="电子邮箱" value="email" />
                <el-option label="网址" value="uri" />
                <el-option label="UUID" value="uuid" />
                <el-option label="日期" value="date" />
                <el-option label="日期时间" value="date-time" />
              </el-select>
              <el-input v-model="constraintForms[i].pattern" placeholder="匹配规则（可选）" />
            </div>
            <div class="prop-detail fixed-value-field">
              <div class="fixed-value-head">
                <label>固定值（可选）</label>
                <el-switch
                  :model-value="hasFixedValue(i)"
                  inline-prompt
                  active-text="启用"
                  inactive-text="关闭"
                  @change="toggleFixedValue(i, p.data_type, $event)"
                />
              </div>
              <template v-if="hasFixedValue(i)">
                <el-select v-if="p.data_type === 'boolean'" v-model="constraintForms[i].const" style="width:100%">
                  <el-option label="是" :value="true" />
                  <el-option label="否" :value="false" />
                </el-select>
                <el-input-number
                  v-else-if="numericProperty(p.data_type)"
                  v-model="constraintForms[i].const"
                  :precision="p.data_type === 'integer' ? 0 : undefined"
                  controls-position="right"
                  style="width:100%"
                  placeholder="该属性唯一允许的数值"
                />
                <StructuredValueEditor
                  v-else-if="p.data_type === 'json'"
                  v-model="constraintForms[i].const"
                  root
                />
                <el-date-picker
                  v-else-if="p.data_type === 'date' || p.data_type === 'datetime'"
                  v-model="constraintForms[i].const"
                  :type="p.data_type === 'date' ? 'date' : 'datetime'"
                  :value-format="p.data_type === 'date' ? 'YYYY-MM-DD' : 'YYYY-MM-DDTHH:mm:ssZ'"
                  style="width:100%"
                  placeholder="选择固定日期"
                />
                <el-input v-else v-model="constraintForms[i].const" placeholder="该属性唯一允许的值" />
                <small class="field-help">启用后，每个对象实例的该属性都必须等于这里的值。</small>
              </template>
            </div>
          </div>
          <div class="muted" v-if="!editor.form.properties.length" style="padding:6px 2px">暂无属性，点击「添加」</div>
        </div>
      </template>

      <!-- ══ 关系编辑 ══ -->
      <template v-else-if="editor.kind === 'relation'">
        <div class="ep-field">
          <label>关系名</label>
          <el-input v-model="editor.form.name" placeholder="如：下单、属于" />
        </div>
        <div class="ep-field">
          <label>命名空间</label>
          <el-input v-model.trim="editor.form.namespace" placeholder="如：supply.procurement" />
          <small class="field-help">以字母开头，可使用字母、数字、点、横线和下划线；留空继承场景命名空间。</small>
        </div>
        <div class="ep-field">
          <label>源对象类型</label>
          <el-select v-model="editor.form.source_entity_id" style="width:100%">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>目标对象类型</label>
          <el-select v-model="editor.form.target_entity_id" style="width:100%">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>关系基数</label>
          <el-select v-model="editor.form.relation_type" style="width:100%">
            <el-option v-for="t in REL_TYPES" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>描述</label>
          <el-input v-model="editor.form.description" type="textarea" :rows="2" placeholder="关系说明" />
        </div>
        <div class="ep-sec"><span>关系约束（本体公理）</span></div>
        <el-alert class="relation-semantics" type="info" :closable="false" show-icon>
          <template #title>普通关系默认可从两端遍历</template>
          同一条已保存关系可从源对象或目标对象查询，不需要再建一条“反向关系”。对称、传递和显式逆关系只在查询时推理，不会偷偷新增关系实例。
        </el-alert>
        <div class="axiom-grid" aria-label="关系本体公理">
          <div class="axiom-option">
            <span><b>对称</b><small>A→B 时，查询也解释为 B→A</small></span>
            <el-switch v-model="relationConstraints.symmetric" aria-label="对称关系" />
          </div>
          <div class="axiom-option">
            <span><b>传递</b><small>A→B 且 B→C 时，查询可得到 A→C</small></span>
            <el-switch v-model="relationConstraints.transitive" aria-label="传递关系" />
          </div>
          <div class="axiom-option">
            <span><b>反自反</b><small>禁止对象连接自身</small></span>
            <el-switch v-model="relationConstraints.irreflexive" aria-label="反自反关系" />
          </div>
          <div class="axiom-option">
            <span><b>非对称</b><small>A→B 后严格禁止 B→A，也禁止自连接</small></span>
            <el-switch v-model="relationConstraints.asymmetric" aria-label="非对称关系" />
          </div>
          <div class="axiom-option">
            <span><b>反对称</b><small>不同对象间不能同时存在 A→B 与 B→A</small></span>
            <el-switch v-model="relationConstraints.antisymmetric" aria-label="反对称关系" />
          </div>
          <div class="axiom-option">
            <span><b>无环</b><small>禁止新增边形成有向环</small></span>
            <el-switch v-model="relationConstraints.acyclic" aria-label="无环关系" />
          </div>
        </div>
        <div class="ep-field inverse-field">
          <label>显式逆关系（可选）</label>
          <el-select v-model="relationConstraints.inverse_relation_id" clearable filterable style="width:100%" placeholder="仅选择不同命名的反向谓词">
            <el-option v-for="relation in inverseRelationOptions" :key="relation.id" :label="relation.name" :value="relation.id" />
          </el-select>
          <small class="field-help">仅当业务文档明确给出两个不同命名谓词（如“包含”与“属于”或 OWL inverseOf）时配置；普通反向查看无需配置。</small>
        </div>
        <div class="cardinality-card">
          <b>每个源对象可连接的目标对象数</b>
          <div class="cardinality-row">
            <div><label>最小</label><el-input-number v-model="relationConstraints.source_min_cardinality" :min="0" :precision="0" controls-position="right" aria-label="每个源对象连接目标对象的最小数量" /></div>
            <div><label>最大</label><el-input-number v-model="relationConstraints.source_max_cardinality" :min="0" :precision="0" controls-position="right" aria-label="每个源对象连接目标对象的最大数量" /></div>
          </div>
        </div>
        <div class="cardinality-card">
          <b>每个目标对象可被源对象连接数</b>
          <div class="cardinality-row">
            <div><label>最小</label><el-input-number v-model="relationConstraints.target_min_cardinality" :min="0" :precision="0" controls-position="right" aria-label="每个目标对象被源对象连接的最小数量" /></div>
            <div><label>最大</label><el-input-number v-model="relationConstraints.target_max_cardinality" :min="0" :precision="0" controls-position="right" aria-label="每个目标对象被源对象连接的最大数量" /></div>
          </div>
        </div>
        <small class="field-help cardinality-help">最大基数在新建关系实例时硬校验；最小基数在删除已有边时保护，不会自动补边。</small>
      </template>

      <!-- ══ 实例编辑 ══ -->
      <template v-else-if="editor.kind === 'instance'">
        <el-alert
          v-if="runtimeLoading"
          class="runtime-alert"
          type="info"
          title="正在读取对象的状态、有效期与质量信息…"
          :closable="false"
          show-icon
        />
        <el-alert
          v-else-if="runtimeHydrationError"
          class="runtime-alert"
          type="error"
          :title="runtimeHydrationError"
          :closable="false"
          show-icon
        />
        <div class="ep-field">
          <label>所属对象类型</label>
          <el-select v-model="editor.form.entity_id" style="width:100%" :disabled="!!editor.id" @change="onEntityChange">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>名称</label>
          <el-input v-model="editor.form.name" placeholder="实例名称，如：对象A、记录#1001" />
        </div>
        <div class="ep-field">
          <label>对象状态</label>
          <el-select
            v-if="instanceStateOptions.length"
            :model-value="editor.form.state"
            clearable
            style="width:100%"
            placeholder="选择生命周期状态"
            @change="setInstanceState"
          >
            <el-option v-for="value in instanceStateOptions" :key="value" :label="value" :value="value" />
          </el-select>
          <el-input v-else :model-value="editor.form.state" clearable placeholder="可选：当前业务状态" @update:model-value="setInstanceState" />
        </div>
        <div class="ep-row2 validity-row">
          <div class="ep-field">
            <label>有效期开始</label>
            <el-date-picker v-model="editor.form.valid_from" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" clearable style="width:100%" placeholder="可选" />
          </div>
          <div class="ep-field">
            <label>有效期结束</label>
            <el-date-picker v-model="editor.form.valid_to" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" clearable style="width:100%" placeholder="可选" />
          </div>
        </div>
        <div class="ep-field quality-fields">
          <label>数据质量（可选）</label>
          <div class="ep-row2">
            <el-input-number v-model="qualityForm.score" :min="0" :max="1" :step="0.01" :precision="2" controls-position="right" placeholder="质量分数" />
            <el-select v-model="qualityForm.status" clearable placeholder="质量状态">
              <el-option label="未检查" value="unknown" /><el-option label="有效" value="valid" /><el-option label="有提醒" value="warning" /><el-option label="无效" value="invalid" />
            </el-select>
          </div>
          <el-select v-model="qualityForm.issues" multiple filterable allow-create default-first-option placeholder="输入问题后按回车添加" />
          <div class="ep-row2">
            <el-date-picker v-model="qualityForm.checked_at" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" clearable placeholder="检查时间" />
            <el-input v-model="qualityForm.source" placeholder="检查来源" />
          </div>
        </div>
        <div class="ep-sec"><span>属性值</span></div>
        <div class="ep-props">
          <div class="attr-row" v-for="row in attrRows" :key="row.name">
            <div class="attr-name">{{ row.name }} <el-tag v-if="row.key" size="small" type="warning" effect="plain">主键</el-tag></div>
            <el-select v-if="row.enumValues.length" v-model="editor.form.attributes[row.name]" clearable size="small" :placeholder="row.type">
              <el-option v-for="value in row.enumValues" :key="value" :label="value" :value="coerceEnumValue(value, row.type)" />
            </el-select>
            <el-select v-else-if="row.type === 'boolean'" v-model="editor.form.attributes[row.name]" clearable size="small" placeholder="未设置">
              <el-option label="是 / true" :value="true" />
              <el-option label="否 / false" :value="false" />
            </el-select>
            <el-input-number
              v-else-if="row.type === 'integer' || row.type === 'float' || row.type === 'number'"
              v-model="editor.form.attributes[row.name]"
              :precision="row.type === 'integer' ? 0 : undefined"
              :step="row.type === 'integer' ? 1 : 0.1"
              controls-position="right"
              style="width:100%"
              size="small"
            />
            <el-date-picker
              v-else-if="row.type === 'date' || row.type === 'datetime'"
              v-model="editor.form.attributes[row.name]"
              :type="row.type === 'date' ? 'date' : 'datetime'"
              :value-format="row.type === 'date' ? 'YYYY-MM-DD' : 'YYYY-MM-DDTHH:mm:ssZ'"
              clearable
              style="width:100%"
              size="small"
            />
            <StructuredValueEditor
              v-else-if="row.type === 'json'"
              v-model="editor.form.attributes[row.name]"
              root
            />
            <el-input v-else v-model="editor.form.attributes[row.name]" size="small" :placeholder="row.type" />
          </div>
          <div class="muted" v-if="!attrRows.length" style="padding:6px 2px">该对象类型暂无属性</div>
        </div>
      </template>
    </div>

    <div class="ep-foot">
      <el-button v-if="editor.id" size="small" type="danger" plain @click="emit('delete')">
        <el-icon><Delete /></el-icon> 删除
      </el-button>
      <el-button size="small" @click="emit('close')">取消</el-button>
      <el-button size="small" type="primary" :loading="saving || runtimeLoading" :disabled="Boolean(runtimeHydrationError)" @click="prepareAndSave">保存</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { Entity, Property, Relation } from '@/types'
import {
  buildRelationConstraints,
  relationConstraintForm,
  type RelationConstraintForm,
} from '@/utils/relationConstraints'
import StructuredValueEditor from '@/components/StructuredValueEditor.vue'

const DATA_TYPES = [
  { value: 'string', label: '文本' },
  { value: 'integer', label: '整数' },
  { value: 'float', label: '小数' },
  { value: 'number', label: '数值' },
  { value: 'boolean', label: '是 / 否' },
  { value: 'date', label: '日期' },
  { value: 'datetime', label: '日期时间' },
  { value: 'text', label: '长文本' },
  { value: 'json', label: '结构化对象' },
]
const REL_TYPES = [
  { value: '1:1', label: '一对一（1:1）' },
  { value: '1:N', label: '一对多（1:N）' },
  { value: 'N:1', label: '多对一（N:1）' },
  { value: 'N:M', label: '多对多（N:M）' },
]

const props = defineProps<{
  editor: { kind: 'entity' | 'relation' | 'instance'; id?: string; form: any }
  entities: Entity[]
  relations?: Relation[]
  saving?: boolean
}>()
const emit = defineEmits<{
  (e: 'save'): void
  (e: 'delete'): void
  (e: 'close'): void
}>()

const constraintForms = ref<Array<Record<string, any>>>([])
const relationConstraints = ref<RelationConstraintForm>(relationConstraintForm(undefined))
const qualityForm = ref<Record<string, any>>({ score: undefined, status: '', issues: [], checked_at: '', source: '' })
const runtimeLoading = ref(false)
const runtimeHydrationError = ref('')
let hydrationRequest = 0

const title = computed(() => {
  const map = { entity: '对象类型', relation: '关系类型', instance: '对象实例' }
  return (props.editor.id ? '编辑' : '新建') + map[props.editor.kind]
})
const subtitle = computed(() => {
  if (props.editor.kind === 'entity') return props.editor.form.name || ''
  if (props.editor.kind === 'relation') {
    const s = props.entities.find((e) => e.id === props.editor.form.source_entity_id)?.name
    const t = props.entities.find((e) => e.id === props.editor.form.target_entity_id)?.name
    return s && t ? `${s} → ${t}` : ''
  }
  const ent = props.entities.find((e) => e.id === props.editor.form.entity_id)
  return ent?.name || ''
})
const accent = computed(() => {
  if (props.editor.kind === 'entity') return props.editor.form.color || '#27b9b0'
  if (props.editor.kind === 'instance') {
    const ent = props.entities.find((e) => e.id === props.editor.form.entity_id)
    return ent?.color || '#438be5'
  }
  return '#438be5'
})

function addProp() {
  props.editor.form.properties.push({
    name: '', data_type: 'string', is_key: false, is_title: false, is_required: false,
    is_enum: false, enum_values: [], constraints: {}, description: '', is_sensitive: false,
  })
  constraintForms.value.push({})
}

const editedEntity = computed(() => (
  props.editor.kind === 'entity' && props.editor.id
    ? props.entities.find((entity) => entity.id === props.editor.id)
    : undefined
))
const hasKeyProperty = computed(() => (
  props.editor.kind === 'entity' && (props.editor.form.properties || []).filter((property: Property) => property.is_key).length === 1
))
const hasTitleProperty = computed(() => (
  props.editor.kind === 'entity' && (props.editor.form.properties || []).filter((property: Property) => property.is_title).length === 1
))
const keyPropertyIndex = computed<number | undefined>({
  get: () => {
    const indexes = (props.editor.form.properties || []).flatMap((property: Property, index: number) => property.is_key ? [index] : [])
    return indexes.length === 1 ? indexes[0] : undefined
  },
  set: (selected) => {
    const selectedIndex = selected == null ? -1 : Number(selected)
    ;(props.editor.form.properties || []).forEach((property: Property, index: number) => {
      property.is_key = index === selectedIndex
    })
  },
})
const titlePropertyIndex = computed<number | undefined>({
  get: () => {
    const indexes = (props.editor.form.properties || []).flatMap((property: Property, index: number) => property.is_title ? [index] : [])
    return indexes.length === 1 ? indexes[0] : undefined
  },
  set: (selected) => {
    const selectedIndex = selected == null ? -1 : Number(selected)
    ;(props.editor.form.properties || []).forEach((property: Property, index: number) => {
      property.is_title = index === selectedIndex
    })
  },
})
function propertyOptionLabel(property: Property, index: number) {
  return property.name?.trim() || `未命名属性 ${index + 1}`
}

function removeProp(index: number) {
  props.editor.form.properties.splice(index, 1)
  constraintForms.value.splice(index, 1)
}

function numericProperty(type: string) { return ['integer', 'float', 'number'].includes(type) }
function textProperty(type: string) { return ['string', 'text', 'date', 'datetime'].includes(type) }

function hasFixedValue(index: number) {
  return Object.prototype.hasOwnProperty.call(constraintForms.value[index] || {}, 'const')
}

function defaultFixedValue(type: string): unknown {
  if (type === 'boolean') return true
  if (type === 'integer' || type === 'float' || type === 'number') return 0
  if (type === 'json') return {}
  return ''
}

function toggleFixedValue(index: number, type: string, enabled: string | number | boolean) {
  constraintForms.value[index] ||= {}
  if (Boolean(enabled)) constraintForms.value[index].const = defaultFixedValue(type)
  else delete constraintForms.value[index].const
}

function handlePropertyTypeChange(index: number) {
  // 每种属性类型支持的约束不同；切换类型时清空旧约束，避免把旧类型的
  // 范围或固定值悄悄带入新类型。
  constraintForms.value[index] = {}
}

const statePropertyOptions = computed(() => (
  props.editor.kind === 'entity'
    ? (props.editor.form.properties || []).filter((property: Property) => property.is_enum && property.name && property.enum_values?.length)
    : []
))

const inverseRelationOptions = computed(() => {
  if (props.editor.kind !== 'relation') return []
  const sourceId = String(props.editor.form.source_entity_id || '')
  const targetId = String(props.editor.form.target_entity_id || '')
  return (props.relations || []).filter((relation) => (
    Boolean(relation.id)
    && relation.source_entity_id === targetId
    && relation.target_entity_id === sourceId
    && (relation.id !== props.editor.id || sourceId === targetId)
  ))
})

const currentEntity = computed(() => props.entities.find((entity) => entity.id === props.editor.form.entity_id))
const instanceStateProperty = computed(() => {
  const entity = currentEntity.value
  return entity?.properties.find((property) => property.name === entity.state_property)
})
const instanceStateOptions = computed(() => instanceStateProperty.value?.enum_values || [])

function initializeDrafts() {
  hydrationRequest += 1
  runtimeLoading.value = false
  runtimeHydrationError.value = ''
  constraintForms.value = []
  qualityForm.value = { score: undefined, status: '', issues: [], checked_at: '', source: '' }

  if (props.editor.kind === 'entity') {
    const original = props.entities.find((entity) => entity.id === props.editor.id)
    if (props.editor.form.namespace === undefined) props.editor.form.namespace = original?.namespace || ''
    if (props.editor.form.state_property === undefined) props.editor.form.state_property = original?.state_property || ''
    for (const property of props.editor.form.properties || []) {
      property.is_key = Boolean(property.is_key)
      property.is_title = Boolean(property.is_title)
      if (!Array.isArray(property.enum_values)) property.enum_values = []
      if (!property.constraints || typeof property.constraints !== 'object' || Array.isArray(property.constraints)) property.constraints = {}
      constraintForms.value.push(structuredClone(property.constraints))
    }
    return
  }

  if (props.editor.kind === 'relation') {
    props.editor.form.namespace ??= ''
    relationConstraints.value = relationConstraintForm(props.editor.form.constraints)
    return
  }

  if (props.editor.kind !== 'instance') return
  props.editor.form.state ??= ''
  props.editor.form.valid_from ??= null
  props.editor.form.valid_to ??= null
  props.editor.form.quality ??= {}
  resetInstanceRuntimeForms()
  if (props.editor.id) void hydrateExistingInstance(++hydrationRequest)
}

function resetInstanceRuntimeForms() {
  const quality = props.editor.form.quality || {}
  qualityForm.value = {
    score: typeof quality.score === 'number' ? quality.score : undefined,
    status: String(quality.status || ''),
    issues: Array.isArray(quality.issues) ? [...quality.issues] : [],
    checked_at: quality.checked_at || '',
    source: String(quality.source || ''),
  }
}

async function hydrateExistingInstance(request: number) {
  const scenarioId = currentEntity.value?.scenario_id
  if (!scenarioId || !props.editor.id) {
    runtimeHydrationError.value = '无法确认对象所属场景；为避免覆盖既有运行时元数据，当前不能保存。'
    return
  }
  runtimeLoading.value = true
  try {
    const object = await api.getObject(scenarioId, props.editor.id)
    if (request !== hydrationRequest || props.editor.kind !== 'instance' || props.editor.id !== object.id) return
    props.editor.form.state = object.state || ''
    props.editor.form.valid_from = object.valid_from || null
    props.editor.form.valid_to = object.valid_to || null
    props.editor.form.quality = object.quality || {}
    props.editor.form.access_scope = object.access_scope || 'tenant'
    props.editor.form.source = object.source || 'manual'
    props.editor.form.source_ref = object.source_ref || ''
    resetInstanceRuntimeForms()
  } catch (error: any) {
    if (request !== hydrationRequest) return
    runtimeHydrationError.value = error?.message || '对象运行时元数据读取失败；为避免覆盖既有值，当前不能保存。'
  } finally {
    if (request === hydrationRequest) runtimeLoading.value = false
  }
}

watch(() => props.editor, initializeDrafts, { immediate: true })

// 实例属性行
const attrRows = computed(() => {
  const ent = props.entities.find((e) => e.id === props.editor.form.entity_id)
  const attrs = props.editor.form.attributes || {}
  return (ent?.properties || []).filter((p: Property) => p.name !== ent?.state_property).map((p: Property) => ({
    name: p.name,
    type: p.data_type,
    key: !!p.is_key,
    enumValues: p.is_enum ? (p.enum_values || []) : [],
    value: String(attrs[p.name] ?? ''),
  }))
})
function onEntityChange() {
  props.editor.form.attributes = {}
  props.editor.form.state = ''
  resetInstanceRuntimeForms()
}

function setInstanceState(value: string) {
  props.editor.form.state = value || ''
  const stateProperty = currentEntity.value?.state_property
  if (!stateProperty) return
  if (value) props.editor.form.attributes[stateProperty] = coerceEnumValue(value, instanceStateProperty.value?.data_type || 'string')
  else delete props.editor.form.attributes[stateProperty]
}

function coerceEnumValue(value: string, dataType: string): string | number | boolean {
  if (dataType === 'integer' || dataType === 'float' || dataType === 'number') {
    const numberValue = Number(value)
    return Number.isFinite(numberValue) ? numberValue : value
  }
  if (dataType === 'boolean') {
    if (value.toLowerCase() === 'true') return true
    if (value.toLowerCase() === 'false') return false
  }
  return value
}

function validateNamespace(value: unknown) {
  const namespace = String(value || '').trim()
  if (namespace.length > 180 || (namespace && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(namespace))) {
    throw new Error('命名空间格式不正确：须以字母开头，最长 180 个字符')
  }
  return namespace
}

function prepareAndSave() {
  if (runtimeLoading.value || runtimeHydrationError.value) return
  try {
    if (props.editor.kind === 'entity') {
      props.editor.form.namespace = validateNamespace(props.editor.form.namespace)
      const stateProperty = String(props.editor.form.state_property || '')
      if (stateProperty && !statePropertyOptions.value.some((property: Property) => property.name === stateProperty)) {
        throw new Error('生命周期状态属性必须是包含枚举值的枚举属性')
      }
      for (const [index, property] of (props.editor.form.properties || []).entries()) {
        property.constraints = Object.fromEntries(Object.entries(constraintForms.value[index] || {}).filter(([key, value]) => value !== '' && value !== null && value !== undefined && (key === 'const' || value !== false)))
        property.enum_values = property.is_enum
          ? [...new Set((property.enum_values || []).map((value: unknown) => String(value).trim()).filter(Boolean))]
          : []
        if (property.is_enum && !property.enum_values.length) throw new Error(`枚举属性“${property.name || index + 1}”至少需要一个枚举值`)
      }
    } else if (props.editor.kind === 'relation') {
      props.editor.form.namespace = validateNamespace(props.editor.form.namespace)
      if (
        relationConstraints.value.inverse_relation_id
        && !inverseRelationOptions.value.some((relation) => relation.id === relationConstraints.value.inverse_relation_id)
      ) {
        throw new Error('所选逆关系的源/目标对象类型必须与当前关系反向对应')
      }
      props.editor.form.constraints = buildRelationConstraints(relationConstraints.value, {
        relationType: props.editor.form.relation_type,
        sourceEntityId: props.editor.form.source_entity_id,
        targetEntityId: props.editor.form.target_entity_id,
      })
    } else if (props.editor.kind === 'instance') {
      props.editor.form.quality = Object.fromEntries(Object.entries(qualityForm.value).filter(([, value]) => value !== '' && value !== null && value !== undefined && (!Array.isArray(value) || value.length)))
      for (const property of currentEntity.value?.properties || []) {
        if (property.data_type !== 'json') continue
        const value = props.editor.form.attributes[property.name]
        if ((value === undefined || value === null) && !property.is_required) {
          delete props.editor.form.attributes[property.name]
          continue
        }
        if (!value || typeof value !== 'object') throw new Error(`属性“${property.name}”必须是结构化对象或列表`)
      }
      if (props.editor.form.valid_from && props.editor.form.valid_to && new Date(props.editor.form.valid_to) <= new Date(props.editor.form.valid_from)) {
        throw new Error('有效期结束时间必须晚于开始时间')
      }
    }
    emit('save')
  } catch (error: any) {
    ElMessage.error(error?.message || '请检查属性与约束配置')
  }
}
</script>

<style scoped>
.editor-panel {
  position: absolute;
  top: 14px;
  right: 14px;
  bottom: 14px;
  width: 340px;
  max-width: calc(100% - 28px);
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 16px;
  box-shadow: var(--shadow-lg);
  z-index: 20;
  overflow: hidden;
  animation: slideIn 0.22s var(--ease);
}
@keyframes slideIn {
  from { opacity: 0; transform: translateX(16px); }
  to { opacity: 1; transform: translateX(0); }
}
.ep-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--grad-soft);
}
.ep-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  flex-shrink: 0;
  box-shadow: 0 0 8px currentColor;
}
.ep-title {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.ep-title b {
  font-size: 14.5px;
  color: var(--text);
}
.ep-sub {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ep-close {
  flex-shrink: 0;
}
.ep-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
}
.ep-field {
  margin-bottom: 12px;
}
.ep-field label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 5px;
}
.field-help {
  display: block;
  margin-top: 5px;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.45;
}
.runtime-alert {
  margin-bottom: 12px;
}
.model-readiness-alert,
.draft-readiness-alert {
  margin-bottom: 12px;
}
.model-issue-list {
  margin: 4px 0 0;
  padding-left: 18px;
  line-height: 1.55;
}
.identity-selectors {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.relation-semantics {
  margin-bottom: 10px;
  line-height: 1.5;
}
.axiom-grid {
  display: grid;
  gap: 7px;
  margin-bottom: 12px;
}
.axiom-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
}
.axiom-option span {
  min-width: 0;
  display: grid;
  gap: 2px;
}
.axiom-option b {
  color: var(--text);
  font-size: 12px;
}
.axiom-option small {
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.4;
}
.inverse-field {
  margin-top: 4px;
}
.cardinality-card {
  display: grid;
  gap: 7px;
  margin-bottom: 8px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.cardinality-card > b {
  color: var(--text-2);
  font-size: 11.5px;
}
.cardinality-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.cardinality-row > div {
  display: grid;
  gap: 4px;
}
.cardinality-row label {
  color: var(--text-3);
  font-size: 10.5px;
}
.cardinality-row :deep(.el-input-number) {
  width: 100%;
}
.cardinality-help {
  margin-bottom: 12px;
}
.mono-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.ep-row2 {
  display: flex;
  gap: 12px;
}
.ep-row2 .ep-field {
  flex: 1;
}
.ep-sec {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12.5px;
  font-weight: 700;
  color: var(--text);
  margin: 14px 0 8px;
}
.ep-props {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.prop-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.prop-row .prop-type {
  width: 100%;
}
.prop-flags {
  display: flex;
  align-items: center;
  gap: 10px;
}
.prop-flags .el-checkbox {
  margin-right: 0;
  height: auto;
}
.prop-flags .el-button {
  margin-left: auto;
}
.prop-detail {
  padding-top: 2px;
}
.prop-detail > label {
  display: block;
  margin-bottom: 4px;
  color: var(--text-3);
  font-size: 11px;
}
.constraint-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.constraint-grid > label { grid-column: 1 / -1; }
.constraint-grid :deep(.el-input-number), .quality-fields :deep(.el-input-number), .quality-fields :deep(.el-date-editor) { width: 100%; }
.fixed-value-field {
  display: grid;
  gap: 6px;
}
.fixed-value-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.fixed-value-head > label {
  color: var(--text-3);
  font-size: 11px;
}
.quality-fields { display: grid; gap: 7px; }
.attr-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.attr-name {
  font-size: 12px;
  color: var(--text-2);
  display: flex;
  align-items: center;
  gap: 6px;
}
.ep-foot {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--surface-2);
}
.ep-foot .el-button:first-child {
  margin-right: auto;
}
@media (max-width: 640px) {
  .editor-panel {
    top: auto;
    left: 10px;
    right: 10px;
    bottom: max(10px, env(safe-area-inset-bottom));
    width: auto;
    max-height: min(72dvh, 640px);
  }
  .validity-row {
    display: block;
  }
  .identity-selectors {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }
}
</style>
