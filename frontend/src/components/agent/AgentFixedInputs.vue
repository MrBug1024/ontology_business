<template>
  <details class="fixed-inputs">
    <summary>固定本次业务输入</summary>
    <el-checkbox :model-value="enabled" :disabled="disabled" @update:modelValue="emit('update:enabled', Boolean($event))">按原值提交输入对象</el-checkbox>
    <template v-if="enabled">
      <p>这里的对象直接提交给服务端。未提供的字段保持缺失，AI 不能代为补齐；业务有效性由服务端校验。</p>
      <label for="agent-fixed-business-inputs">本次输入对象（JSON）</label>
      <el-input id="agent-fixed-business-inputs" :model-value="source" type="textarea" :rows="4" :maxlength="65536" :disabled="disabled" aria-label="本次输入对象" :aria-invalid="Boolean(error)" @update:modelValue="emit('update:source', $event)" />
      <p v-if="error" class="fixed-input-error" role="alert">{{ error }}</p>
    </template>
  </details>
</template>
<script setup lang="ts">
defineProps<{ enabled: boolean; source: string; disabled: boolean; error: string }>()
const emit = defineEmits<{ 'update:enabled': [value: boolean]; 'update:source': [value: string] }>()
</script>
<style scoped>
.fixed-inputs { margin: 10px 0; }
summary { cursor: pointer; padding: 8px 0; font-size: 13px; color: var(--text-2); }
p { margin: 8px 0; font-size: 13px; line-height: 1.6; color: var(--text-2); }
label { display: block; margin: 8px 0; font-size: 13px; }
.fixed-input-error { color: var(--el-color-danger); }
</style>
