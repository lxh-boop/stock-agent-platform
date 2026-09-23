import { create } from 'zustand'
import { authApi, type AuthUserInfo } from '../api/authApi'

export type AuthStatus = 'unknown' | 'authed' | 'anonymous'

interface AuthState {
  user: AuthUserInfo | null
  status: AuthStatus
  bootstrap: () => Promise<void>
  setUser: (user: AuthUserInfo) => void
  markAnonymous: () => void
  logout: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: 'unknown',
  bootstrap: async () => {
    try {
      const user = await authApi.me()
      set({ user, status: 'authed' })
    } catch {
      set({ user: null, status: 'anonymous' })
    }
  },
  setUser: (user) => set({ user, status: 'authed' }),
  markAnonymous: () => set({ user: null, status: 'anonymous' }),
  logout: async () => {
    try {
      await authApi.logout()
    } catch {
      // 会话可能已失效，忽略登出接口错误，本地状态照常清理。
    }
    set({ user: null, status: 'anonymous' })
  },
}))
