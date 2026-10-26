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
  defaultAudience,
  defaultAudienceKind,
  describeAudience,
  loadMyToday,
  lockShareNote,
  todayKey,
  choiceFromAudience,
  audienceOfChoice,
  choiceIsComplete,
  defaultChoice,
  audienceChange,
  type AudienceDirectory,
} from './todayAudience'

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

  it('reads the whole Settings default — kind and the picked ids', () => {
    expect(defaultAudience()).toEqual({ kind: 'all_paired', ids: [] })
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"households","ids":["k","h",3,""]}}}' }
    expect(defaultAudience()).toEqual({ kind: 'households', ids: ['k', 'h'] })
    // all paired never carries ids.
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"all_paired","ids":["k"]}}}' }
    expect(defaultAudience()).toEqual({ kind: 'all_paired', ids: [] })
  })

  it('describes an audience by name when every id resolves, else by count', () => {
    const names: Record<string, string> = { a: 'Anna', b: 'Bob' }
    expect(describeAudience('all_paired', [])).toBe('all your paired households')
    expect(describeAudience('users', [])).toBe('only you')
    expect(describeAudience('households', [])).toBe('your household only')
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
    expect(lockShareNote(true)).toContain('public link')
    expect(lockShareNote(false)).toContain('wherever you shared it')
  })
})

describe('audience choice', () => {
  beforeEach(() => { user.value = { user_id: 'u-me' } })

  it('maps an audience to the three options and back', () => {
    expect(choiceFromAudience('all_paired', [])).toMatchObject({ mode: 'all' })
    expect(choiceFromAudience('households', [])).toMatchObject({ mode: 'household' })
    expect(choiceFromAudience('households', ['h1'])).toMatchObject({
      mode: 'choose', chooseKind: 'households', households: ['h1'],
    })
    expect(choiceFromAudience('users', ['u1'])).toMatchObject({
      mode: 'choose', chooseKind: 'users', users: ['u1'],
    })
    for (const [kind, ids] of [
      ['all_paired', []], ['households', []], ['households', ['h1']], ['users', ['u1', 'u2']],
    ] as const) {
      expect(audienceOfChoice(choiceFromAudience(kind, [...ids]))).toEqual({ kind, ids: [...ids] })
    }
  })

  it('choosing needs at least one pick (B21)', () => {
    expect(choiceIsComplete(choiceFromAudience('all_paired', []))).toBe(true)
    expect(choiceIsComplete(choiceFromAudience('households', []))).toBe(true)
    const none = { ...choiceFromAudience('households', ['h1']), households: [] }
    expect(choiceIsComplete(none)).toBe(false)
    expect(choiceIsComplete({ ...none, chooseKind: 'users', users: ['u1'] })).toBe(true)
  })

  it('a new day starts from Settings; "chosen, nobody picked" asks', () => {
    expect(defaultChoice()).toMatchObject({ mode: 'all' })
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"users","ids":["u1"]}}}' }
    expect(defaultChoice()).toMatchObject({ mode: 'choose', chooseKind: 'users', users: ['u1'] })
    user.value = { preferences_json: '{"highlights":{"default_audience":{"kind":"households"}}}' }
    const ask = defaultChoice()
    expect(ask).toMatchObject({ mode: 'choose', chooseKind: 'households', households: [] })
    expect(choiceIsComplete(ask)).toBe(false)
  })
})

describe('audienceChange — what changes for everyone', () => {
  const dir: AudienceDirectory = {
    ownName: 'Berger',
    housemates: [{ user_id: 'u-anna', display_name: 'Anna' }],
    households: [
      {
        instance_id: 'h-smith', display_name: 'The Smiths',
        members: [{ user_id: 'u-bob', display_name: 'Bob' }, { user_id: 'u-bea', display_name: 'Bea' }],
      },
      { instance_id: 'h-dora', display_name: 'Dora’s', members: [{ user_id: 'u-dora', display_name: 'Dora' }] },
      { instance_id: 'h-empty', display_name: 'Quiet house', members: [] },
    ],
  }

  it('narrowing to your household: every paired household loses it', () => {
    const out = audienceChange('all_paired', [], 'households', [], dir)
    expect(out.gains).toEqual([])
    expect(out.losses).toEqual(['The Smiths', 'Dora’s', 'Quiet house'])
  })

  it('widening from one household: the others gain it', () => {
    const out = audienceChange('households', ['h-smith'], 'all_paired', [], dir)
    expect(out.gains).toEqual(['Dora’s', 'Quiet house'])
    expect(out.losses).toEqual([])
  })

  it('people: names whole households when everyone there changes, else people', () => {
    const out = audienceChange('users', ['u-bob', 'u-bea', 'u-anna'], 'users', ['u-bea', 'u-dora'], dir)
    expect(out.gains).toEqual(['Dora’s'])
    expect(out.losses).toEqual(['Anna', 'Bob'])
    const whole = audienceChange('users', ['u-anna'], 'users', ['u-anna', 'u-bob', 'u-bea'], dir)
    expect(whole.gains).toEqual(['The Smiths'])
  })

  it('the same people again changes nothing', () => {
    expect(audienceChange('users', ['u-bob'], 'users', ['u-bob'], dir)).toEqual({ gains: [], losses: [] })
  })
})
