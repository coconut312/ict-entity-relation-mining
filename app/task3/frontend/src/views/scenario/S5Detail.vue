<template>
  <div class="edm-card">
    <div class="edm-card__head">
      <div class="edm-card__title">共同项目</div>
      <div class="edm-toolbar edm-toolbar--right">
        <span class="edm-dim" style="font-size:12.5px">共 {{ rows.length }} 个</span>
      </div>
    </div>
    <el-table :data="rows" size="small" border stripe
              empty-text="暂无共同项目" max-height="480">
      <el-table-column type="index" label="#" width="52" align="center" />
      <!-- 项目名 / 中标方全称换行展示，不再省略号截断 -->
      <el-table-column prop="project_name" label="项目" min-width="290"
                       class-name="edm-cell-wrap" />
      <el-table-column prop="purchaser" label="采购方" width="200"
                       class-name="edm-cell-wrap" />
      <el-table-column prop="announce_date" label="公告日期" width="106" />
      <el-table-column :label="`项目金额（${amountUnit}）`" width="132" align="right" sortable
                       :sort-method="(a,b)=>a.amount-b.amount">
        <template #default="{ row }">{{ fmtMoneyUnit(row.amount, amountUnit) }}</template>
      </el-table-column>
      <el-table-column label="中标" width="200" class-name="edm-cell-wrap">
        <template #default="{ row }">
          <span>{{ row.winner }}</span>
          <el-tag v-if="row.winner_is_target" size="small" type="success"
                  effect="plain" style="margin-left:6px">已选供应商</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="参与投标方" min-width="240">
        <template #default="{ row }">
          <el-tag v-for="p in row.participants" :key="p" size="small"
                  :type="p === row.winner ? 'success' : 'info'"
                  effect="plain" style="margin:2px 4px 2px 0">{{ p }}</el-tag>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { fmtMoneyUnit, moneyUnit } from '@/utils/format'

const props = defineProps({ data: { type: Object, default: () => ({}) } })
const rows = computed(() => props.data?.projects || [])

/** 整列统一单位：标签与单元格用同一个单位 */
const amountUnit = computed(() => moneyUnit(rows.value.map((r) => r.amount)))
</script>
