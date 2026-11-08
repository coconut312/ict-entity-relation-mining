import { createRouter, createWebHashHistory } from 'vue-router'
import { isLoggedIn } from '@/store/auth'

/**
 * 路由表。
 *
 * 业务页面只有三个，对应三块能力，不设额外层级：
 *   数据总览    数据集导入与处理 + 数据质量
 *   标的检索    标的信息的全字段检索、筛选、查看
 *   场景分析    采购方与供应商的关系分析
 *
 * 另加登录页与「模型配置」页（页面快捷输入自己的 API Key）。
 *
 * 用 hash 路由：评测机上由 FastAPI 单进程托管静态文件，
 * 不需要服务端做 SPA 回退，部署最省事。
 */
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录', public: true },
  },
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: () => import('@/views/Overview.vue'),
    meta: { title: '数据总览', icon: 'DataAnalysis' },
  },
  {
    path: '/items',
    name: 'items',
    component: () => import('@/views/ItemSearch.vue'),
    meta: { title: '标的检索', icon: 'Search' },
  },
  {
    path: '/scenarios',
    name: 'scenarios',
    component: () => import('@/views/ScenarioWorkbench.vue'),
    meta: { title: '场景分析', icon: 'Share' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/ModelSettings.vue'),
    meta: { title: '模型配置', icon: 'Setting' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

/** 未登录一律先去登录页，登录后回到原来想去的页面 */
router.beforeEach((to) => {
  if (to.meta?.public) {
    if (to.name === 'login' && isLoggedIn()) return { path: '/overview' }
    return true
  }
  if (!isLoggedIn()) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta?.title
    ? `${to.meta.title} · 招采数据智能分析平台`
    : '招采数据智能分析平台'
})

export default router
