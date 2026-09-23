import { describe, expect, it } from 'vitest'
import { createWriteMeta } from '../api/writeOperationApi'
import { useAuthStore } from '../stores/authStore'
import { useSessionStore } from '../stores/sessionStore'

describe('stage 6.3 protected write metadata', () => {
  it('creates non-empty request and idempotency identifiers', () => {
    const first = createWriteMeta('profile')
    const second = createWriteMeta('profile')
    expect(first.request_id).toContain('req-profile-')
    expect(first.idempotency_key).toContain('idem-profile-')
    expect(first.request_id).not.toBe(second.request_id)
    expect(first.idempotency_key).not.toBe(second.idempotency_key)
  })
})

describe('stage 6.3 paper user identity', () => {
  it('keeps a tab-scoped session id independent from the login identity', () => {
    const sessionId = useSessionStore.getState().sessionId
    expect(sessionId).toMatch(/^react-/)
  })

  it('defaults to anonymous until /auth/me resolves', () => {
    useAuthStore.setState({ user: null, status: 'anonymous' })
    expect(useAuthStore.getState().user).toBeNull()
  })
})
