<template>
  <div>
    <div class="edm-card">
      <div class="edm-card__head">
        <div class="edm-card__title">协同投标组合</div>
        <div class="edm-toolbar edm-toolbar--right">
          <el-tag size="small" effect="plain">≥ 2 次</el-tag>
        </div>
      </div>
      <el-table :data="pairs" size="small" border stripe
                empty-text="暂无协同记录" max-height="380">
        <el-table-column type="index" label="#" width="52" align="center" />
        <el-table-column label="组合成员" min-width="330">
          <template #default="{ row }">
            <el-tag v-for="m in row.members" :key="m" size="small"
                    effect="plain" style="margin-right:6px">{{ m }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="协同次数" width="104" align="right" sortable
                         :sort-method="(a,b)=>a.count-b.count">
          <template #default="{ row }">
            <el-tag size="small" type="success" effect="dark">{{ row.count }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="共同项目" min-width="300">
          <template #default="{ row }">
            <div v-for="(p, i) in row.projects" :key="i"
                 class="edm-muted" style="font-size:12.5px;line-height:1.7">
              · {{ p }}
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="edm-card">
      <div class="edm-card__head">
        <div class="edm-card__title">高频投标方</div>
      </div>
      <el-table :data="bidders" size="small" border stripe empty-text="暂无数据">
        <el-table-column type="index" label="#" width="52" align="center" />
        <!-- 长投标方全称换行展示，不再省略号截断 -->
        <el-table-column prop="name" label="投标方" min-width="240"
                         class-name="edm-cell-wrap" />
        <el-table-column prop="bid_count" label="投标次数" width="130" align="right" />
        <el-table-column prop="win_count" label="中标数" width="110" align="right" />
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ data: { type: Object, default: () => ({}) } })
const pairs = computed(() => props.data?.co_bid_pairs || [])
const bidders = computed(() => props.data?.top_bidders || [])
</script>
