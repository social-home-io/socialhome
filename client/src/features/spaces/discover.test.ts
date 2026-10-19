import { describe, expect, it } from 'vitest'
import type { DirectoryEntry } from '@/types'
import {
  buildHouseholdEntries, canFollow, contentIsGated, discoverAction, discoverable, entryAreaVerdict,
  hostLabel, matchesTerm, parseDiscoverSource, sourceOf, withMembership,
} from './discover'

const entry = (over: Partial<DirectoryEntry> = {}): DirectoryEntry => ({
  space_id: 's1', host_instance_id: 'iid-a', host_display_name: 'Keller household',
  host_is_paired: true, name: 'Bike repair', description: null, emoji: null,
  member_count: 3, scope: 'public', join_mode: 'open', min_age: 0, ...over,
})

describe('parseDiscoverSource', () => {
  it('knows the three chips and the retired browse tab name', () => {
    expect(parseDiscoverSource('household')).toBe('household')
    expect(parseDiscoverSource('paired')).toBe('paired')
    expect(parseDiscoverSource('friends')).toBe('paired')
    expect(parseDiscoverSource('global')).toBe('global')
    expect(parseDiscoverSource('nope')).toBeNull()
    expect(parseDiscoverSource(undefined)).toBeNull()
  })
})

describe('sourceOf', () => {
  it('maps an entry back to the chip it came from', () => {
    expect(sourceOf(entry({ host_instance_id: 'local' }))).toBe('household')
    expect(sourceOf(entry({ scope: 'household' }))).toBe('household')
    expect(sourceOf(entry({ scope: 'global' }))).toBe('global')
    expect(sourceOf(entry())).toBe('paired')
  })
})

describe('buildHouseholdEntries', () => {
  const row = {
    space_id: 'h1', name: 'Home', description: null, emoji: null, space_type: 'household' as const,
    join_mode: 'open' as const, category: 'general', allow_subscribers: false, min_age: 0,
    member_count: 4, role: null as string | null,
  }
  it('carries the count and the caller seat', () => {
    const [e] = buildHouseholdEntries([row], { subIds: new Set(), pendingIds: new Set(['h1']) })
    expect(e.member_count).toBe(4)
    expect(e.already_member).toBe(false)
    expect(e.request_pending).toBe(true)
    expect(e.host_instance_id).toBe('local')
  })
  it('a follower is subscribed, not a member', () => {
    const [e] = buildHouseholdEntries([{ ...row, role: 'subscriber' }], { subIds: new Set(), pendingIds: new Set() })
    expect(e.already_member).toBe(false)
    expect(e.already_subscribed).toBe(true)
  })
})

describe('withMembership', () => {
  it('fails closed when an older backend omits join_mode / followers', () => {
    const [g] = withMembership([entry({ join_mode: undefined as never })], 'global', {
      memberIds: new Set(), subIds: new Set(), pendingIds: new Set(),
    })
    expect(g.join_mode).toBe('invite_only')
    expect(g.allow_subscribers).toBe(false)
  })
})

describe('discoverable', () => {
  it('never lists invite-only, joined, followed or seated spaces', () => {
    const list = [
      entry({ space_id: 'ok' }),
      entry({ space_id: 'inv', join_mode: 'invite_only' }),
      entry({ space_id: 'mem', already_member: true }),
      entry({ space_id: 'sub', already_subscribed: true }),
      entry({ space_id: 'seat' }),
    ]
    expect(discoverable(list, new Set(['seat'])).map((e) => e.space_id)).toEqual(['ok'])
  })
})

describe('discoverAction', () => {
  it('open → Join, request → Ask, pending → Requested', () => {
    expect(discoverAction(entry())).toBe('join')
    expect(discoverAction(entry({ join_mode: 'request' }))).toBe('request')
    expect(discoverAction(entry({ request_pending: true }))).toBe('pending')
  })
  it('a global space from an unpaired host: Follow when it takes followers, else nothing', () => {
    const far = entry({ scope: 'global', host_is_paired: false })
    expect(discoverAction({ ...far, allow_subscribers: true })).toBe('follow')
    expect(discoverAction({ ...far, allow_subscribers: false })).toBeNull()
  })
  it('never suggests pairing: an unpaired host is Follow or nothing', () => {
    expect(discoverAction(entry({ host_is_paired: false }))).toBeNull()
    expect(discoverAction(entry({ scope: 'global', host_is_paired: false, join_mode: 'open', allow_subscribers: true }))).toBe('follow')
  })
})

describe('canFollow', () => {
  it('needs an explicit opt-in and a followable scope', () => {
    expect(canFollow(entry({ scope: 'global', allow_subscribers: true }))).toBe(true)
    expect(canFollow(entry({ scope: 'global' }))).toBe(false)
    expect(canFollow(entry({ allow_subscribers: true }))).toBe(false)
    expect(canFollow(entry({ host_instance_id: 'local', allow_subscribers: true }))).toBe(true)
  })
})

describe('matchesTerm / hostLabel / contentIsGated', () => {
  it('matches name or description, case-insensitively', () => {
    expect(matchesTerm('BIKE', 'Bike repair')).toBe(true)
    expect(matchesTerm('fix', 'Bikes', 'We fix things')).toBe(true)
    expect(matchesTerm('', 'x')).toBe(true)
    expect(matchesTerm('zzz', 'x', null)).toBe(false)
  })
  it('shortens a raw instance id', () => {
    expect(hostLabel(entry({ host_display_name: 'abcdefghijklmnop', host_instance_id: 'abcdefghijklmnop' }))).toBe('abcdefgh…')
    expect(hostLabel(entry())).toBe('Keller household')
  })
  it('only a public / global space that takes no followers is gated', () => {
    expect(contentIsGated(entry({ allow_subscribers: false }))).toBe(true)
    expect(contentIsGated(entry({ scope: 'household', allow_subscribers: false }))).toBe(false)
    expect(contentIsGated(entry())).toBe(false)
  })
})

describe('entryAreaVerdict (local communities)', () => {
  const area = { lat: 47.3769, lon: 8.5417, radius_km: 5 }
  const near = { lat: 47.392, lon: 8.56 }
  const bern = { lat: 46.9481, lon: 7.4474 }

  it('a space without an area has no verdict', () => {
    expect(entryAreaVerdict(entry(), near)).toBeNull()
    expect(entryAreaVerdict(entry({ local_area: null }), near)).toBeNull()
  })

  it('checks our home against the area', () => {
    expect(entryAreaVerdict(entry({ local_area: area }), near)).toBe('inside')
    expect(entryAreaVerdict(entry({ local_area: area }), bern)).toBe('outside')
    expect(entryAreaVerdict(entry({ local_area: area }), null)).toBe('no_home')
    expect(entryAreaVerdict(entry({ local_area: area }), undefined)).toBe('unknown')
  })

  it('a paired host we do not share our home with can not check it', () => {
    expect(entryAreaVerdict(entry({ local_area: area, home_shared_with_host: false }), near))
      .toBe('not_shared')
    // An older backend says nothing: the host decides.
    expect(entryAreaVerdict(entry({ local_area: area }), near)).toBe('inside')
  })

  it('a space this household hosts needs no sharing', () => {
    expect(entryAreaVerdict(
      entry({ host_instance_id: 'local', local_area: area, home_shared_with_host: false }), near,
    )).toBe('inside')
  })

  it('household rows carry the area', () => {
    const [e] = buildHouseholdEntries([{
      space_id: 'h1', name: 'Q', description: null, emoji: null, space_type: 'public',
      join_mode: 'open', category: 'general', allow_subscribers: false, min_age: 0,
      member_count: 1, role: null, local_area: area,
    }], { subIds: new Set(), pendingIds: new Set() })
    expect(e.local_area).toEqual(area)
  })
})
