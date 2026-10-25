import { describe, it, expect, beforeEach, vi } from 'vitest'

const mockApi = { get: vi.fn() }
vi.mock('@/api', () => ({
  get api() {
    return mockApi
  },
}))
const user: { value: Record<string, unknown> | null } = { value: null }
vi.mock('@/store/auth', () => ({
  get currentUser() {
    return user
  },
}))

import {
  audienceBody,
  defaultAudienceKind,
  describeAudience,
  loadMyToday,
  todayFixedNote,
  todayKey,
} from './todayAudience'
import type { Highlight } from '@/types'

describe('todayAudience', () => {
  beforeEach(() => {
    user.value = { user_id: 'u-me' }
    mockApi.get.mockReset()
  })

  it('keys the day in UTC like the server', () => {
    expect(todayKey(new Date('2026-10-25T23:30:00-05:00'))).toBe('2026-10-26')
  })

  it('reads the Settings default, all paired when unset or unknown', () => {
    expect(defaultAudienceKind()).toBe('all_paired')
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"users"}}}' }
    expect(defaultAudienceKind()).toBe('users')
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"bogus"}}}' }
    expect(defaultAudienceKind()).toBe('all_paired')
    user.value = { preferences_json: 'not json' }
    expect(defaultAudienceKind()).toBe('all_paired')
  })

  it('describes an audience by name when every id resolves, else by count', () => {
    const names: Record<string, string> = { a: 'Anna', b: 'Bob' }
    expect(describeAudience('all_paired', [])).toBe('all your paired households')
    expect(describeAudience('users', [])).toBe('only you')
    expect(describeAudience('users', ['a', 'b'], id => names[id])).toBe('Anna, Bob')
    expect(describeAudience('users', ['a', 'x'], id => names[id])).toBe('2 people')
    expect(describeAudience('users', ['a'])).toBe('1 person')
    expect(describeAudience('households', ['h1'])).toBe('1 paired household')
    expect(describeAudience('households', ['h1', 'h2'])).toBe('2 paired households')
  })

  it("finds the author's highlight for today only", async () => {
    const today = todayKey()
    mockApi.get.mockResolvedValue([
      { highlight: { author_user_id: 'u-other', highlight_date: today }, frames: [] },
      { highlight: { author_user_id: 'u-me', highlight_date: '2020-01-01' }, frames: [] },
      { highlight: { id: 'h', author_user_id: 'u-me', highlight_date: today }, frames: [{}, {}] },
    ])
    const mine = await loadMyToday()
    expect(mine?.highlight.id).toBe('h')
    expect(mine?.frames).toBe(2)
    mockApi.get.mockResolvedValue([])
    expect(await loadMyToday()).toBeNull()
  })

  it('always names an audience, ids only where the kind uses them', () => {
    expect(audienceBody('all_paired', ['x'])).toEqual({ audience_kind: 'all_paired', audience: [] })
    expect(audienceBody('users', ['a'])).toEqual({ audience_kind: 'users', audience: ['a'] })
  })

  it('reads whether today has a public link, and says so', async () => {
    const today = todayKey()
    const h = { id: 'h', author_user_id: 'u-me', highlight_date: today, audience_kind: 'users', audience: ['a'] }
    mockApi.get.mockImplementation(async (path: string) =>
      path === '/api/highlights/h/publish' ? { published: true } : [{ highlight: h, frames: [{}] }])
    expect((await loadMyToday())?.published).toBe(true)
    mockApi.get.mockImplementation(async (path: string) => {
      if (path === '/api/highlights/h/publish') throw new Error('offline')
      return [{ highlight: h, frames: [{}] }]
    })
    expect((await loadMyToday())?.published).toBe(false)
    const hl = h as unknown as Highlight
    expect(todayFixedNote(hl, true)).toContain('has a public link')
    expect(todayFixedNote(hl, false)).toContain('wherever you shared it')
  })
})
