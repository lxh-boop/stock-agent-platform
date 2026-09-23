import { useAuthStore } from '../stores/authStore'

// 登录用户的 user_id 是唯一身份来源；后端同样以登录会话强制覆盖 user_id。
export function useCurrentUserId(): string {
  return useAuthStore((state) => state.user?.user_id ?? 'default')
}
