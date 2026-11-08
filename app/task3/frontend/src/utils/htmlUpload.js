/**
 * 「数据集处理」上传文件的类型白名单 + 上传路线判定 —— 纯函数，可由 Node 直接断言。
 *
 * 背景：后端 4 条识别路线里，**路线②（原始 HTML 数据集）**要求「一篇公告 = 一个 .html」，
 * 并且是通过 `/api/v1/dataset/import-folder`（多文件 + 各自相对路径）接收的。
 * 原来的前端只允许 .zip/.rar/.7z/.xlsx/.csv：
 *   * `<input accept>` 直接让用户在文件选择器里**看不到** .html；
 *   * 就算把 .html 拖进来，也会按「单个普通文件」落到单文件入口 POST /dataset/import
 *     —— 那个入口只认压缩包/表格，.html 必然被当成坏 zip 而进不了路线②。
 *
 * 所以这里做两件事：
 *   1. **白名单**：`.zip/.rar/.7z/.xlsx/.csv` + `.html/.htm`（html 与压缩包同级放行）；
 *   2. **路线判定 `planUpload()`**：把「用户选了什么」映射到三条**已有**通道之一
 *        * `single`    —— 单个平铺的压缩包/表格：POST /dataset/import（行为与改造前逐字一致）
 *        * `folder`    —— 带目录结构（选择文件夹 / 拖入文件夹）：原样保留相对路径
 *        * `html-flat` —— **平铺多选的 .html**（浏览器文件选择器给不出目录结构）：
 *                         每个文件的相对路径就用它的**文件名**（如
 *                         `t20260202_26141082.html`），仍走 import-folder ——
 *                         后端把多文件还原成一个临时目录，`work/` 根下就是
 *                         「一篇公告 = 一个 .html」的集合，自动识别为路线②，
 *                         **不需要新增任何接口**。
 * 判定抽成纯函数是为了能在 Node 里钉住契约（见同目录 htmlUpload.test.mjs）：
 * 页面、单测、验收 grep 共用同一份扩展名表与文案。
 */

/** 压缩包 / 表格：原来就支持的格式，走原通道 */
export const ARCHIVE_EXTS = ['.zip', '.rar', '.7z', '.xlsx', '.csv']

/** 公告 HTML：一篇公告 = 一个 .html（`.htm` 同义，前端一并放行） */
export const HTML_EXTS = ['.html', '.htm']

/** 允许上传的全部扩展名 */
export const ALLOWED_EXTS = [...ARCHIVE_EXTS, ...HTML_EXTS]

/**
 * `<input type=file accept>` / el-upload 的 accept 串。
 * 注意：Overview.vue 模板里写的是**同字面量**（便于产物 grep），单测会逐字比对两者。
 */
export const UPLOAD_ACCEPT = ALLOWED_EXTS.join(',')

/** 上传区头部「支持 …」说明（页面直接展示这行常量） */
export const UPLOAD_SUPPORT_LINE =
  '支持 文件夹 / .zip / .xlsx / .csv（含无 HTML 的单篇公告附件包），也可直接多选 .html 公告文件'

/** 拖拽区第二行提示（前半句是支持的形态，后半句由页面拼上覆盖确认说明） */
export const HTML_DROP_HINT =
  '支持整个文件夹（自动还原目录结构，含子目录）、.zip / .xlsx / .csv，也可直接多选 .html 公告文件'

/* ---------------- 扩展名判定 ---------------- */

const extOf = (name) => {
  const n = String(name || '').trim().toLowerCase()
  const i = n.lastIndexOf('.')
  return i < 0 ? '' : n.slice(i)
}

/** 是否为公告 HTML（.html / .htm，大小写不敏感） */
export const isHtmlName = (name) => HTML_EXTS.includes(extOf(name))

/** 是否为压缩包 / 表格 */
export const isArchiveName = (name) => ARCHIVE_EXTS.includes(extOf(name))

/**
 * 扩展名是否在白名单内（`.html/.htm` 与压缩包/表格**同等放行**）。
 *
 * 说明：这里**不用它去丢弃**用户选的文件，只用来判定上传路线 ——
 * 「选择文件夹 / 拖入文件夹」传上来的可能是 .pdf/.docx 附件（路线④ 要解析它们），
 * 按扩展名硬过滤会把合法附件挡在门外。
 */
export const isAllowedName = (name) => ALLOWED_EXTS.includes(extOf(name))

/** 一批选择里是否带目录结构（相对路径含分隔符） */
export const hasDirStructure = (items) =>
  (Array.isArray(items) ? items : []).some((it) => {
    const rel = String(it?.relPath || '')
    return rel.includes('/') || rel.includes('\\')
  })

/* ---------------- 路线判定 ---------------- */

/** 路线取值（页面按它选通道） */
export const KIND_SINGLE = 'single'   // 单文件：POST /dataset/import
export const KIND_FOLDER = 'folder'   // 多文件 / 目录：POST /dataset/import-folder
export const KIND_HTML = 'html-flat'  // 平铺多选 .html：同样 POST /dataset/import-folder（= 路线②）
export const KIND_NONE = 'none'

/**
 * 由「用户选了什么」得到上传路线。
 *
 * @param {Array<{file: File, relPath?: string}>} items 选择/拖入的文件（relPath 可缺省 = 文件名）
 * @returns {{kind: string, items: Array<{file: File, relPath: string}>,
 *            nHtml: number, nOther: number, note: string}}
 *   * `items` 已补齐 relPath，可直接喂 `uploadDatasetFolder()`；
 *   * `nHtml` / `nOther`：本次选择里 .html/.htm 与其他文件的数量（用于提示文案）；
 *   * `note`：给用户看的一句话说明（空字符串表示无需额外说明）。
 */
export function planUpload (items) {
  const list = (Array.isArray(items) ? items : [])
    .filter((it) => it && it.file)
    .map((it) => ({ file: it.file, relPath: it.relPath || it.file.name || '' }))

  if (!list.length) return { kind: KIND_NONE, items: [], nHtml: 0, nOther: 0, note: '' }

  const htmls = list.filter((it) => isHtmlName(it.file.name))
  const nHtml = htmls.length
  const nOther = list.length - nHtml
  const dirs = hasDirStructure(list)

  // 1) 平铺选择里含 .html：一律走「HTML 公告集合」通道（路线②）。
  //    单个 .html 也走这里 —— 单文件入口只认压缩包/表格，喂 .html 会解析失败。
  //    同批的非 HTML 文件**照样一起传**：`<公告号>_file.zip` 这种附件包要和 html 同级，
  //    后端才能按公告号配对（pipeline_impl 里 `work/<aid>_file.zip` 是第二个候选路径）。
  if (nHtml && !dirs) {
    const note = nOther
      ? `已选 ${nHtml} 个 HTML 公告 + ${nOther} 个其他文件：按「HTML 公告集合」处理（同名的 _file.zip 附件包会一并配对，其余格式本批不单独解析）`
      : `已选 ${nHtml} 个 HTML 公告：按「HTML 公告集合」逐篇解析`
    return { kind: KIND_HTML, items: list, nHtml, nOther, note }
  }

  // 2) 只有一个平铺文件：保持改造前的单文件通道（zip / xlsx / csv 行为不变）
  if (list.length === 1 && !dirs) {
    return { kind: KIND_SINGLE, items: list, nHtml, nOther, note: '' }
  }

  // 3) 其余（多文件 / 带目录结构，含「拖入文件夹」里的 html）：沿用文件夹通道原样上传
  return { kind: KIND_FOLDER, items: list, nHtml, nOther, note: '' }
}
