<template>
  <div>
    <div class="edm-page-head">
      <div>
        <h2 class="edm-page-title">数据总览</h2>
        <p class="edm-page-desc">
          上传公告数据集，自动解析并入库。本页汇总数据规模与质量。
        </p>
      </div>
    </div>

    <!-- ============ 数据集导入与处理 ============ -->
    <div class="edm-card">
      <div class="edm-card__head">
        <div class="edm-card__title">数据集处理</div>
        <div class="edm-toolbar--right edm-toolbar">
          <span class="edm-dim" style="font-size:12px">
            {{ UPLOAD_SUPPORT_LINE }}
          </span>
          <!-- 选择文件夹：<input webkitdirectory> 会把每个文件的相对路径放进
               file.webkitRelativePath（形如 sub/dir/a.html），走 /dataset/import-folder -->
          <el-button size="small" :disabled="running"
                     data-testid="pick-folder" @click="pickFolder">
            <el-icon style="margin-right:4px"><FolderOpened /></el-icon>
            选择文件夹
          </el-button>
          <!-- 提取结果下载：现场读平台当前库（= 上传解析结果）生成官方三表，见 backend/app/export_api.py -->
          <el-button type="primary" size="small"
                     :loading="exportingXlsx" :disabled="running"
                     @click="onDownloadOfficial">
            <el-icon style="margin-right:4px"><Download /></el-icon>
            下载提取结果（官方格式）
          </el-button>
        </div>
      </div>

      <!-- 隐藏的文件夹选择框：只由「选择文件夹」按钮触发 -->
      <input
        ref="folderInput"
        class="edm-hidden-input"
        type="file"
        webkitdirectory
        directory
        multiple
        @change="onFolderPick"
      >

      <!-- 拖拽区：DOM 事件在**捕获阶段**被接管（见 onMounted 的说明），
           这样 el-upload 自带的 drop 处理不会再吞掉文件夹（它只认普通文件）。
           `multiple` 让文件选择器可一次多选（多个 .html 公告就是这么进来的）；
           `accept` 与 utils/htmlUpload.js 的 UPLOAD_ACCEPT 同字面量（单测逐字比对）：
           .html/.htm 与 .zip/.xlsx/.csv 同级放行。 -->
      <div ref="dropZone">
        <el-upload
          class="edm-drop"
          drag
          multiple
          :auto-upload="false"
          :show-file-list="false"
          accept=".zip,.rar,.7z,.xlsx,.csv,.html,.htm"
          :on-change="onFileChange"
          :disabled="running"
        >
          <div class="edm-drop__inner">
            <el-icon class="edm-drop__icon"><UploadFilled /></el-icon>
            <div>
              <div class="edm-drop__title">
                {{ running ? '正在处理，请稍候…' : '拖入文件夹或压缩包，或点击选择' }}
              </div>
              <div class="edm-drop__hint">
                {{ HTML_DROP_HINT }} · {{ OVERWRITE_DROP_HINT }}
              </div>
            </div>
          </div>
        </el-upload>
      </div>

      <!-- 上传前先让用户看清要传什么：已选 N 个文件 / 合计 X MB -->
      <div v-if="selection" class="edm-dim edm-selection">
        {{ selection }}
      </div>

      <!-- 进度：上传 + 处理，合并为一条，减少状态感知成本 -->
      <div v-if="running || job" class="edm-progress">
        <div class="edm-progress__meta">
          <el-tag :type="statusTag(job?.status)" effect="light" size="small">
            {{ job ? statusText(job.status) : '上传中' }}
          </el-tag>
          <span class="edm-muted">{{ job?.stage || `正在上传 ${uploadPct}%` }}</span>
          <span class="edm-toolbar--right edm-perf">
            <el-icon><Timer /></el-icon>
            {{ ((job?.elapsed_ms || 0) / 1000).toFixed(1) }}s
            <template v-if="perDoc">
              · 单篇 {{ perDoc }} ms
            </template>
          </span>
        </div>
        <el-progress
          :percentage="job ? Math.round((job.progress || 0) * 100) : uploadPct"
          :status="job?.status === 'failed' ? 'exception'
                 : job?.status === 'succeeded' ? 'success' : ''"
          :stroke-width="8"
          :show-text="false"
        />
        <div v-if="job?.message" class="edm-dim" style="font-size:12px;margin-top:8px">
          {{ job.message }}
        </div>
      </div>

      <!-- 恢复默认数据：上传小样本后一键回到仓库内置的完整交付集。
           说明里写清「当前规模」与「恢复目标」，用户点之前就知道会发生什么。 -->
      <div class="edm-restore">
        <div class="edm-restore__hint">
          当前规模：<b>{{ stats.n_docs }}</b> 篇公告 / <b>{{ stats.n_items }}</b> 条标的。
        </div>
        <el-button
          type="warning" plain size="small"
          :loading="restoring" :disabled="running || restoring"
          data-testid="restore-default"
          @click="onRestoreDefault"
        >
          <el-icon style="margin-right:4px"><RefreshLeft /></el-icon>
          {{ restoring ? '正在恢复…' : '恢复默认数据' }}
        </el-button>
      </div>
    </div>

    <!-- 无数据时的引导：不让用户面对一片零值 -->
    <el-alert
      v-if="!loading && !stats.n_items"
      type="info" :closable="false" show-icon
      title="尚未导入数据"
      style="margin-top:14px"
    >
      <div style="font-size:12.5px;line-height:1.7">
        将公告数据集文件夹（或压缩包）拖入上方区域即可开始处理，处理完成后本页会自动刷新。
      </div>
    </el-alert>

    <!-- ============ 规模指标 ============ -->
    <div class="edm-stat-grid" v-loading="loading" style="margin-top:14px">
      <StatCard label="公告篇数" :value="stats.n_docs" icon="Document" />
      <StatCard label="标的信息" :value="stats.n_items" icon="Goods" />
      <StatCard label="采购方" :value="stats.n_purchasers" icon="OfficeBuilding" />
      <StatCard label="供应商" :value="stats.n_suppliers" icon="User" />
      <StatCard label="品牌数" :value="stats.n_brands" icon="PriceTag" />
      <StatCard label="单篇耗时" :value="stats.avg_process_ms" unit="ms"
                :auto-unit="false" icon="Timer" />
    </div>

    <!-- ============ 数据质量可视化 ============ -->
    <el-row :gutter="14" style="margin-top:14px">
      <el-col :xs="24" :md="10">
        <ChartPanel
          title="字段完整度"
          :option="fillOption" :empty="!stats.n_items"
          empty-title="暂无数据" empty-hint="上传数据集后自动统计" :height="290"
        />
      </el-col>
      <el-col :xs="24" :md="7">
        <ChartPanel
          title="品目分布"
          :option="categoryOption" :empty="!stats.n_items"
          empty-title="暂无数据" :height="290"
        />
      </el-col>
      <el-col :xs="24" :md="7">
        <ChartPanel
          title="品牌 TOP"
          :option="brandOption" :empty="!stats.n_items"
          empty-title="暂无数据" :height="290"
        />
      </el-col>
    </el-row>

    <!-- 覆盖范围 -->
    <div class="edm-card" style="margin-top:14px">
      <div class="edm-card__head"><div class="edm-card__title">数据范围</div></div>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="最早公告">{{ stats.date_min || '—' }}</el-descriptions-item>
        <el-descriptions-item label="最晚公告">{{ stats.date_max || '—' }}</el-descriptions-item>
        <el-descriptions-item label="数据源">
          <el-tag size="small" effect="plain"
                  :type="health?.data_backend === 'real' ? 'success' : 'info'">
            {{ health?.data_backend === 'real' ? '已接入' : '未接入' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="接口最近耗时">
          <span class="edm-perf" :class="perfMs != null && perfMs < 1000 ? 'edm-perf--good' : ''">
            {{ perfMs == null ? '—' : perfMs + ' ms' }}
          </span>
        </el-descriptions-item>
      </el-descriptions>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, FolderOpened, RefreshLeft, Timer, UploadFilled } from '@element-plus/icons-vue'
import StatCard from '@/components/StatCard.vue'
import ChartPanel from '@/components/ChartPanel.vue'
import {
  downloadOfficial, getDatasetStats, getHealth,
  getImportJob, getItemFacets, restoreDefaultDataset, uploadDataset,
  uploadDatasetFolder,
} from '@/api'
import { perf } from '@/api/client'
import { bumpDataVersion, useDataFreshness } from '@/utils/dataVersion'
import {
  FOLDER_DRAG_UNSUPPORTED_TIP,
  MODE_APPEND, OVERWRITE_CANCELLED_TIP, OVERWRITE_CONFIRM_TEXT, OVERWRITE_DROP_HINT,
  OVERWRITE_KEEP_TEXT, OVERWRITE_TITLE,
  UPLOAD_ACTION_CLOSE, UPLOAD_ACTION_CONFIRM,
  decideUploadMode, needsOverwriteConfirm, overwriteConfirmMessage, selectionText,
} from '@/utils/overwriteGuard'
import {
  HTML_DROP_HINT, KIND_NONE, KIND_SINGLE,
  UPLOAD_SUPPORT_LINE, planUpload,
} from '@/utils/htmlUpload'

const stats = ref({
  n_docs: 0, n_items: 0, n_suppliers: 0, n_purchasers: 0, n_brands: 0,
  total_amount: 0, date_min: null, date_max: null,
  avg_process_ms: null, field_fill_rate: {},
})
const facets = ref({ categories: [], brands: [] })
const health = ref(null)
const loading = ref(false)
const perfMs = ref(null)

const job = ref(null)
const uploadPct = ref(0)
const running = ref(false)
/** stats 是否已成功加载过：判断「当前库空不空」的前提 */
const statsLoaded = ref(false)
/**
 * 上传闸门：同步置位，覆盖「确认框还没点完」的时间窗。
 * running 要等确认通过才置位（弹框期间不该显示“正在处理”），
 * 所以必须另有一个在 await 之前就生效的锁，否则连拖两次会弹两个框 / 发两次上传。
 */
const uploadBusy = ref(false)
/** 「恢复默认数据」进行中：按钮 loading/禁用并显示「正在恢复…」（本机实测约 2 秒，低配机器更久） */
const restoring = ref(false)
let pollTimer = null

const perDoc = computed(() => {
  const j = job.value
  if (!j?.elapsed_ms || !j?.processed_docs) return null
  return Math.round(j.elapsed_ms / j.processed_docs)
})

const STATUS = {
  pending: ['排队中', 'info'],
  running: ['处理中', 'warning'],
  succeeded: ['已完成', 'success'],
  failed: ['失败', 'danger'],
}
const statusText = (s) => (STATUS[s] || ['进行中', 'info'])[0]
const statusTag = (s) => (STATUS[s] || ['info'])[1]

/* ---------------- 图表 ---------------- */

const FIELD_LABELS = {
  product_name: '产品服务名称', category: '品目', brand: '品牌',
  spec: '规格型号', unit_price: '单价', quantity: '数量', total_price: '总价',
}

const fillOption = computed(() => {
  const rate = stats.value.field_fill_rate || {}
  const keys = Object.keys(FIELD_LABELS)
  const vals = keys.map((k) => Math.round((rate[k] || 0) * 1000) / 10)
  return {
    grid: { left: 6, right: 40, top: 10, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: (v) => v + '%' },
    xAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%', color: '#4b5563', fontSize: 12 },
             splitLine: { lineStyle: { color: '#eef1f6' } } },
    yAxis: { type: 'category', data: keys.map((k) => FIELD_LABELS[k]).reverse(),
             axisTick: { show: false }, axisLine: { show: false },
             axisLabel: { color: '#374151', fontSize: 12 } },
    series: [{
      type: 'bar', data: vals.reverse(), barWidth: 12,
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: (p) => (p.value >= 90 ? '#16a34a' : p.value >= 60 ? '#2563eb' : '#f59e0b'),
      },
      label: { show: true, position: 'right', formatter: '{c}%',
               color: '#374151', fontSize: 12, fontWeight: 500 },
    }],
  }
})

const categoryOption = computed(() => {
  const rows = (facets.value.categories || []).slice(0, 6)
  return {
    tooltip: { trigger: 'item', formatter: '{b}<br/>{c} 条（{d}%）' },
    legend: { bottom: 0, icon: 'circle', itemWidth: 8, itemHeight: 8,
              textStyle: { color: '#374151', fontSize: 12 } },
    color: ['#2563eb', '#4f8ef7', '#7c3aed', '#0d9488', '#f59e0b', '#94a3b8'],
    series: [{
      type: 'pie', radius: ['46%', '70%'], center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: false }, labelLine: { show: false },
      data: rows.map((r) => ({ name: r.value, value: r.count })),
    }],
  }
})

const brandOption = computed(() => {
  const rows = (facets.value.brands || []).slice(0, 8).reverse()
  return {
    grid: { left: 6, right: 40, top: 10, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f6' } },
             axisLine: { show: false }, axisTick: { show: false },
             axisLabel: { color: '#4b5563', fontSize: 12 } },
    yAxis: { type: 'category', data: rows.map((r) => r.value),
             axisTick: { show: false }, axisLine: { show: false },
             // 品牌名可能很长（如"广州广电五舟科技股份有限公司"），换行而不截断
             axisLabel: { color: '#374151', fontSize: 12, lineHeight: 15,
                          width: 120, overflow: 'break' } },
    series: [{
      type: 'bar', data: rows.map((r) => r.count), barMaxWidth: 12,
      itemStyle: { borderRadius: [0, 4, 4, 0], color: '#4f8ef7' },
      label: { show: true, position: 'right',
               color: '#374151', fontSize: 12, fontWeight: 500 },
    }],
  }
})

/* ---------------- 数据加载 ---------------- */

const load = async () => {
  loading.value = true
  try {
    const [s, f, h] = await Promise.all([
      getDatasetStats(), getItemFacets(), getHealth(),
    ])
    stats.value = s
    facets.value = f
    health.value = h
    perfMs.value = perf.lastMs
    statsLoaded.value = true
  } catch (e) { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

const stopPoll = () => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

const startPoll = (jobId) => {
  stopPoll()
  pollTimer = setInterval(async () => {
    try {
      const j = await getImportJob(jobId)
      job.value = j
      if (j.status === 'succeeded' || j.status === 'failed') {
        stopPoll()
        running.value = false
        if (j.status === 'succeeded') {
          // 数据已经真正落库了 -> 广播一次数据版本。
          // 本页自己的统计、标的检索、场景结果、实体补全都监听它自动重查，
          // 用户不再需要手动刷新（这里**不再单独调 load()**，否则本页会查两遍）。
          bumpDataVersion()
          await load()   // 本页 6 个规模卡片立即重查（bump 只通知其它页面）
        }
        ElMessage[j.status === 'succeeded' ? 'success' : 'error'](
          j.status === 'succeeded' ? '数据集处理完成' : '数据集处理失败')
      }
    } catch (e) {
      stopPoll(); running.value = false
    }
  }, 700)
}

/**
 * 取「当前库规模」用于覆盖判定：页面已加载过 stats 就直接用，
 * 否则补一次 GET /dataset/stats（例如首屏还没回来就拖了文件）。
 */
const resolveCurrentStats = async () => {
  if (statsLoaded.value) return stats.value
  try {
    const s = await getDatasetStats()
    stats.value = s
    statsLoaded.value = true
    return s
  } catch (e) {
    // 兜底：拿不到规模就沿用本地已有值（首次进入时即全 0 -> 不弹框）
    return stats.value
  }
}

/**
 * 统一「开始上传」入口 —— 拖拽（普通文件 / 文件夹）、点击选择文件、选择文件夹
 * 三条路径都走这里。
 *
 * 覆盖确认的语义只在这一处（utils/overwriteGuard.js 的 decideUploadMode 决定，
 * 有单测钉住），所以「选择文件夹」不可能绕过覆盖确认：
 *   * 点「覆盖并上传」      -> mode=replace，替换当前平台数据
 *   * 点「保留原数据并解析」-> mode=append，**照样解析新文件**，结果追加到现有数据之后
 *   * 点 X / 按 Esc（close）-> 真正放弃：不发请求、平台原数据保持不变
 *
 * @param {(mode: string, onProgress: Function) => Promise<object>} send
 *        真正的上传动作（单文件 / 文件夹两种通道），返回后端 ImportJob
 */
const runUpload = async (send) => {
  if (uploadBusy.value || running.value) return   // 防重复提交：连拖两次只处理一次
  uploadBusy.value = true
  try {
    const snapshot = await resolveCurrentStats()
    const needsConfirm = needsOverwriteConfirm(snapshot)
    let action = null
    if (needsConfirm) {
      try {
        // 有数据才问。两个按钮都是「继续」，只是入库方式不同：
        // 确认 = 覆盖并上传；取消按钮 = 保留原数据并解析（仍然上传）。
        await ElMessageBox.confirm(
          overwriteConfirmMessage(snapshot, selection.value), OVERWRITE_TITLE, {
            confirmButtonText: OVERWRITE_CONFIRM_TEXT,
            cancelButtonText: OVERWRITE_KEEP_TEXT,
            type: 'warning',
            // 关键：区分「点取消按钮」与「点 X / 按 Esc」——
            // 没有它两者都以 reject('cancel') 返回，就无法让取消按钮承担「追加」语义。
            distinguishCancelAndClose: true,
          })
        action = UPLOAD_ACTION_CONFIRM
      } catch (e) {
        // distinguishCancelAndClose 下：'cancel' = 点取消按钮；'close' = X / Esc。
        action = typeof e === 'string' ? e : UPLOAD_ACTION_CLOSE
      }
    }

    const { proceed, mode } = decideUploadMode({ needsConfirm, action })
    if (!proceed) {
      // 只有 X / Esc 才走到这里：不发请求，平台原数据一个字节都没动
      ElMessage.info(OVERWRITE_CANCELLED_TIP)
      return
    }
    if (needsConfirm) {
      ElMessage.info(mode === MODE_APPEND
        ? '已选择「保留原数据并解析」：新数据将追加到现有数据之后'
        : '已选择「覆盖并上传」：当前平台数据将被替换')
    }

    running.value = true
    uploadPct.value = 0
    job.value = null
    const j = await send(mode, (p) => { uploadPct.value = p })
    uploadPct.value = 100
    job.value = j
    startPoll(j.job_id)
  } catch (e) {
    running.value = false
  } finally {
    uploadBusy.value = false
  }
}

/** 单文件通道：原来的 POST /dataset/import（zip / xlsx / csv），行为与改造前一致 */
const beginUpload = (raw) => {
  selection.value = selectionText(1, raw.size)
  return runUpload((mode, onProgress) => uploadDataset(raw, onProgress, mode))
}

/**
 * 文件夹通道：POST /dataset/import-folder（多文件 + 各自相对路径）。
 * 上传前把「已选 N 个文件 / 合计 X MB」算出来并展示。
 *
 * **直接多选 .html 公告文件也走这里**：`planUpload()` 把每个文件的相对路径设成它的
 * 文件名（如 `t20260202_26141082.html`），后端落盘的 `work/` 根目录下就是
 * 「一篇公告 = 一个 .html」的集合，自动识别为路线②（逐篇解析），同一批里的
 * `<公告号>_file.zip` 附件包也能按公告号配对。
 */
const beginFolderUpload = (picked) => {
  const bytes = picked.reduce((n, it) => n + (it.file.size || 0), 0)
  selection.value = selectionText(picked.length, bytes)
  const label = selection.value || ''
  return runUpload((mode, onProgress) => {
    ElMessage.info(`${label}，正在上传…`)
    return uploadDatasetFolder(picked, onProgress, mode)
  })
}

/**
 * 统一分发：一次选择（点选若干个文件 / 拖入文件夹）先交给 `planUpload()` 判定路线，
 * 再走对应通道 —— 三条都是**已有**通道，没有新增接口：
 *   * single    单个平铺压缩包/表格 -> POST /dataset/import（行为与改造前一致）
 *   * folder    多文件 / 带目录结构 -> POST /dataset/import-folder
 *   * html-flat 平铺多选 .html      -> POST /dataset/import-folder（= 路线②）
 */
const dispatchPicked = async (items) => {
  const plan = planUpload(items)
  if (plan.kind === KIND_NONE) return
  if (plan.note) ElMessage.info(plan.note)   // 混选 / HTML 集合的处理说明，上传前就说清
  if (plan.kind === KIND_SINGLE) return beginUpload(plan.items[0].file)
  return beginFolderUpload(plan.items)
}

/* ---------------- 点击选择：把同一批 on-change 合并成一次上传 ----------------
   el-upload 的 on-change 是**每个文件一次**（内部 nextTick 逐个 emit）：一次多选
   3 个 .html 会连来 3 次回调。若每次都立刻上传，就会变成 3 次独立导入、彼此覆盖
   （replace 语义下最后只剩 1 篇）。这里按 uid 累积，等这一批回调全部到齐
   （50ms 内没有新文件）后只分发一次，于是「3 个 .html」= 一次 import-folder = 3 篇。 */
const pendingPicks = new Map()
let pendingTimer = null
const PICK_MERGE_MS = 50

const flushPicked = () => {
  pendingTimer = null
  const items = [...pendingPicks.values()]
  pendingPicks.clear()
  if (items.length) dispatchPicked(items)
}

/** 拖拽入区 / 点击选择文件 —— 同一个 on-change 回调，交给统一入口处理 */
const onFileChange = (file) => {
  const raw = file?.raw
  if (!raw) return
  pendingPicks.set(file.uid ?? `${raw.name}:${raw.size}:${raw.lastModified}`, {
    file: raw,
    relPath: raw.webkitRelativePath || raw.name,
  })
  if (pendingTimer) clearTimeout(pendingTimer)
  pendingTimer = setTimeout(flushPicked, PICK_MERGE_MS)
}

/* ---------------- 选择文件夹（<input webkitdirectory>）---------------- */

const folderInput = ref(null)
/** 「已选 N 个文件 / 合计 X MB」文案（上传前展示，见模板 .edm-selection） */
const selection = ref('')
/** 拖拽区容器：drop 事件在捕获阶段接管，见 onMounted */
const dropZone = ref(null)

/**
 * 点「选择文件夹」-> 直接打开系统的**目录选择器**。
 *
 * 必须由用户手势（click 回调）同步触发 `<input webkitdirectory>.click()`，否则浏览器
 * 会把文件选择当作「非用户手势」拦掉。历史上这里点不动有两个原因，都在本函数里兜住：
 *   1. 隐藏 input 用 `display:none` —— 部分浏览器（Safari / 某些 WebView / 部分
 *      Chromium 企业策略）拒绝为「不参与布局」的 file input 打开选择器；
 *      现在改成 `.edm-hidden-input`：`position:absolute; opacity:0; width:0; height:0`
 *      （真实存在于 DOM、可被点击，但不占位、不可见）。
 *   2. `folderInput.value` 为 null（组件还没挂载 / ref 名写错）时静默失败、用户毫无反馈；
 *      现在回退到 `document.querySelector('input[webkitdirectory]')`，仍找不到就给明确提示。
 */
const pickFolder = () => {
  if (running.value) return
  const el = folderInput.value
    || document.querySelector('input[type="file"][webkitdirectory]')
  if (!el) {
    ElMessage.error('当前浏览器不支持文件夹选择，请改用拖入文件夹或上传 .zip')
    return
  }
  // 清空上次选择：允许连续两次选同一个目录（否则 change 不再触发）
  try { el.value = '' } catch (e) { /* 只读时忽略 */ }
  el.click()
}

const onFolderPick = async (e) => {
  const files = Array.from(e.target?.files || [])
  // 清空 value：否则连续两次选同一个文件夹不会再触发 change
  if (e.target) e.target.value = ''
  if (!files.length) return
  // webkitdirectory 下每个文件的相对路径（形如 sub/dir/a.html）在 webkitRelativePath 里
  const picked = files.map((f) => ({
    file: f,
    relPath: f.webkitRelativePath || f.name,
  }))
  await beginFolderUpload(picked)
}

/* ---------------- 拖入文件夹（DataTransferItem.webkitGetAsEntry 递归）---------------- */

/** 递归读一个目录 reader 的**全部**条目：readEntries 一次最多返回 100 条，必须读到空 */
const readAllEntries = (reader) => new Promise((resolve, reject) => {
  const acc = []
  const step = () => {
    reader.readEntries((batch) => {
      if (!batch.length) return resolve(acc)   // 返回空数组 = 读完了
      acc.push(...batch)
      step()                                    // 继续读下一批
    }, reject)
  }
  step()
})

/** 递归遍历一个 FileSystemEntry，把文件摊平成 [{file, relPath}] */
const walkEntry = async (entry, prefix, out) => {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej))
    out.push({ file, relPath: path })
    return
  }
  if (entry.isDirectory) {
    const children = await readAllEntries(entry.createReader())
    for (const child of children) await walkEntry(child, path, out)
  }
}

/**
 * 从 dataTransfer 取出「文件夹 / 文件」清单。
 *
 * 返回 `Array<{file, relPath}>`；**返回 null 表示浏览器不支持** webkitGetAsEntry
 * （`DataTransferItem.webkitGetAsEntry` 缺失），调用方退化为只处理普通文件并提示。
 */
const collectDroppedItems = async (dt) => {
  if (!dt) return null
  const items = dt.items ? Array.from(dt.items) : []
  const hasEntryApi = items.length > 0
    && typeof items[0].webkitGetAsEntry === 'function'
  if (!hasEntryApi) return null

  const roots = []
  for (const it of items) {
    if (it.kind !== 'file') continue
    const entry = it.webkitGetAsEntry()
    if (entry) roots.push(entry)
  }
  const out = []
  for (const entry of roots) await walkEntry(entry, '', out)
  return out
}

/** 拖拽落点：文件夹 -> 文件夹通道；单个普通文件 -> 沿用原来的单文件通道；
    平铺多选的 .html -> 文件夹通道但相对路径 = 文件名（后端识别为路线②）。
    具体判定全在 `dispatchPicked()` / `planUpload()` 里，这里只负责取到清单。 */
const handleDrop = async (dt) => {
  if (running.value || uploadBusy.value) return
  let picked = null
  try {
    picked = await collectDroppedItems(dt)
  } catch (e) {
    picked = null
  }

  if (picked === null) {
    // 降级：不支持目录 API -> 只处理普通文件（保持改造前的能力）
    const files = Array.from(dt?.files || [])
    if (files.length > 1) ElMessage.warning(FOLDER_DRAG_UNSUPPORTED_TIP)
    if (files.length) {
      await dispatchPicked(files.map((f) => ({ file: f, relPath: f.name })))
    }
    return
  }
  if (!picked.length) return
  // 单个平铺文件 / 平铺多选 .html / 带目录结构的文件夹，统一由 planUpload 判定通道
  await dispatchPicked(picked)
}

/**
 * drop / dragover 走**捕获阶段**：el-upload 自带 el-upload-dragger 的 drop 处理
 * 只认 dataTransfer.files（文件夹会变成 0 字节空条目），必须在事件到达它之前拦下
 * —— 捕获阶段的 stopPropagation() 正好做到这一点（Vue 模板里的 @drop.stop 在
 * 后代元素上生效太晚，事件已先被 dragger 处理）。
 * dragover 只 preventDefault 不停止传播，让 el-upload 正常显示高亮态。
 */
const installDropHandlers = (el) => {
  if (!el) return null
  const onDragOver = (e) => { e.preventDefault() }
  const onDrop = (e) => {
    e.preventDefault()
    e.stopPropagation()
    handleDrop(e.dataTransfer)
  }
  el.addEventListener('dragover', onDragOver, true)
  el.addEventListener('drop', onDrop, true)
  return () => {
    el.removeEventListener('dragover', onDragOver, true)
    el.removeEventListener('drop', onDrop, true)
  }
}

let removeDropHandlers = null

onMounted(() => {
  load()
  removeDropHandlers = installDropHandlers(dropZone.value)
})
// 数据版本变化（上传落库成功）时重取 stats；离开再回到本页也会重取一次。
// 首次挂载由 onMounted 负责，二者不会重复请求（见 utils/dataVersion.js）。
useDataFreshness(load)
onBeforeUnmount(() => {
  stopPoll()
  if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null }
  pendingPicks.clear()
  if (removeDropHandlers) removeDropHandlers()
})

/* ---------------- 提取结果下载（官方格式）---------------- */

const exportingXlsx = ref(false)

/** 触发浏览器下载：把 blob 交给临时 <a download>，文件名由前端指定 */
const saveBlob = (blob, filename) => {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

const onDownloadOfficial = async () => {
  exportingXlsx.value = true
  try {
    // 走统一的 axios 客户端：Bearer token 自动附带，responseType=blob
    const blob = await downloadOfficial()
    saveBlob(blob, '官方数据集提取结果.xlsx')
    ElMessage.success('提取结果已开始下载')
  } catch (e) { /* 拦截器已提示 */ } finally {
    exportingXlsx.value = false
  }
}

/* ---------------- 恢复默认数据（仓库内置完整交付集）---------------- */

/** 确认框正文：需求要求把「完整交付集（1038 篇 / 6244 条）」与影响写清楚 */
const RESTORE_CONFIRM_HTML =
  '将用<strong>仓库内置的完整交付集（1038 篇 / 6244 条）</strong>'
  + '覆盖当前平台数据，并重建图库。是否继续？'

/**
 * 一键把平台恢复成仓库内置的完整交付集（后端 POST /dataset/restore）。
 *
 * 流程（每一段都有明确原因）：
 *   1. 先确认 —— 这是**破坏性**操作（覆盖当前数据 + 重建图库），不能一键静默执行；
 *   2. `restoring` 期间按钮 loading/禁用并显示「正在恢复…」（接口可能耗时数十秒，
 *      没有反馈用户会重复点击；后端也有 409 兜底，这里只是更好的交互）；
 *   3. 成功后 `load()`（内部就是 `getDatasetStats` + 筛选取值域）刷新本页规模，
 *      再 `bumpDataVersion()` 让标的检索 / 场景分析等页面自动重查；
 *   4. 失败提示由 client.js 拦截器统一弹出（后端 detail 是可直接展示的中文原因，
 *      例如 409「正在恢复中，请稍候」），这里不重复弹一次。
 */
const onRestoreDefault = async () => {
  if (restoring.value || running.value || uploadBusy.value) return
  try {
    await ElMessageBox.confirm(RESTORE_CONFIRM_HTML, '恢复默认数据', {
      confirmButtonText: '恢复默认数据',
      cancelButtonText: '取消',
      type: 'warning',
      // 正文是**常量**（只是为了保留需求里的加粗强调），不存在注入面
      dangerouslyUseHTMLString: true,
    })
  } catch (e) {
    return                            // 点「取消」/ X / Esc：什么都不做
  }
  restoring.value = true
  try {
    const r = await restoreDefaultDataset()
    await load()                      // getDatasetStats + 筛选取值域：本页规模立即更新
    bumpDataVersion()                 // 其它数据视图随之自动重查
    ElMessage.success(`已恢复：${r.n_docs} 篇 / ${r.n_items} 条`)
  } catch (e) {
    /* 失败提示由 client.js 拦截器统一弹出 */
  } finally {
    restoring.value = false
  }
}
</script>

<style scoped>
/* 把拖拽区压扁：默认的 el-upload-dragger 太高，占掉一屏 */
.edm-drop :deep(.el-upload-dragger) {
  padding: 16px 20px;
  border-radius: var(--edm-radius-sm);
  background: #fafbfd;
  transition: background-color var(--edm-dur) var(--edm-ease),
              border-color var(--edm-dur) var(--edm-ease);
}
.edm-drop :deep(.el-upload-dragger:hover),
.edm-drop :deep(.el-upload-dragger.is-dragover) {
  background: var(--edm-primary-soft);
  border-color: var(--edm-primary);
}
.edm-drop__inner {
  display: flex; align-items: center; justify-content: center; gap: 14px;
}
.edm-drop__icon {
  font-size: 30px; color: var(--edm-primary);
  transition: transform var(--edm-dur) var(--edm-ease);
}
.edm-drop :deep(.el-upload-dragger:hover) .edm-drop__icon { transform: translateY(-2px); }
.edm-drop__title { font-size: 13.5px; color: var(--edm-text); }
.edm-drop__hint { font-size: 12px; color: var(--edm-text-3); margin-top: 3px; }

/* 「选择文件夹」用的隐藏 file input：必须**真实存在于 DOM 且可点击**，
   但不要占位/可见。注意不能用 display:none —— 部分浏览器会因此拒绝弹出目录选择器，
   这就是「点『选择文件夹』没反应」的根因，改成「零尺寸 + 全透明 + 绝对定位」。 */
.edm-hidden-input {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  overflow: hidden;
}

/* 上传前的选择摘要：已选 N 个文件 / 合计 X MB */
.edm-selection { font-size: 12px; margin-top: 8px; }

.edm-progress { margin-top: 14px; }
.edm-progress__meta {
  display: flex; align-items: center; gap: 8px;
  font-size: 12.5px; margin-bottom: 8px;
}

/* 「恢复默认数据」：上传区下方的一行说明 + 按钮（右侧，与上传动作区分开） */
.edm-restore {
  display: flex; align-items: center; gap: 14px;
  margin-top: 14px; padding-top: 12px;
  border-top: 1px dashed #e6eaf0;
}
.edm-restore__hint {
  flex: 1; font-size: 12px; color: var(--edm-text-3); line-height: 1.6;
}
.edm-restore__hint b { color: var(--edm-text); font-weight: 600; }
</style>
