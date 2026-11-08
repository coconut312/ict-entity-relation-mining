/**
 * dataVersion（全局数据版本通知）的无浏览器单元验证。
 *
 * 跑法（在 app/task3/frontend 下）：
 *   "<node>" src/utils/dataVersion.test.mjs
 *
 * 覆盖「上传成功后各数据视图自动重查」这条链路的四个关键性质：
 *   1. bump 一次 -> 每个注册的视图重查一次（不重不漏）；
 *   2. 同一 tick 内多次 bump 会被 Vue 调度器合并（不会重复打请求）；
 *   3. refetch 自己绝不会再 bump —— 不存在事件回环 / 无限请求；
 *   4. 视图销毁（effectScope 停止）后不再被唤醒，不留悬空监听。
 *
 * 说明：`onActivated` / `onDeactivated` 只有在组件 setup 内调用才有实例可挂，
 * 纯脚本环境（本测试）里 Vue 会打 warning 但不影响 `watch` 驱动的核心逻辑，
 * 因此测试期间静音 console.warn。
 */
import assert from 'node:assert/strict'
import { effectScope, nextTick } from 'vue'

console.warn = () => {}          // 静音「无组件实例」的 lifecycle warning

const { bumpDataVersion, dataVersion, useDataFreshness } = await import('./dataVersion.js')

let pass = 0
const t = async (name, fn) => {
  await fn()
  pass += 1
  console.log(`  [OK ] ${name}`)
}

const base = dataVersion.value

console.log('== dataVersion / bumpDataVersion ==')

await t('初始为 ref(0) 语义：数字、可直接读', () => {
  assert.equal(typeof dataVersion.value, 'number')
  assert.equal(base, 0)
})
await t('bump 一次只 +1（同一入库动作只广播一次）', () => {
  bumpDataVersion()
  assert.equal(dataVersion.value, base + 1)
})

console.log('== useDataFreshness ==')

await t('注册本身不发请求；bump 一次 -> 重查恰好一次', async () => {
  let calls = 0
  const scope = effectScope()
  scope.run(() => useDataFreshness(() => { calls += 1 }))
  assert.equal(calls, 0, '注册不应触发请求')
  bumpDataVersion()
  // watch 默认 pre-flush：回调在微任务里执行（组件里表现为「本帧渲染前」）
  await nextTick()
  assert.equal(calls, 1)
  scope.stop()
})

await t('同一 tick 内多次 bump -> 调度器合并成一次重查（不重复打请求）', async () => {
  let calls = 0
  const scope = effectScope()
  scope.run(() => useDataFreshness(() => { calls += 1 }))
  bumpDataVersion()
  bumpDataVersion()
  bumpDataVersion()
  await nextTick()
  assert.equal(calls, 1, 'pre-flush 队列按 watcher 去重')
  scope.stop()
})

await t('分开 tick 的多次 bump（多次上传）-> 每次各重查一次，不丢', async () => {
  let calls = 0
  const scope = effectScope()
  scope.run(() => useDataFreshness(() => { calls += 1 }))
  for (let i = 1; i <= 3; i += 1) {
    bumpDataVersion()
    await nextTick()
    assert.equal(calls, i)
  }
  scope.stop()
})

await t('多个视图各自重查一次（互不影响）', async () => {
  const hits = { items: 0, stats: 0, scenario: 0 }
  const scope = effectScope()
  scope.run(() => {
    useDataFreshness(() => { hits.items += 1 })
    useDataFreshness(() => { hits.stats += 1 })
    useDataFreshness(() => { hits.scenario += 1 })
  })
  bumpDataVersion()
  await nextTick()
  assert.deepEqual(hits, { items: 1, stats: 1, scenario: 1 })
  scope.stop()
})

await t('refetch 内部不 bump -> 不会形成无限循环', async () => {
  let calls = 0
  const scope = effectScope()
  scope.run(() => useDataFreshness(() => { calls += 1 }))
  const before = dataVersion.value
  bumpDataVersion()
  await nextTick()
  assert.equal(calls, 1)
  assert.equal(dataVersion.value, before + 1, 'refetch 不应改变版本号')
  scope.stop()
})

await t('视图销毁后不再被唤醒（watch 随作用域释放）', async () => {
  let calls = 0
  const scope = effectScope()
  scope.run(() => useDataFreshness(() => { calls += 1 }))
  bumpDataVersion()
  await nextTick()
  assert.equal(calls, 1)
  scope.stop()
  bumpDataVersion()
  await nextTick()
  assert.equal(calls, 1, '停止作用域后不应再触发')
})

console.log(`\n通过 ${pass} 项，失败 0 项`)
