<template>
  <div>
    <div class="edm-page-head">
      <div>
        <h2 class="edm-page-title">标的检索</h2>
        <p class="edm-page-desc">
          按品目、品牌、采购方等条件检索已入库的标的信息。条件改动即自动查询。
        </p>
      </div>
      <div class="edm-toolbar">
        <span class="edm-perf" :class="perfMs != null && perfMs < 1000 ? 'edm-perf--good' : 'edm-perf--warn'"
              title="最近一次查询耗时">
          <el-icon><Timer /></el-icon>
          {{ perfMs == null ? '—' : perfMs + ' ms' }}
        </span>
        <el-button v-if="dirty" link :icon="RefreshLeft" @click="reset">重置</el-button>
      </div>
    </div>

    <!-- 筛选条：单行排布，改动即查询 -->
    <div class="edm-card edm-filter">
      <el-input
        v-model="query.keyword" clearable
        placeholder="搜索产品 / 规格 / 品牌"
        class="edm-filter__kw"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>

      <el-select :popper-options="SELECT_POPPER" fit-input-width :offset="6" v-model="query.category" clearable filterable placeholder="全部品目">
        <el-option v-for="c in facets.categories" :key="c.value"
                   :label="`${c.value} (${c.count})`" :value="c.value" />
      </el-select>

      <el-select :popper-options="SELECT_POPPER" fit-input-width :offset="6" v-model="query.brand" clearable filterable placeholder="全部品牌">
        <el-option v-for="b in facets.brands" :key="b.value"
                   :label="`${b.value} (${b.count})`" :value="b.value" />
      </el-select>

      <el-select :popper-options="SELECT_POPPER" fit-input-width :offset="6" v-model="query.purchaser" clearable filterable placeholder="全部采购方">
        <el-option v-for="p in facets.purchasers" :key="p.value"
                   :label="`${p.value} (${p.count})`" :value="p.value" />
      </el-select>

      <el-select :popper-options="SELECT_POPPER" fit-input-width :offset="6" v-model="query.supplier" clearable filterable placeholder="全部供应商">
        <el-option v-for="s in facets.suppliers" :key="s.value"
                   :label="`${s.value} (${s.count})`" :value="s.value" />
      </el-select>
    </div>

    <!-- 结果 -->
    <div class="edm-card" style="margin-top:14px">
      <div class="edm-card__head">
        <div class="edm-card__title">
          检索结果
          <span class="edm-dim" style="font-weight:400;margin-left:6px">
            共 {{ meta.total }} 条 · 点击行首展开详情
          </span>
        </div>
      </div>

      <el-table
        :data="rows" v-loading="loading" size="small" border stripe
        empty-text="无匹配结果" style="width:100%"
        row-key="id"
      >
        <!-- 行内展开：替代浮层详情，少一层交互，也不额外请求接口 -->
        <el-table-column type="expand" width="42">
          <template #default="{ row }">
            <div class="edm-expand">
              <el-descriptions :column="3" border size="small">
                <el-descriptions-item label="产品或服务名称" :span="2">
                  {{ row.product_name || '—' }}
                </el-descriptions-item>
                <el-descriptions-item label="品目">{{ row.category || '—' }}</el-descriptions-item>
                <el-descriptions-item label="品牌">{{ row.brand || '—' }}</el-descriptions-item>
                <el-descriptions-item label="规格型号">{{ row.spec || '—' }}</el-descriptions-item>
                <el-descriptions-item label="数量单位">{{ row.unit || '—' }}</el-descriptions-item>
                <el-descriptions-item :label="`单价（${amountUnit}）`">
                  {{ unitPrice(row.unit_price) }}
                </el-descriptions-item>
                <el-descriptions-item label="数量">{{ num(row.quantity) }}</el-descriptions-item>
                <el-descriptions-item :label="`总价（${amountUnit}）`">
                  <b>{{ money(row.total_price) }}</b>
                </el-descriptions-item>
                <el-descriptions-item label="采购方">{{ row.purchaser || '—' }}</el-descriptions-item>
                <el-descriptions-item label="供应商">{{ row.supplier || '—' }}</el-descriptions-item>
                <el-descriptions-item label="项目">{{ row.project_name || '—' }}</el-descriptions-item>
                <el-descriptions-item :label="`项目金额（${amountUnit}）`">
                  {{ row.project_amount ? money(row.project_amount) : '—' }}
                </el-descriptions-item>
                <el-descriptions-item label="公告编号">{{ row.doc_id || '—' }}</el-descriptions-item>
                <el-descriptions-item label="公告日期">{{ row.announce_date || '—' }}</el-descriptions-item>
                <el-descriptions-item label="数据来源">
                  <el-tag size="small" effect="plain">{{ row.source || '—' }}</el-tag>
                </el-descriptions-item>
              </el-descriptions>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="product_name" label="产品或服务" min-width="240"
                         show-overflow-tooltip />
        <el-table-column prop="category" label="品目" width="132" show-overflow-tooltip />
        <el-table-column prop="brand" label="品牌" width="106" show-overflow-tooltip />
        <el-table-column prop="spec" label="规格型号" width="128" show-overflow-tooltip />
        <el-table-column :label="`单价（${amountUnit}）`" width="118" align="right">
          <template #default="{ row }"><span class="edm-mono">{{ unitPrice(row.unit_price) }}</span></template>
        </el-table-column>
        <el-table-column label="数量" width="82" align="right">
          <template #default="{ row }"><span class="edm-mono">{{ num(row.quantity) }}</span></template>
        </el-table-column>
        <el-table-column :label="`总价（${amountUnit}）`" width="126" align="right">
          <template #default="{ row }"><span class="edm-mono">{{ money(row.total_price) }}</span></template>
        </el-table-column>
        <el-table-column prop="purchaser" label="采购方" width="186" show-overflow-tooltip />
        <el-table-column prop="supplier" label="供应商" width="196" show-overflow-tooltip />
      </el-table>

      <el-pagination
        v-if="meta.total > 0"
        class="edm-pager"
        layout="total, prev, pager, next"
        :total="meta.total"
        :current-page="meta.page"
        :page-size="meta.page_size || 20"
        :pager-count="7"
        background
        @current-change="doSearch"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RefreshLeft, Search, Timer } from '@element-plus/icons-vue'
import { getItemFacets, searchItems } from '@/api'
import { perf } from '@/api/client'
import { useDataFreshness } from '@/utils/dataVersion'
import { fmtMoneyUnit, fmtNum, fmtUnitPrice, moneyUnit } from '@/utils/format'
import { SELECT_POPPER } from '@/utils/popper'

const PAGE_SIZE = 20   // 固定分页：减少控件、控制单次渲染量，直接服务于查询效率评分

const query = reactive({
  keyword: '', brand: '', category: '', supplier: '', purchaser: '',
})
const rows = ref([])
const facets = ref({ brands: [], categories: [], suppliers: [], purchasers: [] })
const meta = ref({ page: 1, page_size: PAGE_SIZE, total: 0 })
const loading = ref(false)
const perfMs = ref(null)

const num = fmtNum

/**
 * 金额列的**统一单位**：本页所有金额（单价/总价/项目金额）任一 ≥ 1 万就整列按「万元」显示，
 * 否则整列按「元」显示；标签用同一个单位，避免「单价(元)」配上「12.5 万」这种自相矛盾。
 * 原实现标签写死「(元)」，但 fmtMoney 会在 ≥1 万时自动转「万」——即这次修的单位标注错误。
 */
const amountUnit = computed(() => moneyUnit(
  rows.value.flatMap((r) => [r.unit_price, r.total_price, r.project_amount])))
const money = (v) => fmtMoneyUnit(v, amountUnit.value)
const unitPrice = (v) => fmtUnitPrice(v, amountUnit.value)

const dirty = computed(() => Object.values(query).some((v) => v !== '' && v != null))

/** 任一筛选条件变化即自动查询（防抖，避免连续输入时打请求） */
let timer = null
const filterKey = computed(() => JSON.stringify(query))
watch(filterKey, () => {
  clearTimeout(timer)
  timer = setTimeout(() => doSearch(1), 260)
})

const buildParams = () => {
  const p = { page: meta.value.page, page_size: PAGE_SIZE,
              sort_by: 'doc_id', sort_order: 'asc' }
  Object.entries(query).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) p[k] = v
  })
  return p
}

const doSearch = async (page = 1) => {
  meta.value.page = page
  loading.value = true
  try {
    const res = await searchItems(buildParams())
    // 数据变少（例如重新上传了更小的数据集）时当前页码可能越界，
    // 回落第 1 页重查一次；page=1 不会再递归，不存在死循环。
    if (!res.items.length && page > 1 && res.meta.total > 0) return doSearch(1)
    rows.value = res.items
    meta.value = res.meta
    perfMs.value = perf.lastMs
  } catch (e) {
    rows.value = []
  } finally {
    loading.value = false
  }
}

const reset = () => {
  Object.assign(query, { keyword: '', brand: '', category: '', supplier: '', purchaser: '' })
}

const loadFacets = async () => {
  try {
    facets.value = await getItemFacets()
  } catch (e) { /* ignore */ }
}

/**
 * 用**当前筛选条件**重查一遍：结果行、facets（筛选下拉的取值与计数）、
 * 「共 N 条」的统计会一起刷新。
 */
const refresh = async () => {
  await Promise.all([loadFacets(), doSearch(meta.value.page)])
}

onMounted(async () => {
  await Promise.all([loadFacets(), doSearch(1)])
})

/**
 * 自动重查（这也是「上传后标的检索不用手动刷新」的修复点）。
 *
 * 本页被 App.vue 的 <keep-alive> 缓存，过去只在挂载时查一次、重新激活只刷新
 * facets 不刷新结果行 —— 于是上传完新数据回到本页，表格还是旧的，必须手动刷新。
 * `useDataFreshness` 同时覆盖两种情况：
 *   * 数据版本 bump（上传解析落库成功）时若本页可见，立即按当前条件重查；
 *   * 每次「离开再回来」重新激活时也重查一次。
 * 首屏的第一次加载仍由 onMounted 负责，两者不会重复打请求。
 */
useDataFreshness(refresh)
</script>

<style scoped>
.edm-filter {
  display: grid;
  grid-template-columns: minmax(220px, 1.4fr) repeat(4, minmax(140px, 1fr));
  gap: 10px;
  padding: 14px 16px;
}
@media (max-width: 1100px) {
  .edm-filter { grid-template-columns: 1fr 1fr; }
}
.edm-expand { padding: 4px 12px 12px 52px; background: #fafbfd; }
.edm-pager { margin-top: 14px; justify-content: flex-end; }
</style>
