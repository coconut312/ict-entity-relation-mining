<template>
  <div class="edm-card">
    <div class="edm-card__head">
      <div class="edm-card__title">{{ title }}</div>
      <div class="edm-toolbar edm-toolbar--right">
        <slot name="extra" />
      </div>
    </div>

    <div v-if="empty" class="edm-empty">
      <el-icon :size="34" color="#c4cad6"><DataLine /></el-icon>
      <div class="edm-empty__title">{{ emptyTitle }}</div>
      <div class="edm-empty__hint">{{ emptyHint }}</div>
    </div>

    <template v-else>
      <!-- 图表容器：v-show 而非 v-if，避免切换 tab 时反复销毁重建 ECharts -->
      <div v-show="type !== 'table'" ref="el" class="edm-chart" :style="{ height: height + 'px' }" />
      <div v-show="type === 'table'">
        <slot />
      </div>
    </template>
  </div>
</template>

<script setup>
/**
 * 图表容器：封装 ECharts 的生命周期。
 *
 * 渲染性能考虑（前端渲染时间计入响应耗时）：
 *   * 用 ResizeObserver 而不是 window.resize，避免多图时全量重算
 *   * dispose 在 onBeforeUnmount 里做，切页不留内存
 *   * 默认关闭动画，图形较大时渲染更快
 */
import { onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  title: { type: String, default: '' },
  /** ECharts option；为 null 且 empty=false 时显示空态 */
  option: { type: Object, default: null },
  height: { type: Number, default: 320 },
  empty: { type: Boolean, default: false },
  emptyTitle: { type: String, default: '暂无数据' },
  emptyHint: { type: String, default: '请先设置查询条件' },
  /** 'chart' | 'table'：table 模式下只渲染默认插槽 */
  type: { type: String, default: 'chart' },
  /**
   * 图表动画时长（毫秒）。设 0 关闭。
   *
   * 这个值需要权衡：动画能让数据变化更易读，但 ECharts 的动画时间
   * 属于「结果完全渲染完毕」的一部分。场景分析页是被计时的查询路径，
   * 那里显式传了更短的值（见 ScenarioWorkbench.vue）。
   */
  animateMs: { type: Number, default: 320 },
})

const el = ref(null)
let chart = null
let ro = null

const render = async () => {
  if (props.type === 'table' || props.empty || !props.option) return
  await nextTick()
  if (!el.value) return
  if (!chart) {
    chart = echarts.init(el.value, null, { renderer: 'canvas' })
  }
  const ms = Math.max(0, props.animateMs)
  chart.setOption(
    {
      animation: ms > 0,
      animationDuration: ms,
      animationDurationUpdate: Math.round(ms * 0.6),
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      ...props.option,
    },
    { notMerge: true, lazyUpdate: true },
  )
  chart.resize()
}

onMounted(() => {
  render()
  if (window.ResizeObserver && el.value) {
    ro = new ResizeObserver(() => chart && chart.resize())
    ro.observe(el.value)
  }
})

watch(() => [props.option, props.empty, props.type], render, { deep: false })

onBeforeUnmount(() => {
  if (ro) ro.disconnect()
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>
