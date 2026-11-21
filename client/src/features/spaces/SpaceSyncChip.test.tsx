import { describe, it, expect, vi, beforeEach } from 'vitest'
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

function chip(sync: unknown) {
  return render(<SpaceSyncChip spaceId="s1" sync={sync} />)
}

const text = (c: Element) =>
  (c.querySelector('[data-testid="space-sync-chip"]')?.textContent ?? '').trim()

beforeEach(() => { handlers.clear() })

describe('SpaceSyncChip', () => {
  it('shows nothing when the space is up to date and nothing waits', () => {
    expect(chip({ state: 'up_to_date', last_ok_at: ago(5), waiting_to_send: 0 })
      .container.innerHTML).toBe('')
  })

  it('shows nothing for a follower (no sync status) or a bad body', () => {
    expect(chip(null).container.innerHTML).toBe('')
    expect(chip({ state: 'bogus' }).container.innerHTML).toBe('')
  })

  it('catching up', () => {
    const { container } = chip({ state: 'catching_up', last_ok_at: null, waiting_to_send: 0 })
    expect(text(container)).toBe('Catching up…')
  })

  it('behind, with when it was last up to date', () => {
    const { container } = chip({ state: 'behind', last_ok_at: ago(5), waiting_to_send: 0 })
    expect(text(container)).toBe('Behind — last up to date 5 min ago')
  })

  it('host offline since a clock time', () => {
    const { container } = chip({ state: 'host_offline', last_ok_at: ago(3), waiting_to_send: 0 })
    expect(text(container)).toMatch(/^Host offline since \d{1,2}:\d{2}/)
  })

  it('host offline, never up to date', () => {
    const { container } = chip({ state: 'host_offline', last_ok_at: null, waiting_to_send: 0 })
    expect(text(container)).toBe('Host offline')
  })

  it('not synced yet', () => {
    const { container } = chip({ state: 'never', last_ok_at: null, waiting_to_send: 0 })
    expect(text(container)).toBe('Not synced yet')
  })

  it('items waiting to send show even when up to date', () => {
    expect(text(chip({ state: 'up_to_date', last_ok_at: ago(1), waiting_to_send: 1 }).container))
      .toBe('1 waiting to send')
    expect(text(chip({ state: 'behind', last_ok_at: ago(5), waiting_to_send: 3 }).container))
      .toBe('Behind — last up to date 5 min ago · 3 waiting to send')
  })

  it('links to the Sync section of the space settings', () => {
    const { container } = chip({ state: 'never', last_ok_at: null, waiting_to_send: 0 })
    const a = container.querySelector('a') as HTMLAnchorElement
    expect(a.getAttribute('href')).toMatch(/\/spaces\/s1\/settings\/sync$/)
    expect(a.getAttribute('aria-label')).toContain('Not synced yet')
  })

  it('follows the space.sync_state frame of its own space only', () => {
    const { container } = chip({ state: 'behind', last_ok_at: ago(5), waiting_to_send: 0 })
    const frame = handlers.get('space.sync_state')!
    act(() => frame({ type: 'space.sync_state', data: {
      space_id: 'other', sync: { state: 'up_to_date', last_ok_at: ago(0), waiting_to_send: 0 },
    } }))
    expect(text(container)).toContain('Behind')
    act(() => frame({ type: 'space.sync_state', data: {
      space_id: 's1', sync: { state: 'up_to_date', last_ok_at: ago(0), waiting_to_send: 0 },
    } }))
    expect(container.innerHTML).toBe('')
  })
})
