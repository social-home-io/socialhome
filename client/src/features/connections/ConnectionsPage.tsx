/**
 * ConnectionsPage — federation connections (§23.86, §11).
 *
 * Three sections:
 *   1. Incoming auto-pair requests — admin inbox populated when one
 *      of our paired peers has introduced a new household to us.
 *      One click approves (no QR or SAS; B's vouch signature takes
 *      the place of the out-of-band verification).
 *   2. Households (HFS) with a polished paired-peer list + the
 *      outgoing "pair via a trusted peer" flow.
 *   3. Global Federation Servers.
 */
import { useEffect, useState, lazy, Suspense } from 'preact/compat'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AutoPairDialog, openAutoPair } from '@/components/AutoPairDialog'
import { ConnectionDetail } from '@/components/ConnectionDetail'
import { showToast } from '@/components/Toast'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  connections,
  selfLat,
  selfLon,
  type Connection,
} from '@/store/connections'
import {
  compatPeers,
  compatOurs,
  loadFederationCompat,
  peersBehindCount,
  type CompatPeer,
} from '@/store/federationCompat'

import { useTitle } from '@/store/pageTitle'
import type { GfsConnection } from '@/types'
import { t } from '@/i18n/i18n'
import { isSupervisorAddon } from '@/platform'
import { confirmDialog } from '@/components/confirm'
import { relativeDocsTime } from '@/utils/relativeTime'

const FederationMap = lazy(() => import('./FederationMap'))

interface AutoPairRequest {
  request_id: string
  from_a_id: string
  from_a_display: string
  via_b_id: string
  via_b_display: string
  ts: string
  received_at: string
}

const gfsConnections = signal<GfsConnection[]>([])
const autoPairRequests = signal<AutoPairRequest[]>([])
const loading = signal(true)
const gfsLoading = signal(true)
const disconnectTarget = signal<GfsConnection | null>(null)


/** Re-pair-class auth failures: the GFS no longer recognizes this home,
 *  so reconnecting can never succeed without re-pairing. */
const GFS_REPAIR_ERRORS = new Set(['unknown-instance', 'bad-signature'])

interface GfsLiveState {
  /** Full ``sh-status-dot …`` class for the leading dot. */
  dotClass: string
  /** i18n key for the human label, or null to render no label. */
  labelKey: string | null
  /** CSS class for the label span (warning vs muted), or null. */
  labelClass: string | null
}

/** Map a GFS connection's PAIRING status + LIVE WS health to what the
 *  card should show. ``status`` is the stored pairing state; ``connected``
 *  / ``last_error`` are the supervisor's live signal. A stored
 *  ``status='active'`` whose socket is down must NOT read as connected —
 *  ``last_error`` is GFS-controlled and is only ever surfaced via these
 *  mapped, translated strings, never rendered verbatim. */
function gfsLiveState(gfs: GfsConnection): GfsLiveState {
  if (gfs.status === 'pending')
    return {
      dotClass: 'sh-status-dot sh-status-dot--pending',
      labelKey: 'gfs.status_pending',
      labelClass: 'sh-text-warning',
    }
  if (gfs.status === 'suspended')
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_suspended',
      labelClass: 'sh-muted',
    }
  // status === 'active' — defer to live WS liveness.
  if (gfs.connected)
    return {
      dotClass: 'sh-status-dot sh-status-dot--active',
      labelKey: 'gfs.status_connected',
      labelClass: 'sh-muted',
    }
  if (gfs.last_error && GFS_REPAIR_ERRORS.has(gfs.last_error))
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_repair_needed',
      labelClass: 'sh-text-warning',
    }
  if (gfs.last_error === 'ts-skew')
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_clock_skew',
      labelClass: 'sh-text-warning',
    }
  // Transient / reconnecting (any other reason, or none recorded yet).
  return {
    dotClass: 'sh-status-dot sh-status-dot--pending',
    labelKey: 'gfs.status_reconnecting',
    labelClass: 'sh-muted',
  }
}

function hfsStatusDotClass(conn: Connection): string {
  if (!conn.reachable) return 'sh-status-dot sh-status-dot--unreachable'
  if (conn.status === 'confirmed') return 'sh-status-dot sh-status-dot--active'
  return 'sh-status-dot sh-status-dot--pending'
}

function transportIcon(t: Connection['transport']) {
  if (t === 'rtc') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--rtc"
        title="Direct connection — low latency"
        aria-label="Direct (WebRTC)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />
        </svg>
      </span>
    )
  }
  if (t === 'https') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--https"
        title="Via HTTPS — works, but slower than direct"
        aria-label="Via HTTPS (fallback)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M19 18H6a4 4 0 010-8 5 5 0 019.6-2A4 4 0 0119 18z" />
        </svg>
      </span>
    )
  }
  return (
    <span class="sh-transport-icon" aria-hidden="true" />
  )
}

/**
 * Compatibility badge for a confirmed household, derived from its matched
 * CompatPeer. Mirrors AdminPage's `_compatStatus` three-state logic but kept
 * local to this surface (no cross-feature import):
 *   * no compat row yet            → nothing.
 *   * caps not yet learned         → "version unknown".
 *   * caps known, none lacking     → "up to date ✓".
 *   * caps known, N features short → "N behind" (titled with the list).
 */
function compatBadge(peer: CompatPeer | undefined) {
  if (peer === undefined) return null
  if (!peer.capabilities_known) {
    return <span class="sh-chip sh-chip--muted">version unknown</span>
  }
  if (peer.lacking_features.length === 0) {
    return <span class="sh-chip sh-chip--success">up to date ✓</span>
  }
  return (
    <span class="sh-chip sh-chip--update" title={peer.lacking_features.join(', ')}>
      {peer.lacking_features.length} behind
    </span>
  )
}

async function loadConnections() {
  loading.value = true
  try {
    connections.value = await api.get('/api/connections') as Connection[]
  } catch {
    connections.value = []
  }
  loading.value = false
}

async function loadSelfHome() {
  try {
    const j = await api.get('/api/friends') as {
      instance?: { home_lat?: number | null; home_lon?: number | null }
    }
    if (j.instance?.home_lat != null && j.instance?.home_lon != null) {
      selfLat.value = j.instance.home_lat
      selfLon.value = j.instance.home_lon
    }
  } catch {
    // Non-fatal — the federation map just won't show the "You" pin
    // until a `local.home_changed` WS frame arrives. The List view
    // doesn't need this data.
  }
}

async function loadGfsConnections() {
  gfsLoading.value = true
  try {
    gfsConnections.value = await api.get('/api/gfs/connections') as GfsConnection[]
  } catch {
    gfsConnections.value = []
  }
  gfsLoading.value = false
}

async function loadAutoPairRequests() {
  try {
    autoPairRequests.value = await api.get(
      '/api/pairing/auto-pair-requests',
    ) as AutoPairRequest[]
  } catch {
    autoPairRequests.value = []
  }
}

async function approveAutoPair(r: AutoPairRequest) {
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/approve`, {},
    )
    showToast(`Paired with ${r.from_a_display}`, 'success')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      `Approve failed: ${(err as Error).message ?? err}`, 'error',
    )
  }
}

async function declineAutoPair(r: AutoPairRequest) {
  if (!await confirmDialog(`Decline ${r.from_a_display}'s pairing request?`, { destructive: true })) return
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/decline`,
      {},
    )
    showToast('Request declined', 'info')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      `Decline failed: ${(err as Error).message ?? err}`, 'error',
    )
  }
}

async function disconnectGfs(gfs: GfsConnection) {
  try {
    await api.delete(`/api/gfs/connections/${gfs.id}`)
    showToast(t('gfs.disconnect'), 'success')
    gfsConnections.value = gfsConnections.value.filter(c => c.id !== gfs.id)
  } catch (e: unknown) {
    showToast((e as Error).message || t('gfs.pairing_failed'), 'error')
  }
  disconnectTarget.value = null
}

async function unpair(instanceId: string) {
  if (!await confirmDialog('Unpair this household? You will lose access to its spaces.', { destructive: true })) return
  try {
    await api.delete(`/api/pairing/connections/${instanceId}`)
    showToast('Unpaired', 'info')
    await loadConnections()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Unpair failed', 'error')
  }
}


interface IceOverview {
  servers: { urls: string[]; kinds: string[]; has_credentials: boolean }[]
  has_turn: boolean
  turn_usable: boolean
  pulls_from_home_assistant: boolean
}

/**
 * DiagnosticsDownload — one file to attach to a bug report.
 *
 * Sits under the connection-servers panel as a plain link-weight action
 * rather than a button: an operator reaches for it once, when asked to,
 * and it should not look like something to click casually.
 *
 * Fetched through the ``api`` client (so it carries auth and respects the
 * ingress base) and saved via a Blob, because a bare ``<a download>``
 * pointing at the endpoint would be an unauthenticated navigation.
 *
 * The copy states plainly what is in it, because "diagnostics" invites
 * the reasonable worry that it might contain messages or keys.
 */
function DiagnosticsDownload() {
  const busy = useSignal(false)

  const download = async () => {
    busy.value = true
    try {
      const data = await api.get('/api/admin/diagnostics')
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      a.download = `socialhome-diagnostics-${stamp}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Revoke on the next tick — revoking synchronously can cancel the
      // download in some browsers before it has read the blob.
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch {
      showToast('Could not build the diagnostics file.', 'error')
    } finally {
      busy.value = false
    }
  }

  return (
    <p class="sh-diagnostics-row sh-muted">
      <button
        type="button"
        class="sh-diagnostics-link"
        disabled={busy.value}
        onClick={() => void download()}
      >
        {busy.value ? 'Preparing…' : 'Download diagnostics'}
      </button>
      <span class="sh-diagnostics-note">
        Connection state, peer reachability times and delivery backlog —
        no messages, names, keys or locations. Safe to attach to a bug
        report.
      </span>
    </p>
  )
}

/**
 * IceServersPanel — what the federation transport uses to punch through
 * NAT, secrets stripped.
 *
 * Deliberately a collapsed disclosure rather than a visible block: an
 * operator needs this roughly once, when federation won't connect. But
 * when they do need it, the alternative today is reading a log warning
 * they have probably never seen — a missing or credential-less TURN entry
 * degrades RTC to slow HTTPS in complete silence. Collapsed keeps it out
 * of the way while making the answer one click away.
 *
 * The summary line carries the conclusion so the detail is optional: an
 * operator can tell at a glance whether a relay is in play.
 */
function IceServersPanel() {
  // Per-mount state, not module-level: the list changes underneath us (an
  // HA pull replaces it daily), so a panel reopened after navigating away
  // must refetch rather than show whatever it saw last time.
  const iceOpen = useSignal(false)
  const iceLoaded = useSignal(false)
  const iceServers = useSignal<IceOverview | null>(null)
  const data = iceServers.value
  const panelId = 'sh-ice-servers-panel'

  const loadIceServers = async () => {
    iceLoaded.value = false
    try {
      iceServers.value = await api.get(
        '/api/admin/federation/ice-servers',
      ) as IceOverview
    } catch {
      iceServers.value = null
    } finally {
      iceLoaded.value = true
    }
  }

  const summary = !iceLoaded.value
    ? ''
    : data === null
      ? 'unavailable'
      : data.turn_usable
        ? 'relay ready'
        : data.has_turn
          ? 'relay not usable'
          : 'direct only'

  return (
    <div class="sh-ice-panel">
      <button
        type="button"
        class="sh-ice-panel__toggle"
        aria-expanded={iceOpen.value}
        aria-controls={panelId}
        onClick={() => {
          iceOpen.value = !iceOpen.value
          if (iceOpen.value) void loadIceServers()
        }}
      >
        <span class="sh-ice-panel__caret" aria-hidden="true">
          {iceOpen.value ? '▾' : '▸'}
        </span>
        Connection servers
        {summary && (
          <span class={`sh-ice-panel__badge sh-ice-panel__badge--${
            summary === 'relay ready'
              ? 'ok'
              : summary === 'direct only' || summary === 'relay not usable'
                ? 'warn'
                : 'muted'
          }`}>
            {summary}
          </span>
        )}
      </button>
      {iceOpen.value && (
        <div id={panelId} class="sh-ice-panel__body">
          {!iceLoaded.value && <p class="sh-muted">Loading…</p>}
          {iceLoaded.value && data === null && (
            <p class="sh-muted">Couldn’t read the server list.</p>
          )}
          {iceLoaded.value && data !== null && (
            <>
              {data.servers.length === 0 ? (
                <p class="sh-muted">
                  No connection servers configured — households behind most
                  home routers won’t be able to reach each other directly.
                </p>
              ) : (
                <ul class="sh-ice-list">
                  {data.servers.map(s => (
                    <li key={s.urls.join(',')} class="sh-ice-list__item">
                      <code>{s.urls.join(', ')}</code>
                      <span class="sh-muted sh-ice-list__note">
                        {s.kinds.includes('turn') || s.kinds.includes('turns')
                          ? s.has_credentials
                            ? 'relay · signed in'
                            : 'relay · no credentials'
                          : 'address lookup'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <p class="sh-muted sh-ice-panel__hint">
                {!data.has_turn
                  ? 'Only address lookup (STUN) is set up. That is enough for'
                    + ' most home networks, but not for stricter ones — add a'
                    + ' relay (TURN) if pairing connects but stays slow.'
                  : !data.turn_usable
                    ? 'A relay is listed but has no credentials, so it will be'
                      + ' rejected and connections quietly fall back to the'
                      + ' slow path. Check the relay secret.'
                    : 'A relay is available, so households behind strict'
                      + ' routers can still reach each other.'}
                {data.pulls_from_home_assistant
                  && ' This list comes from Home Assistant and refreshes daily.'}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}


//: Module-level like ``autoPairRequests`` above, so the loader can be a
//: plain function rather than a component-scoped closure the effect would
//: have to list as a dependency.
const extUrlStored = signal<string | null>(null)
const extUrlEffective = signal<string | null>(null)
const extUrlSource = signal<string | null>(null)
const extUrlDraft = signal('')
const extUrlBusy = signal(false)
const extUrlLoaded = signal(false)

async function loadExternalUrl() {
  try {
    const r = await api.get('/api/admin/federation/external-url') as {
      base: string | null; effective: string | null; source: string | null
    }
    extUrlStored.value = r.base
    extUrlEffective.value = r.effective
    extUrlSource.value = r.source
    extUrlDraft.value = r.base ?? ''
  } catch {
    // Non-fatal — the rest of the page is still useful without it.
  } finally {
    extUrlLoaded.value = true
  }
}

/**
 * ExternalUrlSection — the federation inbox base URL peers POST to.
 *
 * Admin-only, and hidden under the Supervisor add-on (haos): there the
 * companion Home Assistant integration owns this value, and a
 * hand-typed URL would point at an inbox path only that integration
 * registers inside Home Assistant — so offering the field would invite
 * an unreachable address rather than fix one.
 *
 * `effective` is shown alongside the stored value because the two differ when
 * an automatic source is also present (`socialhome.toml` under
 * standalone, the integration under ha). Without it, an admin cannot
 * tell "I typed something" apart from "it is in effect".
 */
function ExternalUrlSection() {
  useEffect(() => { void loadExternalUrl() }, [])

  const save = async () => {
    extUrlBusy.value = true
    try {
      const body = extUrlDraft.value.trim() ? { base: extUrlDraft.value.trim() } : { base: null }
      const r = await api.put('/api/admin/federation/external-url', body) as {
        base: string | null; changed: boolean; peers_notified: number
      }
      extUrlStored.value = r.base
      await loadExternalUrl()
      showToast(
        r.base === null
          ? 'External URL cleared.'
          : r.peers_notified > 0
            ? `External URL saved — ${r.peers_notified} paired household(s) notified.`
            : 'External URL saved.',
        'success',
      )
    } catch (err) {
      showToast(
        err instanceof Error && /422/.test(err.message)
          ? 'That needs to be a full http(s) URL, e.g. https://home.example.com'
          : 'Could not save the external URL.',
        'error',
      )
    } finally {
      extUrlBusy.value = false
    }
  }

  if (!extUrlLoaded.value) return null

  const dirty = extUrlDraft.value.trim() !== (extUrlStored.value ?? '')

  return (
    <section class="sh-connections-section sh-external-url-section">
      <div class="sh-section-header">
        <div class="sh-section-header__title">
          <h2>External URL</h2>
        </div>
      </div>
      <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
        The address other households reach this Social Home at. Pairing
        needs it — without one, generating a pairing code fails.
      </p>
      <div class="sh-external-url-row">
        <label class="sh-external-url-label" for="sh-external-url">
          Base URL
        </label>
        <input
          id="sh-external-url"
          class="sh-input"
          type="url"
          inputMode="url"
          autocomplete="off"
          placeholder="https://home.example.com"
          value={extUrlDraft.value}
          disabled={extUrlBusy.value}
          onInput={(e) => { extUrlDraft.value = (e.target as HTMLInputElement).value }}
        />
        <Button onClick={() => void save()} disabled={extUrlBusy.value || !dirty}>
          {extUrlBusy.value ? 'Saving…' : 'Save'}
        </Button>
      </div>
      {extUrlEffective.value ? (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          Peers currently POST to <code>{extUrlEffective.value}</code>
          {extUrlSource.value === 'auto' && ' (from this deployment’s configuration)'}
          .
        </p>
      ) : (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          Not configured yet — pairing will fail until this is set.
        </p>
      )}
      {extUrlStored.value && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          Clear the field and save to go back to this deployment’s own
          configuration.
        </p>
      )}
      <IceServersPanel />
      <DiagnosticsDownload />
    </section>
  )
}

export default function ConnectionsPage() {
  useTitle(t('connections.title'))
  const [autoPairBusy, setAutoPairBusy] = useState(false)
  const [detail, setDetail] = useState<Connection | null>(null)
  /** Active view toggle — resets to 'list' on mount. */
  const view = useSignal<'list' | 'map'>('list')

  useEffect(() => {
    void loadConnections()
    void loadSelfHome()
    void loadGfsConnections()
    void loadFederationCompat()
    if (currentUser.value?.is_admin) void loadAutoPairRequests()

    const off1 = ws.on('pairing.confirmed', () => {
      void loadConnections()
    })
    const off2 = ws.on('pairing.aborted', () => {
      void loadConnections()
    })
    const off3 = ws.on('pairing.auto_pair_requested', () => {
      if (currentUser.value?.is_admin) void loadAutoPairRequests()
    })
    const off4 = ws.on('peer.transport_changed', (msg) => {
      const { instance_id, transport } = msg.data as { instance_id: string; transport: 'rtc' | 'https' }
      connections.value = connections.value.map(c =>
        c.instance_id === instance_id
          ? { ...c, transport }
          : c,
      )
    })
    return () => { off1(); off2(); off3(); off4() }
  }, [])

  const confirmed = connections.value.filter(c => c.status === 'confirmed')
  const pending = connections.value.filter(c => c.status !== 'confirmed')
  const isAdmin = !!currentUser.value?.is_admin
  const compatByInstance = new Map(compatPeers.value.map(p => [p.instance_id, p]))

  return (
    <div class="sh-connections">

      {/* ── List / Map toggle ─────────────────────────────────────── */}
      <div class="sh-connections-view-toggle">
        <div class="sh-shopping-grouptoggle" role="group" aria-label="View">
          <button
            type="button"
            class={view.value === 'list' ? 'sh-chip sh-chip--active' : 'sh-chip'}
            aria-pressed={view.value === 'list'}
            onClick={() => { view.value = 'list' }}
          >
            List
          </button>
          <button
            type="button"
            class={view.value === 'map' ? 'sh-chip sh-chip--active' : 'sh-chip'}
            aria-pressed={view.value === 'map'}
            onClick={() => { view.value = 'map' }}
          >
            Map
          </button>
        </div>
      </div>

      {/* ── Federation Map ────────────────────────────────────────── */}
      {view.value === 'map' && (
        <Suspense fallback={<div class="sh-federation-map__loading">Loading map…</div>}>
          <FederationMap />
        </Suspense>
      )}

      {view.value === 'list' && (
      <>
      {/* ── Incoming auto-pair requests (admin-only inbox) ─────────── */}
      {isAdmin && autoPairRequests.value.length > 0 && (
        <section class="sh-auto-pair-inbox">
          <h2>Pair requests</h2>
          <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
            One of your trusted peers has introduced a new household.
            Approving pairs you instantly — the vouch signature
            replaces the QR scan.
          </p>
          {autoPairRequests.value.map(r => (
            <div key={r.request_id} class="sh-auto-pair-request">
              <div class="sh-auto-pair-request-body">
                <div>
                  <strong>{r.from_a_display}</strong>
                  <span class="sh-muted"
                        style={{ fontSize: 'var(--sh-font-size-sm)' }}>
                    {' '}wants to pair with you
                  </span>
                </div>
                <div class="sh-muted"
                     style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                  Vouched for by <strong>{r.via_b_display}</strong>
                  {' · '}
                  <time
                    dateTime={r.received_at}
                    title={new Date(r.received_at).toLocaleString()}
                  >
                    {relativeDocsTime(r.received_at)}
                  </time>
                </div>
              </div>
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
                <Button variant="secondary"
                        onClick={() => void declineAutoPair(r)}>
                  Decline
                </Button>
                <Button onClick={() => void approveAutoPair(r)}>
                  Approve &amp; pair
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {/* ── External URL (admin-only; the integration owns it on haos) ── */}
      {isAdmin && !isSupervisorAddon() && <ExternalUrlSection />}

      {/* ── Households ─────────────────────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <div class="sh-section-header__title">
            <h2>{t('connections.households')}</h2>
            {compatOurs.value > 0 && (
              <span class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
                Your protocol version: v{compatOurs.value}
              </span>
            )}
            {peersBehindCount() > 0 && (
              <span class="sh-chip sh-chip--update"
                    aria-label={`${peersBehindCount()} households behind`}>
                {peersBehindCount()} behind
              </span>
            )}
          </div>
          <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
            {confirmed.length > 0 && (
              <Button variant="secondary"
                      loading={autoPairBusy}
                      onClick={() => {
                        setAutoPairBusy(false)
                        openAutoPair(confirmed.map(c => ({
                          instance_id: c.instance_id,
                          display_name: c.display_name,
                        })))
                      }}>
                Pair via a trusted peer
              </Button>
            )}
            <Button onClick={() => openPairing('household')}>
              + {t('connections.pair')}
            </Button>
          </div>
        </div>
        {loading.value ? (
          <Spinner />
        ) : connections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🔗</div>
            <h3>{t('connections.no_connections')}</h3>
            <p class="sh-muted">{t('connections.no_connections_hint')}</p>
            <Button onClick={() => openPairing('household')}>
              {t('connections.start_pairing')}
            </Button>
          </div>
        ) : (
          <div class="sh-connection-list">
            {pending.length > 0 && (
              <div class="sh-connections-pending-hint sh-muted">
                ⏳ {pending.length} pending handshake
                {pending.length === 1 ? '' : 's'} — waiting on the other side.
              </div>
            )}
            {connections.value.map(c => (
              <div key={c.instance_id}
                   class={`sh-connection-card ${c.status === 'confirmed' ? '' : 'sh-connection-card--pending'}`}>
                <div class="sh-connection-info">
                  <span class={hfsStatusDotClass(c)} />
                  <strong>{c.display_name}</strong>
                  {transportIcon(c.transport)}
                  <span class="sh-type-badge">Household</span>
                  {c.status === 'confirmed' && compatBadge(compatByInstance.get(c.instance_id))}
                  {c.status !== 'confirmed' && (
                    <span class="sh-muted">
                      {c.status === 'pending_sent' ? 'Waiting for scan' :
                       c.status === 'pending_received' ? 'Waiting for confirmation' :
                       c.status}
                    </span>
                  )}
                  {c.status === 'confirmed' && !c.reachable && (
                    <span class="sh-muted">Unreachable</span>
                  )}
                </div>
                <div class="sh-connection-actions">
                  {c.status === 'confirmed' && (
                    <>
                      <Button variant="secondary"
                              onClick={() => setDetail(c)}>
                        Manage
                      </Button>
                      <Button variant="danger"
                              onClick={() => void unpair(c.instance_id)}>
                        Unpair
                      </Button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Global Federation Servers ──────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <h2>{t('connections.global_servers')}</h2>
          <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
        </div>
        {gfsLoading.value ? (
          <Spinner />
        ) : gfsConnections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🌐</div>
            <h3>{t('gfs.no_servers')}</h3>
            <p class="sh-muted">{t('gfs.no_servers_hint')}</p>
            <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
          </div>
        ) : (
          <div class="sh-connection-list">
            {gfsConnections.value.map(gfs => {
              const live = gfsLiveState(gfs)
              return (
              <div key={gfs.id} class="sh-connection-card">
                <div class="sh-connection-info">
                  <span class={live.dotClass} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-type-badge">Global Server</span>
                  {live.labelKey && (
                    <span class={live.labelClass ?? 'sh-muted'}>
                      {t(live.labelKey)}
                    </span>
                  )}
                  <span class="sh-muted">{gfs.inbox_url}</span>
                </div>
                <div class="sh-connection-actions">
                  <Button variant="danger"
                          onClick={() => { disconnectTarget.value = gfs }}>
                    {t('gfs.disconnect')}
                  </Button>
                </div>
              </div>
              )
            })}
          </div>
        )}
      </section>
      </>
      )}

      <PairingFlow onGfsConnected={loadGfsConnections} />
      <AutoPairDialog onPaired={() => void loadConnections()} />
      {detail && (
        <ConnectionDetail
          conn={{
            instance_id: detail.instance_id,
            display_name: detail.display_name,
            federated_display_name: detail.federated_display_name,
            local_alias: detail.local_alias ?? null,
            status: detail.status ?? 'confirmed',
            inbox_url: detail.inbox_url ?? '',
            intro_relay_enabled: detail.intro_relay_enabled ?? true,
            unreachable_since: detail.unreachable_since ?? null,
            paired_at: detail.paired_at ?? null,
            transport: detail.transport ?? null,
            proto_version: detail.proto_version,
          }}
          compat={detail ? compatByInstance.get(detail.instance_id) : undefined}
          onClose={() => setDetail(null)}
          onRevoke={() => { setDetail(null); void loadConnections() }}
          onAliasSaved={() => { setDetail(null); void loadConnections() }}
        />
      )}
      <ConfirmDialog
        open={disconnectTarget.value !== null}
        title={t('gfs.disconnect')}
        message={t('gfs.confirm_disconnect')}
        confirmLabel={t('gfs.disconnect')}
        destructive
        onConfirm={() => disconnectTarget.value && disconnectGfs(disconnectTarget.value)}
        onCancel={() => { disconnectTarget.value = null }}
      />
    </div>
  )
}
