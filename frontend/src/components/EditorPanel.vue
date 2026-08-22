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
        <div class="ep-props">
          <div class="prop-row" v-for="(p, i) in editor.form.properties" :key="i">
            <el-input v-model="p.name" size="small" placeholder="属性名" />
            <el-select v-model="p.data_type" size="small" class="prop-type">
              <el-option v-for="t in DATA_TYPES" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
            <div class="prop-flags">
              <el-checkbox v-model="p.is_key" label="主键" />
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
              <el-checkbox v-model="constraintForms[i].exclusive_minimum" label="不含最小值" />
              <el-checkbox v-model="constraintForms[i].exclusive_maximum" label="不含最大值" />
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
import type { Entity, Property } from '@/types'
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
  saving?: boolean
}>()
const emit = defineEmits<{
  (e: 'save'): void
  (e: 'delete'): void
  (e: 'close'): void
}>()

const constraintForms = ref<Array<Record<string, any>>>([])
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
    name: '', data_type: 'string', is_key: false, is_required: false,
    is_enum: false, enum_values: [], constraints: {}, description: '', is_sensitive: false,
  })
  constraintForms.value.push({})
}

function removeProp(index: number) {
  props.editor.form.properties.splice(index, 1)
  constraintForms.value.splice(index, 1)
}

function numericProperty(type: string) { return ['integer', 'float', 'number'].includes(type) }
function textProperty(type: string) { return ['string', 'text', 'date', 'datetime'].includes(type) }

const statePropertyOptions = computed(() => (
  props.editor.kind === 'entity'
    ? (props.editor.form.properties || []).filter((property: Property) => property.is_enum && property.name && property.enum_values?.length)
    : []
))

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
      if (!Array.isArray(property.enum_values)) property.enum_values = []
      if (!property.constraints || typeof property.constraints !== 'object' || Array.isArray(property.constraints)) property.constraints = {}
      constraintForms.value.push(structuredClone(property.constraints))
    }
    return
  }

  if (props.editor.kind === 'relation') {
    props.editor.form.namespace ??= ''
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
        property.constraints = Object.fromEntries(Object.entries(constraintForms.value[index] || {}).filter(([, value]) => value !== '' && value !== null && value !== undefined && value !== false))
        property.enum_values = property.is_enum
          ? [...new Set((property.enum_values || []).map((value: unknown) => String(value).trim()).filter(Boolean))]
          : []
        if (property.is_enum && !property.enum_values.length) throw new Error(`枚举属性“${property.name || index + 1}”至少需要一个枚举值`)
      }
    } else if (props.editor.kind === 'relation') {
      props.editor.form.namespace = validateNamespace(props.editor.form.namespace)
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
    bottom: 10px;
    width: auto;
    max-height: 70%;
  }
  .validity-row {
    display: block;
  }
}
</style>
