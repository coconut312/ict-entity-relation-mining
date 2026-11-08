<template>
  <div class="edm-card">
    <div class="edm-card__head">
      <div class="edm-card__title">共同采购方</div>
      <div class="edm-toolbar edm-toolbar--right">
        <el-tag v-for="s in suppliers" :key="s" size="small" effect="plain"
                style="margin-left:6px">{{ s }}</el-tag>
      </div>
    </div>
    <el-table :data="rows" size="small" border stripe
              empty-text="暂无共同采购方" max-height="440">
      <el-table-column type="index" label="#" width="52" align="center" />
      <!-- 长采购方全称换行展示，不再省略号截断 -->
      <el-table-column prop="name" label="采购方" min-width="250"
                       class-name="edm-cell-wrap" />
      <el-table-column label="合作频次" width="104" align="right" sortable
                       :sort-method="(a,b)=>a.cooperate_count-b.cooperate_count">
        <template #default="{ row }">
          <el-tag size="small" type="success" effect="plain">{{ row.cooperate_count }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="`合作金额（${amountUnit}）`" width="150" align="right" sortable
                       :sort-method="(a,b)=>a.cooperate_amount-b.cooperate_amount">
        <template #default="{ row }">{{ fmtMoneyUnit(row.cooperate_amount, amountUnit) }}</template>
      </el-table-column>
      <el-table-column label="合作项目" min-width="300">
        <template #default="{ row }">
          <div v-for="(p, i) in row.projects" :key="i"
               class="edm-muted" style="font-size:12.5px;line-height:1.7">· {{ p }}</div>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { fmtMoneyUnit, moneyUnit } from '@/utils/format'

const props = defineProps({ data: { type: Object, default: () => ({}) } })
const rows = computed(() => props.data?.purchasers || [])
const suppliers = computed(() => props.data?.suppliers || [])

/** 整列统一单位：标签与单元格用同一个单位 */
const amountUnit = computed(() => moneyUnit(rows.value.map((r) => r.cooperate_amount)))
</script>
