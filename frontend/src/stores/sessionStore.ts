import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// 身份（ownerId）已由登录态（authStore + 后端会话）接管，这里只保留
// 标识当前浏览器标签页生命周期的 sessionId，用于任务刷新恢复。
interface SessionState {
  sessionId: string
}

function randomSessionId(): string {
  return `react-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export const useSessionStore = create<SessionState>()(
  persist(
    () => ({
      sessionId: randomSessionId(),
    }),
    {
      name: 'stock-stage6-session',
      version: 3,
      migrate: (persistedState) => {
        const state = (persistedState ?? {}) as Partial<SessionState>
        return { sessionId: String(state.sessionId || randomSessionId()) }
      },
    },
  ),
)
