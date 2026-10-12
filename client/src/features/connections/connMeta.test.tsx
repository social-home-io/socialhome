import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/preact'
import type { Connection } from '@/store/connections'
import { householdAriaLabel, householdMeta, transportItem } from './connMeta'
import type { Household } from './model'

const NOW = new Date('2026-10-12T15:00:00Z')

function hh(over: Partial<Connection> = {}, status: Household['status'] = 'ok', behind = 0): Household {
  return {
    conn: { instance_id: 'e', display_name: 'Epsilon House', reachable: status !== 'down', status: 'confirmed', ...over },
    status,
    behind,
  }
}

describe('ConnMeta', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW) })
  afterEach(() => { vi.useRealTimers() })

  it('reachable: transport + "Last connected …"', () => {
    const { container } = render(<div>{householdMeta(hh({ transport: 'rtc', last_reachable_at: '2026-10-12 14:58:00' }))}</div>)
    expect(container.textContent).toBe('DirectLast connected 2 min ago')
  })

  it('unreachable: weekday + queued messages, with a short phone variant', () => {
    const { container } = render(<div>{householdMeta(hh({
      unreachable_since: '2026-10-05T09:00:00Z', queued_envelopes: 12,
    }, 'down'), NOW.getTime())}</div>)
    const down = container.querySelector('.sh-conn-down')!
    expect(down.textContent).toContain('Unreachable since Oct 5')
    expect(down.querySelector('.sh-conn-long')!.textContent).toBe('12 messages waiting')
    expect(down.querySelector('.sh-conn-short')!.textContent).toBe('12 waiting')
  })

  it('unreachable without a timestamp or queue says just "Unreachable"', () => {
    const { container } = render(<div>{householdMeta(hh({}, 'down'))}</div>)
    expect(container.textContent).toBe('Unreachable')
  })

  it('no transport → no transport item', () => {
    expect(transportItem(null)).toBeNull()
    const { container } = render(<div>{transportItem('gfs_relay')}</div>)
    expect(container.textContent).toBe('Through the GFS')
  })
})

describe('householdAriaLabel', () => {
  it('names the full status for an unreachable household', () => {
    const label = householdAriaLabel(hh({ unreachable_since: '2026-10-12T10:00:00Z', queued_envelopes: 1 }, 'down'),
      NOW.getTime() + 3 * 86400000)
    expect(label).toBe('Epsilon House, unreachable since Monday, 1 message waiting')
  })

  it('a behind household is connected and N updates behind', () => {
    expect(householdAriaLabel(hh({}, 'warn', 2))).toBe('Epsilon House, connected, 2 updates behind')
    expect(householdAriaLabel(hh())).toBe('Epsilon House, connected')
  })
})
