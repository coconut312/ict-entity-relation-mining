/**
 * 轻量全局「数据版本」通知。
 *
 * 解决的问题：上传数据集并解析入库成功后，被 `<keep-alive>` 缓存着的数据视图
 * （标的检索、场景分析、实体补全）不知道库已经变了，用户必须手动刷新才看得到新数据。
 *
 * **为什么用 Vue `ref` 计数而不是 window 上的 CustomEvent**：
 *   1. 不往宿主页面挂全局对象，任何模块 `import` 即用，测试环境（Node，无 window）也能跑；
 *   2. `watch` 注册在组件作用域内，可随 `onScopeDispose` 自动解绑 —— 事件监听则要自己
 *      `removeEventListener`，漏一处就是内存泄漏；
 *   3. 计数天然可比大小，视图可以据此判断「我处理到哪个版本了」，
 *      天然避免重复请求与事件回环（`refetch` 自身绝不会再 bump）。
 *
 * 用法：
 *   bumpDataVersion()                          // 数据落库成功后调用一次（只在 Overview 调）
 *   useDataFreshness(() => doSearch(1))        // 视图里注册自动重查
 */
import { onActivated, onDeactivated, ref, watch } from 'vue'

/** 数据版本号：每次「上传 + 解析 + 落库成功」后 +1 */
export const dataVersion = ref(0)

/**
 * 通知所有数据视图：库内数据已变化。
 *
 * 注意：**同一个入库动作只调一次**。调用点多处会让视图重复查询；
 * 各视图的 refetch 里也不要再调它，否则会形成事件回环。
 */
export function bumpDataVersion () {
  dataVersion.value += 1
}

/**
 * 数据新鲜度：让视图在「数据落库成功」和「keep-alive 重新激活」时自动重查。
 *
 * 语义（每一条都是为了不重复请求、也不漏请求）：
 *   * 首次激活跳过 —— `onMounted` 已经拉过一次，不重复打同一个请求；
 *   * 版本变化时：视图**当前可见**才立即重查；被 keep-alive 缓存着（不可见）
 *     就不发请求，等重新激活时的那一次重查来覆盖（事件不会漏，也不会白打）；
 *   * 每次重新激活（离开再回来）都重查一次 —— 这正是「上传后不用手动刷新」的关键；
 *   * `refetch` 只读数据、不 bump 版本，因此不存在无限循环。
 *
 * @param {() => void} refetch 重新拉取本视图数据的函数（可 async，内部自行 catch）
 */
export function useDataFreshness (refetch) {
  const active = ref(true)      // 不在 keep-alive 内时永远是 true（onDeactivated 不会触发）
  let firstActivation = true

  watch(dataVersion, () => {
    if (active.value) refetch()
  })

  onActivated(() => {
    active.value = true
    if (firstActivation) {      // 首次激活 = 刚挂载，onMounted 已经查过
      firstActivation = false
      return
    }
    refetch()
  })

  onDeactivated(() => { active.value = false })
}
