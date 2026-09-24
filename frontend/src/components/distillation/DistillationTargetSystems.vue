<template>
  <section class="target-systems" aria-labelledby="target-systems-title">
    <div class="target-heading">
      <div><h3 id="target-systems-title">目标业务系统</h3><p>告诉 AI 去哪里调查，以及要弄清什么问题。</p></div>
      <el-button :disabled="document.target_systems.length >= 10" @click="addTarget">添加系统</el-button>
    </div>
    <el-empty v-if="!document.target_systems.length" :image-size="72" description="还没有调查系统，可先添加一个明确的页面范围。" />
    <article v-for="(target, index) in document.target_systems" :key="target.key" class="target-card">
      <div class="target-heading">
        <strong>{{ target.name || `调查来源 ${index + 1}` }}</strong>
        <div class="target-actions">
          <el-switch v-model="target.enabled" :aria-label="`允许 AI 读取${target.name || '该系统'}`" />
          <el-button text type="danger" :aria-label="`移除目标系统 ${target.name || index + 1}`" @click="document.target_systems.splice(index, 1)">移除</el-button>
        </div>
      </div>
      <div class="target-fields">
        <el-form-item label="系统名称" required><el-input v-model="target.name" maxlength="200" placeholder="便于辨认的业务系统名称" /></el-form-item>
        <el-form-item label="系统地址" required><el-input v-model="target.base_url" maxlength="2048" placeholder="https://system.example.com" /></el-form-item>
      </div>
      <el-form-item label="调查目的" required><el-input v-model="target.purpose" type="textarea" :rows="2" maxlength="4000" placeholder="例如：弄清请求从受理到结果确认经过哪些角色和环节" /></el-form-item>
      <el-form-item label="允许读取的页面路径" required>
        <el-input :model-value="target.allowed_paths.join('\n')" type="textarea" :rows="3" maxlength="20000" placeholder="每行一个完整路径，例如 / 或 /help/process" @update:model-value="(value: string) => target.allowed_paths = linesOf(value)" />
        <span class="target-note">仅读取列出的页面，最多 20 个；路径不包含查询参数或登录令牌。</span>
      </el-form-item>
      <el-form-item label="业务背景与已知限制"><el-input v-model="target.notes" type="textarea" :rows="2" maxlength="4000" placeholder="页面涉及哪些角色、可能缺少哪些流程；不要填写账号或密码" /></el-form-item>
      <p class="target-access">当前工具可只读查看无需登录的 HTTPS 页面。遇到登录页、动态页面或权限限制时，AI 会报告缺口并向你求证；登录后的业务内容可通过截图、导出文件和过程记录补充。</p>
    </article>
    <p class="target-note">保存来源后，AI 才能在后续对话中调用页面读取工具。开关只控制此项目的调查来源。</p>
  </section>
</template>

<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
import { linesOf } from '@/utils/businessDistillation'
import { createClientRequestId } from '@/utils/clientRequestId'
const document = defineModel<DistillationDocument>({ required: true })
function addTarget() {
  document.value.target_systems.push({
    key: `system_${createClientRequestId().replace(/-/g, '').slice(0, 16)}`,
    name: '', base_url: '', purpose: '', allowed_paths: ['/'], notes: '',
    access_mode: 'anonymous_readonly', enabled: true,
  })
}
</script>

<style scoped>
.target-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.target-heading h3 { margin: 0 0 6px; }
.target-heading p, .target-note, .target-access { color: var(--text-2); font-size: 13px; line-height: 1.7; }
.target-heading p { margin: 0; }
.target-card { margin-top: 16px; border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
.target-actions { display: flex; align-items: center; gap: 12px; }
.target-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }
.target-access { margin-bottom: 0; padding: 12px; background: var(--surface-2); border-radius: 6px; }
@media (max-width: 640px) { .target-fields { grid-template-columns: 1fr; gap: 0; } .target-card { padding: 12px; } }
</style>
