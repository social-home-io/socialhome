import { describe, it, expect } from 'vitest'
import type { Connection } from '@/store/connections'
import type { CompatPeer } from '@/store/federationCompat'
import type { GfsConnection } from '@/types'
import {
  TINTS, buildHouseholds, formatSince, gfsLiveState, hashString, heroVariant, hostOf, householdStatus,
  initialOf, isHousehold, isSpaceOnly, originOf, pendingRows, spaceOnlyRows, tintFor,
  versionsBehind,
} from './model'

function conn(over: Partial<Connection> = {}): Connection {
  return { instance_id: 'i', display_name: 'House', status: 'confirmed', reachable: true, ...over }
}

function peer(over: Partial<CompatPeer> = {}): CompatPeer {
  return {
    instance_id: 'i', display_name: 'House', proto_version: 55, status: 'confirmed',
    last_reachable_at: null, capabilities_known: true, lacking_features: [], ...over,
  }
}

describe('tint', () => {
  it('is stable for an instance id', () => {
    expect(tintFor('5z45utmlwve32cqrjyatki7xb44kjmkh')).toBe(tintFor('5z45utmlwve32cqrjyatki7xb44kjmkh'))
    expect(hashString('abc')).toBe(hashString('abc'))
  })

  it('spreads ids over all four tints, also on the low bits', () => {
    const seen = new Map<string, number>()
    for (let i = 0; i < 400; i++) {
      const t = tintFor(`household-${i.toString(36)}-id`)
      seen.set(t, (seen.get(t) ?? 0) + 1)
    }
    expect([...seen.keys()].sort()).toEqual([...TINTS].sort())
    for (const n of seen.values()) expect(n).toBeGreaterThan(60)
  })
})

describe('initialOf', () => {
  it('upper-cases the first letter and falls back to "?"', () => {
    expect(initialOf('alpha House')).toBe('A')
    expect(initialOf('  ')).toBe('?')
    expect(initialOf(undefined)).toBe('?')
    expect(initialOf('Ölberg')).toBe('Ö')
  })
})

describe('grouping', () => {
  const rows = [
    conn({ instance_id: 'a' }),
    conn({ instance_id: 'p', status: 'pending_sent' }),
    conn({ instance_id: 's', source: 'space_session' }),
  ]

  it('splits households, pending pairings and space-only peers', () => {
    expect(rows.filter(isHousehold).map((c) => c.instance_id)).toEqual(['a'])
    expect(pendingRows(rows).map((c) => c.instance_id)).toEqual(['p'])
    expect(spaceOnlyRows(rows).map((c) => c.instance_id)).toEqual(['s'])
    expect(isSpaceOnly(rows[2])).toBe(true)
  })
})

describe('versionsBehind', () => {
  it('counts protocol versions behind ours', () => {
    expect(versionsBehind(peer({ proto_version: 55 }), 57)).toBe(2)
    expect(versionsBehind(peer({ proto_version: 58 }), 57)).toBe(0)
  })

  it('is 0 when unknown (no row, caps not learned, our version not loaded)', () => {
    expect(versionsBehind(undefined, 57)).toBe(0)
    expect(versionsBehind(peer({ capabilities_known: false }), 57)).toBe(0)
    expect(versionsBehind(peer(), 0)).toBe(0)
  })
})

describe('householdStatus — one rule for list, map and Manage', () => {
  it('the live reachable flag wins over unreachable_since', () => {
    expect(householdStatus({ reachable: true, unreachable_since: '2026-01-01' }, 0)).toBe('ok')
    expect(householdStatus({ reachable: false, unreachable_since: null }, 0)).toBe('down')
  })

  it('without a flag, unreachable_since decides', () => {
    expect(householdStatus({ unreachable_since: '2026-01-01' }, 0)).toBe('down')
    expect(householdStatus({ unreachable_since: null }, 0)).toBe('ok')
  })

  it('down beats behind; behind > 0 is warn', () => {
    expect(householdStatus({ reachable: false }, 3)).toBe('down')
    expect(householdStatus({ reachable: true }, 3)).toBe('warn')
  })
})

describe('buildHouseholds', () => {
  const rows = [
    conn({ instance_id: 'ok-old', last_reachable_at: '2026-01-01 00:00:00' }),
    conn({ instance_id: 'ok-new', last_reachable_at: '2026-06-01 00:00:00' }),
    conn({ instance_id: 'behind' }),
    conn({ instance_id: 'down', reachable: false }),
    conn({ instance_id: 'space', source: 'space_session' }),
  ]
  const compat = new Map([['behind', peer({ instance_id: 'behind', proto_version: 50 })]])

  it('puts problems first (down, then behind), then most recent', () => {
    const hs = buildHouseholds(rows, compat, 57, true)
    expect(hs.map((h) => [h.conn.instance_id, h.status])).toEqual([
      ['down', 'down'], ['behind', 'warn'], ['ok-new', 'ok'], ['ok-old', 'ok'],
    ])
    expect(hs[1].behind).toBe(7)
  })

  it('never shows version data to a member', () => {
    const hs = buildHouseholds(rows, compat, 57, false)
    const behind = hs.find((h) => h.conn.instance_id === 'behind')!
    expect(behind.status).toBe('ok')
    expect(behind.behind).toBe(0)
  })
})

describe('heroVariant', () => {
  const ok = buildHouseholds([conn()], new Map(), 0, true)
  const down = buildHouseholds([conn({ reachable: false })], new Map(), 0, true)

  it('error wins, then loading, then empty / problem / healthy', () => {
    expect(heroVariant({ loading: true, error: true, households: ok })).toBe('error')
    expect(heroVariant({ loading: true, error: false, households: [] })).toBe('loading')
    expect(heroVariant({ loading: false, error: false, households: [] })).toBe('empty')
    expect(heroVariant({ loading: false, error: false, households: down })).toBe('problem')
    expect(heroVariant({ loading: false, error: false, households: ok })).toBe('healthy')
  })
})

describe('formatSince', () => {
  const now = Date.parse('2026-10-12T15:00:00Z')

  it('is a time today, a weekday this week, else a date', () => {
    expect(formatSince('2026-10-12T09:05:00Z', 'short', 'en-US', now)).toMatch(/\d{1,2}:05/)
    expect(formatSince('2026-10-10T09:00:00Z', 'short', 'en-US', now)).toBe('Sat')
    expect(formatSince('2026-10-10T09:00:00Z', 'long', 'en-US', now)).toBe('Saturday')
    expect(formatSince('2026-09-01T09:00:00Z', 'short', 'en-US', now)).toBe('Sep 1')
    expect(formatSince('2025-09-01T09:00:00Z', 'short', 'en-US', now)).toBe('Sep 1, 2025')
  })

  it('reads naive SQLite timestamps as UTC and rejects junk', () => {
    expect(formatSince('2026-10-10 09:00:00', 'short', 'en-US', now)).toBe('Sat')
    expect(formatSince(null, 'short', 'en-US', now)).toBeNull()
    expect(formatSince('not a date', 'short', 'en-US', now)).toBeNull()
  })
})

describe('gfsLiveState', () => {
  const g = (over: Partial<GfsConnection>): GfsConnection => ({
    id: 'g', gfs_instance_id: 'i', display_name: 'G', inbox_url: 'https://g', status: 'active',
    paired_at: '', published_space_count: 0, ...over,
  })

  it('maps pairing status + live socket to a mapped label, never raw text', () => {
    expect(gfsLiveState(g({ status: 'pending' }))).toEqual({ status: 'pending', labelKey: 'gfs.status_pending' })
    expect(gfsLiveState(g({ status: 'suspended' })).status).toBe('down')
    expect(gfsLiveState(g({ connected: true })).labelKey).toBe('gfs.status_connected')
    expect(gfsLiveState(g({ connected: false, last_error: 'auth-failed' })).labelKey).toBe('gfs.status_repair_needed')
    expect(gfsLiveState(g({ connected: false, last_error: 'ts-skew' })).labelKey).toBe('gfs.status_clock_skew')
    expect(gfsLiveState(g({ connected: false, last_error: '<b>' })).labelKey).toBe('gfs.status_reconnecting')
  })
})

describe('hostOf / originOf', () => {
  it('extracts host and origin, passing non-URLs through', () => {
    expect(hostOf('http://127.0.0.1:18765/inbox/x')).toBe('127.0.0.1:18765')
    expect(originOf('http://127.0.0.1:18765/inbox/x')).toBe('http://127.0.0.1:18765')
    expect(hostOf('not a url')).toBe('not a url')
    expect(originOf('nope')).toBe('nope')
    expect(hostOf(null)).toBe('')
  })
})
