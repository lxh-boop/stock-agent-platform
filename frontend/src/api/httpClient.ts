import axios from 'axios'

export const httpClient = axios.create({
  baseURL: '/',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

// 会话过期时统一跳转登录页；/auth 接口自身的 401 由调用方（登录页、authStore）处理。
httpClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const url = String(error?.config?.url ?? '')
    if (status === 401 && !url.includes('/api/v1/auth/') && window.location.pathname !== '/login') {
      window.location.assign('/login')
    }
    return Promise.reject(error)
  },
)
