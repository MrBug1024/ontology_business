<template>
  <main class="auth-page">
    <button class="auth-theme" type="button" :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'" @click="toggleTheme">
      <el-icon aria-hidden="true"><component :is="theme === 'light' ? 'Moon' : 'Sunny'" /></el-icon>
    </button>
    <section class="auth-intro">
      <div class="auth-brand">
        <div class="auth-mark"><span></span><span></span><span></span></div>
        <div>
          <div class="brand-name">本体智能平台</div>
          <div class="brand-caption">ONTOLOGY AI WORKSPACE</div>
        </div>
      </div>
      <div class="intro-copy">
        <div class="eyebrow">AI · ONTOLOGY · WORKFLOW</div>
        <h1>把复杂业务，<br /><em>变成可协作的智能系统。</em></h1>
        <p>从业务场景与本体模型出发，连接真实数据、工作流与 Agent，让每一次对话都更接近业务结果。</p>
      </div>
      <div class="intro-foot"><span class="status-dot"></span> 私有工作区 · 安全隔离</div>
    </section>

    <section class="auth-panel">
      <div class="auth-card">
        <template v-if="mode === 'login'">
          <div class="form-heading">
            <div class="form-kicker">WELCOME BACK</div>
            <h2>登录工作台</h2>
            <p>进入你的业务智能空间</p>
          </div>
          <el-form @submit.prevent="submit">
            <el-form-item label="邮箱">
              <el-input v-model="email" size="large" type="email" placeholder="name@company.com" autocomplete="email">
                <template #prefix><el-icon><Message /></el-icon></template>
              </el-input>
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="password" size="large" :type="showPassword ? 'text' : 'password'" placeholder="请输入密码" autocomplete="current-password">
                <template #prefix><el-icon><Lock /></el-icon></template>
                <template #suffix><button class="toggle-pass" type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><el-icon aria-hidden="true"><component :is="showPassword ? 'View' : 'Hide'" /></el-icon></button></template>
              </el-input>
            </el-form-item>
            <div class="form-row"><span></span><button type="button" class="link-button" @click="mode = 'forgot'">忘记密码？</button></div>
            <div v-if="error" class="form-error" role="alert" aria-live="assertive"><el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ error }}</div>
            <el-button class="submit-button" native-type="submit" type="primary" size="large" :loading="loading">登录 <el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="switch-line">还没有账户？<button class="link-button" @click="mode = 'register'; error = ''">创建个人工作区</button></div>
        </template>

        <template v-else-if="mode === 'register'">
          <div class="form-heading"><div class="form-kicker">GET STARTED</div><h2>创建工作区</h2><p>使用邮箱注册你的个人租户</p></div>
          <el-form @submit.prevent="submit">
            <el-form-item label="邮箱"><el-input v-model="email" size="large" type="email" placeholder="name@company.com" autocomplete="email"><template #prefix><el-icon><Message /></el-icon></template></el-input></el-form-item>
            <el-form-item label="显示名称"><el-input v-model="displayName" size="large" placeholder="你的名字或团队名称" autocomplete="name"><template #prefix><el-icon><User /></el-icon></template></el-input></el-form-item>
            <el-form-item label="密码"><el-input v-model="password" size="large" :type="showPassword ? 'text' : 'password'" placeholder="至少 8 位字符" autocomplete="new-password"><template #prefix><el-icon><Lock /></el-icon></template><template #suffix><button class="toggle-pass" type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><el-icon aria-hidden="true"><component :is="showPassword ? 'View' : 'Hide'" /></el-icon></button></template></el-input></el-form-item>
            <el-form-item label="确认密码"><el-input v-model="passwordConfirm" size="large" :type="showConfirmPassword ? 'text' : 'password'" placeholder="再次输入密码" autocomplete="new-password"><template #prefix><el-icon><Lock /></el-icon></template><template #suffix><button class="toggle-pass" type="button" :aria-label="showConfirmPassword ? '隐藏确认密码' : '显示确认密码'" @click="showConfirmPassword = !showConfirmPassword"><el-icon aria-hidden="true"><component :is="showConfirmPassword ? 'View' : 'Hide'" /></el-icon></button></template></el-input></el-form-item>
            <div v-if="error" class="form-error" role="alert" aria-live="assertive"><el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ error }}</div>
            <el-button class="submit-button" native-type="submit" type="primary" size="large" :loading="loading">发送验证邮件 <el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="switch-line">已经有账户？<button class="link-button" @click="mode = 'login'; error = ''">返回登录</button></div>
        </template>

        <template v-else-if="mode === 'verify'">
          <div class="form-heading"><div class="form-kicker">CHECK YOUR INBOX</div><h2>验证邮箱</h2><p>验证码已发送至 <strong>{{ email }}</strong></p></div>
          <el-form @submit.prevent="submit">
            <el-form-item label="6 位验证码"><el-input v-model="code" size="large" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="请输入邮件中的验证码" class="code-input"><template #prefix><el-icon><Key /></el-icon></template></el-input></el-form-item>
            <div v-if="error" class="form-error" role="alert" aria-live="assertive"><el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ error }}</div>
            <el-button class="submit-button" native-type="submit" type="primary" size="large" :loading="loading">完成验证 <el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="switch-line"><button class="link-button" :disabled="resending" @click="resend">{{ resending ? '发送中…' : '重新发送验证码' }}</button><span class="dot-sep">·</span><button class="link-button" @click="mode = 'login'">返回登录</button></div>
        </template>

        <template v-else-if="mode === 'forgot'">
          <div class="form-heading"><div class="form-kicker">ACCOUNT RECOVERY</div><h2>找回密码</h2><p>输入注册邮箱，我们会发送重置验证码</p></div>
          <el-form @submit.prevent="submit">
            <el-form-item label="邮箱"><el-input v-model="email" size="large" type="email" autocomplete="email" placeholder="name@company.com"><template #prefix><el-icon><Message /></el-icon></template></el-input></el-form-item>
            <div v-if="error" class="form-error" role="alert" aria-live="assertive"><el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ error }}</div>
            <el-button class="submit-button" native-type="submit" type="primary" size="large" :loading="loading">发送重置邮件 <el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="switch-line"><button class="link-button" @click="mode = 'login'">返回登录</button></div>
        </template>

        <template v-else>
          <div class="form-heading"><div class="form-kicker">SET A NEW PASSWORD</div><h2>重置密码</h2><p>输入邮件中的验证码并设置新密码</p></div>
          <el-form @submit.prevent="submit">
            <el-form-item label="验证码"><el-input v-model="code" size="large" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="6 位验证码"><template #prefix><el-icon><Key /></el-icon></template></el-input></el-form-item>
            <el-form-item label="新密码"><el-input v-model="password" size="large" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="至少 8 位字符"><template #prefix><el-icon><Lock /></el-icon></template><template #suffix><button class="toggle-pass" type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><el-icon aria-hidden="true"><component :is="showPassword ? 'View' : 'Hide'" /></el-icon></button></template></el-input></el-form-item>
            <el-form-item label="确认密码"><el-input v-model="passwordConfirm" size="large" :type="showConfirmPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="再次输入密码"><template #prefix><el-icon><Lock /></el-icon></template><template #suffix><button class="toggle-pass" type="button" :aria-label="showConfirmPassword ? '隐藏确认密码' : '显示确认密码'" @click="showConfirmPassword = !showConfirmPassword"><el-icon aria-hidden="true"><component :is="showConfirmPassword ? 'View' : 'Hide'" /></el-icon></button></template></el-input></el-form-item>
            <div v-if="error" class="form-error" role="alert" aria-live="assertive"><el-icon aria-hidden="true"><WarningFilled /></el-icon>{{ error }}</div>
            <el-button class="submit-button" native-type="submit" type="primary" size="large" :loading="loading">确认重置 <el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="switch-line"><button class="link-button" @click="mode = 'login'">返回登录</button></div>
        </template>
      </div>
      <div class="auth-note">© 2026 Ontology AI Platform · 你的数据属于你的工作区</div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'

type AuthMode = 'login' | 'register' | 'verify' | 'forgot' | 'reset'
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const mode = ref<AuthMode>('login')
const email = ref(String(route.query.email || ''))
const displayName = ref('')
const password = ref('')
const passwordConfirm = ref('')
const code = ref('')
const error = ref('')
const loading = ref(false)
const resending = ref(false)
const showPassword = ref(false)
const showConfirmPassword = ref(false)
const theme = ref<'light' | 'dark'>((localStorage.getItem('ontology-theme') as 'light' | 'dark') || 'light')

function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  document.documentElement.dataset.theme = theme.value
  localStorage.setItem('ontology-theme', theme.value)
  window.dispatchEvent(new CustomEvent('ontology-theme-change', { detail: theme.value }))
}

function messageFromError(e: any, fallback: string) {
  return e?.response?.data?.detail || e?.message || fallback
}

async function submit() {
  error.value = ''
  loading.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(email.value, password.value)
      await router.replace(String(route.query.redirect || '/dashboard'))
    } else if (mode.value === 'register') {
      await api.register({ email: email.value, password: password.value, password_confirm: passwordConfirm.value, display_name: displayName.value })
      mode.value = 'verify'
      ElMessage.success('验证邮件已发送')
    } else if (mode.value === 'verify') {
      await api.verifyEmail({ email: email.value, code: code.value })
      mode.value = 'login'
      password.value = ''
      ElMessage.success('邮箱验证成功，请登录')
    } else if (mode.value === 'forgot') {
      await api.forgotPassword(email.value)
      mode.value = 'reset'
      ElMessage.success('如果邮箱已注册，重置验证码已发送')
    } else {
      await api.resetPassword({ email: email.value, code: code.value, password: password.value, password_confirm: passwordConfirm.value })
      mode.value = 'login'
      password.value = ''
      passwordConfirm.value = ''
      ElMessage.success('密码已重置')
    }
  } catch (e: any) {
    error.value = messageFromError(e, '操作失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

async function resend() {
  resending.value = true
  error.value = ''
  try {
    await api.resendCode(email.value)
    ElMessage.success('新的验证码已发送')
  } catch (e: any) {
    error.value = messageFromError(e, '发送失败')
  } finally {
    resending.value = false
  }
}
</script>

<style scoped>
.auth-page { min-height: 100dvh; display: grid; grid-template-columns: minmax(420px, 0.92fr) minmax(480px, 1.08fr); background: var(--auth-bg); color: var(--text); overflow-x: hidden; }
.auth-theme { position: fixed; top: 24px; right: 28px; z-index: 5; width: 44px; height: 44px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text-2); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; box-shadow: var(--shadow-xs); }
.auth-theme:hover { color: var(--primary); border-color: var(--border-strong); background: var(--primary-soft); }
.auth-intro { padding: 48px clamp(42px, 7vw, 120px); display: flex; flex-direction: column; justify-content: space-between; background: var(--auth-intro-bg); position: relative; overflow: hidden; }
.auth-intro::before { content: ''; position: absolute; width: 580px; height: 580px; right: -270px; top: -190px; border: 1px solid var(--auth-line); border-radius: 50%; box-shadow: 0 0 0 34px transparent, 0 0 0 35px var(--auth-line); opacity: .6; }
.auth-intro::after { content: ''; position: absolute; inset: 0; background-image: linear-gradient(var(--auth-grid) 1px, transparent 1px), linear-gradient(90deg, var(--auth-grid) 1px, transparent 1px); background-size: 52px 52px; mask-image: linear-gradient(135deg, rgba(0,0,0,.5), transparent 65%); pointer-events: none; }
.auth-brand, .intro-copy, .intro-foot { position: relative; z-index: 1; }
.auth-brand { display: flex; align-items: center; gap: 13px; }
.auth-mark { width: 42px; height: 42px; border: 1px solid var(--auth-mark-border); border-radius: 13px; display: flex; align-items: center; justify-content: center; gap: 4px; background: var(--auth-mark-bg); }
.auth-mark span { width: 6px; height: 19px; background: var(--auth-mark); border-radius: 5px; transform: rotate(35deg); }
.auth-mark span:nth-child(2) { height: 25px; opacity: .7; transform: rotate(-35deg); }
.auth-mark span:nth-child(3) { width: 5px; height: 13px; opacity: .42; }
.brand-name { font-size: 15px; font-weight: 750; letter-spacing: .02em; }
.brand-caption, .form-kicker, .eyebrow { font-size: 10px; letter-spacing: .18em; font-weight: 700; color: var(--text-3); }
.brand-caption { margin-top: 3px; color: var(--auth-muted); }
.intro-copy { max-width: 540px; margin: auto 0; }
.eyebrow { color: var(--auth-accent); margin-bottom: 22px; }
.intro-copy h1 { margin: 0; font-size: clamp(34px, 4.2vw, 58px); line-height: 1.13; letter-spacing: -.055em; font-weight: 720; }
.intro-copy h1 em { color: var(--auth-accent); font-style: normal; }
.intro-copy p { max-width: 455px; color: var(--auth-muted); font-size: 15px; line-height: 1.9; margin: 26px 0 0; }
.intro-foot { color: var(--auth-muted); font-size: 12px; display: flex; align-items: center; gap: 8px; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: #67a89c; box-shadow: 0 0 0 4px rgba(103,168,156,.13); }
.auth-panel { display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 40px 28px; background: var(--auth-panel-bg); }
.auth-card { width: min(100%, 420px); padding: 42px 42px 34px; background: var(--surface); border: 1px solid var(--border); border-radius: 18px; box-shadow: var(--shadow-md); }
.form-heading { margin-bottom: 30px; }
.form-kicker { color: var(--primary); margin-bottom: 11px; }
.form-heading h2 { margin: 0; font-size: 28px; line-height: 1.25; letter-spacing: -.035em; }
.form-heading p { margin: 8px 0 0; color: var(--text-2); font-size: 13px; }
.form-heading strong { color: var(--primary); font-weight: 650; }
.auth-card :deep(.el-form-item) { margin-bottom: 18px; }
.auth-card :deep(.el-form-item__label) { color: var(--text-2); font-size: 12px; font-weight: 650; padding-bottom: 6px; line-height: 1.2; }
.auth-card :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--border) inset; background: var(--surface-2); border-radius: 10px; }
.auth-card :deep(.el-input__wrapper:hover) { box-shadow: 0 0 0 1px var(--border-strong) inset; }
.toggle-pass { width: 32px; height: 32px; border: 0; padding: 0; border-radius: 8px; background: transparent; cursor: pointer; color: var(--text-3); display: inline-flex; align-items: center; justify-content: center; }
.toggle-pass:hover { color: var(--primary-600); background: var(--primary-soft); }
.form-row { display: flex; justify-content: space-between; margin: 0 0 20px; }
.link-button { border: 0; background: none; padding: 0; color: var(--primary); cursor: pointer; font: inherit; font-size: 12px; font-weight: 650; }
.link-button:hover { color: var(--primary-600); text-decoration: underline; }
.link-button:disabled { opacity: .5; cursor: default; text-decoration: none; }
.form-error { display: flex; align-items: center; gap: 6px; padding: 9px 11px; color: var(--danger); background: var(--danger-soft); border-radius: 8px; font-size: 12px; margin: 4px 0 14px; }
.submit-button { width: 100%; margin-top: 4px; height: 44px; letter-spacing: .01em; }
.submit-button .el-icon { margin-left: 7px; }
.switch-line { text-align: center; margin-top: 26px; color: var(--text-3); font-size: 12px; }
.dot-sep { margin: 0 8px; color: var(--border-strong); }
.auth-note { color: var(--text-3); font-size: 11px; margin-top: 24px; }

@media (max-width: 920px) {
  .auth-page { display: block; }
  .auth-intro { min-height: 250px; padding: 28px 26px; }
  .intro-copy { margin: 42px 0 0; }
  .intro-copy h1 { font-size: 34px; }
  .intro-copy p, .intro-foot { display: none; }
  .auth-panel { min-height: calc(100vh - 250px); padding: 30px 18px; }
  .auth-card { padding: 32px 24px 28px; }
  .auth-theme { top: 18px; right: 18px; }
}
</style>
