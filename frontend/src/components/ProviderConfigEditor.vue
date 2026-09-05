<template>
  <section class="provider-config-editor" aria-label="Provider 配置">
    <el-form-item
      v-for="[fieldName, descriptor] in descriptorEntries"
      :key="fieldName"
      :label="descriptor.label || fieldName"
      :required="requiredFields.has(fieldName)"
    >
      <el-select
        v-if="descriptor.control === 'semantic_mapping_multiselect'"
        :model-value="arrayValue(fieldName)"
        multiple
        filterable
        :loading="loading"
        :disabled="disabled"
        :placeholder="descriptor.placeholder || '请选择'"
        style="width: 100%"
        @update:model-value="updateField(fieldName, $event)"
      >
        <el-option
          v-for="mapping in activeMappings"
          :key="mapping.id"
          :label="mapping.mapping_key"
          :value="mapping.id"
        />
      </el-select>
      <el-select
        v-else-if="descriptor.control === 'select'"
        :model-value="fieldValue(fieldName)"
        :disabled="disabled"
        :placeholder="descriptor.placeholder || '请选择'"
        style="width: 100%"
        @update:model-value="updateField(fieldName, $event)"
      >
        <el-option
          v-for="option in enumValues(fieldName)"
          :key="String(option)"
          :label="String(option)"
          :value="option"
        />
      </el-select>
      <el-switch
        v-else-if="descriptor.control === 'checkbox'"
        :model-value="Boolean(fieldValue(fieldName))"
        :disabled="disabled"
        @update:model-value="updateField(fieldName, $event)"
      />
      <el-input-number
        v-else-if="descriptor.control === 'number'"
        :model-value="numberValue(fieldName)"
        :disabled="disabled"
        controls-position="right"
        style="width: 100%"
        @update:model-value="updateField(fieldName, $event)"
      />
      <el-input
        v-else
        :model-value="textValue(fieldName)"
        :disabled="disabled || isConstant(fieldName)"
        :placeholder="descriptor.placeholder || ''"
        @update:model-value="updateField(fieldName, $event)"
      />
      <div v-if="descriptor.help" class="form-help">{{ descriptor.help }}</div>
    </el-form-item>
    <el-empty
      v-if="!descriptorEntries.length"
      :image-size="48"
      description="该 Provider 不需要额外配置"
    />
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type {
  FunctionProviderManifest,
  ProviderUiFieldDescriptor,
  SemanticMapping,
} from '@/types'

type JsonObject = Record<string, unknown>

const props = withDefaults(defineProps<{
  manifest: FunctionProviderManifest
  modelValue: JsonObject
  semanticMappings?: SemanticMapping[]
  loading?: boolean
  disabled?: boolean
}>(), {
  semanticMappings: () => [],
  loading: false,
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: JsonObject]
}>()

const descriptorEntries = computed<Array<[string, ProviderUiFieldDescriptor]>>(
  () => Object.entries(props.manifest.ui_schema),
)
const requiredFields = computed(() => {
  const required = (props.manifest.config_schema as JsonObject).required
  return new Set(Array.isArray(required) ? required.filter((item): item is string => typeof item === 'string') : [])
})
const activeMappings = computed(() => props.semanticMappings.filter((item) => item.status === 'active'))

function providerConfig(): JsonObject {
  const value = props.modelValue.provider_config
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function fieldSchema(fieldName: string): JsonObject {
  const properties = (props.manifest.config_schema as JsonObject).properties
  if (properties === null || typeof properties !== 'object' || Array.isArray(properties)) return {}
  const schema = (properties as JsonObject)[fieldName]
  return schema !== null && typeof schema === 'object' && !Array.isArray(schema)
    ? schema as JsonObject
    : {}
}

function fieldValue(fieldName: string): unknown {
  return providerConfig()[fieldName]
}

function arrayValue(fieldName: string): unknown[] {
  const value = fieldValue(fieldName)
  return Array.isArray(value) ? value : []
}

function enumValues(fieldName: string): unknown[] {
  const value = fieldSchema(fieldName).enum
  return Array.isArray(value) ? value : []
}

function numberValue(fieldName: string): number | undefined {
  const value = fieldValue(fieldName)
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function textValue(fieldName: string): string {
  const value = fieldValue(fieldName)
  return value === undefined || value === null ? '' : String(value)
}

function isConstant(fieldName: string): boolean {
  return 'const' in fieldSchema(fieldName)
}

function updateField(fieldName: string, value: unknown) {
  if (props.disabled || isConstant(fieldName)) return
  emit('update:modelValue', {
    ...props.modelValue,
    provider_config: {
      ...providerConfig(),
      [fieldName]: value,
    },
  })
}
</script>

<style scoped>
.provider-config-editor {
  width: 100%;
}
</style>
