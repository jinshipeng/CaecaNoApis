import axios, { AxiosInstance, AxiosRequestConfig, AxiosResponse, CancelTokenSource } from 'axios'
import { ElMessage, ElLoading } from 'element-plus'
import router from '@/router'
import { useLoadingStore } from '@/stores/loading'

interface CacheItem {
  data: any
  timestamp: number
}

interface InflightRequest {
  promise: Promise<any>
  resolve: (value: any) => void
  reject: (reason?: any) => void
  timestamp: number
}

interface RequestConfig extends AxiosRequestConfig {
  showLoading?: boolean
  retry?: number
  retryDelay?: number
  loadingText?: string
  deduplication?: boolean
  deduplicationKey?: string
  skipErrorHandler?: boolean
}

interface LoadingInstance {
  instance: ReturnType<typeof ElLoading.service>
  count: number
  requestId: string
}

const requestCache = new Map<string, CacheItem>()
const inflightRequests = new Map<string, InflightRequest>()
const cancelTokenSources = new Map<string, CancelTokenSource>()
const activeLoadingInstances = new Map<string, LoadingInstance>()
const CACHE_TIMEOUT = 60000
const DEFAULT_RETRY = 2
const DEFAULT_RETRY_DELAY = 1000
const DEDUPLICATION_TIMEOUT = 2000
const MAX_CONCURRENT_LOADING = 3

let globalLoadingCount = 0
let globalLoadingInstance: ReturnType<typeof ElLoading.service> | null = null

const service: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  },
  withCredentials: true
})

function getCacheKey(config: AxiosRequestConfig): string {
  const url = config.url || ''
  const params = config.params ? JSON.stringify(config.params) : ''
  const data = config.data ? JSON.stringify(config.data) : ''
  return `${config.method}:${url}:${params}:${data}`
}

function getRequestId(config: AxiosRequestConfig): string {
  const cached = (config as any).__requestId
  if (cached) return cached

  const url = config.url || ''
  const method = config.method?.toUpperCase() || 'GET'
  const timestamp = Date.now()
  const id = `${method}:${url}:${timestamp}`
  ;(config as any).__requestId = id
  return id
}

function getDeduplicationKey(config: RequestConfig): string {
  if (config.deduplicationKey) {
    return config.deduplicationKey
  }
  
  const url = config.url || ''
  const method = config.method?.toUpperCase() || 'GET'
  const params = config.params ? JSON.stringify(config.params) : ''
  const data = config.data && method !== 'GET' ? JSON.stringify(config.data) : ''
  
  return `${method}:${url}:${params}:${data}`
}

function validateToken(token: string | null): boolean {
  if (!token) return false
  if (token.length < 10) return false

  const jwtRegex = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/
  if (jwtRegex.test(token)) {
    try {
      const payload = token.split('.')[1]
      if (!payload) return false
      const decodedPayload = JSON.parse(atob(payload))
      if (decodedPayload.exp) {
        const currentTime = Math.floor(Date.now() / 1000)
        const bufferTime = 60
        if (decodedPayload.exp < currentTime + bufferTime) {
          console.warn('Token is about to expire or already expired')
          return false
        }
      }
      return true
    } catch {
      return false
    }
  }

  const djangoTokenRegex = /^[a-f0-9]{40}$/
  if (djangoTokenRegex.test(token)) return true

  return token.length >= 20 && /^[A-Za-z0-9]+$/.test(token)
}

function manageGlobalLoading(show: boolean, text?: string): void {
  if (show) {
    globalLoadingCount++
    
    if (globalLoadingCount === 1 && !globalLoadingInstance) {
      globalLoadingInstance = ElLoading.service({
        lock: true,
        text: text || '加载中...',
        background: 'rgba(0, 0, 0, 0.7)',
        customClass: 'global-loading-overlay'
      })
    } else if (globalLoadingInstance) {
      const loadingText = globalLoadingInstance.$el?.querySelector('.el-loading-text') as HTMLElement
      if (loadingText) {
        loadingText.textContent = text || '加载中...'
      }
    }
  } else {
    globalLoadingCount = Math.max(0, globalLoadingCount - 1)
    
    if (globalLoadingCount === 0 && globalLoadingInstance) {
      globalLoadingInstance.close()
      globalLoadingInstance = null
    }
  }
}

function showNetworkReconnectionPrompt(): void {
  const existingPrompt = document.getElementById('network-reconnection-prompt')
  if (existingPrompt) return

  const promptDiv = document.createElement('div')
  promptDiv.id = 'network-reconnection-prompt'
  promptDiv.innerHTML = `
    <div style="
      position: fixed;
      top: 20px;
      left: 50%;
      transform: translateX(-50%);
      background: #fef0f0;
      color: #f56c6c;
      padding: 12px 24px;
      border-radius: 4px;
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.15);
      z-index: 9999;
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 14px;
    ">
      <span>网络连接已断开，正在尝试重新连接...</span>
      <button id="manual-reconnect-btn" style="
        background: #f56c6c;
        color: white;
        border: none;
        padding: 4px 12px;
        border-radius: 3px;
        cursor: pointer;
        font-size: 12px;
      ">手动重连</button>
    </div>
  `
  
  document.body.appendChild(promptDiv)
  
  const reconnectBtn = document.getElementById('manual-reconnect-btn')
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', () => {
      window.location.reload()
    })
  }

  setTimeout(() => {
    if (document.getElementById('network-reconnection-prompt')) {
      promptDiv.remove()
    }
  }, 10000)
}

export function getErrorMessage(error: any, fallback = '操作失败，请稍后重试'): string {
  const data = error?.response?.data
  const status = error?.response?.status

  const collect = (value: any): string[] => {
    if (!value) return []
    if (typeof value === 'string') return [value]
    if (Array.isArray(value)) return value.flatMap(collect)
    if (typeof value === 'object') return Object.values(value).flatMap(collect)
    return [String(value)]
  }

  const messages = collect(data)
  const firstMessage = messages.find(Boolean)
  const rawText = [firstMessage, error?.message, String(status || '')].filter(Boolean).join(' ')

  if (/已存在|already exists|unique|duplicate|完整性冲突|UNIQUE constraint/i.test(rawText)) {
    return firstMessage && firstMessage.includes('已存在')
      ? firstMessage
      : '该数据已存在，请检查后再提交'
  }

  if (firstMessage) {
    return firstMessage.length > 100 ? fallback : firstMessage
  }

  if (error?.message) return error.message
  return fallback
}

function sanitizeErrorMessage(error: any): string {
  if (!error) return '系统错误'
  
  if (error.response?.data?.detail) {
    const detail = error.response.data.detail
    if (typeof detail === 'string') {
      if (detail.toLowerCase().includes('credential') || 
          detail.toLowerCase().includes('authentication')) {
        return '认证失败，请重新登录'
      }
      if (detail.toLowerCase().includes('permission') || 
          detail.toLowerCase().includes('forbidden')) {
        return '权限不足，无法执行此操作'
      }
      return detail.length > 100 ? '操作失败，请稍后重试' : detail
    }
  }
  
  if (error.message) {
    if (error.message.includes('timeout')) {
      return '请求超时，请检查网络连接后重试'
    }
    if (error.message.includes('Network Error') || error.message.includes('network')) {
      return '网络连接失败，请检查网络设置'
    }
    if (error.message.includes('401')) {
      return '登录已过期，请重新登录'
    }
    if (error.message.includes('403')) {
      return '没有权限执行此操作'
    }
    if (error.message.includes('404')) {
      return '请求的资源不存在'
    }
    if (error.message.includes('500')) {
      return '服务器内部错误，请联系管理员'
    }
  }

  return getErrorMessage(error)
}

service.interceptors.request.use(
  (config: any) => {
    const token = localStorage.getItem('token')

    // 只在token通过格式验证时添加认证头
    // 不再因格式问题主动清除token，由后端401响应统一处理
    if (token && validateToken(token)) {
      config.headers.Authorization = `Token ${token}`
    } else if (token && !validateToken(token)) {
      console.warn('Token format may be invalid, sending request without auth header')
      // 不清除token、不拒绝请求 — 让后端返回401时再由response拦截器统一处理
    }

    if (config.data instanceof FormData) {
      delete config.headers['Content-Type']
    }

    const requestId = getRequestId(config)
    const source = axios.CancelToken.source()
    cancelTokenSources.set(requestId, source)
    config.cancelToken = source.token

    if (config.deduplication !== false) {
      const dedupeKey = getDeduplicationKey(config)
      const existingRequest = inflightRequests.get(dedupeKey)

      if (existingRequest) {
        if (Date.now() - existingRequest.timestamp < DEDUPLICATION_TIMEOUT) {
          source.cancel('Duplicate request cancelled')
          return existingRequest.promise
        }
        inflightRequests.delete(dedupeKey)
      }
    }

    if (config.showLoading !== false) {
      if (activeLoadingInstances.size < MAX_CONCURRENT_LOADING) {
        const loadingStore = useLoadingStore()
        loadingStore.startLoading(requestId, config.loadingText)
        
        const existingLoading = activeLoadingInstances.get(requestId)
        if (existingLoading) {
          existingLoading.count++
        } else {
          activeLoadingInstances.set(requestId, {
            instance: {} as any,
            count: 1,
            requestId
          })
        }
      } else {
        manageGlobalLoading(true, config.loadingText)
      }
    }

    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

const handleResponseError = (error: any): Promise<any> => {
  if (axios.isCancel(error)) {
    console.debug('Request cancelled:', error.message)
    return Promise.reject(error)
  }

  const shouldSkipErrorHandling = error.config?.skipErrorHandler
  if (shouldSkipErrorHandling) {
    return Promise.reject(error)
  }

  if (error.response) {
    const status = error.response.status
    const sanitizedMessage = sanitizeErrorMessage(error)

    switch (status) {
      case 401:
        localStorage.removeItem('token')
        localStorage.removeItem('userInfo')
        
        const currentPath = window.location.pathname
        if (currentPath !== '/login') {
          ElMessage.error(sanitizedMessage)
          setTimeout(() => {
            router.push('/login')
          }, 1000)
        }
        break
        
      case 403:
        ElMessage.error(sanitizedMessage)
        break
        
      case 404:
        ElMessage.error(sanitizedMessage)
        break
        
      case 400:
      case 409:
        ElMessage.error(sanitizedMessage)
        break
        
      case 429:
        ElMessage.warning('请求过于频繁，请稍后重试')
        break
        
      case 500:
      case 502:
      case 503:
      case 504:
        ElMessage.error(sanitizedMessage)
        break
        
      default:
        if (sanitizedMessage !== '操作失败，请稍后重试') {
          ElMessage.error(sanitizedMessage)
        }
    }
  } else if (error.message) {
    const sanitizedMessage = sanitizeErrorMessage(error)
    
    if (error.message.includes('Network Error') || 
        error.message.includes('network') ||
        error.code === 'ERR_NETWORK') {
      showNetworkReconnectionPrompt()
      ElMessage.error(sanitizedMessage)
    } else if (error.message.includes('timeout')) {
      ElMessage.error(sanitizedMessage)
    } else if (!error.message.includes('cancelled') && 
               !error.message.includes('Duplicate')) {
      ElMessage.error(sanitizedMessage)
    }
  } else {
    ElMessage.error('请求处理失败')
  }

  return Promise.reject(error)
}

service.interceptors.response.use(
  (response: AxiosResponse) => {
    const requestId = getRequestId(response.config)
    cancelTokenSources.delete(requestId)
    
    const config = response.config as any
    
    if (config.deduplication !== false) {
      const dedupeKey = getDeduplicationKey(config)
      inflightRequests.delete(dedupeKey)
    }
    
    if (config.showLoading !== false) {
      const loadingInstance = activeLoadingInstances.get(requestId)
      if (loadingInstance) {
        loadingInstance.count--
        if (loadingInstance.count <= 0) {
          const loadingStore = useLoadingStore()
          loadingStore.stopLoading(requestId)
          activeLoadingInstances.delete(requestId)
        }
      } else {
        manageGlobalLoading(false)
      }
    }
    
    return response.data
  },
  (error) => {
    const errorConfig = error.config as any
    const requestId = errorConfig ? getRequestId(errorConfig) : `error-${Date.now()}`
    
    if (errorConfig) {
      cancelTokenSources.delete(getRequestId(errorConfig))
      
      if (errorConfig.deduplication !== false) {
        const dedupeKey = getDeduplicationKey(errorConfig)
        inflightRequests.delete(dedupeKey)
      }
      
      if (errorConfig.showLoading !== false) {
        const loadingInstance = activeLoadingInstances.get(requestId)
        if (loadingInstance) {
          loadingInstance.count--
          if (loadingInstance.count <= 0) {
            const loadingStore = useLoadingStore()
            loadingStore.stopLoading(requestId)
            activeLoadingInstances.delete(requestId)
          }
        } else {
          manageGlobalLoading(false)
        }
      }
    }
    
    return handleResponseError(error)
  }
)

export function cancelPendingRequests(url?: string): void {
  if (url) {
    const requestId = getRequestId({ url })
    const source = cancelTokenSources.get(requestId)
    if (source) {
      source.cancel('请求已取消')
      cancelTokenSources.delete(requestId)
    }
  } else {
    cancelTokenSources.forEach((source) => {
      source.cancel('请求已取消')
    })
    cancelTokenSources.clear()
  }
  
  activeLoadingInstances.forEach((_loading, key) => {
    const loadingStore = useLoadingStore()
    loadingStore.stopLoading(key)
  })
  activeLoadingInstances.clear()
  
  if (globalLoadingInstance) {
    globalLoadingInstance.close()
    globalLoadingInstance = null
  }
  globalLoadingCount = 0
}

export function clearAllCaches(): void {
  requestCache.clear()
  inflightRequests.clear()
}

export function requestWithRetry(config: RequestConfig, retryCount: number = 0): Promise<any> {
  const maxRetry = config.retry ?? DEFAULT_RETRY
  const retryDelay = config.retryDelay ?? DEFAULT_RETRY_DELAY

  return service(config).catch((error) => {
    if (retryCount < maxRetry && !axios.isCancel(error)) {
      const shouldRetry = !error.response || 
        (error.response.status >= 500 || error.response.status === 429)
      
      if (shouldRetry) {
        console.log(`Retrying request (${retryCount + 1}/${maxRetry})...`)
        
        return new Promise((resolve) => {
          setTimeout(() => {
            resolve(requestWithRetry(config, retryCount + 1))
          }, retryDelay * Math.pow(2, retryCount))
        })
      }
    }
    return Promise.reject(error)
  })
}

export function requestWithCache(config: RequestConfig): Promise<any> {
  const cacheKey = getCacheKey(config)
  
  if (config.method === 'get' && config.params?._useCache) {
    const cached = requestCache.get(cacheKey)
    if (cached && Date.now() - cached.timestamp < CACHE_TIMEOUT) {
      return Promise.resolve(cached.data)
    }
  }

  const dedupeKey = getDeduplicationKey(config)
  const existingInflight = inflightRequests.get(dedupeKey)
  
  if (existingInflight && Date.now() - existingInflight.timestamp < DEDUPLICATION_TIMEOUT) {
    return existingInflight.promise
  }

  let resolveFn: (value: any) => void
  let rejectFn: (reason?: any) => void
  
  const promise = new Promise((resolve, reject) => {
    resolveFn = resolve
    rejectFn = reject
  })

  inflightRequests.set(dedupeKey, {
    promise,
    resolve: resolveFn!,
    reject: rejectFn!,
    timestamp: Date.now()
  })

  requestWithRetry(config)
    .then((data) => {
      if (config.method === 'get' && config.params?._useCache) {
        requestCache.set(cacheKey, {
          data,
          timestamp: Date.now()
        })
      }
      const inflightData = inflightRequests.get(dedupeKey)
      if (inflightData) {
        inflightData.resolve(data)
        inflightRequests.delete(dedupeKey)
      }
    })
    .catch((error) => {
      const inflightData = inflightRequests.get(dedupeKey)
      if (inflightData) {
        inflightData.reject(error)
        inflightRequests.delete(dedupeKey)
      }
    })

  return promise
}

export function checkNetworkStatus(): boolean {
  return navigator.onLine
}

export function setupNetworkStatusListener(): void {
  window.addEventListener('online', () => {
    ElMessage.success('网络连接已恢复')
    const prompt = document.getElementById('network-reconnection-prompt')
    if (prompt) {
      prompt.remove()
    }
  })
  
  window.addEventListener('offline', () => {
    ElMessage.warning('网络连接已断开')
    showNetworkReconnectionPrompt()
  })
}

const typedRequest = {
  get: <T = any>(url: string, config?: RequestConfig): Promise<T> =>
    service.get(url, config as any) as any,
  post: <T = any>(url: string, data?: any, config?: RequestConfig): Promise<T> =>
    service.post(url, data, config as any) as any,
  put: <T = any>(url: string, data?: any, config?: RequestConfig): Promise<T> =>
    service.put(url, data, config as any) as any,
  delete: <T = any>(url: string, config?: RequestConfig): Promise<T> =>
    service.delete(url, config as any) as any,
  patch: <T = any>(url: string, data?: any, config?: RequestConfig): Promise<T> =>
    service.patch(url, data, config as any) as any,
}

export { service }

export default typedRequest
