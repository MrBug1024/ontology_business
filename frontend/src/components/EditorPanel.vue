<template>
  <div class="editor-panel" @mousedown.stop @wheel.stop>
    <div class="ep-head">
      <span class="ep-dot" :style="{ background: accent }"></span>
      <div class="ep-title">
        <b>{{ title }}</b>
        <span class="ep-sub">{{ subtitle }}</span>
      </div>
      <el-button class="ep-close" size="small" text @click="emit('close')"><el-icon><Close /></el-icon></el-button>
    </div>

    <div class="ep-body">
      <!-- ══ 实体编辑 ══ -->
      <template v-if="editor.kind === 'entity'">
        <div class="ep-field">
          <label>名称</label>
          <el-input v-model="editor.form.name" placeholder="如：客户、订单" />
        </div>
        <div class="ep-row2">
          <div class="ep-field">
            <label>颜色</label>
            <el-color-picker v-model="editor.form.color" size="small" />
          </div>
          <div class="ep-field">
            <label>抽象实体</label>
            <el-switch v-model="editor.form.is_abstract" />
          </div>
        </div>
        <div class="ep-field">
          <label>描述</label>
          <el-input v-model="editor.form.description" type="textarea" :rows="2" placeholder="实体说明" />
        </div>

        <div class="ep-sec">
          <span>属性（{{ editor.form.properties.length }}）</span>
          <el-button size="small" text type="primary" @click="addProp"><el-icon><Plus /></el-icon> 添加</el-button>
        </div>
        <div class="ep-props">
          <div class="prop-row" v-for="(p, i) in editor.form.properties" :key="i">
            <el-input v-model="p.name" size="small" placeholder="属性名" />
            <el-select v-model="p.data_type" size="small" class="prop-type">
              <el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t" />
            </el-select>
            <div class="prop-flags">
              <el-checkbox v-model="p.is_key" label="主键" />
              <el-checkbox v-model="p.is_required" label="必填" />
              <el-button size="small" text type="danger" @click="editor.form.properties.splice(i, 1)"><el-icon><Delete /></el-icon></el-button>
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
          <label>源实体</label>
          <el-select v-model="editor.form.source_entity_id" style="width:100%">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>目标实体</label>
          <el-select v-model="editor.form.target_entity_id" style="width:100%">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>关系类型</label>
          <el-select v-model="editor.form.relation_type" style="width:100%">
            <el-option v-for="t in REL_TYPES" :key="t" :label="t" :value="t" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>描述</label>
          <el-input v-model="editor.form.description" type="textarea" :rows="2" placeholder="关系说明" />
        </div>
      </template>

      <!-- ══ 实例编辑 ══ -->
      <template v-else-if="editor.kind === 'instance'">
        <div class="ep-field">
          <label>所属实体</label>
          <el-select v-model="editor.form.entity_id" style="width:100%" :disabled="!!editor.id" @change="onEntityChange">
            <el-option v-for="e in entities" :key="e.id" :label="e.name" :value="e.id" />
          </el-select>
        </div>
        <div class="ep-field">
          <label>名称</label>
          <el-input v-model="editor.form.name" placeholder="实例名称，如：张三、订单#1001" />
        </div>
        <div class="ep-sec"><span>属性值</span></div>
        <div class="ep-props">
          <div class="attr-row" v-for="row in attrRows" :key="row.name">
            <div class="attr-name">{{ row.name }} <el-tag v-if="row.key" size="small" type="warning" effect="plain">主键</el-tag></div>
            <el-input v-model="editor.form.attributes[row.name]" size="small" :placeholder="row.type" />
          </div>
          <div class="muted" v-if="!attrRows.length" style="padding:6px 2px">该实体暂无属性</div>
        </div>
      </template>
    </div>

    <div class="ep-foot">
      <el-button v-if="editor.id" size="small" type="danger" plain @click="emit('delete')">
        <el-icon><Delete /></el-icon> 删除
      </el-button>
      <el-button size="small" @click="emit('close')">取消</el-button>
      <el-button size="small" type="primary" :loading="saving" @click="emit('save')">保存</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Entity, Property } from '@/types'

const DATA_TYPES = ['string', 'integer', 'float', 'boolean', 'date', 'datetime', 'json', 'text']
const REL_TYPES = ['1:1', '1:N', 'N:M']

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

const title = computed(() => {
  const map = { entity: '实体', relation: '关系', instance: '实例' }
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
  if (props.editor.kind === 'entity') return props.editor.form.color || '#6366f1'
  if (props.editor.kind === 'instance') {
    const ent = props.entities.find((e) => e.id === props.editor.form.entity_id)
    return ent?.color || '#06b6d4'
  }
  return '#8b5cf6'
})

function addProp() {
  props.editor.form.properties.push({ name: '', data_type: 'string', is_key: false, is_required: false })
}

// 实例属性行
const attrRows = computed(() => {
  const ent = props.entities.find((e) => e.id === props.editor.form.entity_id)
  const attrs = props.editor.form.attributes || {}
  return (ent?.properties || []).map((p: Property) => ({
    name: p.name,
    type: p.data_type,
    key: !!p.is_key,
    value: String(attrs[p.name] ?? ''),
  }))
})
function onEntityChange() {
  props.editor.form.attributes = {}
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
}
</style>
