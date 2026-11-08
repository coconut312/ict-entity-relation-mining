/**
 * 接口封装 —— 与后端 backend/app/schemas.py 一一对应。
 *
 * 合并约定：队友只需让后端返回符合 schemas 的数据，
 * 这个文件与所有页面都不需要改动。
 *
 * 说明：只保留产品实际需要的能力（数据集处理、标的检索、场景分析）。
 * 界面用不到的接口一律不定义 —— 少一个接口就少一处维护与出错点。
 */
import client from './client'

// ---------------- 系统 ----------------
export const getHealth = () => client.get('/health')

// ---------------- 鉴权 ----------------
export const login = (username, password) =>
  client.post('/auth/login', { username, password })
export const logout = () => client.post('/auth/logout')
export const getMe = () => client.get('/auth/me')

// ---------------- 数据集 ----------------
export const getDatasetStats = () => client.get('/dataset/stats')
export const getImportJob = (jobId) => client.get(`/dataset/import/${jobId}`)

/**
 * 恢复默认数据 —— 对应后端 `POST /dataset/restore`（见 backend/app/api.py）。
 *
 * 用**仓库内置的完整交付集**（1038 篇 / 6244 条）覆盖当前平台数据，并重建图库；
 * 数据总览页「恢复默认数据」按钮专用。
 *
 * 耗时取决于机器与图库规模（本机 Neo4j 实测约 1.5–2 秒；2C4G 云主机 / 冷缓存时
 * 可能数十秒），因此这里把超时从客户端默认的 30s 放宽到 5 分钟
 * （timeout: 0 会变成无限等待，不适合这里）。
 * 失败语义：409 = 已有一次恢复在进行（detail「正在恢复中，请稍候」）；
 *          500 = 恢复失败（detail 为可读原因）。
 */
export const restoreDefaultDataset = () =>
  client.post('/dataset/restore', null, { timeout: 300000 })

/**
 * 上传数据集（zip / xlsx / csv）。onProgress 接收 0~100 的上传百分比。
 * 上传完成返回 job_id，之后用 getImportJob 轮询**处理**进度。
 *
 * mode（后端 /dataset/import 的 mode 参数，见 backend/app/api.py）：
 *   * 'replace'（默认）——清空后重建，库里只剩本次数据集（兼容改造前行为）；
 *   * 'append'         ——保留库内现有数据，把本次解析结果按行去重后追加。
 * 用查询参数传：请求在 DevTools / 访问日志里一眼能看出这次是覆盖还是追加。
 */
export function uploadDataset(file, onProgress, mode = 'replace') {
  const form = new FormData()
  form.append('file', file)
  return client.post(`/dataset/import?mode=${encodeURIComponent(mode)}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 0, // 数据集可达数 GB，不做超时限制
    onUploadProgress: (e) => {
      if (onProgress && e.total) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    },
  })
}

/**
 * 上传**文件夹**（多文件 + 各自相对路径）—— 对应后端
 * `POST /dataset/import-folder`（见 backend/app/api.py）。
 *
 * @param {Array<{file: File, relPath: string}>} items
 *        每个元素的 relPath 形如 `sub/dir/a.html`（`webkitRelativePath`
 *        或拖拽目录递归得到的相对路径）。
 * @param {(pct:number)=>void} onProgress 0~100 的上传百分比
 * @param {'replace'|'append'} mode 与单文件入口完全同义
 *
 * 相对路径**双保险**传给后端（服务端优先用 `paths` 字段，没有则回退用 filename）：
 *   1. `FormData.append('files', file, relPath)` 的第三个参数会成为服务端看到的
 *      `file.filename`（就是要的相对路径）；
 *   2. 同时并行追加一份 `paths` 表单字段，顺序与 `files` 严格一一对应 ——
 *      个别浏览器/代理会丢掉第三参数，这时后端仍能按 paths 还原目录结构。
 * 后端会先校验全部路径（拒绝 `..` / 绝对路径 / 盘符）再落盘，任一非法整体 400。
 */
export function uploadDatasetFolder(items, onProgress, mode = 'replace') {
  const form = new FormData()
  items.forEach(({ file, relPath }) => {
    const rel = relPath || file.name
    form.append('files', file, rel)
    form.append('paths', rel)
  })
  return client.post(`/dataset/import-folder?mode=${encodeURIComponent(mode)}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 0,
    onUploadProgress: (e) => {
      if (onProgress && e.total) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    },
  })
}

// ---------------- 标的检索 ----------------
export const searchItems = (params) => client.get('/items', { params })
export const getItemFacets = () => client.get('/items/facets')

// ---------------- 场景分析 ----------------
/** 场景元定义由后端提供，前端据此自动渲染查询表单 */
export const getScenarios = () => client.get('/scenarios')

export const runScenario1 = (params) => client.get('/scenarios/s1', { params })
export const runScenario2 = (params) => client.get('/scenarios/s2', { params })
export const runScenario3 = (params) => client.get('/scenarios/s3', { params })

/** 场景 4/5 的 suppliers 是重复参数，axios 默认序列化成 suppliers[]=，
 *  这里改为 suppliers=a&suppliers=b，与 FastAPI 的 List[str] Query 对齐。 */
const repeatParams = (obj) => {
  const p = new URLSearchParams()
  Object.entries(obj).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x))
    else if (v !== undefined && v !== null && v !== '') p.append(k, v)
  })
  return p
}
export const runScenario4 = (params) =>
  client.get('/scenarios/s4', { params: repeatParams(params) })
export const runScenario5 = (params) =>
  client.get('/scenarios/s5', { params: repeatParams(params) })

export const SCENARIO_API = {
  s1: runScenario1, s2: runScenario2, s3: runScenario3,
  s4: runScenario4, s5: runScenario5,
}

// ---------------- 实体（场景查询输入用）----------------
export const suggestEntities = (q, type) =>
  client.get('/entities/suggest', { params: { q, type } })

// ---------------- 模型配置（API Key）----------------
// 响应里只有掩码（****1234），明文 key 只在前端输入框内、保存时上行一次
export const getModelSettings = () => client.get('/settings/model')
export const saveModelSettings = (body) => client.put('/settings/model', body)
export const testModelSettings = () => client.post('/settings/model/test')
export const clearModelSettings = () => client.delete('/settings/model')

// ---------------- 提取结果下载（官方《输出标准样例表》格式）----------------
// 走同一个 client：Bearer token 自动附带；responseType=blob 直接拿二进制附件。
// 后端现读平台当前库（= 上传解析后的落库结果）生成三张标准表，因此下载内容
// 始终等于「本次上传解析出来的提取结果」，而不是预生成的静态交付表。
const BLOB_OPTS = { responseType: 'blob', timeout: 300000 }

/** 下载官方格式工作簿（xlsx，三张标准表：总表/标的表/投标人表） */
export const downloadOfficial = () => client.get('/export/official', BLOB_OPTS)

/** 下载单张标准表（CSV）—— 表名含中文，必须 URL 编码 */
export const downloadOfficialSheet = (sheet) =>
  client.get(`/export/official/${encodeURIComponent(sheet)}`, BLOB_OPTS)
