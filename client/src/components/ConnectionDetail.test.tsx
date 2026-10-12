import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, cleanup, waitFor } from '@testing-library/preact'
import { setLocale } from '@/i18n/i18n'

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
  peerSupportsResync: (p: { capabilities_known: boolean; lacking_feature_keys?: string[] }) =>
    p.capabilities_known && !(p.lacking_feature_keys ?? []).includes('household_resync'),
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
  unreachable_since: null,
  paired_at: '2026-05-18T10:00:00+00:00',
  ...over,
})

describe('ConnectionDetail — header + inline rename', () => {
  it('module exports exist', async () => {
    const mod = await import('./ConnectionDetail')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  async function open(over: Partial<Record<string, unknown>> = {}, extra: Record<string, unknown> = {}) {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    const onClose = vi.fn()
    const onAliasSaved = vi.fn()
    const r = render(
      <ConnectionDetail
        conn={_conn(over) as any}
        onClose={onClose}
        onRevoke={() => {}}
        onAliasSaved={onAliasSaved}
        {...extra}
      />,
    )
    return { ...r, onClose, onAliasSaved }
  }

  async function startRename() {
    fireEvent.click(await screen.findByRole('button', { name: 'Rename' }))
    return await screen.findByLabelText('Name on your side') as HTMLInputElement
  }

  it('shows the name as the dialog title, a 44 px Rename pencil and a status line', async () => {
    await open({ display_name: 'Alpha House', transport: 'rtc' })
    const dialog = screen.getByRole('dialog')
    expect(dialog.classList.contains('sh-conn-manage')).toBe(true)
    const titleId = dialog.getAttribute('aria-labelledby')!
    expect(document.getElementById(titleId)!.textContent).toBe('Alpha House')
    expect(screen.getByRole('button', { name: 'Rename' }).className).toContain('sh-conn-iconbtn')
    expect(screen.getByText('Connected · Direct')).toBeTruthy()
  })

  it('an unreachable household says since when in the status line', async () => {
    await open({ unreachable_since: '2026-06-01T09:00:00Z' })
    expect(document.querySelector('.sh-conn-manage__down')!.textContent).toMatch(/^Unreachable since /)
  })

  it('rename swaps the × for ✓ + Cancel and prefills the current name', async () => {
    await open({ display_name: 'Alpha House', federated_display_name: 'Alpha House' })
    const input = await startRename()
    expect(input.value).toBe('Alpha House')
    expect(document.activeElement).toBe(input)
    expect(screen.queryByRole('button', { name: 'Close dialog' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Save name' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(screen.getByText(/They call themselves "Alpha House"/)).toBeTruthy()
  })

  it('✓ PATCHes the alias (id URL-encoded), keeps the sheet open and shows the new name', async () => {
    const { onAliasSaved, onClose } = await open({ instance_id: 'a/b c', display_name: 'z7k63zfi' })
    const input = await startRename()
    fireEvent.input(input, { target: { value: "  Brother's house  " } })
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/pairing/connections/a%2Fb%20c/alias', { alias: "Brother's house" })
    })
    await waitFor(() => expect(onAliasSaved).toHaveBeenCalled())
    expect(onClose).not.toHaveBeenCalled()
    await waitFor(() => {
      const titleId = screen.getByRole('dialog').getAttribute('aria-labelledby')!
      expect(document.getElementById(titleId)!.textContent).toBe("Brother's house")
    })
    expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy()
  })

  it('an empty name, or the household\'s own name, clears the alias (null)', async () => {
    await open({ local_alias: 'Brother', display_name: 'Brother', federated_display_name: 'z7k63zfi' })
    let input = await startRename()
    fireEvent.input(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/pairing/connections/z7k63zfi/alias', { alias: null })
    })
    apiPatch.mockClear()
    cleanup()
    await open({ local_alias: 'Brother', display_name: 'Brother', federated_display_name: 'z7k63zfi' })
    input = await startRename()
    fireEvent.input(input, { target: { value: 'z7k63zfi' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith('/api/pairing/connections/z7k63zfi/alias', { alias: null })
    })
  })

  it('an unchanged name saves nothing and just leaves edit mode', async () => {
    await open()
    await startRename()
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy())
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('Esc while renaming discards the edit only — the sheet stays, focus returns to the pencil', async () => {
    const { onClose } = await open()
    const input = await startRename()
    fireEvent.input(input, { target: { value: 'Something else' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy())
    expect(onClose).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Rename' })))
    // A second Esc closes the sheet.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('Cancel discards the edit too', async () => {
    await open()
    const input = await startRename()
    fireEvent.input(input, { target: { value: 'Something else' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy())
    expect(apiPatch).not.toHaveBeenCalled()
  })
})

describe('ConnectionDetail — technical details + danger zone', () => {
  it('Technical details start collapsed and copy the ID and address', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: null, inbox_url: 'https://alpha.example/federation/inbox/x' })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    const details = document.querySelector('details.sh-conn-tech') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Copy household ID' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('z7k63zfi'))
    fireEvent.click(await screen.findByRole('button', { name: 'Copy address' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://alpha.example/federation/inbox/x'))
  })

  it('Unpair asks first, then DELETEs with the id URL-encoded', async () => {
    const onRevoke = vi.fn()
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail conn={_conn({ instance_id: 'a/b', display_name: 'Alpha House' }) as any}
        onClose={() => {}} onRevoke={onRevoke} />,
    )
    const danger = document.querySelector('.sh-conn-danger')!
    expect(danger.textContent).toContain('Danger zone')
    fireEvent.click(screen.getByRole('button', { name: 'Unpair Alpha House' }))
    expect(apiDelete).not.toHaveBeenCalled()
    expect(await screen.findByText('Remove Alpha House?')).toBeTruthy()
    expect(screen.getByText(/You'd need to pair again to undo this/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Unpair' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/pairing/connections/a%2Fb'))
    await waitFor(() => expect(onRevoke).toHaveBeenCalled())
  })
})

describe('ConnectionDetail — structure + status rule', () => {
  it('the Technical <dl> holds only dt/dd groups; Check version again sits after it', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail conn={_conn() as any}
        compat={{ instance_id: 'z7k63zfi', display_name: 'z', proto_version: 57, status: 'confirmed',
          last_reachable_at: null, capabilities_known: true, lacking_features: [] } as any}
        onClose={() => {}} onRevoke={() => {}} />,
    )
    const dl = document.querySelector('.sh-conn-tech dl')!
    for (const group of dl.children) {
      expect(group.tagName).toBe('DIV')
      expect([...group.children].map((c) => c.tagName)).toEqual(['DT', 'DD'])
    }
    const btn = screen.getByRole('button', { name: 'Check version again' })
    expect(dl.contains(btn)).toBe(false)
  })

  it('uses the live reachable flag like the list (unreachable_since alone is stale)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail conn={_conn({ reachable: true, unreachable_since: '2026-06-01T09:00:00Z', transport: 'rtc' }) as any}
        onClose={() => {}} onRevoke={() => {}} />,
    )
    expect(document.querySelector('.sh-conn-manage__down')).toBeNull()
    expect(screen.getByText('Connected · Direct')).toBeTruthy()
    expect(document.querySelector('.sh-modal-header .sh-conn-av')!.getAttribute('data-s')).toBe('ok')
  })
})

describe('App version tile', () => {
  it.each([
    [57, 57, 'v57, same as you'],
    [55, 57, 'v55, 2 behind'],
    [56, 57, 'v56, 1 behind'],
    [58, 57, 'v58, newer than yours'],
    [55, 0, 'v55'],
  ])('peer v%i, ours v%i → %s', async (peer, ours, text) => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ proto_version: peer }) as any}
        compat={{ instance_id: 'z7k63zfi', display_name: 'z', proto_version: peer, status: 'confirmed', last_reachable_at: null, capabilities_known: true, lacking_features: [] } as any}
        ours={ours}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(text)).toBeTruthy()
  })
})

describe('Transport row', () => {
  it('shows Direct for rtc', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'rtc' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(/^Direct$/)).toBeTruthy()
  })

  it('shows the slower internet path for https', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Over the internet')).toBeTruthy()
  })

  it('omits the Connection row when transport is null', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: null }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('Connection')).toBeNull()
  })
})

describe('App version row', () => {
  it('shows the peer protocol version when present', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ proto_version: 19 }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('App version')).toBeTruthy()
    expect(screen.getByText('v19')).toBeTruthy()
  })

  it('omits the App version row when the field is absent', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText('App version')).toBeNull()
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
    lacking_features: ['Bids and offers in the bazaar', 'Calendar overrides'],
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
    expect(screen.getByText('Bids and offers in the bazaar, Calendar overrides')).toBeTruthy()
  })

  describe('in German', () => {
    beforeEach(async () => { await setLocale('de') })
    afterEach(async () => { await setLocale('en') })

    it('lists the missing features in German, by their stable keys', async () => {
      const { ConnectionDetail } = await import('./ConnectionDetail')
      render(
        <ConnectionDetail
          conn={_conn({ proto_version: 12 }) as any}
          compat={_compat({
            proto_version: 12,
            lacking_features: [
              'Syncing when a direct link fails',
              'Space moderators',
              'A feature from a newer server',
            ],
            lacking_feature_keys: ['sync_https_fallback', 'space_moderators', 'from_the_future'],
          }) as any}
          onClose={() => {}}
          onRevoke={() => {}}
        />,
      )
      expect(await screen.findByText('Fehlende Funktionen')).toBeTruthy()
      // A slug this build doesn't know yet keeps the server's English label.
      expect(screen.getByText(
        'Abgleich, wenn die direkte Verbindung ausfällt, Moderatoren in Räumen, A feature from a newer server',
      )).toBeTruthy()
    })
  })

  it('renders a "Check version again" button that calls resyncPeerCapabilities', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        compat={_compat() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const btn = await screen.findByText('Check version again')
    fireEvent.click(btn)
    await new Promise(r => setTimeout(r, 0))
    expect(resyncPeerCapabilities).toHaveBeenCalledWith('z7k63zfi')
  })

  it('hides the Re-check button when the peer cannot honor a resync request', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        compat={_compat({
          lacking_features: ['Asking a household to send updates again'],
          lacking_feature_keys: ['household_resync'],
        }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await screen.findByText('Missing features')
    expect(screen.queryByText('Check version again')).toBeNull()
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
    await screen.findByRole('button', { name: 'Rename' })
    expect(screen.queryByText('Missing features')).toBeNull()
    expect(screen.queryByText('Check version again')).toBeNull()
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
      expect(screen.getByText(/Your last chat went through peer-relay/)).toBeTruthy()
      expect(screen.getByText(/You → peer-relay →/)).toBeTruthy()
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
      expect(screen.queryByText(/Your last chat went through/i)).toBeNull(),
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
    expect(screen.getByText('Over the internet')).toBeTruthy()
    expect(screen.queryByText(/Your last chat went through/i)).toBeNull()
  })
})

describe('Address row — admin-only, confirmed direct peers only', () => {
  it('shows the inbox_url /transport-detail returns', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/pairing/connections/z7k63zfi/transport-detail') {
        return Promise.resolve({ last_relay: null, inbox_url: 'https://peer.example/federation/inbox/wh-1' })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    expect(await screen.findByText('https://peer.example/federation/inbox/wh-1')).toBeTruthy()
    expect(screen.getByText('Address')).toBeTruthy()
  })

  it('hides the row when the server withholds the address (space_session / relay-only / non-admin)', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: null, inbox_url: null })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/transport-detail',
    ))
    expect(screen.queryByText('Address')).toBeNull()
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
    expect(screen.getByText('56 messages waiting to be sent')).toBeTruthy()
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
    expect(screen.getByText('1 message waiting to be sent')).toBeTruthy()
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
    // The tile shows the relative time; the absolute stamp is its title.
    expect(document.querySelector(`[title="${utcStamp('2026-06-04 11:26:45')}"]`)).not.toBeNull()
    expect(document.querySelector('[title*="11:26:45"]')).toBeNull()
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

describe('GFS row — relay acceptance is not delivery', () => {
  it('shows relay-only and the last acceptance time for a relay-only peer', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({
          last_reachable_at: null,
          last_relay_accepted_at: '2026-09-20 10:00:00',
          relay_only: true,
        }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('GFS only')).toBeTruthy()
    const stamp = new Date('2026-09-20T10:00:00Z').toLocaleString()
    expect(screen.getByText(`Last handed over ${stamp}`, { exact: false })).toBeTruthy()
    expect(screen.getByText(/aren't confirmed as delivered/i)).toBeTruthy()
  })

  it('shows the acceptance time without the relay-only chip once delivery is proven', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({
          last_reachable_at: '2026-09-20 11:00:00',
          last_relay_accepted_at: '2026-09-20 10:00:00',
          relay_only: false,
        }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('GFS')).toBeTruthy()
    expect(screen.queryByText('GFS only')).toBeNull()
  })

  it('is absent when the relay never accepted anything (or older API)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />,
    )
    expect(screen.queryByText('GFS')).toBeNull()
  })
})

// Every write this panel makes must hit a route the backend registers. It
// used to carry an "Allow introduced pairing" checkbox that PATCHed
// ``/connections/{id}/settings`` — a route that never existed, so the toggle
// 404'd and the flag it claimed to set was never read by any code path.
describe('ConnectionDetail — writes only target registered routes', () => {
  const _users = [
    { user_id: 'u-anna', username: 'anna', display_name: 'Anna', is_admin: false, visible: true },
  ]

  it('has no "introduced pairing" toggle and never PATCHes /settings', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    const { container } = render(
      <ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />,
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(container.textContent).not.toMatch(/introduced pairing/i)
    for (const cb of container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) {
      fireEvent.click(cb)
    }
    for (const [url] of apiPatch.mock.calls) {
      expect(String(url)).not.toContain('/settings')
    }
  })

  it('a visibility tick PATCHes /visible-users with the exact update body', async () => {
    apiGet.mockImplementation(async (u: string) =>
      u.endsWith('/visible-users') ? { users: _users } : { last_relay: null })
    apiPatch.mockResolvedValue({ users: [{ ..._users[0], visible: false }] })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    const cb = await screen.findByRole('checkbox', { name: /Anna/ })
    fireEvent.click(cb)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(1))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/visible-users',
      { updates: [{ user_id: 'u-anna', visible: false }] },
    )
  })

  it('a failed visibility save shows an error toast', async () => {
    const { showToast } = await import('@/components/Toast')
    ;(showToast as ReturnType<typeof vi.fn>).mockClear()
    apiGet.mockImplementation(async (u: string) =>
      u.endsWith('/visible-users') ? { users: _users } : { last_relay: null })
    apiPatch.mockRejectedValue(new Error('Peer not found.'))
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    fireEvent.click(await screen.findByRole('checkbox', { name: /Anna/ }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('Peer not found.', 'error'))
  })
})

describe('ConnectionDetail — GFS fallback section', () => {
  const section = () => document.querySelector('.sh-gfs-fallback')

  it('shows the switch for a direct pair when a GFS relays envelopes', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ source: 'manual', gfs_relay_available: true }) as any}
        relayGfs
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(section()).not.toBeNull()
    expect(screen.getByRole('switch', { name: 'Use the GFS as a fallback' })).toBeTruthy()
  })

  it('hides the switch without a relaying GFS while it is off', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(<ConnectionDetail conn={_conn() as any} onClose={() => {}} onRevoke={() => {}} />)
    expect(section()).toBeNull()
  })

  it('keeps the switch reachable when it is on, so it can be turned off', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ gfs_relay: true, gfs_relay_available: true }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(section()).not.toBeNull()
  })

  it('never offers it for a household met through an invite link', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ source: 'space_session' }) as any}
        relayGfs
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(section()).toBeNull()
  })
})

describe('ConnectionDetail in German', () => {
  beforeEach(async () => {
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('de')
  })
  afterEach(async () => {
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('en')
  })

  it('labels the rows plainly and picks the singular for one waiting message', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ proto_version: 19, queued_envelopes: 1, transport: 'https' }) as any}
        ours={19}
        compat={{
          instance_id: 'z7k63zfi', display_name: 'z7k63zfi', proto_version: 19,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: true, lacking_features: [],
        } as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText('Haushalts-ID')).toBeTruthy()
    expect(screen.getByText('App-Version')).toBeTruthy()
    expect(screen.getByText('Verbunden · Über das Internet')).toBeTruthy()
    expect(screen.getByText('v19, wie bei dir')).toBeTruthy()
    expect(screen.getByText('1 Nachricht wartet auf den Versand')).toBeTruthy()
    expect(screen.getAllByText('Über das Internet').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'z7k63zfi entkoppeln' })).toBeTruthy()
  })
})
