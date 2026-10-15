import { describe, it, expect } from 'vitest'
import {
  buildModel, emojiOnly, fold, letterOf, matches, pairingSpan, peopleByLetter, statusLine,
  type FriendsPayload, type Household,
} from './friendsModel'

function hh(id: string, name: string, people: string[], extra: Partial<Household> = {}): Household {
  return {
    instance_id: id, display_name: name, home_lat: null, home_lon: null, paired_at: null,
    reachable: true, unreachable_since: null, member_count: people.length,
    members: people.map((p, i) => ({
      user_id: `${id}-${i}`, instance_id: id, remote_username: p.toLowerCase(),
      display_name: p, picture_url: null,
    })),
    ...extra,
  }
}

function data(households: Household[], local: string[] = ['Pascal']): FriendsPayload {
  return {
    instance: {
      instance_id: 'us', display_name: 'Home', home_lat: null, home_lon: null,
      member_count: local.length,
      members: local.map((n, i) => ({
        user_id: i === 0 ? 'me' : `l${i}`, username: n.toLowerCase(), display_name: n, picture_url: null,
      })),
    },
    households,
    totals: { households: 0, people: 0 },
  }
}

describe('friendsModel', () => {
  it('fold drops case and accents', () => {
    expect(fold('Müller-Lüdenscheidt')).toBe('muller-ludenscheidt')
    expect(fold('ÉLODIE')).toBe('elodie')
    expect(fold('田中')).toBe('田中')
  })

  it('letterOf: Latin letters fold to A–Z, everything else is #', () => {
    expect(letterOf('Ömer')).toBe('O')
    expect(letterOf('élodie')).toBe('E')
    expect(letterOf('田中 健')).toBe('#')
    expect(letterOf('ليلى')).toBe('#')
    expect(letterOf('42 Kids')).toBe('#')
  })

  it('buildModel: problems first, then A–Z; quiet households apart; counts', () => {
    const m = buildModel(data([
      hh('z', 'zulu', ['Zed']),
      hh('d', 'Delta', ['Dan'], { reachable: false }),
      hh('a', 'Älpha', ['Ann', 'Al']),
      hh('q', 'Quiet', []),
    ], ['Pascal', 'Ada']), { selfId: 'me', locale: 'en' })
    expect(m.groups.map((g) => g.household.display_name)).toEqual(['Delta', 'Älpha', 'zulu'])
    expect(m.quiet.map((h) => h.display_name)).toEqual(['Quiet'])
    expect(m.atHome.map((p) => p.shown)).toEqual(['Pascal', 'Ada'])
    expect(m.atHome[0].isSelf).toBe(true)
    expect(m.groups[1].people.map((p) => p.shown)).toEqual(['Al', 'Ann'])
    expect(m.peopleCount).toBe(6)
    expect(m.householdCount).toBe(5)
  })

  it('buildModel: hidden people drop out; a household left empty turns quiet', () => {
    const m = buildModel(data([hh('a', 'A', ['Ann'])]), {
      selfId: 'me', locale: 'en', hidden: (id) => id === 'a-0',
    })
    expect(m.groups).toHaveLength(0)
    expect(m.quiet.map((h) => h.instance_id)).toEqual(['a'])
    expect(m.peopleCount).toBe(1)
  })

  it('matches covers nickname, real name, handle and household', () => {
    const d = data([hh('o', 'Okafor family', ['Chidi'])])
    d.households[0].members[0].personal_alias = 'Coach'
    const m = buildModel(d, { selfId: 'me', locale: 'en' })
    const chidi = m.groups[0].people[0]
    expect(chidi.shown).toBe('Coach')
    expect(chidi.aliased).toBe(true)
    for (const q of ['coach', 'chidi', 'okafor', '']) expect(matches(chidi, fold(q))).toBe(true)
    expect(matches(chidi, fold('zzz'))).toBe(false)
  })

  it('peopleByLetter puts # last and sorts within a letter', () => {
    const m = buildModel(data([hh('t', 'T', ['田中', 'Ömer', 'Otto', 'Bea'])]), { selfId: 'me', locale: 'de' })
    const groups = peopleByLetter([...m.atHome, ...m.groups[0].people], 'de')
    expect(groups.map((g) => g.letter)).toEqual(['B', 'O', 'P', '#'])
    expect(groups[1].people.map((p) => p.shown)).toEqual(['Ömer', 'Otto'])
  })

  it('status helpers split emoji-only from a status line', () => {
    expect(emojiOnly({ emoji: '🍝', text: null, expires_at: null })).toBe('🍝')
    expect(emojiOnly({ emoji: '🍝', text: 'Dinner', expires_at: null })).toBeNull()
    expect(statusLine({ emoji: '🍝', text: 'Dinner', expires_at: null })).toBe('🍝 Dinner')
    expect(statusLine({ emoji: null, text: 'Dinner', expires_at: null })).toBe('Dinner')
    expect(statusLine({ emoji: '🍝', text: '  ', expires_at: null })).toBeNull()
    expect(statusLine(null)).toBeNull()
  })

  it('pairingSpan: earliest + newest, naive SQLite stamps read as UTC', () => {
    const a = hh('a', 'A', [], { paired_at: '2025-08-01 10:00:00' })
    const b = hh('b', 'B', [], { paired_at: '2026-01-01T00:00:00Z' })
    const c = hh('c', 'C', [], { paired_at: null })
    const span = pairingSpan([a, b, c])
    expect(span.since).toBe(Date.parse('2025-08-01T10:00:00Z'))
    expect(span.newest?.instance_id).toBe('b')
    expect(pairingSpan([c])).toEqual({ since: null, newest: null })
  })
})
