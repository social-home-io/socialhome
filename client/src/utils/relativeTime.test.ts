import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
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
    expect(relativeFutureTime(ahead(5 * 60_000))).toBe('in 5 min')
  })

  it('renders hours within the day', () => {
    expect(relativeFutureTime(ahead(3 * 3_600_000))).toBe('in 3h')
  })

  it('renders days up to a month', () => {
    expect(relativeFutureTime(ahead(7 * 86_400_000))).toBe('in 7 days')
    expect(relativeFutureTime(ahead(86_400_000))).toBe('in 1 day')
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
    expect(relativeFutureTime('2026-05-08 13:05:00')).toBe('in 5 min')
  })

  it('echoes garbage back', () => {
    expect(relativeFutureTime('not-a-date')).toBe('not-a-date')
  })
})
