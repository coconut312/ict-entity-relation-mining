<template>
  <div class="edm-card">
    <div class="edm-card__head">
      <div class="edm-card__title">全部投标方</div>
      <div class="edm-toolbar edm-toolbar--right">
        <span class="edm-dim" style="font-size:12.5px">共 {{ rows.length }} 家</span>
      </div>
    </div>
    <el-table :data="rows" size="small" border stripe
              empty-text="暂无竞标记录" max-height="440">
      <el-table-column type="index" label="#" width="52" align="center" />
      <!-- 长投标方全称换行展示，不再省略号截断 -->
      <el-table-column prop="name" label="投标方" min-width="240"
                       class-name="edm-cell-wrap" />
      <el-table-column label="竞标次数" width="130" align="right" sortable
                       :sort-method="(a,b)=>a.bid_count-b.bid_count">
        <template #default="{ row }">
          <el-tag size="small" effect="plain"
                  :type="row.bid_count >= 2 ? 'warning' : 'info'">{{ row.bid_count }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="对方中标" width="122" align="right">
        <template #default="{ row }">{{ row.win_count }}</template>
      </el-table-column>
      <el-table-column :label="`项目金额（${amountUnit}）`" width="160" align="right" sortable
                       :sort-method="(a,b)=>a.cooperate_amount-b.cooperate_amount">
        <template #default="{ row }">{{ fmtMoneyUnit(row.cooperate_amount, amountUnit) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { fmtMoneyUnit, moneyUnit } from '@/utils/format'

const props = defineProps({ data: { type: Object, default: () => ({}) } })
const rows = computed(() => props.data?.all_bidders || [])

/** 整列统一单位：标签与单元格用同一个单位 */
const amountUnit = computed(() => moneyUnit(rows.value.map((r) => r.cooperate_amount)))
</script>
