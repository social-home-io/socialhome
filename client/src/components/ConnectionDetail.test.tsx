import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, cleanup, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    post: vi.fn(),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

const resyncPeerCapabilities = vi.fn().mockResolvedValue(undefined)
const loadFederationCompat = vi.fn().mockResolvedValue(undefined)
vi.mock('@/store/federationCompat', () => ({
  resyncPeerCapabilities: (...a: unknown[]) => resyncPeerCapabilities(...a),
  loadFederationCompat: (...a: unknown[]) => loadFederationCompat(...a),
  peerSupportsResync: (p: { capabilities_known: boolean; lacking_features: string[] }) =>
    p.capabilities_known && !p.lacking_features.includes('Instance resync request'),
}))

vi.mock('./ShareHomeToggle', () => ({
  ShareHomeToggle: ({ instanceId, peerName, initialValue }: {
    instanceId: string; peerName: string; initialValue: boolean
  }) => (
    <div data-testid="share-home-toggle"
         data-instance-id={instanceId}
         data-peer-name={peerName}
         data-initial-value={String(initialValue)} />
  ),
}))

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue({ users: [] })
  apiPatch.mockReset().mockResolvedValue({})
  apiDelete.mockReset().mockResolvedValue({})
  cleanup()
})

const _conn = (over: Partial<Record<string, unknown>> = {}) => ({
  instance_id: 'z7k63zfi',
  display_name: 'z7k63zfi',
  federated_display_name: 'z7k63zfi',
  local_alias: null,
  status: 'confirmed',
  inbox_url: 'https://x/wh/abc',
  intro_relay_enabled: true,
  unreachable_since: null,
  paired_at: '2026-05-18T10:00:00+00:00',
  ...over,
})

describe('ConnectionDetail — alias rename row', () => {
  it('module exports exist', async () => {
    const mod = await import('./ConnectionDetail')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('shows the federated name in the placeholder and hint when no alias is set', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    expect(input.placeholder).toBe('z7k63zfi')
    // Hint mentions the peer's advertised name when no alias is set.
    const hint = document.querySelector('.sh-connection-alias__hint')
    expect(hint?.textContent).toMatch(/They advertise themselves as "z7k63zfi"/)
  })

  it('Save button is disabled until the alias differs from the persisted value', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ local_alias: 'Brother' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const save = (await screen.findAllByText('Save'))[0] as HTMLButtonElement
    expect(save.disabled).toBe(true)
    const input = screen.getByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Brother\'s house' } })
    expect(save.disabled).toBe(false)
  })

  it('Save PATCHes /api/pairing/connections/{id}/alias with the new value', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    const onAliasSaved = vi.fn()
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
        onAliasSaved={onAliasSaved}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: "Brother's house" } })
    fireEvent.click((screen.getAllByText('Save'))[0])
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: "Brother's house" },
    )
    expect(onAliasSaved).toHaveBeenCalledTimes(1)
  })

  it('whitespace-only alias clears (posts null)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ local_alias: 'OldName' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: '   ' } })
    fireEvent.click((screen.getAllByText('Save'))[0])
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: null },
    )
  })

  it('Enter key submits the alias too', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Mom' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: 'Mom' },
    )
  })
})

describe('Transport row', () => {
  it('shows Direct (WebRTC DataChannel) for rtc', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'rtc' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(/Direct \(WebRTC DataChannel\)/i)).toBeTruthy()
  })

  it('shows HTTPS inbox (fallback) for https', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(/HTTPS inbox \(fallback\)/i)).toBeTruthy()
  })

  it('omits the Transport row when transport is null', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: null }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText(/Transport/i)).toBeNull()
  })
})

describe('Protocol version row', () => {
  it('shows the peer protocol version when present', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ proto_version: 19 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Protocol version')).toBeTruthy()
    expect(screen.getByText('v19')).toBeTruthy()
  })

  it('omits the Protocol version row when the field is absent', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Protocol version')).toBeNull()
  })
})

describe('Federation compatibility row + re-check', () => {
  const _compat = (over: Partial<Record<string, unknown>> = {}) => ({
    instance_id: 'z7k63zfi',
    display_name: 'z7k63zfi',
    proto_version: 15,
    status: 'confirmed',
    last_reachable_at: null,
    capabilities_known: true,
    lacking_features: ['Bazaar bids', 'Calendar overrides'],
    ...over,
  })

  beforeEach(() => {
    resyncPeerCapabilities.mockClear()
    loadFederationCompat.mockClear()
  })

  it('shows the Missing features row when the peer lacks features', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ proto_version: 15 }) as any}
        compat={_compat() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(await screen.findByText('Missing features')).toBeTruthy()
    expect(screen.getByText('Bazaar bids, Calendar overrides')).toBeTruthy()
  })

  it('shows "up to date ✓" when caps known and nothing lacking', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        compat={_compat({ lacking_features: [] }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(await screen.findByText('Compatibility')).toBeTruthy()
    expect(screen.getByText('up to date ✓')).toBeTruthy()
  })

  it('renders a "Re-check version" button that calls resyncPeerCapabilities', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        compat={_compat() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const btn = await screen.findByText('Re-check version')
    fireEvent.click(btn)
    await new Promise(r => setTimeout(r, 0))
    expect(resyncPeerCapabilities).toHaveBeenCalledWith('z7k63zfi')
  })

  it('hides the Re-check button when the peer cannot honor a resync request', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        compat={_compat({ lacking_features: ['Instance resync request'] }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await screen.findByText('Missing features')
    expect(screen.queryByText('Re-check version')).toBeNull()
  })

  it('renders no compat row when compat prop is absent', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await screen.findByLabelText('Display this household as')
    expect(screen.queryByText('Missing features')).toBeNull()
    expect(screen.queryByText('Compatibility')).toBeNull()
    expect(screen.queryByText('Re-check version')).toBeNull()
  })
})

describe('ShareHomeToggle integration', () => {
  it('renders ShareHomeToggle with correct props when share_home is true', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ share_home: true }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const toggle = await screen.findByTestId('share-home-toggle')
    expect(toggle).toBeTruthy()
    expect(toggle.getAttribute('data-instance-id')).toBe('z7k63zfi')
    expect(toggle.getAttribute('data-peer-name')).toBe('z7k63zfi')
    expect(toggle.getAttribute('data-initial-value')).toBe('true')
  })

  it('passes share_home=false to ShareHomeToggle when disabled', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ share_home: false }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const toggle = await screen.findByTestId('share-home-toggle')
    expect(toggle.getAttribute('data-initial-value')).toBe('false')
  })

  it('defaults share_home to true when field is absent (old API response)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    const connWithoutShareHome = _conn()
    delete (connWithoutShareHome as any).share_home
    render(
      <ConnectionDetail
        conn={connWithoutShareHome as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const toggle = await screen.findByTestId('share-home-toggle')
    expect(toggle.getAttribute('data-initial-value')).toBe('true')
  })
})

describe('DM path row', () => {
  it('renders when /transport-detail returns a recent relay', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: { via: 'peer-relay', ts: '2026-05-19T07:30:00+00:00' } })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await waitFor(() => {
      expect(screen.getByText(/Last DM took the relay path/i)).toBeTruthy()
      expect(screen.getByText(/peer-relay/)).toBeTruthy()
    })
  })

  it('does not render when /transport-detail returns null', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: null })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'rtc' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await waitFor(() =>
      expect(screen.queryByText(/Last DM took the relay path/i)).toBeNull(),
    )
  })

  it('hides the DM path row silently on fetch error', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.reject(new Error('Network error'))
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    // The Transport row should still render — proves the panel didn't crash:
    expect(screen.getByText(/HTTPS inbox \(fallback\)/i)).toBeTruthy()
    expect(screen.queryByText(/Last DM took the relay path/i)).toBeNull()
  })
})

describe('Waiting to send — queued envelope backlog', () => {
  it('renders the backlog line when envelopes are queued', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 56 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Waiting to send')).toBeTruthy()
    expect(screen.getByText('56 messages queued for delivery')).toBeTruthy()
  })

  it('uses the singular for exactly one queued envelope', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 1 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('1 message queued for delivery')).toBeTruthy()
  })

  it('is absent when nothing is queued', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 0 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Waiting to send')).toBeNull()
  })

  it('is absent when the field is missing entirely (older API)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Waiting to send')).toBeNull()
  })
})

describe('ConnectionDetail — naive-UTC timestamps render in the viewer\'s zone', () => {
  // ``last_reachable_at`` / ``unreachable_since`` come from SQLite
  // ``datetime('now')`` as the naive shape "YYYY-MM-DD HH:MM:SS" — a UTC
  // value with no zone designator, which ``new Date()`` parses as LOCAL
  // time. Rendering it raw skewed the absolute stamp by the viewer's UTC
  // offset (2 h for a CEST household). These assert the component routes
  // both through ``normaliseTimestamp`` first. The suite otherwise runs
  // under TZ=UTC, where the bug is invisible — so pin a positive offset.
  const realTz = process.env.TZ

  beforeEach(() => { process.env.TZ = 'Europe/Zurich' })
  afterEach(() => {
    if (realTz === undefined) delete process.env.TZ
    else process.env.TZ = realTz
  })

  const utcStamp = (naive: string) =>
    new Date(naive.replace(' ', 'T') + 'Z').toLocaleString()

  it('renders last_reachable_at as UTC, not as local wall-clock', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ last_reachable_at: '2026-06-04 11:26:45' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    // 11:26:45Z is 13:26:45 in Zurich — the naive misparse would show 11:26:45.
    expect(screen.getByText(utcStamp('2026-06-04 11:26:45'), { exact: false })).toBeTruthy()
    expect(screen.queryByText(/11:26:45/)).toBeNull()
  })

  it('renders unreachable_since as UTC, not as local wall-clock', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ unreachable_since: '2026-06-04 11:43:24' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(utcStamp('2026-06-04 11:43:24'), { exact: false })).toBeTruthy()
    expect(screen.queryByText(/11:43:24/)).toBeNull()
  })
})

describe('Undelivered — dropped envelope count', () => {
  it('renders the dropped line when envelopes were given up on', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 56, dropped_envelopes: 263 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Undelivered')).toBeTruthy()
    expect(
      screen.getByText('263 messages could not be delivered and were dropped.'),
    ).toBeTruthy()
  })

  it('uses the singular for exactly one dropped envelope', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ dropped_envelopes: 1 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(
      screen.getByText('1 message could not be delivered and was dropped.'),
    ).toBeTruthy()
  })

  it('is absent when nothing was dropped', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ dropped_envelopes: 0 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Undelivered')).toBeNull()
  })

  it('is absent when the field is missing entirely (older API)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Undelivered')).toBeNull()
  })

  it('renders without the queued line when only envelopes were dropped', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 0, dropped_envelopes: 4 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Undelivered')).toBeTruthy()
    expect(screen.queryByText('Waiting to send')).toBeNull()
  })

  it('leaves the queued line alone when nothing was dropped', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ queued_envelopes: 7, dropped_envelopes: 0 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Waiting to send')).toBeTruthy()
    expect(screen.queryByText('Undelivered')).toBeNull()
  })
})
