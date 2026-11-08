<template>
  <div class="edm-stat">
    <div class="edm-stat__label">
      <el-icon v-if="icon" :size="13"><component :is="icon" /></el-icon>
      {{ label }}
    </div>
    <div class="edm-stat__value">
      {{ display }}<span v-if="unit" class="edm-stat__unit">{{ unit }}</span>
    </div>
    <div v-if="foot" class="edm-stat__foot">{{ foot }}</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, default: '' },
  value: { type: [Number, String], default: 0 },
  unit: { type: String, default: '' },
  foot: { type: String, default: '' },
  icon: { type: String, default: '' },
  /** 超过 1 万时自动用「万」显示，避免大数字挤爆卡片 */
  autoUnit: { type: Boolean, default: true },
  decimals: { type: Number, default: 0 },
})

const display = computed(() => {
  const v = props.value
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'string') return v
  if (props.autoUnit && Math.abs(v) >= 10000) {
    return (v / 10000).toFixed(1) + ' 万'
  }
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: props.decimals,
    maximumFractionDigits: props.decimals,
  })
})
</script>
