import { describe, it, expect, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

type WsHandler = (e: { data: Record<string, unknown> }) => void
const wsHandlers = new Map<string, WsHandler[]>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: WsHandler) => {
      wsHandlers.set(type, [...(wsHandlers.get(type) ?? []), fn])
      return () => {
        wsHandlers.set(type, (wsHandlers.get(type) ?? []).filter(h => h !== fn))
      }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  for (const h of wsHandlers.get(type) ?? []) h({ data })
}

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

apiGet.mockImplementation(async (url: string) => {
  if (url.endsWith('/members')) {
    return [
      { user_id: 'u-o', display_name: 'Olivia', role: 'owner', joined_at: '2026-01-01' },
      { user_id: 'u-a', display_name: 'Adam', role: 'admin', joined_at: '2026-01-01' },
      { user_id: 'u-m', display_name: 'Mo', role: 'moderator', joined_at: '2026-01-01' },
      { user_id: 'u-b', display_name: 'Bob', role: 'member', joined_at: '2026-01-01' },
    ]
  }
  return []
})

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-m', display_name: 'Mo', is_admin: false, picture_url: null } },
}))

describe('SpaceMemberList', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceMemberList')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('badges owner, admin and moderator; a moderator manages nobody', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="moderator" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--moderator')).toBeTruthy())
    expect(r.container.querySelector('.sh-badge--moderator')!.textContent).toBe('Moderator')
    expect(r.container.querySelector('.sh-badge--admin')!.textContent).toBe('Admin')
    expect(r.container.querySelector('.sh-badge--owner')!.textContent).toBe('Owner')
    // Content authority only — no member-management kebab, no invites.
    expect(r.container.querySelector('.sh-post-overflow')).toBeNull()
    expect(r.queryByText('+ Invite by code')).toBeNull()
  })

  it("refetches when the host's role update arrives, not for other config", async () => {
    // v_47: a stub's forwarded role change lands as the host's role
    // broadcast, which the backend re-emits as space.config.changed.
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--moderator')).toBeTruthy())
    const memberFetches = () =>
      apiGet.mock.calls.filter(c => String(c[0]).endsWith('/members')).length
    const before = memberFetches()
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'renamed' })
    emit('space.config.changed', { space_id: 'sp-other', event_type: 'role_changed' })
    expect(memberFetches()).toBe(before)
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'role_changed' })
    expect(memberFetches()).toBe(before + 1)
  })

  it('a failed load offers Retry instead of spinning forever', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (url.endsWith('/members')) {
        if (fail) throw new Error(`API 429: ${url}`)
        return [{ user_id: 'u-b', display_name: 'Bob', role: 'member', joined_at: '2026-01-01' }]
      }
      return []
    })
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toContain("Couldn't load the members.")
    fail = false
    fireEvent.click(r.getByText('Retry'))
    await waitFor(() => expect(r.getByText('Bob')).toBeTruthy())
  })

  it("offers no role change on the owner's seat a stub mirrors", async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    apiGet.mockImplementation(async (url: string) => url.endsWith('/members')
      ? [{
          user_id: 'u-h', display_name: 'Hannah', role: 'owner', is_owner: true,
          instance_id: 'host-x', joined_at: '2026-01-01',
        }]
      : [])
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--owner')).toBeTruthy())
    fireEvent.click(r.getByLabelText('Manage Hannah'))
    await waitFor(() => expect(r.baseElement.querySelector('.sh-member-actions')).toBeTruthy())
    expect(r.baseElement.querySelector('[data-role-option]')).toBeNull()
  })
})
