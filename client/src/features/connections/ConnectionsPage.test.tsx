import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

// Mock i18n
// Returns the key, with any params appended as ``key(n=2)`` so tests can
// assert both which string is used and what is filled in. A test that
// flips ``i18nMode.real`` gets the real catalog instead (German render
// tests), via the actual module's ``t`` / ``isOne`` / ``setLocale``.
const { i18nMode } = vi.hoisted(() => ({ i18nMode: { real: false } }))
vi.mock('@/i18n/i18n', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/i18n/i18n')>()
  return {
    ...real,
    t: (key: string, params?: Record<string, string>) => {
      if (i18nMode.real) return real.t(key, params)
      return params
        ? `${key}(${Object.entries(params).map(([k, v]) => `${k}=${v}`).join(',')})`
        : key
    },
    isOne: (n: number) => (i18nMode.real ? real.isOne(n) : n === 1),
  }
})

// Mock pageTitle
vi.mock('@/store/pageTitle', () => ({
  useTitle: () => {},
}))

// Mock PairingFlow and related components to avoid complex dependencies
vi.mock('@/components/PairingFlow', () => ({
  openPairing: vi.fn(),
  PairingFlow: () => null,
}))

vi.mock('@/components/ConfirmDialog', () => ({
  ConfirmDialog: () => null,
}))

vi.mock('@/components/AutoPairDialog', () => ({
  AutoPairDialog: () => null,
  openAutoPair: vi.fn(),
}))

// ConnectionDetail renders nothing, but records the ``conn`` prop it was
// handed so tests can assert which API fields actually reach the modal —
// the prop literal in ConnectionsPage is an explicit field list, so a
// field the API returns can silently never arrive.
const { detailConns } = vi.hoisted(() => ({
  detailConns: [] as Array<Record<string, unknown>>,
}))
vi.mock('@/components/ConnectionDetail', () => ({
  ConnectionDetail: ({ conn }: { conn: Record<string, unknown> }) => {
    detailConns.push(conn)
    return null
  },
}))

// Federation-compat store — back it with real signals so the page can read
// compatPeers/compatOurs and call loadFederationCompat()/peersBehindCount().
const { compatSignals } = vi.hoisted(() => {
  return {
    compatSignals: {
      compatPeers: { value: [] as Array<Record<string, unknown>> },
      compatOurs: { value: 0 },
    },
  }
})
vi.mock('@/store/federationCompat', () => ({
  compatPeers: compatSignals.compatPeers,
  compatOurs: compatSignals.compatOurs,
  loadFederationCompat: vi.fn().mockResolvedValue(undefined),
  peersBehindCount: () =>
    compatSignals.compatPeers.value.filter(
      (p) => p.capabilities_known && (p.proto_version as number) < compatSignals.compatOurs.value,
    ).length,
}))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn(),
}))

// WS mock with handler capture for transport_changed tests.
// Use vi.hoisted so the factory and the test variables share the same
// reference even though vi.mock is hoisted to the top of the file.
const { wsHandlers, wsMock } = vi.hoisted(() => {
  const wsHandlers = new Map<string, Set<(evt: { type: string; data: Record<string, unknown> }) => void>>()
  const wsMock = {
    on: vi.fn((type: string, handler: (evt: { type: string; data: Record<string, unknown> }) => void) => {
      if (!wsHandlers.has(type)) wsHandlers.set(type, new Set())
      wsHandlers.get(type)!.add(handler)
      return () => { wsHandlers.get(type)?.delete(handler) }
    }),
  }
  return { wsHandlers, wsMock }
})
vi.mock('@/ws', () => ({ ws: wsMock }))

// The map view is lazy + Leaflet-backed; a stub records the props the page
// hands it so tests can drive its "Manage" hook directly.
const { mapProps } = vi.hoisted(() => ({
  mapProps: [] as Array<{
    onManage?: (c: Record<string, unknown>) => void
    canManage?: (c: Record<string, unknown>) => boolean
  }>,
}))
vi.mock('./FederationMap', () => ({
  default: (props: (typeof mapProps)[number]) => {
    mapProps.push(props)
    return <div data-testid="map-stub" />
  },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import ConnectionsPage from './ConnectionsPage'
import { setLocale } from '@/i18n/i18n'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

// Management controls render for admins only, so every test starts as the
// admin; a test that wants a member flips it itself. Without this reset a
// test would inherit whichever role the previous one left behind.
beforeEach(() => {
  ;(currentUser as { value: unknown }).value = {
    user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true,
    picture_url: null, bio: null, is_new_member: false,
  }
})

function makeConnection(over: Record<string, unknown> = {}) {
  return {
    instance_id: 'inst-1',
    display_name: 'Household Alpha',
    status: 'confirmed',
    reachable: true,
    transport: null,
    ...over,
  }
}

describe('ConnectionsPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./ConnectionsPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)

  describe('transport glyph', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    it('renders the RTC lightning glyph for transport=rtc', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'rtc' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        const icon = container.querySelector('.sh-transport-icon--rtc')
        expect(icon).not.toBeNull()
      })

      const icon = container.querySelector('.sh-transport-icon--rtc')!
      expect(icon.getAttribute('title')).toBe('connections.transport.direct_title')
      expect(icon.getAttribute('aria-label')).toBe('connections.transport.direct')
    })

    it('renders the HTTPS cloud glyph for transport=https', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'https' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        const icon = container.querySelector('.sh-transport-icon--https')
        expect(icon).not.toBeNull()
      })

      const icon = container.querySelector('.sh-transport-icon--https')!
      expect(icon.getAttribute('title')).toBe('connections.transport.internet_title')
      expect(icon.getAttribute('aria-label')).toBe('connections.transport.internet')
    })

    it('renders the relay glyph labelled "Through the GFS" for transport=gfs_relay', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'gfs_relay' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        const icon = container.querySelector('.sh-transport-icon--gfs-relay')
        expect(icon).not.toBeNull()
      })

      const icon = container.querySelector('.sh-transport-icon--gfs-relay')!
      expect(icon.getAttribute('title')).toBe('connections.transport.gfs')
      expect(icon.getAttribute('aria-label')).toBe('connections.transport.gfs')
      // Not mislabelled as a plain HTTPS peer.
      expect(container.querySelector('.sh-transport-icon--https')).toBeNull()
    })

    it('renders no transport glyph when transport is null', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: null })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        // Card should render
        expect(container.querySelector('.sh-connection-card')).not.toBeNull()
      })

      // Placeholder span reserves the 18px slot but renders nothing visible
      const slot = container.querySelector('.sh-transport-icon')
      expect(slot).not.toBeNull()
      expect(slot?.querySelector('svg')).toBeNull()
      expect(slot?.getAttribute('title')).toBeNull()
    })

    it('swaps the glyph in place when peer.transport_changed fires', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'https' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      // Wait for HTTPS glyph to appear
      await waitFor(() => {
        expect(container.querySelector('.sh-transport-icon--https')).not.toBeNull()
      })

      // Fire the peer.transport_changed WS event
      const handlers = wsHandlers.get('peer.transport_changed')
      expect(handlers).toBeDefined()
      handlers!.forEach(h => h({
        type: 'peer.transport_changed',
        data: { type: 'peer.transport_changed', instance_id: 'inst-1', transport: 'rtc' },
      }))

      // HTTPS glyph gone, RTC glyph appears
      await waitFor(() => {
        expect(container.querySelector('.sh-transport-icon--https')).toBeNull()
        expect(container.querySelector('.sh-transport-icon--rtc')).not.toBeNull()
      })
    })
  })

  describe('List / Map view toggle', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
      apiMock.get.mockResolvedValue([])
    })

    it('renders both List and Map tab buttons', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      expect(getByRole('button', { name: 'connections.view_list' })).toBeDefined()
      expect(getByRole('button', { name: 'connections.view_map' })).toBeDefined()
    })

    it('shows the lazy map fallback when Map tab is clicked', async () => {
      const { getByRole } = render(<ConnectionsPage />)

      // Click the Map tab — lazy Suspense fallback or map container appears
      getByRole('button', { name: 'connections.view_map' }).click()

      await waitFor(() => {
        // Either the Suspense fallback "Loading map…" or the rendered
        // FederationMap container is present.  In the test environment
        // the lazy module resolves synchronously so the testid wins.
        const container = document.querySelector('[data-testid="sh-federation-map"]')
        const fallback = document.querySelector('.sh-federation-map__loading')
        expect(container ?? fallback).not.toBeNull()
        // Guard: old placeholder text must NOT appear
        expect(document.body.textContent).not.toContain('Map coming in Task 11')
      })
    })

    it('List tab is aria-pressed=true by default', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      const listBtn = getByRole('button', { name: 'connections.view_list' })
      expect(listBtn.getAttribute('aria-pressed')).toBe('true')
      const mapBtn = getByRole('button', { name: 'connections.view_map' })
      expect(mapBtn.getAttribute('aria-pressed')).toBe('false')
    })

    it('Map tab becomes aria-pressed=true after click', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      const mapBtn = getByRole('button', { name: 'connections.view_map' })
      mapBtn.click()
      await waitFor(() => {
        expect(mapBtn.getAttribute('aria-pressed')).toBe('true')
        expect(getByRole('button', { name: 'connections.view_list' }).getAttribute('aria-pressed')).toBe('false')
      })
    })
  })

  describe('GFS connection status labels', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    function makeGfs(over: Record<string, unknown> = {}) {
      return {
        id: 'gfs-1',
        gfs_instance_id: 'i1',
        display_name: 'Town GFS',
        inbox_url: 'https://gfs.example.com',
        status: 'active',
        paired_at: '2026-06-06T00:00:00+00:00',
        published_space_count: 0,
        ...over,
      }
    }

    it('renders the "Pending approval" label for a pending GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'pending' })])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_pending')).toBeTruthy()
    })

    it('renders the "Suspended" label for a suspended GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'suspended' })])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_suspended')).toBeTruthy()
    })

    it('renders no status label for an active GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'active' })])
        return Promise.resolve([])
      })

      const { container, queryByText } = render(<ConnectionsPage />)
      await waitFor(() => {
        expect(container.querySelector('.sh-type-badge')).not.toBeNull()
      })
      expect(queryByText('gfs.status_pending')).toBeNull()
      expect(queryByText('gfs.status_suspended')).toBeNull()
    })

    it('shows "Connected" for an active GFS with a live socket', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([makeGfs({ status: 'active', connected: true, last_error: null })])
        return Promise.resolve([])
      })

      const { findByText, container } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_connected')).toBeTruthy()
      // Green/active dot, not unreachable.
      await waitFor(() => {
        expect(container.querySelector('.sh-status-dot--active')).not.toBeNull()
      })
    })

    it('shows "re-pair needed" for an active GFS whose WS is rejected (unknown-instance)', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({ status: 'active', connected: false, last_error: 'unknown-instance' }),
          ])
        return Promise.resolve([])
      })

      const { findByText, queryByText, container } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_repair_needed')).toBeTruthy()
      // Must NOT read as connected, and the dot is the unreachable one.
      expect(queryByText('gfs.status_connected')).toBeNull()
      await waitFor(() => {
        expect(container.querySelector('.sh-status-dot--unreachable')).not.toBeNull()
      })
    })

    it('shows "re-pair needed" for a bad-signature WS close', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({ status: 'active', connected: false, last_error: 'bad-signature' }),
          ])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_repair_needed')).toBeTruthy()
    })

    it('shows "clock out of sync" for a ts-skew WS close', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({ status: 'active', connected: false, last_error: 'ts-skew' }),
          ])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_clock_skew')).toBeTruthy()
    })

    it('shows "Reconnecting…" for an active GFS down with a transient/unknown reason', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({ status: 'active', connected: false, last_error: 'hello-timeout' }),
          ])
        return Promise.resolve([])
      })

      const { findByText, queryByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_reconnecting')).toBeTruthy()
      expect(queryByText('gfs.status_connected')).toBeNull()
    })

    it('shows "Reconnecting…" when an active GFS is down with no recorded reason', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({ status: 'active', connected: false, last_error: null }),
          ])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_reconnecting')).toBeTruthy()
    })

    it('does not render a raw GFS-controlled last_error string verbatim', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections')
          return Promise.resolve([
            makeGfs({
              status: 'active',
              connected: false,
              last_error: '<img src=x onerror=alert(1)>',
            }),
          ])
        return Promise.resolve([])
      })

      const { findByText, queryByText } = render(<ConnectionsPage />)
      // Unknown reason → mapped to the muted reconnecting label, never the raw string.
      expect(await findByText('gfs.status_reconnecting')).toBeTruthy()
      expect(queryByText('<img src=x onerror=alert(1)>')).toBeNull()
    })
  })

  describe('federation compatibility on the households surface', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
      compatSignals.compatPeers.value = []
      compatSignals.compatOurs.value = 0
    })

    function makeCompat(over: Record<string, unknown> = {}) {
      return {
        instance_id: 'inst-1',
        display_name: 'Household Alpha',
        proto_version: 19,
        status: 'confirmed',
        last_reachable_at: null,
        capabilities_known: true,
        lacking_features: [] as string[],
        ...over,
      }
    }

    it('shows "up to date ✓" for a confirmed peer with no lacking features', async () => {
      compatSignals.compatOurs.value = 19
      compatSignals.compatPeers.value = [makeCompat()]
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection()])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)
      await waitFor(() => {
        const card = container.querySelector('.sh-connection-card')
        expect(card).not.toBeNull()
        expect(card!.textContent).toContain('connections.compat.up_to_date')
      })
    })

    it('shows "N behind" for a peer lacking features', async () => {
      compatSignals.compatOurs.value = 19
      compatSignals.compatPeers.value = [
        makeCompat({ proto_version: 15, lacking_features: ['Bids and offers in the bazaar', 'Calendar overrides'] }),
      ]
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection()])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)
      await waitFor(() => {
        const card = container.querySelector('.sh-connection-card')
        expect(card).not.toBeNull()
        expect(card!.textContent).toContain('connections.compat.behind(n=2)')
      })
      const chip = container.querySelector('.sh-connection-card .sh-chip--update')
      expect(chip!.getAttribute('title')).toBe('Bids and offers in the bazaar, Calendar overrides')
    })

    describe('in German', () => {
      beforeEach(async () => {
        i18nMode.real = true
        await setLocale('de')
      })
      afterEach(async () => {
        await setLocale('en')
        i18nMode.real = false
      })

      it('renders the up-to-date, behind and unknown badges in German', async () => {
        compatSignals.compatOurs.value = 19
        compatSignals.compatPeers.value = [
          makeCompat(),
          makeCompat({ instance_id: 'inst-2', proto_version: 15, lacking_features: ['A', 'B'] }),
          makeCompat({ instance_id: 'inst-3', capabilities_known: false, proto_version: 1 }),
        ]
        apiMock.get.mockImplementation((url: string) => {
          if (url === '/api/connections') {
            return Promise.resolve([
              makeConnection(),
              makeConnection({ instance_id: 'inst-2', display_name: 'Beta' }),
              makeConnection({ instance_id: 'inst-3', display_name: 'Gamma' }),
            ])
          }
          return Promise.resolve([])
        })

        const { container } = render(<ConnectionsPage />)
        await waitFor(() => {
          expect(container.querySelectorAll('.sh-connection-card')).toHaveLength(3)
        })
        const cards = [...container.querySelectorAll('.sh-connection-card')]
        expect(cards[0].querySelector('.sh-chip--success')!.textContent).toBe('aktuell ✓')
        expect(cards[1].querySelector('.sh-chip--update')!.textContent).toBe('2 zurück')
        expect(cards[2].querySelector('.sh-chip--muted')!.textContent).toBe('Version unbekannt')
        expect(cards[0].querySelector('.sh-type-badge')!.textContent).toBe('Haushalt')
        // Households header: one household behind, singular aria label.
        const summary = container.querySelector('.sh-section-header .sh-chip--update')!
        expect(summary.textContent).toBe('1 zurück')
        expect(summary.getAttribute('aria-label')).toBe('1 Haushalt nicht aktuell')
        expect(container.textContent).toContain('Deine App-Version: v19')
      })
    })

    it('shows "version unknown" for a caps-unknown peer', async () => {
      compatSignals.compatOurs.value = 19
      compatSignals.compatPeers.value = [makeCompat({ capabilities_known: false, proto_version: 1 })]
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection()])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)
      await waitFor(() => {
        const card = container.querySelector('.sh-connection-card')
        expect(card).not.toBeNull()
        expect(card!.textContent).toContain('connections.compat.unknown')
      })
    })

    it('shows the "N behind" summary chip and "Your app version: vN" in the households header', async () => {
      compatSignals.compatOurs.value = 19
      compatSignals.compatPeers.value = [
        makeCompat({ proto_version: 15, lacking_features: ['Bids and offers in the bazaar'] }),
      ]
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection()])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)
      await waitFor(() => {
        expect(container.querySelector('.sh-connection-card')).not.toBeNull()
      })
      // Scope to the households section: the admin-only External URL
      // block also renders a `.sh-section-header`, and it sits above this
      // one, so a bare first-match selector picks the wrong header.
      const header = [...container.querySelectorAll('.sh-section-header')]
        .find(h => h.textContent?.includes('connections.households'))!
      expect(header.textContent).toContain('connections.your_version(v=19)')
      const summary = header.querySelector('.sh-chip--update')
      expect(summary).not.toBeNull()
      expect(summary!.textContent).toContain('connections.compat.behind_one(n=1)')
      expect(summary!.getAttribute('aria-label')).toBe('connections.compat.households_behind_one(n=1)')
    })

    it('omits the protocol-version line when compat has not loaded (ours=0)', async () => {
      compatSignals.compatOurs.value = 0
      compatSignals.compatPeers.value = []
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection()])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)
      await waitFor(() => {
        expect(container.querySelector('.sh-connection-card')).not.toBeNull()
      })
      expect(container.textContent).not.toContain('connections.your_version')
    })
  })

})

describe('ConnectionsPage — External URL (admin, non-haos)', () => {
  /**
   * The federation inbox base URL peers POST to. Until this landed, the
   * pairing failure told admins to "set this Social Home's external URL
   * in Settings → Connections" — and no such field existed anywhere in
   * the SPA, so the instruction was unfollowable.
   */
  async function renderPage(opts: {
    mode?: 'standalone' | 'ha' | 'haos'
    admin?: boolean
    base?: string | null
    effective?: string | null
    source?: string | null
  } = {}) {
    const { api } = await import('@/api')
    const { instanceConfig } = await import('@/store/instance')
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'admin', display_name: 'Admin',
      is_admin: opts.admin ?? true, picture_url: null, bio: null,
      is_new_member: false,
    }
    instanceConfig.value = {
      mode: opts.mode ?? 'standalone',
      instance_name: 'Test', instance_id: 'iid',
      capabilities: [], setup_required: false,
    }
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/admin/federation/external-url') {
        return Promise.resolve({
          base: opts.base ?? null,
          effective: opts.effective ?? null,
          source: opts.source ?? null,
        })
      }
      return Promise.resolve([])
    })
    const { default: ConnectionsPage } = await import('./ConnectionsPage')
    const r = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(document.body.textContent).toBeTruthy()
    })
    return r
  }

  it('standalone admin sees the field and the resolved inbox URL', async () => {
    const { container } = await renderPage({
      mode: 'standalone',
      base: 'https://home.example.com',
      effective: 'https://home.example.com/federation/inbox',
      source: 'manual',
    })
    await waitFor(() => {
      expect(container.querySelector('.sh-external-url-section')).not.toBeNull()
    })
    const input = container.querySelector('#sh-external-url') as HTMLInputElement
    expect(input).not.toBeNull()
    expect(input.value).toBe('https://home.example.com')
    // The admin must be able to see what peers actually POST to, which is
    // not the same string they typed.
    expect(container.textContent).toContain(
      'https://home.example.com/federation/inbox',
    )
  })

  it('warns when nothing is configured, since pairing will fail', async () => {
    const { container } = await renderPage({ mode: 'standalone' })
    await waitFor(() => {
      expect(container.querySelector('.sh-external-url-section')).not.toBeNull()
    })
    expect(container.textContent).toContain('connections.ext.not_configured')
  })

  it('ha admin sees the field too', async () => {
    const { container } = await renderPage({ mode: 'ha' })
    await waitFor(() => {
      expect(container.querySelector('.sh-external-url-section')).not.toBeNull()
    })
  })

  it('haos hides it — the HA integration owns the value there', async () => {
    // A hand-typed URL under the add-on would point at an inbox path only
    // the integration registers inside Home Assistant, so offering the
    // field would invite an unreachable address rather than fix one.
    const { container } = await renderPage({ mode: 'haos' })
    expect(container.querySelector('.sh-external-url-section')).toBeNull()
  })

  it('non-admins never see it', async () => {
    const { container } = await renderPage({ mode: 'standalone', admin: false })
    expect(container.querySelector('.sh-external-url-section')).toBeNull()
  })

  it('saves the trimmed value and clears with an empty field', async () => {
    const { api } = await import('@/api')
    const { container } = await renderPage({
      mode: 'standalone',
      base: 'https://old.example.com',
      effective: 'https://old.example.com/federation/inbox',
      source: 'manual',
    })
    await waitFor(() => {
      expect(container.querySelector('#sh-external-url')).not.toBeNull()
    })
    const input = container.querySelector('#sh-external-url') as HTMLInputElement
    const { fireEvent } = await import('@testing-library/preact')

    fireEvent.input(input, { target: { value: '  https://new.example.com  ' } })
    const save = [...container.querySelectorAll('button')]
      .find(b => /save/i.test(b.textContent ?? ''))!
    fireEvent.click(save)
    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/api/admin/federation/external-url',
        { base: 'https://new.example.com' },
      )
    })

    // Emptying the field clears it rather than storing a blank.
    fireEvent.input(input, { target: { value: '' } })
    fireEvent.click(
      [...container.querySelectorAll('button')]
        .find(b => /save/i.test(b.textContent ?? ''))!,
    )
    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/api/admin/federation/external-url',
        { base: null },
      )
    })
  })
})

describe('ConnectionsPage — connection-servers disclosure', () => {
  /**
   * Admin diagnostics for WebRTC. Collapsed by default and fetched only
   * on open — an operator needs it about once, when federation won't
   * connect, and until now the only evidence of a bad TURN setup was a
   * log warning they would likely never see.
   */
  async function renderWithIce(ice: unknown) {
    const { api } = await import('@/api')
    const { instanceConfig } = await import('@/store/instance')
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true,
      picture_url: null, bio: null, is_new_member: false,
    }
    instanceConfig.value = {
      mode: 'standalone', instance_name: 'T', instance_id: 'iid',
      capabilities: [], setup_required: false,
    }
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/admin/federation/ice-servers') return Promise.resolve(ice)
      if (url === '/api/admin/federation/external-url') {
        return Promise.resolve({ base: null, effective: null, source: null })
      }
      return Promise.resolve([])
    })
    const { default: ConnectionsPage } = await import('./ConnectionsPage')
    const r = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(r.container.querySelector('.sh-ice-panel__toggle')).not.toBeNull()
    })
    return r
  }

  /**
   * This used to assert the opposite — that nothing was fetched until the
   * row was opened. That deferral defeated the feature: ``summary`` is
   * '' until the data lands, so the badge was blank while collapsed, and
   * an operator who doesn't know their TURN was wiped has no reason to
   * click. The fetch now happens on mount so the warning is visible
   * without one. The row is still collapsed by default; only the fetch
   * timing changed.
   */
  it('is collapsed by default but fetches so the badge can warn first', async () => {
    const { api } = await import('@/api')
    const { container } = await renderWithIce({
      servers: [], has_turn: false, turn_usable: false,
      pulls_from_home_assistant: false,
    })
    const toggle = container.querySelector('.sh-ice-panel__toggle')!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelector('.sh-ice-panel__body')).toBeNull()
    expect(api.get).toHaveBeenCalledWith('/api/admin/federation/ice-servers')
  })

  it('shows the relay verdict on the collapsed row, before any click', async () => {
    // The point of the badge: a bad TURN setup announces itself instead
    // of hiding behind a disclosure nobody opens.
    const { container } = await renderWithIce({
      servers: [{ urls: ['turn:t.example:3478'], kinds: ['turn'], has_credentials: false }],
      has_turn: true, turn_usable: false, pulls_from_home_assistant: false,
    })
    await waitFor(() => {
      expect(container.querySelector('.sh-ice-panel__badge')?.textContent)
        .toBe('connections.ice.state_broken')
    })
    expect(container.querySelector('.sh-ice-panel__body')).toBeNull()
  })

  it('opens, fetches, and names the relay state', async () => {
    const { fireEvent } = await import('@testing-library/preact')
    const { container } = await renderWithIce({
      servers: [
        { urls: ['stun:stun.example:3478'], kinds: ['stun'], has_credentials: false },
        {
          urls: ['turn:t.example:3478'], kinds: ['turn'], has_credentials: true,
        },
      ],
      has_turn: true, turn_usable: true, pulls_from_home_assistant: false,
    })
    const toggle = container.querySelector('.sh-ice-panel__toggle')!
    fireEvent.click(toggle)
    await waitFor(() => {
      expect(container.querySelector('.sh-ice-panel__body')).not.toBeNull()
    })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // aria-controls must point at the panel it reveals.
    expect(toggle.getAttribute('aria-controls')).toBe(
      container.querySelector('.sh-ice-panel__body')!.id,
    )
    expect(container.textContent).toContain('turn:t.example:3478')
    expect(container.textContent).toContain('connections.ice.state_ready')
  })

  it('flags a relay that has no credentials', async () => {
    const { fireEvent } = await import('@testing-library/preact')
    const { container } = await renderWithIce({
      servers: [{ urls: ['turn:t.example:3478'], kinds: ['turn'], has_credentials: false }],
      has_turn: true, turn_usable: false, pulls_from_home_assistant: false,
    })
    fireEvent.click(container.querySelector('.sh-ice-panel__toggle')!)
    await waitFor(() => {
      expect(container.textContent).toContain('connections.ice.kind_backup_no_login')
    })
    expect(container.textContent).toContain('connections.ice.state_broken')
  })

  it('says so when nothing is configured', async () => {
    const { fireEvent } = await import('@testing-library/preact')
    const { container } = await renderWithIce({
      servers: [], has_turn: false, turn_usable: false,
      pulls_from_home_assistant: false,
    })
    fireEvent.click(container.querySelector('.sh-ice-panel__toggle')!)
    await waitFor(() => {
      expect(container.textContent).toContain('connections.ice.none')
    })
  })

  it('mentions Home Assistant when the list is pulled from it', async () => {
    const { fireEvent } = await import('@testing-library/preact')
    const { container } = await renderWithIce({
      servers: [{ urls: ['turn:t.example:3478'], kinds: ['turn'], has_credentials: true }],
      has_turn: true, turn_usable: true, pulls_from_home_assistant: true,
    })
    fireEvent.click(container.querySelector('.sh-ice-panel__toggle')!)
    await waitFor(() => {
      expect(container.textContent).toContain('connections.ice.from_ha')
    })
  })
})

describe('ConnectionsPage — diagnostics download', () => {
  async function renderAdmin() {
    const { api } = await import('@/api')
    const { instanceConfig } = await import('@/store/instance')
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true,
      picture_url: null, bio: null, is_new_member: false,
    }
    instanceConfig.value = {
      mode: 'standalone', instance_name: 'T', instance_id: 'iid',
      capabilities: [], setup_required: false,
    }
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/admin/diagnostics') {
        return Promise.resolve({ schema: 1, peers: [], build: { version: '1' } })
      }
      if (url === '/api/admin/federation/external-url') {
        return Promise.resolve({ base: null, effective: null, source: null })
      }
      return Promise.resolve([])
    })
    const { default: ConnectionsPage } = await import('./ConnectionsPage')
    const r = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(r.container.querySelector('.sh-diagnostics-link')).not.toBeNull()
    })
    return r
  }

  it('says what the file contains, so it is obviously safe to share', async () => {
    const { container } = await renderAdmin()
    const text = container.textContent ?? ''
    expect(text).toContain('connections.diag.note')
  })

  it('fetches through the api client and saves a timestamped file', async () => {
    const { fireEvent } = await import('@testing-library/preact')
    const { api } = await import('@/api')

    // jsdom has no Blob URL plumbing; record what the component does.
    const created: unknown[] = []
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = ((b: unknown) => {
      created.push(b)
      return 'blob:fake'
    }) as typeof URL.createObjectURL
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL
    const clicks: string[] = []
    const origClick = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
      clicks.push(this.download)
    }
    try {
      const { container } = await renderAdmin()
      fireEvent.click(container.querySelector('.sh-diagnostics-link')!)
      await waitFor(() => {
        expect(clicks.length).toBe(1)
      })
      // Auth + ingress base come from the api client, so it must not be a
      // bare <a href> navigation to the endpoint.
      expect(api.get).toHaveBeenCalledWith('/api/admin/diagnostics')
      expect(created).toHaveLength(1)
      expect(clicks[0]).toMatch(/^socialhome-diagnostics-.*\.json$/)
    } finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
      HTMLAnchorElement.prototype.click = origClick
    }
  })

  it('is not offered to non-admins', async () => {
    const { instanceConfig } = await import('@/store/instance')
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'bob', display_name: 'Bob', is_admin: false,
      picture_url: null, bio: null, is_new_member: false,
    }
    instanceConfig.value = {
      mode: 'standalone', instance_name: 'T', instance_id: 'iid',
      capabilities: [], setup_required: false,
    }
    const { default: ConnectionsPage } = await import('./ConnectionsPage')
    const { container } = render(<ConnectionsPage />)
    expect(container.querySelector('.sh-diagnostics-link')).toBeNull()
  })
})

describe('ConnectionsPage — admin panels under the Supervisor add-on (haos)', () => {
  /**
   * The connection-servers panel and the diagnostics download were
   * written as children of ExternalUrlSection, which is gated
   * ``!isSupervisorAddon()`` because the HA integration owns the
   * external URL there. They inherited that gate and vanished on haos —
   * the one mode where both matter most: haos is the only mode that
   * pulls ICE servers from HA (an empty reply used to wipe the
   * operator's STUN/TURN outright), and the diagnostics file is what an
   * add-on user attaches to a bug report.
   *
   * Every pre-existing test for both panels renders ``standalone``,
   * which is why nothing caught it. These pin the mode explicitly.
   */
  async function renderHaos(opts: { admin?: boolean } = {}) {
    const { api } = await import('@/api')
    const { instanceConfig } = await import('@/store/instance')
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'admin', display_name: 'Admin',
      is_admin: opts.admin ?? true, picture_url: null, bio: null,
      is_new_member: false,
    }
    instanceConfig.value = {
      mode: 'haos', instance_name: 'T', instance_id: 'iid',
      capabilities: [], setup_required: false,
    }
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/admin/federation/ice-servers') {
        return Promise.resolve({ servers: [], has_turn: false, turn_usable: false })
      }
      if (url === '/api/admin/federation/external-url') {
        return Promise.resolve({ base: null, effective: null, source: null })
      }
      return Promise.resolve([])
    })
    const { default: ConnectionsPage } = await import('./ConnectionsPage')
    return render(<ConnectionsPage />)
  }

  it('haos admin can still reach the connection-servers panel', async () => {
    const { container } = await renderHaos()
    await waitFor(() => {
      expect(container.querySelector('.sh-ice-panel__toggle')).not.toBeNull()
    })
  })

  it('haos admin can still download diagnostics', async () => {
    const { container } = await renderHaos()
    await waitFor(() => {
      expect(container.querySelector('.sh-diagnostics-link')).not.toBeNull()
    })
    expect(container.textContent).toContain('connections.diag.download')
  })

  it('haos still hides the External URL field', async () => {
    // Guard against over-correcting: only the URL field is haos-gated.
    const { container } = await renderHaos()
    await waitFor(() => {
      expect(container.querySelector('.sh-ice-panel__toggle')).not.toBeNull()
    })
    expect(container.querySelector('.sh-external-url-section')).toBeNull()
    expect(container.querySelector('#sh-external-url')).toBeNull()
  })

  it('sits below the households list, not above it', async () => {
    // Troubleshooting is a once-in-a-while tool; the households list is
    // what people open this page for. Compare document order so a future
    // re-shuffle has to be deliberate.
    const { container } = await renderHaos()
    await waitFor(() => {
      expect(container.querySelector('.sh-ice-panel__toggle')).not.toBeNull()
    })
    const sections = Array.from(container.querySelectorAll('section'))
    const households = sections.findIndex(s => s.querySelector('.sh-connection-list, .sh-empty-state'))
    const trouble = sections.findIndex(s => s.querySelector('.sh-ice-panel'))
    expect(households).toBeGreaterThanOrEqual(0)
    expect(trouble).toBeGreaterThan(households)
  })

  it('non-admins on haos see neither panel', async () => {
    const { container } = await renderHaos({ admin: false })
    await waitFor(() => {
      expect(document.body.textContent).toBeTruthy()
    })
    expect(container.querySelector('.sh-ice-panel__toggle')).toBeNull()
    expect(container.querySelector('.sh-diagnostics-link')).toBeNull()
  })
})


describe('ConnectionDetail prop plumbing', () => {
  beforeEach(() => {
    detailConns.length = 0
  })

  async function openManage(conn: Record<string, unknown>) {
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') return Promise.resolve([conn])
      return Promise.resolve([])
    })
    const { container } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-connection-card')).not.toBeNull()
    })
    const manage = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'connections.manage',
    )!
    expect(manage).toBeTruthy()
    fireEvent.click(manage)
    await waitFor(() => expect(detailConns.length).toBeGreaterThan(0))
    return detailConns[detailConns.length - 1]
  }

  it('passes last_reachable_at through so "Last connected" is not always "never"', async () => {
    const conn = await openManage(
      makeConnection({ reachable: false, last_reachable_at: '2026-06-04 08:12:33' }),
    )
    expect(conn.last_reachable_at).toBe('2026-06-04 08:12:33')
  })

  it('passes queued_envelopes through to the modal', async () => {
    const conn = await openManage(makeConnection({ queued_envelopes: 56 }))
    expect(conn.queued_envelopes).toBe(56)
  })

  it('passes dropped_envelopes through to the modal', async () => {
    const conn = await openManage(makeConnection({ dropped_envelopes: 263 }))
    expect(conn.dropped_envelopes).toBe(263)
  })

  it('passes the relay-acceptance state through to the modal', async () => {
    const conn = await openManage(
      makeConnection({ last_relay_accepted_at: '2026-09-20 10:00:00', relay_only: true }),
    )
    expect(conn.last_relay_accepted_at).toBe('2026-09-20 10:00:00')
    expect(conn.relay_only).toBe(true)
  })
})

describe('a household seated by an invite link (source = space_session)', () => {
  beforeEach(() => {
    wsHandlers.clear()
    wsMock.on.mockClear()
  })

  it('labels the row "Space only" and explains what it is not', async () => {
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') {
        return Promise.resolve([makeConnection({ source: 'space_session' })])
      }
      return Promise.resolve([])
    })
    const { container } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-connection-card')).not.toBeNull()
    })
    expect(container.querySelector('.sh-type-badge')!.textContent!.trim())
      .toBe('connections.type.space_only')
    expect(container.querySelector('[data-testid="space-only-note-inst-1"]'))
      .not.toBeNull()
  })

  it('offers no Manage (there is no social relationship to manage)', async () => {
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') {
        return Promise.resolve([makeConnection({ source: 'space_session' })])
      }
      return Promise.resolve([])
    })
    const { container, queryByText } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-connection-card')).not.toBeNull()
    })
    expect(queryByText('connections.manage')).toBeNull()
    // Unpair stays — leaving the arrangement must always be possible.
    expect(queryByText('connections.unpair')).not.toBeNull()
  })

  it('keeps the normal Household label + Manage for a QR-paired peer', async () => {
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') {
        return Promise.resolve([makeConnection({ source: 'qr' })])
      }
      return Promise.resolve([])
    })
    const { container, queryByText } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-connection-card')).not.toBeNull()
    })
    expect(container.querySelector('.sh-type-badge')!.textContent!.trim())
      .toBe('connections.type.household')
    expect(queryByText('connections.manage')).not.toBeNull()
  })

  it('never offers a space-only peer as a pairing voucher', async () => {
    // The backend rejects a space_session row as a voucher
    // (auto_pair_coordinator), so offering it here would be a dead end.
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') {
        return Promise.resolve([makeConnection({ source: 'space_session' })])
      }
      return Promise.resolve([])
    })
    const { container, queryByText } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-connection-card')).not.toBeNull()
    })
    expect(queryByText('connections.pair_via_peer')).toBeNull()
  })
})

describe('ConnectionsPage — managing household connections is admin-only', () => {
  // The backend answers 403 to a non-admin on every pairing / unpair /
  // connection-server write, so the page must not offer those controls to
  // a member who reaches it by URL. The list itself stays readable.
  async function setAdmin(isAdmin: boolean) {
    const { currentUser } = await import('@/store/auth')
    ;(currentUser as { value: unknown }).value = {
      user_id: 'u1', username: 'u', display_name: 'U', is_admin: isAdmin,
      picture_url: null, bio: null, is_new_member: false,
    }
  }

  async function renderAs(isAdmin: boolean) {
    await setAdmin(isAdmin)
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') return Promise.resolve([makeConnection()])
      if (url === '/api/gfs/connections') {
        return Promise.resolve([{
          id: 'g1', display_name: 'Relay One', inbox_url: 'https://gfs.example',
          status: 'active',
        }])
      }
      if (url === '/api/admin/federation/external-url') {
        return Promise.resolve({ base: null, effective: null, source: null })
      }
      return Promise.resolve([])
    })
    const r = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(r.container.textContent).toContain('Relay One')
      expect(r.container.textContent).toContain('Household Alpha')
    })
    return r
  }

  function buttonTexts(container: Element): string[] {
    return [...container.querySelectorAll('button')]
      .map(b => (b.textContent ?? '').trim())
  }

  const MANAGE_CONTROLS = [
    '+ connections.pair', 'connections.pair_via_peer', 'connections.manage', 'connections.unpair',
    '+ gfs.add', 'gfs.disconnect',
  ]

  it('an admin gets pair, pair-via, manage, unpair and connection-server controls', async () => {
    const { container } = await renderAs(true)
    const texts = buttonTexts(container)
    for (const shown of MANAGE_CONTROLS) expect(texts).toContain(shown)
    expect(container.textContent)
      .not.toContain('connections.admin_only')
  })

  it('a non-admin sees the households read-only with an admin hint', async () => {
    const { container } = await renderAs(false)
    const texts = buttonTexts(container)
    for (const hidden of MANAGE_CONTROLS) expect(texts).not.toContain(hidden)
    expect(container.textContent)
      .toContain('connections.admin_only')
  })

  it('a non-admin with no connections gets no start-pairing button', async () => {
    await setAdmin(false)
    apiMock.get.mockImplementation(() => Promise.resolve([]))
    const { container } = render(<ConnectionsPage />)
    await waitFor(() => {
      expect(container.textContent).toContain('connections.no_connections')
    })
    expect(buttonTexts(container)).not.toContain('connections.start_pairing')
    expect(buttonTexts(container)).not.toContain('+ gfs.add')
  })
})


describe('Map view "Manage"', () => {
  beforeEach(() => {
    detailConns.length = 0
    mapProps.length = 0
  })

  async function openMap(conns: Record<string, unknown>[]) {
    apiMock.get.mockImplementation((url: string) => {
      if (url === '/api/connections') return Promise.resolve(conns)
      return Promise.resolve([])
    })
    const r = render(<ConnectionsPage />)
    const chip = await waitFor(() => {
      const b = Array.from(r.container.querySelectorAll('button')).find(
        (x) => x.textContent?.trim() === 'connections.view_map',
      )
      expect(b).toBeTruthy()
      return b!
    })
    fireEvent.click(chip)
    await waitFor(() => expect(r.getByTestId('map-stub')).toBeTruthy())
    return mapProps[mapProps.length - 1]
  }

  it('opens the same manage dialog as the list for a household peer', async () => {
    const peer = makeConnection()
    const props = await openMap([peer])
    expect(props.canManage!(peer)).toBe(true)
    props.onManage!(peer)
    await waitFor(() => expect(detailConns.length).toBeGreaterThan(0))
    expect(detailConns[detailConns.length - 1].instance_id).toBe('inst-1')
  })

  it('offers no Manage for a space-only or pending peer', async () => {
    const props = await openMap([makeConnection()])
    expect(props.canManage!(makeConnection({ source: 'space_session' }))).toBe(false)
    expect(props.canManage!(makeConnection({ status: 'pending_sent' }))).toBe(false)
  })

  it('a member (not admin) gets no Manage hook at all', async () => {
    ;(currentUser as { value: { is_admin: boolean } }).value.is_admin = false
    const props = await openMap([makeConnection()])
    expect(props.onManage).toBeUndefined()
  })
})
