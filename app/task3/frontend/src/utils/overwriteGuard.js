/**
 * 上传入库方式确认 —— 判定与文案抽成纯函数。
 *
 * 为什么单独放一个文件：
 * 1. 页面里的判定必须「有数据才询问、空库直接上传」，这是回归风险最高的一行；
 *    抽出来就能在 Node 里跑断言（见同目录 overwriteGuard.test.mjs），不依赖浏览器。
 * 2. 文案集中一处，页面、提示、单测共用同一份字符串，避免改一处漏一处。
 * 3. 「取消按钮 = 保留原数据并解析（仍然上传）」这类语义最容易在改动中被误解成
 *    「放弃」，所以把 ElMessageBox 的动作到入库模式的映射也做成纯函数 `decideUploadMode()`，
 *    直接用断言把语义钉住。
 */

/** 确认框标题 */
export const OVERWRITE_TITLE = '当前平台已有数据，请选择入库方式'

/** 确认按钮文案：覆盖并上传（mode=replace） */
export const OVERWRITE_CONFIRM_TEXT = '覆盖并上传'

/**
 * 取消按钮文案：**保留原数据并解析**（mode=append）。
 *
 * 注意：这里的「取消」只是 Element Plus 的按钮角色（次要按钮），
 * 业务语义**不是放弃**——点它依然会解析新文件，只是把结果追加到现有数据之后。
 */
export const OVERWRITE_KEEP_TEXT = '保留原数据并解析'

/** 真正放弃（点右上角 X / 按 Esc）后的提示：明确告诉用户什么都没动 */
export const OVERWRITE_CANCELLED_TIP = '已取消，未做任何改动'

/** 拖拽区常驻提示：让用户上传前就知情 */
export const OVERWRITE_DROP_HINT = '上传前会询问：覆盖当前数据，或保留原数据并解析'

/* ---------------- 文件夹上传（选择文件夹 / 拖入文件夹）---------------- */

/** 「选择文件夹」入口文案（文件夹上传入口的唯一文案来源，验收 grep 用它） */
export const FOLDER_PICK_TEXT = '选择文件夹'

/** 文件数 / 合计大小的展示模板（上传前先让用户看清要传什么） */
export const SELECTION_LABEL_PREFIX = '已选'

/** 整理「已选 N 个文件 / 合计 X MB」文案；空选择返回 '' */
export const selectionText = (files, bytes) => {
  const n = Number(files) || 0
  if (n <= 0) return ''
  const mb = (Number(bytes) || 0) / 1048576
  const size = mb >= 100 ? mb.toFixed(0) : mb >= 1 ? mb.toFixed(1) : mb.toFixed(2)
  return `${SELECTION_LABEL_PREFIX} ${n} 个文件 / 合计 ${size} MB`
}

/** 浏览器不支持 webkitGetAsEntry（拖拽目录 API）时的降级提示 */
export const FOLDER_DRAG_UNSUPPORTED_TIP =
  '当前浏览器不支持拖入文件夹，已按普通文件处理；可点「选择文件夹」上传'

/* ---------------- 入库模式 ---------------- */

/** 覆盖：清空后重建，库里只剩本次数据集 */
export const MODE_REPLACE = 'replace'
/** 追加：保留库内现有数据，把本次解析结果按行去重后追加 */
export const MODE_APPEND = 'append'

/* ---------------- ElMessageBox 的动作 ---------------- */

/**
 * `distinguishCancelAndClose: true` 时 ElMessageBox 的三种结果：
 *   * resolve      -> confirm（点「覆盖并上传」）
 *   * reject('cancel') -> 点「保留原数据并解析」
 *   * reject('close')  -> 点 X / 按 Esc（真正的放弃）
 */
export const UPLOAD_ACTION_CONFIRM = 'confirm'
export const UPLOAD_ACTION_CANCEL = 'cancel'
export const UPLOAD_ACTION_CLOSE = 'close'

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : 0
}

/** 当前库是否已有数据（公告数或标的数任一大于 0） */
export const needsOverwriteConfirm = (stats) => {
  const nDocs = num(stats?.n_docs)
  const nItems = num(stats?.n_items)
  return nItems > 0 || nDocs > 0
}

/** 确认框正文：把两种选择的后果都说清楚，用户才有判断依据
 *
 * @param {object} stats 当前库规模（/dataset/stats）
 * @param {string} [extra] 追加一行说明（文件夹上传时放「已选 N 个文件 / 合计 X MB」）。
 *        不传时输出与改造前**逐字相同**（单文件上传行为不变）。
 */
export const overwriteConfirmMessage = (stats, extra = '') => {
  const nDocs = num(stats?.n_docs)
  const nItems = num(stats?.n_items)
  const head = `上传会替换当前平台数据（现有 ${nDocs} 篇公告 / ${nItems} 条标的）。`
    + '选择“覆盖并上传”将替换；选择“保留原数据并解析”将追加到现有数据之后。'
  return extra ? `${extra}\n${head}` : head
}

/**
 * 由「是否需要询问」+「用户动作」决定是否继续上传、以及用哪种模式。
 *
 * @param {{needsConfirm: boolean, action?: string|null}} input
 *        action 取 UPLOAD_ACTION_CONFIRM / CANCEL / CLOSE，未弹框时传 null
 * @returns {{proceed: boolean, mode: string}}
 *        proceed=false 表示真正放弃：不发请求、平台原数据保持不变
 */
export function decideUploadMode ({ needsConfirm, action }) {
  if (!needsConfirm) {
    return { proceed: true, mode: MODE_REPLACE }        // 空库：直接上传，无破坏性
  }
  switch (action) {
    case UPLOAD_ACTION_CONFIRM:
      return { proceed: true, mode: MODE_REPLACE }      // 「覆盖并上传」
    case UPLOAD_ACTION_CANCEL:
      return { proceed: true, mode: MODE_APPEND }       // 「保留原数据并解析」
    default:                                            // 'close' / 未知动作 = 放弃
      return { proceed: false, mode: MODE_REPLACE }
  }
}
