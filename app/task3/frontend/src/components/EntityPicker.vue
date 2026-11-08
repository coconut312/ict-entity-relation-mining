<template>
  <el-select :popper-options="SELECT_POPPER" :offset="6"
    :model-value="modelValue"
    :multiple="multiple"
    filterable
    remote
    reserve-keyword
    clearable
    collapse-tags
    collapse-tags-tooltip
    :remote-method="search"
    :loading="loading"
    :placeholder="placeholder"
    style="width: 100%"
    @update:model-value="onChange"
  >
    <el-option
      v-for="it in options"
      :key="it.id"
      :label="it.name"
      :value="it.name"
    >
      <span style="float:left">{{ it.name }}</span>
      <span style="float:right;color:var(--edm-text-3);font-size:12px">
        {{ it.count }} 条
      </span>
    </el-option>
    <template #empty>
      <div style="padding:12px;text-align:center;color:var(--edm-text-3);font-size:12.5px">
        输入关键词搜索
      </div>
    </template>
  </el-select>
</template>

<script setup>
/**
 * 实体选择器：从后端做远程搜索，而不是把全部实体拉到前端。
 *
 * 为什么重要：真实数据集里实体可能有上万个，一次性拉全量会拖慢首屏、
 * 也违背「响应高效」的要求。远程搜索是这里唯一正确的做法。
 */
import { onMounted, ref } from 'vue'
import { suggestEntities } from '@/api'
import { useDataFreshness } from '@/utils/dataVersion'
import { SELECT_POPPER } from '@/utils/popper'

const props = defineProps({
  modelValue: { type: [String, Array], default: '' },
  type: { type: String, default: 'purchaser' }, // 'purchaser' | 'supplier'
  multiple: { type: Boolean, default: false },
  placeholder: { type: String, default: '输入名称搜索' },
})

const emit = defineEmits(['update:modelValue'])

const options = ref([])
const loading = ref(false)
let timer = null
let lastKeyword = ''      // 记住最近一次搜索词，数据变过之后用它重查

const search = (kw) => {
  lastKeyword = kw || ''
  clearTimeout(timer)
  // 防抖：避免每敲一个字都打一次接口（这也是查询效率的一部分）
  timer = setTimeout(async () => {
    loading.value = true
    try {
      options.value = (await suggestEntities(lastKeyword, props.type)) || []
    } catch (e) {
      options.value = []
    } finally {
      loading.value = false
    }
  }, 220)
}

const onChange = (v) => emit('update:modelValue', v)

onMounted(() => search(''))

/**
 * 实体补全的候选来自库里已有的实体（supplier_portrait / purchaser_portrait），
 * 上传新数据后候选也会变 —— 所以数据版本一变化（或在场景页离开再回来）就按
 * 最近一次的关键词重查一遍，用户不必重新输入关键词才能看到新实体。
 */
useDataFreshness(() => search(lastKeyword))
</script>
