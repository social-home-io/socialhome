import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen, cleanup, waitFor } from '@testing-library/preact'
import en from '@/i18n/locales/en.json'

const apiPost = vi.fn()
const apiGet = vi.fn()
// The real ``ApiError``: its ``.code`` / ``.message`` drive which words a
// failure gets, so a stand-in would only test the stand-in.
vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    api: {
      get: (...args: unknown[]) => apiGet(...args),
      post: (...args: unknown[]) => apiPost(...args),
      patch: vi.fn(),
      delete: vi.fn(),
    },
    ApiError: real.ApiError,
  }
})

// jsdom has no camera / BarcodeDetector. The stand-in hands the flow a
// decoded "scan" on click, so QR-mode handling is testable.
vi.mock('./QrScanner', () => ({
  QrScanner: ({ onPayload }: { onPayload: (raw: string) => void }) => (
    <button type="button" onClick={() => onPayload('not-a-code')}>mock-qr-scan</button>
  ),
}))

// WS event handlers are captured so tests can fire synthetic events.
const wsHandlers: Record<string, ((e: { data: unknown }) => void)[]> = {}
const wsOnMock = vi.fn((event: string, handler: (e: { data: unknown }) => void) => {
  if (!wsHandlers[event]) wsHandlers[event] = []
  wsHandlers[event].push(handler)
  return () => {
    wsHandlers[event] = (wsHandlers[event] || []).filter(h => h !== handler)
  }
})
function fireWs(event: string, data: unknown) {
  for (const h of wsHandlers[event] ?? []) h({ data })
}

vi.mock('@/ws', () => ({
  ws: { on: (...args: Parameters<typeof wsOnMock>) => wsOnMock(...args) },
}))

// preact-iso: the success-dialog CTA navigates via the base/ingress-aware
// client router (loc.route), never a raw location.assign. Mock useLocation
// so the test can assert the route call.
const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))

const showToastSpy = vi.fn()
vi.mock('./Toast', () => ({
  showToast: (...args: unknown[]) => showToastSpy(...args),
}))

vi.mock('@/i18n/i18n', () => ({
  // Params are appended so a test can see what was substituted
  // (``key host`` for ``t(key, {host})``); plain keys render as-is.
  t: (key: string, params?: Record<string, string>) =>
    params ? `${key} ${Object.values(params).join(' ')}` : key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
}))

vi.mock('./ShareHomeToggle', () => ({
  ShareHomeToggle: ({ instanceId, peerName }: { instanceId: string; peerName: string }) => (
    <div data-testid="share-home-toggle"
         data-instance-id={instanceId}
         data-peer-name={peerName} />
  ),
}))

// Trivial QR mock — the unit test isn't here to verify the QR
// library, just the surrounding flow.
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,abc') },
}))

const writeText = vi.fn().mockResolvedValue(undefined)
Object.assign(navigator, { clipboard: { writeText } })

beforeEach(() => {
  vi.resetModules()
  apiPost.mockReset()
  apiGet.mockReset()
  routeSpy.mockReset()
  showToastSpy.mockReset()
  writeText.mockReset().mockResolvedValue(undefined)
  // Clear captured WS handlers so stale handlers from prior tests don't fire.
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  cleanup()
})

describe('PairingFlow — module surface', () => {
  it('module exports', async () => {
    const mod = await import('./PairingFlow')
    expect(typeof mod.openPairing).toBe('function')
    expect(typeof mod.PairingFlow).toBe('function')
    expect(typeof mod.buildPairingCode).toBe('function')
  })
})

describe('PairingFlow — socialhome://pair code round-trip', () => {
  it('encodes the payload as a single-line socialhome://pair#<b64> URL', async () => {
    const { buildPairingCode } = await import('./PairingFlow')
    const payload = JSON.stringify({
      token: 'tok-123',
      identity_pk: 'ed25519:beef',
      dh_pk: 'x25519:cafe',
      inbox_url: 'https://alice.example/inbox',
      expires_at: '2026-05-18T10:00:00Z',
    })
    const code = buildPairingCode(payload)
    expect(code.startsWith('socialhome://pair#')).toBe(true)
    expect(code.includes('\n')).toBe(false)
    // Whole code should round-trip back through atob → JSON.parse.
    const fragment = code.slice('socialhome://pair#'.length)
    const padded = fragment.replace(/-/g, '+').replace(/_/g, '/')
        + '='.repeat((4 - (fragment.length % 4)) % 4)
    const decoded = atob(padded)
    expect(JSON.parse(decoded)).toEqual(JSON.parse(payload))
  })

  it('inviter side copies socialhome://pair# code on click', async () => {
    apiPost.mockResolvedValueOnce({
      token: 'tok-xyz',
      identity_pk: 'ed25519:bb',
      dh_pk: 'x25519:cc',
      inbox_url: 'https://h.example/inbox/abc',
      expires_at: '2026-05-18T10:05:00Z',
    })
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_show')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    const copyBtn = await screen.findByText('pairing.copy_code')
    fireEvent.click(copyBtn)
    await new Promise(r => setTimeout(r, 0))
    expect(writeText).toHaveBeenCalledTimes(1)
    const arg = writeText.mock.calls[0][0] as string
    expect(arg.startsWith('socialhome://pair#')).toBe(true)
  })
})

describe('PairingFlow — scanner paste decoding', () => {
  it('decodes a socialhome://pair# code paste and POSTs to /api/pairing/accept', async () => {
    const payload = {
      token: 'tok-zzz',
      identity_pk: 'ed25519:11',
      dh_pk: 'x25519:22',
      inbox_url: 'https://bob.example/inbox',
      expires_at: '2026-05-18T10:00:00Z',
    }
    const json = JSON.stringify(payload)
    const { buildPairingCode, PairingFlow, openPairing } = await import('./PairingFlow')
    const code = buildPairingCode(json)
    apiPost.mockResolvedValueOnce({ verification_code: '123456', token: 'tok-zzz' })

    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_scan')
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))

    // Method picker — click Paste code
    const pasteCard = await screen.findByText('pairing.method_paste')
    fireEvent.click(pasteCard)

    // Find the textarea + fill it
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: code } })

    // Submit
    fireEvent.click(screen.getByText('pairing.paste_submit'))

    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/pairing/accept', payload)
  })

  it('back-compat: raw JSON paste still works', async () => {
    const payload = {
      token: 'legacy',
      identity_pk: 'ed25519:legacy',
      dh_pk: 'x25519:legacy',
      inbox_url: 'https://l.example/inbox',
    }
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    apiPost.mockResolvedValueOnce({ verification_code: '098765', token: 'legacy' })
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_scan')
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: JSON.stringify(payload) } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/pairing/accept', payload)
  })
})

describe('PairingFlow — GFS paste decoding', () => {
  it('parses socialhome://gfs-pair/{url}?token=… and POSTs {gfs_url, token}', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    apiPost.mockResolvedValueOnce({})
    render(<PairingFlow />)
    openPairing('gfs')

    // gfs idle → click Add GFS
    const addBtn = await screen.findByText('gfs.add')
    fireEvent.click(addBtn)

    // Method picker
    fireEvent.click(await screen.findByText('pairing.method_paste'))

    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=t-aabbcc'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))

    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/gfs/connections', {
      gfs_url: 'https://gfs.example.com',
      token: 't-aabbcc',
    })
  })

  it('rejects a malformed GFS code without hitting the backend', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    fireEvent.input(textarea, { target: { value: 'not-a-code' } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('PairingFlow — GFS connect failures', () => {
  const GFS_PATH = '/api/gfs/connections'

  async function startGfs() {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
  }

  async function pasteGfsCode() {
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    fireEvent.input(textarea, {
      target: { value: 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs' },
    })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
  }

  // The i18n mock renders keys: each case pins which line the panel
  // picked. ``ApiError.message`` comes from the real ``apiErrors`` table,
  // so a GFS code resolves to its ``error.gfs_*`` key.
  it('502 GFS_UNREACHABLE: a GFS heading and the GFS-unreachable line', async () => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(502, GFS_PATH, {
      code: 'GFS_UNREACHABLE', detail: "Couldn't reach the GFS. Try again later.",
    }))
    await startGfs()
    await pasteGfsCode()
    expect(await screen.findByText('gfs.pair_failed')).toBeTruthy()
    expect(screen.getByText('error.gfs_unavailable')).toBeTruthy()
    // Never the household-pairing words.
    expect(screen.queryByText('pairing.failed')).toBeNull()
    expect(screen.queryByText('pairing.error.server')).toBeNull()
    expect(screen.queryByText('pairing.error.malformed')).toBeNull()
  })

  it.each([
    ['GFS_IDENTITY_MISMATCH', 422, 'error.gfs_identity_mismatch'],
    ['GFS_PAIRING_FAILED', 422, 'error.gfs_pairing_failed'],
    ['GFS_SIGNUP_CLOSED', 409, 'error.gfs_signup_closed'],
    ['ALREADY_CONNECTED', 409, 'error.gfs_already_connected'],
    ['GFS_BUSY', 503, 'error.gfs_busy'],
  ])('%s (%i): its own line, never "pairing code looks malformed"', async (code, status, key) => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(status, GFS_PATH, { code, detail: 'server words' }))
    await startGfs()
    await pasteGfsCode()
    expect(await screen.findByText('gfs.pair_failed')).toBeTruthy()
    expect(screen.getByText(key)).toBeTruthy()
    expect(screen.queryByText('pairing.error.malformed')).toBeNull()
    expect(screen.queryByText('pairing.error.already_paired')).toBeNull()
    expect(screen.queryByText('server words')).toBeNull()
  })

  it('an unknown refusal shows the server reason, and Retry goes back to the start', async () => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(422, GFS_PATH, {
      code: 'GFS_TOKEN_EXPIRED', detail: 'That code has expired.',
    }))
    await startGfs()
    await pasteGfsCode()
    expect(await screen.findByText('gfs.pair_failed')).toBeTruthy()
    expect(screen.getByText('That code has expired.')).toBeTruthy()
    fireEvent.click(screen.getByText('pairing.retry'))
    expect(await screen.findByText('gfs.add')).toBeTruthy()
  })

  it('a QR scan that is not a GFS code shows gfs.invalid_code in QR mode', async () => {
    await startGfs()
    // QR is the default method — the scanner stand-in "decodes" junk.
    fireEvent.click(await screen.findByText('mock-qr-scan'))
    expect(await screen.findByText('gfs.invalid_code')).toBeTruthy()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('household: a QR scan that is not a pairing code shows the error in QR mode too', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    fireEvent.click(await screen.findByLabelText('pairing.role_scan_aria'))
    fireEvent.click(await screen.findByText('mock-qr-scan'))
    expect(await screen.findByText('pairing.scan_invalid_code')).toBeTruthy()
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('PairingFlow — the failure panel is announced and focused', () => {
  // The verdict ("Pairing failed" / "Couldn't connect to the GFS") must
  // reach screen readers (role=alert) and keyboard focus must land on
  // Retry, the panel's one control — not stay on the modal's close ✕.
  async function expectAnnouncedAndFocused() {
    const panel = document.querySelector('.sh-pairing-failed')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('role')).toBe('alert')
    const retry = screen.getByText('pairing.retry')
    await waitFor(() => expect(document.activeElement).toBe(retry))
  }

  it('GFS mode: role=alert on the panel, focus on Retry', async () => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(502, '/api/gfs/connections', {
      code: 'GFS_UNREACHABLE', detail: 'down',
    }))
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    fireEvent.input(textarea, {
      target: { value: 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs' },
    })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    expect(await screen.findByText('gfs.pair_failed')).toBeTruthy()
    await expectAnnouncedAndFocused()
  })

  it('household mode: role=alert on the panel, focus on Retry', async () => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(500, '/api/pairing/initiate', {
      code: 'INTERNAL', detail: 'boom',
    }))
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_show')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    expect(await screen.findByText('pairing.failed')).toBeTruthy()
    await expectAnnouncedAndFocused()
  })
})

describe('PairingFlow — leaving a step for the start puts focus on its first control', () => {
  // Retry (and the Back / Cancel links) unmount the control that had
  // focus, so ``document.activeElement`` would fall to ``body``: the
  // focus trap keeps Tab inside the dialog, but a screen-reader user
  // gets no announced landing spot. Focus must land on the first
  // control of the start step — the GFS "Add" button, or the first
  // role card in household mode.
  async function failGfs() {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(502, '/api/gfs/connections', {
      code: 'GFS_UNREACHABLE', detail: 'down',
    }))
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    fireEvent.input(textarea, {
      target: { value: 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs' },
    })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    const retry = await screen.findByText('pairing.retry')
    await waitFor(() => expect(document.activeElement).toBe(retry))
    return openPairing
  }

  it('GFS mode: Retry after a failure focuses the "Add GFS" button', async () => {
    await failGfs()
    fireEvent.click(screen.getByText('pairing.retry'))
    const add = await screen.findByText('gfs.add')
    await waitFor(() => expect(document.activeElement).toBe(add))
  })

  it('household mode: Retry after a failure focuses the "Show my QR" role card', async () => {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(500, '/api/pairing/initiate', {
      code: 'INTERNAL', detail: 'boom',
    }))
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_show')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    const retry = await screen.findByText('pairing.retry')
    await waitFor(() => expect(document.activeElement).toBe(retry))
    fireEvent.click(retry)
    const showCard = await screen.findByLabelText('pairing.role_show_aria')
    await waitFor(() => expect(document.activeElement).toBe(showCard))
  })

  it('household mode: Back from the scanner focuses the "Show my QR" role card', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    fireEvent.click(await screen.findByLabelText('pairing.role_scan_aria'))
    const back = await screen.findByText('pairing.back')
    back.focus()
    fireEvent.click(back)
    const showCard = await screen.findByLabelText('pairing.role_show_aria')
    await waitFor(() => expect(document.activeElement).toBe(showCard))
  })

  it('a fresh open leaves focus where the Modal put it — inside the dialog, never body', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    await screen.findByText('gfs.add')
    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog).not.toBeNull()
    await waitFor(() => {
      expect(document.activeElement).not.toBe(document.body)
      expect(dialog!.contains(document.activeElement)).toBe(true)
    })
    // The Modal's own open-focus picks the first focusable control (the
    // close ✕ in the header); the start-step effect must not override it.
    expect(document.activeElement).toBe(screen.getByLabelText('modal.close'))
  })

  it('closing on the failure panel and re-opening does not fight the Modal open-focus', async () => {
    const openPairing = await failGfs()
    fireEvent.click(screen.getByLabelText('modal.close'))
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
    openPairing('gfs')
    await screen.findByText('gfs.add')
    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText('modal.close')))
  })
})

describe('PairingFlow — configure-sharing step', () => {
  it('household success Done advances to configure-sharing', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')

    // Fire the pairing.confirmed WS event to flip to 'success'.
    fireWs('pairing.confirmed', {
      instance_id: 'peer-abc123',
      display_name: 'Alice Home',
    })
    await new Promise(r => setTimeout(r, 0))

    // The success screen shows "pairing.success" and a Done button.
    await screen.findByText('pairing.success')
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    // We should now see the configure-sharing step.
    expect(screen.getByText('pairing.configure_sharing_title')).toBeTruthy()
    // ShareHomeToggle should be rendered with the just-paired peer's id.
    const toggle = screen.getByTestId('share-home-toggle')
    expect(toggle.getAttribute('data-instance-id')).toBe('peer-abc123')
    expect(toggle.getAttribute('data-peer-name')).toBe('Alice Home')
  })

  it('configure-sharing Done closes the wizard', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')

    // Drive to success via WS event.
    fireWs('pairing.confirmed', {
      instance_id: 'peer-xyz',
      display_name: 'Bob Home',
    })
    await new Promise(r => setTimeout(r, 0))
    await screen.findByText('pairing.success')

    // Advance to configure-sharing.
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))
    await screen.findByText('pairing.configure_sharing_title')

    // Click Done on configure-sharing — wizard should close.
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    // Modal is closed — success title is no longer visible.
    expect(screen.queryByText('pairing.configure_sharing_title')).toBeNull()
    expect(screen.queryByText('pairing.success')).toBeNull()
  })

  it('GFS success path is unchanged — Done closes directly without configure-sharing', async () => {
    apiPost.mockResolvedValueOnce({})
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')

    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))

    // GFS success — Done should close, not go to configure-sharing.
    await screen.findByText('gfs.connected')
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    expect(screen.queryByText('pairing.configure_sharing_title')).toBeNull()
  })
})

describe('PairingFlow — GFS pending-approval success dialog', () => {
  async function connectGfs(status: string) {
    apiPost.mockResolvedValueOnce({
      id: 'gfs-1',
      gfs_instance_id: 'i1',
      display_name: 'Town GFS',
      inbox_url: 'https://gfs.example.com',
      status,
      paired_at: '2026-06-06T00:00:00+00:00',
      published_space_count: 0,
    })
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
  }

  it('pending: shows the pending heading/message and only Done (no publish CTA)', async () => {
    await connectGfs('pending')

    // Pending copy, not the "Connected!" copy.
    expect(screen.getByText('gfs.connected_pending')).toBeTruthy()
    expect(screen.getByText('gfs.pending_approval')).toBeTruthy()
    expect(screen.queryByText('gfs.connected')).toBeNull()

    // Only the Done button — no "Open public sharing settings".
    expect(screen.getByText('pairing.done')).toBeTruthy()
    expect(screen.queryByText('gfs.open_publishing')).toBeNull()

    // Neutral/info toast, not the green "connected" toast.
    expect(showToastSpy).toHaveBeenCalledWith('gfs.pending_toast', 'info')
  })

  it('active: shows the connected copy + the publish CTA, which routes via loc.route', async () => {
    await connectGfs('active')

    expect(screen.getByText('gfs.connected')).toBeTruthy()
    expect(screen.queryByText('gfs.connected_pending')).toBeNull()
    expect(showToastSpy).toHaveBeenCalledWith('gfs.pair_success', 'success')

    // The "Open public sharing settings" CTA is present and navigates via
    // the base-aware client router — never location.assign to an absolute path.
    const cta = screen.getByText('gfs.open_publishing')
    fireEvent.click(cta)
    await new Promise(r => setTimeout(r, 0))
    expect(routeSpy).toHaveBeenCalledWith('/momentum/public/sharing')
  })
})

describe('PairingFlow — "external URL not configured" hint per platform', () => {
  /**
   * The inviter's POST /api/pairing/initiate returns 422 when this Social
   * Home has no externally-reachable inbox URL. What the admin should DO
   * about that differs per deployment, and the haos case is the reason
   * this exists: the add-on sits behind HA Ingress with no address of its
   * own and no settings field to type one into, so the old copy sent
   * those users looking for a control that isn't there.
   *
   * Drives the real `instanceConfig` signal rather than mocking
   * `@/platform`, so the accessors themselves are covered.
   */
  async function hintAfter422(mode: 'standalone' | 'ha' | 'haos' | null) {
    const { instanceConfig } = await import('@/store/instance')
    instanceConfig.value = mode === null
      ? null
      : {
          mode,
          instance_name: 'Test Home',
          instance_id: 'iid',
          capabilities: [],
          setup_required: false,
        }
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(422, '/api/pairing/initiate', {
      code: 'NOT_CONFIGURED', detail: 'External URL is not configured.',
    }))

    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_show')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    await screen.findByText('pairing.failed')
    return document.body.textContent ?? ''
  }

  // The i18n mock renders keys, so each case asserts which hint key the
  // flow picked, and the English catalog pins what that hint says.
  const HAOS = 'pairing.error.no_url_haos'
  const HA = 'pairing.error.no_url_ha'
  const STANDALONE = 'pairing.error.no_url'
  // Most specific first: the standalone key is a prefix of the other two.
  const hint = (text: string) =>
    [HAOS, HA, STANDALONE].find((k) => text.includes(k)) ?? null

  it('haos: points at the HA custom integration, never at settings', async () => {
    const text = await hintAfter422('haos')
    expect(hint(text)).toBe(HAOS)
    expect(en[HAOS]).toContain('Social Home integration in Home Assistant')
    // The add-on has no external-URL field, so mentioning settings here
    // would send the admin hunting for a control that does not exist.
    expect(en[HAOS]).not.toContain('Settings → Connections')
    // The add-on ships the integration (Supervisor discovery), so there
    // is no HACS step for the user to take.
    expect(en[HAOS]).not.toContain('HACS')
  })

  it('ha: offers the settings field AND the integration', async () => {
    const text = await hintAfter422('ha')
    expect(hint(text)).toBe(HA)
    expect(en[HA]).toContain('Settings → Connections')
    expect(en[HA]).toContain('Social Home integration in Home Assistant')
    expect(en[HA]).not.toContain('HACS')
  })

  it('standalone: unchanged — settings only, no HA wording', async () => {
    const text = await hintAfter422('standalone')
    expect(hint(text)).toBe(STANDALONE)
    expect(en[STANDALONE]).toContain('Settings → Connections')
    expect(en[STANDALONE]).not.toContain('Home Assistant')
    expect(en[STANDALONE]).not.toContain('HACS')
  })

  it('falls back to the standalone wording before the config loads', async () => {
    // `instanceConfig` is null until GET /api/instance/config resolves. The
    // hint must still be the text this always showed, not an HA-specific
    // one guessed at from nothing.
    const text = await hintAfter422(null)
    expect(hint(text)).toBe(STANDALONE)
  })
})

// ─── Reach picker (inviter): direct URL / URL + GFS / GFS only ──────────

const RELAY_GFS = {
  id: 'gfs-a', gfs_instance_id: 'gi-a', display_name: 'GFS A',
  inbox_url: 'https://gfs-a.example', status: 'active', paired_at: '',
  published_space_count: 0, connected: true, envelope_relay: true,
}

/** Answer the two start-step lookups: the GFS connections list and the
 *  external-URL read (``effective`` null = no URL configured). */
function stubReach(conns: unknown[], effective: string | null) {
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/gfs/connections') return Promise.resolve(conns)
    if (path === '/api/admin/federation/external-url') {
      return Promise.resolve({ base: null, effective, source: effective ? 'auto' : null })
    }
    return Promise.resolve(undefined)
  })
}

async function openStart() {
  const { PairingFlow, openPairing } = await import('./PairingFlow')
  render(<PairingFlow />)
  openPairing('household')
  await screen.findByText('pairing.role_show')
}

const radio = (key: string) =>
  screen.getByLabelText(key) as HTMLInputElement

describe('PairingFlow — reach picker', () => {
  it('is not rendered without a relay-capable GFS (today\'s start step)', async () => {
    stubReach([
      { ...RELAY_GFS, envelope_relay: false },
      { ...RELAY_GFS, id: 'gfs-p', status: 'pending', envelope_relay: true },
    ], 'https://home.example')
    await openStart()
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/admin/federation/external-url'))
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('pairing.reach.legend')).toBeNull()
  })

  it('with a URL: "Direct URL" is the default and no GFS warning shows', async () => {
    stubReach([RELAY_GFS], 'https://home.example')
    await openStart()
    await screen.findByText('pairing.reach.legend')
    expect(radio('pairing.reach.url').checked).toBe(true)
    expect(radio('pairing.reach.url').disabled).toBe(false)
    expect(radio('pairing.reach.url_gfs').disabled).toBe(false)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.queryByTestId('pairing-reach-no-url')).toBeNull()
  })

  it('without a URL: "GFS only" is preselected, URL options disabled with the how-to hint', async () => {
    stubReach([RELAY_GFS], null)
    await openStart()
    await screen.findByText('pairing.reach.legend')
    await waitFor(() => expect(radio('pairing.reach.gfs').checked).toBe(true))
    expect(radio('pairing.reach.url').disabled).toBe(true)
    expect(radio('pairing.reach.url_gfs').disabled).toBe(true)
    // The picker's own line: it speaks only for the URL options ("GFS
    // only" works without a URL), never the "set a URL before pairing" hint.
    expect(screen.getByTestId('pairing-reach-no-url').textContent).toBe('pairing.reach.no_url')
    expect(en['pairing.reach.no_url']).toContain('Settings → Connections')
    expect(en['pairing.reach.no_url']).not.toContain('before pairing')
    const fieldset = screen.getByText('pairing.reach.legend').closest('fieldset')!
    expect(fieldset.getAttribute('aria-describedby'))
      .toBe('sh-pairing-reach-hint sh-pairing-reach-no-url')
  })

  it('shows the privacy note only while a GFS option is selected', async () => {
    stubReach([RELAY_GFS], 'https://home.example')
    await openStart()
    await screen.findByText('pairing.reach.legend')
    fireEvent.click(radio('pairing.reach.url_gfs'))
    expect((await screen.findByRole('note')).textContent).toBe('pairing.reach.gfs_warning')
    fireEvent.click(radio('pairing.reach.gfs'))
    expect(screen.getByRole('note')).toBeTruthy()
    fireEvent.click(radio('pairing.reach.url'))
    await waitFor(() => expect(screen.queryByRole('note')).toBeNull())
  })

  it('initiate sends {reach, gfs_id}; a second relay GFS can be picked', async () => {
    stubReach([RELAY_GFS, { ...RELAY_GFS, id: 'gfs-b', display_name: 'GFS B' }], 'https://home.example')
    apiPost.mockResolvedValue({ token: 't', identity_pk: 'i', dh_pk: 'd' })
    await openStart()
    await screen.findByText('pairing.reach.legend')
    // Only a GFS option needs the server picker.
    expect(screen.queryByLabelText('pairing.reach.gfs_select')).toBeNull()
    fireEvent.click(radio('pairing.reach.gfs'))
    const select = await screen.findByLabelText('pairing.reach.gfs_select') as HTMLSelectElement
    expect(select.value).toBe('gfs-a')
    fireEvent.change(select, { target: { value: 'gfs-b' } })
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/pairing/initiate', { reach: 'gfs', gfs_id: 'gfs-b' },
    ))
  })

  it('initiate sends {reach: "url"} (no gfs_id) for the direct default', async () => {
    stubReach([RELAY_GFS], 'https://home.example')
    apiPost.mockResolvedValue({ token: 't', identity_pk: 'i', dh_pk: 'd' })
    await openStart()
    await screen.findByText('pairing.reach.legend')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/pairing/initiate', { reach: 'url' },
    ))
  })

  it('initiate keeps the empty body when there is no picker', async () => {
    stubReach([], 'https://home.example')
    apiPost.mockResolvedValue({ token: 't', identity_pk: 'i', dh_pk: 'd' })
    await openStart()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/pairing/initiate', {}))
  })
})

// ─── GFS-reach refusals get their own words, never "malformed" ──────────

describe('PairingFlow — GFS reach error copy', () => {
  async function initiateFails(code: string) {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(422, '/api/pairing/initiate', { code, detail: 'x' }))
    await openStart()
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    await screen.findByText('pairing.failed')
    return document.body.textContent ?? ''
  }

  async function acceptFails(code: string, payload: Record<string, unknown>) {
    const { ApiError } = await import('@/api')
    apiPost.mockRejectedValueOnce(new ApiError(422, '/api/pairing/accept', { code, detail: 'x' }))
    const { buildPairingCode } = await import('./PairingFlow')
    await openStart()
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: buildPairingCode(JSON.stringify(payload)) } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await screen.findByText('pairing.failed')
    return document.body.textContent ?? ''
  }

  const GFS_CODE = {
    token: 't', identity_pk: 'i', dh_pk: 'd', inbox_url: '',
    reach: 'gfs', gfs: { url: 'https://relay.example.org', instance_id: 'gi' },
  }

  it('GFS_NOT_CONNECTED at initiate', async () => {
    const text = await initiateFails('GFS_NOT_CONNECTED')
    expect(text).toContain('pairing.error.gfs_not_connected')
    expect(text).not.toContain('pairing.error.no_url')
    expect(en['pairing.error.gfs_not_connected']).toContain('GFS')
  })

  it('INVALID_REACH at initiate is not the missing-URL hint', async () => {
    const text = await initiateFails('INVALID_REACH')
    expect(text).toContain('pairing.error.invalid_reach')
    expect(text).not.toContain('pairing.error.no_url')
  })

  it('NOT_CONFIGURED at initiate keeps the missing-URL hint', async () => {
    const text = await initiateFails('NOT_CONFIGURED')
    expect(text).toContain('pairing.error.no_url')
  })

  it('GFS_NOT_SHARED on accept names the code\'s GFS host', async () => {
    const text = await acceptFails('GFS_NOT_SHARED', GFS_CODE)
    expect(text).toContain('pairing.error.gfs_not_shared relay.example.org')
    expect(text).not.toContain('pairing.error.malformed')
    expect(en['pairing.error.gfs_not_shared']).toContain('{host}')
    expect(en['pairing.error.gfs_not_shared']).toContain('direct URL')
  })

  it('GFS_NOT_SHARED without a host in the code falls back to the generic line', async () => {
    const text = await acceptFails('GFS_NOT_SHARED', {
      token: 't', identity_pk: 'i', dh_pk: 'd', inbox_url: '',
    })
    expect(text).toContain('pairing.error.gfs_not_shared_generic')
  })

  it('KEYWRAP_INVALID on accept', async () => {
    const text = await acceptFails('KEYWRAP_INVALID', GFS_CODE)
    expect(text).toContain('pairing.error.keywrap_invalid')
    expect(text).not.toContain('pairing.error.malformed')
  })

  it('INVALID_REACH on accept', async () => {
    const text = await acceptFails('INVALID_REACH', { ...GFS_CODE, reach: 'carrier-pigeon' })
    expect(text).toContain('pairing.error.invalid_reach')
    expect(text).not.toContain('pairing.error.malformed')
  })

  it('a plain 422 on accept is still "malformed"', async () => {
    const text = await acceptFails('UNPROCESSABLE', GFS_CODE)
    expect(text).toContain('pairing.error.malformed')
  })
})

// ─── Scanner: "this pairing also uses the GFS at …" ────────────────────

describe('PairingFlow — scan side names the GFS a code uses', () => {
  async function scan(payload: Record<string, unknown>) {
    apiPost.mockResolvedValueOnce({ verification_code: '123456', token: 't' })
    const { buildPairingCode } = await import('./PairingFlow')
    await openStart()
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: buildPairingCode(JSON.stringify(payload)) } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await screen.findByText('pairing.sas_heading')
    return document.body.textContent ?? ''
  }
  const base = { token: 't', identity_pk: 'i', dh_pk: 'd', inbox_url: 'https://a.example/inbox' }

  it('reach=url_gfs: the pairing ALSO uses the GFS', async () => {
    const text = await scan({ ...base, reach: 'url_gfs', gfs: { url: 'https://relay.example.org/', instance_id: 'gi' } })
    expect(text).toContain('pairing.scan.uses_gfs relay.example.org')
    expect(text).not.toContain('pairing.scan.via_gfs')
  })

  it('reach=gfs: the pairing goes THROUGH the GFS', async () => {
    const text = await scan({ ...base, inbox_url: '', reach: 'gfs', gfs: { url: 'https://relay.example.org/', instance_id: 'gi' } })
    expect(text).toContain('pairing.scan.via_gfs relay.example.org')
    expect(text).not.toContain('pairing.scan.uses_gfs')
    expect(en['pairing.scan.via_gfs']).toContain('goes through')
  })

  it('reach=url shows no GFS line', async () => {
    const text = await scan({ ...base, reach: 'url' })
    expect(text).not.toContain('pairing.scan.uses_gfs')
  })

  it('an old code without reach fields still decodes and shows no GFS line', async () => {
    const text = await scan(base)
    expect(apiPost).toHaveBeenCalledWith('/api/pairing/accept', base)
    expect(text).not.toContain('pairing.scan.uses_gfs')
  })

  it('pairingCodeGfsHost ignores junk', async () => {
    const { pairingCodeGfsHost } = await import('./PairingFlow')
    expect(pairingCodeGfsHost({ reach: 'gfs', gfs: { url: 'not a url' } })).toBeNull()
    expect(pairingCodeGfsHost({ reach: 'gfs', gfs: 'x' })).toBeNull()
    expect(pairingCodeGfsHost({ reach: 'gfs' })).toBeNull()
    expect(pairingCodeGfsHost({ reach: 'url', gfs: { url: 'https://x.example' } })).toBeNull()
  })
})

// ─── Reach picker: placement, loading race, retry, reopen ──────────────

describe('PairingFlow — reach picker lifecycle', () => {
  it('renders above the role cards', async () => {
    stubReach([RELAY_GFS], 'https://home.example')
    await openStart()
    const legend = await screen.findByText('pairing.reach.legend')
    const show = screen.getByLabelText('pairing.role_show_aria')
    expect(legend.compareDocumentPosition(show) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('"Show my QR" waits while the lookups load — a tap mints nothing', async () => {
    let release: (v: unknown) => void = () => {}
    apiGet.mockImplementation((path: string) => path === '/api/gfs/connections'
      ? new Promise(r => { release = r })
      : Promise.resolve({ effective: null }))
    apiPost.mockResolvedValue({ token: 't', identity_pk: 'i', dh_pk: 'd' })
    await openStart()
    const show = screen.getByLabelText('pairing.role_show_aria')
    expect(show.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(show)
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).not.toHaveBeenCalled()
    release([RELAY_GFS])
    await screen.findByText('pairing.reach.legend')
    await waitFor(() => expect(show.getAttribute('aria-disabled')).toBeNull())
    fireEvent.click(show)
    // No URL → GFS only was preselected, never the bare {} body.
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/pairing/initiate', { reach: 'gfs', gfs_id: 'gfs-a' },
    ))
  })

  it('external-url lookup failure → every option stays enabled', async () => {
    apiGet.mockImplementation((path: string) => path === '/api/gfs/connections'
      ? Promise.resolve([RELAY_GFS])
      : Promise.reject(new Error('403')))
    await openStart()
    await screen.findByText('pairing.reach.legend')
    for (const k of ['pairing.reach.url', 'pairing.reach.url_gfs', 'pairing.reach.gfs']) {
      expect(radio(k).disabled).toBe(false)
    }
    expect(radio('pairing.reach.url').checked).toBe(true)
    expect(screen.queryByTestId('pairing-reach-no-url')).toBeNull()
  })

  it('Retry after a GFS refusal re-reads the lookups and keeps the choice; focus lands on "Show my QR"', async () => {
    const { ApiError } = await import('@/api')
    stubReach([RELAY_GFS], 'https://home.example')
    apiPost.mockRejectedValueOnce(new ApiError(422, '/api/pairing/initiate', {
      code: 'GFS_NOT_CONNECTED', detail: 'x',
    }))
    await openStart()
    await screen.findByText('pairing.reach.legend')
    fireEvent.click(radio('pairing.reach.url_gfs'))
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    const retry = await screen.findByText('pairing.retry')
    const getsBefore = apiGet.mock.calls.length
    fireEvent.click(retry)
    const show = await screen.findByLabelText('pairing.role_show_aria')
    await waitFor(() => expect(document.activeElement).toBe(show))
    await waitFor(() => expect(apiGet.mock.calls.length).toBe(getsBefore + 2))
    await waitFor(() => expect(show.getAttribute('aria-disabled')).toBeNull())
    expect(radio('pairing.reach.url_gfs').checked).toBe(true)
  })

  it('Retry drops a choice that is no longer possible', async () => {
    const { ApiError } = await import('@/api')
    stubReach([RELAY_GFS], 'https://home.example')
    apiPost.mockRejectedValueOnce(new ApiError(422, '/api/pairing/initiate', {
      code: 'GFS_NOT_CONNECTED', detail: 'x',
    }))
    await openStart()
    await screen.findByText('pairing.reach.legend')
    fireEvent.click(radio('pairing.reach.url_gfs'))
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    // Meanwhile the URL went away: url_gfs can't work any more.
    stubReach([RELAY_GFS], null)
    fireEvent.click(await screen.findByText('pairing.retry'))
    await waitFor(() => expect(radio('pairing.reach.gfs').checked).toBe(true))
  })

  it('reopening resets the choice, and a late answer for the old open is ignored', async () => {
    const answers: ((v: unknown) => void)[] = []
    apiGet.mockImplementation((path: string) => path === '/api/gfs/connections'
      ? new Promise(r => { answers.push(r) })
      : Promise.resolve({ effective: 'https://home.example' }))
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')      // lookup #1 — will answer late
    openPairing('gfs')            // a GFS-mode open invalidates it too
    openPairing('household')      // lookup #2
    await waitFor(() => expect(answers.length).toBe(2))
    answers[1]([RELAY_GFS])
    await screen.findByText('pairing.reach.legend')
    fireEvent.click(radio('pairing.reach.gfs'))
    // The stale answer (no relay GFS) must not wipe the current picker.
    answers[0]([])
    await new Promise(r => setTimeout(r, 0))
    expect(screen.getByText('pairing.reach.legend')).toBeTruthy()
    expect(radio('pairing.reach.gfs').checked).toBe(true)
    // A fresh open starts from the default again.
    stubReach([RELAY_GFS], 'https://home.example')
    openPairing('household')
    await waitFor(() => expect(radio('pairing.reach.url').checked).toBe(true))
  })
})
