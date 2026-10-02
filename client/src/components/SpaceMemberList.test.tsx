import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockImplementation(async (url: string) => {
      if (url.endsWith('/members')) {
        return [
          { user_id: 'u-o', display_name: 'Olivia', role: 'owner', joined_at: '2026-01-01' },
          { user_id: 'u-a', display_name: 'Adam', role: 'admin', joined_at: '2026-01-01' },
          { user_id: 'u-m', display_name: 'Mo', role: 'moderator', joined_at: '2026-01-01' },
          { user_id: 'u-b', display_name: 'Bob', role: 'member', joined_at: '2026-01-01' },
        ]
      }
      return []
    }),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

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
})
