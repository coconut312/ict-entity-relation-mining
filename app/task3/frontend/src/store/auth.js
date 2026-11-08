/**
 * 登录态。
 *
 * 用模块级 reactive 对象而不是 Pinia —— 只有 token 和用户名两个字段，
 * 引入状态管理库不划算（也更符合"能少一个依赖就少一个"的取舍）。
 *
 * token 存 localStorage：刷新页面不会被踢下线。
 */
import { reactive, readonly } from 'vue'

const KEY_TOKEN = 'edm_token'
const KEY_USER = 'edm_user'
const KEY_ROLE = 'edm_role'

const state = reactive({
  token: localStorage.getItem(KEY_TOKEN) || '',
  username: localStorage.getItem(KEY_USER) || '',
  role: localStorage.getItem(KEY_ROLE) || '',
})

export const auth = readonly(state)

export const isLoggedIn = () => !!state.token

export function setAuth({ token, username, role }) {
  state.token = token || ''
  state.username = username || ''
  state.role = role || 'user'
  localStorage.setItem(KEY_TOKEN, state.token)
  localStorage.setItem(KEY_USER, state.username)
  localStorage.setItem(KEY_ROLE, state.role)
}

export function clearAuth() {
  state.token = ''
  state.username = ''
  state.role = ''
  localStorage.removeItem(KEY_TOKEN)
  localStorage.removeItem(KEY_USER)
  localStorage.removeItem(KEY_ROLE)
}

export const getToken = () => state.token
