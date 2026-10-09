<template>
  <section class="validation-target" aria-label="本次验证目标">
    <label for="agent-validation-release">本次验证使用</label>
    <el-select id="agent-validation-release" :model-value="modelValue" :disabled="disabled || loading" :loading="loading" placeholder="当前定义（用于建设验证）" aria-label="本次验证使用" @change="emit('update:modelValue', $event)">
      <el-option label="当前定义（用于建设验证）" value="" />
      <el-option v-if="modelValue && !releases.some(item => item.id === modelValue)" label="所选发布（服务端校验）" :value="modelValue" disabled />
      <el-option v-for="release in releases" :key="release.id" :label="release.name + (release.enabled && release.status === 'released' ? '' : '（不可调用）')" :value="release.id" :disabled="!release.enabled || release.status !== 'released'" />
    </el-select>
    <p>构建插件前，请选择该发布，分别运行并核对成功、边界和失败处理案例。历史回执仍属于原调用版本。</p>
    <p v-if="error" class="target-error" role="alert">{{ error }}</p>
    <div class="target-controls">
      <el-button size="small" :disabled="loading || disabled" @click="load">刷新发布</el-button>
      <el-button v-if="offset" size="small" :disabled="loading || disabled" @click="page(-50)">上一页发布</el-button>
      <el-button v-if="hasMore" size="small" :disabled="loading || disabled" @click="page(50)">下一页发布</el-button>
      <span v-if="!loading && !error && !releases.length">暂无可选择的发布</span>
    </div>
  </section>
</template>
<script setup lang="ts">
import { toRef } from 'vue'
import { useScenarioReleases } from '@/composables/useScenarioReleases'
const props = defineProps<{ scenarioId: string; modelValue: string; disabled: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const { releases, loading, error, offset, hasMore, load } = useScenarioReleases(toRef(props, 'scenarioId'))
function page(delta: number) { offset.value = Math.max(0, offset.value + delta); void load() }
</script>
<style scoped>
.validation-target { padding: 12px 16px; border-bottom: 1px solid var(--border); flex: 0 0 auto; }
.validation-target label { display: block; margin-bottom: 6px; font-weight: 600; }
.validation-target .el-select { width: min(460px, 100%); }
.validation-target p { margin: 8px 0; font-size: 12px; color: var(--text-secondary); overflow-wrap: anywhere; }
.validation-target .target-error { color: var(--el-color-danger); }
.target-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.target-controls .el-button { margin-left: 0; }
</style>
