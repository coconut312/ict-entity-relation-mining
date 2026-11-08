/**
 * 「数据集处理」支持直接上传 .html 的纯函数验证（不依赖浏览器，只用 Node 内置能力）。
 *
 * 跑法（在 app/task3/frontend 下）：
 *   "<node>" src/utils/htmlUpload.test.mjs
 *
 * 覆盖的契约（都是改 .html 时最容易踩坏的地方）：
 *   1. 扩展名白名单：`.html/.htm` 与 `.zip/.rar/.7z/.xlsx/.csv` 同级放行；
 *   2. `accept` 字面量与常量逐字一致（产物里就一个 accept，不能两处各说一套）；
 *   3. 平铺多选 .html -> 走 import-folder（路线②），相对路径 = 文件名；
 *   4. 单个 zip / 多个 zip / 带目录结构的文件夹 —— 上传通道与改造前一致（不回归）；
 *   5. 提示文案里必须出现「也可直接多选 .html 公告文件」（验收 grep 用）。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ALLOWED_EXTS, ARCHIVE_EXTS, HTML_DROP_HINT, HTML_EXTS, KIND_FOLDER, KIND_HTML,
  KIND_NONE, KIND_SINGLE, UPLOAD_ACCEPT, UPLOAD_SUPPORT_LINE,
  hasDirStructure, isAllowedName, isHtmlName, planUpload,
} from './htmlUpload.js'

const OVERVIEW = readFileSync(new URL('../views/Overview.vue', import.meta.url), 'utf8')

let pass = 0
const t = (name, fn) => {
  fn()
  pass += 1
  console.log(`  [OK ] ${name}`)
}

const f = (name) => ({ name, size: 10 })
const item = (name, relPath) => (relPath === undefined
  ? { file: f(name) }
  : { file: f(name), relPath })

console.log('== 扩展名白名单 ==')

t('.html / .htm 在白名单里，原 5 种压缩包/表格一个不少', () => {
  assert.deepEqual(ARCHIVE_EXTS, ['.zip', '.rar', '.7z', '.xlsx', '.csv'])
  assert.deepEqual(HTML_EXTS, ['.html', '.htm'])
  assert.deepEqual(ALLOWED_EXTS, [...ARCHIVE_EXTS, ...HTML_EXTS])
  assert.equal(UPLOAD_ACCEPT, '.zip,.rar,.7z,.xlsx,.csv,.html,.htm')
  for (const n of ['a.html', 'a.htm', 'a.HTML', 'a.zip', 'a.xlsx', 'a.csv']) {
    assert.ok(isAllowedName(n), `${n} 应被放行`)
  }
})

t('isHtmlName 大小写不敏感 / 不误判 .htmlzip', () => {
  assert.equal(isHtmlName('t20260202_26141082.html'), true)
  assert.equal(isHtmlName('公告.HTM'), true)
  assert.equal(isHtmlName('a.html.zip'), false)
  assert.equal(isHtmlName('a.xlsx'), false)
  assert.equal(isHtmlName(''), false)
})

console.log('== accept 与页面模板一致（产物 grep 用）==')

t('Overview.vue 的 accept 字面量 = UPLOAD_ACCEPT', () => {
  const m = OVERVIEW.match(/accept="([^"]+)"/)
  assert.ok(m, 'Overview.vue 里应仍有一个字面量 accept（而不是只有变量绑定）')
  assert.equal(m[1], UPLOAD_ACCEPT)
  assert.ok(m[1].includes('.html') && m[1].includes('.htm'))
})

t('页面提示改用常量（文案只有一处来源）', () => {
  assert.ok(OVERVIEW.includes('UPLOAD_SUPPORT_LINE'))
  assert.ok(OVERVIEW.includes('HTML_DROP_HINT'))
})

t('「支持 …」文案含 .html 多选与原有说法', () => {
  assert.ok(UPLOAD_SUPPORT_LINE.includes('也可直接多选 .html 公告文件'))
  assert.ok(UPLOAD_SUPPORT_LINE.includes('（含无 HTML 的单篇公告附件包）'))
  assert.ok(HTML_DROP_HINT.includes('.html'))
})

console.log('== 上传路线判定 planUpload ==')

t('3 个平铺 .html -> html-flat，相对路径就是文件名', () => {
  const plan = planUpload([
    item('t20260202_26141082.html'), item('t20260202_26141083.html'),
    item('t20260202_26141084.html')])
  assert.equal(plan.kind, KIND_HTML)
  assert.equal(plan.nHtml, 3)
  assert.equal(plan.nOther, 0)
  assert.deepEqual(plan.items.map((it) => it.relPath), [
    't20260202_26141082.html', 't20260202_26141083.html', 't20260202_26141084.html'])
  assert.ok(plan.note.includes('HTML 公告集合'))
})

t('单个平铺 .html 也走 import-folder（不能被单文件入口吃掉）', () => {
  const plan = planUpload([item('t20260202_26141082.html')])
  assert.equal(plan.kind, KIND_HTML)
  assert.deepEqual(plan.items.map((it) => it.relPath), ['t20260202_26141082.html'])
})

t('单个平铺 .zip / .xlsx -> 仍是单文件通道（行为不变）', () => {
  assert.equal(planUpload([item('data.zip')]).kind, KIND_SINGLE)
  assert.equal(planUpload([item('官方三表.xlsx')]).kind, KIND_SINGLE)
})

t('多个平铺 .zip -> 文件夹通道（行为不变）', () => {
  const plan = planUpload([item('a.zip'), item('b.zip')])
  assert.equal(plan.kind, KIND_FOLDER)
  assert.deepEqual(plan.items.map((it) => it.relPath), ['a.zip', 'b.zip'])
})

t('带目录结构的 html（拖入文件夹 / 选择文件夹）-> 文件夹通道，路径原样保留', () => {
  const plan = planUpload([
    item('t1.html', '赛题五.基准测试数据_html/t1.html'),
    item('t1_file.zip', '赛题五.基准测试数据_html/t1_file.zip')])
  assert.equal(plan.kind, KIND_FOLDER)
  assert.equal(plan.nHtml, 1)
  assert.deepEqual(plan.items.map((it) => it.relPath), [
    '赛题五.基准测试数据_html/t1.html', '赛题五.基准测试数据_html/t1_file.zip'])
})

t('混选 1 zip + 2 html -> 仍走 html-flat 且不丢 zip（附件包要能配对）', () => {
  const plan = planUpload([item('data.zip'), item('a.html'), item('b.html')])
  assert.equal(plan.kind, KIND_HTML)
  assert.equal(plan.nHtml, 2)
  assert.equal(plan.nOther, 1)
  assert.equal(plan.items.length, 3)
  assert.ok(plan.note.includes('_file.zip'))
})

t('平铺多个附件（无 html，如 2 个 pdf）-> 文件夹通道，附件不被扩展名挡掉', () => {
  const plan = planUpload([item('说明书.pdf'), item('报价单.pdf')])
  assert.equal(plan.kind, KIND_FOLDER)
  assert.equal(plan.items.length, 2)
})

t('空选择 -> none；relPath 缺省时回退文件名', () => {
  assert.equal(planUpload([]).kind, KIND_NONE)
  assert.equal(planUpload(undefined).kind, KIND_NONE)
  assert.equal(planUpload([{}, null]).kind, KIND_NONE)
  assert.equal(planUpload([{ file: f('x.html') }]).items[0].relPath, 'x.html')
})

t('hasDirStructure 同时认 / 与 \\', () => {
  assert.equal(hasDirStructure([item('a.html')]), false)
  assert.equal(hasDirStructure([item('a.html', 'dir/a.html')]), true)
  assert.equal(hasDirStructure([item('a.html', 'dir\\a.html')]), true)
})

console.log('== 页面接线 ==')

t('Overview.vue 用 planUpload 选通道，并放开多选', () => {
  assert.ok(OVERVIEW.includes('planUpload'))
  assert.ok(/multiple/.test(OVERVIEW), 'el-upload 需要 multiple 才能一次多选多个 .html')
})

console.log(`\n通过 ${pass} 项，失败 0 项`)
