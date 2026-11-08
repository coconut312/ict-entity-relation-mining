<template>
  <div>
    <div class="edm-page-head">
      <div>
        <h2 class="edm-page-title">场景分析</h2>
        <p class="edm-page-desc">
          分析采购方与供应商之间的合作与竞争关系。
        </p>
      </div>
      <div class="edm-toolbar">
        <span class="edm-perf" :class="perfMs != null && perfMs < 1000 ? 'edm-perf--good' : 'edm-perf--warn'"
              title="最近一次查询耗时">
          <el-icon><Timer /></el-icon>
          {{ perfMs == null ? '—' : perfMs + ' ms' }}
        </span>
      </div>
    </div>

    <!-- 场景切换：分段按钮，比标签页更紧凑 -->
    <div class="edm-seg">
      <button
        v-for="s in scenarios" :key="s.id"
        class="edm-seg__item" :class="{ 'is-active': activeId === s.id }"
        @click="switchTo(s.id)"
      >
        <span class="edm-seg__badge">{{ s.id.toUpperCase() }}</span>
        <span class="edm-seg__label">{{ s.title }}</span>
      </button>
    </div>

    <div v-if="def" class="edm-workbench">
      <!-- 左：查询条件 -->
      <aside class="edm-panel">
        <div class="edm-panel__title">{{ def.subtitle }}</div>
        <div class="edm-panel__desc">{{ def.description }}</div>

        <el-form label-position="top" size="default" @submit.prevent>
          <el-form-item v-for="p in def.params" :key="p.name" :required="p.required">
            <template #label>
              <span class="edm-panel__label">
                {{ p.label }}
                <el-tooltip v-if="p.help" :content="p.help" placement="top">
                  <el-icon><QuestionFilled /></el-icon>
                </el-tooltip>
              </span>
            </template>

            <EntityPicker v-if="p.type === 'entity'" v-model="form[p.name]"
                          :type="p.entity_type" :placeholder="p.placeholder" />
            <EntityPicker v-else-if="p.type === 'entities'" v-model="form[p.name]"
                          :type="p.entity_type" multiple :placeholder="p.placeholder" />
            <el-input-number v-else-if="p.type === 'number'" v-model="form[p.name]"
                             :min="p.min" :max="p.max" style="width:100%" />
            <el-input v-else v-model="form[p.name]" :placeholder="p.placeholder" />
          </el-form-item>

          <div v-if="hint" class="edm-panel__hint">
            <el-icon><WarningFilled /></el-icon><span>{{ hint }}</span>
          </div>

          <el-button type="primary" :icon="Search" :loading="loading"
                     style="width:100%;margin-top:6px" @click="run">
            查询
          </el-button>
        </el-form>
      </aside>

      <!-- 右：结果 -->
      <section class="edm-result">
        <template v-if="result">
          <div v-if="errorMsg" class="edm-card" style="border-color:#fde68a;background:#fffbeb">
            <el-icon color="#d97706"><WarningFilled /></el-icon>
            <span style="margin-left:6px">{{ errorMsg }}</span>
          </div>

          <div v-if="summaryCards.length" class="edm-stat-grid">
            <StatCard v-for="c in summaryCards" :key="c.key"
                      :label="c.label" :value="c.value" :auto-unit="false" />
          </div>

          <ChartPanel
            v-if="chartOption"
            :title="chartTitle" :option="chartOption" :height="chartHeight"
            style="margin-top:14px"
            :animate-ms="CHART_ANIM_MS"
          />

          <component :is="detailComp" :data="result" style="margin-top:14px" />
        </template>

        <div v-else class="edm-card">
          <div class="edm-empty">
            <el-icon :size="38" color="#c9d0dc"><Share /></el-icon>
            <div class="edm-empty__title">设置左侧条件后查询</div>
            <div class="edm-empty__hint">
              结果以指标、图表与明细三种形式呈现。
            </div>
          </div>
        </div>
      </section>
    </div>

    <div v-else-if="!loading" class="edm-card">
      <div class="edm-empty">
        <el-icon :size="34" color="#c4cad6"><Warning /></el-icon>
        <div class="edm-empty__title">场景加载失败</div>
        <div class="edm-empty__hint">请确认服务是否正常。</div>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 场景分析工作台。
 *
 * 设计要点（三方合并时前端不用改）：
 *   * 场景的标题、说明、参数、接口全部由后端 /scenarios 提供，
 *     前端只认识 param.type（entity/entities/number），不认识业务字段。
 *   * 结果渲染按 scenario id 分支，新增场景只需加一个分支 + 一个明细组件。
 *
 * 布局：左条件栏常驻 + 右结果区，避免"填表 → 下滑看结果 → 再填表"的来回滚动。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { QuestionFilled, Search, Timer, WarningFilled } from '@element-plus/icons-vue'
import EntityPicker from '@/components/EntityPicker.vue'
import StatCard from '@/components/StatCard.vue'
import ChartPanel from '@/components/ChartPanel.vue'
import { getScenarios, SCENARIO_API } from '@/api'
import { perf } from '@/api/client'
import { useDataFreshness } from '@/utils/dataVersion'
import { fmtMoneyUnit, fmtNum, fmtPct, moneyUnit } from '@/utils/format'

import S1Detail from '@/views/scenario/S1Detail.vue'
import S2Detail from '@/views/scenario/S2Detail.vue'
import S3Detail from '@/views/scenario/S3Detail.vue'
import S4Detail from '@/views/scenario/S4Detail.vue'
import S5Detail from '@/views/scenario/S5Detail.vue'

const DETAIL_MAP = { s1: S1Detail, s2: S2Detail, s3: S3Detail, s4: S4Detail, s5: S5Detail }

/**
 * 本页图表动画时长（毫秒）—— 一个需要权衡的旋钮。
 *
 * 本页是**被计时的查询路径**（查询效率以五大场景各一次查询的平均响应时间为准，
 * 口径是「前端发起请求至结果完全渲染完毕」），因此 ECharts 的动画时间会
 * 直接计入响应耗时。
 *
 * 160ms 的取舍：足够让数据变化显得平滑，又不至于在接口耗时接近 1 秒时
 * 把总时间顶过阈值。等真实数据接入、实测出接口耗时后，如果余量充足
 * 可以调大到 300；如果吃紧，直接设 0 关闭。
 */
const CHART_ANIM_MS = 160

const scenarios = ref([])
const activeId = ref('s1')
const form = reactive({})
const loading = ref(false)
const result = ref(null)
const perfMs = ref(null)
const errorMsg = ref('')

const def = computed(() => scenarios.value.find((s) => s.id === activeId.value) || null)
const detailComp = computed(() => DETAIL_MAP[activeId.value] || null)

/** 多选类场景选不足 2 个时，提前给出提示而不是等接口报错 */
const hint = computed(() => {
  const p = def.value?.params?.find((x) => x.type === 'entities')
  if (!p) return ''
  const v = form[p.name]
  if (Array.isArray(v) && v.length === 1) return '至少选 2 个供应商'
  return ''
})

const resetForm = () => {
  Object.keys(form).forEach((k) => delete form[k])
  def.value?.params?.forEach((p) => {
    form[p.name] = p.type === 'entities'
      ? []
      : (p.default ?? (p.type === 'number' ? p.min : ''))
  })
  result.value = null
  errorMsg.value = ''
  perfMs.value = null
}

const switchTo = (id) => {
  if (id === activeId.value) return
  activeId.value = id
  resetForm()
}

/** 参数校验：必填项是否都填了（run 与自动重查共用同一判据） */
const isFormValid = () => {
  if (!def.value) return false
  for (const p of def.value.params) {
    const v = form[p.name]
    const empty = v === '' || v === null || v === undefined
      || (Array.isArray(v) && v.length === 0)
    if (p.required && empty) return false
  }
  return true
}

const run = async () => {
  if (!def.value) return
  if (!isFormValid()) {
    const p = def.value.params.find((x) => {
      const v = form[x.name]
      return x.required && (v === '' || v === null || v === undefined
        || (Array.isArray(v) && v.length === 0))
    })
    if (p) ElMessage.warning(`请填写 ${p.label}`)
    return
  }
  errorMsg.value = ''
  loading.value = true
  try {
    const payload = {}
    for (const p of def.value.params) {
      const v = form[p.name]
      if (v !== '' && v !== null && v !== undefined) payload[p.name] = v
    }
    const res = await SCENARIO_API[activeId.value](payload)
    result.value = res
    perfMs.value = perf.lastMs
    if (res.summary && res.summary.error) errorMsg.value = res.summary.error
  } catch (e) {
    result.value = null
  } finally {
    loading.value = false
  }
}

/**
 * 数据变过（上传解析落库成功）后，把**当前已经查出来的那个场景**用同样的条件
 * 重新查一遍：指标卡、图表与 S1~S5 明细组件都吃 `result` 这个 prop，
 * 结果一换，明细表自然跟着刷新。
 *
 * 只在「已经查过结果、且条件仍然完整」时才重查 —— 否则会凭空弹出必填校验提示。
 */
const refreshResult = () => {
  if (!result.value || !def.value) return
  if (!isFormValid()) return
  run()
}

/* ---------------- 汇总指标 ---------------- */

/**
 * 场景摘要键 -> 中文标签。
 *
 * 后端两大仓储实现（`integrations/repos.py` 的 SQLite 版与
 * `integrations/neo4j_repo.py` 的 Neo4j 版）put 进 `summary` 的键全部在这里登记；
 * 金额类键**不写死单位**，单位由 `moneyUnit()` 按实际数值决定后拼进标签，
 * 保证「总金额（万元）＝ 5,765.03」这类标签与数值永远一致（见 utils/format.js）。
 */
const SUMMARY_LABELS = {
  // ---- 计数类 ----
  n_suppliers: '供应商数',
  n_product_suppliers: '产品供应商数',
  n_notices: '公告数',
  n_purchasers: '采购单位数',
  n_projects: '项目数',
  n_items: '标的数',
  n_bidders: '投标人数',
  n_winners: '中标人数',
  n_co_bidders: '共同竞标方数',
  n_co_bid_pairs: '协同投标组合数',
  n_competitors: '竞争对手数',
  n_participants: '参与方数',
  n_docs: '公告篇数',
  n_brands: '品牌数',
  // ---- 次数 / 比率 ----
  min_count: '最小合作次数',
  win_notices: '中标次数',
  bid_notices: '参与投标次数',
  win_rate: '中标率',
  max_cooperate: '最高合作次数',
  top_n: '显示条数',
  // ---- 金额（单位随后拼接）----
  total_amount: '总金额',
  total_coop_amount: '合作总金额',
  won_amount: '中标金额',
  avg_amount: '平均金额',
  // ---- 其它 ----
  suppliers: '供应商',
  engine: '数据引擎',
  avg_process_ms: '单篇耗时',
  date_min: '最早公告日期',
  date_max: '最晚公告日期',
  field_fill_rate: '字段完整度',
  // ---- 历史 / 备用键（跨版本接口兼容）----
  project_count: '项目数',
  supplier_count: '供应商数',
  bidder_count: '投标方数',
  bid_count: '投标次数',
  win_count: '中标次数',
  pair_count: '协同组合',
  competitor_count: '竞对方数',
  purchaser_count: '采购方数',
  target_win_count: '其中中标',
}

/** 金额类摘要键：值按「元」传入，标签单位由实际显示单位决定 */
const MONEY_SUMMARY_KEYS = new Set([
  'total_amount', 'total_coop_amount', 'avg_amount', 'won_amount',
  'project_amount', 'cooperate_amount', 'max_amount', 'min_amount', 'amount',
])

/** 比率类（0~1）：按百分比展示 */
const PCT_SUMMARY_KEYS = new Set(['win_rate'])

/** 毫秒类：标签补上单位，避免出现「单篇耗时 1200」这种没有单位的卡片 */
const MS_SUMMARY_KEYS = new Set(['avg_process_ms'])

/**
 * 未登记的键兜底：转成空格分隔的可读文案并首字母大写，
 * **绝不把 `n_xxx` 这种英文下划线键名直接暴露给用户**。
 */
const humanizeKey = (k) => String(k)
  .replace(/_/g, ' ')
  .trim()
  .replace(/\b([a-z])/g, (c) => c.toUpperCase())

const summaryCards = computed(() => {
  const s = result.value?.summary || {}
  const out = []
  Object.entries(s).forEach(([k, v]) => {
    if (k === 'error' || v === null || v === undefined) return
    const base = SUMMARY_LABELS[k] || humanizeKey(k)
    if (MONEY_SUMMARY_KEYS.has(k)) {
      const unit = moneyUnit(v)                 // 数值实际会以什么单位显示
      out.push({ key: k, label: `${base}（${unit}）`, value: fmtMoneyUnit(v, unit) })
    } else if (PCT_SUMMARY_KEYS.has(k)) {
      out.push({ key: k, label: base, value: fmtPct(v) })
    } else if (MS_SUMMARY_KEYS.has(k)) {
      out.push({ key: k, label: `${base}（毫秒）`, value: fmtNum(v) })
    } else if (Array.isArray(v)) {
      out.push({ key: k, label: base, value: v.join('、') })
    } else if (typeof v === 'number') {
      out.push({ key: k, label: base, value: fmtNum(v) })
    } else {
      out.push({ key: k, label: base, value: String(v) })
    }
  })
  return out
})

/* ---------------- 图表 ---------------- */

/**
 * 横向条形图。
 *
 * 为什么不用竖向：本页类目名是实体全称（如"山东第一医科大学第一附属医院
 * （山东省千佛山医院）"），竖向条形图的类目轴只有窄窄一条，只能靠旋转 24°
 * 或截断来塞下，结果是斜排 + 显示不全 + 字号被迫缩小。
 * 改成横向后类目名占满左侧一行，可换行、不旋转、字号能放大。
 *
 * `unit`（' 次' / ' 万元'）由调用方给出，同时写进 tooltip、数值轴刻度与
 * 数值标签，保证「图上数值」与 `chartTitle` 里的单位始终一致。
 */
const barOption = (rows, color = '#2563eb', unit = '') => {
  // ECharts 的类目轴自下而上排列，先反转，让数值大的显示在最上方
  const data = [...rows].reverse()
  return {
    grid: { left: 6, right: 64, top: 8, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: (v) => `${v}${unit}` },
    xAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#eef1f6' } },
      axisLabel: {
        color: '#4b5563', fontSize: 12,
        formatter: unit ? `{value}${unit}` : '{value}',
      },
    },
    yAxis: {
      type: 'category',
      data: data.map((r) => r.name),
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: {
        color: '#374151',        // 正文级深灰，不再是 #98a1b0 那种浅灰
        fontSize: 12,
        lineHeight: 15,
        width: 220,              // 给足宽度，容纳绝大多数实体全称
        overflow: 'break',       // 仍放不下就换行，绝不截断成省略号
      },
    },
    series: [{
      type: 'bar',
      data: data.map((r) => r.value),
      barMaxWidth: 16,
      itemStyle: { borderRadius: [0, 4, 4, 0], color },
      label: {
        show: true, position: 'right',
        color: '#374151', fontSize: 12, fontWeight: 500,
        formatter: (p) => `${p.value}${unit}`,
      },
    }],
  }
}

/** 关系图：中心实体 ↔ 关联实体，用节点大小编码频次 */
const graphOption = (center, nodes, color = '#2563eb') => ({
  tooltip: {
    formatter: (p) => (p.dataType === 'edge'
      ? `${p.data.source} — ${p.data.target}<br/>共同投标：${p.data.value} 次`
      : `${p.name}`),
  },
  series: [{
    type: 'graph', layout: 'force', roam: true, draggable: true,
    data: [
      { name: center, symbolSize: 48, itemStyle: { color: '#1d4ed8' },
        label: { show: true, fontSize: 12, fontWeight: 600, color: '#1f2430' } },
      ...nodes.map((n) => ({
        name: n.name,
        symbolSize: Math.max(14, Math.min(40, 12 + n.value * 4)),
        itemStyle: { color },
        label: { show: nodes.length <= 8, fontSize: 11, color: '#374151' },
      })),
    ],
    links: nodes.map((n) => ({ source: center, target: n.name, value: n.value })),
    force: { repulsion: 280, edgeLength: [90, 160], gravity: 0.09 },
    lineStyle: { color: '#b6cbf0', width: 1.4, curveness: 0.06 },
    emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
    // 节点较多时关闭标签，保证渲染速度（查询效率计入前端渲染）
    labelLayout: { hideOverlap: true },
  }],
})

const chartTitle = computed(() => ({
  s1: '合作次数', s2: '高频投标方',
  s3: '竞对关系', s4: '共同采购方',
  // 下图 s5 的 series 用 `amount / 10000`，数值轴单位就是万元 -> 标题必须写明单位
  s5: '项目金额（万元）',
}[activeId.value] || '结果'))

const chartOption = computed(() => {
  const r = result.value
  if (!r) return null
  switch (activeId.value) {
    case 's1': {
      const rows = (r.suppliers || []).slice(0, 12)
        .map((x) => ({ name: x.name, value: x.cooperate_count }))
      return rows.length ? barOption(rows, '#2563eb', ' 次') : null
    }
    case 's2': {
      const rows = (r.top_bidders || []).map((x) => ({ name: x.name, value: x.bid_count }))
      return rows.length ? graphOption(r.purchaser, rows) : null
    }
    case 's3': {
      const rows = (r.top_competitors || []).map((x) => ({ name: x.name, value: x.bid_count }))
      return rows.length ? graphOption(r.supplier, rows, '#0d9488') : null
    }
    case 's4': {
      const rows = (r.purchasers || []).slice(0, 12)
        .map((x) => ({ name: x.name, value: x.cooperate_count }))
      return rows.length ? barOption(rows, '#7c3aed', ' 次') : null
    }
    case 's5': {
      // 横轴 = 项目金额（万元），故坐标值除以 1 万，标题与 tooltip/轴刻度都标注「万元」
      const rows = (r.projects || []).slice(0, 12)
        .map((x) => ({ name: x.project_name, value: Math.round(x.amount / 10000) }))
      return rows.length ? barOption(rows, '#ea580c', ' 万元') : null
    }
    default:
      return null
  }
})

/** 条形图的类目名会换行，行数多时需要更高的画布，否则每行挤在一起 */
const chartHeight = computed(() => {
  if (activeId.value === 's2' || activeId.value === 's3') return 340   // 关系图
  const n = Math.min(12, Math.max(
    (result.value?.suppliers || []).length,
    (result.value?.purchasers || []).length,
    (result.value?.projects || []).length,
  ))
  return Math.max(300, Math.min(460, n * 34 + 40))
})

const loadDefs = async () => {
  try {
    scenarios.value = (await getScenarios()) || []
    if (scenarios.value.length) {
      activeId.value = scenarios.value[0].id
      resetForm()
    }
  } catch (e) { /* ignore */ }
}

onMounted(loadDefs)

// 数据版本变化 / 离开再回来：重取当前场景结果（明细组件 S1~S5 由 result 驱动，自动跟着更新）
useDataFreshness(refreshResult)
</script>

<style scoped>
/* ---- 场景切换分段控件 ---- */
.edm-seg {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.edm-seg__item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border: 1px solid var(--edm-border);
  background: var(--edm-surface);
  border-radius: 999px;
  cursor: pointer;
  font-size: 13px;
  color: var(--edm-text-2);
  transition: all .16s ease;
  font-family: inherit;
}
.edm-seg__item:hover { border-color: #bcd0f5; color: var(--edm-primary); }
.edm-seg__item.is-active {
  background: var(--edm-primary);
  border-color: var(--edm-primary);
  color: #fff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, .25);
}
.edm-seg__badge {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: .3px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--edm-primary-soft);
  color: var(--edm-primary);
}
.edm-seg__item.is-active .edm-seg__badge {
  background: rgba(255, 255, 255, .22);
  color: #fff;
}

/* ---- 工作台两栏 ---- */
.edm-workbench {
  display: grid;
  grid-template-columns: 310px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
@media (max-width: 1000px) {
  .edm-workbench { grid-template-columns: 1fr; }
}

.edm-panel {
  background: var(--edm-surface);
  border: 1px solid var(--edm-border);
  border-radius: var(--edm-radius);
  box-shadow: var(--edm-shadow);
  padding: 16px 18px;
  position: sticky;
  top: 12px;
}
.edm-panel__title { font-size: 13.5px; font-weight: 650; margin-bottom: 6px; }
.edm-panel__desc {
  font-size: 12px; color: var(--edm-text-3); line-height: 1.7;
  margin-bottom: 14px; padding-bottom: 12px;
  border-bottom: 1px dashed var(--edm-border);
}
.edm-panel__label { display: inline-flex; align-items: center; gap: 4px; }
.edm-panel__hint {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--edm-warning); margin-bottom: 8px;
}
.edm-result { min-width: 0; }
</style>
