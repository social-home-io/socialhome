import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  groupByMonthDay, itemDay, readAlbumOrder, sortParam, takenCursor, writeAlbumOrder,
} from './albumOrder'

beforeEach(() => { localStorage.clear(); vi.restoreAllMocks() })

describe('album order', () => {
  it('is newest first until the person picks oldest first, per album and person', () => {
    expect(readAlbumOrder('u1', 'a')).toBe('newest')
    writeAlbumOrder('u1', 'a', 'oldest')
    expect(readAlbumOrder('u1', 'a')).toBe('oldest')
    expect(readAlbumOrder('u1', 'b')).toBe('newest')
    expect(readAlbumOrder('u2', 'a')).toBe('newest')
    writeAlbumOrder('u1', 'a', 'newest')
    expect(localStorage.length).toBe(0)
    expect(readAlbumOrder(undefined, 'a')).toBe('newest')
    writeAlbumOrder(undefined, 'a', 'oldest')
    expect(localStorage.length).toBe(0)
  })

  it('blocked storage means newest first, never an error', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(readAlbumOrder('u1', 'a')).toBe('newest')
    expect(() => writeAlbumOrder('u1', 'a', 'oldest')).not.toThrow()
  })

  it('maps to the server sort and its cursor', () => {
    expect(sortParam('newest')).toBe('taken')
    expect(sortParam('oldest')).toBe('taken_asc')
    expect(takenCursor({ id: 'i 1', day: '2024-07-12', created_at: '2026-10-01T10:00:00+00:00' }))
      .toBe('&after_day=2024-07-12&after=2026-10-01T10%3A00%3A00%2B00%3A00&after_id=i%201')
  })

  it('an item sorts by the day taken, else the upload day', () => {
    expect(itemDay({ id: 'a', day: '2020-01-02' })).toBe('2020-01-02')
    expect(itemDay({ id: 'a', taken_at: '2021-03-04' })).toBe('2021-03-04')
    expect(itemDay({ id: 'a', created_at: '2026-10-01T23:00:00+00:00' })).toBe('2026-10-01')
    expect(itemDay({ id: 'a' })).toBe('')
  })

  it('groups items in order into months and days', () => {
    const groups = groupByMonthDay([
      { id: '1', day: '2026-08-02' },
      { id: '2', day: '2026-07-13' },
      { id: '3', day: '2026-07-13' },
      { id: '4', day: '2026-07-01' },
      { id: '5', day: '2025-07-01' },
    ])
    expect(groups.map(m => m.month)).toEqual(['2026-08', '2026-07', '2025-07'])
    expect(groups[1].days.map(d => [d.day, d.items.map(i => i.id)])).toEqual([
      ['2026-07-13', ['2', '3']],
      ['2026-07-01', ['4']],
    ])
    expect(groupByMonthDay([])).toEqual([])
  })
})
