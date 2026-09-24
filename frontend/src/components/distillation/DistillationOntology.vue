<template>
  <section>
    <h2>从业务对象到数据流转</h2>
    <p class="distill-hint">描述业务含义、对象关系和数据如何产生或转化。字段相似不等于血缘成立，缺少依据时请在证据中记录限制。</p>
    <el-tabs>
      <el-tab-pane label="对象与 ER 关系" name="entities">
        <DistillationGraph variant="entity" title="ER 关系图" :nodes="document.entities.map(item => ({ ...item, detail: item.description }))" :edges="document.relations" />
        <div class="distill-section-head"><h3>业务对象</h3><el-button @click="addEntity">添加对象</el-button></div>
        <article v-for="(item, index) in document.entities" :key="item.key" class="distill-item">
          <div class="distill-fields">
            <el-form-item :label="`对象 ${index + 1} 名称`"><el-input v-model="item.name" maxlength="200" /></el-form-item>
            <el-form-item label="业务含义"><el-input v-model="item.description" type="textarea" :rows="2" maxlength="4000" /></el-form-item>
            <el-form-item label="业务属性（每行一项）"><el-input :model-value="item.attributes.join('\n')" type="textarea" :rows="3" maxlength="4000" @update:model-value="(value: string) => item.attributes = linesOf(value)" /></el-form-item>
          </div>
          <el-button text type="danger" :aria-label="`移除对象 ${item.name || index + 1}`" @click="removeEntity(item.key)">移除对象</el-button>
        </article>
        <div class="distill-section-head"><h3>对象关系</h3><el-button :disabled="!document.entities.length" @click="document.relations.push({ source: '', target: '', label: '', cardinality: 'unconfirmed' })">添加关系</el-button></div>
        <article v-for="(relation, index) in document.relations" :key="index" class="distill-item">
          <div class="distill-fields">
            <el-form-item :label="`关系 ${index + 1} 来源对象`"><el-select v-model="relation.source"><el-option v-for="item in document.entities" :key="item.key" :label="item.name || '未命名对象'" :value="item.key" /></el-select></el-form-item>
            <el-form-item label="目标对象"><el-select v-model="relation.target"><el-option v-for="item in document.entities" :key="item.key" :label="item.name || '未命名对象'" :value="item.key" /></el-select></el-form-item>
            <el-form-item label="关系含义"><el-input v-model="relation.label" maxlength="200" /></el-form-item>
            <el-form-item label="数量关系"><el-select v-model="relation.cardinality"><el-option label="基数待核对" value="unconfirmed" /><el-option label="一对一" value="one_to_one" /><el-option label="一对多" value="one_to_many" /><el-option label="多对多" value="many_to_many" /></el-select></el-form-item>
          </div>
          <el-button text type="danger" :aria-label="`移除关系 ${index + 1}`" @click="document.relations.splice(index, 1)">移除关系</el-button>
        </article>
      </el-tab-pane>
      <el-tab-pane label="数据血缘" name="lineage">
        <DistillationGraph variant="lineage" title="数据血缘图" :nodes="document.entities" :edges="document.lineage.map(item => ({ ...item, label: item.transformation, detail: item.transformation }))" />
        <div class="distill-section-head"><h3>来源 → 转换 → 结果</h3><el-button :disabled="!document.entities.length" @click="document.lineage.push({ source: '', target: '', transformation: '', evidence_refs: [] })">添加血缘</el-button></div>
        <article v-for="(item, index) in document.lineage" :key="index" class="distill-item">
          <div class="distill-fields">
            <el-form-item :label="`血缘 ${index + 1} 来源`"><el-select v-model="item.source"><el-option v-for="entity in document.entities" :key="entity.key" :label="entity.name || '未命名对象'" :value="entity.key" /></el-select></el-form-item>
            <el-form-item label="去向"><el-select v-model="item.target"><el-option v-for="entity in document.entities" :key="entity.key" :label="entity.name || '未命名对象'" :value="entity.key" /></el-select></el-form-item>
            <el-form-item label="转换规则 / 产生方式"><el-input v-model="item.transformation" type="textarea" :rows="3" maxlength="4000" /></el-form-item>
            <el-form-item label="证明转换的依据"><el-select v-model="item.evidence_refs" multiple><el-option v-for="source in document.evidence" :key="source.key" :label="source.title || '未命名证据'" :value="source.key" /></el-select></el-form-item>
          </div>
          <el-button text type="danger" :aria-label="`移除血缘 ${index + 1}`" @click="document.lineage.splice(index, 1)">移除血缘</el-button>
        </article>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>
<script setup lang="ts">
import type { DistillationDocument } from '@/types/businessDistillation'
import { linesOf } from '@/utils/businessDistillation'
import { createClientRequestId } from '@/utils/clientRequestId'
import DistillationGraph from './DistillationGraph.vue'
const document = defineModel<DistillationDocument>({ required: true })
function addEntity() { document.value.entities.push({ key: `entity_${createClientRequestId().replace(/-/g, '').slice(0, 16)}`, name: '', description: '', attributes: [] }) }
function removeEntity(key: string) {
  document.value.entities = document.value.entities.filter(item => item.key !== key)
  document.value.relations = document.value.relations.filter(item => item.source !== key && item.target !== key)
  document.value.lineage = document.value.lineage.filter(item => item.source !== key && item.target !== key)
}
</script>
