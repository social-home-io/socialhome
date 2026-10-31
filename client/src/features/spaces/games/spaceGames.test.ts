/**
 * The space-games rules: what the strip shows, what a card's button
 * does, who may close a game, the Games section's lists, the labels,
 * and which apps Settings › Games and the Start-a-game sheet offer.
 */
import { describe, it, expect } from 'vitest'
import type { CatalogEntry, InstalledApp } from '@/store/apps'
import {
  appHere, appLook, canClose, cardAction, closesLabel, daysLeft, endedLabel, gameChoices, gamePath,
  groupTables, isMine, joinableCount, knownTables, playersLabel, seatRange, seatedLabel, startableApps,
  starterLabel, stripTables, type SpaceTable,
} from './spaceGames'

const NOW = Date.parse('2026-10-31T12:00:00Z')

function table(over: Partial<SpaceTable> = {}): SpaceTable {
  return {
    session_id: 's1', app_id: 'quiz', host_name: 'Anna', host_is_local: false, note: null,
    min: 3, max: 8, seated: 2, state: 'open', late_join: false,
    created_at: '2026-10-31T10:00:00Z', expires_at: '2026-11-03T12:00:00Z',
    app: { installed: true, enabled: true }, group: null,
    ...over,
  }
}

function installed(over: Partial<InstalledApp> = {}): InstalledApp {
  return {
    app_id: 'quiz', name: 'Quiz', version: '1', enabled: true, capabilities: [], icon: null,
    min_age: 0, players: { min: 3, max: 8 }, tables: true, ...over,
  }
}

function entry(over: Partial<CatalogEntry> = {}): CatalogEntry {
  return {
    app_id: 'quiz', name: 'Quiz', latest_version: '1', description: null, icon_url: null,
    capabilities: [], ...over,
  }
}

describe('stripTables', () => {
  it('waiting games only, yours first, at most three; total counts every waiting one', () => {
    const list = [
      table({ session_id: 'a' }),
      table({ session_id: 'b', state: 'playing' }),
      table({ session_id: 'c', state: 'full' }),
      table({ session_id: 'd', group: { is_host: true, my_state: 'seated' } }),
      table({ session_id: 'e' }),
      table({ session_id: 'f', state: 'closed' }),
    ]
    const { shown, total } = stripTables(list)
    expect(shown.map((x) => x.session_id)).toEqual(['d', 'a', 'c'])
    expect(total).toBe(4)
  })

  it('nothing waiting → nothing shown', () => {
    expect(stripTables([table({ state: 'playing' })]).shown).toEqual([])
    expect(stripTables([]).total).toBe(0)
  })
})

describe('knownTables (§3.5)', () => {
  it('drops a game whose app this home neither has nor finds in the catalog — once the catalog answered', () => {
    const list = [
      table({ session_id: 'here' }),
      table({ session_id: 'cat', app_id: 'draw', app: { installed: false, enabled: false } }),
      table({ session_id: 'unknown', app_id: 'zzz', app: { installed: false, enabled: false } }),
    ]
    expect(knownTables(list, [entry({ app_id: 'draw' })], true).map((x) => x.session_id))
      .toEqual(['here', 'cat'])
    expect(knownTables(list, [], false)).toHaveLength(3)
  })
})

describe('isMine / joinableCount', () => {
  it('a place counts unless you left or were removed', () => {
    expect(isMine(table({ group: { is_host: false, my_state: 'joining' } }))).toBe(true)
    expect(isMine(table({ group: { is_host: false, my_state: 'left' } }))).toBe(false)
    expect(isMine(table({ group: { is_host: false, my_state: 'removed' } }))).toBe(false)
    expect(isMine(table())).toBe(false)
  })

  it('the dot counts open games you could join', () => {
    expect(joinableCount([
      table(), table({ state: 'full' }), table({ group: { is_host: true, my_state: 'seated' } }),
      table({ app: { installed: false, enabled: false } }),
    ])).toBe(1)
  })
})

describe('cardAction', () => {
  const member = { householdAdmin: false }
  it('Join a group game; Play takes the seat of a two-player one (D11)', () => {
    expect(cardAction(table(), member)).toBe('join')
    expect(cardAction(table({ min: 2, max: 2, seated: 1 }), member)).toBe('take')
  })

  it('your own game opens it; your game being played resumes it', () => {
    const mine = { is_host: false, my_state: 'seated' }
    expect(cardAction(table({ group: mine }), member)).toBe('open')
    expect(cardAction(table({ group: mine, state: 'playing' }), member)).toBe('resume')
  })

  it('Full when no seat is left', () => {
    expect(cardAction(table({ state: 'full' }), member)).toBe('full')
    expect(cardAction(table({ seated: 8 }), member)).toBe('full')
  })

  it('no app here (or off) → Ask an admin; a household admin sees the app', () => {
    expect(cardAction(table({ app: { installed: false, enabled: false } }), member)).toBe('ask')
    expect(cardAction(table({ app: { installed: true, enabled: false } }), member)).toBe('ask')
    expect(cardAction(table({ app: { installed: false, enabled: false } }), { householdAdmin: true }))
      .toBe('see')
  })

  it('a game being played takes people only with late join; finished takes nobody', () => {
    expect(cardAction(table({ state: 'playing' }), member)).toBe('none')
    expect(cardAction(table({ state: 'playing', late_join: true }), member)).toBe('join')
    expect(cardAction(table({ state: 'closed' }), member)).toBe('none')
  })
})

describe('canClose', () => {
  it('the person who started it, and the owner / an admin / a moderator for any game', () => {
    expect(canClose(table({ group: { is_host: true, my_state: 'seated' } }), false)).toBe(true)
    expect(canClose(table(), true)).toBe(true)
    expect(canClose(table(), false)).toBe(false)
    expect(canClose(table({ state: 'closed' }), true)).toBe(false)
  })
})

describe('groupTables', () => {
  it('waiting (yours first), being played, finished', () => {
    const g = groupTables([
      table({ session_id: 'a' }),
      table({ session_id: 'b', state: 'playing' }),
      table({ session_id: 'c', group: { is_host: true, my_state: 'seated' } }),
      table({ session_id: 'd', state: 'closed' }),
    ])
    expect(g.waiting.map((x) => x.session_id)).toEqual(['c', 'a'])
    expect(g.playing.map((x) => x.session_id)).toEqual(['b'])
    expect(g.finished.map((x) => x.session_id)).toEqual(['d'])
  })
})

describe('labels', () => {
  it('players, joined, seats', () => {
    expect(playersLabel(3, 30)).toBe('3–30 players')
    expect(playersLabel(2, 2)).toBe('2 players')
    expect(seatedLabel(table({ seated: 1 }))).toBe('1 joined')
    expect(seatedLabel(table({ seated: 9 }))).toBe('9 joined')
    expect(seatedLabel(table({ max: 2, seated: 1 }))).toBe('1 of 2')
  })

  it('who started it — never "host"', () => {
    expect(starterLabel(table())).toBe('Anna started this game')
    expect(starterLabel(table({ group: { is_host: true, my_state: 'seated' } }))).toBe('You started this game')
    expect(starterLabel(table({ host_name: null }))).toBe('Someone in the space started this game')
  })

  it('how long it stays open', () => {
    expect(daysLeft('2026-11-03T12:00:00Z', NOW)).toBe(3)
    expect(daysLeft('2026-10-30T12:00:00Z', NOW)).toBe(0)
    expect(closesLabel('2026-11-03T12:00:00Z', NOW)).toBe('closes in 3 days')
    expect(closesLabel('2026-11-01T14:00:00Z', NOW)).toBe('closes in 1 day')
    expect(closesLabel('2026-10-31T20:00:00Z', NOW)).toBe('closes in less than a day')
    expect(closesLabel('nope', NOW)).toBe('closes in less than a day')
  })

  it('why a finished game ended', () => {
    expect(endedLabel(table())).toBeNull()
    expect(endedLabel(table({ state: 'closed', end_reason: 'closed_by_moderator' })))
      .toBe('A moderator closed this game.')
    expect(endedLabel(table({ state: 'closed', end_reason: 'app_removed' })))
      .toBe('This game is no longer allowed here.')
    expect(endedLabel(table({ state: 'closed', end_reason: 'finished' }))).toBe('Played to the end.')
    expect(endedLabel(table({ state: 'closed', end_reason: 'cancelled' }))).toBe('This game was closed.')
    expect(endedLabel(table({ state: 'closed', end_reason: 'host_gone' }))).toBe('This game is over.')
  })

  it('the game screen lives with the app', () => {
    expect(gamePath({ app_id: 'gift draw', session_id: 's/1' })).toBe('/apps/gift%20draw/tables/s%2F1')
  })
})

describe('apps', () => {
  it('appHere', () => {
    expect(appHere({ app: { installed: true, enabled: true } })).toBe('yes')
    expect(appHere({ app: { installed: true, enabled: false } })).toBe('off')
    expect(appHere({})).toBe('missing')
  })

  it('appLook: the catalog name in your language, else the installed name, else the id', () => {
    const cat = [entry({ i18n: { de: { name: 'Quizrunde', description: null } } })]
    expect(appLook('quiz', [], cat, 'de').name).toBe('Quizrunde')
    expect(appLook('quiz', [installed()], [], 'de').name).toBe('Quiz')
    expect(appLook('other', [], [], 'en').name).toBe('other')
  })

  it('gameChoices: added group games, the catalog’s, and anything on the list — by name', () => {
    const list = gameChoices(
      ['zzz-gone'],
      [installed(), installed({ app_id: 'chess', name: 'Chess', tables: false, players: { min: 2, max: 2 } })],
      [entry({ app_id: 'draw', name: 'Gift draw', tables: true, players: { min: 3, max: 30 } }),
        entry({ app_id: 'go', name: 'Go', tables: false })],
      'en',
    )
    expect(list.map((c) => c.app_id)).toEqual(['draw', 'quiz', 'zzz-gone'])
    expect(list.find((c) => c.app_id === 'draw')).toMatchObject({ here: 'missing', players: { min: 3, max: 30 } })
    expect(list.find((c) => c.app_id === 'quiz')).toMatchObject({ here: 'yes', players: { min: 3, max: 8 } })
    expect(list.find((c) => c.app_id === 'zzz-gone')).toMatchObject({ here: 'missing', players: null })
  })

  it('startableApps: on the list, here, on, and a group game', () => {
    const apps = [
      installed(),
      installed({ app_id: 'off', enabled: false }),
      installed({ app_id: 'oneone', tables: false }),
      installed({ app_id: 'other' }),
    ]
    expect(startableApps(['quiz', 'off', 'oneone'], apps).map((a) => a.app_id)).toEqual(['quiz'])
  })

  it('seatRange: the app’s players, at most 30', () => {
    expect(seatRange(installed({ players: { min: 3, max: 50 } }))).toEqual({ min: 3, max: 30 })
    expect(seatRange(installed({ players: undefined }))).toEqual({ min: 2, max: 2 })
  })
})
