<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Cpu } from '@element-plus/icons-vue'
import { login } from '@/api/auth'
import type { LoginResponse } from '@/types/api'

const router = useRouter()

interface LoginForm {
  username: string
  password: string
}

const loginForm = ref<LoginForm>({
  username: '',
  password: ''
})

const loading = ref(false)
const errorMsg = ref<string>('')

const validateForm = (): boolean => {
  if (!loginForm.value.username.trim()) {
    errorMsg.value = '请输入用户名'
    return false
  }
  if (!loginForm.value.password.trim()) {
    errorMsg.value = '请输入密码'
    return false
  }
  if (loginForm.value.password.length < 6) {
    errorMsg.value = '密码长度至少为6位'
    return false
  }
  return true
}

const handleLogin = async (): Promise<void> => {
  errorMsg.value = ''

  if (!validateForm()) {
    return
  }

  loading.value = true

  try {
    const response: LoginResponse = await login({
      username: loginForm.value.username,
      password: loginForm.value.password
    })

    if (response && response.token) {
      localStorage.setItem('token', response.token)
      localStorage.setItem('userInfo', JSON.stringify({
        id: response.user_id,
        username: response.username || loginForm.value.username,
        full_name: response.username || loginForm.value.username,
        email: response.email
      }))

      router.push('/dashboard')
    } else {
      errorMsg.value = '登录响应异常，请重试'
    }
  } catch (error: unknown) {
    const err = error as { response?: { data?: { detail?: string } }; message?: string }

    if (err.response?.data?.detail) {
      const detail = String(err.response.data.detail)
      if (detail.toLowerCase().includes('credential') ||
          detail.toLowerCase().includes('authentication') ||
          detail.toLowerCase().includes('invalid')) {
        errorMsg.value = '用户名或密码错误'
      } else if (detail.toLowerCase().includes('permission') ||
                 detail.toLowerCase().includes('forbidden')) {
        errorMsg.value = '账号已被禁用，请联系管理员'
      } else {
        errorMsg.value = '登录失败，请检查用户名和密码'
      }
    } else if (err.message) {
      if (err.message.includes('timeout')) {
        errorMsg.value = '请求超时，请检查网络连接后重试'
      } else if (err.message.includes('Network Error') || err.message.includes('network')) {
        errorMsg.value = '网络连接失败，请检查网络设置'
      } else {
        errorMsg.value = '登录失败，请稍后重试'
      }
    } else {
      errorMsg.value = '登录失败，请检查用户名和密码'
    }

    console.error('Login error:', error)
  } finally {
    loading.value = false
  }
}

const handleKeyPress = (event: KeyboardEvent): void => {
  if (event.key === 'Enter' && !loading.value) {
    handleLogin()
  }
}
</script>

<template>
  <div class="login-container">
    <div class="login-card">
      <div class="login-header">
        <div class="logo"><Cpu /></div>
        <h1 class="title">联宝智能供应链系统</h1>
        <p class="subtitle">智能供应链预测与决策平台</p>
      </div>

      <form class="login-form" @submit.prevent="handleLogin">
        <div class="form-group">
          <input
            v-model="loginForm.username"
            type="text"
            placeholder="用户名"
            class="form-input"
            :disabled="loading"
            autocomplete="username"
            @keypress="handleKeyPress"
          />
        </div>

        <div class="form-group">
          <input
            v-model="loginForm.password"
            type="password"
            placeholder="密码"
            class="form-input"
            :disabled="loading"
            autocomplete="current-password"
            @keypress="handleKeyPress"
          />
        </div>

        <button
          type="submit"
          class="submit-btn"
          :disabled="loading"
          :class="{ loading: loading }"
        >
          {{ loading ? '登录中...' : '登 录' }}
        </button>
      </form>

      <div v-if="errorMsg" class="error-message">
        <p>{{ errorMsg }}</p>
      </div>

      <div class="login-footer">
        <p>默认账号: admin / admin123</p>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.login-container {
  width: 100%;
  height: 100vh;
  background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%);
  display: flex;
  align-items: center;
  justify-content: center;
}

.login-card {
  width: 100%;
  max-width: 420px;
  padding: 48px;
  background: rgba(5, 13, 26, 0.9);
  border: 1px solid rgba(64, 158, 255, 0.15);
  border-radius: 20px;
  backdrop-filter: blur(30px);

  .login-header {
    text-align: center;
    margin-bottom: 32px;

    .logo {
      width: 56px;
      height: 56px;
      margin-left: auto;
      margin-right: auto;
      margin-bottom: 16px;
      color: #6e9ef7;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 14px;
      background: rgba(110, 158, 247, 0.12);
      border: 1px solid rgba(110, 158, 247, 0.22);

      svg {
        width: 30px;
        height: 30px;
      }
    }

    .title {
      color: #e2e8f0;
      font-size: 24px;
      font-weight: 700;
      margin: 0 0 8px 0;
    }

    .subtitle {
      color: #606266;
      font-size: 13px;
      margin: 0;
    }
  }

  .login-form {
    display: flex;
    flex-direction: column;
    gap: 20px;

    .form-group {
      width: 100%;

      .form-input {
        width: 100%;
        padding: 14px 16px;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        color: #e2e8f0;
        font-size: 14px;
        outline: none;
        transition: all 0.3s ease;
        box-sizing: border-box;

        &::placeholder {
          color: #606266;
        }

        &:focus {
          border-color: rgba(64, 158, 255, 0.5);
          background: rgba(64, 158, 255, 0.05);
          box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.1);
        }

        &:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
      }
    }

    .submit-btn {
      width: 100%;
      padding: 14px;
      background: linear-gradient(135deg, #409EFF, #67C23A);
      border: none;
      border-radius: 12px;
      color: white;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.3s ease;

      &:hover:not(:disabled) {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(64, 158, 255, 0.35);
      }

      &:active:not(:disabled) {
        transform: translateY(0);
      }

      &:disabled {
        opacity: 0.7;
        cursor: not-allowed;
      }

      &.loading {
        animation: pulse 1.5s infinite;
      }
    }
  }

  .error-message {
    padding: 12px 16px;
    background: rgba(255, 77, 79, 0.1);
    border: 1px solid rgba(255, 77, 79, 0.3);
    border-radius: 8px;
    margin-top: 20px;

    p {
      color: #FF4D4F;
      font-size: 14px;
      margin: 0;
    }
  }

  .login-footer {
    text-align: center;
    margin-top: 24px;
    padding-top: 20px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);

    p {
      color: #4a4c4e;
      font-size: 12px;
      font-family: monospace;
      margin: 0;
    }
  }
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.8;
  }
}

@media (max-width: 480px) {
  .login-card {
    padding: 32px 24px;
    margin: 16px;

    .login-header {
      .logo {
        width: 48px;
        height: 48px;

        svg {
          width: 26px;
          height: 26px;
        }
      }

      .title {
        font-size: 20px;
      }
    }

    .login-form {
      .form-group {
        .form-input {
          padding: 12px 14px;
          font-size: 13px;
        }
      }

      .submit-btn {
        padding: 12px;
        font-size: 15px;
      }
    }
  }
}
</style>
