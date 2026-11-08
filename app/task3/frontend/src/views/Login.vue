<template>
  <div class="edm-login">
    <div class="edm-login__card">
      <div class="edm-login__brand">
        <div class="edm-login__mark">ICT</div>
        <div>
          <div class="edm-login__title">招采数据智能分析平台</div>
          <div class="edm-login__sub">实体挖掘 · 关系分析 · 可视化检索</div>
        </div>
      </div>

      <el-form :model="form" @submit.prevent="submit" size="large">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" clearable
                    :prefix-icon="User" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码"
                    show-password :prefix-icon="Lock" autocomplete="current-password"
                    @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width:100%"
                   size="large" @click="submit">
          登 录
        </el-button>
      </el-form>

      <!-- 演示账号提示：直接展示，省去现场沟通 -->
      <div class="edm-login__hint">
        <el-icon><InfoFilled /></el-icon>
        <span>演示账号：<b>reviewer</b> / <b>ict2026</b></span>
      </div>

      <div class="edm-login__foot">
        © 2026 招采数据智能分析平台
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { InfoFilled, Lock, User } from '@element-plus/icons-vue'
import { getMe, login } from '@/api'
import { setAuth } from '@/store/auth'

const router = useRouter()
const route = useRoute()

const form = reactive({ username: 'reviewer', password: '' })
const loading = ref(false)

const submit = async () => {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const res = await login(form.username.trim(), form.password)
    setAuth(res)
    // 顺手确认一次令牌可用，避免带着无效令牌进主界面
    try { await getMe() } catch (e) { /* 忽略，拦截器会处理 */ }
    const to = route.query.redirect || '/overview'
    router.replace(typeof to === 'string' ? to : '/overview')
    ElMessage.success(`欢迎，${res.username}`)
  } catch (e) {
    /* 错误提示已由拦截器给出 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.edm-login {
  height: 100%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(1000px 500px at 12% -10%, #dbeafe 0%, transparent 55%),
    radial-gradient(900px 460px at 105% 110%, #e0e7ff 0%, transparent 55%),
    var(--edm-bg);
  padding: 24px;
}

.edm-login__card {
  width: 380px;
  max-width: 100%;
  background: var(--edm-surface);
  border: 1px solid var(--edm-border);
  border-radius: 14px;
  box-shadow: 0 8px 32px rgba(16, 24, 40, .08), 0 2px 8px rgba(16, 24, 40, .04);
  padding: 30px 30px 22px;
  /* 入场：轻微上浮就位。不用缩放，避免"弹出"的廉价感 */
  animation: loginIn 360ms cubic-bezier(0.4, 0, 0.2, 1) both;
}

@keyframes loginIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}

/* 品牌区与账号提示做 60/140ms 的阶梯，让视线自上而下走一遍 */
.edm-login__brand { animation: loginIn 360ms cubic-bezier(0.4, 0, 0.2, 1) 60ms both; }
.edm-login__hint { animation: loginIn 360ms cubic-bezier(0.4, 0, 0.2, 1) 140ms both; }

.edm-login__brand {
  display: flex; align-items: center; gap: 12px; margin-bottom: 24px;
}
.edm-login__mark {
  width: 42px; height: 42px; border-radius: 11px;
  background: linear-gradient(135deg, #2563eb, #4f8ef7);
  color: #fff; display: grid; place-items: center;
  font-weight: 700; font-size: 15px; letter-spacing: -.5px;
  flex: none;
}
.edm-login__title { font-size: 16px; font-weight: 650; line-height: 1.3; }
.edm-login__sub { font-size: 12px; color: var(--edm-text-3); margin-top: 2px; }

.edm-login__hint {
  display: flex; align-items: center; gap: 6px;
  margin-top: 16px; padding: 9px 12px;
  background: var(--edm-primary-soft);
  border-radius: var(--edm-radius-sm);
  font-size: 12.5px; color: #1e40af;
}
.edm-login__hint b { font-weight: 650; }

.edm-login__foot {
  margin-top: 20px; padding-top: 14px;
  border-top: 1px solid var(--edm-border);
  text-align: center; font-size: 11.5px; color: var(--edm-text-3);
  line-height: 1.7;
}
</style>
