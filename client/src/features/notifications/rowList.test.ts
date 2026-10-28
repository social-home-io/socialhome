import { describe, it, expect, vi, afterEach } from 'vitest'
import type { Notification } from '@/types'
import { ANNOUNCE_INTERVAL_MS, createAnnouncer, dayLabel, groupByDay } from './rowList'

function row(id: string, created_at: string): Notification {
  return { id, type: 'x', title: id, body: null, link_url: null, read_at: null, created_at }
}

afterEach(() => { vi.useRealTimers() })

describe('day headers', () => {
  const now = new Date(2026, 9, 28, 15, 0, 0) // local time

  it('Today, Yesterday, then the date', () => {
    expect(dayLabel(new Date(2026, 9, 28, 0, 5).toISOString(), now)).toBe('Today')
    expect(dayLabel(new Date(2026, 9, 27, 23, 55).toISOString(), now)).toBe('Yesterday')
    const older = dayLabel(new Date(2026, 9, 20, 12).toISOString(), now)
    expect(older).not.toBe('Today')
    expect(older).toMatch(/20/)
    expect(older).not.toMatch(/2026/)
  })

  it('a date in another year shows the year', () => {
    expect(dayLabel(new Date(2025, 11, 31, 12).toISOString(), now)).toMatch(/2025/)
  })

  it('groups consecutive rows by local day, keeping list order', () => {
    const groups = groupByDay([
      row('a', new Date(2026, 9, 28, 14).toISOString()),
      row('b', new Date(2026, 9, 28, 9).toISOString()),
      row('c', new Date(2026, 9, 27, 20).toISOString()),
      row('d', new Date(2026, 9, 20, 8).toISOString()),
    ], now)
    expect(groups.map(g => [g.label === 'Today' || g.label === 'Yesterday' ? g.label : 'date', g.rows.map(r => r.id)]))
      .toEqual([['Today', ['a', 'b']], ['Yesterday', ['c']], ['date', ['d']]])
  })
})

describe('live-region announcer', () => {
  it('speaks the first arrival at once, then at most once per 10 s with the count', () => {
    vi.useFakeTimers()
    const said: string[] = []
    const a = createAnnouncer((s) => said.push(s))
    a.arrived()
    expect(said).toEqual(['1 new notification'])
    a.arrived()
    a.arrived()
    vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS - 1)
    expect(said).toEqual(['1 new notification'])
    vi.advanceTimersByTime(1)
    expect(said).toEqual(['1 new notification', '2 new notifications'])
    // A quiet window says nothing, and the next arrival is spoken at once.
    vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS)
    expect(said.length).toBe(2)
    a.arrived()
    expect(said[2]).toBe('1 new notification')
    a.dispose()
  })

  it('dispose drops what is waiting', () => {
    vi.useFakeTimers()
    const said: string[] = []
    const a = createAnnouncer((s) => said.push(s))
    a.arrived()
    a.arrived()
    a.dispose()
    vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS * 2)
    expect(said).toEqual(['1 new notification'])
  })
})
