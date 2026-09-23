import { httpClient } from './httpClient'

export interface AuthUserInfo {
  user_id: string
  display_name: string
  role: string
}

export interface AuthPayload {
  user: AuthUserInfo
  first_account?: boolean
}

export const authApi = {
  register: async (username: string, password: string, displayName: string): Promise<AuthPayload> => {
    const response = await httpClient.post<AuthPayload>('/api/v1/auth/register', {
      username,
      password,
      display_name: displayName,
    })
    return response.data
  },
  login: async (username: string, password: string): Promise<AuthPayload> => {
    const response = await httpClient.post<AuthPayload>('/api/v1/auth/login', { username, password })
    return response.data
  },
  logout: async (): Promise<void> => {
    await httpClient.post('/api/v1/auth/logout')
  },
  me: async (): Promise<AuthUserInfo> => {
    const response = await httpClient.get<{ user: AuthUserInfo }>('/api/v1/auth/me')
    return response.data.user
  },
}
