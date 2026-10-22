import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import {
  localClockTime, localDayKey, localSinceTime,
  relativeChatTime, relativeDocsTime, relativeFutureTime,
} from './relativeTime'

const FIXED_NOW = new Date('2026-05-08T13:00:00Z').getTime()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(FIXED_NOW)
})
afterEach(() => {
  vi.useRealTimers()
})

const iso = (offsetMs: number) => new Date(FIXED_NOW - offsetMs).toISOString()

describe('relativeChatTime', () => {
  it('renders "now" under a minute', () => {
    expect(relativeChatTime(iso(15_000))).toBe('now')
  })
  it('renders ``Nm`` minutes within the hour', () => {
    expect(relativeChatTime(iso(5 * 60_000))).toBe('5m')
  })
  it('renders ``Nh`` hours within the same calendar day', () => {
    expect(relativeChatTime(iso(3 * 3_600_000))).toBe('3h')
  })
  it('renders "Yesterday" for a stamp that crosses one calendar boundary', () => {
    // 30h ago lands on the previous calendar day (with FIXED_NOW @ 13:00 UTC).
    expect(relativeChatTime(iso(30 * 3_600_000))).toBe('Yesterday')
  })
  it('renders the weekday name for last 6 days', () => {
    expect(relativeChatTime(iso(3 * 86_400_000))).toMatch(/^[A-Za-z]{3}$/)
  })
  it('renders ``Mon D`` past the weekday window', () => {
    // 14 days ago — should land on a "MMM D" shape.
    const out = relativeChatTime(iso(14 * 86_400_000))
    expect(out).toMatch(/^[A-Za-z]{3,} \d+$/)
  })
  it('echoes the input on a parse failure', () => {
    expect(relativeChatTime('not-a-date')).toBe('not-a-date')
  })
  it('speaks the UI language', async () => {
    await setLocale('de')
    try {
      expect(relativeChatTime(iso(15_000))).toBe('jetzt')
      expect(relativeChatTime(iso(5 * 60_000))).toBe('5 Min.')
      expect(relativeChatTime(iso(30 * 3_600_000))).toBe('Gestern')
      expect(relativeChatTime(iso(14 * 86_400_000))).toMatch(/^\d+\. [A-Za-zä]{3,}\.?$/)
    } finally {
      await setLocale('en')
    }
  })
})

describe('relativeDocsTime', () => {
  it('renders "just now" under a minute', () => {
    expect(relativeDocsTime(iso(15_000))).toBe('just now')
  })
  it('renders "N min ago" within the hour', () => {
    expect(relativeDocsTime(iso(5 * 60_000))).toBe('5 min ago')
  })
  it('renders "Nh ago" within the same calendar day', () => {
    expect(relativeDocsTime(iso(3 * 3_600_000))).toBe('3h ago')
  })
  it('renders "yesterday" for a stamp that crosses one calendar boundary', () => {
    expect(relativeDocsTime(iso(30 * 3_600_000))).toBe('yesterday')
  })
  it('renders "N days ago" inside the week', () => {
    expect(relativeDocsTime(iso(3 * 86_400_000))).toBe('3 days ago')
  })
  it('echoes the input on a parse failure', () => {
    expect(relativeDocsTime('not-a-date')).toBe('not-a-date')
  })
  it('says "1 day ago" in the singular', () => {
    // 01:00 UTC "now" minus 25h is two calendar days back but one whole day.
    vi.setSystemTime(new Date('2026-05-08T00:30:00'))
    const stamp = new Date(new Date('2026-05-08T00:30:00').getTime() - 25 * 3_600_000).toISOString()
    expect(relativeDocsTime(stamp)).toBe('1 day ago')
  })
  it('speaks the UI language', async () => {
    await setLocale('de')
    try {
      expect(relativeDocsTime(iso(15_000))).toBe('gerade eben')
      expect(relativeDocsTime(iso(5 * 60_000))).toBe('vor 5 Min.')
      expect(relativeDocsTime(iso(3 * 3_600_000))).toBe('vor 3 Std.')
      expect(relativeDocsTime(iso(30 * 3_600_000))).toBe('gestern')
      expect(relativeDocsTime(iso(3 * 86_400_000))).toBe('vor 3 Tagen')
      // Past the week: a German short date ("15. März").
      expect(relativeDocsTime('2026-03-15T12:00:00Z')).toBe('15. März')
    } finally {
      await setLocale('en')
    }
  })
})

describe('SQLite naive-UTC normalisation', () => {
  // Naive shape ``YYYY-MM-DD HH:MM:SS`` from SQLite's ``datetime('now')``
  // — no ``T``, no ``Z``. Without normalisation Date.parse would treat
  // it as the viewer's local time, so a fresh "just now" stamp would
  // read as "{tz-offset}h ago" everywhere east of UTC. The Highlights
  // seen-by sheet was the surface that surfaced this for the user.
  it('treats a same-second naive timestamp as "just now"', () => {
    const nowSqlite = '2026-05-08 13:00:00'
    expect(relativeDocsTime(nowSqlite)).toBe('just now')
    expect(relativeChatTime(nowSqlite)).toBe('now')
  })

  it('does not mis-attribute a few-minute-old naive timestamp as hours old', () => {
    // 30 seconds ago in UTC.
    const naive = '2026-05-08 12:59:30'
    expect(relativeDocsTime(naive)).toBe('just now')
  })

  it('still respects the fractional-seconds shape', () => {
    expect(relativeDocsTime('2026-05-08 13:00:00.000')).toBe('just now')
  })

  it('leaves a real-UTC ISO string with offset alone', () => {
    expect(relativeDocsTime('2026-05-08T13:00:00+00:00')).toBe('just now')
  })

  it('echoes garbage that looks vaguely like a timestamp', () => {
    expect(relativeDocsTime('not-a-date')).toBe('not-a-date')
  })
})

describe('relativeFutureTime', () => {
  const ahead = (ms: number) => new Date(FIXED_NOW + ms).toISOString()

  it('renders minutes for something lapsing within the hour', () => {
    expect(relativeFutureTime(ahead(5 * 60_000))).toBe('in 5 minutes')
  })

  it('renders hours within the day', () => {
    expect(relativeFutureTime(ahead(3 * 3_600_000))).toBe('in 3 hours')
  })

  it('renders days up to a month', () => {
    expect(relativeFutureTime(ahead(7 * 86_400_000))).toBe('in 7 days')
    expect(relativeFutureTime(ahead(86_400_000))).toBe('in 1 day')
  })

  it('does not shave a day off a link that was just minted', () => {
    // A 7-day link created a second ago is 6.9999 days out. Flooring
    // made the invite dialog contradict its own picker ("Stops working
    // after 7 days" → "lapses in 6 days"); same for the 1-day option,
    // which came out as "in 23h".
    expect(relativeFutureTime(ahead(7 * 86_400_000 - 1_500))).toBe('in 7 days')
    expect(relativeFutureTime(ahead(86_400_000 - 1_500))).toBe('in 1 day')
  })

  it('falls back to an absolute date beyond a month', () => {
    expect(relativeFutureTime(ahead(90 * 86_400_000))).toMatch(/^on /)
  })

  it('says "expired" for a stamp already in the past', () => {
    // relativeDocsTime would say "just now" here, which reads as still
    // alive on a surface whose whole point is the remaining life.
    expect(relativeFutureTime(iso(60_000))).toBe('expired')
  })

  it('treats a naive SQLite timestamp as UTC, not local time', () => {
    expect(relativeFutureTime('2026-05-08 13:05:00')).toBe('in 5 minutes')
  })

  it('echoes garbage back', () => {
    expect(relativeFutureTime('not-a-date')).toBe('not-a-date')
  })

  it('speaks the UI language', async () => {
    await setLocale('de')
    try {
      expect(relativeFutureTime(ahead(7 * 86_400_000))).toBe('in 7 Tagen')
      expect(relativeFutureTime(ahead(3 * 3_600_000))).toBe('in 3 Stunden')
      expect(relativeFutureTime(iso(60_000))).toBe('abgelaufen')
      expect(relativeFutureTime(ahead(90 * 86_400_000))).toMatch(/^am /)
    } finally {
      await setLocale('en')
    }
  })
})

describe('localClockTime / localDayKey', () => {
  // Server stamps are UTC (tz-aware ISO or naive SQLite); the SPA shows
  // the viewer's wall clock and day, never a slice of the string.
  it.each(['America/Los_Angeles', 'Europe/Zurich', 'Asia/Tokyo'])('reads the stamp in %s', (tz) => {
    const prev = process.env.TZ
    process.env.TZ = tz
    try {
      const local = new Date('2026-10-19T22:30:00Z')
      const clock = local.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
      expect(localClockTime('2026-10-19T22:30:00+00:00')).toBe(clock)
      expect(localClockTime('2026-10-19 22:30:00')).toBe(clock)
      const pad = (n: number) => String(n).padStart(2, '0')
      const key = `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}`
      expect(localDayKey('2026-10-19 22:30:00')).toBe(key)
    } finally {
      process.env.TZ = prev
    }
  })

  it('is the next day in Zurich for a late-evening UTC stamp', () => {
    const prev = process.env.TZ
    process.env.TZ = 'Europe/Zurich'
    try {
      expect(localDayKey('2026-10-19T22:30:00Z')).toBe('2026-10-20')
      expect(localClockTime('2026-10-19T22:30:00Z')).toBe('12:30 AM')
    } finally {
      process.env.TZ = prev
    }
  })

  it('falls back on an unparseable stamp', () => {
    expect(localClockTime('garbage')).toBe('garbage')
    expect(localDayKey('garbage')).toBeNull()
  })
})

describe('localSinceTime', () => {
  // FIXED_NOW is 2026-05-08 13:00 UTC; the suite runs in UTC.
  it('is the clock today, yesterday + clock, weekday + clock, then the date', () => {
    expect(localSinceTime('2026-05-08T09:12:00Z')).toBe(localClockTime('2026-05-08T09:12:00Z'))
    expect(localSinceTime('2026-05-07T18:40:00Z')).toBe(`yesterday ${localClockTime('2026-05-07T18:40:00Z')}`)
    const mon = localSinceTime('2026-05-04T18:40:00Z')
    expect(mon.endsWith(localClockTime('2026-05-04T18:40:00Z'))).toBe(true)
    expect(mon.startsWith('Mon')).toBe(true)
    expect(localSinceTime('2026-04-12T09:00:00Z')).toBe('Apr 12')
    expect(localSinceTime('2025-04-12T09:00:00Z')).toBe('Apr 12, 2025')
  })

  it('reads a naive SQLite stamp as UTC and falls back on garbage', () => {
    expect(localSinceTime('2026-05-08 09:12:00')).toBe(localClockTime('2026-05-08T09:12:00Z'))
    expect(localSinceTime('garbage')).toBe('garbage')
  })
})
