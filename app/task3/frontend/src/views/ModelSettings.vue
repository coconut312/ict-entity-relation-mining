<template>
  <div>
    <div class="edm-page-head">
      <div>
        <h2 class="edm-page-title">模型配置 / API Key</h2>
        <p class="edm-page-desc">
          在此填入自己的 API Key，保存后立刻生效（无需重启后端）。
          上传数据集解析将使用该 Key，未填写时自动回退规则层抽取。
          Key 只回显末 4 位掩码，不显示明文。
        </p>
      </div>
      <div class="edm-toolbar">
        <el-tag :type="sourceTag(text.source)" effect="light" size="small" round>
          {{ text.has_key ? text.source : '未配置 Key' }}
        </el-tag>
      </div>
    </div>

    <el-alert
      v-if="!loading && !text.has_key"
      type="warning" :closable="false" show-icon
      title="尚未配置文本模型 API Key"
      description="未配置时数据集处理会退回规则层抽取（不调用大模型）。填入 Key 并保存即可启用 LLM 抽取。"
    />

    <!-- ============ 文本模型 ============ -->
    <div class="edm-card" style="margin-top:14px">
      <div class="edm-card__head">
        <div class="edm-card__title">文本模型（实体与关系抽取）</div>
        <span class="edm-dim" style="font-size:12px">
          Base URL / 模型名留空即沿用环境变量或默认值
        </span>
      </div>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key" type="password" show-password clearable
            autocomplete="new-password"
            placeholder="粘贴以 sk- 开头的 Key；留空表示不修改"
          />
          <div class="edm-ms__hint">
            当前：
            <span class="edm-mono">{{ text.key_masked || '未配置' }}</span>
            <template v-if="text.has_key">（来源：{{ text.source }}）</template>
          </div>
        </el-form-item>

        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>

        <el-form-item label="模型名">
          <el-input v-model="form.model" placeholder="deepseek-flash" />
        </el-form-item>
      </el-form>

      <!-- 视觉模型默认折叠：多数场景只配文本模型 -->
      <el-collapse v-model="openVision" class="edm-ms__collapse">
        <el-collapse-item name="vision">
          <template #title>
            <span class="edm-ms__collapse-title">
              视觉模型（扫描件 / 图片 → 文本）
              <el-tag size="small" effect="plain" :type="visionTipTag">
                {{ visionTip }}
              </el-tag>
            </span>
          </template>

          <el-form label-position="top" @submit.prevent>
            <el-form-item label="API Key">
              <el-input
                v-model="form.vision_api_key" type="password" show-password clearable
                autocomplete="new-password"
                placeholder="留空表示不修改；不填则沿用文本模型的 Key"
              />
              <div class="edm-ms__hint">
                当前：
                <span class="edm-mono">{{ vision.key_masked || '未配置' }}</span>
                <template v-if="vision.has_key">（来源：{{ vision.source }}）</template>
              </div>
            </el-form-item>

            <el-form-item label="Base URL">
              <el-input v-model="form.vision_base_url"
                        placeholder="https://api.deepseek.com/v1" />
            </el-form-item>

            <el-form-item label="模型名">
              <el-input v-model="form.vision_model" placeholder="deepseek-flash" />
            </el-form-item>
          </el-form>
        </el-collapse-item>
      </el-collapse>

      <div class="edm-toolbar edm-ms__actions">
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
        <el-button :loading="testing" @click="test">测试连接</el-button>
        <el-button plain :loading="clearing" @click="clear">清除</el-button>
        <span class="edm-dim" style="font-size:12px">
          测试连接用的是已保存的生效配置（不是输入框里未保存的内容）；清除后平台不再使用任何 Key，上传解析回退规则层抽取
        </span>
      </div>
    </div>

    <!-- ============ 测试结果 ============ -->
    <el-alert
      v-if="result"
      class="edm-ms__result"
      :type="result.ok ? 'success' : 'error'"
      :title="result.ok
        ? `连接成功 · ${result.model} · ${result.latency_ms} ms`
        : '连接失败'"
      show-icon
      @close="result = null"
    >
      <div class="edm-ms__result-body">
        <div v-if="result.ok">Base URL：{{ result.base_url }}</div>
        <div v-else>{{ result.error || '未知错误' }}</div>
      </div>
    </el-alert>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  clearModelSettings, getModelSettings, saveModelSettings, testModelSettings,
} from '@/api'

const EMPTY_GROUP = {
  base_url: '', model: '', has_key: false, key_masked: '', source: '未配置',
}
const PLAIN_FIELDS = ['base_url', 'model', 'vision_base_url', 'vision_model']

const text = ref({ ...EMPTY_GROUP })
const vision = ref({ ...EMPTY_GROUP })
const result = ref(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const clearing = ref(false)
const openVision = ref([])

// 表单里的**非密钥字段**预填生效值：清空某个输入框即表示「清除该项覆盖」。
// 密钥输入框一律留空，绝不回填明文，也绝不用掩码顶替。
const form = reactive({
  api_key: '', base_url: '', model: '',
  vision_api_key: '', vision_base_url: '', vision_model: '',
})
// 载入时的取值快照，用来判断哪些字段被改动过（只提交改动，避免把生效值固化成覆盖）
const snapshot = reactive({ ...form })

const sourceTag = (s) =>
  (s === '页面输入' ? 'success' : s === '环境变量' ? 'info' : 'danger')

const visionTip = computed(() => {
  if (!vision.value.has_key) {
    return text.value.has_key ? '未配置（将沿用文本 Key）' : '未配置'
  }
  // 视觉 key 与文本 key 相同 => 实际是沿用了文本那份，说清楚而不是假装单独配过
  if (text.value.has_key && vision.value.key_masked === text.value.key_masked) {
    return `沿用文本 Key（${vision.value.key_masked}）`
  }
  return `已配置 ${vision.value.key_masked}`
})
const visionTipTag = computed(() =>
  (vision.value.has_key ? 'success' : text.value.has_key ? 'info' : 'warning'))

/** 用接口返回的生效值刷新界面 */
const apply = (d) => {
  const v = d.vision || EMPTY_GROUP
  text.value = {
    base_url: d.base_url || '', model: d.model || '',
    has_key: !!d.has_key, key_masked: d.key_masked || '',
    source: d.source || '未配置',
  }
  vision.value = {
    base_url: v.base_url || '', model: v.model || '',
    has_key: !!v.has_key, key_masked: v.key_masked || '',
    source: v.source || '未配置',
  }
  form.api_key = ''
  form.vision_api_key = ''
  form.base_url = text.value.base_url
  form.model = text.value.model
  form.vision_base_url = vision.value.base_url
  form.vision_model = vision.value.model
  Object.assign(snapshot, form)
}

const load = async () => {
  loading.value = true
  try {
    apply(await getModelSettings())
  } catch (e) { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

/** 是否有未保存的内容（有则重新进页面时不覆盖，避免丢输入） */
const dirty = () =>
  !!form.api_key.trim() || !!form.vision_api_key.trim()
  || PLAIN_FIELDS.some((k) => form[k] !== snapshot[k])

/** 组装 PUT body：字段缺省=不改；空字符串=清除该项 */
const buildPatch = () => {
  const p = {}
  if (form.api_key.trim()) p.api_key = form.api_key.trim()
  if (form.vision_api_key.trim()) p.vision_api_key = form.vision_api_key.trim()
  PLAIN_FIELDS.forEach((k) => {
    if (form[k] !== snapshot[k]) p[k] = form[k].trim()
  })
  return p
}

const badUrl = (v) => !!v && !/^https?:\/\//i.test(v)

const save = async () => {
  const patch = buildPatch()
  if (!Object.keys(patch).length) {
    ElMessage.info('没有需要保存的改动')
    return
  }
  if (badUrl(patch.base_url) || badUrl(patch.vision_base_url)) {
    ElMessage.warning('Base URL 需以 http:// 或 https:// 开头')
    return
  }
  saving.value = true
  result.value = null
  try {
    apply(await saveModelSettings(patch))
    ElMessage.success('已保存并即时生效')
  } catch (e) { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

const test = async () => {
  testing.value = true
  try {
    result.value = await testModelSettings()
  } catch (e) { /* 拦截器已提示 */ } finally {
    testing.value = false
  }
}

const clear = async () => {
  try {
    await ElMessageBox.confirm(
      '将删除页面上保存的模型配置。清除后平台不再使用任何 Key（不读环境变量），上传解析回退规则层抽取。是否继续？',
      '清除模型配置',
      { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
    )
  } catch (e) {
    return                                  // 用户取消
  }
  clearing.value = true
  result.value = null
  try {
    apply(await clearModelSettings())
    ElMessage.success('已清除：平台不再使用任何 Key，上传解析回退规则层')
  } catch (e) { /* 拦截器已提示 */ } finally {
    clearing.value = false
  }
}

onMounted(load)
// 页面被 keep-alive 缓存：返回时若没有未保存输入就刷新一次
onActivated(() => { if (!dirty()) load() })
</script>

<style scoped>
.edm-ms__hint {
  font-size: 12px;
  color: var(--edm-text-3);
  line-height: 1.7;
  margin-top: 4px;
}
.edm-ms__collapse {
  margin-top: 2px;
}
.edm-ms__collapse-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
}
.edm-ms__actions {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--edm-border);
}
.edm-ms__result {
  margin-top: 14px;
}
.edm-ms__result-body {
  font-size: 12.5px;
  line-height: 1.7;
  word-break: break-all;
}
</style>
