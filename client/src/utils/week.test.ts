import { describe, it, expect, afterEach, vi } from 'vitest'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: null as unknown },
}))

import { currentUser } from '@/store/auth'
import {
  detectLocaleWeekStart,
  getWeekStartPref,
  isoWeekday,
  isoWeekNumber,
  resolveWeekStart,
  startOfWeek,
  weekdayOrder,
} from './week'

function setPrefs(prefs: Record<string, unknown> | null): void {
  ;(currentUser as { value: unknown }).value = prefs === null
    ? null
    : { user_id: 'u', preferences_json: JSON.stringify(prefs) }
}

/** Replace ``Intl.Locale`` with a constructible stub whose instances
 *  are whatever ``impl`` returns (returning an object from a
 *  constructor makes ``new`` yield it). */
function stubLocale(impl: (tag: string) => unknown): void {
  function FakeLocale(tag: string) { return impl(tag) }
  vi.stubGlobal('Intl', { ...Intl, Locale: FakeLocale })
}

/** Local-calendar ``YYYY-MM-DD`` for assertions. */
function ymdLocal(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  setPrefs(null)
})

describe('isoWeekday', () => {
  it('maps Monday to 0 and Sunday to 6', () => {
    expect(isoWeekday(new Date(2026, 8, 28))).toBe(0) // Mon 28 Sep 2026
    expect(isoWeekday(new Date(2026, 9, 3))).toBe(5)  // Sat 3 Oct 2026
    expect(isoWeekday(new Date(2026, 9, 4))).toBe(6)  // Sun 4 Oct 2026
  })
})

describe('startOfWeek', () => {
  it('Monday start: a Sunday belongs to the week that began the previous Monday', () => {
    const s = startOfWeek(new Date(2026, 9, 4, 15, 30), 0)
    expect(ymdLocal(s)).toBe('2026-09-28')
    expect([s.getHours(), s.getMinutes(), s.getSeconds(), s.getMilliseconds()])
      .toEqual([0, 0, 0, 0])
  })

  it('Sunday start: a Sunday is the first day of its own week', () => {
    const s = startOfWeek(new Date(2026, 9, 4, 15, 30), 6)
    expect(ymdLocal(s)).toBe('2026-10-04')
    expect(s.getHours()).toBe(0)
  })

  it('Sunday start: a Saturday belongs to the week that began the previous Sunday', () => {
    expect(ymdLocal(startOfWeek(new Date(2026, 9, 3, 23, 59), 6))).toBe('2026-09-27')
  })

  it('Monday start: a Monday is its own week start', () => {
    expect(ymdLocal(startOfWeek(new Date(2026, 8, 28, 0, 0), 0))).toBe('2026-09-28')
  })

  it('crosses month and year boundaries', () => {
    expect(ymdLocal(startOfWeek(new Date(2027, 0, 1), 0))).toBe('2026-12-28')
    expect(ymdLocal(startOfWeek(new Date(2027, 0, 1), 6))).toBe('2026-12-27')
  })

  // The suite runs in UTC (no DST), so pin real DST zones for these
  // cases; Node re-reads ``process.env.TZ`` at runtime.
  for (const [tz, day] of [
    ['America/New_York', new Date(2026, 9, 28, 12)], // US DST ends Sun 1 Nov
    ['Europe/Berlin', new Date(2026, 9, 21, 12)],    // EU DST ends Sun 25 Oct
  ] as const) {
    it(`stays on local midnight across a DST transition week (${tz})`, () => {
      const prevTz = process.env.TZ
      process.env.TZ = tz
      try {
        const d = new Date(day)
        const monday = startOfWeek(new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12), 0)
        const nextMonday = new Date(monday)
        nextMonday.setDate(monday.getDate() + 7)
        // Guard: the week really crosses a DST change in this zone.
        expect(monday.getTimezoneOffset()).not.toBe(nextMonday.getTimezoneOffset())
        for (const ws of [0, 6] as const) {
          const s = startOfWeek(nextMonday, ws)
          expect(s.getHours()).toBe(0)
          expect(s.getMinutes()).toBe(0)
        }
        expect(startOfWeek(nextMonday, 0).getDate()).toBe(nextMonday.getDate())
      } finally {
        process.env.TZ = prevTz
      }
    })
  }

  it('does not mutate its input', () => {
    const d = new Date(2026, 9, 4, 15, 30)
    const before = d.getTime()
    startOfWeek(d, 0)
    expect(d.getTime()).toBe(before)
  })
})

describe('weekdayOrder', () => {
  it('Monday-first is 0..6', () => {
    expect(weekdayOrder(0)).toEqual([0, 1, 2, 3, 4, 5, 6])
  })
  it('Sunday-first leads with 6', () => {
    expect(weekdayOrder(6)).toEqual([6, 0, 1, 2, 3, 4, 5])
  })
})

describe('detectLocaleWeekStart', () => {

  it('firstDay 7 via getWeekInfo() → Sunday', () => {
    stubLocale(() => ({ getWeekInfo: () => ({ firstDay: 7 }) }))
    expect(detectLocaleWeekStart('en-US')).toBe(6)
  })

  it('firstDay 1 via getWeekInfo() → Monday', () => {
    stubLocale(() => ({ getWeekInfo: () => ({ firstDay: 1 }) }))
    expect(detectLocaleWeekStart('de-CH')).toBe(0)
  })

  it('falls back to the weekInfo property', () => {
    stubLocale(() => ({ weekInfo: { firstDay: 7 } }))
    expect(detectLocaleWeekStart('en-US')).toBe(6)
  })

  it('other firstDay values (e.g. Saturday = 6) → Monday', () => {
    stubLocale(() => ({ getWeekInfo: () => ({ firstDay: 6 }) }))
    expect(detectLocaleWeekStart('ar-EG')).toBe(0)
  })

  it('missing weekInfo → Monday', () => {
    stubLocale(() => ({}))
    expect(detectLocaleWeekStart('xx')).toBe(0)
  })

  it('Intl.Locale throwing → Monday', () => {
    stubLocale(() => { throw new RangeError('Incorrect locale information provided') })
    expect(detectLocaleWeekStart('not a locale!')).toBe(0)
  })

  it('defaults to navigator.language', () => {
    const seen: string[] = []
    stubLocale((tag) => { seen.push(tag); return { weekInfo: { firstDay: 7 } } })
    vi.spyOn(navigator, 'language', 'get').mockReturnValue('en-US')
    expect(detectLocaleWeekStart()).toBe(6)
    expect(seen).toEqual(['en-US'])
  })
})

describe('getWeekStartPref / resolveWeekStart', () => {
  it('reads mon / sun from preferences', () => {
    setPrefs({ week_start: 'mon' })
    expect(getWeekStartPref()).toBe('mon')
    setPrefs({ week_start: 'sun' })
    expect(getWeekStartPref()).toBe('sun')
  })

  it('anything else is auto', () => {
    setPrefs(null)
    expect(getWeekStartPref()).toBe('auto')
    setPrefs({ week_start: 'tue' })
    expect(getWeekStartPref()).toBe('auto')
    setPrefs({ week_start: 1 })
    expect(getWeekStartPref()).toBe('auto')
  })

  it('explicit prefs resolve without consulting the locale', () => {
    stubLocale(() => { throw new Error('nope') })
    expect(resolveWeekStart('mon')).toBe(0)
    expect(resolveWeekStart('sun')).toBe(6)
  })

  it('auto / omitted resolves via the locale', () => {
    stubLocale(() => ({ getWeekInfo: () => ({ firstDay: 7 }) }))
    expect(resolveWeekStart('auto')).toBe(6)
    expect(resolveWeekStart()).toBe(6)
  })
})

describe('isoWeekNumber', () => {
  it('numbers ordinary weeks Monday to Sunday', () => {
    expect(isoWeekNumber(new Date(2026, 9, 5))).toBe(41)  // Mon 5 Oct 2026
    expect(isoWeekNumber(new Date(2026, 9, 11))).toBe(41) // Sun 11 Oct 2026
    expect(isoWeekNumber(new Date(2026, 9, 12))).toBe(42)
  })

  it('week 1 is the week with the year\'s first Thursday', () => {
    // 1 Jan 2026 is a Thursday → Mon 29 Dec 2025 opens week 1 of 2026.
    expect(isoWeekNumber(new Date(2025, 11, 29))).toBe(1)
    expect(isoWeekNumber(new Date(2026, 0, 1))).toBe(1)
    // 1 Jan 2027 is a Friday → it still belongs to week 53 of 2026.
    expect(isoWeekNumber(new Date(2027, 0, 1))).toBe(53)
    expect(isoWeekNumber(new Date(2027, 0, 4))).toBe(1)
  })

  it('has a week 53 only in long years', () => {
    expect(isoWeekNumber(new Date(2026, 11, 28))).toBe(53) // 2026 is long
    expect(isoWeekNumber(new Date(2025, 11, 28))).toBe(52) // Sun 28 Dec 2025
    expect(isoWeekNumber(new Date(2020, 11, 31))).toBe(53)
  })

  it('ignores the time of day and DST', () => {
    expect(isoWeekNumber(new Date(2026, 2, 29, 23, 30))).toBe(13) // DST Sunday (EU)
    expect(isoWeekNumber(new Date(2026, 2, 30, 0, 5))).toBe(14)
  })
})
