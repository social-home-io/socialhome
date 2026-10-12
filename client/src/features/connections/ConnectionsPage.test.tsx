import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent, act } from '@testing-library/preact'

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

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

// i18n returns the key, with params appended as ``key(n=2)`` so tests can
// assert which string is used and what is filled in. Placeholders the
// caller fills with JSX nodes (``{em}``, ``{name}`` in bold…) are kept as
// ``key{name}`` so ``fillNodes`` still has somewhere to put them.
vi.mock('@/i18n/i18n', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/i18n/i18n')>()
  return {
    ...real,
    t: (key: string, params?: Record<string, string>) => {
      const missing = [...real.t(key).matchAll(/\{(\w+)\}/g)]
        .map((m) => m[1])
        .filter((p) => !params || !(p in params))
      const base = params
        ? `${key}(${Object.entries(params).map(([k, v]) => `${k}=${v}`).join(',')})`
        : key
      return base + missing.map((m) => `{${m}}`).join('')
    },
    isOne: (n: number) => n === 1,
    formatLocale: () => 'en-US',
  }
})

vi.mock('@/store/pageTitle', () => ({ useTitle: () => {} }))

vi.mock('@/components/PairingFlow', () => ({
  openPairing: vi.fn(),
  PairingFlow: () => null,
}))

vi.mock('@/components/ConfirmDialog', () => ({
  ConfirmDialog: ({ open, onConfirm }: { open: boolean; onConfirm: () => void }) =>
    open ? <button data-testid="gfs-disconnect-confirm" onClick={onConfirm}>confirm</button> : null,
}))

vi.mock('@/components/AutoPairDialog', () => ({
  AutoPairDialog: () => null,
  openAutoPair: vi.fn(),
}))

// ConnectionDetail renders nothing but records the ``conn`` prop, so tests
// can assert which API fields actually reach the Manage dialog.
const { detailConns, detailProps } = vi.hoisted(() => ({
  detailConns: [] as Array<Record<string, unknown>>,
  detailProps: [] as Array<{ onRevoke: () => void }>,
}))
vi.mock('@/components/ConnectionDetail', () => ({
  ConnectionDetail: (props: { conn: Record<string, unknown>; onRevoke: () => void }) => {
    detailConns.push(props.conn)
    detailProps.push(props)
    return null
  },
}))

const { compatSignals, loadCompat } = vi.hoisted(() => ({
  compatSignals: {
    compatPeers: { value: [] as Array<Record<string, unknown>> },
    compatOurs: { value: 0 },
  },
  loadCompat: { fn: null as null | (() => Promise<void>) },
}))
vi.mock('@/store/federationCompat', () => ({
  compatPeers: compatSignals.compatPeers,
  compatOurs: compatSignals.compatOurs,
  loadFederationCompat: () => (loadCompat.fn ? loadCompat.fn() : Promise.resolve()),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))

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

const { mapProps } = vi.hoisted(() => ({
  mapProps: [] as Array<{
    onManage?: (c: Record<string, unknown>) => void
    canManage?: (c: Record<string, unknown>) => boolean
    showOffMapFooter?: boolean
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
import { instanceConfig } from '@/store/instance'
import {
  connections, connectionsLoadError, connectionsLoadedOnce, connectionsLoading,
} from '@/store/connections'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { openAutoPair } from '@/components/AutoPairDialog'
import { openPairing } from '@/components/PairingFlow'
import ConnectionsPage from './ConnectionsPage'
import {
  autoPairRequests, externalUrl, gfsConnections, gfsError, gfsLoading, householdAdmins,
} from './connectionsData'
import { advancedOpen } from './AdvancedRow'

const apiGet = api.get as unknown as ReturnType<typeof vi.fn>
const apiPost = api.post as unknown as ReturnType<typeof vi.fn>
const apiPut = api.put as unknown as ReturnType<typeof vi.fn>
const apiDelete = api.delete as unknown as ReturnType<typeof vi.fn>

function setUser(isAdmin: boolean) {
  ;(currentUser as { value: unknown }).value = {
    user_id: 'u1', username: isAdmin ? 'admin' : 'mona', display_name: isAdmin ? 'Admin' : 'Mona',
    is_admin: isAdmin, picture_url: null, bio: null, is_new_member: false,
  }
}

function setMode(mode: 'standalone' | 'ha' | 'haos') {
  instanceConfig.value = {
    mode, instance_name: 'Test', instance_id: 'iid', capabilities: [], setup_required: false,
  } as unknown as typeof instanceConfig.value
}

function makeConnection(over: Record<string, unknown> = {}) {
  return {
    instance_id: 'inst-1',
    display_name: 'Household Alpha',
    status: 'confirmed',
    reachable: true,
    transport: 'rtc',
    last_reachable_at: '2026-06-04 08:12:33',
    ...over,
  }
}

function makeGfs(over: Record<string, unknown> = {}) {
  return {
    id: 'gfs-1',
    gfs_instance_id: 'i1',
    display_name: 'Town GFS',
    inbox_url: 'https://gfs.example.com:8443/inbox/x',
    status: 'active',
    connected: true,
    paired_at: '2026-06-06T00:00:00+00:00',
    published_space_count: 0,
    ...over,
  }
}

interface Fixture {
  conns?: unknown[] | Error
  gfs?: unknown[] | Error
  requests?: unknown[]
  users?: unknown[]
  ext?: Record<string, unknown>
  ice?: unknown
  extra?: Record<string, unknown>
}

function serve(f: Fixture = {}) {
  apiGet.mockImplementation((url: string) => {
    const pick = (v: unknown) => (v instanceof Error ? Promise.reject(v) : Promise.resolve(v))
    if (url === '/api/connections') return pick(f.conns ?? [])
    if (url === '/api/gfs/connections') return pick(f.gfs ?? [])
    if (url === '/api/pairing/auto-pair-requests') return pick(f.requests ?? [])
    if (url === '/api/users') return pick(f.users ?? [])
    if (url === '/api/admin/federation/external-url') {
      return pick(f.ext ?? { base: null, effective: 'https://home.example.com', source: 'auto', manual_supported: true })
    }
    if (url === '/api/admin/federation/ice-servers') {
      return pick(f.ice ?? { servers: [], has_turn: false, turn_usable: false, pulls_from_home_assistant: false })
    }
    if (f.extra && url in f.extra) return pick(f.extra[url])
    return Promise.resolve([])
  })
}

async function renderPage(f: Fixture = {}) {
  serve(f)
  const r = render(<ConnectionsPage />)
  await waitFor(() => {
    expect(r.container.querySelector('.sh-conn-hero--skeleton')).toBeNull()
    expect(gfsLoading.value).toBe(false)
  })
  return r
}

function adminCalls(): string[] {
  return apiGet.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith('/api/admin/'))
}

beforeEach(() => {
  setUser(true)
  setMode('standalone')
  apiGet.mockReset()
  apiPost.mockReset().mockResolvedValue({})
  apiPut.mockReset().mockResolvedValue({})
  apiDelete.mockReset().mockResolvedValue(undefined)
  ;(showToast as ReturnType<typeof vi.fn>).mockReset()
  ;(confirmDialog as ReturnType<typeof vi.fn>).mockReset()
  ;(openAutoPair as ReturnType<typeof vi.fn>).mockReset()
  ;(openPairing as ReturnType<typeof vi.fn>).mockReset()
  connections.value = []
  connectionsLoading.value = true
  connectionsLoadError.value = false
  connectionsLoadedOnce.value = false
  gfsConnections.value = []
  gfsLoading.value = true
  gfsError.value = false
  autoPairRequests.value = []
  householdAdmins.value = null
  externalUrl.value = { loaded: false, stored: null, effective: null, source: null, manualSupported: true }
  advancedOpen.value = {}
  compatSignals.compatPeers.value = []
  compatSignals.compatOurs.value = 0
  loadCompat.fn = null
  detailConns.length = 0
  detailProps.length = 0
  mapProps.length = 0
  wsHandlers.clear()
  wsMock.on.mockClear()
})

describe('ConnectionsPage — admin, problem state', () => {
  const eps = makeConnection({
    instance_id: 'eps', display_name: 'Epsilon House', reachable: false, transport: null,
    unreachable_since: '2026-06-01T09:00:00+00:00', queued_envelopes: 12,
  })
  const beta = makeConnection({ instance_id: 'beta', display_name: 'Beta House' })
  const alpha = makeConnection({ instance_id: 'alpha', display_name: 'Alpha House' })

  function withCompat() {
    loadCompat.fn = async () => {
      compatSignals.compatOurs.value = 57
      compatSignals.compatPeers.value = [
        { instance_id: 'beta', proto_version: 55, capabilities_known: true, lacking_features: ['a', 'b'] },
        { instance_id: 'alpha', proto_version: 57, capabilities_known: true, lacking_features: [] },
      ]
    }
  }

  it('orders problems first and names the problem in the hero', async () => {
    withCompat()
    const { container } = await renderPage({ conns: [alpha, beta, eps], gfs: [makeGfs()] })
    const names = [...container.querySelectorAll('.sh-conn-list .sh-conn-card__name')].map((n) => n.textContent)
    expect(names.slice(0, 3)).toEqual(['Epsilon House', 'Beta House', 'Alpha House'])
    const headline = container.querySelector('.sh-conn-hero__headline')!.textContent
    expect(headline).toContain('connections.hero.problem(n=2,total=3)')
    expect(container.querySelector('.sh-conn-hero__sub')!.textContent).toContain('connections.hero.sub_down(name=Epsilon House')
    const chips = [...container.querySelectorAll('.sh-conn-chips > li')].map((c) => c.textContent)
    expect(chips[0]).toBe('connections.hero.chip_unreachable(n=1)')
    expect(chips[1]).toBe('connections.hero.chip_behind_one(n=1)')
    expect(chips[2]).toContain('connections.hero.your_version')
  })

  it('shows the "N updates behind" chip, honey ring and ↑ badge for an admin', async () => {
    withCompat()
    const { container } = await renderPage({ conns: [beta] })
    const card = container.querySelector('#sh-conn-card-beta')!
    expect(card.classList.contains('sh-conn-card--warn')).toBe(true)
    expect(card.querySelector('.sh-conn-chip--warn')!.textContent).toBe('connections.card.behind(n=2)')
    expect(card.querySelector('.sh-conn-av')!.getAttribute('data-s')).toBe('warn')
    expect(card.querySelector('.sh-conn-badge--warn')).not.toBeNull()
  })

  it('gives an unreachable card a full-status accessible name', async () => {
    const { container } = await renderPage({ conns: [eps] })
    const card = container.querySelector('#sh-conn-card-eps')!
    expect(card.tagName).toBe('BUTTON')
    const label = card.getAttribute('aria-label')!
    expect(label.startsWith('Epsilon House, connections.card.status_unreachable_since(when=')).toBe(true)
    expect(label).toContain('connections.meta.waiting(n=12)')
    expect(card.querySelector('.sh-conn-down')!.textContent).toContain('connections.meta.unreachable_since')
  })

  it('a problem chip moves focus to the card', async () => {
    const { container, getByText } = await renderPage({ conns: [alpha, eps] })
    fireEvent.click(getByText('connections.hero.chip_unreachable(n=1)'))
    expect(document.activeElement).toBe(container.querySelector('#sh-conn-card-eps'))
  })

  it('the hero CTAs open the QR flow and the via-a-household flow', async () => {
    const { getByText } = await renderPage({ conns: [alpha] })
    fireEvent.click(getByText('connections.hero.pair'))
    expect(openPairing).toHaveBeenCalledWith('household')
    fireEvent.click(getByText('connections.hero.via_long'))
    expect(openAutoPair).toHaveBeenCalledWith([{ instance_id: 'alpha', display_name: 'Alpha House' }])
  })

  it('a card opens Manage with every field the dialog reads', async () => {
    const conn = makeConnection({
      reachable: false, last_reachable_at: '2026-06-04 08:12:33', queued_envelopes: 7,
      dropped_envelopes: 3, last_relay_accepted_at: '2026-06-04 09:00:00', relay_only: true,
      share_home: false, gfs_relay: true, gfs_routes: 2, peer_keywrap_known: true,
      gfs_relay_available: true, transport: null,
    })
    const { container } = await renderPage({ conns: [conn] })
    fireEvent.click(container.querySelector('#sh-conn-card-inst-1')!)
    await waitFor(() => expect(detailConns.length).toBeGreaterThan(0))
    const got = detailConns[detailConns.length - 1]
    expect(got).toMatchObject({
      last_reachable_at: '2026-06-04 08:12:33', queued_envelopes: 7, dropped_envelopes: 3,
      last_relay_accepted_at: '2026-06-04 09:00:00', relay_only: true, share_home: false,
      gfs_relay: true, gfs_routes: 2, peer_keywrap_known: true, gfs_relay_available: true,
    })
  })

  it('passes the live reachable flag to Manage (one status rule everywhere)', async () => {
    const { container } = await renderPage({ conns: [alpha] })
    fireEvent.click(container.querySelector('#sh-conn-card-alpha')!)
    await waitFor(() => expect(detailConns.length).toBeGreaterThan(0))
    expect(detailConns[detailConns.length - 1].reachable).toBe(true)
  })

  it('after Unpair in Manage, focus lands on the Households heading', async () => {
    const { container } = await renderPage({ conns: [alpha, beta] })
    fireEvent.click(container.querySelector('#sh-conn-card-alpha')!)
    await waitFor(() => expect(detailProps.length).toBeGreaterThan(0))
    serve({ conns: [beta] })
    act(() => { detailProps[detailProps.length - 1].onRevoke() })
    await waitFor(() => expect(document.activeElement?.id).toBe('sh-conn-hh-title'))
  })

  it('offers no Unpair on the cards (Manage keeps Remove)', async () => {
    const { container } = await renderPage({ conns: [alpha, eps] })
    expect(container.textContent).not.toContain('connections.unpair')
  })

  it('swaps the transport in place on peer.transport_changed', async () => {
    const { container } = await renderPage({ conns: [alpha] })
    expect(container.querySelector('[data-transport="rtc"]')).not.toBeNull()
    for (const h of wsHandlers.get('peer.transport_changed') ?? []) {
      h({ type: 'peer.transport_changed', data: { instance_id: 'alpha', transport: 'gfs_relay' } })
    }
    await waitFor(() => {
      expect(container.querySelector('[data-transport="gfs_relay"]')!.textContent).toContain('connections.transport.gfs')
    })
  })
})

describe('ConnectionsPage — healthy and first run', () => {
  it('healthy: "Everyone\'s connected" with the household count and version', async () => {
    compatSignals.compatOurs.value = 57
    const { container } = await renderPage({ conns: [makeConnection()] })
    expect(container.querySelector('.sh-conn-hero__headline')!.textContent).toContain('connections.hero.healthy')
    expect(container.querySelector('.sh-conn-hero__ok')!.textContent)
      .toBe('connections.hero.households_one(n=1) · connections.hero.your_version(v=v57)')
  })

  it('empty: PairingSteps replace the list, no via CTA, and the setup warning links both fixes', async () => {
    const { container, queryByText, getByText } = await renderPage({
      conns: [], ext: { base: null, effective: null, source: null, manual_supported: true },
    })
    expect(container.querySelector('.sh-conn-steps')).not.toBeNull()
    expect(queryByText('connections.hero.via_long')).toBeNull()
    await waitFor(() => expect(container.querySelector('.sh-conn-hero__warn')).not.toBeNull())
    expect(getByText('connections.hero.setup_warn_ext')).toBeTruthy()
    fireEvent.click(getByText('connections.hero.setup_warn_gfs'))
    expect(openPairing).toHaveBeenCalledWith('gfs')
    // Not set → the External address row starts open.
    await waitFor(() => {
      expect(container.querySelector('#sh-conn-adv-ext')!.hasAttribute('open')).toBe(true)
    })
  })

  it('empty: no setup warning once a GFS is connected', async () => {
    const { container } = await renderPage({
      conns: [], gfs: [makeGfs()], ext: { base: null, effective: null, source: null },
    })
    await waitFor(() => expect(externalUrl.value.loaded).toBe(true))
    expect(container.querySelector('.sh-conn-hero__warn')).toBeNull()
  })

  it('empty GFS list shows the explainer with an add button for admins', async () => {
    const { getByText } = await renderPage({ conns: [makeConnection()], gfs: [] })
    expect(getByText('connections.gfs.empty_title')).toBeTruthy()
    fireEvent.click(getByText('connections.gfs.empty_add'))
    expect(openPairing).toHaveBeenCalledWith('gfs')
  })
})

describe('ConnectionsPage — loading and load errors', () => {
  it('shows card-shaped skeletons while the first load runs, never the empty state', async () => {
    apiGet.mockImplementation(() => new Promise(() => {}))
    const { container } = render(<ConnectionsPage />)
    expect(container.querySelector('.sh-conn-hero--skeleton')).not.toBeNull()
    expect(container.querySelectorAll('.sh-conn-card--skeleton').length).toBe(4)
    expect(container.querySelector('[role="status"]')!.textContent).toBe('connections.loading')
    expect(container.querySelector('.sh-conn-steps')).toBeNull()
  })

  it('a failed load shows LoadError (hero hidden), not "no households yet"', async () => {
    const { container, getByRole } = await renderPage({ conns: new Error('offline') })
    await waitFor(() => expect(container.querySelector('.sh-conn-error')).not.toBeNull())
    expect(getByRole('alert').textContent).toContain('connections.error.title')
    expect(container.querySelector('.sh-conn-hero')).toBeNull()
    expect(container.querySelector('.sh-conn-steps')).toBeNull()

    serve({ conns: [makeConnection()] })
    fireEvent.click(getByRole('button', { name: 'connections.error.retry' }))
    await waitFor(() => expect(container.querySelector('.sh-conn-error')).toBeNull())
    expect(container.querySelector('.sh-conn-hero')).not.toBeNull()
  })

  it('a failed REFETCH keeps the known list and says so in a toast', async () => {
    const { container } = await renderPage({ conns: [makeConnection()] })
    expect(container.querySelector('#sh-conn-card-inst-1')).not.toBeNull()
    serve({ conns: new Error('offline') })
    for (const h of wsHandlers.get('pairing.aborted') ?? []) h({ type: 'pairing.aborted', data: {} })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('connections.error.refresh', 'error'))
    expect(container.querySelector('.sh-conn-error')).toBeNull()
    expect(container.querySelector('#sh-conn-card-inst-1')).not.toBeNull()
    expect(container.querySelector('.sh-conn-hero')).not.toBeNull()
  })

  it('a refetch of an empty list never flashes the skeleton', async () => {
    const { container } = await renderPage({ conns: [] })
    expect(container.querySelector('.sh-conn-steps')).not.toBeNull()
    apiGet.mockImplementation((url: string) =>
      url === '/api/connections' ? new Promise(() => {}) : Promise.resolve([]))
    for (const h of wsHandlers.get('pairing.aborted') ?? []) h({ type: 'pairing.aborted', data: {} })
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('.sh-conn-card--skeleton')).toBeNull()
    expect(container.querySelector('.sh-conn-hero--skeleton')).toBeNull()
    expect(container.querySelector('.sh-conn-steps')).not.toBeNull()
  })

  it('a failed GFS load says so in the GFS section', async () => {
    const { container } = await renderPage({ conns: [makeConnection()], gfs: new Error('x') })
    expect(container.querySelector('#sh-conn-gfs .sh-conn-error')!.textContent).toContain('connections.error.gfs_title')
    expect(container.textContent).not.toContain('connections.gfs.empty_title')
  })
})

describe('ConnectionsPage — member view', () => {
  beforeEach(() => setUser(false))

  it('shows no version data, no chevrons, no requests, no Advanced and calls no admin API', async () => {
    loadCompat.fn = async () => {
      compatSignals.compatOurs.value = 57
      compatSignals.compatPeers.value = [
        { instance_id: 'inst-1', proto_version: 50, capabilities_known: true, lacking_features: ['a'] },
      ]
    }
    const { container } = await renderPage({
      conns: [makeConnection()],
      requests: [{ request_id: 'r', from_a_display: 'G', via_b_display: 'B', received_at: '2026-06-01T00:00:00Z' }],
      gfs: [makeGfs()],
      users: [{ display_name: 'Dave', username: 'dave', is_admin: true }, { display_name: 'Mona', username: 'mona', is_admin: false }],
    })
    expect(adminCalls()).toEqual([])
    expect(container.querySelector('.sh-conn-chip--warn')).toBeNull()
    expect(container.querySelector('[data-s="warn"]')).toBeNull()
    expect(container.querySelector('.sh-conn-chev')).toBeNull()
    expect(container.querySelector('button.sh-conn-card')).toBeNull()
    expect(container.querySelector('.sh-conn-request')).toBeNull()
    expect(container.querySelector('.sh-conn-adv')).toBeNull()
    expect(container.querySelector('.sh-conn-more')).toBeNull()
    expect(container.textContent).not.toContain('your_version')
    expect(container.textContent).not.toContain('connections.hero.pair')
    await waitFor(() => {
      expect(container.querySelector('.sh-conn-hero__note')!.textContent).toContain('connections.hero.ask_oneDave')
    })
  })

  it('falls back to "Ask an admin" when the roster cannot be read', async () => {
    const { container } = await renderPage({ conns: [makeConnection()], users: new Error('x') as unknown as unknown[] })
    expect(container.querySelector('.sh-conn-hero__note')!.textContent).toContain('connections.hero.ask_any')
  })

  it('never sees pending pairings', async () => {
    const { container } = await renderPage({
      conns: [makeConnection(), makeConnection({ instance_id: 'z', display_name: 'Zeta', status: 'pending_sent' })],
    })
    expect(container.textContent).not.toContain('Zeta')
  })
})

describe('ConnectionsPage — pair requests', () => {
  const req = {
    request_id: 'req-1', from_a_id: 'g', from_a_display: 'Gamma House', via_b_id: 'b',
    via_b_display: 'Beta House', ts: '', received_at: '2026-06-01T08:00:00Z',
  }

  it('renders above the hero and approves in one click', async () => {
    const { container, getByText } = await renderPage({ conns: [makeConnection()], requests: [req] })
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).not.toBeNull())
    const order = [...container.querySelector('.sh-conn')!.children].map((c) => c.className)
    expect(order[0]).toContain('sh-conn-sect--tight')
    expect(order[1]).toContain('sh-conn-hero')
    expect(getByText('connections.requests.label_one(n=1)')).toBeTruthy()
    fireEvent.click(getByText('connections.requests.approve'))
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/pairing/auto-pair-requests/req-1/approve', {})
    })
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).toBeNull())
  })

  it('asks before declining', async () => {
    ;(confirmDialog as ReturnType<typeof vi.fn>).mockResolvedValue(false)
    const { container, getByText } = await renderPage({ conns: [makeConnection()], requests: [req] })
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).not.toBeNull())
    fireEvent.click(getByText('connections.requests.decline'))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('ConnectionsPage — pending pairing cancel', () => {
  const zeta = makeConnection({ instance_id: 'zeta', display_name: 'Zeta House', status: 'pending_sent', transport: null })

  async function openCancel() {
    const r = await renderPage({ conns: [makeConnection(), zeta] })
    fireEvent.click(r.getByRole('button', { name: 'connections.pending.cancel_label(name=Zeta House)' }))
    const dialog = await waitFor(() => r.getByRole('alertdialog'))
    expect(dialog.textContent).toContain('connections.pending.confirm_title(name=Zeta House)')
    return r
  }

  it('confirms, then DELETEs the pending row; the card disappears with a toast', async () => {
    const { getByText, container } = await openCancel()
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/pairing/pending/zeta'))
    await waitFor(() => expect(container.textContent).not.toContain('Zeta House'))
    expect(showToast).toHaveBeenCalledWith('connections.pending.cancelled(name=Zeta House)', 'info')
  })

  it('"Keep waiting" backs out without a request', async () => {
    const { getByText, queryByRole } = await openCancel()
    fireEvent.click(getByText('connections.pending.keep'))
    await waitFor(() => expect(queryByRole('alertdialog')).toBeNull())
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('PAIRING_CONFIRMED (confirmed meanwhile) reloads and reports it as a failed cancel', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('x'), { status: 409, code: 'PAIRING_CONFIRMED' }))
    const { getByText } = await openCancel()
    const before = apiGet.mock.calls.filter((c) => c[0] === '/api/connections').length
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('error.pairing_confirmed', 'error'))
    await waitFor(() => {
      expect(apiGet.mock.calls.filter((c) => c[0] === '/api/connections').length).toBeGreaterThan(before)
    })
  })

  it('404 (already gone) drops the card', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('nf'), { status: 404 }))
    serve({ conns: [makeConnection(), zeta] })
    const { getByText, container } = await openCancel()
    serve({ conns: [makeConnection()] })
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(container.textContent).not.toContain('Zeta House'))
  })

  it('"Start again" opens a fresh QR pairing', async () => {
    const { getByText } = await renderPage({ conns: [makeConnection(), zeta] })
    fireEvent.click(getByText('connections.pending.restart'))
    expect(openPairing).toHaveBeenCalledWith('household')
  })

  it('a 409 without that code is just a failed cancel', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('x'), { status: 409, code: 'SOMETHING_ELSE' }))
    const { getByText } = await openCancel()
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('connections.pending.cancel_failed', 'error'))
  })

  it('after a successful cancel focus moves to the Households heading', async () => {
    const { getByText } = await openCancel()
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(document.activeElement?.id).toBe('sh-conn-hh-title'))
    expect(document.activeElement!.getAttribute('tabindex')).toBe('-1')
  })

  it('a 200 with peer_notified=false says the other side may not know yet', async () => {
    apiDelete.mockResolvedValue({ ok: true, peer_notified: false })
    const { getByText } = await openCancel()
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('connections.pending.cancelled_unnotified(name=Zeta House)', 'info')
    })
  })

  it('a request through a trust relay has no one to tell — plain toast', async () => {
    apiDelete.mockResolvedValue({ ok: true, peer_notified: false })
    const r = await renderPage({ conns: [makeConnection(), { ...zeta, pending_kind: 'request' }] })
    fireEvent.click(r.getByRole('button', { name: 'connections.pending.cancel_label(name=Zeta House)' }))
    await waitFor(() => r.getByRole('alertdialog'))
    fireEvent.click(r.getByText('connections.pending.request_ok'))
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('connections.pending.cancelled(name=Zeta House)', 'info')
    })
    expect(showToast).not.toHaveBeenCalledWith(
      'connections.pending.cancelled_unnotified(name=Zeta House)', 'info',
    )
  })

  it('encodes the household id into the path (never a path of its own)', async () => {
    const r = await renderPage({
      conns: [makeConnection(), { ...zeta, instance_id: '../connections/victim' }],
    })
    fireEvent.click(r.getByRole('button', { name: 'connections.pending.cancel_label(name=Zeta House)' }))
    await waitFor(() => r.getByRole('alertdialog'))
    fireEvent.click(r.getByText('connections.pending.confirm_ok'))
    await waitFor(() => {
      expect(apiDelete).toHaveBeenCalledWith('/api/pairing/pending/..%2Fconnections%2Fvictim')
    })
  })

  it('a pending_received card offers Cancel too', async () => {
    const r = await renderPage({ conns: [makeConnection(), { ...zeta, status: 'pending_received' }] })
    expect(r.getByRole('button', { name: 'connections.pending.cancel_label(name=Zeta House)' })).toBeTruthy()
  })

  it('a confirmed household has no Cancel', async () => {
    const r = await renderPage({ conns: [makeConnection()] })
    expect(r.queryByText('connections.pending.cancel')).toBeNull()
  })

  it('a non-admin gets no pending card and no Cancel', async () => {
    setUser(false)
    const r = await renderPage({ conns: [makeConnection(), zeta] })
    expect(r.queryByText('connections.pending.cancel')).toBeNull()
    expect(r.container.querySelector('[data-pending-kind]')).toBeNull()
  })

  it('404 is quiet: the outcome the admin wanted, so no toast', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('nf'), { status: 404 }))
    const { getByText } = await openCancel()
    fireEvent.click(getByText('connections.pending.confirm_ok'))
    await waitFor(() => expect(apiDelete).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(showToast).not.toHaveBeenCalled()
  })

  it.each([
    ['code', 'connections.meta.waiting_code', 'connections.pending.confirm_title', 'connections.pending.confirm_ok', true],
    ['request', 'connections.meta.waiting_request', 'connections.pending.request_title', 'connections.pending.request_ok', false],
    ['approval', 'connections.meta.waiting_approval', 'connections.pending.approval_title', 'connections.pending.approval_ok', false],
  ])('pending_kind=%s: meta, confirm wording, and Start again only for a code', async (kind, meta, title, ok, restart) => {
    const row = { ...zeta, pending_kind: kind }
    const r = await renderPage({ conns: [makeConnection(), row] })
    expect(r.container.querySelector('[data-pending-kind]')!.textContent).toContain(meta)
    expect(!!r.queryByText('connections.pending.restart')).toBe(restart)
    fireEvent.click(r.getByRole('button', { name: 'connections.pending.cancel_label(name=Zeta House)' }))
    const dialog = await waitFor(() => r.getByRole('alertdialog'))
    expect(dialog.textContent).toContain(`${title}(name=Zeta House)`)
    expect(r.getByRole('button', { name: ok })).toBeTruthy()
  })
})

describe('ConnectionsPage — approving a pair request', () => {
  const req = {
    request_id: 'r/1', from_a_id: 'g', from_a_display: 'Gamma House', via_b_id: 'b',
    via_b_display: 'Beta House', ts: '', received_at: '2026-06-01T08:00:00Z',
  }

  it('approves (id URL-encoded) and says pairing completes when they confirm', async () => {
    const { container, getByText } = await renderPage({ conns: [makeConnection()], requests: [req] })
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).not.toBeNull())
    fireEvent.click(getByText('connections.requests.approve'))
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/pairing/auto-pair-requests/r%2F1/approve', {})
    })
    expect(showToast).toHaveBeenCalledWith('connections.requests.approved(name=Gamma House)', 'success')
  })

  it('410 (expired) shows the mapped message and drops the request', async () => {
    apiPost.mockRejectedValue(Object.assign(new Error('This pair request has expired.'), { status: 410 }))
    const { container, getByText } = await renderPage({ conns: [makeConnection()], requests: [req] })
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).not.toBeNull())
    serve({ conns: [makeConnection()], requests: [] })
    fireEvent.click(getByText('connections.requests.approve'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('This pair request has expired.', 'error'))
    await waitFor(() => expect(container.querySelector('.sh-conn-request')).toBeNull())
  })
})

describe('ConnectionsPage — space-only households', () => {
  const space = makeConnection({ instance_id: 'sp', display_name: 'Lindqvist', source: 'space_session', transport: 'gfs_relay' })

  it('labels the row "Space only", explains it, and offers no Manage', async () => {
    const { container, getByTestId } = await renderPage({ conns: [makeConnection(), space] })
    expect(getByTestId('space-only-note-sp').textContent).toBe('connections.meta.shares_space')
    const row = getByTestId('space-only-note-sp').closest('.sh-conn-card')!
    expect(row.tagName).toBe('DIV')
    expect(row.textContent).toContain('connections.type.space_only')
    expect(container.querySelector('#sh-conn-card-sp')).toBeNull()
  })

  it('is never offered as a pairing voucher', async () => {
    const { getByText } = await renderPage({ conns: [makeConnection(), space] })
    fireEvent.click(getByText('connections.hero.via_long'))
    expect(openAutoPair).toHaveBeenCalledWith([{ instance_id: 'inst-1', display_name: 'Household Alpha' }])
  })
})

describe('ConnectionsPage — map view', () => {
  it('toggles with aria-pressed, keeps the hero above, and names households not on the map', async () => {
    const { getByRole, container, getByTestId } = await renderPage({
      conns: [makeConnection({ home_lat: 1, home_lon: 2 }), makeConnection({ instance_id: 'b', display_name: 'Beta' })],
    })
    const list = getByRole('button', { name: 'connections.view_list' })
    const map = getByRole('button', { name: 'connections.view_map' })
    expect(list.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(map)
    await waitFor(() => expect(getByTestId('map-stub')).toBeTruthy())
    expect(map.getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.sh-conn-hero')).not.toBeNull()
    expect(mapProps[mapProps.length - 1].showOffMapFooter).toBe(false)
    expect(container.querySelector('.sh-conn-map__note')!.textContent).toContain('connections.map.not_shown_oneBeta')
    fireEvent.click(getByRole('button', { name: 'connections.map.show_list' }))
    await waitFor(() => expect(container.querySelector('.sh-conn-list')).not.toBeNull())
  })

  it('Manage from the map only for household peers, and only for admins', async () => {
    const { getByRole } = await renderPage({ conns: [makeConnection()] })
    fireEvent.click(getByRole('button', { name: 'connections.view_map' }))
    await waitFor(() => expect(mapProps.length).toBeGreaterThan(0))
    const props = mapProps[mapProps.length - 1]
    expect(props.canManage!(makeConnection())).toBe(true)
    expect(props.canManage!(makeConnection({ source: 'space_session' }))).toBe(false)
    expect(props.canManage!(makeConnection({ status: 'pending_sent' }))).toBe(false)
  })

  it('pins the rows the page lists — a member never gets a pending pin', async () => {
    setUser(false)
    const { getByRole } = await renderPage({
      conns: [makeConnection(), makeConnection({ instance_id: 'z', display_name: 'Zeta', status: 'pending_sent' })],
    })
    fireEvent.click(getByRole('button', { name: 'connections.view_map' }))
    await waitFor(() => expect(mapProps.length).toBeGreaterThan(0))
    const rows = (mapProps[mapProps.length - 1] as { rows?: Array<{ instance_id: string }> }).rows!
    expect(rows.map((r) => r.instance_id)).toEqual(['inst-1'])
  })

  it('a member gets no Manage hook from the map', async () => {
    setUser(false)
    const { getByRole } = await renderPage({ conns: [makeConnection()] })
    fireEvent.click(getByRole('button', { name: 'connections.view_map' }))
    await waitFor(() => expect(mapProps.length).toBeGreaterThan(0))
    expect(mapProps[mapProps.length - 1].onManage).toBeUndefined()
  })
})

describe('ConnectionsPage — GFS rows', () => {
  async function stateFor(over: Record<string, unknown>) {
    const { container } = await renderPage({ conns: [makeConnection()], gfs: [makeGfs(over)] })
    return container.querySelector('[data-gfs-state]')!
  }

  it.each([
    [{ status: 'pending' }, 'gfs.status_pending', 'pending'],
    [{ status: 'suspended' }, 'gfs.status_suspended', 'down'],
    [{ connected: true }, 'gfs.status_connected', 'ok'],
    [{ connected: false, last_error: 'unknown-instance' }, 'gfs.status_repair_needed', 'down'],
    [{ connected: false, last_error: 'bad-signature' }, 'gfs.status_repair_needed', 'down'],
    [{ connected: false, last_error: 'auth-failed' }, 'gfs.status_repair_needed', 'down'],
    [{ connected: false, last_error: 'ts-skew' }, 'gfs.status_clock_skew', 'down'],
    [{ connected: false, last_error: 'hello-timeout' }, 'gfs.status_reconnecting', 'pending'],
    [{ connected: false, last_error: null }, 'gfs.status_reconnecting', 'pending'],
  ])('%o → %s', async (over, label, status) => {
    const el = await stateFor(over)
    expect(el.textContent).toBe(label)
    expect(el.getAttribute('data-gfs-state')).toBe(status)
  })

  it('never renders a GFS-controlled last_error verbatim', async () => {
    const { container } = await renderPage({
      conns: [makeConnection()], gfs: [makeGfs({ connected: false, last_error: '<img src=x onerror=alert(1)>' })],
    })
    expect(container.textContent).not.toContain('<img')
  })

  it('shows the host in mono with the full URL as a title', async () => {
    const { container } = await renderPage({ conns: [makeConnection()], gfs: [makeGfs()] })
    const mono = container.querySelector('.sh-conn-mono')!
    expect(mono.textContent).toBe('gfs.example.com:8443')
    expect(mono.getAttribute('title')).toBe('https://gfs.example.com:8443/inbox/x')
  })

  it('the ⋯ menu copies the address and disconnects after confirming', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { getByRole, getByTestId, container } = await renderPage({ conns: [makeConnection()], gfs: [makeGfs()] })
    const more = getByRole('button', { name: 'connections.gfs.more(name=Town GFS)' })
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    await waitFor(() => expect(document.activeElement?.getAttribute('role')).toBe('menuitem'))
    fireEvent.click(getByRole('menuitem', { name: 'connections.gfs.copy' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://gfs.example.com:8443'))

    fireEvent.click(more)
    fireEvent.click(getByRole('menuitem', { name: 'gfs.disconnect' }))
    fireEvent.click(getByTestId('gfs-disconnect-confirm'))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gfs/connections/gfs-1'))
    await waitFor(() => expect(container.querySelector('.sh-conn-more')).toBeNull())
  })

  it('Esc closes the ⋯ menu and returns focus to the button', async () => {
    const { getByRole, queryByRole } = await renderPage({ conns: [makeConnection()], gfs: [makeGfs()] })
    const more = getByRole('button', { name: 'connections.gfs.more(name=Town GFS)' })
    fireEvent.click(more)
    const menu = await waitFor(() => getByRole('menu'))
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(more)
  })
})

describe('ConnectionsPage — Advanced (admin)', () => {
  it('standalone: External address row with what is in effect, saved trimmed', async () => {
    const { container, getByLabelText, getByText } = await renderPage({
      conns: [makeConnection()],
      ext: { base: null, effective: 'https://home.example.com', source: 'auto', manual_supported: true },
    })
    await waitFor(() => expect(container.querySelector('#sh-conn-adv-ext')).not.toBeNull())
    const summary = container.querySelector('#sh-conn-adv-ext > summary')!
    expect(summary.textContent).toContain('connections.adv.ext_desc_auto(host=home.example.com)')
    expect(summary.textContent).toContain('connections.adv.chip_from_setup')
    expect(summary.getAttribute('aria-expanded')).toBe('false')

    serve({ conns: [makeConnection()], ext: { base: 'https://x.example', effective: 'https://x.example', source: 'manual' } })
    apiPut.mockResolvedValue({ base: 'https://x.example', changed: true, peers_notified: 2 })
    fireEvent.input(getByLabelText('connections.ext.label'), { target: { value: '  https://x.example  ' } })
    await waitFor(() => expect((getByText('common.save') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(getByText('common.save'))
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith('/api/admin/federation/external-url', { base: 'https://x.example' })
    })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('connections.ext.saved_notified(n=2)', 'success'))
  })

  it('an empty field clears the stored address', async () => {
    const { getByLabelText, getByText } = await renderPage({
      conns: [makeConnection()],
      ext: { base: 'https://x.example', effective: 'https://x.example', source: 'manual' },
    })
    await waitFor(() => expect((getByLabelText('connections.ext.label') as HTMLInputElement).value).toBe('https://x.example'))
    apiPut.mockResolvedValue({ base: null, changed: true, peers_notified: 0 })
    fireEvent.input(getByLabelText('connections.ext.label'), { target: { value: '' } })
    await waitFor(() => expect((getByText('common.save') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(getByText('common.save'))
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith('/api/admin/federation/external-url', { base: null })
    })
  })

  it.each([
    [{ servers: [], has_turn: false, turn_usable: false, pulls_from_home_assistant: false }, 'connections.adv.relay_direct_only'],
    [{ servers: [], has_turn: true, turn_usable: false, pulls_from_home_assistant: false }, 'connections.adv.relay_broken'],
    [{ servers: [], has_turn: true, turn_usable: true, pulls_from_home_assistant: true }, 'connections.adv.relay_ready'],
    [new Error('x'), 'connections.adv.relay_unavailable'],
  ])('relay chip is visible before opening: %o', async (ice, chip) => {
    const { container } = await renderPage({ conns: [makeConnection()], ice })
    await waitFor(() => {
      expect(container.querySelector('#sh-conn-adv-relay > summary')!.textContent).toContain(chip)
    })
  })

  it('diagnostics downloads through the api client as a timestamped file', async () => {
    const created: unknown[] = []
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = ((b: unknown) => { created.push(b); return 'blob:fake' }) as typeof URL.createObjectURL
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL
    const clicks: string[] = []
    const origClick = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) { clicks.push(this.download) }
    try {
      const { getByText, container } = await renderPage({
        conns: [makeConnection()], extra: { '/api/admin/diagnostics': { schema: 1 } },
      })
      expect(container.textContent).toContain('connections.diag.note')
      fireEvent.click(getByText('connections.diag.download'))
      await waitFor(() => expect(clicks.length).toBe(1))
      expect(apiGet).toHaveBeenCalledWith('/api/admin/diagnostics')
      expect(created).toHaveLength(1)
      expect(clicks[0]).toMatch(/^socialhome-diagnostics-.*\.json$/)
    } finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
      HTMLAnchorElement.prototype.click = origClick
    }
  })

  it('sits last on the page', async () => {
    const { container } = await renderPage({ conns: [makeConnection()] })
    const kids = [...container.querySelector('.sh-conn')!.children]
    const adv = kids.findIndex((k) => k.querySelector('.sh-conn-adv'))
    const gfs = kids.findIndex((k) => k.id === 'sh-conn-gfs')
    expect(adv).toBeGreaterThan(gfs)
  })
})

describe('ConnectionsPage — Home Assistant App mode', () => {
  beforeEach(() => setMode('haos'))

  it('hides the External address row but keeps relay + diagnostics', async () => {
    const { container } = await renderPage({ conns: [makeConnection()] })
    await waitFor(() => expect(externalUrl.value.loaded).toBe(true))
    expect(container.querySelector('#sh-conn-adv-ext')).toBeNull()
    expect(container.querySelector('#sh-conn-adv-relay')).not.toBeNull()
    expect(container.querySelector('#sh-conn-adv-diag')).not.toBeNull()
  })

  it('the setup warning never links to the hidden External address row', async () => {
    const { container, queryByText } = await renderPage({
      conns: [], ext: { base: null, effective: null, source: null, manual_supported: false },
    })
    await waitFor(() => expect(container.querySelector('.sh-conn-hero__warn')).not.toBeNull())
    expect(queryByText('connections.hero.setup_warn_ext')).toBeNull()
    expect(queryByText('connections.hero.setup_warn_gfs_cap')).not.toBeNull()
  })
})

describe('ConnectionsPage — server refuses a manual address', () => {
  it('hides the row and the link when manual_supported is false', async () => {
    const { container, queryByText } = await renderPage({
      conns: [], ext: { base: null, effective: null, source: null, manual_supported: false },
    })
    await waitFor(() => expect(container.querySelector('.sh-conn-hero__warn')).not.toBeNull())
    expect(container.querySelector('#sh-conn-adv-ext')).toBeNull()
    expect(queryByText('connections.hero.setup_warn_ext')).toBeNull()
  })
})
