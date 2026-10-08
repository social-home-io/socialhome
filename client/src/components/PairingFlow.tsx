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
import { gfsConnectErrorText } from '@/features/connections/gfsErrors'
import { ShareHomeToggle } from './ShareHomeToggle'
import { QrCodeImg } from './QrCodeImg'
import { QrScanner } from './QrScanner'

type PairingMode = 'household' | 'gfs'
type PairingRole = 'unset' | 'inviter' | 'scanner'
type ScanMethod = 'qr' | 'paste'
/** How the scanner reaches the code owner (``POST /api/pairing/initiate``
 *  ``reach``): its direct URL, the URL with a GFS fallback, or only a GFS. */
export type PairingReach = 'url' | 'url_gfs' | 'gfs'
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

// ── Reach picker (inviter) ─────────────────────────────────────────────
/** Active GFS connections that proved ``envelope_relay`` — the only ones a
 *  ``url_gfs`` / ``gfs`` code can name. Empty → no picker (classic code). */
const relayGfs = signal<GfsConnection[]>([])
/** Whether this household has a federation URL; ``null`` = unknown (the
 *  lookup failed), treated as "has one" so nothing is disabled on a guess. */
const hasUrl = signal<boolean | null>(null)
const reach = signal<PairingReach>('url')
const reachGfsId = signal('')
/** Bumped per load so a slow answer for an earlier open can't land late. */
let reachLoadSeq = 0
/** Scanner: host of the GFS a scanned ``url_gfs`` / ``gfs`` code uses. */
const scannedGfsHost = signal<string | null>(null)

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
function friendlyPairError(
  err: unknown,
  stage?: 'initiate',
  gfsHost?: string | null,
): string {
  if (err instanceof ApiError) {
    // The GFS-reach refusals carry their own code — never "malformed",
    // never the missing-URL hint: each has a different way out.
    switch (err.code) {
      case 'GFS_NOT_CONNECTED':
        return t('pairing.error.gfs_not_connected')
      case 'GFS_NOT_SHARED':
        return gfsHost
          ? t('pairing.error.gfs_not_shared', { host: gfsHost })
          : t('pairing.error.gfs_not_shared_generic')
      case 'KEYWRAP_INVALID':
        return t('pairing.error.keywrap_invalid')
      case 'INVALID_REACH':
        return t('pairing.error.invalid_reach')
    }
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

/** Host of a scanned code's bootstrap GFS when the code asks to be
 *  reached through one (``reach`` ``url_gfs`` / ``gfs``), else ``null``.
 *  Codes from before reach existed carry neither field → ``null``. */
export function pairingCodeGfsHost(payload: Record<string, unknown>): string | null {
  if (payload.reach !== 'url_gfs' && payload.reach !== 'gfs') return null
  const gfs = payload.gfs
  if (!gfs || typeof gfs !== 'object') return null
  const url = (gfs as { url?: unknown }).url
  if (typeof url !== 'string' || !url) return null
  try {
    return new URL(url).host || null
  } catch {
    return null
  }
}

/** ``GET /api/gfs/connections`` + ``GET /api/admin/federation/external-url``
 *  for the reach picker. Best-effort: any failure leaves the picker hidden
 *  (no relay GFS) or every option enabled (unknown URL) — today's flow. */
async function loadReachOptions(): Promise<void> {
  const seq = ++reachLoadSeq
  relayGfs.value = []
  hasUrl.value = null
  reach.value = 'url'
  reachGfsId.value = ''
  let conns: unknown = null
  let ext: unknown = null
  try {
    conns = await api.get<GfsConnection[]>('/api/gfs/connections')
  } catch {
    conns = null
  }
  try {
    ext = await api.get('/api/admin/federation/external-url')
  } catch {
    ext = null
  }
  if (seq !== reachLoadSeq) return
  const eligible = Array.isArray(conns)
    ? (conns as GfsConnection[]).filter(c => c.status === 'active' && c.envelope_relay === true)
    : []
  const known = ext !== null && typeof ext === 'object'
    ? Boolean((ext as { effective?: unknown }).effective)
    : null
  hasUrl.value = known
  reachGfsId.value = eligible[0]?.id ?? ''
  // No URL → "GFS only" is the one option that can work; otherwise the
  // direct URL stays the default (it is the private one).
  reach.value = eligible.length > 0 && known === false ? 'gfs' : 'url'
  relayGfs.value = eligible
}

/** ``https://gfs.example`` → ``gfs.example`` for labels; the raw value
 *  when it isn't a URL. */
function gfsLabel(c: GfsConnection): string {
  if (c.display_name) return c.display_name
  try {
    return new URL(c.inbox_url).host || c.inbox_url
  } catch {
    return c.inbox_url
  }
}

/**
 * Inviter-side "How can they reach you?" radio group — rendered only when
 * this household has an active GFS connection that relays envelopes.
 * Without a URL the two URL options are disabled with the same
 * how-to-add-a-URL hint the 422 path shows, and "GFS only" is preselected.
 */
function ReachPicker() {
  const conns = relayGfs.value
  const urlMissing = hasUrl.value === false
  const options: { value: PairingReach; label: string; needsUrl: boolean }[] = [
    { value: 'url', label: t('pairing.reach.url'), needsUrl: true },
    { value: 'url_gfs', label: t('pairing.reach.url_gfs'), needsUrl: true },
    { value: 'gfs', label: t('pairing.reach.gfs'), needsUrl: false },
  ]
  const usesGfs = reach.value !== 'url'
  return (
    <fieldset class="sh-pairing-reach" aria-describedby="sh-pairing-reach-hint">
      <legend class="sh-pairing-reach__legend">{t('pairing.reach.legend')}</legend>
      <p id="sh-pairing-reach-hint" class="sh-pairing-reach__hint">
        {t('pairing.reach.applies')}
      </p>
      <div class="sh-pairing-reach__options">
        {options.map(o => {
          const disabled = o.needsUrl && urlMissing
          return (
            <label key={o.value}
                   class={`sh-pairing-reach__opt${reach.value === o.value ? ' is-on' : ''}${disabled ? ' is-disabled' : ''}`}>
              <input type="radio" name="sh-pairing-reach" value={o.value}
                     checked={reach.value === o.value}
                     disabled={disabled}
                     onChange={() => { reach.value = o.value }} />
              <span>{o.label}</span>
            </label>
          )
        })}
      </div>
      {urlMissing && (
        <p class="sh-pairing-reach__hint" data-testid="pairing-reach-no-url">
          {notConfiguredHint()}
        </p>
      )}
      {usesGfs && conns.length > 1 && (
        <label class="sh-pairing-reach__gfs">
          <span>{t('pairing.reach.gfs_select')}</span>
          <select value={reachGfsId.value}
                  onChange={(e) => { reachGfsId.value = (e.target as HTMLSelectElement).value }}>
            {conns.map(c => (
              <option key={c.id} value={c.id}>{gfsLabel(c)}</option>
            ))}
          </select>
        </label>
      )}
      {usesGfs && (
        <p class="sh-pairing-reach__note" role="note">
          {t('pairing.reach.gfs_warning')}
        </p>
      )}
    </fieldset>
  )
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
  scannedGfsHost.value = null
  relayGfs.value = []
  if (pairingMode === 'household') void loadReachOptions()
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
  const failedPanelRef = useRef<HTMLDivElement | null>(null)
  const startPanelRef = useRef<HTMLDivElement | null>(null)

  // ── Failure panel focus ───────────────────────────────────────────
  // The Modal focuses its first control on open only; when the flow
  // flips to ``failed`` mid-way, focus would otherwise stay on the ✕.
  // Move it to Retry — the panel's one control — so keyboard and
  // screen-reader users land on the way out. The panel itself is
  // ``role="alert"`` so the verdict is announced.
  const isFailed = step.value === 'failed'
  useEffect(() => {
    if (!isFailed) return
    failedPanelRef.current?.querySelector('button')?.focus()
  }, [isFailed])

  // ── Start-step focus after Retry / Back / Cancel ───────────────────
  // Those controls unmount the panel that held focus (the failure
  // panel's Retry, the scanner's Back, the QR card's Cancel), so
  // ``document.activeElement`` would fall to ``body`` — the trap still
  // keeps Tab in the dialog, but a screen-reader user hears nothing.
  // Land on the start step's first control instead: the "Add GFS"
  // button, or the first role card in household mode. Only on a
  // step → idle transition while the modal stays open — on a fresh
  // open the Modal already focuses its first control and ``openPairing``
  // resets the step at the same time, so the previous step is cleared
  // whenever the modal is closed.
  const isIdle = step.value === 'idle'
  const isOpen = open.value
  const prevStepRef = useRef<PairingStep | null>(null)
  useEffect(() => {
    const prev = prevStepRef.current
    prevStepRef.current = isOpen ? step.value : null
    if (!isOpen || !isIdle || prev === null || prev === 'idle') return
    startPanelRef.current?.querySelector('button')?.focus()
  }, [isIdle, isOpen])

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
      // The server sources the inbox base URL from the platform adapter
      // (HA integration pushes it; standalone reads
      // [standalone].external_url) — 422 NOT_CONFIGURED if unset. With no
      // relay-capable GFS there is no picker and the body stays empty:
      // the classic ``url`` code.
      const body: Record<string, string> = {}
      if (relayGfs.value.length > 0) {
        body.reach = reach.value
        if (reach.value !== 'url' && reachGfsId.value) body.gfs_id = reachGfsId.value
      }
      const result = await api.post('/api/pairing/initiate', body) as {
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
    scannedGfsHost.value = pairingCodeGfsHost(parsed)
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
      peerHint.value = friendlyPairError(err, undefined, scannedGfsHost.value)
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
      // GFS words, not the household-pairing table: a 422 here is the
      // GFS refusing, never "the pairing code looks malformed".
      peerHint.value = gfsConnectErrorText(err)
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
    scannedGfsHost.value = null
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
          <div class="sh-pairing-start" ref={startPanelRef}>
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
            {relayGfs.value.length > 0 && <ReachPicker />}
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
              <QrScanner onPayload={onPayload} />
            )}
            {scanMethod === 'paste' && (
              <PastePanel
                onSubmit={onPayload}
                placeholder={pastePlaceholder}
                label={pasteLabel}
                mode={mode.value}
              />
            )}
            {/* The flow's own decode verdict ("not a GFS code", "wrong
                kind of code") — shown whichever way the code came in.
                The scanner panel renders only its camera / image errors. */}
            {scanError.value && (
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
            {scannedGfsHost.value && (
              <p class="sh-muted sh-pairing-via-gfs">
                {t('pairing.scan.uses_gfs', { host: scannedGfsHost.value })}
              </p>
            )}
          </div>
        )}

        {/* Scanner — SAS display */}
        {step.value === 'sas-display' && mode.value === 'household' && (
          <div class="sh-pairing-sas">
            <h3 style={{ margin: 0 }}>{t('pairing.sas_heading')}</h3>
            <p class="sh-muted">{t('pairing.sas_instructions')}</p>
            <SasDisplay code={scannedSas.value} />
            {scannedGfsHost.value && (
              <p class="sh-muted sh-pairing-via-gfs">
                {t('pairing.scan.uses_gfs', { host: scannedGfsHost.value })}
              </p>
            )}
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
          <div class="sh-pairing-failed" role="alert" ref={failedPanelRef}>
            <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
            <h3 style={{ margin: 0 }}>
              {mode.value === 'gfs' ? t('gfs.pair_failed') : t('pairing.failed')}
            </h3>
            <p class="sh-muted">
              {peerHint.value ?? t('pairing.failed_message')}
            </p>
            <Button onClick={resetAll}>{t('pairing.retry')}</Button>
          </div>
        )}

        {/* ── GFS mode ─────────────────────────────────────────── */}
        {mode.value === 'gfs' && step.value === 'idle' && (
          <div class="sh-pairing-start" ref={startPanelRef}>
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
