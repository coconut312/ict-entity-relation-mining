/**
 * 展示格式化工具。
 *
 * 金额一律按「元」处理并在大数时转「万」，与后端 schemas 的约定保持一致。
 *
 * **单位标注纪律**（2026 修复）：界面上的金额标签必须与数值的实际显示单位一致。
 * `fmtMoney` 会按数值大小自动在「元 / 万」之间切换，若标签写死「（元）」就会出现
 * 「总金额（元）＝ 5,765.03 万」这种自相矛盾的展示。因此金额要么用
 * `moneyUnit()` 取实际单位后拼进标签，要么用 `fmtMoneyUnit(v, 固定单位)` 让**整列**
 * 用同一个单位，二者保证标签与数值永远自洽。
 */

export const MONEY_UNIT_THRESHOLD = 10000

/**
 * 一组金额值实际会以什么单位展示：任一值 ≥ 1 万 -> '万元'，否则 '元'。
 * 列头/卡片标签用它拼出与数值一致的文案。
 */
export function moneyUnit(value) {
  const arr = Array.isArray(value) ? value : [value]
  for (const v of arr) {
    if (v === null || v === undefined || v === '') continue
    const n = Number(v)
    if (!Number.isNaN(n) && Math.abs(n) >= MONEY_UNIT_THRESHOLD) return '万元'
  }
  return '元'
}

/**
 * 按**指定单位**格式化金额（不带单位后缀，单位交给标签写），
 * 用于「整列统一单位」的表格：标签写 `累计金额（万元）`，单元格全部按万元渲染。
 */
export function fmtMoneyUnit(v, unit = '元', digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const x = unit === '万元' ? n / MONEY_UNIT_THRESHOLD : n
  return x.toLocaleString('zh-CN', {
    minimumFractionDigits: unit === '万元' ? 1 : 0,
    maximumFractionDigits: digits,
  })
}

/** 金额 + 单位后缀（单值展示用；单位由数值本身决定，故与数值必然自洽）。 */
export function fmtMoneyWithUnit(v, digits = 2) {
  const s = fmtMoneyUnit(v, moneyUnit(v), digits)
  return s === '—' ? s : `${s} ${moneyUnit(v)}`
}

export function fmtMoney(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  if (Math.abs(n) >= 10000) {
    return (n / 10000).toLocaleString('zh-CN', {
      minimumFractionDigits: 1, maximumFractionDigits: 2,
    }) + ' 万'
  }
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: 0, maximumFractionDigits: digits,
  })
}

export function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return Number.isInteger(n) ? n.toLocaleString('zh-CN')
    : n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || v === '') return '—'
  return (Number(v) * 100).toFixed(digits) + '%'
}

export function fmtDate(s) {
  return s || '—'
}

/**
 * **单价专用**格式化：单价的量级可能极小（例如 0.945 元/个 = 0.0000945 万元），
 * 用 `fmtMoneyUnit` 的 2 位小数会被舍成「0.0」，造成"单价丢失"的误读。
 * 因此在「万元」单位下对 |x| < 0.01 的值保留到 6 位小数（去尾零），其余仍按 2 位。
 */
export function fmtUnitPrice(v, unit = '元') {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const x = unit === '万元' ? n / MONEY_UNIT_THRESHOLD : n
  const tiny = unit === '万元' && x !== 0 && Math.abs(x) < 0.01
  return x.toLocaleString('zh-CN', {
    minimumFractionDigits: unit === '万元' ? (tiny ? 2 : 1) : 0,
    maximumFractionDigits: unit === '万元' ? (tiny ? 6 : 2) : 2,
  })
}
