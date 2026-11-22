import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/preact'

const { handlers } = vi.hoisted(() => ({
  handlers: new Map<string, (e: { type: string; data: Record<string, unknown> }) => void>(),
}))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: (e: { type: string; data: Record<string, unknown> }) => void) => {
      handlers.set(type, fn)
      return () => { handlers.delete(type) }
    },
  },
}))

import { SpaceSyncChip } from './SpaceSyncChip'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const base = { last_ok_at: ago(5), waiting_to_send: 0, may_be_missing: false, unsent_posts: [] }

function chip(sync: unknown) {
  return render(<SpaceSyncChip spaceId="s1" sync={sync} />)
}

const theChip = (c: Element) => c.querySelector('[data-testid="space-sync-chip"]')
const text = (c: Element) => (theChip(c)?.querySelector('.sh-sync-chip__text')?.textContent ?? '').trim()
const live = (c: Element) => c.querySelector('[role="status"]')?.textContent ?? ''

function frame(sync: Record<string, unknown>, spaceId = 's1') {
  act(() => { handlers.get('space.sync_state')?.({ type: 'space.sync_state', data: { space_id: spaceId, sync } }) })
}

beforeEach(() => { handlers.clear() })
afterEach(() => { vi.useRealTimers() })

describe('SpaceSyncChip', () => {
  it('up to date: no chip, nothing said', () => {
    const { container } = chip({ ...base, state: 'up_to_date' })
    expect(theChip(container)).toBeNull()
    expect(live(container)).toBe('')
  })

  it('a follower (no sync status) or a bad body: nothing at all', () => {
    expect(chip(null).container.innerHTML).toBe('')
    expect(chip({ state: 'bogus' }).container.innerHTML).toBe('')
  })

  it('the warning: icon, words and an RTL-mirrored chevron, linking to settings', () => {
    const { container } = chip({ ...base, state: 'behind', may_be_missing: true })
    const a = theChip(container) as HTMLAnchorElement
    expect(text(container)).toBe('Some posts may be missing')
    expect(a.className).toContain('sh-sync-chip--missing')
    expect(a.querySelector('.sh-sync-chip__icon')?.getAttribute('aria-hidden')).toBe('true')
    expect(a.querySelector('.sh-sync-chip__chev')?.textContent).toBe('›')
    expect(a.getAttribute('href')).toContain('/spaces/s1/settings/sync')
    expect(a.getAttribute('aria-label')).toBe('Some posts may be missing — open details')
  })

  it('first join: "Getting the space ready…" without a chevron', () => {
    const { container } = chip({ ...base, state: 'never', last_ok_at: null })
    expect(text(container)).toBe('Getting the space ready…')
    expect(theChip(container)?.querySelector('.sh-sync-chip__chev')).toBeNull()
  })

  it.each([
    ['host_offline'],
    ['behind'],
  ])('%s without the warning: no chip (the away host is for settings only)', (state) => {
    expect(theChip(chip({ ...base, state }).container)).toBeNull()
  })

  it('a first unclean stream (no ok ever, no warning) still reads "Getting the space ready…"', () => {
    const { container } = chip({ ...base, state: 'behind', last_ok_at: null })
    expect(text(container)).toBe('Getting the space ready…')
  })

  it('no space-level "waiting to send" any more', () => {
    const { container } = chip({ ...base, state: 'up_to_date', waiting_to_send: 3 })
    expect(theChip(container)).toBeNull()
  })

  it('catching up shows a muted chip only after 10 s, and goes without a trace', () => {
    vi.useFakeTimers()
    const { container } = chip({ ...base, state: 'catching_up' })
    expect(theChip(container)).toBeNull()
    act(() => { vi.advanceTimersByTime(9_000) })
    expect(theChip(container)).toBeNull()
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(text(container)).toBe('Getting the latest…')
    expect(theChip(container)?.className).toContain('sh-sync-chip--latest')
    frame({ ...base, state: 'up_to_date' })
    expect(theChip(container)).toBeNull()
    expect(live(container)).toBe('')
  })

  it('a short catch-up never shows', () => {
    vi.useFakeTimers()
    const { container } = chip({ ...base, state: 'catching_up' })
    act(() => { vi.advanceTimersByTime(3_000) })
    frame({ ...base, state: 'up_to_date' })
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(theChip(container)).toBeNull()
  })

  it('announces only a change INTO the warning', () => {
    const { container } = chip({ ...base, state: 'up_to_date' })
    frame({ ...base, state: 'catching_up' })
    expect(live(container)).toBe('')
    frame({ ...base, state: 'behind', may_be_missing: true })
    expect(live(container)).toBe('Some posts may be missing')
    frame({ ...base, state: 'up_to_date' })
    expect(live(container)).toBe('')
  })

  it('a warning already there on load is shown, not announced', () => {
    const { container } = chip({ ...base, state: 'behind', may_be_missing: true })
    expect(text(container)).toBe('Some posts may be missing')
    expect(live(container)).toBe('')
  })

  it('a frame for another space changes nothing', () => {
    const { container } = chip({ ...base, state: 'up_to_date' })
    frame({ ...base, state: 'behind', may_be_missing: true }, 's2')
    expect(theChip(container)).toBeNull()
  })
})
