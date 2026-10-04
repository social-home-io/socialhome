import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  compatPeers,
  compatOurs,
  compatError,
  loadFederationCompat,
  peersBehindCount,
  peerSupportsResync,
  resyncPeerCapabilities,
  RESYNC_FEATURE,
  _resetFederationCompatForTest,
  type CompatPeer,
} from './federationCompat'

const fetchMock = vi.fn()

beforeEach(() => {
  _resetFederationCompatForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  _resetFederationCompatForTest()
})

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json' },
  })
}

function peer(over: Partial<CompatPeer>): CompatPeer {
  return {
    instance_id:        'i1',
    display_name:       'Peer One',
    proto_version:      18,
    status:             'confirmed',
    last_reachable_at:  null,
    capabilities_known: true,
    lacking_features:   [],
    ...over,
  }
}

describe('federationCompat store', () => {
  it('loadFederationCompat calls GET /api/admin/federation/compat and populates signals', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ours: 18,
        peers: [
          peer({ instance_id: 'i1', display_name: 'Alpha', proto_version: 18 }),
          peer({
            instance_id: 'i2', display_name: 'Beta', proto_version: 15,
            lacking_features: ['Bids and offers in the bazaar'],
          }),
        ],
      }),
    )
    await loadFederationCompat()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/admin/federation/compat')
    expect(compatOurs.value).toBe(18)
    expect(compatPeers.value).toHaveLength(2)
    expect(compatPeers.value[1].display_name).toBe('Beta')
  })

  it('loadFederationCompat sets compatError + empty list on failure (does not throw)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'SERVER_ERROR', detail: 'boom' } }, { status: 500 }),
    )
    await expect(loadFederationCompat()).resolves.toBeUndefined()
    expect(compatError.value).toBeTruthy()
    expect(compatPeers.value).toHaveLength(0)
  })

  // ── peersBehindCount ─────────────────────────────────────────────

  it('peersBehindCount counts a capabilities_known peer below ours', async () => {
    compatOurs.value = 18
    compatPeers.value = [
      peer({ instance_id: 'i1', proto_version: 15, capabilities_known: true }),
    ]
    expect(peersBehindCount()).toBe(1)
  })

  it('peersBehindCount does NOT count a capabilities_known=false peer (phantom first-contact nag)', async () => {
    compatOurs.value = 18
    compatPeers.value = [
      // Unknown caps reported at v1 placeholder — must not count as behind.
      peer({ instance_id: 'i1', proto_version: 1, capabilities_known: false }),
    ]
    expect(peersBehindCount()).toBe(0)
  })

  it('peersBehindCount does NOT count a peer at or above ours', async () => {
    compatOurs.value = 18
    compatPeers.value = [
      peer({ instance_id: 'i1', proto_version: 18, capabilities_known: true }),
      peer({ instance_id: 'i2', proto_version: 20, capabilities_known: true }),
    ]
    expect(peersBehindCount()).toBe(0)
  })

  it('peersBehindCount mixes the three cases correctly', async () => {
    compatOurs.value = 18
    compatPeers.value = [
      peer({ instance_id: 'i1', proto_version: 15, capabilities_known: true }),  // behind → counts
      peer({ instance_id: 'i2', proto_version: 1, capabilities_known: false }),  // unknown → skip
      peer({ instance_id: 'i3', proto_version: 18, capabilities_known: true }),  // up to date → skip
      peer({ instance_id: 'i4', proto_version: 12, capabilities_known: true }),  // behind → counts
    ]
    expect(peersBehindCount()).toBe(2)
  })

  // ── resync (§319.6) ──────────────────────────────────────────────

  it('peerSupportsResync: true for a known peer not lacking the resync feature', () => {
    expect(peerSupportsResync(peer({ capabilities_known: true, lacking_features: [] }))).toBe(true)
  })

  it('peerSupportsResync: false when capabilities unknown', () => {
    expect(peerSupportsResync(peer({ capabilities_known: false, lacking_features: [] }))).toBe(false)
  })

  it('peerSupportsResync: false when the peer still lacks the resync feature', () => {
    expect(
      peerSupportsResync(peer({ capabilities_known: true, lacking_features: [RESYNC_FEATURE] })),
    ).toBe(false)
  })

  it('resyncPeerCapabilities POSTs the capabilities scope to the resync endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ status: 'ok', instance_id: 'i9', scope: 'capabilities' }),
    )
    await resyncPeerCapabilities('i9')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/admin/federation/resync')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      instance_id: 'i9',
      scope: 'capabilities',
    })
  })
})
