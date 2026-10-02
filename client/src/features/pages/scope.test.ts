import { describe, it, expect, vi } from 'vitest'

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'tok' },
  isAuthed: { value: true },
}))

import { householdPageScope, spacePageScope } from './scope'

const base = { spaceId: 's1', role: 'member' as const, level: 'open' as const, writable: true, archived: false }

describe('page scopes', () => {
  it('household: /api/pages with locks; restore for admins only', () => {
    const s = householdPageScope()
    expect(s.base).toBe('/api/pages')
    expect(s.locks).toBe(true)
    expect(s.canRevert).toBe(false)
    expect(s.revertNote()).toBe('Only household admins can restore an older version.')
    expect(s.canWrite).toBe(true)
    expect(s.reviewed({ created_by: 'u2' })).toBe(false)
  })

  it('space: its own routes, never locks or revert', () => {
    const s = spacePageScope(base)
    expect(s.base).toBe('/api/spaces/s1/pages')
    expect(s.spaceId).toBe('s1')
    expect(s.locks).toBe(false)
    expect(s.canRevert).toBe(false)
    expect(s.key).toBe('space:s1')
  })

  it('space writes need a writable seat in a live space', () => {
    expect(spacePageScope(base).canWrite).toBe(true)
    expect(spacePageScope({ ...base, writable: false }).canWrite).toBe(false)
    expect(spacePageScope({ ...base, archived: true }).canWrite).toBe(false)
  })

  it('Reviewed: a member\'s create and edits of others\' pages; never a moderator\'s', () => {
    const member = spacePageScope({ ...base, level: 'moderated' })
    expect(member.reviewedCreate).toBe(true)
    expect(member.reviewed({ created_by: 'u2' })).toBe(true)
    expect(member.reviewed({ created_by: 'u1' })).toBe(false)
    for (const role of ['moderator', 'admin', 'owner'] as const) {
      const s = spacePageScope({ ...base, role, level: 'moderated' })
      expect(s.reviewedCreate).toBe(false)
      expect(s.reviewed({ created_by: 'u2' })).toBe(false)
    }
    expect(spacePageScope(base).reviewed({ created_by: 'u2' })).toBe(false)
  })
})
