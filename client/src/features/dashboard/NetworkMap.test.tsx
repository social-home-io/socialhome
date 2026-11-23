/**
 * NetworkMap — the dashboard's map of paired households. It draws and
 * counts exactly what ``GET /api/connections`` lists: the households we're
 * paired with (confirmed or pending), never one we only share a space with.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

import NetworkMap from './NetworkMap'
import { connections } from '@/store/connections'

beforeEach(() => {
  connections.value = []
  apiGet.mockReset()
})

describe('NetworkMap', () => {
  it('counts and draws the paired households the server lists', async () => {
    apiGet.mockResolvedValue([
      { instance_id: 'a', display_name: 'The Müllers', status: 'confirmed', reachable: true },
      { instance_id: 'p', display_name: 'Pending House', status: 'pending_sent', reachable: false },
    ])
    const { container } = render(<NetworkMap />)
    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull())
    expect(apiGet).toHaveBeenCalledWith('/api/connections')
    expect(container.querySelector('header')!.textContent)
      .toBe('Network1 paired · 1 reachable · 1 pending')
    expect(container.querySelectorAll('.sh-network-map-svg g')).toHaveLength(2)
  })

  it('shows the empty note when no household is paired', async () => {
    apiGet.mockResolvedValue([])
    const { container } = render(<NetworkMap />)
    await waitFor(() => expect(container.textContent).toContain('No paired households yet'))
    expect(container.querySelector('svg')).toBeNull()
  })
})
