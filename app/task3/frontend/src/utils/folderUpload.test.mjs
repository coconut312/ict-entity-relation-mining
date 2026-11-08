/**
 * 文件夹上传相关的纯函数验证（不依赖浏览器，只用 Node 内置能力）。
 *
 * 跑法（在 app/task3/frontend 下）：
 *   "<node>" src/utils/folderUpload.test.mjs
 *
 * 覆盖三条契约：
 *   1. 「已选 N 个文件 / 合计 X MB」文案（上传前必须让用户看清要传什么）；
 *   2. `overwriteConfirmMessage(stats)` **不传第二参数时输出与改造前逐字相同**
 *      —— 单文件上传的确认框不能被文件夹功能改坏；
 *   3. 文件夹入口与降级提示的文案（与页面/验收 grep 对齐）。
 */
import assert from 'node:assert/strict'
import {
  FOLDER_DRAG_UNSUPPORTED_TIP, FOLDER_PICK_TEXT, SELECTION_LABEL_PREFIX,
  OVERWRITE_CONFIRM_TEXT, OVERWRITE_KEEP_TEXT,
  overwriteConfirmMessage, selectionText,
} from './overwriteGuard.js'

let pass = 0
const t = (name, fn) => {
  fn()
  pass += 1
  console.log(`  [OK ] ${name}`)
}

console.log('== selectionText ==')

t('没有文件 -> 空文案（不显示这一行）', () => {
  assert.equal(selectionText(0, 0), '')
  assert.equal(selectionText(undefined, undefined), '')
  assert.equal(selectionText(-1, 100), '')
})
t('单个文件 -> 已选 1 个文件 / 合计 X MB', () => {
  const s = selectionText(1, 1024 * 1024)
  assert.equal(s, '已选 1 个文件 / 合计 1.0 MB')
})
t('多个文件 + 小数 MB', () => {
  const s = selectionText(12, Math.round(3.47 * 1048576))
  assert.equal(s, '已选 12 个文件 / 合计 3.5 MB')
})
t('上千 MB 不再显示小数', () => {
  assert.equal(selectionText(3, 1500 * 1048576), '已选 3 个文件 / 合计 1500 MB')
})
t('小于 1 MB 显示两位小数', () => {
  assert.equal(selectionText(2, 1024), '已选 2 个文件 / 合计 0.00 MB')
})
t('文案含「已选」与「合计」（验收 grep 用）', () => {
  const s = selectionText(5, 5 * 1048576)
  assert.ok(s.includes(SELECTION_LABEL_PREFIX))
  assert.ok(s.includes('合计'))
})

console.log('== overwriteConfirmMessage 向后兼容 ==')

t('不传第二参数 -> 与改造前逐字相同', () => {
  const stats = { n_docs: 1038, n_items: 6244 }
  assert.equal(
    overwriteConfirmMessage(stats),
    '上传会替换当前平台数据（现有 1038 篇公告 / 6244 条标的）。'
      + '选择“覆盖并上传”将替换；选择“保留原数据并解析”将追加到现有数据之后。')
})
t('传第二参数 -> 先显示已选，再显示两种后果', () => {
  const msg = overwriteConfirmMessage({ n_docs: 1, n_items: 2 }, '已选 3 个文件 / 合计 1.0 MB')
  assert.ok(msg.startsWith('已选 3 个文件 / 合计 1.0 MB'))
  assert.ok(msg.includes('覆盖并上传'))
  assert.ok(msg.includes('保留原数据并解析'))
})
t('第二参数为空字符串 -> 退化为原行为', () => {
  const a = overwriteConfirmMessage({ n_docs: 2, n_items: 9 })
  const b = overwriteConfirmMessage({ n_docs: 2, n_items: 9 }, '')
  assert.equal(a, b)
})

console.log('== 文案契约 ==')

t('文件夹入口文案 = 「选择文件夹」', () => {
  assert.equal(FOLDER_PICK_TEXT, '选择文件夹')
})
t('降级提示说明「不支持拖入文件夹」且给出替代入口', () => {
  assert.match(FOLDER_DRAG_UNSUPPORTED_TIP, /不支持拖入文件夹/)
  assert.match(FOLDER_DRAG_UNSUPPORTED_TIP, /选择文件夹/)
})
t('覆盖确认按钮文案未被改动', () => {
  assert.equal(OVERWRITE_CONFIRM_TEXT, '覆盖并上传')
  assert.equal(OVERWRITE_KEEP_TEXT, '保留原数据并解析')
})

console.log(`\n通过 ${pass} 项，失败 0 项`)
