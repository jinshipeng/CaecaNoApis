import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as loginApi, logout as logoutApi, getUserInfo as getUserInfoApi, refreshToken as refreshTokenApi } from '@/api/auth'
import type { LoginData, UserInfo } from '@/types/api'

interface TokenRefreshTimer {
  timer: ReturnType<typeof setTimeout> | null
  isRefreshing: boolean
}

export const useUserStore = defineStore('user', () => {
  const token = ref<string>(localStorage.getItem('token') || '')
  const userInfo = ref<UserInfo | null>(null)
  const loading = ref(false)
  const tokenRefreshState = ref<TokenRefreshTimer>({
    timer: null,
    isRefreshing: false
  })

  const isLoggedIn = computed(() => !!token.value)

  function validateUserInfo(data: any): data is UserInfo {
    if (!data || typeof data !== 'object') return false
    if (typeof data.id !== 'number' && typeof data.id !== 'string') return false
    if (typeof data.username !== 'string' || !data.username.trim()) return false
    return true
  }

  function sanitizeErrorMessage(error: unknown): string {
    if (error instanceof Error) {
      if (error.message.includes('401') || error.message.includes('Unauthorized')) {
        return '登录已过期，请重新登录'
      }
      if (error.message.includes('403') || error.message.includes('Forbidden')) {
        return '没有权限执行此操作'
      }
      if (error.message.includes('network') || error.message.includes('Network')) {
        return '网络连接失败，请检查网络设置'
      }
      if (error.message.includes('timeout')) {
        return '请求超时，请稍后重试'
      }
      return '操作失败，请稍后重试'
    }
    return '发生系统错误'
  }

  async function setupTokenRefresh(): Promise<void> {
    try {
      const payload = parseTokenPayload(token.value)
      if (!payload?.exp) return

      const currentTime = Math.floor(Date.now() / 1000)
      const expiresIn = payload.exp - currentTime
      const refreshThreshold = 300

      if (expiresIn > refreshThreshold && expiresIn < 3600) {
        const refreshDelay = (expiresIn - refreshThreshold) * 1000
        
        if (tokenRefreshState.value.timer) {
          clearTimeout(tokenRefreshState.value.timer)
        }

        tokenRefreshState.value.timer = setTimeout(async () => {
          await attemptTokenRefresh()
        }, refreshDelay)
      }
    } catch {
      console.warn('Failed to setup token refresh')
    }
  }

  function parseTokenPayload(token: string): any | null {
    try {
      const parts = token.split('.')
      if (parts.length !== 3) return null
      const payload = parts[1]
      const decoded = atob(payload.replace(/-/g, '+').replace(/_/g, '/'))
      return JSON.parse(decoded)
    } catch {
      return null
    }
  }

  async function attemptTokenRefresh(): Promise<boolean> {
    if (tokenRefreshState.value.isRefreshing) {
      return false
    }

    tokenRefreshState.value.isRefreshing = true

    try {
      const response = await refreshTokenApi()
      if (response && response.token) {
        token.value = response.token
        localStorage.setItem('token', response.token)
        await setupTokenRefresh()
        return true
      }
      return false
    } catch (error) {
      console.warn('Token refresh failed:', sanitizeErrorMessage(error))
      return false
    } finally {
      tokenRefreshState.value.isRefreshing = false
    }
  }

  async function login(data: LoginData) {
    try {
      loading.value = true
      
      if (!data.username || !data.password) {
        throw new Error('用户名和密码不能为空')
      }

      const response: any = await loginApi(data)
      
      if (!response || typeof response !== 'object') {
        throw new Error('服务器响应格式错误')
      }

      if (response.detail || response.error) {
        throw new Error(response.detail || response.error || '登录失败')
      }

      if (!response.token) {
        throw new Error('未收到有效的认证令牌')
      }

      token.value = response.token
      
      const newUserInfo: UserInfo = {
        id: response.user_id,
        username: response.username || '',
        email: response.email || '',
        full_name: response.full_name || response.username || '',
        department: response.department || '系统管理员'
      }

      if (validateUserInfo(newUserInfo)) {
        userInfo.value = newUserInfo
        localStorage.setItem('userInfo', JSON.stringify(newUserInfo))
      } else {
        console.warn('Received invalid user info format')
      }

      localStorage.setItem('token', response.token)
      
      await setupTokenRefresh()
      
      return Promise.resolve(response)
    } catch (error) {
      const sanitizedError = sanitizeErrorMessage(error)
      return Promise.reject(new Error(sanitizedError))
    } finally {
      loading.value = false
    }
  }

  async function getUserInfo() {
    try {
      loading.value = true
      const response = await getUserInfoApi()
      
      if (validateUserInfo(response)) {
        userInfo.value = response
        localStorage.setItem('userInfo', JSON.stringify(response))
      } else {
        console.warn('Invalid user info received from server')
      }
      
      return Promise.resolve(response)
    } catch (error) {
      const sanitizedError = sanitizeErrorMessage(error)
      return Promise.reject(new Error(sanitizedError))
    } finally {
      loading.value = false
    }
  }

  async function logout() {
    clearAuthData()
    logoutApi().catch(() => {})
  }

  function clearAuthData(): void {
    if (tokenRefreshState.value.timer) {
      clearTimeout(tokenRefreshState.value.timer)
      tokenRefreshState.value.timer = null
    }
    
    tokenRefreshState.value.isRefreshing = false
    token.value = ''
    userInfo.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('userInfo')
  }

  function checkAuth(): boolean {
    try {
      const savedToken = localStorage.getItem('token')
      const savedUserInfo = localStorage.getItem('userInfo')

      if (!savedToken) {
        return false
      }

      const jwtRegex = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/
      const djangoTokenRegex = /^[a-f0-9]{40}$/
      if (!jwtRegex.test(savedToken) && !djangoTokenRegex.test(savedToken) && savedToken.length < 20) {
        console.warn('Invalid token format in storage')
        clearAuthData()
        return false
      }

      if (djangoTokenRegex.test(savedToken)) {
        if (savedUserInfo) {
          try {
            userInfo.value = JSON.parse(savedUserInfo)
            token.value = savedToken
            return true
          } catch {
            clearAuthData()
            return false
          }
        }
        return true
      }

      try {
        const payload = parseTokenPayload(savedToken)
        if (payload?.exp) {
          const currentTime = Math.floor(Date.now() / 1000)
          if (payload.exp < currentTime) {
            console.warn('Token expired in storage')
            clearAuthData()
            return false
          }
        }
      } catch (parseError) {
        console.warn('Failed to parse token:', parseError)
        clearAuthData()
        return false
      }

      token.value = savedToken

      if (savedUserInfo) {
        try {
          const parsedInfo = JSON.parse(savedUserInfo)
          if (validateUserInfo(parsedInfo)) {
            userInfo.value = parsedInfo
          } else {
            console.warn('Invalid user info format in storage')
            localStorage.removeItem('userInfo')
          }
        } catch (parseError) {
          console.warn('Failed to parse user info:', parseError)
          localStorage.removeItem('userInfo')
        }
      }

      setupTokenRefresh()
      return true
    } catch (error) {
      console.error('Auth check failed:', error)
      clearAuthData()
      return false
    }
  }

  return {
    token,
    userInfo,
    loading,
    isLoggedIn,
    login,
    getUserInfo,
    logout,
    checkAuth,
    clearAuthData
  }
})
