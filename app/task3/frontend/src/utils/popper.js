/**
 * el-select 下拉浮层的统一定位策略。
 *
 * 背景（实测问题）：Element Plus 的 fitInputWidth 默认为 false，下拉面板宽度
 * 会**按选项文本撑开**。品牌名很短（"大华 (113)"）时面板窄、位置正常；
 * 而采购方/供应商是长名称（"山东第一医科大学第一附属医院（山东省千佛山医院）(7)"），
 * 面板被撑得很宽，popperjs 的 preventOverflow 为避免溢出视口会把它**向左平移**
 * （必要时还翻到上方），于是出现「下拉不在选择框下面」。
 *
 * 因此这里：
 *   strategy: 'fixed'                  相对视口定位，内容区滚动时也不会错位
 *   preventOverflow.mainAxis: false    禁止左右平移 —— 始终左对齐选择框
 *   flip.fallbackPlacements: []        禁止翻到上方 —— 始终在选择框下方
 *   -- 同时组件侧开启 fit-input-width，让面板宽度 = 选择框宽度（超长文本省略号截断）
 */
export const SELECT_POPPER = {
  strategy: 'fixed',
  modifiers: [
    { name: 'preventOverflow', options: { mainAxis: false, altAxis: true, padding: 8 } },
    { name: 'flip', options: { fallbackPlacements: [] } },
  ],
}
