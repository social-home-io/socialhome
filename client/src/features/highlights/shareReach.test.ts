import { describe, it, expect } from 'vitest'
import type { Highlight } from '@/types'
import { widensAudience, type ShareTarget } from './shareReach'

const hl = (kind: Highlight['audience_kind'], audience: string[] = []): Highlight => ({
  id: 'h1', author_user_id: 'me', highlight_date: '2026-10-26',
  audience_kind: kind, audience_label: 'paired', audience,
  created_at: '2026-10-26T08:00:00Z', expires_at: '2026-11-25T08:00:00Z',
})
const paired = new Set(['inst-oak', 'inst-elm'])
const household = (...ids: string[]): ShareTarget => ({
  kind: 'household', members: ids.map(user_id => ({ user_id })),
})
const space = (
  members: { user_id: string, instance_id?: string | null }[] | null,
  spaceType: 'private' | 'household' | 'public' | 'global' = 'private',
): ShareTarget => ({ kind: 'space', spaceType, members })

describe('widensAudience — is the share target wider than the highlight? (S6/P6)', () => {
  it('household feed: only a picked-people highlight can get wider there', () => {
    expect(widensAudience(hl('all_paired'), 'me', household('me', 'lena'), paired)).toBe(false)
    expect(widensAudience(hl('households'), 'me', household('me', 'lena'), paired)).toBe(false)
    expect(widensAudience(hl('users', ['lena']), 'me', household('me', 'lena'), paired)).toBe(false)
    expect(widensAudience(hl('users', ['lena']), 'me', household('me', 'lena', 'finn'), paired)).toBe(true)
  })

  it('a public or global space is always wider: its followers are not on the member list', () => {
    expect(widensAudience(hl('all_paired'), 'me', space([], 'public'), paired)).toBe(true)
    expect(widensAudience(hl('all_paired'), 'me', space([], 'global'), paired)).toBe(true)
  })

  it('members we could not load count as wider — never share silently', () => {
    expect(widensAudience(hl('all_paired'), 'me', space(null), paired)).toBe(true)
  })

  it('all paired households: wider only with a member from an unpaired household', () => {
    const local = { user_id: 'lena' }
    const oak = { user_id: 'ru-1', instance_id: 'inst-oak' }
    const far = { user_id: 'ru-2', instance_id: 'inst-far' }
    expect(widensAudience(hl('all_paired'), 'me', space([local, oak]), paired)).toBe(false)
    expect(widensAudience(hl('all_paired'), 'me', space([local, oak, far]), paired)).toBe(true)
  })

  it('chosen households: wider with a member of a household not chosen', () => {
    const oak = { user_id: 'ru-1', instance_id: 'inst-oak' }
    const elm = { user_id: 'ru-2', instance_id: 'inst-elm' }
    expect(widensAudience(hl('households', ['inst-oak']), 'me', space([{ user_id: 'me' }, oak]), paired)).toBe(false)
    expect(widensAudience(hl('households', ['inst-oak']), 'me', space([oak, elm]), paired)).toBe(true)
    // "Only my household" (no households chosen): any remote member is more.
    expect(widensAudience(hl('households'), 'me', space([{ user_id: 'lena' }]), paired)).toBe(false)
    expect(widensAudience(hl('households'), 'me', space([oak]), paired)).toBe(true)
  })

  it('picked people: wider with anyone else in the space', () => {
    const lena = { user_id: 'lena' }
    expect(widensAudience(hl('users', ['lena']), 'me', space([{ user_id: 'me' }, lena]), paired)).toBe(false)
    expect(widensAudience(hl('users', ['lena']), 'me', space([lena, { user_id: 'finn' }]), paired)).toBe(true)
  })
})
