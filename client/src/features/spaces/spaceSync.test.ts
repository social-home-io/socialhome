import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/preact'
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import {
  CATCHING_UP_CHIP_DELAY_MS, parseSpaceSync, syncBody, syncChipKind, syncChipLabel,
  syncDetailLines, syncHeading, syncHostName, syncView, useHeldFor,
  useMissingAnnouncement, type SpaceSync,
} from './spaceSync'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

function sync(over: Partial<SpaceSync> = {}): SpaceSync {
  return {
    state: 'up_to_date', last_ok_at: ago(5), waiting_to_send: 0,
    may_be_missing: false, unsent_posts: [], ...over,
  }
}

describe('syncView — one plain state per status', () => {
  it.each([
    [{ state: 'up_to_date' }, 'here'],
    [{ state: 'catching_up' }, 'latest'],
    // A failed round and no stream running: "Checking again soon".
    [{ state: 'behind' }, 'retry'],
    // No complete stream ever, not (yet) a warning: still getting ready.
    [{ state: 'behind', last_ok_at: null }, 'preparing'],
    [{ state: 'host_offline' }, 'away'],
    [{ state: 'never' }, 'preparing'],
    [{ state: 'behind', may_be_missing: true }, 'missing'],
  ] as const)('%o → %s', (over, view) => {
    expect(syncView(sync(over as Partial<SpaceSync>))).toBe(view)
  })

  it('a space hosted here always has everything', () => {
    expect(syncView(sync({ state: 'never', may_be_missing: true }), true)).toBe('here')
  })
})

describe('the header chip', () => {
  it('shows only the warning, the first join and a slow catch-up', () => {
    expect(syncChipKind(sync({ state: 'behind', may_be_missing: true }), false)).toBe('missing')
    expect(syncChipKind(sync({ state: 'never' }), false)).toBe('preparing')
    expect(syncChipKind(sync({ state: 'behind', last_ok_at: null }), false)).toBe('preparing')
    expect(syncChipKind(sync({ state: 'catching_up' }), false)).toBeNull()
    expect(syncChipKind(sync({ state: 'catching_up' }), true)).toBe('latest')
  })

  it('shows nothing for an away host, one failed round, up to date or a follower', () => {
    expect(syncChipKind(sync({ state: 'host_offline' }), true)).toBeNull()
    expect(syncChipKind(sync({ state: 'behind' }), true)).toBeNull()
    expect(syncChipKind(sync(), true)).toBeNull()
    expect(syncChipKind(null, true)).toBeNull()
  })

  it('words the chips plainly (the DM gap wording for the warning)', () => {
    expect(syncChipLabel('missing')).toBe('Some posts may be missing')
    expect(syncChipLabel('preparing')).toBe('Getting the space ready…')
    expect(syncChipLabel('latest')).toBe('Getting the latest…')
  })
})

describe('useHeldFor — the 10 s debounce of "Getting the latest…"', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('turns on only after the delay, and off at once', () => {
    const { result, rerender } = renderHook(({ on }) => useHeldFor(on, CATCHING_UP_CHIP_DELAY_MS),
      { initialProps: { on: true } })
    expect(result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(9_999) })
    expect(result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe(true)
    rerender({ on: false })
    expect(result.current).toBe(false)
  })

  it('a stream shorter than the delay never shows (no flicker)', () => {
    const { result, rerender } = renderHook(({ on }) => useHeldFor(on, CATCHING_UP_CHIP_DELAY_MS),
      { initialProps: { on: true } })
    act(() => { vi.advanceTimersByTime(4_000) })
    rerender({ on: false })
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(result.current).toBe(false)
    // A new stream starts the wait again.
    rerender({ on: true })
    act(() => { vi.advanceTimersByTime(6_000) })
    expect(result.current).toBe(false)
  })
})

describe('useMissingAnnouncement — only changes INTO the warning', () => {
  it('is silent on first render, speaks on entering, clears on leaving', () => {
    const { result, rerender } = renderHook(({ m }) => useMissingAnnouncement(m),
      { initialProps: { m: true } })
    expect(result.current).toBe('')
    rerender({ m: false })
    expect(result.current).toBe('')
    rerender({ m: true })
    expect(result.current).toBe('Some posts may be missing')
    rerender({ m: true })
    expect(result.current).toBe('Some posts may be missing')
    rerender({ m: false })
    expect(result.current).toBe('')
  })
})

describe('the settings wording', () => {
  it('headings, with the host household by name', () => {
    expect(syncHeading('here', 'The Müllers')).toBe("Everything's here")
    expect(syncHeading('latest', null)).toBe('Getting the latest…')
    expect(syncHeading('retry', null)).toBe('Checking again soon')
    expect(syncHeading('missing', null)).toBe('Some posts may be missing')
    expect(syncHeading('away', 'The Müllers')).toBe("Can't reach The Müllers right now")
    expect(syncHeading('away', null)).toBe("Can't reach the host right now")
    expect(syncHeading('preparing', null)).toBe('Getting the space ready')
  })

  it('bodies leave {time} for a <time> element', () => {
    const at = ago(5)
    expect(syncBody('here', sync({ last_ok_at: at }), 'The Müllers', false)).toEqual({
      text: 'Checked with The Müllers {time}.', time: { iso: at, style: 'relative' },
    })
    expect(syncBody('here', sync({ last_ok_at: null }), null, false).text)
      .toBe('Checked with the host.')
    expect(syncBody('missing', sync(), null, false).text)
      .toBe("We'll keep trying every 30 minutes. You can check now.")
    expect(syncBody('away', sync({ last_ok_at: at }), null, false)).toEqual({
      text: "You're seeing everything up to {time}. What you post goes out once the connection is back.",
      time: { iso: at, style: 'since' },
    })
    expect(syncBody('away', sync({ last_ok_at: null }), null, false).text)
      .toBe('What you post goes out once the connection is back.')
    expect(syncBody('retry', sync({ state: 'behind' }), 'Ann', false).text)
      .toBe("The last check didn't bring everything. It tries again every 30 minutes.")
    expect(syncBody('preparing', sync(), 'Ann', false).text)
      .toBe("The first posts from Ann haven't arrived yet.")
    expect(syncBody('latest', sync(), 'Ann', false).text)
      .toBe('New posts from Ann are on their way.')
    expect(syncBody('here', sync(), null, true).text)
      .toBe('This household hosts the space, so it always has everything.')
  })

  it('the host household is named from the space\'s member list — paired with it or not', () => {
    // A host met through an invite link is no connection of ours: its
    // name comes from the space, like every other member household.
    const members = [
      { instance_id: null, household_name: null },
      { instance_id: 'h1', household_name: 'The Müllers' },
      { instance_id: 'h2', household_name: 'Lindqvist' },
    ]
    expect(syncHostName(undefined, 'h1')).toBeNull()
    expect(syncHostName([], 'h1')).toBeNull()
    expect(syncHostName(members, 'h1')).toBe('The Müllers')
    expect(syncHostName(members, 'h2')).toBe('Lindqvist')
    expect(syncHostName(members, 'h3')).toBeNull()
    expect(syncHostName(members, null)).toBeNull()
  })
})

describe('the admin Details', () => {
  const page = { seq: 41, at: ago(30), match: null, missing: null, differing_buckets: null,
    signer_role: 'owner', conflict: false }

  it('a matching page, the signer, the outbox and the last sync', () => {
    const lines = syncDetailLines(sync({
      page: { ...page, match: true, missing: 0, differing_buckets: 0 },
      waiting_to_send: 2, last_ok_at: ago(5),
    }))
    expect(lines).toEqual([
      'Matches page 41 · 30 min ago',
      'Signed by the owner',
      '2 waiting to send',
      'Last successful sync 5 min ago',
    ])
  })

  it('parts that differ, rows proven missing, an admin signer and a conflict', () => {
    const lines = syncDetailLines(sync({
      page: { ...page, match: false, missing: 3, differing_buckets: 2, signer_role: 'admin', conflict: true },
      last_ok_at: null,
    }))
    expect(lines).toEqual([
      '2 parts differ from page 41',
      '3 items proven missing',
      'Signed by an admin',
      "The host signed two pages that don't agree",
    ])
    expect(syncDetailLines(sync({ page: { ...page, differing_buckets: 1 }, last_ok_at: null }))[0])
      .toBe('1 part differs from page 41')
    expect(syncDetailLines(sync({ page, last_ok_at: null }))[0]).toBe('Page 41 · not compared yet')
    expect(syncDetailLines(sync({
      page: { ...page, match: false, missing: 1, differing_buckets: 1 }, last_ok_at: null,
    }))).toContain('1 item proven missing')
    expect(syncDetailLines(sync({ waiting_to_send: 1, last_ok_at: null }))).toEqual(['1 waiting to send'])
  })
})

describe('parseSpaceSync', () => {
  it('narrows an untrusted body', () => {
    expect(parseSpaceSync(null)).toBeNull()
    expect(parseSpaceSync({ state: 'bogus' })).toBeNull()
    expect(parseSpaceSync({
      state: 'behind', last_ok_at: 5, waiting_to_send: -1, may_be_missing: 'yes',
      unsent_posts: ['p1', 7, 'p2'],
      page: { seq: 4, at: 'x', match: 'no', missing: -2, differing_buckets: 2.7,
        signer_role: 9, conflict: 'yes' },
    })).toEqual({
      state: 'behind', last_ok_at: null, waiting_to_send: 0, may_be_missing: false,
      unsent_posts: ['p1', 'p2'],
      page: { seq: 4, at: 'x', match: null, missing: null, differing_buckets: 2,
        signer_role: null, conflict: false },
    })
  })

  it('keeps a readable next_check_at, drops a bad one', () => {
    expect(parseSpaceSync({ state: 'up_to_date', next_check_at: '2026-11-22T10:00:00+00:00' })?.next_check_at)
      .toBe('2026-11-22T10:00:00+00:00')
    expect(parseSpaceSync({ state: 'up_to_date', next_check_at: 'soon' })).not.toHaveProperty('next_check_at')
  })

  it('an older server without the new fields reads as no warning, nothing unsent', () => {
    expect(parseSpaceSync({ state: 'up_to_date', last_ok_at: null, waiting_to_send: 0 }))
      .toEqual(sync({ last_ok_at: null }))
    expect(parseSpaceSync({ state: 'up_to_date', page: { seq: 'x' } })?.page).toBeUndefined()
  })
})
