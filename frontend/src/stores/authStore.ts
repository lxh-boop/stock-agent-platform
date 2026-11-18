import { create } from 'zustand'
import { authApi, type AuthUserInfo } from '../api/authApi'
import { useTaskStore } from './taskStore'

export type AuthStatus = 'unknown' | 'authed' | 'anonymous'

interface AuthState {
  user: AuthUserInfo | null
  status: AuthStatus
  bootstrap: () => Promise<void>
  setUser: (user: AuthUserInfo) => void
  markAnonymous: () => void
  logout: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set, get) => {
  const setUser = (user: AuthUserInfo) => {
    const previous = get().user
    const task = useTaskStore.getState().task
    if ((previous && previous.user_id !== user.user_id) || (task && task.owner_id !== user.user_id)) {
      useTaskStore.getState().clear()
    }
    set({ user, status: 'authed' })
  }
  const markAnonymous = () => {
    useTaskStore.getState().clear()
    set({ user: null, status: 'anonymous' })
  }
  return {
    user: null,
    status: 'unknown',
    bootstrap: async () => {
      try {
        setUser(await authApi.me())
      } catch {
        markAnonymous()
      }
    },
    setUser,
    markAnonymous,
    logout: async () => {
      markAnonymous()
      try {
        await authApi.logout()
      } catch {
        // 会话可能已失效，忽略登出接口错误，本地状态已清理。
      }
    },
  }
})
