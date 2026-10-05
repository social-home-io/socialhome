/**
 * PairingFlow — household + GFS pairing (§11 / §23.4 / §24.7).
 *
 * Two sides, one component:
 *
 *   **Show QR** (inviter): generates a QR, waits for the other side
 *   to scan, auto-fills the 6-digit SAS code when the peer's
 *   ``peer-accept`` lands, admin confirms, WS ``pairing.confirmed``
 *   flips to success. Renders the QR + a peer ``socialhome://pair#…``
 *   "share code" card so remote-pairing (chat, SMS, email) works.
 *
 *   **Scan QR** (scanner): a two-method picker — camera scan via the
 *   native ``BarcodeDetector`` (with image-upload fallback inside the
 *   same method) and "Paste code" textarea as the equal-weight peer.
 *   Posts the parsed payload to ``/api/pairing/accept``, shows the
 *   SAS for the scanner to read aloud to the inviter, waits for
 *   ``pairing.confirmed``.
 *
 * GFS mode: the Global-Federation-Server connect flow uses the same
 * two-method picker. The QR encodes a ``socialhome://gfs-pair/{url}
 * ?token={token}`` URL the GFS landing page now publishes; the paste
 * field accepts that URL directly. POSTs ``{gfs_url, token}`` (the
 * shape ``gfs_connection_service.pair`` requires).
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import type { GfsConnection } from '@/types'
import { ws } from '@/ws'
import { base64UrlEncode, base64UrlDecode } from '@/lib/base64Url'
import { Modal } from './Modal'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import { isHomeAssistant, isSupervisorAddon } from '@/platform'
import { ShareHomeToggle } from './ShareHomeToggle'
import { QrCodeImg } from './QrCodeImg'
import { QrScanner } from './QrScanner'

type PairingMode = 'household' | 'gfs'
type PairingRole = 'unset' | 'inviter' | 'scanner'
type ScanMethod = 'qr' | 'paste'
type PairingStep =
  | 'idle'        // mode picker (inviter / scanner)
  | 'generating'  // inviter: POST /api/pairing/initiate
  | 'waiting'     // inviter: QR + code shown, waiting for SAS auto-fill
  | 'scanning'    // scanner: camera / upload / paste
  | 'accepting'   // scanner: POST /api/pairing/accept
  | 'sas-display' // scanner: show the 6-digit SAS for out-of-band verify
  | 'verifying'   // inviter: POST /api/pairing/confirm
  | 'success'
  | 'configure-sharing' // household: toggle home-location sharing after success
  | 'failed'

const step = signal<PairingStep>('idle')
const role = signal<PairingRole>('unset')
const mode = signal<PairingMode>('household')
const qrPayload = signal('')         // raw JSON for household, socialhome://gfs-pair URL for gfs
const pairingCode = signal('')       // socialhome://pair#… (household) or socialhome://gfs-pair/… (gfs)
const verificationCode = signal('')
const sasDigits = signal(['', '', '', '', '', ''])
const pairingToken = signal('')
const scannedSas = signal('')  // scanner-side SAS to display
const open = signal(false)
const onGfsConnectedCb = signal<(() => void) | null>(null)
/** Status of the GFS connection just created — drives the success-dialog
 *  copy + CTA. ``pending`` = the GFS holds new households for admin review,
 *  so we can't offer "publish" yet. */
const gfsResultStatus = signal<string>('active')
const peerHint = signal<string | null>(null)
const scanError = signal<string | null>(null)
/** Instance ID of the peer that was just paired — populated on ``pairing.confirmed``
 *  so the configure-sharing step can PATCH the right connection. */
const justPairedInstanceId = signal<string | null>(null)
/** Display name of the peer that was just paired — shown in the toggle label. */
const justPairedDisplayName = signal<string | null>(null)

/**
 * The inviter side can't mint a pairing token until this Social Home
 * knows an externally-reachable inbox URL (a 422 from
 * ``/api/pairing/initiate``). *How* an admin supplies that URL depends
 * entirely on the deployment, so the hint has to as well — telling a
 * Supervisor add-on user to edit a settings field it doesn't have just
 * strands them.
 *
 * - **haos** — the add-on lives behind HA Ingress and has no address of
 *   its own to advertise. The Social Home HA integration is the only
 *   supported source: it runs inside Home Assistant, resolves the
 *   reachable URL (`external_url`, or Nabu Casa Remote UI) and pushes it
 *   to the add-on, which then stamps it into new pairing QRs. The add-on
 *   ships it — `HaBootstrap` registers with the Supervisor's discovery
 *   integration (`platform/haos/supervisor.py`) so Home Assistant offers
 *   it directly; there is no HACS step to mention, and no settings field
 *   to point at either.
 * - **ha** — the URL can be configured directly, and the integration
 *   will also supply it, so offer both.
 * - **standalone** — the external-URL instruction, unchanged in
 *   substance. "Settings → Connections" is the sidebar's own label for
 *   the page that carries the External URL field
 *   (`features/connections/ConnectionsPage`).
 *
 * Read through the `@/platform` accessors rather than comparing mode
 * strings, mirroring the backend's "consume capabilities, never branch on
 * `config.mode`" rule. Both accessors are false until
 * `GET /api/instance/config` has loaded, which falls through to the
 * standalone wording — the same text this showed before, so a slow config
 * fetch can't make the hint wrong, only less specific.
 */
function notConfiguredHint(): string {
  if (isSupervisorAddon()) {
    return t('pairing.error.no_url_haos')
  }
  if (isHomeAssistant()) {
    return t('pairing.error.no_url_ha')
  }
  return t('pairing.error.no_url')
}

/**
 * Translate an API failure into a human-friendly hint shown under the
 * "Pairing failed" headline. The previous behaviour dumped the raw
 * ``Error.message`` string ("API 422: /api/pairing/initiate") which
 * tells a household admin nothing useful — they just see the verb and
 * the path with no clue what to do next.
 *
 * The ``stage`` argument lets us tailor the hint to where the failure
 * happened (only ``'initiate'`` carries a 422 today — the inviter side
 * needs an external URL configured before the server will mint a
 * pairing token; see {@link notConfiguredHint}).
 */
function friendlyPairError(err: unknown, stage?: 'initiate'): string {
  if (err instanceof ApiError) {
    if (stage === 'initiate' && err.status === 422) {
      return notConfiguredHint()
    }
    if (err.status === 401 || err.status === 403) {
      return t('pairing.error.not_admin')
    }
    if (err.status === 404) {
      return t('pairing.error.not_found')
    }
    if (err.status === 409) {
      return t('pairing.error.already_paired')
    }
    if (err.status === 422) {
      return t('pairing.error.malformed')
    }
    if (err.status >= 500) {
      return t('pairing.error.server')
    }
    return t('pairing.error.status', { status: String(err.status) })
  }
  if (err instanceof Error && err.message) {
    // Network / parse failure — keep the message but trim the prefix.
    return err.message.replace(/^Error:\s*/, '')
  }
  return t('pairing.error.generic')
}

// ────────────────────────────────────────────────────────────────
//  socialhome:// URL scheme — encode / decode
// ────────────────────────────────────────────────────────────────

/**
 * ``socialhome://pair#<base64url(JSON)>`` — a single-line, chat-safe
 * pairing string. Payload sits in the URL fragment so a stray paste
 * into a browser address bar (or a URL preview generator) never sends
 * the secret to the receiving instance's server logs — fragments stay
 * client-side. The base64url codec lives in ``@/lib/base64Url``.
 */
export function buildPairingCode(payloadJson: string): string {
  return `socialhome://pair#${base64UrlEncode(payloadJson)}`
}

/**
 * Decode a household pairing string. Accepts:
 *   - ``socialhome://pair#<base64url(JSON)>`` (the new shape)
 *   - Raw multi-field JSON (back-compat for codes already in flight)
 * Returns the parsed payload or ``null`` if it's neither.
 */
function decodePairingCode(raw: string): Record<string, unknown> | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  if (trimmed.startsWith('socialhome://pair#')) {
    const fragment = trimmed.slice('socialhome://pair#'.length)
    const json = base64UrlDecode(fragment)
    if (!json) return null
    try {
      return JSON.parse(json) as Record<string, unknown>
    } catch {
      return null
    }
  }
  // Back-compat: raw JSON payload (what older QR codes encoded).
  if (trimmed.startsWith('{')) {
    try {
      return JSON.parse(trimmed) as Record<string, unknown>
    } catch {
      return null
    }
  }
  return null
}

/**
 * Decode a GFS pairing string. Accepts ``socialhome://gfs-pair/{base_url}
 * ?token={token}`` — the URL the GFS landing page renders.
 *
 * The ``URL`` constructor can't parse non-special schemes' pathname
 * cleanly, so we hand-strip the prefix and re-parse with
 * ``new URL(rest, 'https://placeholder')``.
 */
function decodeGfsCode(raw: string): { gfs_url: string; token: string } | null {
  const trimmed = raw.trim()
  if (!trimmed.startsWith('socialhome://gfs-pair/')) return null
  const rest = trimmed.slice('socialhome://gfs-pair/'.length)
  // ``rest`` is now ``{base_url-without-scheme}?token=…`` — but the QR
  // payload keeps the ``https://`` on the base URL, so peel it off
  // before the query split.
  const qIdx = rest.indexOf('?')
  if (qIdx < 0) return null
  const base = rest.slice(0, qIdx).replace(/\/$/, '')
  const query = rest.slice(qIdx + 1)
  const params = new URLSearchParams(query)
  const token = params.get('token') ?? ''
  if (!base || !token) return null
  // The QR keeps the scheme — make sure ``base`` actually starts with
  // ``http://`` or ``https://`` so the SH service doesn't get tricked
  // into hitting an arbitrary host as if it were a URL.
  if (!/^https?:\/\//.test(base)) return null
  return { gfs_url: base, token }
}

// ────────────────────────────────────────────────────────────────
//  Component-level handles + helpers
// ────────────────────────────────────────────────────────────────

export function openPairing(pairingMode: PairingMode = 'household') {
  mode.value = pairingMode
  open.value = true
  step.value = 'idle'
  role.value = 'unset'
  peerHint.value = null
  verificationCode.value = ''
  sasDigits.value = ['', '', '', '', '', '']
  scannedSas.value = ''
  scanError.value = null
  qrPayload.value = ''
  pairingCode.value = ''
  pairingToken.value = ''
  gfsResultStatus.value = 'active'
}

function SasInput({ autofilled }: { autofilled?: boolean }) {
  const handleDigitInput = (index: number, value: string) => {
    if (!/^\d?$/.test(value)) return
    const next = [...sasDigits.value]
    next[index] = value
    sasDigits.value = next
    verificationCode.value = next.join('')
    if (value && index < 5) {
      const nextInput = document.querySelector(
        `.sh-sas-digit[data-index="${index + 1}"]`,
      ) as HTMLInputElement | null
      nextInput?.focus()
    }
  }

  const handleKeyDown = (index: number, e: KeyboardEvent) => {
    if (e.key === 'Backspace' && !sasDigits.value[index] && index > 0) {
      const prevInput = document.querySelector(
        `.sh-sas-digit[data-index="${index - 1}"]`,
      ) as HTMLInputElement | null
      prevInput?.focus()
    }
  }

  return (
    <div class="sh-sas-input">
      <label>{t('pairing.enter_code')}</label>
      <div class={`sh-sas-digits ${autofilled ? 'sh-sas-digits--autofilled' : ''}`}>
        {sasDigits.value.map((digit, i) => (
          <input
            key={i}
            type="text"
            inputMode="numeric"
            maxLength={1}
            class="sh-sas-digit"
            data-index={i}
            value={digit}
            autoFocus={i === 0 && !autofilled}
            readOnly={autofilled}
            onInput={(e) => handleDigitInput(i, (e.target as HTMLInputElement).value)}
            onKeyDown={(e) => handleKeyDown(i, e as unknown as KeyboardEvent)}
          />
        ))}
      </div>
      {autofilled && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          {t('pairing.sas_autofilled')}
        </p>
      )}
    </div>
  )
}

/**
 * Large, readable SAS digits rendered for the scanner side. The
 * scanner reads these aloud so the inviter can compare them against
 * the auto-filled digits on their screen.
 */
function SasDisplay({ code }: { code: string }) {
  const digits = code.padStart(6, ' ').split('')
  return (
    <div class="sh-sas-display" aria-label={t('pairing.sas_display_label')}>
      {digits.map((d, i) => (
        <span key={i} class="sh-sas-display-digit">{d.trim() || '·'}</span>
      ))}
    </div>
  )
}

/**
 * Step indicator — reflects the inviter flow by default. The scanner
 * flow has its own labels since the middle step is different.
 */
function StepIndicator({ current, role: currentRole }: {
  current: PairingStep
  role: PairingRole
}) {
  const isScanner = currentRole === 'scanner'
  const labels = isScanner
    ? [
        t('pairing.step_start'),
        t('pairing.step_scan'),
        t('pairing.step_verify'),
        t('pairing.step_done'),
      ]
    : [
        t('pairing.step_start'),
        t('pairing.step_show'),
        t('pairing.step_verify'),
        t('pairing.step_done'),
      ]

  const stepIndex = (() => {
    switch (current) {
      case 'idle': return 0
      case 'generating':
      case 'waiting': return 1
      case 'scanning':
      case 'accepting': return 1
      case 'verifying':
      case 'sas-display': return 2
      default: return 3
    }
  })()

  return (
    <ol class="sh-pairing-steps" aria-label={t('pairing.progress_label')}>
      {labels.map((label, i) => (
        <li key={label}
            class={`sh-pairing-step ${i <= stepIndex ? 'sh-pairing-step--done' : ''} ${i === stepIndex ? 'sh-pairing-step--active' : ''}`}>
          <span class="sh-pairing-step-dot" aria-hidden="true">
            {i <= stepIndex ? '✓' : i + 1}
          </span>
          <span class="sh-pairing-step-label">{label}</span>
        </li>
      ))}
    </ol>
  )
}

function PastePanel({
  onSubmit,
  placeholder,
  label,
  mode: pasteMode,
}: {
  onSubmit: (raw: string) => void
  placeholder: string
  label: string
  mode: PairingMode
}) {
  const [value, setValue] = useState('')
  return (
    <div class="sh-scan-paste">
      <label>{label}</label>
      <textarea
        class="sh-textarea"
        rows={pasteMode === 'household' ? 4 : 3}
        placeholder={placeholder}
        value={value}
        onInput={(e) => setValue((e.target as HTMLTextAreaElement).value)}
        autoFocus
      />
      <div class="sh-pairing-actions">
        <Button onClick={() => onSubmit(value.trim())} disabled={!value.trim()}>
          {t('pairing.paste_submit')}
        </Button>
      </div>
    </div>
  )
}

/**
 * Two-method picker: Scan QR card + Paste code card. Equal-weight
 * peers — the "Paste code" path is no longer a buried fallback link
 * but a first-class entry point for remote pairing.
 */
function MethodPicker({
  active,
  onPick,
}: {
  active: ScanMethod
  onPick: (method: ScanMethod) => void
}) {
  return (
    <div class="sh-pairing-method-grid" role="tablist"
         aria-label={t('pairing.scan_intro')}>
      <button
        type="button"
        role="tab"
        aria-selected={active === 'qr'}
        class={`sh-pairing-method-card ${active === 'qr' ? 'sh-pairing-method-card--active' : ''}`}
        onClick={() => onPick('qr')}
      >
        <span class="sh-pairing-method-icon" aria-hidden="true">📷</span>
        <span class="sh-pairing-method-title">{t('pairing.method_qr')}</span>
        <span class="sh-pairing-method-hint">{t('pairing.method_qr_hint')}</span>
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={active === 'paste'}
        class={`sh-pairing-method-card ${active === 'paste' ? 'sh-pairing-method-card--active' : ''}`}
        onClick={() => onPick('paste')}
      >
        <span class="sh-pairing-method-icon" aria-hidden="true">📋</span>
        <span class="sh-pairing-method-title">{t('pairing.method_paste')}</span>
        <span class="sh-pairing-method-hint">{t('pairing.method_paste_hint')}</span>
      </button>
    </div>
  )
}

/**
 * Inviter-side "Or share a code" card — the chat-safe peer of the QR
 * image. Lives next to (desktop) or under (mobile) the QR with an OR
 * divider so it reads as an alternative path, not a fallback.
 */
function ShareCodeCard({ code, onCopy }: { code: string; onCopy: () => void }) {
  return (
    <div class="sh-pairing-code-card">
      <div class="sh-pairing-code-heading">{t('pairing.code_share_heading')}</div>
      <code class="sh-pairing-code-string" aria-label={code}>{code}</code>
      <Button onClick={onCopy} variant="secondary">
        {t('pairing.copy_code')}
      </Button>
      <p class="sh-muted sh-pairing-code-hint">{t('pairing.code_share_hint')}</p>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────
//  Main component
// ────────────────────────────────────────────────────────────────

/** Per-state timeout. Pairing waits indefinitely for the peer at
 *  ``waiting`` (inviter showing QR) and ``sas-display`` (scanner
 *  showing the 6-digit code) — five minutes is plenty for two
 *  people in the same room and avoids leaving the modal "verifying"
 *  forever when the peer never confirms. */
const PAIRING_STEP_TIMEOUT_MS = 5 * 60 * 1000

export function PairingFlow({ onGfsConnected }: { onGfsConnected?: () => void }) {
  onGfsConnectedCb.value = onGfsConnected ?? null
  const loc = useLocation()
  const sasAutofilledRef = useRef(false)
  const [scanMethod, setScanMethod] = useState<ScanMethod>('qr')

  // ── Per-state timeout ─────────────────────────────────────────────
  // Watches ``step.value``; when the user enters one of the open-ended
  // wait states, schedule a single fail-out so the modal doesn't sit
  // forever if the peer ghosts.
  useEffect(() => {
    if (!open.value) return
    if (step.value !== 'waiting' && step.value !== 'sas-display') return
    const timer = setTimeout(() => {
      // Re-check current state before flipping — the user might have
      // advanced past the timed state by the time the timer fires.
      if (step.value === 'waiting' || step.value === 'sas-display') {
        step.value = 'failed'
        peerHint.value = t('pairing.error.timed_out')
      }
    }, PAIRING_STEP_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [step.value, open.value])

  // ── Live updates from the federation layer ─────────────────────────
  useEffect(() => {
    const offAccept = ws.on('pairing.accept_received', (e) => {
      const d = e.data as { token?: string; verification_code?: string }
      if (!open.value || mode.value !== 'household') return
      if (role.value !== 'inviter') return
      if (!pairingToken.value || d.token !== pairingToken.value) return
      if (!d.verification_code) return
      // Auto-fill the 6 digits — saves the user typing when the
      // other device just accepted.
      const digits = d.verification_code.split('')
      if (digits.length === 6) {
        sasDigits.value = digits
        verificationCode.value = d.verification_code
        sasAutofilledRef.current = true
      }
    })
    const offConfirm = ws.on('pairing.confirmed', (e) => {
      const d = e.data as { instance_id?: string; display_name?: string }
      if (!open.value) return
      peerHint.value = d.display_name ?? null
      justPairedInstanceId.value = d.instance_id ?? null
      justPairedDisplayName.value = d.display_name ?? null
      step.value = 'success'
      showToast(t('pairing.successful'), 'success')
    })
    const offAborted = ws.on('pairing.aborted', (e) => {
      const d = e.data as { reason?: string }
      if (!open.value) return
      step.value = 'failed'
      if (d.reason) peerHint.value = d.reason
    })
    return () => { offAccept(); offConfirm(); offAborted() }
  }, [])

  // ── Inviter path ─────────────────────────────────────────────────
  const initiate = async () => {
    role.value = 'inviter'
    step.value = 'generating'
    peerHint.value = null
    sasAutofilledRef.current = false
    try {
      // No body: the server sources the inbox base URL from the platform
      // adapter (HA integration pushes it; standalone reads
      // [standalone].external_url). Returns 422 NOT_CONFIGURED if unset.
      const result = await api.post('/api/pairing/initiate', {}) as {
        token: string; [key: string]: unknown
      }
      const json = JSON.stringify(result)
      qrPayload.value = buildPairingCode(json)
      pairingCode.value = qrPayload.value
      pairingToken.value = result.token
      step.value = 'waiting'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = friendlyPairError(err, 'initiate')
    }
  }

  const verify = async () => {
    step.value = 'verifying'
    try {
      const result = await api.post('/api/pairing/confirm', {
        token: pairingToken.value,
        verification_code: verificationCode.value,
      }) as { instance_id?: string; display_name?: string }
      // Capture peer identity for the configure-sharing step (the WS
      // subscriber may have already done this; set only if not yet set).
      if (result.instance_id) {
        justPairedInstanceId.value = result.instance_id
        justPairedDisplayName.value = result.display_name ?? null
      }
      // Success is dispatched by the WS subscriber above.
      // As a fallback, mark success after the API call resolves:
      if (step.value === 'verifying') step.value = 'success'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = friendlyPairError(err)
    }
  }

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(pairingCode.value)
      showToast(t('pairing.code_copied'), 'success')
    } catch {
      showToast(t('pairing.clipboard_unavailable'), 'error')
    }
  }

  // ── Scanner path ─────────────────────────────────────────────────
  const startScan = () => {
    role.value = 'scanner'
    step.value = 'scanning'
    scanError.value = null
    setScanMethod('qr')
  }

  const handleScanned = async (raw: string) => {
    const parsed = decodePairingCode(raw)
    if (!parsed) {
      scanError.value = t('pairing.scan_invalid_code')
      return
    }
    if (!parsed.token || !parsed.identity_pk || !parsed.dh_pk) {
      scanError.value = t('pairing.scan_wrong_kind')
      return
    }
    step.value = 'accepting'
    try {
      const result = await api.post('/api/pairing/accept', parsed) as {
        verification_code: string
        token: string
      }
      pairingToken.value = result.token
      scannedSas.value = result.verification_code
      step.value = 'sas-display'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = friendlyPairError(err)
    }
  }

  // ── GFS path ─────────────────────────────────────────────────────
  const startGfs = () => {
    role.value = 'scanner'  // GFS is "scan/paste a code that the GFS issued"
    step.value = 'scanning'
    scanError.value = null
    setScanMethod('qr')
  }

  const handleGfsScanned = async (raw: string) => {
    const parsed = decodeGfsCode(raw)
    if (!parsed) {
      scanError.value = t('gfs.invalid_code')
      return
    }
    step.value = 'generating'
    try {
      const conn = await api.post<GfsConnection>('/api/gfs/connections', parsed)
      gfsResultStatus.value = conn.status
      step.value = 'success'
      if (conn.status === 'pending') {
        showToast(t('gfs.pending_toast'), 'info')
      } else {
        showToast(t('gfs.pair_success'), 'success')
      }
      if (onGfsConnectedCb.value) onGfsConnectedCb.value()
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = friendlyPairError(err)
    }
  }

  // ── Shared reset ─────────────────────────────────────────────────
  const resetSas = () => {
    sasDigits.value = ['', '', '', '', '', '']
    verificationCode.value = ''
    sasAutofilledRef.current = false
  }

  const resetAll = () => {
    step.value = 'idle'
    role.value = 'unset'
    resetSas()
    peerHint.value = null
    qrPayload.value = ''
    pairingCode.value = ''
    pairingToken.value = ''
    scannedSas.value = ''
    scanError.value = null
    justPairedInstanceId.value = null
    justPairedDisplayName.value = null
    gfsResultStatus.value = 'active'
    setScanMethod('qr')
  }

  const modalTitle = mode.value === 'gfs' ? t('gfs.modal_title') : t('pairing.title')
  const onPayload = mode.value === 'gfs' ? handleGfsScanned : handleScanned
  const pastePlaceholder = mode.value === 'gfs'
    ? t('gfs.paste_placeholder')
    : t('pairing.paste_placeholder')
  const pasteLabel = mode.value === 'gfs'
    ? t('gfs.paste_label')
    : t('pairing.paste_label')

  return (
    <Modal open={open.value}
           onClose={() => { open.value = false }}
           title={modalTitle}>
      <div class="sh-pairing-flow">
        {mode.value === 'household' && step.value !== 'configure-sharing' && (
          <StepIndicator current={step.value} role={role.value} />
        )}

        {mode.value === 'household' && step.value === 'idle' && (
          <div class="sh-pairing-start">
            <div class="sh-pairing-hero" aria-hidden="true">🔗</div>
            <p class="sh-muted">{t('pairing.intro')}</p>
            <div class="sh-pairing-role-grid">
              <button
                type="button"
                class="sh-pairing-role-card"
                onClick={initiate}
                aria-label={t('pairing.role_show_aria')}
              >
                <span class="sh-pairing-role-icon" aria-hidden="true">🪪</span>
                <span class="sh-pairing-role-title">
                  {t('pairing.role_show')}
                </span>
                <span class="sh-pairing-role-hint">
                  {t('pairing.role_show_hint')}
                </span>
              </button>
              <button
                type="button"
                class="sh-pairing-role-card"
                onClick={startScan}
                aria-label={t('pairing.role_scan_aria')}
              >
                <span class="sh-pairing-role-icon" aria-hidden="true">📷</span>
                <span class="sh-pairing-role-title">
                  {t('pairing.role_scan')}
                </span>
                <span class="sh-pairing-role-hint">
                  {t('pairing.role_scan_hint')}
                </span>
              </button>
            </div>
          </div>
        )}

        {step.value === 'generating' && (
          <div class="sh-pairing-generating">
            <Spinner />
          </div>
        )}

        {/* ── Inviter — QR + share-code card ─────────────────────── */}
        {mode.value === 'household' && step.value === 'waiting' && (
          <div class="sh-pairing-qr">
            <div class="sh-pairing-share">
              <div class="sh-pairing-share-qr">
                <p class="sh-muted">{t('pairing.show_qr')}</p>
                <QrCodeImg data={qrPayload.value} size={220} alt={t('pairing.qr_alt')} />
              </div>
              <div class="sh-pairing-or" aria-hidden="true">
                <span>{t('pairing.or_divider')}</span>
              </div>
              <ShareCodeCard code={pairingCode.value} onCopy={copyCode} />
            </div>
            <div class="sh-pairing-waiting" role="status">
              <span class="sh-pairing-pulse" aria-hidden="true" />
              <span>{t('pairing.waiting')}</span>
            </div>
            <SasInput autofilled={sasAutofilledRef.current} />
            <div class="sh-pairing-actions">
              <Button onClick={verify}
                      disabled={verificationCode.value.length !== 6}>
                {t('pairing.verify')}
              </Button>
              {!sasAutofilledRef.current && (
                <button type="button" class="sh-link" onClick={resetSas}>
                  {t('pairing.clear_code')}
                </button>
              )}
              <button type="button" class="sh-link" onClick={resetAll}>
                {t('pairing.cancel')}
              </button>
            </div>
          </div>
        )}

        {/* ── Scanner / GFS — method picker + active panel ────────── */}
        {step.value === 'scanning' && (
          <div class="sh-pairing-scan">
            {mode.value === 'gfs' && (
              <p class="sh-muted">{t('gfs.scan_intro')}</p>
            )}
            {mode.value === 'household' && (
              <p class="sh-muted">{t('pairing.scan_intro')}</p>
            )}
            <MethodPicker active={scanMethod} onPick={(m) => {
              scanError.value = null
              setScanMethod(m)
            }} />
            {scanMethod === 'qr' && (
              <QrScanner
                onPayload={onPayload}
                onError={(msg) => { scanError.value = msg }}
              />
            )}
            {scanMethod === 'paste' && (
              <PastePanel
                onSubmit={onPayload}
                placeholder={pastePlaceholder}
                label={pasteLabel}
                mode={mode.value}
              />
            )}
            {scanMethod === 'paste' && scanError.value && (
              <p class="sh-scan-error-inline" role="alert">
                {scanError.value}
              </p>
            )}
            <div class="sh-pairing-actions">
              <button type="button" class="sh-link" onClick={resetAll}>
                {t('pairing.back')}
              </button>
            </div>
          </div>
        )}

        {step.value === 'accepting' && mode.value === 'household' && (
          <div class="sh-pairing-accepting">
            <Spinner />
            <p class="sh-muted">{t('pairing.accepting')}</p>
          </div>
        )}

        {/* Scanner — SAS display */}
        {step.value === 'sas-display' && mode.value === 'household' && (
          <div class="sh-pairing-sas">
            <h3 style={{ margin: 0 }}>{t('pairing.sas_heading')}</h3>
            <p class="sh-muted">{t('pairing.sas_instructions')}</p>
            <SasDisplay code={scannedSas.value} />
            <div class="sh-pairing-waiting" role="status">
              <span class="sh-pairing-pulse" aria-hidden="true" />
              <span>{t('pairing.sas_waiting')}</span>
            </div>
            <div class="sh-pairing-actions">
              <button type="button" class="sh-link" onClick={resetAll}>
                {t('pairing.cancel')}
              </button>
            </div>
          </div>
        )}

        {step.value === 'verifying' && <Spinner />}

        {step.value === 'success' && mode.value === 'household' && (
          <div class="sh-pairing-success">
            <div class="sh-pairing-success-burst" aria-hidden="true">
              <span>✓</span>
            </div>
            <h3 style={{ margin: 0 }}>{t('pairing.success')}</h3>
            <p class="sh-muted">
              {peerHint.value
                ? t('pairing.success_named').replace('{peer}', peerHint.value)
                : t('pairing.success_message')}
            </p>
            <Button onClick={() => { step.value = 'configure-sharing' }}>
              {t('pairing.done')}
            </Button>
          </div>
        )}

        {step.value === 'configure-sharing' && (
          <div class="sh-pairing-configure-sharing">
            <h3>{t('pairing.configure_sharing_title')}</h3>
            <p class="sh-muted">{t('pairing.configure_sharing_intro')}</p>
            {justPairedInstanceId.value && (
              <ShareHomeToggle
                instanceId={justPairedInstanceId.value}
                peerName={justPairedDisplayName.value || justPairedInstanceId.value}
                initialValue={true}
              />
            )}
            <Button onClick={() => { open.value = false }}>
              {t('pairing.done')}
            </Button>
          </div>
        )}

        {step.value === 'failed' && (
          <div class="sh-pairing-failed">
            <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
            <h3 style={{ margin: 0 }}>{t('pairing.failed')}</h3>
            <p class="sh-muted">
              {peerHint.value ?? t('pairing.failed_message')}
            </p>
            <Button onClick={resetAll}>{t('pairing.retry')}</Button>
          </div>
        )}

        {/* ── GFS mode ─────────────────────────────────────────── */}
        {mode.value === 'gfs' && step.value === 'idle' && (
          <div class="sh-pairing-start">
            <p class="sh-gfs-url-intro sh-muted">{t('gfs.modal_intro')}</p>
            <div class="sh-pairing-actions">
              <Button onClick={startGfs}>{t('gfs.add')}</Button>
            </div>
          </div>
        )}

        {mode.value === 'gfs' && step.value === 'success' && (
          gfsResultStatus.value === 'pending' ? (
            <div class="sh-pairing-success">
              <div class="sh-pairing-success-burst" aria-hidden="true">
                <span>✓</span>
              </div>
              <h3 style={{ margin: 0 }}>{t('gfs.connected_pending')}</h3>
              <p class="sh-muted">{t('gfs.pending_approval')}</p>
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
                <Button onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
              </div>
            </div>
          ) : (
            <div class="sh-pairing-success">
              <div class="sh-pairing-success-burst" aria-hidden="true">
                <span>✓</span>
              </div>
              <h3 style={{ margin: 0 }}>{t('gfs.connected')}</h3>
              <p class="sh-muted">{t('gfs.pair_success')}</p>
              <p class="sh-muted">{t('gfs.next_steps')}</p>
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
                <Button variant="secondary"
                        onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
                <Button onClick={() => {
                  // Base/ingress-aware client-side nav — never location.assign
                  // to a raw absolute path (escapes the ingress prefix +
                  // forces a full reload). See CLAUDE.md "Frontend & ingress".
                  open.value = false
                  loc.route('/momentum/public/sharing')
                }}>
                  {t('gfs.open_publishing')}
                </Button>
              </div>
            </div>
          )
        )}
      </div>
    </Modal>
  )
}
