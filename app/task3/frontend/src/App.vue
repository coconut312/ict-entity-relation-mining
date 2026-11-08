<template>
  <!-- 登录页不套壳：全屏居中卡片 -->
  <router-view v-if="isLoginPage" />

  <el-container v-else class="edm-layout">
    <!-- 顶栏：只留标识、状态与账号，不放业务操作按钮，减少视觉噪音 -->
    <el-header class="edm-header" height="56px">
      <div class="edm-brand">
        <div class="edm-brand__mark">ICT</div>
        <div class="edm-brand__text">
          <div class="edm-brand__title">招采数据智能分析平台</div>
          <div class="edm-brand__sub">智能实体挖掘与关系建模</div>
        </div>
      </div>

      <div class="edm-header__right">
        <el-tag v-if="health" :type="health.has_data ? 'success' : 'info'"
                effect="light" size="small" round>
          <el-icon><component :is="health.has_data ? 'CircleCheck' : 'InfoFilled'" /></el-icon>
          <span style="margin-left:4px">
            {{ health.has_data ? '数据已就绪' : '暂无数据' }}
          </span>
        </el-tag>

        <!-- 实测耗时直接展示，便于感知响应速度 -->
        <span class="edm-perf" :class="perfClass" title="最近一次查询耗时">
          <el-icon><Timer /></el-icon>
          {{ perf.lastMs == null ? '—' : perf.lastMs + ' ms' }}
        </span>

        <el-dropdown trigger="click" @command="onCommand">
          <span class="edm-user">
            <el-icon><UserFilled /></el-icon>
            <span>{{ auth.username || '未登录' }}</span>
            <el-icon class="edm-user__caret"><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>
                角色：{{ auth.role === 'admin' ? '管理员' : '普通用户' }}
              </el-dropdown-item>
              <el-dropdown-item divided command="logout">
                <el-icon><SwitchButton /></el-icon> 退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>

    <el-container>
      <el-aside class="edm-sidebar">
        <el-menu :default-active="activeMenu" router>
          <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <span>{{ m.title }}</span>
          </el-menu-item>
        </el-menu>

        <div class="edm-sidebar__foot">
          <div class="edm-dim" style="font-size:11.5px">v0.1.0</div>
        </div>
      </el-aside>

      <el-main class="edm-main">
        <router-view v-slot="{ Component }">
          <transition name="edm-route" mode="out-in">
            <keep-alive :max="3">
              <component :is="Component" />
            </keep-alive>
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowDown, SwitchButton, Timer, UserFilled,
} from '@element-plus/icons-vue'
import { getHealth, logout } from '@/api'
import { perf } from '@/api/client'
import { auth, clearAuth } from '@/store/auth'

const route = useRoute()
const router = useRouter()
const health = ref(null)

const menus = [
  { path: '/overview', title: '数据总览', icon: 'DataAnalysis' },
  { path: '/items', title: '标的检索', icon: 'Search' },
  { path: '/scenarios', title: '场景分析', icon: 'Share' },
  { path: '/settings', title: '模型配置', icon: 'Setting' },
]

const isLoginPage = computed(() => route.name === 'login')
const activeMenu = computed(() => route.path)
const perfClass = computed(() => {
  const ms = perf.lastMs
  if (ms == null) return ''
  return ms < 1000 ? 'edm-perf--good' : 'edm-perf--warn'
})

const loadHealth = async () => {
  if (isLoginPage.value) return
  try {
    health.value = await getHealth()
  } catch (e) { /* 拦截器已提示 */ }
}

const onCommand = async (cmd) => {
  if (cmd !== 'logout') return
  try { await logout() } catch (e) { /* 令牌可能已失效，忽略 */ }
  clearAuth()
  ElMessage.success('已退出登录')
  router.replace({ name: 'login' })
}

onMounted(loadHealth)
watch(() => route.name, loadHealth)
</script>

<style scoped>
.edm-sidebar { display: flex; flex-direction: column; }
.edm-sidebar__foot {
  margin-top: auto;
  padding: 12px 12px 4px;
  border-top: 1px solid var(--edm-border);
}

.edm-user {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 13px; color: var(--edm-text-2);
  cursor: pointer; outline: none;
  padding: 4px 8px; border-radius: var(--edm-radius-sm);
  transition: background .15s ease;
}
.edm-user:hover { background: #f2f5fa; color: var(--edm-primary); }
.edm-user__caret { font-size: 11px; }
</style>
