/**
 * overwriteGuard 的无浏览器单元验证（只用 Node 内置能力，无依赖）。
 *
 * 跑法（在 app/task3/frontend 下）：
 *   "<node>" src/utils/overwriteGuard.test.mjs
 *
 * 覆盖三条契约：
 *   1. 要不要弹确认框（有数据才问、空库直接传）；
 *   2. 弹框动作 -> 入库模式：确认 = 覆盖、取消按钮 = **追加（仍然上传）**、
 *      X / Esc = 真正放弃（不发请求）；
 *   3. 确认/取消的文案（与页面、验收 grep 对齐）。
 */
import assert from 'node:assert/strict'
import {
  MODE_APPEND, MODE_REPLACE, OVERWRITE_CANCELLED_TIP, OVERWRITE_CONFIRM_TEXT,
  OVERWRITE_DROP_HINT, OVERWRITE_KEEP_TEXT, OVERWRITE_TITLE,
  UPLOAD_ACTION_CANCEL, UPLOAD_ACTION_CLOSE, UPLOAD_ACTION_CONFIRM,
  decideUploadMode, needsOverwriteConfirm, overwriteConfirmMessage,
} from './overwriteGuard.js'

let pass = 0
const t = (name, fn) => {
  fn()
  pass += 1
  console.log(`  [OK ] ${name}`)
}

console.log('== needsOverwriteConfirm ==')

t('有数据（1038 公告 / 6244 标的）-> 需要确认', () => {
  assert.equal(needsOverwriteConfirm({ n_docs: 1038, n_items: 6244 }), true)
})
t('只有公告、没有标的 -> 需要确认', () => {
  assert.equal(needsOverwriteConfirm({ n_docs: 3, n_items: 0 }), true)
})
t('只有标的、没有公告 -> 需要确认', () => {
  assert.equal(needsOverwriteConfirm({ n_docs: 0, n_items: 5 }), true)
})
t('空库（0 / 0）-> 不需要询问，直接上传', () => {
  assert.equal(needsOverwriteConfirm({ n_docs: 0, n_items: 0 }), false)
})
t('stats 尚未加载（undefined / null）-> 视为空库，不弹框', () => {
  assert.equal(needsOverwriteConfirm(undefined), false)
  assert.equal(needsOverwriteConfirm(null), false)
  assert.equal(needsOverwriteConfirm({}), false)
})
t('脏数据（负数 / 字符串 / NaN）-> 退化为 0，不误弹', () => {
  assert.equal(needsOverwriteConfirm({ n_docs: -3, n_items: 'abc' }), false)
  assert.equal(needsOverwriteConfirm({ n_docs: '2', n_items: NaN }), true)
})

console.log('== decideUploadMode：按钮语义 ==')

t('空库：无需询问 -> 直接 replace 上传', () => {
  assert.deepEqual(decideUploadMode({ needsConfirm: false, action: null }),
    { proceed: true, mode: MODE_REPLACE })
})
t('点「覆盖并上传」-> replace', () => {
  assert.deepEqual(decideUploadMode({ needsConfirm: true, action: UPLOAD_ACTION_CONFIRM }),
    { proceed: true, mode: MODE_REPLACE })
})
t('点「保留原数据并解析」-> append，而且**要上传**', () => {
  assert.deepEqual(decideUploadMode({ needsConfirm: true, action: UPLOAD_ACTION_CANCEL }),
    { proceed: true, mode: MODE_APPEND })
})
t('点 X / 按 Esc（close）-> 放弃：不发请求、原数据不变', () => {
  assert.deepEqual(decideUploadMode({ needsConfirm: true, action: UPLOAD_ACTION_CLOSE }),
    { proceed: false, mode: MODE_REPLACE })
})
t('未知动作 -> 一律按放弃处理（安全侧）', () => {
  assert.equal(decideUploadMode({ needsConfirm: true, action: undefined }).proceed, false)
  assert.equal(decideUploadMode({ needsConfirm: true, action: 'whatever' }).proceed, false)
})

console.log('== overwriteConfirmMessage ==')

t('正文含两种后果的规模数字与两个按钮的说法', () => {
  const msg = overwriteConfirmMessage({ n_docs: 1038, n_items: 6244 })
  assert.match(msg, /1038 篇公告/)
  assert.match(msg, /6244 条标的/)
  assert.match(msg, /替换当前平台数据/)
  assert.match(msg, /覆盖并上传/)
  assert.match(msg, /保留原数据并解析/)
  assert.match(msg, /追加到现有数据之后/)
})
t('空库正文也不崩（退化为 0）', () => {
  const msg = overwriteConfirmMessage({})
  assert.match(msg, /0 篇公告 \/ 0 条标的/)
})

console.log('== 文案契约（与页面/验收 grep 对齐）==')
t('确认按钮 = 「覆盖并上传」', () => assert.equal(OVERWRITE_CONFIRM_TEXT, '覆盖并上传'))
t('取消按钮 = 「保留原数据并解析」', () => assert.equal(OVERWRITE_KEEP_TEXT, '保留原数据并解析'))
t('关闭提示 = 「已取消，未做任何改动」', () => assert.equal(OVERWRITE_CANCELLED_TIP, '已取消，未做任何改动'))
t('标题非空且点明要选入库方式', () => assert.match(OVERWRITE_TITLE, /入库方式/))
t('拖拽区提示说明两种选择', () => assert.match(OVERWRITE_DROP_HINT, /覆盖当前数据/)
  && assert.match(OVERWRITE_DROP_HINT, /保留原数据并解析/))
t('模式常量与后端契约一致', () => {
  assert.equal(MODE_REPLACE, 'replace')
  assert.equal(MODE_APPEND, 'append')
})

console.log(`\n通过 ${pass} 项，失败 0 项`)
