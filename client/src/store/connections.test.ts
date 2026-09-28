/**
 * Tests for the connections store's WS handler wiring.
 *
 * ``wireConnectionsWs()`` is the single source of truth for
 * ``peer.home_changed`` and ``local.home_changed`` frame handling.
 * We mock the ``ws`` module so we can drive synthetic frames at the
 * registered callbacks and assert that the signals mutate correctly.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}))

import { connections, selfLat, selfLon, wireConnectionsWs } from './connections'

describe('wireConnectionsWs', () => {
  beforeEach(() => {
    connections.value = []
    selfLat.value = null
    selfLon.value = null
    Object.keys(handlers).forEach(k => delete handlers[k])
    apiGet.mockReset()
    wireConnectionsWs()
  })

  it('does not listen for the never-emitted connection.added frame', () => {
    // A new pairing is signalled by ``pairing.confirmed`` — the server has
    // no ``connection.added`` frame, so a handler for it is dead code.
    expect(handlers['connection.added']).toBeUndefined()
  })

  it('pairing.confirmed refetches the list so a new peer shows without a reload', async () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true, status: 'confirmed' },
    ]
    apiGet.mockResolvedValueOnce([
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true, status: 'confirmed' },
      { instance_id: 'peer-2', display_name: 'Carol', reachable: true, status: 'confirmed' },
    ])
    handlers['pairing.confirmed']({ data: { type: 'pairing.confirmed', instance_id: 'peer-2' } })
    await vi.waitFor(() => expect(connections.value).toHaveLength(2))
    expect(apiGet).toHaveBeenCalledWith('/api/connections')
    expect(connections.value[1].display_name).toBe('Carol')
  })

  it('pairing.confirmed keeps the current list when the refetch fails', async () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true },
    ]
    apiGet.mockRejectedValueOnce(new Error('offline'))
    handlers['pairing.confirmed']({ data: { instance_id: 'peer-2' } })
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalled())
    await Promise.resolve()
    expect(connections.value).toHaveLength(1)
  })

  it('connection.removed drops the unpaired peer from the list', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true },
      { instance_id: 'peer-2', display_name: 'Carol', reachable: true },
    ]
    handlers['connection.removed']({ data: { type: 'connection.removed', instance_id: 'peer-1' } })
    expect(connections.value.map(c => c.instance_id)).toEqual(['peer-2'])
  })

  it('connection.removed without an instance_id is a no-op', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true },
    ]
    handlers['connection.removed']({ data: {} })
    expect(connections.value).toHaveLength(1)
  })

  it('local.home_changed updates selfLat and selfLon signals', () => {
    handlers['local.home_changed']({ data: { latitude: 52.52, longitude: 13.405 } })
    expect(selfLat.value).toBe(52.52)
    expect(selfLon.value).toBe(13.405)
  })

  it('local.home_changed is a no-op when latitude is missing', () => {
    handlers['local.home_changed']({ data: { longitude: 13.405 } })
    expect(selfLat.value).toBeNull()
    expect(selfLon.value).toBeNull()
  })

  it('local.home_changed is a no-op when longitude is missing', () => {
    handlers['local.home_changed']({ data: { latitude: 52.52 } })
    expect(selfLat.value).toBeNull()
    expect(selfLon.value).toBeNull()
  })

  it('peer.home_changed updates the matching connection coords', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true, home_lat: null, home_lon: null },
      { instance_id: 'peer-2', display_name: 'Carol', reachable: true, home_lat: 50.0, home_lon: 8.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-1', latitude: 53.55, longitude: 9.99 },
    })
    const updated = connections.value.find(c => c.instance_id === 'peer-1')
    expect(updated?.home_lat).toBe(53.55)
    expect(updated?.home_lon).toBe(9.99)
    // peer-2 must remain unchanged
    expect(connections.value.find(c => c.instance_id === 'peer-2')?.home_lon).toBe(8.0)
  })

  it('peer.home_changed for unknown instance_id is a no-op', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Peer 1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-unknown', latitude: 99, longitude: 99 },
    })
    expect(connections.value).toHaveLength(1)
    expect(connections.value[0].home_lat).toBe(1.0)
    expect(connections.value[0].home_lon).toBe(2.0)
  })

  it('peer.home_changed is a no-op when instance_id is missing', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Peer 1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { latitude: 99, longitude: 99 },
    })
    expect(connections.value[0].home_lat).toBe(1.0)
  })

  it('peer.home_changed is a no-op when latitude is null', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Peer 1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-1', longitude: 9.99 },
    })
    expect(connections.value[0].home_lat).toBe(1.0)
    expect(connections.value[0].home_lon).toBe(2.0)
  })
})
