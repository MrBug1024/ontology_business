<template>
  <div class="discovery-details">
    <el-tabs v-model="tab">
      <el-tab-pane label="对话与交付" name="project"><el-form label-position="top" :disabled="!canEdit || busy"><el-form-item label="对话名称"><el-input v-model="draft.name" maxlength="200" /></el-form-item></el-form><p class="distill-hint">所属场景：{{ scenarios.find(item => item.id === draft.scenario_id)?.name || (draft.scenario_id ? '当前场景' : '未关联场景的项目') }}。已有对话的场景归属保持固定；切换顶栏场景将开始另一场景的对话。</p><el-button v-if="project && !project.scenario_id && canEdit" :disabled="dirty || busy" @click="$emit('copy')">复制到目标场景</el-button><DistillationPublications :publications="publications" :busy="busy" @open="$emit('open', $event)" @download="(publication, artifact) => $emit('download', publication, artifact)" /><el-button v-if="publications.length" @click="$emit('build')">进入场景能力建设</el-button></el-tab-pane>
      <el-tab-pane label="价值" name="value"><el-form label-position="top" :disabled="!canEdit || busy"><DistillationPurpose v-model="draft.document" /></el-form></el-tab-pane>
      <el-tab-pane label="流程" name="process"><el-form label-position="top" :disabled="!canEdit || busy"><DistillationProcesses v-model="draft.document" /></el-form></el-tab-pane>
      <el-tab-pane label="对象与血缘" name="ontology"><el-form label-position="top" :disabled="!canEdit || busy"><DistillationOntology v-model="draft.document" /></el-form></el-tab-pane>
      <el-tab-pane label="人工决策" name="review"><el-form label-position="top" :disabled="!canEdit || busy"><DistillationHandoff v-model="draft.document" /></el-form></el-tab-pane>
      <el-tab-pane label="证据与来源" name="evidence"><el-form label-position="top" :disabled="!canEdit || busy"><DistillationEvidence v-model="draft.document" :materials="materials" /><DistillationTargetSystems v-model="draft.document" /></el-form></el-tab-pane>
    </el-tabs>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { DataSource, Scenario } from '@/types'
import type { DistillationArtifact, DistillationDraft, DistillationProject, DistillationPublication } from '@/types/businessDistillation'
import DistillationPurpose from './DistillationPurpose.vue'
import DistillationProcesses from './DistillationProcesses.vue'
import DistillationOntology from './DistillationOntology.vue'
import DistillationHandoff from './DistillationHandoff.vue'
import DistillationPublications from './DistillationPublications.vue'
import DistillationEvidence from './DistillationEvidence.vue'
import DistillationTargetSystems from './DistillationTargetSystems.vue'
const draft = defineModel<DistillationDraft>({ required: true })
defineProps<{ project: DistillationProject | null; scenarios: Scenario[]; materials: DataSource[]; publications: DistillationPublication[]; canEdit: boolean; busy: boolean; dirty: boolean; scopeLocked: boolean }>()
defineEmits<{ copy: []; build: []; open: [publication: DistillationPublication]; download: [publication: DistillationPublication, artifact: DistillationArtifact] }>()
const tab = ref('project')
</script>
