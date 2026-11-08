<template>
  <div class="edm-card">
    <div class="edm-card__head">
      <div class="edm-card__title">供应商明细</div>
      <div class="edm-toolbar edm-toolbar--right">
        <span class="edm-dim" style="font-size:12.5px">共 {{ rows.length }} 家</span>
      </div>
    </div>
    <el-table :data="rows" size="small" border stripe
              empty-text="暂无合作记录" max-height="440">
      <el-table-column type="index" label="#" width="52" align="center" />
      <!-- 长供应商全称换行展示，不再省略号截断（与图表侧「长名换行」同一取舍） -->
      <el-table-column prop="name" label="供应商名称" min-width="230"
                       class-name="edm-cell-wrap" />
      <el-table-column label="合作次数" width="100" align="right" sortable
                       :sort-method="(a,b)=>a.cooperate_count-b.cooperate_count">
        <template #default="{ row }">
          <el-tag size="small" :type="row.cooperate_count >= 2 ? 'success' : 'info'" effect="plain">
            {{ row.cooperate_count }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="`累计金额（${amountUnit}）`" width="150" align="right" sortable
                       :sort-method="(a,b)=>a.total_amount-b.total_amount">
        <template #default="{ row }">{{ fmtMoneyUnit(row.total_amount, amountUnit) }}</template>
      </el-table-column>
      <el-table-column prop="first_date" label="首次合作" width="112" />
      <el-table-column prop="last_date" label="最近合作" width="112" />
      <el-table-column label="类型" width="112" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_product_supplier ? 'warning' : 'info'"
                  effect="plain">
            {{ row.is_product_supplier ? '产品' : '服务' }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { fmtMoneyUnit, moneyUnit } from '@/utils/format'

const props = defineProps({ data: { type: Object, default: () => ({}) } })
const rows = computed(() => props.data?.suppliers || [])

/** 整列统一单位：标签与单元格用同一个单位，杜绝「标签写元、数值是万」 */
const amountUnit = computed(() => moneyUnit(rows.value.map((r) => r.total_amount)))
</script>
