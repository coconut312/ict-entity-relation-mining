/**
 * HTTP 客户端。
 *
 * 统一处理四件事，业务代码里不再重复：
 *   1. baseURL 与超时
 *   2. 自动附带 Bearer token
 *   3. 401 统一跳登录（登录接口自身除外）
 *   4. 请求耗时统计 —— 界面实时显示，便于感知响应速度
 */
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { clearAuth, getToken } from '@/store/auth'

const client = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

/** 最近一次请求的网络耗时（毫秒），供界面显示性能指标 */
export const perf = {
  lastMs: null,
  lastUrl: '',
  history: [],
}

client.interceptors.request.use((cfg) => {
  cfg.metadata = { start: performance.now() }
  const token = getToken()
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

const isLoginCall = (cfg) => (cfg?.url || '').includes('/auth/login')

client.interceptors.response.use(
  (res) => {
    const start = res.config?.metadata?.start
    if (start != null) {
      const ms = Math.round(performance.now() - start)
      perf.lastMs = ms
      perf.lastUrl = res.config.url
      perf.history.push(ms)
      if (perf.history.length > 50) perf.history.shift()
    }
    return res.data
  },
  (err) => {
    const res = err.response

    // 登录态失效：清空并回登录页（登录接口本身的 401 不跳转，交给页面提示）
    if (res?.status === 401 && !isLoginCall(err.config)) {
      clearAuth()
      if (!location.hash.startsWith('#/login')) {
        const back = encodeURIComponent(location.hash || '#/overview')
        location.hash = `#/login?redirect=${back}`
      }
      ElMessage.warning('登录已过期，请重新登录')
      return Promise.reject(new Error('unauthorized'))
    }

    if (res) {
      const d = res.data || {}
      const msg = d.message
        || (typeof d.detail === 'string' ? d.detail : '')
        || (Array.isArray(d.detail) ? '请求参数有误' : '')
        || `请求失败（${res.status}）`
      ElMessage.error(msg)
      return Promise.reject(new Error(msg))
    }
    if (err.code === 'ECONNABORTED') {
      ElMessage.error('请求超时，请稍后重试')
    } else {
      ElMessage.error('网络异常，请稍后重试')
    }
    return Promise.reject(err)
  },
)

export default client
